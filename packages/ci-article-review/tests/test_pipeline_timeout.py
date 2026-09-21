"""Tests for the review pipeline's per-task wall-clock backstop.

Modelled on ci-style-profile's ``test_call_all_applies_wall_clock_backstop``:
the point of a backstop is that the run stops *waiting*, so these assert the
call returns before the slow work finishes, not merely that a TimeoutError is
eventually raised.
"""

from __future__ import annotations

import threading
import time

from unittest.mock import patch

import pytest

from ci_core.llm import client, cost

import ci_article_review.pipeline as pipeline


class TestRunReviewsInParallel:
    """Exercises ``_run_reviews_in_parallel`` — the pipeline's real fan-out for
    the (model, domain) review calls, not a standalone reimplementation of the
    pattern. It delegates the daemon-thread mechanics to
    :func:`ci_core.concurrency.run_all_with_timeout`, which has its own
    thorough coverage (including the subprocess-level "does not hold the
    interpreter open" regression); these tests are about this function's own
    bookkeeping — result shape, per-model timeout lookup, and that a slow call
    doesn't stall its batch-mates.
    """

    def test_returns_result_when_under_budget(self):
        runners = [("claude:accuracy", lambda: {"ok": True})]
        results = pipeline._run_reviews_in_parallel(runners, {}, {}, task_timeout=5)
        assert results["claude:accuracy"] == {"ok": True}

    def test_propagates_exceptions_from_the_task(self):
        def _boom():
            raise ValueError("adapter blew up")

        runners = [("claude:accuracy", _boom)]
        results = pipeline._run_reviews_in_parallel(runners, {}, {}, task_timeout=5)
        assert results["claude:accuracy"]["failed"] is True
        assert "adapter blew up" in results["claude:accuracy"]["error"]

    def test_gives_up_before_the_slow_call_completes(self):
        """The backstop must bound wall-clock time, not just detect the overrun.

        Under streaming the socket timeout is only the inter-token read gap, so
        a model that keeps dribbling tokens has nothing else to stop it — the
        thread backstop has to be what returns control, not the call itself
        finishing.
        """
        # The slow call blocks on an event this test releases, rather than on a
        # fixed sleep. Two reasons, and speed is the lesser one: a `sleep(2)`
        # made `assert not finished.is_set()` below a statement about timing
        # (it could pass because 2s had not elapsed yet), where a gate makes it
        # a statement about fact — the call *cannot* have completed. It also
        # means the test spends the 0.2s it is measuring instead of the 2s it
        # was waiting out at the end.
        release = threading.Event()
        finished = threading.Event()

        def _slow():
            release.wait(timeout=5)
            finished.set()
            return {"failed": False}

        runners = [("claude:accuracy", _slow)]
        model_configs = {"claude": {"timeout_seconds": 0.2}}

        started = time.monotonic()
        results = pipeline._run_reviews_in_parallel(
            runners, {}, model_configs, task_timeout=5
        )
        gave_up_after = time.monotonic() - started

        assert gave_up_after < 1.5
        assert results["claude:accuracy"]["failed"] is True
        assert "timed out after 0.2s" in results["claude:accuracy"]["error"].lower()
        # The abandoned call is genuinely still running — that is the accepted
        # tradeoff (a running thread cannot be killed), not a leak. Because it
        # runs on a daemon thread, it cannot hold the interpreter open either
        # (see ci_core.concurrency for the regression test proving that).
        assert not finished.is_set()
        release.set()
        assert finished.wait(timeout=5), "the abandoned call never resumed"

    def test_one_slow_task_does_not_hold_up_the_batch(self):
        """Shape of the pipeline's parallel block: fast passes are not blocked.

        Previously the outer executor's own ``with`` exit joined its workers —
        including ones that had already been given up on — so a backstop that
        only *detected* the overrun would stall the whole batch until the
        slowest call finished.
        """

        def _slow():
            time.sleep(2)
            return {"failed": False}

        runners = [
            ("claude:accuracy", _slow),
            ("openai:structure", lambda: {"failed": False}),
        ]
        model_configs = {"claude": {"timeout_seconds": 0.2}}

        started = time.monotonic()
        results = pipeline._run_reviews_in_parallel(
            runners, {}, model_configs, task_timeout=5
        )
        batch_elapsed = time.monotonic() - started

        assert batch_elapsed < 1.5
        assert results["openai:structure"] == {"failed": False}
        assert results["claude:accuracy"]["failed"] is True
        assert "timed out" in results["claude:accuracy"]["error"].lower()

    def test_an_explicit_null_task_timeout_does_not_crash(self):
        """``task_timeout_seconds: null`` in user.yaml is a real, reachable
        value — config_loader's 180 default only fires when the key is
        *absent*, not when it is present and null, which is exactly the shape
        of the PR #105 regression. Computing this runner's own timeout must
        not raise trying to add a stagger offset to None; it falls back to
        being bound by the group's global ceiling instead (see
        ci_core.concurrency.run_all_with_timeout)."""
        runners = [("claude:accuracy", lambda: {"ok": True})]
        results = pipeline._run_reviews_in_parallel(runners, {}, {}, task_timeout=None)
        assert results["claude:accuracy"] == {"ok": True}


