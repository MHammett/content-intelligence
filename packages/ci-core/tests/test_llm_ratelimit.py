"""The tokens-per-minute meter (issue #361).

A review lost 5 citation relevance checks to Mistral 429s because the ensemble's
Mistral calls, finished seconds before, had spent the key's 100,000 tokens a
minute. These tests hold the meter to that: a call waits for the window to
drain, only where a limit is set, and a refused call does not count against it.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ci_core.llm import client, ratelimit


def _usage(prompt=100, completion=50):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=None,
        cache_read_input_tokens=None,
    )


def _chunk(content=None, finish_reason=None, usage=None):
    choices = (
        [
            SimpleNamespace(
                delta=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
        if content or finish_reason
        else []
    )
    return SimpleNamespace(
        choices=choices,
        usage=usage,
        citations=None,
        search_results=None,
        vertex_ai_grounding_metadata=None,
    )


def _completion_stream(text='{"flags": []}', usage=None):
    chunks = [_chunk(content=piece) for piece in text]
    chunks.append(_chunk(finish_reason="stop"))
    chunks.append(_chunk(usage=usage if usage is not None else _usage()))
    return chunks


def _http_error(status):
    exc = Exception(f"HTTP {status}")
    exc.status_code = status
    exc.response = SimpleNamespace(status_code=status, text="")
    return exc


def _call(provider="mistral", **kwargs):
    defaults = dict(
        system_prompt="sys",
        user_prompt="user",
        api_key="key",
        retry=False,
        retry_delay=0,
    )
    defaults.update(kwargs)
    return client.call(provider, **defaults)


class _Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def namespace(self):
        return SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep)


@pytest.fixture
def clock():
    fake = _Clock()
    with patch.object(ratelimit, "time", fake.namespace()):
        yield fake


@pytest.fixture(autouse=True)
def _no_limits():
    ratelimit.configure(None)
    yield
    ratelimit.configure(None)


def _limit(tokens_per_minute, provider="mistral"):
    ratelimit.configure({provider: {"tokens_per_minute": tokens_per_minute}})


class TestMeter:
    def test_no_limit_means_no_wait_and_no_record(self, clock):
        assert ratelimit.reserve("mistral", 10**9) is None
        assert clock.slept == []

    def test_calls_that_fit_do_not_wait(self, clock):
        _limit(1000)
        ratelimit.reserve("mistral", 400)
        ratelimit.reserve("mistral", 400)
        assert clock.slept == []

    def test_a_full_window_waits_for_the_oldest_call_to_expire(self, clock):
        _limit(1000)
        first = ratelimit.reserve("mistral", 600)
        ratelimit.settle(first, 600, 0)
        clock.now += 10

        ratelimit.reserve("mistral", 600)

        # The first call was 10s old, so 50s of its minute remain.
        assert clock.slept == [pytest.approx(50.0)]

    def test_completion_tokens_count_from_when_the_call_ended(self, clock):
        """The ensemble case: a long call's output lands at its end, not its start."""
        _limit(1000)
        ticket = ratelimit.reserve("mistral", 300)
        clock.now += 40
        ratelimit.settle(ticket, 300, 500)
        clock.now += 15

        ratelimit.reserve("mistral", 500)

        # 800 in use; the prompt is 55s old and the completion 15s old. Room for
        # 500 more needs the completion to go, 45s away; the prompt alone
        # leaving, in 5s, is not enough.
        assert clock.slept == [pytest.approx(45.0)]

    def test_a_call_larger_than_the_budget_goes_when_the_window_is_empty(self, clock):
        _limit(1000)
        ratelimit.reserve("mistral", 5000)
        assert clock.slept == []

    def test_a_call_larger_than_the_budget_waits_for_an_empty_window(self, clock):
        _limit(1000)
        ratelimit.reserve("mistral", 100)
        ratelimit.reserve("mistral", 5000)
        assert clock.slept == [pytest.approx(60.0)]

    def test_released_tokens_are_free_again(self, clock):
        _limit(1000)
        ticket = ratelimit.reserve("mistral", 800)
        ratelimit.release(ticket)
        ratelimit.reserve("mistral", 800)
        assert clock.slept == []

    def test_no_call_waits_past_the_ceiling(self, clock):
        _limit(1000)
        with (
            patch.object(ratelimit, "_WINDOW_SECONDS", 1000.0),
            patch.object(ratelimit, "_MAX_WAIT_SECONDS", 5.0),
        ):
            ratelimit.reserve("mistral", 800)
            ratelimit.reserve("mistral", 800)
        assert sum(clock.slept) == pytest.approx(5.0)

    def test_a_refusal_fills_the_window_for_the_next_caller(self, clock):
        _limit(1000)
        ratelimit.note_refused("mistral")
        ratelimit.reserve("mistral", 1)
        assert clock.slept == [pytest.approx(60.0)]

    def test_a_burst_of_refusals_costs_one_window_not_one_each(self, clock):
        _limit(1000)
        for _ in range(5):
            ratelimit.note_refused("mistral")
        ratelimit.reserve("mistral", 1)
        assert sum(clock.slept) == pytest.approx(60.0)

    def test_a_refusal_is_ignored_where_no_limit_is_set(self, clock):
        ratelimit.note_refused("mistral")
        assert ratelimit.reserve("mistral", 1) is None
        assert clock.slept == []

    def test_other_providers_are_not_metered(self, clock):
        _limit(1000)
        ratelimit.reserve("mistral", 800)
        assert ratelimit.reserve("grok", 800) is None
        assert clock.slept == []

    def test_configure_clears_the_previous_limits(self, clock):
        _limit(1000)
        ratelimit.configure({})
        assert ratelimit.reserve("mistral", 10**6) is None

    @pytest.mark.parametrize("value", [0, -5, "many", None])
    def test_a_bad_limit_is_ignored_with_a_warning(self, value, caplog, clock):
        with caplog.at_level(logging.WARNING):
            ratelimit.configure({"mistral": {"tokens_per_minute": value}})
        assert ratelimit.reserve("mistral", 10**6) is None
        assert "tokens_per_minute" in caplog.text


