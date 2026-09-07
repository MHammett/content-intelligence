"""The drafting model is kept out of the review domain it cannot judge.

``ai_speak.txt`` asks the reviewer to flag hedging, throat-clearing, vague
significance gesturing and the problem→cause→solution skeleton. Those are AI
defaults, so the model that wrote the draft is the worst available judge of
them — it under-reports its own habits. The pipeline therefore drops the
declared drafting model from ``voice_style``.

Declaration has two sources: ``pipeline.drafting_model`` in user.yaml as a
standing default, and a ``Drafted with:`` line in the handoff for one article.
"""

import logging

import pytest

from ci_article_review import report_markdown
from ci_article_review.consolidation import build_report
from ci_article_review.handoff_parser import parse_draft_submission
from ci_article_review.pipeline import (
    _THOROUGHNESS_PRESETS,
    _build_assignments,
    _domains_never_attempted,
    _drafter_is_excluded,
    _drafting_model,
)

ALL_KEYS = {
    m: {"api_key": "k"}
    for m in ("openai", "gemini", "mistral", "grok", "perplexity", "claude")
}
ALL_ENABLED = {m: {} for m in ALL_KEYS}


class TestDeclaringTheDraftingModel:
    def test_config_supplies_the_default(self):
        assert _drafting_model({}, {"drafting_model": "claude"}) == "claude"

    def test_handoff_overrides_the_config(self):
        """The drafting tool can change per article; the config cannot."""
        got = _drafting_model({"drafted_with": "openai"}, {"drafting_model": "claude"})
        assert got == "openai"

    def test_handoff_alone_is_enough(self):
        assert _drafting_model({"drafted_with": "gemini"}, {}) == "gemini"

    def test_undeclared_is_none(self):
        assert _drafting_model({}, {}) is None
        assert _drafting_model({"drafted_with": "  "}, {"drafting_model": ""}) is None

    def test_name_is_case_and_space_insensitive(self):
        assert _drafting_model({"drafted_with": "  Claude "}, {}) == "claude"

    def test_unknown_name_warns_and_excludes_nothing(self, caplog):
        """A typo costs a dropped review pass, not the run."""
        with caplog.at_level(logging.WARNING):
            assert _drafting_model({"drafted_with": "ChatGPT"}, {}) is None
        assert "ChatGPT" in caplog.text


class TestExclusionScope:
    def test_drafter_is_excluded_from_voice_style(self):
        assert _drafter_is_excluded("claude", "voice_style", "claude")

    @pytest.mark.parametrize(
        "domain",
        ["fact_check", "completeness", "argument_integrity", "red_team"],
    )
    def test_reasoning_domains_are_untouched(self, domain):
        """Only voice_style. The other four ask about the draft, not the prose."""
        assert not _drafter_is_excluded("claude", domain, "claude")

    def test_other_models_still_review_voice(self):
        assert not _drafter_is_excluded("openai", "voice_style", "claude")

    def test_no_declaration_excludes_nothing(self):
        assert not _drafter_is_excluded("claude", "voice_style", None)


