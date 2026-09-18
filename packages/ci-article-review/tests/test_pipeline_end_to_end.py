"""End-to-end coverage of ``run_draft_pipeline``, guarded by a golden report.

Why this file exists
--------------------
PR #43 consolidated three LLM call paths into a shared ci-core layer and silently
dropped three separate features, which had to be re-landed in PRs #46, #49 and #50.
The suite stayed green throughout. It could not have caught them: the tests that
existed covered *units in isolation*, and #43 changed how those units are **wired
together**, not what any one of them computes.

The audit (docs/AUDIT-2026-08.md, finding 6) measured the gap. Before this file,
no test in the suite had ever produced a report object — the three tests that
called ``run_draft_pipeline`` for real were each steered into an early
``sys.exit`` (empty draft, or ``_build_assignments`` patched to ``[]``), so
everything from the ensemble dispatch onward through consolidation, citations,
cost and the history save was never executed.

What this file does
-------------------
Runs the whole pipeline with every *network* boundary stubbed and every
*nondeterministic* input pinned, then compares the resulting report against a
committed golden file. A dropped field, section, or call-log entry fails with a
readable diff instead of vanishing silently.

The golden file is a fixture, not an assertion about correctness — it records
what the pipeline produces today. When a change legitimately alters the report,
regenerate it (see ``REGENERATE``) and **read the diff**: that diff is the
review artifact this file exists to produce.

Stub seam
---------
Model calls are stubbed at ``_run_domain`` rather than at the six provider
adapters. That is deliberate: ``_run_domain`` is the pipeline's own boundary, so
the test keeps working when adapters are refactored (exactly the PR #43 scenario)
while still exercising prompt assembly, the parallel dispatch, timeout wiring,
result re-keying, the API call log, consolidation, citations, cost and the save.
"""

import copy
import json
import os

from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ci_article_review.report_markdown import render_report_markdown
from ci_core.llm import cost

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review import ensemble_capture
from ci_article_review import report_markdown


GOLDEN_PATH = Path(__file__).parent / "golden" / "draft_run_report.json"

#: Set CI_REGENERATE_GOLDEN=1 to rewrite the golden file from a live run.
REGENERATE = os.environ.get("CI_REGENERATE_GOLDEN") == "1"


# ---------------------------------------------------------------------------
# Canned model output — one payload per domain, shaped like the real prompts
# ---------------------------------------------------------------------------

_DOMAIN_DATA = {
    "fact_check": {
        "confirmed": [
            {
                "claim": "The grid served 41 percent of load from nuclear.",
                "source": "EIA State Profile, https://example.org/eia-profile",
                "confidence": "high",
            }
        ],
        "outdated": [
            {
                "claim": "The 2019 figure was 33 percent.",
                "current_value": "38 percent as of 2025",
                "source": "https://example.org/updated-table",
                "confidence": "medium",
            }
        ],
        "contradicted": [],
        "unverifiable": [
            {
                "claim": "Local officials were not consulted.",
                "checked": "county board minutes",
                "reason": "no public record found",
            }
        ],
        "primary_source_needed": [
            {
                "claim": "Cooling draws 3 million gallons annually.",
                "best_candidate_source": "https://example.org/water-report",
            }
        ],
        "additional_observations": [],
    },
    "voice_style": {
        "flags": [
            {
                "passage": "It is important to note that the grid is complex.",
                "problem": "Filler opener with no content.",
                "suggested_rewrite": "The grid is complex.",
                "steelman_considered": "Could be signposting; it is not.",
            }
        ],
        "low_confidence": [
            {
                "passage": "This is a critical juncture.",
                "observation": "Borderline stock phrase.",
            }
        ],
        "additional_observations": [
            {
                "passage": "Local officials were not consulted.",
                "category": "red_team",
                "observation": "The claim names an omission without evidence of it.",
                "confidence": "high",
            }
        ],
    },
    "completeness": {
        "flags": [
            {
                "what_is_missing": "No mention of transmission constraints.",
                "passage_reference": "The grid is complex.",
                "audience_affected": "planning staff",
                "steelman_considered": "Out of scope? No — it drives the claim.",
            }
        ],
        "low_confidence": [],
        "additional_observations": [
            {
                "passage": "The grid served 41 percent of load from nuclear.",
                "category": "fact_check",
                "observation": "No source given for the 41 percent figure.",
                "confidence": "low",
            }
        ],
    },
    "argument_integrity": {
        "flags": [
            {
                "passage": "It is important to note that the grid is complex.",
                "logical_problem": "Asserts complexity without supporting the inference.",
                "steelman_considered": "Read as preamble, it still carries weight.",
                "why_it_survived": "The conclusion depends on it.",
            }
        ],
        "low_confidence": [],
        "additional_observations": [
            {
                "passage": "Local officials were not consulted.",
                "category": "red_team",
                "observation": "Naming who was not consulted invites a response.",
                "confidence": "medium",
            }
        ],
    },
    "red_team": {
        "most_vulnerable_claim": {
            "passage": "The 2019 figure was 33 percent.",
            "attack_vector": "The figure is stale.",
            "supporting_evidence_for_attack": "A newer table exists.",
        },
        "highest_audience_risk": {
            "passage": "The grid is complex.",
            "risk": "Reads as condescending to practitioners.",
            "audience_segment": "utility analysts",
        },
        "highest_credibility_risk": {
            "passage": "Local officials were not consulted.",
            "risk": "Unsourced negative claim about named parties.",
            "attack_vector": "Demand the record.",
        },
        "additional_observations": [],
    },
    # Only reached on an --expand run; absent from the golden fixture, which is
    # a default run.
    "expansion": {
        "sources": [
            {
                "title": "EIA Form 861, 2024 utility sales",
                "url": "https://example.org/eia-861",
                "where_to_look": "Table 6, sales by end use",
                "what_it_establishes": "the load growth the piece asserts",
                "supports": "The grid served 41 percent of load from nuclear.",
                "why_it_fits": "quantifies a claim already being made",
            },
            {
                "title": "A source that does not exist",
                "url": "https://example.org/invented",
                "where_to_look": "n/a",
                "what_it_establishes": "nothing — this URL is a fabrication",
                "supports": "The grid is complex.",
                "why_it_fits": "it does not; the link check is what catches it",
            },
        ],
        "topics": [
            {
                "topic": "Transmission interconnection queue timelines",
                "why_it_fits": "explains the delay the draft already asserts",
                "audience_served": "planning staff",
                "where_it_would_go": "after the cooling section",
                "closes_known_gap": None,
            }
        ],
        "angles": [],
        "data_points": [],
        "additional_observations": [],
    },
}

_HANDOFF = {
    "title": "A Grid Piece With A Sufficiently Long Title",
    "draft": (
        "# A Grid Piece With A Sufficiently Long Title\n\n"
        "It is important to note that the grid is complex. The 2019 figure was "
        "33 percent. The grid served 41 percent of load from nuclear.\n\n"
        "## Cooling\n\n"
        "Cooling draws 3 million gallons annually. Local officials were not "
        "consulted. See https://example.org/source-one for the underlying data.\n"
    ),
    "primary_claim": "Geography determines the answer.",
    "target_audience": "Planning staff and local officials.",
    "run_number": 1,
}

_CONFIG = {
    "api_keys": {
        "gemini": {"api_key": "k"},
        "openai": {"api_key": "k"},
        "mistral": {"api_key": "k"},
    },
    "pipeline": {
        "thoroughness": "standard",
        "grammar_pass": False,
        # Off so pre-analysis makes no network call; the citation pass below
        # is where the network-facing behaviour is exercised instead.
        "link_validation": False,
        "parallel_review_calls": True,
    },
    "publication": {
        "publication_description": "A publication.",
        "audience": {"primary": "planners"},
        "style_profile": "Direct.",
        "citation_sources": [{"name": "EIA", "adapter": "eia"}],
        "seo_rules": {"content_review": False},
    },
    "delta": {},
    "ensemble": {},
    "models": {
        "gemini": {"model": "gemini-2.5-flash"},
        "openai": {"model": "gpt-5.4"},
        "mistral": {"model": "mistral-large-latest"},
    },
}