class TestClientWaitsForRoom:
    """The meter is wired into the one place every model call passes through."""

    def test_a_second_call_waits_for_the_first_ones_tokens_to_drain(self, clock):
        _limit(1000)
        order = []

        def completion(**kwargs):
            order.append(("call", len(clock.slept)))
            return _completion_stream(usage=_usage(prompt=500, completion=300))

        with patch.object(client.litellm, "completion", side_effect=completion):
            _call("mistral", user_prompt="x" * 600)
            _call("mistral", user_prompt="x" * 600)

        # 800 tokens used, 201 estimated for the next: over 900, so it waited,
        # and it waited before the request went out, not during it.
        assert len(clock.slept) == 1
        assert clock.slept[0] > 0
        assert order == [("call", 0), ("call", 1)]

    def test_no_limit_configured_changes_nothing(self, clock):
        with patch.object(
            client.litellm,
            "completion",
            side_effect=lambda **kw: _completion_stream(
                usage=_usage(prompt=10**6, completion=10**6)
            ),
        ):
            _call("mistral", user_prompt="x" * 600)
            _call("mistral", user_prompt="x" * 600)
        assert clock.slept == []

    def test_a_refused_call_sends_the_next_one_to_wait_out_the_window(self, clock):
        """A 429 is the provider saying the window is full, whatever the meter thought.

        The estimate can be wrong and the key can be shared with another process,
        neither of which the meter sees. A refusal is the one measurement of the
        provider's own count, so the next call waits a window.
        """
        _limit(1000)
        with patch.object(client.litellm, "completion", side_effect=_http_error(429)):
            result = _call("mistral", user_prompt="x" * 30)
        assert result["failed"] is True

        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            _call("mistral", user_prompt="x" * 30)

        assert sum(clock.slept) == pytest.approx(60.0, abs=1.0)

    def test_a_call_that_fails_some_other_way_still_counts(self, clock):
        """A stream that died part-way may have been billed; keep its reservation."""
        _limit(1000)
        with patch.object(client.litellm, "completion", side_effect=_http_error(500)):
            _call("mistral", user_prompt="x" * 2000)

        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            _call("mistral", user_prompt="x" * 2000)

        assert len(clock.slept) == 1
