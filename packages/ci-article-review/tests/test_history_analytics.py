"""Unit tests for history_analytics — cross-run aggregation over pipeline_history/."""

import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from ci_article_review import history_analytics as ha


def _write_report(root, slug, run_number, ts, report, filename=None):
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    filename = (
        filename
        or f"run_{run_number}_{ts.replace(':', '').replace('-', '')}_report.json"
    )
    path = d / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f)
    return path


def _api_call(pass_key, failed):
    return {"pass": pass_key, "model": pass_key.split(":")[0], "failed": failed}


def _report(
    generated,
    run_number=1,
    article_title="Test Article",
    api_call_log=None,
    cost_usd=None,
    fk_grade=None,
    seo_issue_count=None,
    broken_links=None,
):
    report = {
        "generated": generated,
        "run_number": run_number,
        "article_title": article_title,
        "publication": "test_pub",
        "model_failures": [c["pass"] for c in (api_call_log or []) if c["failed"]],
        "api_call_log": api_call_log or [],
    }
    if cost_usd is not None:
        report["cost_summary"] = {"total_usd": cost_usd}
    pre_analysis = {}
    if fk_grade is not None:
        pre_analysis["readability"] = {"flesch_kincaid_grade": fk_grade}
    if seo_issue_count is not None:
        pre_analysis["seo"] = {
            "issues": [{"type": "x"} for _ in range(seo_issue_count)]
        }
    if broken_links is not None:
        pre_analysis["links"] = [
            {"ok": i >= broken_links} for i in range(max(broken_links, 1))
        ]
    if pre_analysis:
        report["pre_analysis"] = pre_analysis
    return report


class TestLoadReports:
    def test_loads_and_sorts_chronologically(self, tmp_path):
        _write_report(
            tmp_path,
            "article-a",
            1,
            "20260101_000000",
            _report("2026-01-02T00:00:00+00:00"),
        )
        _write_report(
            tmp_path,
            "article-a",
            2,
            "20260101_010000",
            _report("2026-01-01T00:00:00+00:00"),
        )
        entries = ha.load_reports(tmp_path)
        assert len(entries) == 2
        assert entries[0]["timestamp"] < entries[1]["timestamp"]

    def test_skips_malformed_json(self, tmp_path):
        d = tmp_path / "article-a"
        d.mkdir()
        (d / "run_1_bad_report.json").write_text("{not valid json", encoding="utf-8")
        _write_report(
            tmp_path,
            "article-a",
            2,
            "20260101_010000",
            _report("2026-01-01T00:00:00+00:00"),
        )
        entries = ha.load_reports(tmp_path)
        assert len(entries) == 1

    def test_missing_history_root_returns_empty(self, tmp_path):
        entries = ha.load_reports(tmp_path / "does_not_exist")
        assert entries == []

    def test_scoped_to_one_article(self, tmp_path):
        _write_report(
            tmp_path,
            "article-a",
            1,
            "20260101_000000",
            _report("2026-01-01T00:00:00+00:00"),
        )
        _write_report(
            tmp_path,
            "article-b",
            1,
            "20260101_000000",
            _report("2026-01-01T00:00:00+00:00"),
        )
        entries = ha.load_reports(tmp_path, article_slug="article-a")
        assert len(entries) == 1
        assert entries[0]["slug"] == "article-a"

    def test_falls_back_to_mtime_when_generated_missing(self, tmp_path):
        report = _report("2026-01-01T00:00:00+00:00")
        del report["generated"]
        path = _write_report(tmp_path, "article-a", 1, "20260101_000000", report)
        entries = ha.load_reports(tmp_path)
        assert len(entries) == 1
        assert entries[0]["timestamp"] is not None
        assert path.exists()