_CURRENCY = {
    "warnings": [],
    "notices": [],
    "registry_warning": False,
    "registry_stale": False,
    "registry_date": "2026-08-01",
    "registry_age_days": 1,
}


def _fake_run_domain(model_name, domain, *a, **kw):
    """Stand in for one model call, in the adapter's return shape."""
    return {
        "failed": False,
        "data": _DOMAIN_DATA[domain],
        "model": f"{model_name}-test-model",
        "tokens": {"prompt": 1000, "completion": 200},
        "elapsed_seconds": 1.0,
        "grounding_available": domain == "fact_check" and model_name == "gemini",
        "_model": model_name,
        "_domain": domain,
    }


def _fake_resolve_citations(claims, *a, **kw):
    """Stand in for Pass 3 without touching the network."""
    return [
        {
            "claim": c["claim"] if isinstance(c, dict) else c,
            "source_name": "fact-check model",
            "url": "https://example.org/resolved",
            "resolved": True,
            "verification": "checksum",
            "checksum": "0" * 64,
            "checksum_basis": "extracted_text",
            "wayback": {"archived": True, "snapshot_age_days": 10},
        }
        for c in claims
    ]


# ---------------------------------------------------------------------------
# Normalisation — strip what legitimately varies between identical runs
# ---------------------------------------------------------------------------

_VOLATILE_KEYS = {
    "generated",
    "elapsed_seconds",
    "headroom_seconds",
    "timeout_budget_seconds",
    "registry_date",
    "registry_age_days",
    # Derived from today's date against a registry stamp, so it changes on its
    # own: "ok" becomes "notice" at 60 days and "warning" at 120, and this test
    # would start failing on a calendar boundary rather than on a code change.
    "registry_staleness",
    "prior_date",
}


