"""A claim's resolution says where its time went, and a timeout names the step (#389).

On a 190K-character draft, 117 of 267 claims were written off as "Timed out
after 90s", logged in one lump after the batch with nothing about where each
had spent its time; the 90s cap itself was below the sum of the sequential
budgets inside one claim. These tests hold the resolver to three things: the cap
is derived from those budgets, every result carries per-step wall times, and a
claim past its budget is logged at that moment with the step it is in.
"""

import functools
import logging
import time
from unittest.mock import patch

from ci_core.concurrency import run_all_bounded

from ci_article_review.adapters.citation import resolver

from .test_resolver import _SOURCES, _no_wayback, _page_response

_URL = "https://example.com/page"
_SUPPORTS = {
    "failed": False,
    "data": {
        "verdict": "supports",
        "reason": "matches",
        "quote": "The measured value is documented in detail throughout this report.",
    },
    "model": "mistral-small-latest",
    "tokens": {"prompt": 10, "completion": 5},
    "elapsed_seconds": 0.2,
}


def _patched(relevance=None):
    """The three network edges of a known-URL resolution, stubbed."""
    return (
        patch(
            "ci_article_review.adapters.citation.resolver.safe_get",
            return_value=_page_response(),
        ),
        patch(
            "ci_article_review.adapters.citation.resolver.wayback.check",
            side_effect=_no_wayback,
        ),
        patch(
            "ci_article_review.adapters.citation.resolver.llm.call_provider",
            side_effect=relevance or (lambda *a, **k: dict(_SUPPORTS)),
        ),
    )


def _quiet_tail(monkeypatch):
    """Nothing past the resolution jobs is under test; none of it may reach the network."""
    monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_verify_archive_matches", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_note_weak_span_sources", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_note_breaker_state", lambda *a, **k: None)


def _resolve(entries, api_keys=None):
    return resolver.resolve_citations(
        entries, _SOURCES, api_keys=api_keys or {"mistral": {"api_key": "k"}}
    )


class TestTheCapIsDerived:
    def test_the_cap_is_the_sum_of_the_steps_budgets(self):
        assert resolver._RESOLVE_TIMEOUT_SECONDS == (
            resolver._FETCH_CHAIN_SECONDS
            + resolver._ESCALATION_SECONDS
            + resolver._ARCHIVE_SECONDS
            + resolver._PAGINATION_SECONDS
            + resolver._RELEVANCE_SECONDS
        )

    def test_the_fetch_chain_counts_every_redirect_hop(self):
        from ci_core.http import _MAX_REDIRECTS

        assert resolver._FETCH_CHAIN_SECONDS == resolver._FETCH_TIMEOUT_SECONDS * (
            _MAX_REDIRECTS + 1
        )

    def test_the_cap_is_well_above_the_90s_that_lost_117_claims(self):
        assert resolver._RESOLVE_TIMEOUT_SECONDS >= 2 * 90


class TestEveryResultCarriesItsTiming:
    def test_a_resolved_claim_reports_each_step_and_the_total(self, monkeypatch):
        _quiet_tail(monkeypatch)
        with _patched()[0], _patched()[1], _patched()[2]:
            (result,) = _resolve([{"claim": "a claim", "known_url": _URL}])

        assert result["verification"] == "checksum"
        timing = result["resolution_timing"]
        for step in ("fetch", "extract", "archive_check", "pagination", "relevance"):
            assert step in timing, f"{step} missing from {timing}"
        assert timing["total"] >= max(v for k, v in timing.items() if k != "total")
        assert "in_progress" not in timing, "a finished claim is in no step"

    def test_a_claim_that_raised_still_reports_how_far_it_got(self, monkeypatch):
        _quiet_tail(monkeypatch)

        def boom(*args, **kwargs):
            resolver._step("fetch")
            raise RuntimeError("socket on fire")

        monkeypatch.setattr(resolver, "_resolve_one_steps", boom)
        (result,) = _resolve([{"claim": "a claim", "known_url": _URL}])

        assert result["verification"] == "resolution_error"
        assert result["error_kind"] == "error"
        assert "fetch" in result["resolution_timing"]

    def test_the_step_clock_accumulates_a_step_entered_twice(self):
        clock = resolver._StepClock()
        clock.enter("fetch")
        time.sleep(0.02)
        clock.enter("relevance")
        clock.enter("fetch")
        time.sleep(0.02)
        clock.done()
        report = clock.report()
        assert report["fetch"] >= 0.0
        assert set(report) == {"fetch", "relevance", "total"}

    def test_the_formatter_reads_like_a_sentence(self):
        line = resolver._format_timing(
            {"fetch": 1.0, "extract": 1.9, "relevance": 39.0, "total": 44.2}
        )
        assert line == "fetch 1.0s, extract 1.9s, relevance 39.0s; 44.2s total"


class TestATimeoutNamesItsStep:
    def _resolve_with_slow_relevance(self, monkeypatch, caplog, hold, budget, ceiling):
        _quiet_tail(monkeypatch)
        monkeypatch.setattr(resolver, "_RESOLVE_TIMEOUT_SECONDS", budget)
        monkeypatch.setattr(
            resolver,
            "run_all_bounded",
            functools.partial(run_all_bounded, global_timeout=ceiling),
        )

        def slow_relevance(*args, **kwargs):
            time.sleep(hold)
            return dict(_SUPPORTS)

        patches = _patched(relevance=slow_relevance)
        with (
            patches[0],
            patches[1],
            patches[2],
            caplog.at_level(logging.INFO, logger=resolver.log.name),
        ):
            (result,) = _resolve([{"claim": "a claim", "known_url": _URL}])
        return result

    def test_an_overrun_is_logged_when_it_happens_with_the_step_it_is_in(
        self, monkeypatch, caplog
    ):
        result = self._resolve_with_slow_relevance(
            monkeypatch, caplog, hold=0.5, budget=0.05, ceiling=5
        )

        # Kept: it finished before the ceiling (task A), and the result says
        # where the time went.
        assert result["verification"] == "checksum"
        assert result["resolution_timing"]["relevance"] >= 0.4
        overrun = [r for r in caplog.records if "past its 0.05s budget" in r.message]
        assert len(overrun) == 1
        assert "still in relevance" in overrun[0].message
        assert f"claim 0 ({_URL})" in overrun[0].message
        summary = [r for r in caplog.records if "ran past the" in r.message]
        assert summary and "1 finished before the batch ceiling" in summary[0].message

    def test_a_claim_written_off_at_the_ceiling_names_the_step_it_died_in(
        self, monkeypatch, caplog
    ):
        result = self._resolve_with_slow_relevance(
            monkeypatch, caplog, hold=5, budget=0.05, ceiling=0.4
        )

        assert result["verification"] == "resolution_error"
        assert result["error_kind"] == "timeout"
        assert result["timed_out_in"] == "relevance"
        assert result["url"] == _URL
        timing = result["resolution_timing"]
        assert "relevance" in timing and "in_progress" not in timing
        assert "in the relevance step" in result["note"]
        assert "relevance" in result["note"] and "s total" in result["note"]
        written_off = [r for r in caplog.records if "written off" in r.message]
        assert written_off and "in relevance" in written_off[0].message

    def test_nothing_is_logged_for_a_claim_inside_its_budget(self, monkeypatch, caplog):
        _quiet_tail(monkeypatch)
        patches = _patched()
        with (
            patches[0],
            patches[1],
            patches[2],
            caplog.at_level(logging.INFO, logger=resolver.log.name),
        ):
            _resolve([{"claim": "a claim", "known_url": _URL}])
        assert not [r for r in caplog.records if "budget" in r.message]
