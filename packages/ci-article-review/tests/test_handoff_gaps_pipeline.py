"""The gap report on the two input paths that lose the most metadata.

``--draft`` at least *offers* every field; the author can see what they left
blank. The other two paths cannot:

* ``--raw-draft`` carries no metadata at all — ``build_handoff_from_raw_text``
  synthesises ``{title, draft, run_number}`` and nothing else, so every review
  domain runs on a frame the author never got to state.
* ``--url`` is the same shape, arrived at differently: ``build_handoff_from_url``
  can extract a title and a body from a published page, and cannot infer intent
  from either.

Both used to warn once, to the log, and then produce a report indistinguishable
from a fully-specified run. These tests hold that the report now says what was
lost — and, in ``TestNoProposalReachesAModel``, that saying so does not quietly
become filling it in.
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review.handoff_parser import build_handoff_from_raw_text
from ci_article_review.handoff_gaps import _sections_for
from ci_article_review.report_markdown import render_report_markdown


_RAW_DRAFT = (
    "# The Water Figures Do Not Travel\n"
    "\n"
    "Applying arid-geography water figures to a Great Lakes site is not "
    "analysis, it is a category error, and it has now reached three separate "
    "planning documents without anyone checking where the numbers came from.\n"
    "\n"
    "## Cooling\n"
    "\n"
    "Cooling draws 3 million gallons annually. Local officials were not "
    "consulted.\n"
)

_CONFIG = {
    "api_keys": {"openai": {"api_key": "k"}},
    "pipeline": {
        "thoroughness": "standard",
        "grammar_pass": False,
        "link_validation": False,
        "parallel_review_calls": False,
    },
    "publication": {
        "publication_description": "A publication.",
        "audience": {"primary": "Planning staff and commissioners."},
        "style_profile": "Direct.",
        "seo_rules": {"suggestions": False, "content_review": False},
    },
    "delta": {},
    "ensemble": {},
    "models": {"openai": {"model": "gpt-5.4"}},
}

_CURRENCY = {
    "warnings": [],
    "notices": [],
    "registry_warning": False,
    "registry_stale": False,
    "registry_date": "2026-08-01",
    "registry_age_days": 1,
}

_EMPTY_DOMAIN_DATA = {
    "fact_check": {
        "confirmed": [],
        "outdated": [],
        "contradicted": [],
        "unverifiable": [],
        "primary_source_needed": [],
        "additional_observations": [],
    },
    "voice_style": {"flags": [], "low_confidence": [], "additional_observations": []},
    "completeness": {"flags": [], "additional_observations": []},
    "argument_integrity": {"flags": [], "additional_observations": []},
    "red_team": {
        "strongest_counterargument": {},
        "highest_audience_risk": {},
        "additional_observations": [],
    },
}


@contextmanager
def _run(handoff, prompts=None, **run_kwargs):
    """Run the whole draft pipeline offline, recording every prompt sent.

    ``prompts`` collects the *user* prompt each domain would have received —
    built here from the same ``_build_user_prompt`` the real ``_run_domain``
    calls, so what the assertions read is what a model would have read.
    """
    recorded = [] if prompts is None else prompts

    def _record(model_name, domain, draft, handoff_arg, *a, **kw):
        recorded.append(pipeline._build_user_prompt(draft, handoff_arg))
        return {
            "failed": False,
            "data": _EMPTY_DOMAIN_DATA[domain],
            "model": f"{model_name}-test-model",
            "tokens": {"prompt": 10, "completion": 5},
            "elapsed_seconds": 0.1,
            "_model": model_name,
            "_domain": domain,
        }

    with ExitStack() as stack:
        p = lambda target, **kw: stack.enter_context(  # noqa: E731
            patch(f"ci_article_review.pipeline.{target}", **kw)
        )
        p("load_user_config", return_value={"pipeline": {}})
        p("load_publication_config", return_value={})
        p("merge_configs", return_value=_CONFIG)
        p("check_model_currency", return_value=_CURRENCY)
        p("_run_domain", side_effect=_record)
        stack.enter_context(patch.object(pipeline.time, "sleep"))
        yield pipeline.run_draft_pipeline(
            None, "testpub", handoff=dict(handoff), offline=True, **run_kwargs
        )


@pytest.fixture
def raw_draft_report(tmp_path):
    handoff = build_handoff_from_raw_text(_RAW_DRAFT, source_name="water-piece")
    with patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "h")):
        with _run(handoff) as report:
            yield report


@pytest.fixture
def url_report(tmp_path):
    """A handoff in the exact shape ``build_handoff_from_url`` returns."""
    handoff = {
        "title": "The Water Figures Do Not Travel",
        "draft": _RAW_DRAFT,
        "run_number": 1,
    }
    with patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "h")):
        with _run(handoff) as report:
            yield report


def _by_field(report, field):
    (gap,) = [g for g in report["handoff_gaps"] if g["field"] == field]
    return gap


def _domains_that_ran(report):
    """The review domains this run actually executed, from its own call log.

    Derived from the report rather than asserted as a constant so the test
    keeps meaning what it says if the stub config's thoroughness changes.
    """
    return {
        entry["pass"].split(":", 1)[1]
        for entry in report["api_call_log"]
        if ":" in (entry.get("pass") or "")
    }


class TestRawDraftPath:
    """--raw-draft carries no metadata, so every field is a gap."""

    def test_the_report_carries_the_gap_list(self, raw_draft_report):
        fields = {g["field"] for g in raw_draft_report["handoff_gaps"]}
        assert {
            "primary_claim",
            "target_audience",
            "pre_draft_analysis",
            "sources_cited",
            "uncertain_sections",
            "known_gaps",
            "history_key",
        } <= fields

    def test_the_claim_gap_names_only_the_sections_this_run_actually_built(
        self, raw_draft_report
    ):
        """Every section named must be one this run actually produced.

        Asserted as an invariant against the run's own call log rather than as
        a fixed list. The earlier version pinned the expected sections to what
        one model covered at ``standard``, and the ensemble-width work that
        retired that preset widened the fixture from two domains to five —
        failing a test whose subject had not changed. What matters is that the
        report never claims a degradation to a section the run never built.
        """
        domains_ran = _domains_that_ran(raw_draft_report)
        gap = _by_field(raw_draft_report, "primary_claim")
        assert gap["severity"] == "critical"
        assert gap["domains"], "the claim gap named no domain at all"
        assert set(gap["domains"]) <= domains_ran
        assert gap["sections"] == _sections_for(gap["domains"])

    def test_a_narrowed_run_names_only_the_one_domain_it_ran(self, tmp_path):
        """``--only-domain`` is the case the fixture above can no longer show.

        With every domain running, "does not blame a pass that never ran" is
        vacuously true. Narrowing the run to one domain makes it load-bearing
        again at the pipeline level, not only in the unit tests.
        """
        handoff = build_handoff_from_raw_text(_RAW_DRAFT, source_name="water-piece")
        with patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "h")):
            with _run(handoff, only_domain="completeness") as report:
                gap = _by_field(report, "primary_claim")

        assert _domains_that_ran(report) == {"completeness"}
        assert gap["domains"] == ["completeness"]
        assert gap["sections"] == ["SECTION 5: Completeness and Framing"]
        assert "SECTION 4" not in " ".join(gap["sections"])
        assert "SECTION 6" not in " ".join(gap["sections"])

    def test_the_audience_gap_proposes_the_publication_config_audience(
        self, raw_draft_report
    ):
        gap = _by_field(raw_draft_report, "target_audience")
        assert "Planning staff and commissioners." in gap["suggestion"]

    def test_the_rendered_report_shows_the_block_and_the_paste_text(
        self, raw_draft_report
    ):
        md = render_report_markdown(raw_draft_report)
        assert "Handoff metadata gaps" in md
        assert "PRIMARY CLAIM" in md
        assert "```" in md.split("Handoff metadata gaps")[1]
        # And the per-section note points back at it.
        section_5 = md.split("## SECTION 5")[1]
        assert "Built without" in section_5.split("##")[0]

    def test_the_block_states_that_nothing_proposed_was_used(self, raw_draft_report):
        md = render_report_markdown(raw_draft_report)
        assert "not used in this run" in md.lower()


class TestUrlPath:
    """--url synthesises a title and a body and can infer nothing else."""

    def test_the_report_carries_the_gap_list(self, url_report):
        fields = {g["field"] for g in url_report["handoff_gaps"]}
        assert "primary_claim" in fields
        assert "target_audience" in fields
        assert "pre_draft_analysis" in fields

    def test_a_url_run_is_not_reported_as_missing_its_title(self, url_report):
        """The page supplied one; only the fields it could not supply are gaps."""
        assert "title" not in {g["field"] for g in url_report["handoff_gaps"]}

    def test_the_rendered_report_shows_the_block(self, url_report):
        assert "Handoff metadata gaps" in render_report_markdown(url_report)


class TestNoProposalReachesAModel:
    """The proposal is for the author. The run must not have used it.

    This is the property the whole feature turns on. Inferring a primary claim
    and reviewing against it would produce a report that reads as though the
    handoff had been complete — the exact silent degradation being fixed, with
    a confident wrong frame substituted for a missing one.
    """

    def test_no_prompt_carried_a_primary_claim(self, tmp_path):
        prompts = []
        handoff = build_handoff_from_raw_text(_RAW_DRAFT, source_name="water-piece")
        with patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "h")):
            with _run(handoff, prompts=prompts) as report:
                pass

        assert prompts, "no model prompts were captured"
        proposed = _by_field(report, "primary_claim")["suggestion"]
        assert proposed, "the run proposed no claim, so this proves nothing"

        claim_text = proposed.split("\n", 1)[1]
        for prompt in prompts:
            assert "PRIMARY CLAIM" not in prompt
            assert "TARGET AUDIENCE" not in prompt
            # The candidate is drawn from the draft, so it legitimately appears
            # in the draft body. What must not appear is it being *presented*
            # as the claim.
            assert f"PRIMARY CLAIM: {claim_text}" not in prompt

    def test_the_handoff_the_run_used_was_never_amended(self, tmp_path):
        handoff = build_handoff_from_raw_text(_RAW_DRAFT, source_name="water-piece")
        before = dict(handoff)
        with patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "h")):
            with _run(handoff) as report:
                assert report["handoff_gaps"]
        assert handoff == before


class TestASufficientHandoffProducesNoBlock:
    def test_no_gaps_means_no_section_in_the_report(self, tmp_path):
        handoff = {
            "title": "The Water Figures Do Not Travel",
            "draft": _RAW_DRAFT,
            "run_number": 1,
            "primary_claim": "Arid-geography water figures do not transfer.",
            "target_audience": "Primary: planning staff.",
            "pre_draft_analysis": "Steelmanned position: the figures are regional.",
            "sources_cited": "None provided.",
            "uncertain_sections": "None identified by author.",
            "known_gaps": "None identified by author.",
            "additional_context": "Follows the March piece.",
            "history_key": "water-figures",
            "drafted_with": "claude",
        }
        with patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "h")):
            with _run(handoff) as report:
                assert "handoff_gaps" not in report
                md = render_report_markdown(report)
        assert "Handoff metadata gaps" not in md
        assert "Built without" not in md