def _normalise(obj):
    """Recursively blank volatile fields and canonicalise list order.

    Two things vary between byte-identical runs.

    *Volatile values*: timeouts are derived from wall-clock measurement and the
    model registry carries a real date, so both move without the pipeline
    changing.

    *List order*: results are collected via ``as_completed``, so the order of
    ``results`` — and therefore of ``api_call_log``, ``cost_summary.by_pass``
    and any section whose sort has ties — follows thread completion, which is
    genuinely nondeterministic. Lists of dicts are therefore sorted by a
    canonical serialisation before comparison.

    Sorting here means this file cannot assert an ordering, so the one ordering
    that carries meaning — consensus flags descending by weight — is asserted
    separately in ``TestPipelineBodyBehaviour``.
    """
    if isinstance(obj, dict):
        return {
            k: (
                "<volatile>" if k in _VOLATILE_KEYS and v is not None else _normalise(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        items = [_normalise(v) for v in obj]
        if all(isinstance(i, dict) for i in items):
            items.sort(key=lambda i: json.dumps(i, sort_keys=True))
        return items
    return obj


@contextmanager
def _stubbed_run(tmp_path, extra_patches=(), sleeps=None, **run_kwargs):
    """Execute the full draft pipeline against stubs.

    ``extra_patches`` are entered last, so a test can override any stub set up
    here — used to inject failures into optional passes, and to swap the config,
    while asserting the run still completes. ``run_kwargs`` go to
    ``run_draft_pipeline`` (``offline=True``, say).

    ``sleeps``, if given, is a list the pipeline's ``time.sleep`` durations are
    appended to — see the ``time.sleep`` stub below for why it is recorded
    rather than served.
    """
    from contextlib import ExitStack

    with ExitStack() as stack:
        p = lambda target, **kw: stack.enter_context(  # noqa: E731
            patch(f"ci_article_review.pipeline.{target}", **kw)
        )
        p("load_user_config", return_value={"pipeline": {}})
        p("load_publication_config", return_value={})
        p("merge_configs", return_value=_CONFIG)
        p("check_model_currency", return_value=_CURRENCY)
        p("_run_domain", side_effect=_fake_run_domain)
        # Record the pipeline's sleeps instead of serving them. Same-provider
        # calls are staggered by `provider_stagger_seconds` (default 3), so an
        # unstubbed run of this fixture spends its longest offset — 3 real
        # seconds — sitting in `_delay_start`, once per test that uses it.
        #
        # Recording rather than no-op'ing keeps every step of that path live:
        # the offsets are still computed, `_delay_start` still wraps each
        # runner, and the sleep is still called with the offset it computed.
        # `TestProviderStagger` below then asserts the durations, which is
        # strictly more than the wall-clock version proved — it endured the
        # delay and checked nothing. (ci_core.concurrency, which runs the
        # dispatch, does not sleep, so nothing else here is stubbed by this.)
        recorded = [] if sleeps is None else sleeps
        stack.enter_context(
            patch.object(pipeline.time, "sleep", side_effect=recorded.append)
        )
        # Positional `new` — HISTORY_ROOT is a module-level string, not a callable.
        stack.enter_context(
            patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "history"))
        )
        stack.enter_context(
            patch(
                "ci_article_review.adapters.citation.resolver.resolve_citations",
                side_effect=_fake_resolve_citations,
            )
        )
        stack.enter_context(
            patch(
                "ci_article_review.analysis.seo_suggest.generate",
                return_value=({"status": "skipped", "reason": "test"}, None),
            )
        )
        stack.enter_context(
            patch(
                "ci_article_review.analysis.seo_content.review",
                return_value=({"status": "skipped", "reason": "test"}, None),
            )
        )
        for ctx in extra_patches:
            stack.enter_context(ctx)
        # No timestamp injection needed: the only clock-derived values that
        # reach the report are normalised out below, and the run timestamp
        # otherwise only shapes the history filename, which is not compared.
        yield pipeline.run_draft_pipeline(
            None, "testpub", handoff=dict(_HANDOFF), **run_kwargs
        )


@pytest.fixture
def run_report(tmp_path):
    """Execute the full draft pipeline against stubs and return its report."""
    with _stubbed_run(tmp_path) as report:
        yield report


class TestGoldenReport:
    def test_report_matches_golden(self, run_report):
        """The whole report, field for field, against a committed fixture.

        This is the regression net for wiring changes. A refactor that drops a
        section, a call-log entry, or a single key fails here with a diff —
        which is the failure mode PR #43 did not have.
        """
        actual = _normalise(json.loads(json.dumps(run_report)))

        if REGENERATE:
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(
                json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            pytest.skip(f"Regenerated {GOLDEN_PATH.name} — re-run without the flag.")

        assert GOLDEN_PATH.exists(), (
            f"Golden file missing: {GOLDEN_PATH}. Regenerate with "
            "CI_REGENERATE_GOLDEN=1 uv run pytest "
            "packages/ci-article-review/tests/test_pipeline_end_to_end.py"
        )
        expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        # Compare as sorted-key JSON so the failure message is a readable diff
        # rather than a dict repr that pytest truncates.
        actual_s = json.dumps(actual, indent=2, sort_keys=True)
        expected_s = json.dumps(expected, indent=2, sort_keys=True)
        assert actual_s == expected_s, (
            "The pipeline's report changed.\n\n"
            "If the change is intended, regenerate the golden file and review "
            "the diff as part of the PR:\n"
            "  CI_REGENERATE_GOLDEN=1 uv run pytest "
            "packages/ci-article-review/tests/test_pipeline_end_to_end.py\n"
        )


class TestLiveModelCheckIsAdvisoryOnly:
    """Nothing about the newer-model check may cost a run its report.

    It is the last thing a run does, after thirty model calls have already been
    paid for, and it exists only to tell you a model shipped. Losing a report to
    it — or to a provider outage on the other side of it — would be a strictly
    worse trade than never having built it.
    """

    def test_a_check_that_raises_does_not_fail_the_run(self, tmp_path):
        boom = patch(
            "ci_article_review.pipeline.live_model_check.check",
            side_effect=RuntimeError("provider on fire"),
        )
        with _stubbed_run(tmp_path, extra_patches=[boom]) as report:
            assert report["section_1_consensus"] is not None
            live = report["model_currency"]["live"]
            assert live["status"] == "unavailable"
            assert live["newer"] == []

    def test_the_check_is_not_live_by_default(self, tmp_path):
        """Default config must not add a network call to a run."""
        with patch(
            "ci_article_review.pipeline.live_model_check.discover"
            ".collect_available_models"
        ) as sweep:
            with _stubbed_run(tmp_path) as report:
                assert report["model_currency"]["live"]["refreshed"] is False
        sweep.assert_not_called()

    def test_offline_never_queries_even_when_enabled(self, tmp_path):
        config = dict(_CONFIG)
        config["pipeline"] = {**_CONFIG["pipeline"], "live_model_check": True}
        # Entered after the fixture's own merge_configs stub, so this wins.
        enabled = patch("ci_article_review.pipeline.merge_configs", return_value=config)
        with patch(
            "ci_article_review.pipeline.live_model_check.discover"
            ".collect_available_models"
        ) as sweep:
            with _stubbed_run(
                tmp_path, extra_patches=[enabled], offline=True
            ) as report:
                assert report["model_currency"]["live"]["refreshed"] is False
        sweep.assert_not_called()

    def test_enabling_it_does_query_when_online(self, tmp_path):
        """The opt-in has to actually opt in, or the flag is decoration."""
        config = dict(_CONFIG)
        config["pipeline"] = {**_CONFIG["pipeline"], "live_model_check": True}
        enabled = patch("ci_article_review.pipeline.merge_configs", return_value=config)
        with patch(
            "ci_article_review.pipeline.live_model_check.discover"
            ".collect_available_models",
            return_value={},
        ) as sweep:
            with _stubbed_run(tmp_path, extra_patches=[enabled]):
                pass
        sweep.assert_called_once()


class TestReportSchemaContract:
    """The report's shape is a cross-package contract, not an internal detail.

    ``history_analytics``, ``voice_pattern_report`` and
    ``resolver.build_checksum_index`` all read ``pipeline_history/`` JSON. Before
    this, nothing asserted the producer and those three consumers agree — and
    the planned document_runs/document_results migration will move this schema.
    """

    _REQUIRED_TOP_LEVEL = {
        "article_title",
        "publication",
        "run_number",
        "generated",
        "section_1_consensus",
        "section_2_fact_check",
        "section_3_voice",
        "section_4_argument",
        "section_5_completeness",
        "section_6_red_team",
        "section_7_low_confidence",
        "section_8_additional",
        "section_9_citations",
        "api_call_log",
        "cost_summary",
        "pre_analysis",
        "ensemble",
        "model_currency",
    }

    def test_all_contracted_keys_present(self, run_report):
        missing = self._REQUIRED_TOP_LEVEL - set(run_report)
        assert not missing, (
            f"Report is missing contracted top-level keys: {sorted(missing)}. "
            "These are read by history_analytics, voice_pattern_report and/or "
            "resolver.build_checksum_index."
        )

    def test_every_model_domain_pair_appears_in_the_call_log(self, run_report):
        """A dropped pass must not vanish silently — it must show in the log."""
        logged = {e["pass"] for e in run_report["api_call_log"]}
        assigned = set(run_report["ensemble"]["assignments"])
        assert assigned <= logged, (
            f"Assignments absent from api_call_log: {sorted(assigned - logged)}"
        )

    def test_cost_summary_accounts_for_every_call(self, run_report):
        by_pass = {e["pass"] for e in run_report["cost_summary"]["by_pass"]}
        logged = {e["pass"] for e in run_report["api_call_log"]}
        assert logged == by_pass, (
            "cost_summary.by_pass and api_call_log disagree on which calls ran: "
            f"only in log {sorted(logged - by_pass)}, "
            f"only in cost {sorted(by_pass - logged)}"
        )

    def test_history_analytics_can_read_what_the_pipeline_writes(
        self, run_report, tmp_path
    ):
        """The producer/consumer contract, exercised rather than assumed."""
        from ci_article_review import history as hist
        from ci_article_review import history_analytics

        root = tmp_path / "contract"
        hist.save_run(str(root), run_report["article_title"], 1, run_report, [])
        loaded = list(history_analytics.load_reports(str(root)))
        assert len(loaded) == 1, "history_analytics could not load a fresh report"
        assert loaded[0]["report"]["article_title"] == run_report["article_title"]


class TestPipelineBodyBehaviour:
    """Assertions on the parts of the body the golden file cannot express."""

    def test_all_three_configured_models_are_dispatched(self, run_report):
        assignments = run_report["ensemble"]["assignments"]
        models = {a.split(":")[0] for a in assignments}
        assert models == {"gemini", "openai", "mistral"}, (
            f"Expected every credentialled model to be dispatched, got {models}"
        )

    def test_report_and_markdown_are_both_written(self, run_report, tmp_path):
        """Both artifacts, not just the JSON — the .md is what the user reads."""
        history = tmp_path / "history"
        assert history.exists(), "No history directory was created"
        written = {p.name.rsplit("_", 1)[-1] for p in history.rglob("run_*")}
        assert "report.json" in written, "No report.json written"
        assert "review.md" in written, "No review.md written"

    def test_consensus_flags_are_ordered_by_descending_weight(self, run_report):
        """The one ordering that carries meaning.

        ``_normalise`` canonicalises list order for the golden comparison, so
        this property has to be asserted here or it would go unguarded.
        """
        weights = [f["weight_sum"] for f in run_report["section_1_consensus"]]
        assert weights == sorted(weights, reverse=True), (
            f"Consensus flags are not ordered strongest-first: {weights}"
        )

    def test_citation_pass_receives_the_fact_check_claims(self, run_report):
        """Pass 3 must be fed from Pass 2's output, not run in isolation."""
        claims = {c["claim"] for c in run_report["section_9_citations"]}
        assert "The 2019 figure was 33 percent." in claims, (
            "An 'outdated' fact-check claim did not reach citation resolution"
        )


class TestProviderStagger:
    """Same-provider calls are spread; different providers all start at 0.

    ``_stagger_offsets`` is unit-tested in ``test_pipeline_timeout.py`` against a
    hand-built list of runner names. This asserts the wiring the unit test
    cannot see: that a real run's assignments reach it, and that every offset it
    returns is actually handed to ``_delay_start`` and slept.
    """

    def test_same_provider_calls_are_staggered_by_the_configured_interval(
        self, tmp_path
    ):
        slept = []
        with _stubbed_run(tmp_path, sleeps=slept) as report:
            models = [c["model"] for c in report["api_call_log"]]

        # One offset per runner beyond the first of its provider, at the
        # documented 3s default. A provider dispatched once contributes none.
        per_provider = Counter(m.split("-")[0] for m in models)
        expected = sorted(
            3 * i for count in per_provider.values() for i in range(1, count)
        )
        assert expected, (
            "No provider was dispatched more than once, so this run cannot "
            f"exercise staggering at all: {per_provider}"
        )
        assert sorted(slept) == expected, (
            f"Staggered sleeps {sorted(slept)} do not match the offsets the "
            f"dispatch implies for {dict(per_provider)}: {expected}"
        )


class TestAnEmptyPassIsNotLoggedAsOK:
    """A well-formed empty result is a third outcome, not a success.

    Observed on the 2026-09-09 honda-navigation run (thorough preset):

        mistral:argument_integrity: OK (55.18s, mistral-medium-3-5)

    followed, minutes later and hundreds of lines further down, by the
    end-of-run block reporting that same pass had returned nothing after
    spending 9,172 completion tokens. Section 4 had quietly dropped from three
    voters to two. The per-pass line is where a reader looks to see whether a
    pass ran, so it is the line that has to be right.
    """

    def _empty_one_pass(self, emptied):
        """Stub ``_run_domain`` so exactly one pass returns an empty payload.

        Which pass comes out of the dispatch rather than being named here, so
        this keeps testing something real if the preset's assignments change.
        The lock matters: ``parallel_review_calls`` is on in the stub config, so
        without it two threads can both claim to be first.
        """
        import threading

        lock = threading.Lock()

        def _run(model_name, domain, *a, **kw):
            result = _fake_run_domain(model_name, domain, *a, **kw)
            with lock:
                first = not emptied
                if first:
                    emptied.append(f"{model_name}:{domain}")
            if first:
                # Replaced, never mutated in place — _DOMAIN_DATA is shared
                # module state and every other test reads the same dicts.
                result["data"] = {key: [] for key in result["data"]}
            return result

        return _run

    def test_the_per_pass_line_says_empty_rather_than_ok(self, tmp_path, caplog):
        emptied = []
        stub = patch(
            "ci_article_review.pipeline._run_domain",
            side_effect=self._empty_one_pass(emptied),
        )
        with caplog.at_level("INFO"):
            with _stubbed_run(tmp_path, extra_patches=[stub], offline=True) as report:
                pass

        (name,) = emptied
        assert f"  {name}: EMPTY" in caplog.text, (
            f"{name} returned nothing and its status line did not say so:\n"
            f"{caplog.text}"
        )
        assert f"  {name}: OK (" not in caplog.text
        # The end-of-run block still says it too — this fix adds a line, it does
        # not move the warning.
        assert name in report["empty_results"]

    def test_the_call_log_records_it_as_empty_rather_than_ok(self, tmp_path):
        emptied = []
        stub = patch(
            "ci_article_review.pipeline._run_domain",
            side_effect=self._empty_one_pass(emptied),
        )
        with _stubbed_run(tmp_path, extra_patches=[stub], offline=True) as report:
            pass

        (name,) = emptied
        entry = next(e for e in report["api_call_log"] if e["pass"] == name)
        assert entry["status"] == "empty"
        # Still not a failure: it did not fail, and calling it one would send a
        # reader looking for an error that never happened.
        assert entry["failed"] is False
        assert report["model_failures"] == []

    def test_the_empty_pass_is_dropped_from_the_reported_width(self, tmp_path):
        """The width block claims to count what `_find_consensus` counts, and an
        empty payload casts no vote there."""
        emptied = []
        stub = patch(
            "ci_article_review.pipeline._run_domain",
            side_effect=self._empty_one_pass(emptied),
        )
        with _stubbed_run(tmp_path, extra_patches=[stub], offline=True) as report:
            pass

        (name,) = emptied
        model, domain = name.split(":", 1)
        width = report["ensemble"]["width"]
        assert model not in width["models_by_domain"][domain], (
            f"{name} returned nothing but is still counted as reviewing "
            f"{domain}: {width['models_by_domain'][domain]}"
        )

    def test_a_populated_pass_still_says_ok(self, tmp_path, caplog):
        """The control: nothing here reclassifies a pass that did find things."""
        with caplog.at_level("INFO"):
            with _stubbed_run(tmp_path, offline=True) as report:
                pass

        assert report["empty_results"] == []
        review_passes = [
            e["pass"] for e in report["api_call_log"] if ":" in (e.get("pass") or "")
        ]
        assert review_passes, "no review passes ran, so this proves nothing"
        for name in review_passes:
            assert f"  {name}: OK (" in caplog.text
            assert f"  {name}: EMPTY" not in caplog.text


def _calibration_lines(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("[CALIBRATION]")
    ]


class TestStreamTimingReachesTheReport:
    """The socket-budget measurements, from a result to where they are read.

    The client records one entry per stream on the result; this is the wiring
    after that: the report's ``api_call_log``, which history gets mined from,
    and the [CALIBRATION] line, which a person reads.
    """

    #: A stall the retry got past, then the stream that answered.
    _STREAMS = [
        {
            "first_byte_s": 120.02,
            "first_byte_censored": True,
            "first_output_s": 121.37,
            "max_gap_s": None,
            "max_gap_censored": False,
            "stream_read_timeout": 120.0,
            "stream_gap_timeout": 60.0,
            "cut_short_by": "StreamStalled",
        },
        {
            "first_byte_s": 14.5,
            "first_byte_censored": False,
            "first_output_s": 14.93,
            "max_gap_s": 3.25,
            "max_gap_censored": False,
            "stream_read_timeout": 120.0,
            "stream_gap_timeout": 60.0,
        },
    ]

    def _timed(self, model_name, domain, *a, **kw):
        result = _fake_run_domain(model_name, domain, *a, **kw)
        result["stream_timing"] = copy.deepcopy(self._STREAMS)
        return result

    def test_every_review_entry_in_the_call_log_carries_its_streams(self, tmp_path):
        stub = patch("ci_article_review.pipeline._run_domain", side_effect=self._timed)
        with _stubbed_run(tmp_path, extra_patches=[stub], offline=True) as report:
            pass

        review = [e for e in report["api_call_log"] if "status" in e]
        assert review, "no review passes ran, so this proves nothing"
        for entry in review:
            assert entry["stream_timing"] == self._STREAMS

    def test_the_calibration_line_gains_both_fields_at_its_end(self, tmp_path, caplog):
        stub = patch("ci_article_review.pipeline._run_domain", side_effect=self._timed)
        with caplog.at_level("INFO"):
            with _stubbed_run(tmp_path, extra_patches=[stub], offline=True):
                pass

        lines = _calibration_lines(caplog)
        assert lines, "no [CALIBRATION] lines were logged"
        for line in lines:
            # max_tokens follows them: appended after, for the same reason.
            assert line.endswith(
                " first_byte=>120.02s,14.5s max_gap=-,3.25s max_tokens=-"
            ), line
            # Appended, not interleaved: the nine fields anything already
            # splits this line into keep their positions.
            keys = [field.split("=", 1)[0] for field in line.split()[1:]]
            assert keys == [
                "model",
                "domain",
                "effort",
                "chars",
                "budget",
                "elapsed",
                "out_tokens",
                "status",
                "headroom",
                "first_byte",
                "max_gap",
                "max_tokens",
            ]

    def test_a_call_with_no_timing_says_so_rather_than_inventing_one(
        self, tmp_path, caplog
    ):
        """What a replay of a capture from before this existed looks like."""
        with caplog.at_level("INFO"):
            with _stubbed_run(tmp_path, offline=True) as report:
                pass

        lines = _calibration_lines(caplog)
        assert lines, "no [CALIBRATION] lines were logged"
        assert all(
            line.endswith(" first_byte=- max_gap=- max_tokens=-") for line in lines
        )
        assert not any("stream_timing" in e for e in report["api_call_log"])


class TestARecoveredCallStillCostsWhatItCost:
    """What a failed call was billed for, after recovery replaces it.

    ``_recover_failed_calls`` has its unit tests in test_pipeline_timeout.py.
    This is the wiring after it: the ``cost_summary`` a reader of the report
    sees, and the capture a later ``--replay`` re-reports it from.
    """

    _FLAKY = ("openai", "completeness")

    def _run(self, tmp_path, first_answer=None):
        """One stubbed run, the flaky call answering ``first_answer`` once."""
        answered = []

        def _run_domain(model_name, domain, *a, **kw):
            first_call = (model_name, domain) == self._FLAKY and not answered
            if first_answer is not None and first_call:
                answered.append(first_answer)
                return {
                    **copy.deepcopy(first_answer),
                    "_model": model_name,
                    "_domain": domain,
                }
            return _fake_run_domain(model_name, domain, *a, **kw)

        stub = patch("ci_article_review.pipeline._run_domain", side_effect=_run_domain)
        with _stubbed_run(tmp_path, extra_patches=[stub], offline=True) as report:
            pass
        if first_answer is not None:
            assert answered, "the flaky call never ran, so this proves nothing"
        return report

    def _failed(self, error, last_tokens, discarded_tokens, reason):
        return {
            "failed": True,
            "error": error,
            "model": "openai-test-model",
            "tokens": last_tokens,
            "discarded_attempts": {
                "count": 1,
                "costed": 1 if any(discarded_tokens.values()) else 0,
                "reasons": [reason],
                "tokens": discarded_tokens,
            },
        }

    def test_stalled_attempts_count_toward_the_floor(self, tmp_path):
        none = {"prompt": 0, "completion": 0}
        stalled = self._failed(
            "stream stalled mid-stream: nothing received for 120.0s",
            dict(none),
            dict(none),
            "StreamStalled",
        )
        clean = self._run(tmp_path / "clean")["cost_summary"]
        summary = self._run(tmp_path / "stalled", stalled)["cost_summary"]

        assert summary["discarded_calls"] == clean["discarded_calls"] + 2
        assert summary["uncosted_calls"] == clean["uncosted_calls"] + 2
        assert summary["total_usd"] == clean["total_usd"]

        # And the capture keeps them, so a replay of this run bills them too.
        capture = next((tmp_path / "stalled" / "history").rglob("*_results.json"))
        replayable = ensemble_capture.load(capture)
        assert replayable[":".join(self._FLAKY)]["discarded_attempts"]["count"] == 2

    def test_a_malformed_responses_tokens_are_billed(self, tmp_path):
        malformed = self._failed(
            "Malformed JSON response",
            {"prompt": 1000, "completion": 700},
            {"prompt": 1000, "completion": 600},
            "MalformedJSONError",
        )
        clean = self._run(tmp_path / "clean")["cost_summary"]
        summary = self._run(tmp_path / "malformed", malformed)["cost_summary"]

        assert summary["discarded_calls"] == clean["discarded_calls"] + 2
        assert summary["uncosted_calls"] == clean["uncosted_calls"]
        flaky = [p for p in summary["by_pass"] if p["pass"] == ":".join(self._FLAKY)]
        # The answer's 1000/200 and both failed attempts, priced as one bill.
        every_attempt = cost.calculate(
            [
                {
                    "model": "openai-test-model",
                    "tokens": {"prompt": 3000, "completion": 200 + 700 + 600},
                }
            ]
        )
        assert flaky[0]["total_usd"] == pytest.approx(
            every_attempt["by_pass"][0]["total_usd"], abs=1e-6
        )
        assert summary["total_usd"] > clean["total_usd"]


class TestRetryFailedBillsOnlyTheCallsItMakes:
    """A ``--retry-failed`` run's cost is its own calls, not its capture's.

    The capture's other results pass through unchanged, and the run that made
    them already paid for them. Until 2026-09-18 only ``--replay`` marked a
    capture's entries as history, so re-running one failed call worth $0.0045
    reported ``incurred_usd`` $0.0315 — the whole ensemble — plus any attempts
    the carried-over calls had discarded in their own run.
    """

    _RETRIED = "openai:completeness"
    #: Carried over, having discarded two attempts in the run that made it.
    _CARRIER = "gemini:red_team"
    _CARRIER_BILLING = {
        "count": 2,
        "costed": 1,
        "reasons": ["MalformedJSONError", "StreamStalled"],
        "tokens": {"prompt": 1000, "completion": 600},
    }

    def _capture(self, tmp_path):
        """A stubbed run's own capture, with ``_RETRIED`` marked failed."""
        with _stubbed_run(tmp_path / "original", offline=True):
            pass
        written = next((tmp_path / "original" / "history").rglob("*_results.json"))
        raw = ensemble_capture.load(written)
        assert {self._RETRIED, self._CARRIER} <= set(raw), (
            f"the stub preset no longer assigns both passes: {sorted(raw)}"
        )
        model, domain = self._RETRIED.split(":")
        raw[self._RETRIED] = {
            "failed": True,
            "error": "stream stalled mid-stream: nothing received for 120.0s",
            "model": f"{model}-test-model",
            "tokens": {"prompt": 0, "completion": 0},
            "_model": model,
            "_domain": domain,
        }
        raw[self._CARRIER]["discarded_attempts"] = copy.deepcopy(self._CARRIER_BILLING)
        path = tmp_path / "capture_results.json"
        ensemble_capture.save(path, raw, article_title="T", run_number=1)
        return str(path)

    def _retry(self, tmp_path, capture, extra_patches=()):
        with _stubbed_run(
            tmp_path / "retry",
            extra_patches=extra_patches,
            offline=True,
            retry_failed_results=capture,
        ) as report:
            pass
        return report

    def test_only_the_retried_call_is_incurred(self, tmp_path):
        capture = self._capture(tmp_path)
        report = self._retry(tmp_path, capture)

        this_run = [e["pass"] for e in report["api_call_log"] if not e.get("replayed")]
        assert this_run == [self._RETRIED]
        summary = report["cost_summary"]
        retried = next(p for p in summary["by_pass"] if p["pass"] == self._RETRIED)
        assert retried["total_usd"] > 0, (
            "the retry cost nothing, so this proves nothing"
        )
        # --offline, so the retried call is the only thing this run bought.
        assert summary["incurred_usd"] == pytest.approx(retried["total_usd"], abs=1e-4)
        assert report["retry_failed_from"] == capture
        assert "replayed_from" not in report

    def test_what_the_capture_paid_for_is_reported_as_history(self, tmp_path):
        capture = self._capture(tmp_path)
        summary = self._retry(tmp_path, capture)["cost_summary"]

        carried = [p for p in summary["by_pass"] if p["pass"] != self._RETRIED]
        assert carried, "nothing was carried over, so this proves nothing"
        assert all(p.get("replayed") for p in carried)
        assert summary["replayed_usd"] == pytest.approx(
            sum(p["total_usd"] for p in carried), abs=1e-4
        )
        # The carrier's discarded attempts are history too: its own run paid
        # for them, with the answer they preceded.
        carrier = next(p for p in carried if p["pass"] == self._CARRIER)
        every_attempt = cost.calculate(
            [
                {
                    "model": "gemini-test-model",
                    "tokens": {"prompt": 2000, "completion": 800},
                }
            ]
        )
        assert carrier["total_usd"] == pytest.approx(
            every_attempt["by_pass"][0]["total_usd"], abs=1e-6
        )
        split = summary["replayed_usd"] + summary["incurred_usd"]
        assert abs(split - summary["total_usd"]) < 0.0002

    def test_a_call_recovered_during_the_retry_is_this_runs_spend(self, tmp_path):
        """``--retry-failed`` runs the recovery pass too. What recovery buys is
        a new result, so neither it nor the attempt it replaced is history."""
        capture = self._capture(tmp_path)
        failed_once = []

        def _run_domain(model_name, domain, *a, **kw):
            if f"{model_name}:{domain}" == self._RETRIED and not failed_once:
                failed_once.append(True)
                return {
                    "failed": True,
                    "error": "Malformed JSON response",
                    "model": f"{model_name}-test-model",
                    "tokens": {"prompt": 1000, "completion": 700},
                    "_model": model_name,
                    "_domain": domain,
                }
            return _fake_run_domain(model_name, domain, *a, **kw)

        stub = patch("ci_article_review.pipeline._run_domain", side_effect=_run_domain)
        report = self._retry(tmp_path, capture, extra_patches=[stub])
        assert failed_once, "the retried call never failed, so this proves nothing"

        entry = next(e for e in report["api_call_log"] if e["pass"] == self._RETRIED)
        assert not entry.get("replayed")
        # The answer's 1000/200 and the malformed attempt's 1000/700.
        every_attempt = cost.calculate(
            [
                {
                    "model": "openai-test-model",
                    "tokens": {"prompt": 2000, "completion": 900},
                }
            ]
        )
        assert report["cost_summary"]["incurred_usd"] == pytest.approx(
            every_attempt["total_usd"], abs=1e-4
        )

    def test_the_printed_cost_leads_with_what_this_run_bought(self, tmp_path, capsys):
        capture = self._capture(tmp_path)
        capsys.readouterr()  # the capture's own run printed a summary too
        summary = self._retry(tmp_path, capture)["cost_summary"]
        out = capsys.readouterr().out

        block = out[out.index("\nEstimated cost: ") :].strip().split("\n\n")[0]
        headline, *lines = block.splitlines()
        assert headline.startswith(f"Estimated cost: ${summary['incurred_usd']:.4f}")
        priced = [
            line.split()[0] for line in lines if not line.lstrip().startswith("(")
        ]
        assert priced == [self._RETRIED]
        carried = sum(1 for p in summary["by_pass"] if p.get("replayed"))
        assert lines[-1] == (
            f"  ({carried} call(s) carried over from the capture were not re-run "
            f"— the capture's own run paid ${summary['replayed_usd']:.4f} for them)"
        )

    def test_a_plain_run_still_prints_its_whole_bill(self, tmp_path, capsys):
        """The control: a run that carried nothing over prints as it did."""
        with _stubbed_run(tmp_path, offline=True) as report:
            pass
        out = capsys.readouterr().out

        summary = report["cost_summary"]
        block = out[out.index("\nEstimated cost: ") :].strip().split("\n\n")[0]
        headline, *lines = block.splitlines()
        assert headline.startswith(f"Estimated cost: ${summary['total_usd']:.4f}")
        assert [line.split()[0] for line in lines] == [
            p["pass"] for p in summary["by_pass"]
        ]
        assert "carried over" not in block

    def test_a_replay_of_the_same_capture_still_incurs_nothing(self, tmp_path, capsys):
        """The control: the path that already marked history is unchanged."""
        capture = self._capture(tmp_path)
        capsys.readouterr()
        with _stubbed_run(
            tmp_path / "replay", offline=True, replay_results=capture
        ) as report:
            pass

        assert all(e.get("replayed") for e in report["api_call_log"])
        assert report["cost_summary"]["incurred_usd"] == 0.0
        assert report["replayed_from"] == capture
        assert "retry_failed_from" not in report
        assert "\nCost: $0.0000 — replayed from a capture" in capsys.readouterr().out


class TestATruncatedPassIsReportedAsIncomplete:
    """A pass cut off at its output ceiling, carried to every place it is read.

    Shaped like honda-navigation run 3's mistral:fact_check (2026-09-18): the
    salvaged JSON held `confirmed` and `outdated`, and nothing after — no
    `contradicted` at all. The report said so in one header line, and the
    end-of-run summary not at all.
    """

    _PASS = "gemini:fact_check"  # the fixture's only fact_check assignment
    _LOST = [
        "contradicted",
        "unverifiable",
        "primary_source_needed",
        "out_of_scope",
        "additional_observations",
    ]

    @staticmethod
    def _truncated(model_name, domain, *a, **kw):
        result = _fake_run_domain(model_name, domain, *a, **kw)
        if domain == "fact_check":
            full = _DOMAIN_DATA["fact_check"]
            result["data"] = {k: full[k] for k in ("confirmed", "outdated")}
            result["truncated"] = True
            result["max_tokens"] = 16000
            result["tokens"] = {"prompt": 1000, "completion": 16000}
            # Stopped inside the first `contradicted` entry, as run 3 did.
            result["raw"] = (
                json.dumps(result["data"])[:-1]
                + ', "contradicted": [{"claim": "Ten digits can count'
            )
        return result

    def _stub(self):
        return patch(
            "ci_article_review.pipeline._run_domain", side_effect=self._truncated
        )

    def test_the_call_log_records_the_ceiling_it_hit(self, tmp_path, caplog):
        with caplog.at_level("INFO"):
            with _stubbed_run(
                tmp_path, extra_patches=[self._stub()], offline=True
            ) as report:
                pass
        entry = next(e for e in report["api_call_log"] if e["pass"] == self._PASS)
        assert entry["status"] == "partial"
        assert entry["max_tokens"] == 16000
        assert "against a 16000-token ceiling" in caplog.text
        line = next(
            ln for ln in _calibration_lines(caplog) if "domain=fact_check" in ln
        )
        assert line.endswith(" max_tokens=16000"), line

    def test_the_report_says_which_buckets_never_arrived(self, tmp_path):
        with _stubbed_run(
            tmp_path, extra_patches=[self._stub()], offline=True
        ) as report:
            pass
        (detail,) = report["truncated_result_details"]
        assert detail == {
            "pass": self._PASS,
            "model": "gemini-test-model",
            "domain": "fact_check",
            "section": "SECTION 2: Factual Verification",
            "completion_tokens": 16000,
            "max_tokens": 16000,
            "missing_buckets": self._LOST,
            "last_bucket": "outdated",
            "cut_in": "contradicted",
        }

    def test_section_2_and_the_worklist_say_it_on_disk(self, tmp_path):
        with _stubbed_run(tmp_path, extra_patches=[self._stub()], offline=True):
            pass
        (review,) = (tmp_path / "history").rglob("run_1_*_review.md")
        section = review.read_text(encoding="utf-8").split("## SECTION 2")[1]
        section = section.split("## SECTION 3")[0]
        assert "Incomplete: gemini-test-model hit its output-token ceiling" in section
        assert "cut off inside `contradicted`" in section
        (worklist_md,) = (tmp_path / "history").rglob("run_1_*_worklist.md")
        assert "Built from an incomplete fact-check" in worklist_md.read_text(
            encoding="utf-8"
        )

    def test_the_end_of_run_summary_warns(self, tmp_path, capsys):
        with _stubbed_run(tmp_path, extra_patches=[self._stub()], offline=True):
            pass
        out = capsys.readouterr().out
        assert "model pass(es) were cut off at their output-token ceiling" in out
        assert (
            f"{self._PASS} truncated after 16000 output tokens (ceiling 16000)" in out
        )
        assert f"Never arrived: {', '.join(self._LOST)}." in out

    def test_a_clean_run_raises_none_of_it(self, tmp_path, capsys):
        with _stubbed_run(tmp_path, offline=True) as report:
            pass
        assert report["truncated_result_details"] == []
        assert "cut off at their output-token ceiling" not in capsys.readouterr().out


class TestNoTimeoutLiftsTheOutputCeiling:
    """A calibration run exists to measure a call's real length. Cut off at a
    ceiling, a call reports the ceiling instead — so --no-timeout lifts both."""

    def test_a_capped_provider_is_sent_its_models_own_limit(
        self, tmp_path, monkeypatch
    ):
        from ci_core.llm import output_tokens

        monkeypatch.setattr(output_tokens, "model_output_limit", lambda p, m: 128000)
        # A copy: the pipeline writes budgets back into the config it is given,
        # and _CONFIG is shared with every other test in this file.
        config = copy.deepcopy(_CONFIG)
        seen = {}

        def _capture(model_name, domain, *a, **kw):
            seen[model_name] = dict(a[5][model_name])  # the model_configs arg
            return _fake_run_domain(model_name, domain, *a, **kw)

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch("ci_article_review.pipeline.merge_configs", return_value=config),
                patch("ci_article_review.pipeline._run_domain", side_effect=_capture),
            ],
            offline=True,
            no_timeout=True,
        ):
            pass
        assert seen["mistral"]["max_tokens"] == 128000
        assert seen["mistral"]["timeout_seconds"] == 3600
        # The providers that send no ceiling are not handed one.
        assert "max_tokens" not in seen["openai"]
        assert "max_tokens" not in seen["gemini"]