class TestRecoverFailedCalls:
    """``_recover_failed_calls`` — the automatic gap-filling pass that
    re-attempts calls still marked failed after the main ensemble batch, so a
    28/30 run doesn't cost the same as a clean one and require a full re-run
    to fill the last 2.
    """

    def _cfg(self, **overrides):
        cfg = {"recovery_passes": 1, "recovery_delay_seconds": 0}
        cfg.update(overrides)
        return cfg

    def test_a_call_that_recovers_is_recorded_as_successful(self):
        attempts = []

        def _now_succeeds():
            attempts.append(1)
            return {"failed": False}

        raw_results = {
            "claude:accuracy": {
                "failed": True,
                "error": "stream stalled mid-stream: nothing received for 120.0s",
            }
        }
        runners = [("claude:accuracy", _now_succeeds)]

        result = pipeline._recover_failed_calls(
            raw_results, runners, self._cfg(), {}, task_timeout=5
        )

        assert len(attempts) == 1
        assert result["claude:accuracy"]["failed"] is False

    def test_a_permanent_failure_is_never_retried(self):
        attempts = []

        def _dead_account():
            attempts.append(1)
            return {"failed": False}

        raw_results = {"openai:accuracy": {"failed": True, "error": "invalid api key"}}
        runners = [("openai:accuracy", _dead_account)]

        result = pipeline._recover_failed_calls(
            raw_results, runners, self._cfg(), {}, task_timeout=5
        )

        assert attempts == []
        assert result["openai:accuracy"]["error"] == "invalid api key"

    def test_recovery_passes_zero_disables_recovery(self):
        attempts = []

        def _would_succeed():
            attempts.append(1)
            return {"failed": False}

        raw_results = {"claude:accuracy": {"failed": True, "error": "stream stalled"}}
        runners = [("claude:accuracy", _would_succeed)]

        result = pipeline._recover_failed_calls(
            raw_results, runners, self._cfg(recovery_passes=0), {}, task_timeout=5
        )

        assert attempts == []
        assert result["claude:accuracy"]["failed"] is True

    def test_persistent_failure_exhausts_both_passes_and_is_not_retried_a_third_time(
        self,
    ):
        attempts = []

        def _always_fails():
            attempts.append(1)
            return {"failed": True, "error": "stream stalled mid-stream"}

        raw_results = {
            "claude:accuracy": {"failed": True, "error": "stream stalled mid-stream"}
        }
        runners = [("claude:accuracy", _always_fails)]

        result = pipeline._recover_failed_calls(
            raw_results, runners, self._cfg(recovery_passes=2), {}, task_timeout=5
        )

        assert len(attempts) == 2
        assert result["claude:accuracy"]["failed"] is True

    def test_recovery_does_not_touch_calls_that_already_succeeded(self):
        attempts = []

        def _should_never_run():
            attempts.append(1)
            return {"failed": False}

        raw_results = {
            "claude:accuracy": {"failed": False, "data": {"flags": []}},
            "openai:structure": {"failed": True, "error": "stream stalled"},
        }
        runners = [
            ("claude:accuracy", _should_never_run),
            ("openai:structure", lambda: {"failed": False}),
        ]

        result = pipeline._recover_failed_calls(
            raw_results, runners, self._cfg(), {}, task_timeout=5
        )

        assert attempts == []
        assert result["claude:accuracy"] == {"failed": False, "data": {"flags": []}}
        assert result["openai:structure"]["failed"] is False

    # The stalls a recovered call survived are the observations stream timing
    # exists for; recovery used to replace them with the healthy retry.
    _STALLED = {
        "first_byte_s": 120.02,
        "first_byte_censored": True,
        "cut_short_by": "StreamStalled",
    }
    _ANSWERED = {"first_byte_s": 14.5, "first_byte_censored": False}

    def test_a_recovered_call_keeps_the_streams_that_stalled(self):
        """2026-09-09, maximum: seven calls lost both streams to stalls under
        the full fan-out and recovery got all seven back. Replaced as-is, the
        report said none of them had ever stalled."""
        raw_results = {
            "claude:accuracy": {
                "failed": True,
                "error": "stream stalled before the first chunk",
                "stream_timing": [dict(self._STALLED), dict(self._STALLED)],
            }
        }
        runners = [
            (
                "claude:accuracy",
                lambda: {"failed": False, "stream_timing": [dict(self._ANSWERED)]},
            )
        ]

        result = pipeline._recover_failed_calls(
            raw_results, runners, self._cfg(), {}, task_timeout=5
        )

        streams = result["claude:accuracy"]["stream_timing"]
        assert len(streams) == 3
        # Oldest first, and marked: they ran under a heavier fan-out than the
        # stream that finally answered, and fan-out is what is being measured.
        assert [s.get("before_recovery", False) for s in streams] == [
            True,
            True,
            False,
        ]
        assert streams[0]["first_byte_censored"] is True
        assert streams[2] == self._ANSWERED

    def test_streams_accumulate_across_recovery_passes(self):
        passes = []

        def _fails_then_answers():
            passes.append(1)
            if len(passes) == 1:
                return {
                    "failed": True,
                    "error": "stream stalled",
                    "stream_timing": [dict(self._STALLED)],
                }
            return {"failed": False, "stream_timing": [dict(self._ANSWERED)]}

        raw_results = {
            "claude:accuracy": {
                "failed": True,
                "error": "stream stalled",
                "stream_timing": [dict(self._STALLED)],
            }
        }

        result = pipeline._recover_failed_calls(
            raw_results,
            [("claude:accuracy", _fails_then_answers)],
            self._cfg(recovery_passes=2),
            {},
            task_timeout=5,
        )

        streams = result["claude:accuracy"]["stream_timing"]
        assert [s.get("before_recovery", False) for s in streams] == [
            True,
            True,
            False,
        ]

    def test_a_failure_with_no_streams_recovers_as_before(self):
        """A call abandoned at the wall-clock budget never returned its timing;
        there is nothing to carry, and nothing to crash on."""
        raw_results = {
            "claude:accuracy": {"failed": True, "error": "timed out after 420s"}
        }
        answered = {"failed": False, "stream_timing": [dict(self._ANSWERED)]}

        result = pipeline._recover_failed_calls(
            raw_results,
            [("claude:accuracy", lambda: dict(answered))],
            self._cfg(),
            {},
            task_timeout=5,
        )

        # Its billing is carried all the same — see
        # test_a_call_abandoned_at_its_budget_is_counted_but_not_priced.
        recovered = dict(result["claude:accuracy"])
        recovered.pop("discarded_attempts")
        assert recovered == answered

    # What a failed dispatch was billed for, which recovery used to replace
    # along with it. Shaped as `_attempt` returns a call whose two attempts
    # both failed: the first already summarised as discarded, the last as the
    # result's own `tokens`.
    _STALL = "stream stalled before the first chunk: nothing received for 120.0s"
    _NO_TOKENS = {"prompt": 0, "completion": 0}

    def _stalled_twice(self):
        return {
            "failed": True,
            "error": self._STALL,
            "model": "gpt-5.6-sol",
            "tokens": dict(self._NO_TOKENS),
            "discarded_attempts": {
                "count": 1,
                "costed": 0,
                "reasons": ["StreamStalled"],
                "tokens": dict(self._NO_TOKENS),
            },
        }

    def _malformed_twice(self, **last_tokens):
        return {
            "failed": True,
            "error": "Malformed JSON response",
            "model": "gpt-5.6-sol",
            "tokens": {"prompt": 6107, "completion": 9000, **last_tokens},
            "discarded_attempts": {
                "count": 1,
                "costed": 1,
                "reasons": ["MalformedJSONError"],
                "tokens": {"prompt": 6107, "completion": 8000},
            },
        }

    def _answer(self, **extra):
        return {
            "failed": False,
            "model": "gpt-5.6-sol",
            "tokens": {"prompt": 6107, "completion": 18097},
            **extra,
        }

    def _recover(self, failed, *replies, passes=1):
        """Recover one failed call, its runner returning ``replies`` in turn."""
        queue = list(replies)
        return pipeline._recover_failed_calls(
            {"openai:red_team": failed},
            [("openai:red_team", lambda: queue.pop(0))],
            self._cfg(recovery_passes=passes),
            {},
            task_timeout=5,
        )["openai:red_team"]

    @staticmethod
    def _cost(result):
        """``cost_summary`` for a run whose only call is ``result``."""
        return cost.calculate([cost.call_log_entry("openai:red_team", result)])

    def test_a_recovered_call_keeps_the_attempts_that_stalled(self):
        """2026-09-09, maximum: seven calls stalled on both attempts and
        recovery got all seven back. The report counted 3 unpriced attempts,
        and none of them were these fourteen."""
        recovered = self._recover(self._stalled_twice(), self._answer())

        assert recovered["discarded_attempts"] == {
            "count": 2,
            "costed": 0,
            "reasons": ["StreamStalled"],
            "tokens": self._NO_TOKENS,
        }
        summary = self._cost(recovered)
        assert (summary["discarded_calls"], summary["uncosted_calls"]) == (2, 2)
        # A stall reports no usage, so the dollars cannot move: the count is
        # what says the total is a floor, and by how many attempts.
        assert summary["total_usd"] == self._cost(self._answer())["total_usd"]

    def test_a_malformed_response_stays_in_the_total(self):
        """A response that would not parse was complete, and billed in full.
        Replaced, its tokens left the dollar figure, not just the count."""
        recovered = self._recover(self._malformed_twice(), self._answer())

        summary = self._cost(recovered)
        assert (summary["discarded_calls"], summary["uncosted_calls"]) == (2, 0)
        # Every attempt the provider billed for, priced as one bill.
        every_attempt = cost.calculate(
            [
                {
                    "model": "gpt-5.6-sol",
                    "tokens": {
                        "prompt": 6107 * 3,
                        "completion": 18097 + 9000 + 8000,
                    },
                }
            ]
        )
        assert summary["by_pass"][0]["total_usd"] == pytest.approx(
            every_attempt["by_pass"][0]["total_usd"], abs=1e-6
        )

    def test_a_failed_attempts_cached_input_keeps_its_rate(self):
        """The client's own summary keeps no cached share, so cost.py bills a
        discarded attempt's input at the full rate. The last attempt's share
        is known, and is carried."""
        recovered = self._recover(self._malformed_twice(cached=6104), self._answer())

        assert recovered["discarded_attempts"]["tokens"] == {
            "prompt": 6107 * 2,
            "completion": 9000 + 8000,
            "cached": 6104,
        }

    def test_the_replacements_own_retry_is_kept_alongside(self):
        """A recovery attempt can need a retry of its own: grok:red_team's did,
        on the 2026-09-09 maximum run. Both calls' billing stands, under both
        reasons."""
        own_retry = {
            "count": 1,
            "costed": 1,
            "reasons": ["MalformedJSONError"],
            "tokens": {"prompt": 6107, "completion": 7000},
        }
        recovered = self._recover(
            self._stalled_twice(), self._answer(discarded_attempts=own_retry)
        )

        assert recovered["discarded_attempts"] == {
            "count": 3,
            "costed": 1,
            "reasons": ["MalformedJSONError", "StreamStalled"],
            "tokens": {"prompt": 6107, "completion": 7000},
        }

    def test_billing_accumulates_across_recovery_passes(self):
        recovered = self._recover(
            self._stalled_twice(),
            self._stalled_twice(),
            self._answer(),
            passes=2,
        )

        # Two failed calls of two stalled attempts each, then the answer.
        assert recovered["failed"] is False
        assert recovered["discarded_attempts"]["count"] == 4
        assert recovered["discarded_attempts"]["costed"] == 0

    def test_a_call_still_failing_carries_what_came_before_it(self):
        """2026-09-09, thorough: grok:red_team stalled on both attempts in the
        main pass and on both again in recovery. The report kept the second
        failure, and counted none of the first."""
        recovered = self._recover(self._stalled_twice(), self._stalled_twice())

        assert recovered["failed"] is True
        # The main pass's two attempts and the recovery's discarded one. The
        # recovery's last attempt is the entry's own, as on any failed call.
        assert recovered["discarded_attempts"]["count"] == 3
        assert self._cost(recovered)["uncosted_calls"] == 3

    def test_a_call_abandoned_at_its_budget_is_counted_but_not_priced(self):
        """The pipeline stopped waiting at its wall-clock budget; nothing told
        the provider to stop generating, and no usage ever came back."""
        abandoned = {
            "failed": True,
            "error": "Timed out after 420s",
            "model": "gpt-5.6-sol",
            "tokens": {},
        }
        recovered = self._recover(abandoned, self._answer())

        assert recovered["discarded_attempts"] == {
            "count": 1,
            "costed": 0,
            "reasons": ["TimeoutError"],
            "tokens": self._NO_TOKENS,
        }

    def test_a_replaced_grounded_calls_searches_stay_billed(self):
        """A malformed-JSON failure on a grounded call had searched before it
        failed, and each of its attempts was billed for that. Recovery
        replaced the result and, with it, the only record of the searches."""
        failed = self._malformed_twice()
        failed["searches"] = 3
        failed["discarded_attempts"]["searches"] = [4]
        own_retry = {
            "count": 1,
            "costed": 1,
            "reasons": ["MalformedJSONError"],
            "tokens": {"prompt": 6107, "completion": 7000},
            "searches": [2],
        }
        recovered = self._recover(
            failed, self._answer(searches=1, discarded_attempts=own_retry)
        )

        # One count per attempt, in order: the failed call's discarded one,
        # its own last, then the replacement's retry.
        assert recovered["discarded_attempts"]["searches"] == [4, 3, 2]
        assert recovered["searches"] == 1
        summary = self._cost(recovered)
        assert summary["search_units"] == 4 + 3 + 2 + 1
        assert summary["total_search_usd"] == 0.1

    def test_a_replaced_call_that_could_not_search_adds_no_count(self):
        recovered = self._recover(self._stalled_twice(), self._answer())
        assert "searches" not in recovered["discarded_attempts"]