class TestAssignments:
    def test_drafter_loses_only_its_voice_pass(self):
        pairs = _build_assignments("maximum", ALL_ENABLED, ALL_KEYS, "claude")
        assert ("claude", "voice_style") not in pairs
        assert ("claude", "fact_check") in pairs
        assert ("claude", "red_team") in pairs

    def test_the_other_models_still_cover_voice_style(self):
        pairs = _build_assignments("maximum", ALL_ENABLED, ALL_KEYS, "claude")
        reviewers = {m for m, d in pairs if d == "voice_style"}
        assert reviewers == {"openai", "gemini", "mistral", "grok", "perplexity"}

    def test_undeclared_drafter_changes_nothing(self):
        assert _build_assignments(
            "maximum", ALL_ENABLED, ALL_KEYS
        ) == _build_assignments("maximum", ALL_ENABLED, ALL_KEYS, None)

    def test_explicit_prompts_cannot_reintroduce_the_drafter(self):
        """`prompts: [voice_style]` is an override of the preset, not of this."""
        configs = dict(ALL_ENABLED, claude={"prompts": ["voice_style", "red_team"]})
        pairs = _build_assignments("standard", configs, ALL_KEYS, "claude")
        assert ("claude", "voice_style") not in pairs
        assert ("claude", "red_team") in pairs

    def test_backfill_covers_voice_style_when_its_only_model_drafted(self, caplog):
        """At `standard` voice_style is one model; if it drafted, backfill covers it.

        The domain now has three layers of cover, and this test pins the first.
        Backfill assigns a reviewer at assignment time, before any call is made:
        every other configured model is eligible for voice_style and the preset
        asked for one model on it. If backfill cannot find a candidate the
        warning still fires (below), substitution repairs the domain after the
        fact when a provider is available, and the report names it as not
        reviewed when nothing did — see TestTheUnreviewedDomainReachesTheReport.
        An empty voice section that reads as "found nothing" is the failure all
        three exist to prevent.
        """
        with caplog.at_level(logging.WARNING):
            pairs = _build_assignments("standard", ALL_ENABLED, ALL_KEYS, "openai")

        reviewers = {m for m, d in pairs if d == "voice_style"}
        assert reviewers, "voice_style was left with no reviewer"
        assert "openai" not in reviewers, "the drafter must not review its own prose"
        assert "No model is reviewing" not in caplog.text

    def test_still_warns_when_nothing_can_cover_voice_style(self, caplog):
        """The warning is not obsolete — it is the case backfill cannot fix.

        With the drafter the only credentialled model there is no substitute to
        assign, so the domain really does go unreviewed and the run has to say
        so. Kept as its own test because the backfill above would otherwise hide
        the gap this warning exists for.
        """
        keys = {"openai": {"api_key": "k"}}
        with caplog.at_level(logging.WARNING):
            pairs = _build_assignments("standard", ALL_ENABLED, keys, "openai")

        assert not [p for p in pairs if p[1] == "voice_style"]
        assert "voice_style" in caplog.text
        assert "never ran" in caplog.text or "not because" in caplog.text

    def test_backfill_can_be_turned_off(self, caplog):
        """`backfill=False` restores the un-topped-up assignment exactly."""
        with caplog.at_level(logging.WARNING):
            pairs = _build_assignments(
                "standard", ALL_ENABLED, ALL_KEYS, "openai", backfill=False
            )
        assert not [p for p in pairs if p[1] == "voice_style"]
        assert "voice_style" in caplog.text

    def test_no_warning_when_another_model_covers_it(self, caplog):
        with caplog.at_level(logging.WARNING):
            _build_assignments("maximum", ALL_ENABLED, ALL_KEYS, "claude")
        assert "No model is reviewing" not in caplog.text


class TestHandoffParsing:
    def _handoff(self, extra_line=""):
        return parse_draft_submission(
            "DRAFT SUBMISSION HANDOFF\n"
            "Article: A Real Title Here\n"
            "Publication: mikehammett\n"
            f"{extra_line}"
            "PRIMARY CLAIM\nA claim.\n\n"
            "DRAFT\nBody text.\n"
        )

    def test_drafted_with_is_parsed(self):
        assert self._handoff("Drafted with: claude\n")["drafted_with"] == "claude"

    def test_absent_line_yields_empty_string(self):
        assert self._handoff()["drafted_with"] == ""

    def test_absence_does_not_disturb_the_rest_of_the_handoff(self):
        h = self._handoff()
        assert h["title"] == "A Real Title Here"
        assert h["draft"].strip() == "Body text."