class TestTheCallLogRecordsTheEffortThatRan:
    """The call log's `effort` sits beside the ceiling and budget sized from it.

    claude-opus-5 with no effort set thinks at high and is sized as high (see
    ci_core/llm/output_tokens.py). Logged as `effort=none`, a high-effort call's
    length and speed would be filed under none, in the record calibration is
    mined from.
    """

    @pytest.mark.parametrize(
        "configured,answered,expected",
        [
            ("claude-opus-5", "claude-opus-5", "high"),
            ("claude-sonnet-5", "claude-sonnet-5", "high"),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001", "none"),
            # Read off the model that answered: this fallback thinks only when
            # asked, so the same config ran it at none.
            ("claude-opus-5", "claude-sonnet-4-6", "none"),
        ],
    )
    def test_claude_with_no_effort_set(self, tmp_path, configured, answered, expected):
        config = copy.deepcopy(_CONFIG)
        config["api_keys"]["claude"] = {"api_key": "k"}
        config["models"]["claude"] = {"model": configured}

        def _answered_by(model_name, domain, *a, **kw):
            result = _fake_run_domain(model_name, domain, *a, **kw)
            if model_name == "claude":
                result["model"] = answered
                if answered != configured:
                    result["fallback_from"] = configured
            return result

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch("ci_article_review.pipeline.merge_configs", return_value=config),
                patch(
                    "ci_article_review.pipeline._run_domain", side_effect=_answered_by
                ),
            ],
            offline=True,
        ) as report:
            pass

        passes = [e for e in report["api_call_log"] if e["pass"].startswith("claude:")]
        assert passes, "claude reviewed nothing, so this proves nothing"
        assert {e["effort"] for e in passes} == {expected}
        # A provider off the list, with no effort set, still logs none.
        others = [e for e in report["api_call_log"] if e["pass"].startswith("openai:")]
        assert others and {e["effort"] for e in others} == {"none"}