class TestCaptureCouldHaveSearched:
    """Which calls in a capture from before PR #230 may have paid search fees.

    Those captures record no count, so a replay prices none. Whether that
    makes its total a floor is decided per provider, not by
    ``grounding_available``: gemini decides per prompt whether to search, and
    litellm can drop its grounding metadata on some stream shapes, so a gemini
    result reading ungrounded does not prove no search ran.
    """

    _ANSWERED = {"tokens": {"prompt": 900, "completion": 300}}

    @pytest.mark.parametrize("provider", ["gemini", "perplexity"])
    def test_a_provider_that_always_could_search(self, provider):
        result = dict(self._ANSWERED, grounding_available=False)
        assert pipeline._capture_could_have_searched(provider, result) is True

    @pytest.mark.parametrize("provider", ["claude", "openai"])
    def test_a_configured_search_counts_only_where_it_shows(self, provider):
        grounded = dict(self._ANSWERED, grounding_available=True)
        plain = dict(self._ANSWERED, grounding_available=False)
        assert pipeline._capture_could_have_searched(provider, grounded) is True
        assert pipeline._capture_could_have_searched(provider, plain) is False

    def test_a_provider_that_cannot_search(self):
        assert pipeline._capture_could_have_searched("mistral", self._ANSWERED) is False

    def test_a_call_that_never_answered_billed_nothing(self):
        dead = {"failed": True, "tokens": {"prompt": 0, "completion": 0}}
        assert pipeline._capture_could_have_searched("gemini", dead) is False

    def test_an_unparseable_answer_was_still_billed(self):
        malformed = dict(self._ANSWERED, failed=True)
        assert pipeline._capture_could_have_searched("perplexity", malformed) is True


