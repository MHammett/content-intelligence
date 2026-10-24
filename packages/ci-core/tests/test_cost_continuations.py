"""A continuation call is billed as a call of its own.

A pass cut off at its output ceiling gets one follow-up call for the buckets it
never delivered (ci-article-review's ``pipeline._continue_truncated_passes``).
That call is recorded under the entry it continued, in ``continuations``, so the
report keeps one entry per (model, domain) — which makes it the same trap
``discarded_attempts`` was: a field the summary does not read is spend that
vanishes from the total. These tests hold every part of it to the rules the
entry's own call already follows.
"""

import pytest

from ci_core.llm import cost
from ci_core.llm.cost import calculate, call_log_entry

_MODEL = "mistral-small-latest"


def _tokens_usd(model, prompt, completion):
    """The table's price for these tokens, so no rate is restated here."""
    in_p, out_p = cost._price_for_model(model)[:2]
    return (prompt * in_p + completion * out_p) / 1_000_000


def _entry(**extra):
    return {
        "pass": "mistral:fact_check",
        "model": _MODEL,
        "failed": False,
        "tokens": {"prompt": 10_000, "completion": 2_000},
        **extra,
    }


def _continuation(**extra):
    return {
        "buckets_requested": ["confirmed", "additional_observations"],
        "model": _MODEL,
        "tokens": {"prompt": 10_500, "completion": 800},
        "truncated": False,
        "failed": False,
        **extra,
    }


class TestAContinuationIsPriced:
    def test_its_tokens_are_added_to_the_entry_and_the_total(self):
        summary = calculate([_entry(continuations=[_continuation()])])
        expected = _tokens_usd(_MODEL, 10_000, 2_000) + _tokens_usd(_MODEL, 10_500, 800)
        assert summary["total_usd"] == pytest.approx(expected, abs=1e-4)
        (row,) = summary["by_pass"]
        assert row["total_usd"] == pytest.approx(expected, abs=1e-6)
        assert row["continuation_usd"] == pytest.approx(
            _tokens_usd(_MODEL, 10_500, 800), abs=1e-6
        )
        assert summary["continuation_calls"] == 1
        assert summary["continuation_usd"] == pytest.approx(
            _tokens_usd(_MODEL, 10_500, 800), abs=1e-4
        )
        assert summary["incurred_usd"] == summary["total_usd"]

    def test_an_entry_without_one_reports_none(self):
        summary = calculate([_entry()])
        assert summary["continuation_calls"] == 0
        assert summary["continuation_usd"] == 0.0
        assert "continuation_usd" not in summary["by_pass"][0]

    def test_it_is_priced_at_the_model_that_answered_it(self):
        """A fallback can answer the follow-up and not the first call."""
        other = "mistral-large-latest"
        assert cost.known_price(other) != cost.known_price(_MODEL)
        summary = calculate([_entry(continuations=[_continuation(model=other)])])
        assert summary["continuation_usd"] == pytest.approx(
            _tokens_usd(other, 10_500, 800), abs=1e-4
        )

    def test_without_a_model_of_its_own_it_takes_the_entrys(self):
        record = _continuation()
        del record["model"]
        summary = calculate([_entry(continuations=[record])])
        assert summary["continuation_usd"] == pytest.approx(
            _tokens_usd(_MODEL, 10_500, 800), abs=1e-4
        )


class TestItsSearchesAreBilledPerCall:
    _GEMINI = "gemini-3.5-flash"

    def test_its_search_count_is_billed_beside_the_entrys(self):
        fee = cost.known_search_fee(self._GEMINI)
        assert fee and fee[1] == "query", "this test assumes a per-query fee"
        entry = _entry(
            model=self._GEMINI,
            searches=10,
            continuations=[_continuation(model=self._GEMINI, searches=4)],
        )
        summary = calculate([entry])
        assert summary["search_units"] == 14
        assert summary["total_search_usd"] == pytest.approx(
            14 * fee[0] / 1000, abs=1e-4
        )
        assert summary["by_pass"][0]["search_units"] == 14

    def test_an_unreported_count_is_unmeasured_not_free(self):
        entry = _entry(continuations=[_continuation(searches=None)])
        summary = calculate([entry])
        assert summary["unmeasured_search_calls"] == 1
        assert summary["by_pass"][0]["unmeasured_search_calls"] == 1