class TestADomainWithNoReviewerReachesTheReport:
    """The drafter exclusion can empty a domain; the run must not hide it.

    `standard` assigns one model to voice_style, so declaring that model as the
    drafter excludes it and leaves the domain with no reviewer — measured
    2026-09-05 as the only preset/drafter pair that does this. Two repairs cover
    it, backfill at assignment time and substitution after the calls, and the
    report names the domain when neither could. All three run here against the
    real pipeline, because every other test of them supplies the assignments by
    hand and so cannot catch the wiring going missing.
    """

    def _config(self, **pipeline_overrides):
        cfg = copy.deepcopy(_CONFIG)
        cfg["pipeline"].update({"drafting_model": "openai", **pipeline_overrides})
        return cfg

    def _patch(self, cfg):
        return [patch("ci_article_review.pipeline.merge_configs", return_value=cfg)]

    def test_a_substitute_provider_reviews_it_instead(self, tmp_path):
        """The normal outcome: another configured model takes the domain."""
        with _stubbed_run(
            tmp_path, extra_patches=self._patch(self._config())
        ) as report:
            assignments = report["ensemble"]["assignments"]
            voice = [a for a in assignments if a.endswith(":voice_style")]
            assert voice, f"voice_style was never reviewed: {assignments}"
            assert not voice[0].startswith("openai:"), "the drafter reviewed itself"
            assert report["domains_not_run"] == []

    def test_with_both_repairs_off_the_report_says_it_was_not_reviewed(self, tmp_path):
        """The fallback signal, for the run neither repair can fix.

        Both are off here, not just substitution. Backfill runs earlier — at
        assignment time — so leaving it on repairs the domain before
        substitution is ever consulted, and the unreviewed case this asserts on
        never arises.
        """
        cfg = self._config(
            substitute_failed_domains=False, backfill_narrowed_domains=False
        )
        with _stubbed_run(tmp_path, extra_patches=self._patch(cfg)) as report:
            assert not [
                a
                for a in report["ensemble"]["assignments"]
                if a.endswith(":voice_style")
            ]
            (detail,) = report["domains_not_run"]
            assert detail["domain"] == "voice_style"
            assert detail["section"] == "SECTION 3: Voice and AI-Speak"
            assert "openai drafted this article" in detail["reason"]

            markdown = render_report_markdown(report)
            assert "Domains not reviewed (1)" in markdown
            assert "Not reviewed this run" in markdown
            # The section it actually applies to, not just the header block.
            section_3 = markdown.split("## SECTION 3:")[1].split("## SECTION 4:")[0]
            assert "Not reviewed this run" in section_3

    def test_an_ordinary_run_gains_neither_signal(self, tmp_path):
        """No drafter declared — nothing is excluded, so nothing is reported."""
        with _stubbed_run(tmp_path) as report:
            assert report["domains_not_run"] == []
            assert "Domains not reviewed" not in render_report_markdown(report)