class TestProviderReliability:
    def test_detects_degraded_provider(self):
        # perplexity: 5 healthy baseline calls, then 5 straight failures (the
        # "401 across every domain" scenario from the outage this is meant to catch).
        calls = [_api_call("perplexity:fact_check", False) for _ in range(5)]
        calls += [_api_call("perplexity:fact_check", True) for _ in range(5)]
        entries = [
            {"slug": "a", "path": None, "report": {"api_call_log": [c]}, "timestamp": i}
            for i, c in enumerate(calls)
        ]
        result = ha.provider_reliability(entries, recent_window=5, min_baseline=3)
        assert result["perplexity"]["degraded"] is True
        assert result["perplexity"]["recent_success_rate"] == 0.0
        assert result["perplexity"]["baseline_success_rate"] == 1.0

    def test_stable_provider_not_flagged(self):
        calls = [_api_call("openai:fact_check", i % 10 == 0) for i in range(10)]
        entries = [
            {"slug": "a", "path": None, "report": {"api_call_log": [c]}, "timestamp": i}
            for i, c in enumerate(calls)
        ]
        result = ha.provider_reliability(entries, recent_window=5, min_baseline=3)
        assert result["openai"]["degraded"] is False

    def test_insufficient_baseline_not_flagged(self):
        calls = [_api_call("grok:fact_check", True) for _ in range(2)]
        entries = [
            {"slug": "a", "path": None, "report": {"api_call_log": [c]}, "timestamp": i}
            for i, c in enumerate(calls)
        ]
        result = ha.provider_reliability(entries, recent_window=5, min_baseline=3)
        assert result["grok"]["degraded"] is False
        assert result["grok"]["baseline_success_rate"] is None

    def test_no_api_call_log_yields_empty(self):
        entries = [{"slug": "a", "path": None, "report": {}, "timestamp": 0}]
        assert ha.provider_reliability(entries) == {}

    def test_pass_without_colon_is_skipped_not_misattributed(self):
        # Some very old reports recorded "pass" as just the domain name with
        # no colon (e.g. "fact_check" instead of "openai:fact_check") — that
        # can't be attributed to a provider and must not show up as one.
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": {
                    "api_call_log": [
                        _api_call("openai:fact_check", False),
                        {"pass": "fact_check", "failed": False},
                    ]
                },
                "timestamp": 0,
            }
        ]
        result = ha.provider_reliability(entries)
        assert set(result.keys()) == {"openai"}

    def test_multiple_providers_tracked_independently(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": {
                    "api_call_log": [
                        _api_call("openai:fact_check", False),
                        _api_call("perplexity:fact_check", True),
                    ]
                },
                "timestamp": 0,
            }
        ]
        result = ha.provider_reliability(entries)
        assert set(result.keys()) == {"openai", "perplexity"}


class TestCostTrend:
    def test_totals_and_average(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-01T00:00:00+00:00", cost_usd=1.0),
                "timestamp": 0,
            },
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-02T00:00:00+00:00", cost_usd=3.0),
                "timestamp": 1,
            },
        ]
        result = ha.cost_trend(entries, recent_window=5)
        assert result["runs"] == 2
        assert result["total_usd"] == 4.0
        assert result["average_usd"] == 2.0

    def test_increasing_trend_detected(self):
        entries = []
        for i in range(3):
            entries.append(
                {
                    "slug": "a",
                    "path": None,
                    "report": _report("2026-01-01T00:00:00+00:00", cost_usd=1.0),
                    "timestamp": i,
                }
            )
        for i in range(3, 6):
            entries.append(
                {
                    "slug": "a",
                    "path": None,
                    "report": _report("2026-01-01T00:00:00+00:00", cost_usd=5.0),
                    "timestamp": i,
                }
            )
        result = ha.cost_trend(entries, recent_window=3)
        assert result["trend"] == "increasing"

    def test_no_cost_data(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-01T00:00:00+00:00"),
                "timestamp": 0,
            }
        ]
        result = ha.cost_trend(entries)
        assert result["trend"] == "no_data"
        assert result["runs"] == 0

    def test_insufficient_baseline(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-01T00:00:00+00:00", cost_usd=1.0),
                "timestamp": 0,
            }
        ]
        result = ha.cost_trend(entries, recent_window=5)
        assert result["trend"] == "insufficient_history"
        assert result["baseline_average_usd"] is None


class TestQualityTrend:
    def test_per_article_detects_improvement(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report(
                    "2026-01-01T00:00:00+00:00",
                    run_number=1,
                    fk_grade=14.0,
                    seo_issue_count=3,
                    broken_links=2,
                ),
                "timestamp": 0,
            },
            {
                "slug": "a",
                "path": None,
                "report": _report(
                    "2026-01-02T00:00:00+00:00",
                    run_number=2,
                    fk_grade=9.0,
                    seo_issue_count=0,
                    broken_links=0,
                ),
                "timestamp": 1,
            },
        ]
        result = ha.per_article_quality_trend(entries)
        assert result["a"]["runs"] == 2
        assert result["a"]["fk_grade_trend"] == "improved"
        assert result["a"]["seo_issues_trend"] == "improved"
        assert result["a"]["broken_links_trend"] == "improved"

    def test_per_article_detects_worsening(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-01T00:00:00+00:00", fk_grade=8.0),
                "timestamp": 0,
            },
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-02T00:00:00+00:00", fk_grade=15.0),
                "timestamp": 1,
            },
        ]
        result = ha.per_article_quality_trend(entries)
        assert result["a"]["fk_grade_trend"] == "worsened"

    def test_single_run_article_has_unchanged_or_unknown_trend(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-01T00:00:00+00:00", fk_grade=10.0),
                "timestamp": 0,
            }
        ]
        result = ha.per_article_quality_trend(entries)
        assert result["a"]["runs"] == 1
        assert result["a"]["fk_grade_trend"] == "unchanged"

    def test_missing_pre_analysis_is_unknown_not_error(self):
        entries = [
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-01T00:00:00+00:00"),
                "timestamp": 0,
            },
            {
                "slug": "a",
                "path": None,
                "report": _report("2026-01-02T00:00:00+00:00"),
                "timestamp": 1,
            },
        ]
        result = ha.per_article_quality_trend(entries)
        assert result["a"]["fk_grade_trend"] == "unknown"

    def test_global_trend_across_articles(self):
        entries = []
        for i in range(3):
            entries.append(
                {
                    "slug": f"article-{i}",
                    "path": None,
                    "report": _report("2026-01-01T00:00:00+00:00", fk_grade=15.0),
                    "timestamp": i,
                }
            )
        for i in range(3, 6):
            entries.append(
                {
                    "slug": f"article-{i}",
                    "path": None,
                    "report": _report("2026-01-01T00:00:00+00:00", fk_grade=8.0),
                    "timestamp": i,
                }
            )
        result = ha.global_quality_trend(entries, recent_window=3)
        assert result["fk_grade"]["trend"] == "improved"