class TestTheUnreviewedDomainReachesTheReport:
    """An empty section says whether anything reviewed it.

    The exclusion can leave a domain with no reviewer, and two passes repair
    that whenever another provider is configured and reachable: backfill at
    assignment time, and substitution after the calls. When neither can —
    `backfill_narrowed_domains: false`, `substitute_failed_domains: false`, a
    replay, or no other model able to take the domain — the section still has
    to say so. Measured 2026-09-05 before this was added: the never-ran and the
    clean render were byte-identical, both `_No flags._`.
    """

    STANDARD_DOMAINS = set(_THOROUGHNESS_PRESETS["standard"])

    def _results(self, drafter):
        """Results for a real `standard` run, keyed as consolidation keys them.

        Backfill is off here on purpose. With it on, every other configured
        model is eligible for `voice_style`, so the drafter exclusion no longer
        produces an unreviewed domain at all — which is the good outcome, and
        which would leave this class with nothing to test. The unrepairable
        state has to be constructed deliberately now, and that is exactly the
        state the class is about.
        """
        pairs = _build_assignments(
            "standard", ALL_ENABLED, ALL_KEYS, drafter, backfill=False
        )
        return {(m, d): {"failed": False, "data": {"flags": []}} for m, d in pairs}

    def test_the_excluded_domain_is_named_with_its_reason(self):
        not_run = _domains_never_attempted(
            self._results("openai"), self.STANDARD_DOMAINS, "openai"
        )
        assert set(not_run) == {"voice_style"}
        assert "openai drafted this article" in not_run["voice_style"]

    def test_a_domain_that_ran_is_not_named(self):
        assert (
            _domains_never_attempted(
                self._results("claude"), self.STANDARD_DOMAINS, "claude"
            )
            == {}
        )

    def test_a_failed_pass_is_not_reported_as_never_run(self):
        """Distinct problems, distinct notes.

        A failed pass leaves a result entry, so it is already named in *Failed
        model passes* and its section says which model it was built without.
        Claiming it also never ran would double-count one failure as two.
        """
        results = self._results("claude")
        results[("openai", "voice_style")] = {"failed": True, "error": "boom"}
        assert _domains_never_attempted(results, self.STANDARD_DOMAINS, "claude") == {}

    def test_a_blocked_domain_gets_the_generic_reason(self):
        """Nothing to do with the drafter — every model for it was unavailable."""
        not_run = _domains_never_attempted({}, {"red_team"}, None)
        assert "unavailable" in not_run["red_team"]

    def test_the_section_no_longer_reads_as_a_clean_draft(self):
        never_ran = {
            "section_3_voice": [],
            "model_failure_details": [],
            "domains_not_run": [
                {
                    "domain": "voice_style",
                    "section": "SECTION 3: Voice and AI-Speak",
                    "reason": "openai drafted this article and is excluded",
                }
            ],
        }
        clean = {
            "section_3_voice": [],
            "model_failure_details": [],
            "domains_not_run": [],
        }

        def render(rep):
            return report_markdown._render_flags_section(
                "SECTION 3: Voice and AI-Speak",
                rep["section_3_voice"],
                note=report_markdown._domain_notes(rep, "voice_style"),
            )

        assert render(never_ran) != render(clean)
        assert "Not reviewed this run" in "\n".join(render(never_ran))
        assert "not because the draft is clean" in "\n".join(render(never_ran))
        assert "Not reviewed" not in "\n".join(render(clean))

    def test_the_header_block_names_it_once_more(self):
        lines = report_markdown._render_domains_not_run(
            {
                "domains_not_run": [
                    {
                        "domain": "voice_style",
                        "section": "SECTION 3: Voice and AI-Speak",
                        "reason": "openai drafted this article and is excluded",
                    }
                ]
            }
        )
        assert "Domains not reviewed (1)" in lines[0]
        assert "voice_style" in "\n".join(lines)

    def test_the_header_block_is_absent_from_a_complete_run(self):
        assert report_markdown._render_domains_not_run({"domains_not_run": []}) == []
        assert report_markdown._render_domains_not_run({}) == []

    def test_consolidation_carries_it_into_the_report(self):
        report = build_report(
            article_title="Test",
            publication_name="pub",
            run_number=1,
            corrected_draft="draft",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=self._results("openai"),
            ensemble_cfg={},
            api_call_log=[],
            domains_not_run={"voice_style": "openai drafted this article"},
        )
        (detail,) = report["domains_not_run"]
        assert detail["domain"] == "voice_style"
        assert detail["section"] == "SECTION 3: Voice and AI-Speak"
        assert "openai drafted" in detail["reason"]

    def test_a_report_without_the_key_still_renders(self):
        """Reports written before this existed must not crash the renderer."""
        assert report_markdown._domain_notes({}, "voice_style") == []