class TestReproducibilityAcrossRuns:
    """The second run of a draft measures itself against the first, for free.

    ``test_reproducibility.py`` covers the counting rules against hand-built
    reports. This asserts the wiring those unit tests cannot see: that a real
    ``run_draft_pipeline`` writes a history entry the *next* real run finds,
    matches, and reports on — with no extra model call between them.

    The stubs return identical findings every run, so reproduction here is
    total by construction. That is the point: it isolates the plumbing. Whether
    real findings recur is a property of the models, and is what the feature
    exists to measure rather than to assume.
    """

    def test_a_first_run_reports_that_nothing_was_measured(self, tmp_path):
        with _stubbed_run(tmp_path) as report:
            block = report["reproducibility"]

        assert block["comparable_run_count"] == 0
        assert block["skipped_count"] == 0
        # No measurement was taken, so no finding may carry a count.
        assert all("reproduced_in" not in f for f in report["section_1_consensus"])

    def test_a_second_run_is_measured_against_the_first(self, tmp_path):
        with _stubbed_run(tmp_path) as first:
            first_calls = len(first["api_call_log"])

        with _stubbed_run(tmp_path) as second:
            block = second["reproducibility"]
            second_calls = len(second["api_call_log"])

        assert block["comparable_run_count"] == 1, (
            "The second run of an unchanged draft did not find the first in "
            f"pipeline_history/. Skipped: {block['skipped']}"
        )
        assert (
            block["draft_fingerprint"] == first["reproducibility"]["draft_fingerprint"]
        )
        # Free: the comparison adds no model calls to the run that benefits.
        assert second_calls == first_calls

        for section in ("section_1_consensus", "section_3_voice"):
            for finding in second[section]:
                assert finding["reproduced_of"] == 1
                assert finding["reproduced_in"] == 1, (
                    f"{section} finding did not match its own prior run: "
                    f"{finding.get('passage')!r}"
                )

    def test_the_second_run_renders_the_measured_caveat(self, tmp_path):
        from ci_article_review.report_markdown import render_report_markdown

        with _stubbed_run(tmp_path):
            pass
        with _stubbed_run(tmp_path) as second:
            md = render_report_markdown(second)

        assert "Measured on this draft" in md
        assert "also in 1 of 1 comparable prior runs" in md
        assert md.index("Reading this report") < md.index("SECTION 1")

    def test_an_edited_draft_is_not_compared_against_the_old_one(self, tmp_path):
        """A revision must not inherit the previous draft's corroboration."""
        with _stubbed_run(tmp_path):
            pass

        edited = dict(_HANDOFF)
        edited["draft"] = edited["draft"] + "\n\nA newly added closing paragraph."
        with patch.dict(_HANDOFF, edited, clear=False):
            with _stubbed_run(tmp_path) as second:
                block = second["reproducibility"]

        assert block["comparable_run_count"] == 0
        assert block["skipped_count"] == 1
        assert block["skipped"][0]["reason"] == "different draft"

    def test_identical_runs_report_nothing_missed(self, tmp_path):
        """The dropped-findings block must not invent a miss out of a match.

        The stubs return the same findings every run, so a correct
        implementation drops nothing. This is the end-to-end guard on that: if
        the key derivation on the current run ever diverges from the one
        applied to history, every finding would look absent from itself and
        this block would fill with phantom misses — the most damaging way this
        feature could fail, since it accuses the run of missing real work.
        """
        with _stubbed_run(tmp_path):
            pass
        with _stubbed_run(tmp_path) as second:
            dropped = second["reproducibility"]["dropped"]
            md = render_report_markdown(second)

        assert second["reproducibility"]["comparable_run_count"] == 1
        assert dropped["total"] == 0, dropped["findings"]
        assert "may have missed" not in md


