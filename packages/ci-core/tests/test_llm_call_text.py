"""Tests for the ci_core.llm dispatch layer and call_text.

``call_provider`` is a thin name-based dispatch onto the litellm shim.
``call_text`` sits on top of it for callers that want the assembled response
text rather than the shim's JSON-parsing verdict — ci-style-profile parses the
output itself across several passes and feeds some of it back into a later
prompt verbatim, so a prose reply is usable to it rather than fatal.

The distinction that matters: a *malformed JSON* failure carrying text is not a
failure to those callers, but a transport failure still is. Collapsing the two
would feed an HTTP error body into a synthesis prompt as if it were content.
"""

from unittest.mock import patch

import pytest

from ci_core import llm


def _shim_result(**overrides):
    result = {
        "failed": False,
        "raw": '{"a": 1}',
        "data": {"a": 1},
        "model": "some-model",
        "tokens": {"prompt": 10, "completion": 5, "cached": 0},
        "elapsed_seconds": 1.5,
    }
    result.update(overrides)
    return result


class TestCallProvider:
    def test_dispatches_by_name_and_passes_arguments_through(self):
        with patch.object(llm.client, "call", return_value=_shim_result()) as shim:
            out = llm.call_provider(
                "claude",
                "sys",
                "user",
                "key",
                retry=False,
                retry_delay=3,
                model="claude-opus-4-8",
                provider_config={"effort": "high"},
            )

        assert out == _shim_result()
        args, kwargs = shim.call_args
        assert args[0] == "claude"
        assert args[1:4] == ("sys", "user", "key")
        assert kwargs["retry"] is False
        assert kwargs["retry_delay"] == 3
        assert kwargs["model"] == "claude-opus-4-8"
        assert kwargs["provider_config"] == {"effort": "high"}

    def test_result_is_returned_unchanged(self):
        payload = _shim_result(citations=["https://epa.gov"], truncated=True)
        with patch.object(llm.client, "call", return_value=payload):
            assert llm.call_provider("perplexity", "s", "u", "k") == payload

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            llm.call_provider("nope", "s", "u", "k")

    def test_every_advertised_provider_is_routable(self):
        """PROVIDERS is what the pipeline validates model names against; a name
        listed there that the shim cannot route is a config error at runtime."""
        for name in llm.PROVIDERS:
            with patch.object(llm.client, "call", return_value=_shim_result()):
                assert llm.call_provider(name, "s", "u", "k")["failed"] is False


class TestCallText:
    @pytest.mark.parametrize("searches", [0, 2, None])
    def test_a_grounded_calls_search_count_travels_with_its_tokens(self, searches):
        """ci-style-profile bills from what this returns, and gemini grounds
        every call it makes. 0 and None both mean something."""
        with patch.object(
            llm.client, "call", return_value=_shim_result(searches=searches)
        ):
            out = llm.call_text("gemini", "s", "u", "k")

        assert out["searches"] == searches

    def test_a_call_that_could_not_search_has_no_count(self):
        with patch.object(llm.client, "call", return_value=_shim_result()):
            out = llm.call_text("mistral", "s", "u", "k")

        assert "searches" not in out

    def test_successful_call_returns_text(self):
        with patch.object(llm.client, "call", return_value=_shim_result()):
            out = llm.call_text("mistral", "s", "u", "k")

        assert out["failed"] is False
        assert out["content"] == '{"a": 1}'
        assert out["tokens"] == {"prompt": 10, "completion": 5, "cached": 0}
        assert out["elapsed"] == 1.5
        assert out["model"] == "some-model"

    def test_malformed_json_with_text_is_rescued_as_success(self):
        """The shim calls a prose reply a failure because the review pipeline
        needs structured findings. Style synthesis does its own parsing, so the
        text is the answer, not the error."""
        prose = _shim_result(
            failed=True,
            error="Malformed JSON response",
            raw="The author's voice is direct and unhedged.",
            data=None,
        )
        with patch.object(llm.client, "call", return_value=prose):
            out = llm.call_text("claude", "s", "u", "k")

        assert out["failed"] is False
        assert out["content"] == "The author's voice is direct and unhedged."

    def test_malformed_json_without_text_stays_failed(self):
        """Nothing came back. There is no content to rescue."""
        empty = _shim_result(
            failed=True, error="Malformed JSON response", raw="", data=None
        )
        with patch.object(llm.client, "call", return_value=empty):
            out = llm.call_text("claude", "s", "u", "k")

        assert out["failed"] is True

    @pytest.mark.parametrize(
        "error",
        [
            "HTTP 401",
            "Timed out after 300s",
            "Connection aborted",
        ],
    )
    def test_transport_failures_stay_failed(self, error):
        """Rescuing these would feed an HTTP error body into the next prompt as
        if a model had written it."""
        failure = _shim_result(
            failed=True, error=error, raw="", data=None, error_body="upstream said no"
        )
        with patch.object(llm.client, "call", return_value=failure):
            out = llm.call_text("grok", "s", "u", "k")

        assert out["failed"] is True
        assert out["error"] == error
        assert out["error_body"] == "upstream said no"

    def test_transport_failure_carrying_text_stays_failed(self):
        """A partial body received before the socket dropped is not an answer."""
        partial = _shim_result(
            failed=True, error="Connection aborted", raw='{"partial": tru', data=None
        )
        with patch.object(llm.client, "call", return_value=partial):
            out = llm.call_text("grok", "s", "u", "k")

        assert out["failed"] is True

    def test_model_falls_back_to_the_requested_name(self):
        """The cost log is keyed on this; a blank model bills at unknown_price."""
        with patch.object(llm.client, "call", return_value=_shim_result(model=None)):
            out = llm.call_text("grok", "s", "u", "k", model="grok-4.3")

        assert out["model"] == "grok-4.3"