class TestBuildHistoryReport:
    def test_end_to_end_on_disk_fixtures(self, tmp_path):
        _write_report(
            tmp_path,
            "article-a",
            1,
            "20260101_000000",
            _report(
                "2026-01-01T00:00:00+00:00",
                api_call_log=[_api_call("openai:fact_check", False)],
                cost_usd=0.5,
                fk_grade=10.0,
                seo_issue_count=1,
                broken_links=0,
            ),
        )
        _write_report(
            tmp_path,
            "article-a",
            2,
            "20260102_000000",
            _report(
                "2026-01-02T00:00:00+00:00",
                run_number=2,
                api_call_log=[_api_call("openai:fact_check", False)],
                cost_usd=0.7,
                fk_grade=9.0,
                seo_issue_count=0,
                broken_links=0,
            ),
        )
        result = ha.build_history_report(tmp_path)
        assert result["total_reports"] == 2
        assert "openai" in result["provider_reliability"]
        assert result["cost_trend"]["runs"] == 2
        assert result["per_article_quality_trend"]["article-a"]["runs"] == 2

    def test_empty_history_root(self, tmp_path):
        result = ha.build_history_report(tmp_path / "missing")
        assert result["total_reports"] == 0
        assert result["provider_reliability"] == {}
        assert result["cost_trend"]["trend"] == "no_data"

    def test_print_history_report_does_not_error_on_empty(self, tmp_path, capsys):
        result = ha.build_history_report(tmp_path / "missing")
        ha.print_history_report(result)
        captured = capsys.readouterr()
        assert "No report files found" in captured.out

    def test_print_history_report_flags_degraded_provider(self, tmp_path, capsys):
        entries_dir = tmp_path
        for i in range(5):
            _write_report(
                entries_dir,
                "article-a",
                1,
                f"2026010{i + 1}_000000",
                _report(
                    f"2026-01-0{i + 1}T00:00:00+00:00",
                    api_call_log=[_api_call("perplexity:fact_check", False)],
                ),
            )
        for i in range(5):
            _write_report(
                entries_dir,
                "article-a",
                1,
                f"2026011{i}_000000",
                _report(
                    f"2026-01-1{i}T00:00:00+00:00",
                    api_call_log=[_api_call("perplexity:fact_check", True)],
                ),
            )
        result = ha.build_history_report(tmp_path, recent_window=5)
        ha.print_history_report(result)
        captured = capsys.readouterr()
        assert "DEGRADED" in captured.out
        assert "perplexity" in captured.out