class TestExpansionRunsEndToEnd:
    """The opt-in pass, wired from the flag through to the rendered markdown.

    The unit tests in ``test_expansion.py`` cover each piece. This covers the
    wiring between them, which is the half that has broken before (PR #43): the
    flag reaching ``_build_assignments``, the domain reaching the dispatch, its
    output reaching consolidation, the URL check running over that output, and
    Section 10 reaching the file the author actually reads.
    """

    def _fake_links(self, text, check_wayback=True, **kwargs):
        """One proposed URL resolves; the fabricated one 404s."""
        return [
            {"url": url, "ok": True, "status_code": 200, "verified_via": "direct"}
            if "invented" not in url
            else {"url": url, "ok": False, "status_code": 404}
            for url in text.split()
        ]

    def _config(self):
        """`standard` assigns expansion to perplexity alone, so this test needs
        it configured. Scoped here rather than added to the shared _CONFIG."""
        cfg = copy.deepcopy(_CONFIG)
        cfg["api_keys"]["perplexity"] = {"api_key": "k"}
        cfg["models"]["perplexity"] = {"model": "sonar-pro"}
        return cfg

    def _run(self, tmp_path, **kwargs):
        links = patch(
            "ci_article_review.analysis.links.validate_links",
            side_effect=self._fake_links,
        )
        cfg = patch(
            "ci_article_review.pipeline.merge_configs", return_value=self._config()
        )
        # The expansion pass reads each proposed source to see whether it says
        # what the model claimed. `_fake_links` stubs the *URL* check one layer
        # above it; this stubs the read itself, which was going out to the real
        # web on every run of these four tests. Nothing here asserts on
        # `source_check` or `source_verdict` — `url_check` is what these cover —
        # so the live fetch only ever contributed flakiness.
        verify = patch(
            "ci_article_review.adapters.citation.resolver.verify_source_supports",
            return_value={
                "verification": "unverifiable",
                "note": "stubbed: end-to-end wiring test does not fetch",
            },
        )
        return _stubbed_run(
            tmp_path, extra_patches=[cfg, links, verify], expand=True, **kwargs
        )

    def test_the_default_run_does_not_schedule_it(self, tmp_path):
        cfg = patch(
            "ci_article_review.pipeline.merge_configs", return_value=self._config()
        )
        with _stubbed_run(tmp_path, extra_patches=[cfg]) as report:
            assert report["section_10_expansion"] == {}
            assert not [
                a for a in report["ensemble"]["assignments"] if "expansion" in a
            ]

    def test_the_flag_schedules_it_and_its_output_reaches_the_report(self, tmp_path):
        with self._run(tmp_path) as report:
            assert "perplexity:expansion" in report["ensemble"]["assignments"]
            expansion = report["section_10_expansion"]
            assert [s["title"] for s in expansion["sources"]] == [
                "EIA Form 861, 2024 utility sales",
                "A source that does not exist",
            ]
            assert expansion["models"] == ["perplexity"]

    def test_the_fabricated_url_is_marked_and_still_present(self, tmp_path):
        with self._run(tmp_path) as report:
            sources = report["section_10_expansion"]["sources"]
            assert sources[0]["url_status"] == "ok"
            assert sources[1]["url_status"] == "missing"
            assert report["section_10_expansion"]["url_check"] == {
                "checked": 2,
                "resolved": 1,
                "archived": 0,
                "blocked": 0,
                "missing": 1,
                "not_citable": 0,
                "no_url": 0,
                "followed_redirects": 0,
            }

    def test_it_does_not_leak_into_the_defect_sections(self, tmp_path):
        """Section 10 is additive; nothing it proposed may be ranked as a flag."""
        with self._run(tmp_path) as report:
            titles = {
                "EIA Form 861, 2024 utility sales",
                "A source that does not exist",
            }
            for key in ("section_1_consensus", "section_5_completeness"):
                rendered = json.dumps(report[key])
                assert not any(t in rendered for t in titles)

    def test_section_10_reaches_the_rendered_markdown(self, tmp_path):
        with self._run(tmp_path) as report:
            markdown = report_markdown.render_report_markdown(report)
        assert "## SECTION 10: Expansion Candidates" in markdown
        assert "EIA Form 861, 2024 utility sales" in markdown
        assert "LINK DEAD" in markdown
        # Last section in the file: a menu placed above the triage list is how
        # the triage list stops being read.
        assert markdown.index("SECTION 10") > markdown.index("SECTION 9")