class TestFailureReason:
    """``_failure_reason`` — which exception a failed result's text names.

    Every non-empty string below is the ``error`` of a failed call in a saved
    report, apart from the figures in it.
    """

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (
                "stream stalled before the first chunk: nothing received for 120.0s",
                "StreamStalled",
            ),
            ("stream stalled mid-stream: nothing received for 120.0s", "StreamStalled"),
            ("Malformed JSON response", "MalformedJSONError"),
            (
                "litellm.RateLimitError: RateLimitError: MistralException - "
                '{"object":"error","message":"Rate limit exceeded"',
                "RateLimitError",
            ),
            (
                "litellm.MidStreamFallbackError: litellm.APIError: Rate limit "
                "reached for gpt-5.5",
                "MidStreamFallbackError",
            ),
            ("TimeoutError: Timed out after 420s", "TimeoutError"),
            ("Timed out after 420s", "TimeoutError"),
            ("Exceeded global timeout of 1100s", "TimeoutError"),
            (
                "HTTPSConnectionPool(host='api.openai.com', port=443): Read timed "
                "out. (read timeout=120)",
                "TimeoutError",
            ),
            (
                "401 Client Error: Unauthorized for url: "
                "https://api.perplexity.ai/chat/completions",
                "unknown",
            ),
            ("", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_it_reads_the_exception_from_the_text_a_failure_left(self, error, reason):
        assert pipeline._failure_reason(error) == reason

    def test_the_stall_the_client_raises_is_read_as_one(self):
        """The marker is the client's wording, not a contract. Pinned to what
        the client actually raises, so rewording it cannot quietly file every
        stall under ``unknown``."""
        release = threading.Event()

        def _silent():
            release.wait(timeout=5)
            yield from ()

        try:
            with pytest.raises(client.StreamStalled) as stalled:
                list(client._iter_with_gap(_silent(), first_byte=0.05, gap=30.0))
        finally:
            release.set()
        assert pipeline._failure_reason(str(stalled.value)) == "StreamStalled"


class TestMergeRecoveredResults:
    """``_merge_recovered_results`` — the ``--retry-failed`` merge step."""

    def test_previously_successful_entries_pass_through_unchanged(self):
        prior = {"claude:accuracy": {"failed": False, "data": {"flags": []}}}
        merged = pipeline._merge_recovered_results(prior, {})
        assert merged == prior

    def test_previously_failed_entry_is_replaced_by_the_new_attempt(self):
        prior = {"openai:structure": {"failed": True, "error": "stream stalled"}}
        retried = {"openai:structure": {"failed": False, "data": {}}}
        merged = pipeline._merge_recovered_results(prior, retried)
        assert merged["openai:structure"] == {"failed": False, "data": {}}

    def test_a_retry_that_fails_again_still_replaces_the_stale_failure(self):
        prior = {"openai:structure": {"failed": True, "error": "stream stalled"}}
        retried = {"openai:structure": {"failed": True, "error": "still stalled"}}
        merged = pipeline._merge_recovered_results(prior, retried)
        assert merged["openai:structure"]["error"] == "still stalled"

    def test_untouched_entries_from_prior_survive_a_partial_retry(self):
        prior = {
            "claude:accuracy": {"failed": False, "data": {}},
            "openai:structure": {"failed": True, "error": "stream stalled"},
        }
        retried = {"openai:structure": {"failed": False, "data": {}}}
        merged = pipeline._merge_recovered_results(prior, retried)
        assert merged["claude:accuracy"] == {"failed": False, "data": {}}
        assert merged["openai:structure"]["failed"] is False

    def test_a_prior_runs_streams_are_not_carried_into_this_one(self):
        """Unlike in-run recovery: the capture's stalls are already in the
        report of the run that captured them, and carrying them here would
        count them twice across reports."""
        prior = {
            "openai:structure": {
                "failed": True,
                "error": "stream stalled",
                "stream_timing": [{"first_byte_s": 120.0, "first_byte_censored": True}],
            }
        }
        retried = {"openai:structure": {"failed": False, "stream_timing": []}}
        merged = pipeline._merge_recovered_results(prior, retried)
        assert merged["openai:structure"]["stream_timing"] == []

    _PRIOR_BILLING = {
        "count": 5,
        "costed": 0,
        "reasons": ["StreamStalled"],
        "tokens": {"prompt": 0, "completion": 0},
    }

    def test_a_prior_runs_billing_is_not_carried_into_this_one(self):
        """The same double count, in money: the capture's attempts are in the
        cost summary of the run that bought them."""
        prior = {
            "openai:structure": {
                "failed": True,
                "error": "stream stalled",
                "tokens": {"prompt": 0, "completion": 0},
                "discarded_attempts": dict(self._PRIOR_BILLING),
            }
        }
        retried = {"openai:structure": {"failed": False, "tokens": {"prompt": 9}}}
        merged = pipeline._merge_recovered_results(prior, retried)
        assert "discarded_attempts" not in merged["openai:structure"]

    def test_recovery_after_a_retry_carries_only_what_this_run_bought(self):
        """``--retry-failed`` runs the recovery pass too. By then every failed
        entry recovery can retry is one this run made — the merge already
        replaced the capture's — so the billing it carries is this run's."""
        prior = {
            "openai:structure": {
                "failed": True,
                "error": "stream stalled",
                "discarded_attempts": dict(self._PRIOR_BILLING),
            }
        }
        this_run = {
            "openai:structure": {
                "failed": True,
                "error": "stream stalled mid-stream: nothing received for 120.0s",
                "tokens": {"prompt": 0, "completion": 0},
            }
        }
        merged = pipeline._merge_recovered_results(prior, this_run)

        result = pipeline._recover_failed_calls(
            merged,
            [("openai:structure", lambda: {"failed": False})],
            {"recovery_passes": 1, "recovery_delay_seconds": 0},
            {},
            task_timeout=5,
        )

        assert result["openai:structure"]["discarded_attempts"]["count"] == 1


class TestCalibrationTiming:
    """The ``first_byte=`` / ``max_gap=`` values on a [CALIBRATION] line."""

    def test_a_clean_stream_prints_its_two_measurements(self):
        streams = [
            {
                "first_byte_s": 14.5,
                "first_byte_censored": False,
                "max_gap_s": 3.25,
                "max_gap_censored": False,
            }
        ]
        assert pipeline._calibration_timing(streams) == ("14.5s", "3.25s")

    def test_a_censored_value_is_written_as_a_lower_bound(self):
        """Never as the budget presented as a measurement: a dataset that did
        that would cap out at exactly the current setting and confirm it."""
        first_byte_stall = {
            "first_byte_s": 120.02,
            "first_byte_censored": True,
            "max_gap_s": None,
            "max_gap_censored": False,
        }
        gap_stall = {
            "first_byte_s": 30.0,
            "first_byte_censored": False,
            "max_gap_s": 120.01,
            "max_gap_censored": True,
        }
        assert pipeline._calibration_timing([first_byte_stall]) == (">120.02s", "-")
        assert pipeline._calibration_timing([gap_stall]) == ("30.0s", ">120.01s")

    def test_every_stream_is_listed_oldest_first(self):
        """A stall the retry got past stays visible on a line reading ok."""
        streams = [
            {"first_byte_s": 120.02, "first_byte_censored": True, "max_gap_s": None},
            {"first_byte_s": 41.2, "first_byte_censored": False, "max_gap_s": 2.5},
        ]
        assert pipeline._calibration_timing(streams) == (
            ">120.02s,41.2s",
            "-,2.5s",
        )

    def test_a_call_with_nothing_recorded_prints_dashes(self):
        """Replays of older captures, and calls abandoned at the wall clock."""
        assert pipeline._calibration_timing(None) == ("-", "-")
        assert pipeline._calibration_timing([]) == ("-", "-")
        # A hand-edited capture must not crash the line.
        assert pipeline._calibration_timing(["junk", None]) == ("-", "-")


class TestSameProviderStagger:
    """A provider's own calls must not all fire in the same instant.

    Rate limits are per account, not per call, so five simultaneous Perplexity
    requests compete for one quota. Observed 2026-08-12: two returned HTTP 429
    within one second of each other and one failed outright after its retry also
    hit the limit, which then cost Section 9 all of its grounded-search URLs.
    """

    def test_calls_to_one_provider_are_spread(self):
        names = [
            f"perplexity:{d}"
            for d in (
                "fact_check",
                "voice_style",
                "completeness",
                "argument_integrity",
                "red_team",
            )
        ]
        offsets = pipeline._stagger_offsets(names, 3)
        assert sorted(offsets.values()) == [0, 3, 6, 9, 12]

    def test_different_providers_still_start_together(self):
        """The parallelism that matters is across providers, not within one."""
        names = ["perplexity:fact_check", "gemini:fact_check", "grok:fact_check"]
        assert set(pipeline._stagger_offsets(names, 3).values()) == {0}

    def test_stagger_of_zero_disables_it(self):
        names = [f"perplexity:{d}" for d in ("a", "b", "c")]
        assert set(pipeline._stagger_offsets(names, 0).values()) == {0}

    def test_delay_start_is_a_passthrough_at_zero(self):
        """No offset must mean no wrapper, so the common case pays nothing."""
        fn = lambda: "ran"  # noqa: E731
        assert pipeline._delay_start(fn, 0) is fn

    def test_delay_start_sleeps_then_runs(self):
        slept = []
        fn = pipeline._delay_start(lambda: "ran", 6)
        with patch.object(pipeline.time, "sleep", side_effect=slept.append):
            assert fn() == "ran"
        assert slept == [6]