class TestPassContribution:
    """Audit findings 10 and 16 — the data needed before retuning presets.

    Both findings were deliberately left unimplemented in the audit because
    changing the ensemble blind is exactly the wrong move. This derives the
    answer from reports already on disk.
    """

    def _entry(self, *, api_log, by_pass, consensus):
        return {
            "slug": "a",
            "report": {
                "api_call_log": api_log,
                "cost_summary": {"by_pass": by_pass},
                "section_1_consensus": consensus,
            },
        }

    def test_counts_calls_failures_and_cost(self):
        entries = [
            self._entry(
                api_log=[
                    {"pass": "gemini:fact_check", "failed": False},
                    {"pass": "grok:red_team", "failed": True},
                ],
                by_pass=[
                    {"pass": "gemini:fact_check", "total_usd": 0.10},
                    {"pass": "grok:red_team", "total_usd": 0.05},
                ],
                consensus=[],
            )
        ]
        by_name = {s["pass"]: s for s in ha.pass_contribution(entries)}
        assert by_name["gemini:fact_check"]["calls"] == 1
        assert by_name["grok:red_team"]["failures"] == 1
        assert by_name["gemini:fact_check"]["total_usd"] == 0.10

    def test_separates_sole_source_from_corroborated_findings(self):
        """A pass seeing what nothing else sees is the opposite of redundant."""
        entries = [
            self._entry(
                api_log=[{"pass": "grok:red_team", "failed": False}],
                by_pass=[{"pass": "grok:red_team", "total_usd": 0.05}],
                consensus=[
                    {"passage": "p1", "models": ["grok:red_team"]},
                    {
                        "passage": "p2",
                        "models": ["grok:red_team", "claude:red_team"],
                    },
                ],
            )
        ]
        by_name = {s["pass"]: s for s in ha.pass_contribution(entries)}
        grok = by_name["grok:red_team"]
        assert grok["consensus_hits"] == 2
        assert grok["sole_source"] == 1
        assert grok["corroborated"] == 1

    def test_cost_per_consensus_hit_is_the_sorting_signal(self):
        entries = [
            self._entry(
                api_log=[],
                by_pass=[{"pass": "expensive:d", "total_usd": 1.0}],
                consensus=[{"passage": "p", "models": ["expensive:d", "cheap:d"]}],
            )
        ]
        by_name = {s["pass"]: s for s in ha.pass_contribution(entries)}
        assert by_name["expensive:d"]["usd_per_consensus_hit"] == 1.0

    def test_a_pass_that_never_reaches_consensus_is_reported_not_hidden(self):
        """Cost with zero hits is the loudest result this can produce."""
        entries = [
            self._entry(
                api_log=[{"pass": "perplexity:voice_style", "failed": False}],
                by_pass=[{"pass": "perplexity:voice_style", "total_usd": 0.20}],
                consensus=[],
            )
        ]
        result = ha.pass_contribution(entries)
        (entry,) = [s for s in result if s["pass"] == "perplexity:voice_style"]
        assert entry["consensus_hits"] == 0
        assert entry["usd_per_consensus_hit"] is None
        assert entry["total_usd"] == 0.20

    def test_accumulates_across_runs(self):
        e = self._entry(
            api_log=[{"pass": "a:b", "failed": False}],
            by_pass=[{"pass": "a:b", "total_usd": 0.10}],
            consensus=[{"passage": "p", "models": ["a:b"]}],
        )
        result = ha.pass_contribution([e, e, e])
        (entry,) = result
        assert entry["calls"] == 3
        assert entry["total_usd"] == 0.30
        assert entry["consensus_hits"] == 3

    def test_reports_missing_these_fields_do_not_raise(self):
        """The report schema has grown over time; old reports lack these keys."""
        assert ha.pass_contribution([{"slug": "a", "report": {}}]) == []


class TestExpansionIsNotScoredOnConsensus:
    """The expansion domain never reaches Section 1, by design.

    Scored on the consensus table it would show zero hits and no cost-per-hit
    on every run, and land in the "never contributed, consider trimming"
    note — the report recommending deletion of a pass that was working exactly
    as intended.
    """

    def _entry(self, *, api_log, by_pass, expansion, consensus=()):
        return {
            "slug": "a",
            "report": {
                "api_call_log": api_log,
                "cost_summary": {"by_pass": by_pass},
                "section_1_consensus": list(consensus),
                "section_10_expansion": expansion,
            },
        }

    def test_proposals_are_counted_per_proposing_model(self):
        entry = self._entry(
            api_log=[{"pass": "perplexity:expansion", "failed": False}],
            by_pass=[{"pass": "perplexity:expansion", "total_usd": 0.04}],
            expansion={
                "sources": [
                    {"title": "A", "proposed_by": ["perplexity"]},
                    {"title": "B", "proposed_by": ["perplexity", "gemini"]},
                ],
                "topics": [{"topic": "C", "proposed_by": ["gemini"]}],
            },
        )
        by_name = {s["pass"]: s for s in ha.pass_contribution([entry])}
        assert by_name["perplexity:expansion"]["proposals"] == 2
        # A merged candidate credits everyone who offered it.
        assert by_name["gemini:expansion"]["proposals"] == 2

    def test_cost_per_proposal_replaces_cost_per_hit(self):
        entry = self._entry(
            api_log=[{"pass": "perplexity:expansion", "failed": False}],
            by_pass=[{"pass": "perplexity:expansion", "total_usd": 0.04}],
            expansion={
                "sources": [
                    {"title": str(n), "proposed_by": ["perplexity"]} for n in range(4)
                ]
            },
        )
        slot = {s["pass"]: s for s in ha.pass_contribution([entry])}[
            "perplexity:expansion"
        ]
        assert slot["usd_per_proposal"] == 0.01
        assert slot["consensus_hits"] == 0
        assert slot["usd_per_consensus_hit"] is None

    def test_a_report_without_the_section_is_fine(self):
        """Every run before --expand existed, and every run that skipped it."""
        entry = {
            "slug": "a",
            "report": {
                "api_call_log": [{"pass": "gemini:fact_check", "failed": False}],
                "cost_summary": {"by_pass": []},
                "section_1_consensus": [],
            },
        }
        assert ha.pass_contribution([entry])[0]["proposals"] == 0

    def test_the_trim_note_never_names_an_expansion_pass(self, tmp_path, capsys):
        """The regression guard: that note is read as a delete list.

        Driven through the real report builder rather than a hand-built result
        dict, so it also proves the section survives the round trip to disk.
        """
        _write_report(
            tmp_path,
            "a",
            1,
            "2026-09-05T10:00:00",
            {
                "generated": "2026-09-05T10:00:00",
                "run_number": 1,
                "article_title": "T",
                "publication": "p",
                "api_call_log": [
                    {"pass": "perplexity:expansion", "failed": False},
                    {"pass": "gemini:fact_check", "failed": False},
                ],
                "cost_summary": {
                    "by_pass": [
                        {"pass": "perplexity:expansion", "total_usd": 0.04},
                        {"pass": "gemini:fact_check", "total_usd": 0.02},
                    ]
                },
                "section_1_consensus": [],
                "section_10_expansion": {
                    "sources": [{"title": "A", "proposed_by": ["perplexity"]}]
                },
            },
        )

        ha.print_history_report(ha.build_history_report(history_root=str(tmp_path)))
        out = capsys.readouterr().out

        # The note itself, not everything printed after it — the expansion
        # table follows and would otherwise satisfy the substring by accident.
        trim_note = ""
        if "Note:" in out:
            trim_note = out.split("Note:")[1].split("Expansion passes")[0]
        assert "expansion" not in trim_note, (
            "an expansion pass was listed as a candidate for trimming"
        )
        assert "gemini:fact_check" in out
        # It is still reported, just on its own terms.
        assert "scored on proposals" in out
        assert "perplexity:expansion" in out