class TestExpansionSurvivesRetryFailed:
    """``--retry-failed`` was the one entry point expansion had never taken.

    ``--replay`` was covered; this is its sibling, and the one that actually
    makes calls. A capture in which the expansion pass failed must come back
    with Section 10 populated after the retry — the path from "capture says
    this pass died" through the re-run to the rebuilt section.
    """

    def _capture(self, tmp_path, failed_names=()):
        """A capture of a full run, with the named passes marked failed."""
        raw = {}
        for model, domain in (
            ("gemini", "fact_check"),
            ("openai", "voice_style"),
            ("openai", "completeness"),
            ("mistral", "argument_integrity"),
            ("mistral", "red_team"),
            ("perplexity", "expansion"),
        ):
            name = f"{model}:{domain}"
            if name in failed_names:
                raw[name] = {
                    "failed": True,
                    "error": "stream stalled",
                    "model": f"{model}-test-model",
                    "tokens": {},
                    "_model": model,
                    "_domain": domain,
                }
            else:
                raw[name] = _fake_run_domain(model, domain)
        path = tmp_path / "capture_results.json"
        ensemble_capture.save(path, raw, article_title="T", run_number=1)
        return str(path)

    def _config(self):
        """perplexity configured, so `standard` assigns expansion to it rather
        than backfilling gemini in — this class is about the retry, and a
        backfilled substitute would answer a different question."""
        cfg = copy.deepcopy(_CONFIG)
        cfg["api_keys"]["perplexity"] = {"api_key": "k"}
        cfg["models"]["perplexity"] = {"model": "sonar-pro"}
        return cfg

    def _patches(self):
        return [
            patch(
                "ci_article_review.pipeline.merge_configs", return_value=self._config()
            ),
            patch(
                "ci_article_review.analysis.links.validate_links",
                side_effect=lambda text, **kw: [
                    {"url": u, "ok": True, "status_code": 200, "verified_via": "direct"}
                    for u in text.split()
                ],
            ),
            patch(
                "ci_article_review.adapters.citation.resolver.verify_source_supports",
                return_value={
                    "verification": "checksum",
                    "relevance_reason": "backs it",
                },
            ),
        ]

    def _run(self, tmp_path, capture):
        return _stubbed_run(
            tmp_path,
            extra_patches=self._patches(),
            expand=True,
            retry_failed_results=capture,
        )

    def test_a_failed_expansion_pass_is_re_run_and_lands_in_section_10(self, tmp_path):
        capture = self._capture(tmp_path, failed_names={"perplexity:expansion"})
        with self._run(tmp_path, capture) as report:
            expansion = report["section_10_expansion"]
            assert expansion, "the retried pass did not reach consolidation"
            assert expansion["models"] == ["perplexity"]
            assert [s["title"] for s in expansion["sources"]] == [
                "EIA Form 861, 2024 utility sales",
                "A source that does not exist",
            ]
            assert "perplexity:expansion" not in report["model_failures"]

    def test_a_capture_naming_a_pass_this_run_cannot_schedule_says_so(
        self, tmp_path, caplog
    ):
        """The gap the preset change opened: a capture from a run whose
        assignments differ names passes with no runner here."""
        capture = self._capture(tmp_path, failed_names={"perplexity:expansion"})
        # Same capture, replayed by a run that never asked for expansion.
        with caplog.at_level("WARNING"):
            with _stubbed_run(
                tmp_path,
                extra_patches=self._patches(),
                retry_failed_results=capture,
            ) as report:
                assert report["section_10_expansion"] == {}
        assert "perplexity:expansion" in caplog.text
        assert "NOT re-attempted" in caplog.text