class TestOneWithNoUsageMakesTheTotalAFloor:
    def test_no_usage_is_uncosted(self):
        record = _continuation(
            tokens={"prompt": 0, "completion": 0}, failed=True, error="stalled"
        )
        summary = calculate([_entry(continuations=[record])])
        assert summary["uncosted_calls"] == 1
        assert summary["uncosted_continuation_calls"] == 1
        # Not a retried attempt: the Retries line must not count it.
        assert summary["discarded_calls"] == 0

    def test_empty_tokens_are_uncosted_too(self):
        record = _continuation(tokens={}, failed=True, error="Timed out after 300s")
        summary = calculate([_entry(continuations=[record])])
        assert summary["uncosted_continuation_calls"] == 1

    def test_one_refused_before_generating_is_not(self):
        """A 429 at the door billed nothing, as for a discarded attempt (#331)."""
        record = _continuation(
            tokens={"prompt": 0, "completion": 0},
            failed=True,
            error="429",
            unbilled=True,
        )
        summary = calculate([_entry(continuations=[record])])
        assert summary["uncosted_calls"] == 0
        assert summary["continuation_calls"] == 1

    def test_the_attempts_its_own_retry_discarded_are_counted(self):
        record = _continuation(
            discarded_attempts={
                "count": 2,
                "costed": 1,
                "reasons": ["StreamStalled", "MalformedJSONError"],
                "tokens": {"prompt": 10_500, "completion": 3_000},
            }
        )
        summary = calculate([_entry(continuations=[record])])
        assert summary["discarded_calls"] == 2
        assert summary["uncosted_calls"] == 1
        assert summary["uncosted_continuation_calls"] == 0
        assert summary["continuation_usd"] == pytest.approx(
            _tokens_usd(_MODEL, 10_500, 800) + _tokens_usd(_MODEL, 10_500, 3_000),
            abs=1e-4,
        )


class TestReplayedContinuationsAreHistory:
    def test_a_replayed_entrys_continuation_is_history(self):
        summary = calculate([_entry(replayed=True, continuations=[_continuation()])])
        assert summary["incurred_usd"] == 0.0
        assert summary["replayed_usd"] == summary["total_usd"] > 0
        assert summary["by_pass"][0]["replayed"] is True

    def test_one_this_run_made_on_a_replayed_entry_is_spend(self):
        """--retry-failed: the first call is the capture's, the follow-up ours."""
        entry = _entry(replayed=True, continuations=[_continuation(replayed=False)])
        summary = calculate([entry])
        follow_up = _tokens_usd(_MODEL, 10_500, 800)
        assert summary["incurred_usd"] == pytest.approx(follow_up, abs=1e-4)
        assert summary["replayed_usd"] == pytest.approx(
            _tokens_usd(_MODEL, 10_000, 2_000), abs=1e-4
        )
        row = summary["by_pass"][0]
        assert "replayed" not in row, "part of this row is this run's spend"
        assert row["incurred_usd"] == pytest.approx(follow_up, abs=1e-6)

    def test_the_counts_still_describe_the_total(self):
        """Replayed or not, a call is a call: the counters say what total_usd is."""
        record = _continuation(tokens={"prompt": 0, "completion": 0}, failed=True)
        summary = calculate([_entry(replayed=True, continuations=[record])])
        assert summary["continuation_calls"] == 1
        assert summary["uncosted_continuation_calls"] == 1


class TestCallLogEntryCarriesThem:
    def test_copied_when_present(self):
        records = [_continuation()]
        entry = call_log_entry("x", {"model": _MODEL, "continuations": records})
        assert entry["continuations"] == records

    def test_absent_when_none_ran(self):
        assert "continuations" not in call_log_entry("x", {"model": _MODEL})