# ---------------------------------------------------------------------------
# --retry-failed: a capture's run, and the run that re-ran its failed call
# ---------------------------------------------------------------------------

#: What each pass cost in the capture's run. grok:red_team failed there, and
#: its failed attempt was still billed.
_CAPTURE_COSTS = {
    "gemini:fact_check": 0.10,
    "grok:red_team": 0.05,
    "openai:voice_style": 0.15,
}
_RETRIED = "grok:red_team"
#: What re-running it cost the --retry-failed run.
_RETRY_COST = 0.08


def _retry_failed_pair():
    """A capture's run and a --retry-failed run of it, as the pipeline saves them.

    The retry re-ran only ``_RETRIED``. Its report still lists the other two
    passes, carried over from the capture and marked ``replayed`` in both
    ``api_call_log`` and ``by_pass``, and its Section 1 is built over all
    three: the capture's flag again, plus two that need the re-run pass.
    """
    carried = [name for name in _CAPTURE_COSTS if name != _RETRIED]

    def mark(name):
        return {"replayed": True} if name in carried else {}

    capture = {
        "generated": "2026-09-18T12:00:00+00:00",
        "api_call_log": [
            {"pass": name, "model": name.split(":")[0], "failed": name == _RETRIED}
            for name in _CAPTURE_COSTS
        ],
        "cost_summary": {
            "total_usd": 0.30,
            "incurred_usd": 0.30,
            "replayed_usd": 0.0,
            "by_pass": [
                {"pass": name, "total_usd": usd} for name, usd in _CAPTURE_COSTS.items()
            ],
        },
        "section_1_consensus": [
            {"passage": "p1", "models": ["gemini:fact_check", "openai:voice_style"]},
        ],
    }
    retry_costs = {**_CAPTURE_COSTS, _RETRIED: _RETRY_COST}
    retry = {
        "generated": "2026-09-18T13:00:00+00:00",
        "retry_failed_from": "pipeline_history/a/run_1_20260918_120000_results.json",
        "api_call_log": [
            {"pass": name, "model": name.split(":")[0], "failed": False, **mark(name)}
            for name in _CAPTURE_COSTS
        ],
        "cost_summary": {
            "total_usd": 0.33,
            "incurred_usd": _RETRY_COST,
            "replayed_usd": 0.25,
            "by_pass": [
                {"pass": name, "total_usd": usd, **mark(name)}
                for name, usd in retry_costs.items()
            ],
        },
        "section_1_consensus": [
            {"passage": "p1", "models": ["gemini:fact_check", "openai:voice_style"]},
            # Cleared the bar only with the re-run pass's vote.
            {"passage": "p2", "models": ["gemini:fact_check", _RETRIED]},
            {"passage": "p3", "models": [_RETRIED]},
        ],
    }
    return capture, retry


def _entries(*reports):
    """As ``load_reports`` returns them: one article, an hour apart.

    Named the way the pipeline saves a run, so the first is the run whose
    capture ``_retry_failed_pair``'s retry names in ``retry_failed_from``.
    """
    return [
        {
            "slug": "a",
            "path": Path("pipeline_history/a")
            / f"run_{i + 1}_20260918_{12 + i}0000_report.json",
            "report": report,
            "timestamp": i,
        }
        for i, report in enumerate(reports)
    ]


class TestRetryFailedRunsCountOnlyTheCallsTheyMade:
    """A --retry-failed run's report re-lists the calls it carried over.

    It is saved in the article's own history, beside the run whose capture it
    loaded, so reading its report whole counted every carried call twice — in
    spend, in provider reliability, and in per-pass contribution. Since
    PR #211 the carried calls are marked ``replayed``; each figure here now
    counts a call once, in the run that made it.
    """

    def test_total_spend_is_what_each_run_spent(self):
        capture, retry = _retry_failed_pair()
        result = ha.cost_trend(_entries(capture, retry))
        # Not 0.63: the retry's own total lists the capture's 0.25 again.
        assert result["total_usd"] == pytest.approx(0.30 + _RETRY_COST)
        assert result["retry_failed_runs"] == 1
        assert result["retry_failed_usd"] == pytest.approx(_RETRY_COST)
        assert result["carried_over_usd"] == pytest.approx(0.25)

    def test_a_retry_is_counted_with_the_run_it_re_ran(self):
        """One review that took two invocations, not two runs."""
        capture, retry = _retry_failed_pair()
        result = ha.cost_trend(_entries(capture, retry))
        assert result["runs"] == 1
        assert result["average_usd"] == pytest.approx(0.30 + _RETRY_COST)
        assert result["retry_failed_unmatched"] == 0

    def test_a_retry_does_not_turn_the_trend_by_itself(self):
        """Six $1.00 runs, then a $0.03 retry of the last one.

        As a run of its own the retry is a fifth of the recent window, whose
        average falls 19% below the baseline's: "decreasing".
        """
        runs = [
            {"cost_summary": {"total_usd": 1.0, "incurred_usd": 1.0}} for _ in range(6)
        ]
        retry = {
            "retry_failed_from": "pipeline_history/a/run_6_20260918_170000_results.json",
            "cost_summary": {
                "total_usd": 1.0,
                "incurred_usd": 0.03,
                "replayed_usd": 0.97,
            },
        }
        result = ha.cost_trend(_entries(*runs, retry), recent_window=5)
        assert result["runs"] == 6
        assert result["trend"] == "flat"

    def test_a_retry_whose_run_is_not_here_counts_as_a_run(self):
        """Its capture came from another checkout: no run here to add it to."""
        capture, retry = _retry_failed_pair()
        retry["retry_failed_from"] = (
            r"C:\elsewhere\pipeline_history\a\run_9_20260901_000000_results.json"
        )
        result = ha.cost_trend(_entries(capture, retry))
        assert result["runs"] == 2
        assert result["total_usd"] == pytest.approx(0.30 + _RETRY_COST)
        assert result["retry_failed_unmatched"] == 1

    def test_a_retry_of_a_retrys_capture_lands_on_the_first_run(self):
        """A retry saves a capture too, and a second retry can load it."""
        capture, retry = _retry_failed_pair()
        _, again = _retry_failed_pair()
        again["retry_failed_from"] = (
            "pipeline_history/a/run_2_20260918_130000_results.json"
        )
        again["cost_summary"]["incurred_usd"] = 0.02
        result = ha.cost_trend(_entries(capture, retry, again))
        assert result["runs"] == 1
        assert result["total_usd"] == pytest.approx(0.30 + _RETRY_COST + 0.02)
        assert result["retry_failed_runs"] == 2
        assert result["retry_failed_unmatched"] == 0

    def test_a_retry_that_re_ran_nothing_spent_nothing(self):
        """Its capture had no failure, so every call was carried over.

        ``incurred_usd`` 0.0 is the answer, not a gap to fill from the total.
        """
        capture, retry = _retry_failed_pair()
        retry["cost_summary"].update(incurred_usd=0.0, replayed_usd=0.33)
        result = ha.cost_trend(_entries(capture, retry))
        assert result["total_usd"] == pytest.approx(0.30)
        assert result["carried_over_usd"] == pytest.approx(0.33)

    def test_an_unmarked_retry_counts_as_the_full_run_it_looks_like(self):
        """The documented limit: a --retry-failed report from before PR #211.

        It has no ``retry_failed_from`` and nothing ``replayed``, and its cost
        summary is a full run's (from before 2026-09-04, ``total_usd`` alone).
        Nothing in it says what it carried over, and nothing here guesses.
        """
        capture, retry = _retry_failed_pair()
        del retry["retry_failed_from"]
        for call in retry["api_call_log"]:
            call.pop("replayed", None)
        retry["cost_summary"] = {"total_usd": 0.33}
        result = ha.cost_trend(_entries(capture, retry))
        assert result["total_usd"] == pytest.approx(0.63)
        assert result["retry_failed_runs"] == 0
        assert result["carried_over_usd"] == 0.0

    def test_provider_reliability_counts_each_call_once(self):
        capture, retry = _retry_failed_pair()
        result = ha.provider_reliability(_entries(capture, retry))
        assert result["gemini"]["total_calls"] == 1
        assert result["openai"]["total_calls"] == 1
        # Called in both runs: failed in the capture's, answered in the retry.
        assert result["grok"]["total_calls"] == 2
        assert result["grok"]["recent_success_rate"] == 0.5

    def test_a_failure_the_retry_carried_is_counted_once(self):
        """A failed pass the retry could not schedule stays the capture's.

        Still failed, and marked ``replayed``: one failed call, not two.
        """
        capture, retry = _retry_failed_pair()
        for call in retry["api_call_log"]:
            if call["pass"] == _RETRIED:
                call.update(failed=True, replayed=True)
        result = ha.provider_reliability(_entries(capture, retry))
        assert result["grok"]["total_calls"] == 1
        assert result["grok"]["recent_success_rate"] == 0.0

    def test_each_pass_is_billed_once_per_call_made(self):
        capture, retry = _retry_failed_pair()
        by_name = {s["pass"]: s for s in ha.pass_contribution(_entries(capture, retry))}
        assert by_name["gemini:fact_check"]["calls"] == 1
        assert by_name["gemini:fact_check"]["total_usd"] == pytest.approx(0.10)
        assert by_name["openai:voice_style"]["calls"] == 1
        assert by_name["openai:voice_style"]["total_usd"] == pytest.approx(0.15)
        # Two calls, both paid for: the one that failed, and the re-run.
        retried = by_name[_RETRIED]
        assert retried["calls"] == 2
        assert retried["failures"] == 1
        assert retried["total_usd"] == pytest.approx(0.05 + _RETRY_COST)

    def test_a_carried_pass_is_credited_in_the_run_that_made_it(self):
        capture, retry = _retry_failed_pair()
        by_name = {s["pass"]: s for s in ha.pass_contribution(_entries(capture, retry))}
        # p1, from the capture's Section 1. Not p1 again from the retry's, and
        # not p2, which cleared the bar on the re-run pass's vote.
        assert by_name["gemini:fact_check"]["consensus_hits"] == 1
        assert by_name["openai:voice_style"]["consensus_hits"] == 1
        retried = by_name[_RETRIED]
        assert retried["consensus_hits"] == 2
        # A carried result raised p2 as well, and that still corroborates it.
        assert retried["corroborated"] == 1
        assert retried["sole_source"] == 1

    def test_the_call_log_mark_is_enough_without_the_by_pass_one(self):
        """``by_pass`` has carried the mark only since PR #211.

        The call log has marked a --replay's entries since 2026-09-04, and it
        is what ``_carried_over`` reads. So a report whose ``by_pass``
        predates the mark — one of those replays, read with
        ``--history-root pipeline_history/_replay`` — is still billed only
        for what it made.
        """
        capture, retry = _retry_failed_pair()
        for cost_entry in retry["cost_summary"]["by_pass"]:
            cost_entry.pop("replayed", None)
        by_name = {s["pass"]: s for s in ha.pass_contribution(_entries(capture, retry))}
        assert by_name["gemini:fact_check"]["total_usd"] == pytest.approx(0.10)
        assert by_name[_RETRIED]["total_usd"] == pytest.approx(0.05 + _RETRY_COST)

    def test_a_carried_expansion_pass_offers_its_proposals_once(self):
        capture = {
            "api_call_log": [
                {"pass": "perplexity:expansion", "failed": False},
                {"pass": "gemini:expansion", "failed": True},
            ],
            "section_10_expansion": {
                "sources": [
                    {"title": "A", "proposed_by": ["perplexity"]},
                    {"title": "B", "proposed_by": ["perplexity"]},
                ]
            },
        }
        retry = {
            "retry_failed_from": "run_1_results.json",
            "api_call_log": [
                {"pass": "perplexity:expansion", "failed": False, "replayed": True},
                {"pass": "gemini:expansion", "failed": False},
            ],
            "section_10_expansion": {
                "sources": [
                    {"title": "A", "proposed_by": ["perplexity"]},
                    {"title": "B", "proposed_by": ["perplexity", "gemini"]},
                    {"title": "C", "proposed_by": ["gemini"]},
                ]
            },
        }
        by_name = {s["pass"]: s for s in ha.pass_contribution(_entries(capture, retry))}
        assert by_name["perplexity:expansion"]["proposals"] == 2
        assert by_name["gemini:expansion"]["proposals"] == 2

    def test_the_printed_total_says_what_it_left_out(self, tmp_path, capsys):
        """Through the real report builder, so the retry is matched to its run
        by the file name ``load_reports`` saw, not one a test assigned."""
        capture, retry = _retry_failed_pair()
        _write_report(tmp_path, "a", 1, "20260918_120000", capture)
        _write_report(tmp_path, "a", 2, "20260918_130000", retry)
        ha.print_history_report(ha.build_history_report(tmp_path))
        out = capsys.readouterr().out
        assert "Total spend: $0.3800 across 1 run(s)" in out
        assert (
            "(1 --retry-failed run(s) counted with the run whose failed calls "
            "each re-ran: $0.0800 spent; the $0.2500 of calls they carried over "
            "is not counted again)"
        ) in out
        assert "of their own" not in out

    def test_the_printed_total_says_when_a_retry_stands_alone(self, tmp_path, capsys):
        _, retry = _retry_failed_pair()
        _write_report(tmp_path, "a", 2, "20260918_130000", retry)
        ha.print_history_report(ha.build_history_report(tmp_path))
        out = capsys.readouterr().out
        assert "Total spend: $0.0800 across 1 run(s)" in out
        assert (
            "(1 of those counted as run(s) of their own: the run each re-ran is "
            "not in this history)"
        ) in out

    def test_a_history_without_one_prints_as_before(self, tmp_path, capsys):
        """The control: nothing carried over, nothing extra printed."""
        capture, _ = _retry_failed_pair()
        _write_report(tmp_path, "a", 1, "20260918_120000", capture)
        ha.print_history_report(ha.build_history_report(tmp_path))
        out = capsys.readouterr().out
        assert "Total spend: $0.3000 across 1 run(s)" in out
        assert "--retry-failed" not in out


class TestARetryFailedRunThePipelineWrote:
    """The same, over reports the pipeline itself wrote rather than hand-built.

    A stubbed run with one genuinely failed pass, then a --retry-failed run of
    its capture, both saved into one history directory the way a real
    article's runs are. Expectations are read off the two reports, so the
    stub preset can change without this test changing.
    """

    _FAILING = "openai:completeness"

    def _run_pair(self, tmp_path):
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        def _run_domain(model_name, domain, *a, **kw):
            if f"{model_name}:{domain}" == self._FAILING:
                # A complete, billed response that did not parse, so the
                # failed call has a cost of its own in the capture's run.
                return {
                    "failed": True,
                    "error": "Malformed JSON response",
                    "model": f"{model_name}-test-model",
                    "tokens": {"prompt": 1000, "completion": 700},
                    "_model": model_name,
                    "_domain": domain,
                }
            return _fake_run_domain(model_name, domain, *a, **kw)

        failing = patch(
            "ci_article_review.pipeline._run_domain", side_effect=_run_domain
        )
        with _stubbed_run(tmp_path, extra_patches=[failing], offline=True) as capture:
            pass
        (capture_path,) = (tmp_path / "history").rglob("*_results.json")
        with _stubbed_run(
            tmp_path, offline=True, retry_failed_results=str(capture_path)
        ) as retry:
            pass
        return capture, retry

    def test_every_call_counts_once_in_the_run_that_made_it(self, tmp_path):
        capture, retry = self._run_pair(tmp_path)
        carried = {c["pass"] for c in retry["api_call_log"] if c.get("replayed")}
        made = [c["pass"] for c in retry["api_call_log"] if not c.get("replayed")]
        assert carried and made == [self._FAILING], (
            f"the retry did not carry calls over, so this proves nothing: {made}"
        )
        assert retry["cost_summary"]["replayed_usd"] > 0

        result = ha.build_history_report(tmp_path / "history")
        assert result["total_reports"] == 2

        # Spend: the capture's whole bill, and what the retry bought, as one
        # run — the retry found its run by the capture path the pipeline wrote.
        cost = result["cost_trend"]
        assert cost["total_usd"] == pytest.approx(
            capture["cost_summary"]["total_usd"]
            + retry["cost_summary"]["incurred_usd"],
            abs=1e-4,
        )
        assert cost["runs"] == 1
        assert cost["retry_failed_runs"] == 1
        assert cost["retry_failed_unmatched"] == 0
        assert cost["carried_over_usd"] == pytest.approx(
            retry["cost_summary"]["replayed_usd"], abs=1e-4
        )

        # Calls: each pass once, but the failed one twice — it was called in
        # both runs — and its cost is both runs' spend on it.
        contribution = {s["pass"]: s for s in result["pass_contribution"]}
        for call in capture["api_call_log"]:
            name = call["pass"]
            assert contribution[name]["calls"] == (2 if name in made else 1), name

        def _usd(report, name):
            (entry,) = [
                p for p in report["cost_summary"]["by_pass"] if p["pass"] == name
            ]
            return entry["total_usd"]

        failed = contribution[self._FAILING]
        assert failed["failures"] == 1
        assert _usd(capture, self._FAILING) > 0
        assert failed["total_usd"] == pytest.approx(
            _usd(capture, self._FAILING) + _usd(retry, self._FAILING), abs=1e-4
        )

        # Provider reliability: every call made, none of those carried.
        made_by_provider = Counter(
            c["pass"].split(":", 1)[0]
            for report in (capture, retry)
            for c in report["api_call_log"]
            if not c.get("replayed") and ":" in c["pass"]
        )
        assert {
            provider: r["total_calls"]
            for provider, r in result["provider_reliability"].items()
        } == dict(made_by_provider)

        # Consensus: a carried pass keeps the hits its own run gave it.
        capture_hits = Counter(
            name
            for flag in capture["section_1_consensus"]
            for name in set(flag["models"])
        )
        for name in carried:
            assert contribution[name]["consensus_hits"] == capture_hits[name], name
