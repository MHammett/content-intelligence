"""Claims marked out of scope must leave verification without leaving the report.

The bug these guard against is not a crash, it is a *quiet* one. Measured
2026-09-05: `"I have a side job."` reached citation resolution, was checked
against a company team page describing a different person, came back
`not_addressed`, and every provider read that as grounds to withdraw a true
sentence. The same run reported three arithmetic claims as `confirmed` against
the source "Manual Calculation".

So there are two things to hold, and they pull against each other:

* An excluded claim must really be gone from the fact-check verdicts and from
  the Section 9 claim list — not filtered at one of the two, which would leave
  the sections telling different stories.
* An excluded claim must still be *visible*, with its reason, who decided it,
  and any verdict the exclusion overrode. A claim that silently vanishes is the
  failure mode in the other direction, and it is the harder one to notice.

Everything below is one of those two, or the boundary between them: red_team and
the other passes must not lose the passage at all.
"""

import logging

import pytest

from ci_article_review import consolidation, fact_check_scope, report_markdown
from ci_article_review.fact_check_scope import ScopeRules
from ci_article_review.handoff_parser import parse_draft_submission
from ci_article_review.pipeline import _collect_citation_claims


# ---------------------------------------------------------------------------
# Inline markers
# ---------------------------------------------------------------------------


class TestInlineMarkers:
    def test_a_marked_block_is_captured_with_its_reason(self):
        draft = (
            "Before.\n\n"
            "<!-- ci:no-verify: only I can confirm this -->\n"
            "I have a side job.\n"
            "<!-- /ci:no-verify -->\n\n"
            "After.\n"
        )
        found = fact_check_scope.parse_inline_markers(draft)
        assert len(found) == 1
        assert found[0]["passage"] == "I have a side job."
        assert found[0]["reason"] == "only I can confirm this"
        assert found[0]["origin"] == fact_check_scope.ORIGIN_INLINE

    def test_the_reason_is_optional(self):
        draft = "<!-- ci:no-verify -->I have a family.<!-- /ci:no-verify -->"
        (found,) = fact_check_scope.parse_inline_markers(draft)
        assert found["passage"] == "I have a family."
        assert found["reason"] == ""

    def test_several_blocks_are_all_captured(self):
        draft = (
            "<!-- ci:no-verify -->One.<!-- /ci:no-verify -->\n"
            "Middle stays in scope.\n"
            "<!-- ci:no-verify -->Two.<!-- /ci:no-verify -->"
        )
        found = fact_check_scope.parse_inline_markers(draft)
        assert [f["passage"] for f in found] == ["One.", "Two."]

    def test_markers_are_case_insensitive(self):
        draft = "<!-- CI:NO-VERIFY -->Mine.<!-- /CI:NO-VERIFY -->"
        assert fact_check_scope.parse_inline_markers(draft)

    def test_an_unclosed_marker_is_ignored_not_run_to_the_end(self, caplog):
        """Failing toward checking is the whole point.

        Excluding more than the author meant is invisible in the report;
        excluding nothing is obvious the moment the claim turns up
        fact-checked. So the ambiguous case must not swallow the rest of the
        draft.
        """
        draft = "<!-- ci:no-verify -->Everything after this point.\n\nMore text."
        with caplog.at_level(logging.WARNING):
            found = fact_check_scope.parse_inline_markers(draft)
        assert found == []
        assert "unclosed" in caplog.text.lower()

    def test_a_stray_closing_marker_is_ignored(self, caplog):
        with caplog.at_level(logging.WARNING):
            found = fact_check_scope.parse_inline_markers("Text.<!-- /ci:no-verify -->")
        assert found == []
        assert "no opening marker" in caplog.text

    def test_a_nested_marker_does_not_split_the_block(self, caplog):
        draft = (
            "<!-- ci:no-verify -->Outer "
            "<!-- ci:no-verify -->inner<!-- /ci:no-verify -->"
        )
        with caplog.at_level(logging.WARNING):
            found = fact_check_scope.parse_inline_markers(draft)
        assert len(found) == 1
        assert "inner" in found[0]["passage"]
        assert "nested" in caplog.text.lower()

    def test_an_empty_block_is_reported_rather_than_matching_everything(self, caplog):
        with caplog.at_level(logging.WARNING):
            found = fact_check_scope.parse_inline_markers(
                "<!-- ci:no-verify -->   <!-- /ci:no-verify -->"
            )
        assert found == []
        assert "empty" in caplog.text.lower()

    def test_markers_are_stripped_before_a_passage_is_matched(self):
        """The marker text itself must never become part of the passage.

        It is not prose, and leaving it in would let a claim match on the words
        "ci no verify" rather than on the sentence the author marked.
        """
        assert "no-verify" not in fact_check_scope.strip_markers(
            "<!-- ci:no-verify -->x<!-- /ci:no-verify -->"
        )


# ---------------------------------------------------------------------------
# Handoff section
# ---------------------------------------------------------------------------


class TestHandoffSection:
    def test_bullets_become_entries_with_reasons(self):
        found = fact_check_scope.parse_handoff_section(
            "- I have a side job — only I can confirm this\n"
            "- The 7,168-day figure is arithmetic from two dates in the piece\n"
        )
        assert [f["passage"] for f in found] == [
            "I have a side job",
            "The 7,168-day figure is arithmetic from two dates in the piece",
        ]
        assert found[0]["reason"] == "only I can confirm this"
        assert found[1]["reason"] == ""
        assert found[0]["origin"] == fact_check_scope.ORIGIN_HANDOFF

    @pytest.mark.parametrize("sep", ["—", "--", "|"])
    def test_the_ascii_reason_separators_work_too(self, sep):
        (found,) = fact_check_scope.parse_handoff_section(f"- A passage {sep} a reason")
        assert found["passage"] == "A passage"
        assert found["reason"] == "a reason"

    @pytest.mark.parametrize(
        "text", ["None identified by author.", "none", "N/A", "  None provided.  "]
    )
    def test_the_none_sentinels_yield_nothing(self, text):
        """Same convention UNCERTAIN SECTIONS and KNOWN GAPS already use."""
        assert fact_check_scope.parse_handoff_section(text) == []

    def test_the_templates_own_bracketed_guidance_is_not_an_exclusion(self):
        """An author who never filled the section in must exclude nothing.

        Reading the template's instructions as a passage would exclude whatever
        happened to overlap with them — a silent exclusion caused by leaving a
        field blank.
        """
        assert (
            fact_check_scope.parse_handoff_section(
                "[Passages no outside source can settle. If none, write "
                '"None identified by author."]'
            )
            == []
        )

    def test_prose_without_bullets_is_read_a_paragraph_at_a_time(self):
        found = fact_check_scope.parse_handoff_section(
            "I have a side job.\n\nThe closing paragraph is my own read."
        )
        assert len(found) == 2

    def test_an_empty_section_yields_nothing(self):
        assert fact_check_scope.parse_handoff_section("") == []
        assert fact_check_scope.parse_handoff_section(None) == []

    def test_the_parser_extracts_the_section_without_eating_its_neighbours(self):
        handoff = parse_draft_submission(
            "DRAFT SUBMISSION HANDOFF\n"
            "Article: T\n\n"
            "PRIMARY CLAIM\nA claim.\n\n"
            "UNCERTAIN SECTIONS\nSomething uncertain.\n\n"
            "OUT OF SCOPE FOR FACT-CHECK\n- I have a side job\n\n"
            "KNOWN GAPS\nA gap.\n\n"
            "DRAFT\nBody text.\n"
        )
        assert handoff["out_of_scope"] == "- I have a side job"
        assert handoff["uncertain_sections"] == "Something uncertain."
        assert handoff["known_gaps"] == "A gap."
        assert handoff["draft"] == "Body text."

    def test_a_handoff_without_the_section_still_parses(self):
        """Every handoff written before this existed has to keep working."""
        handoff = parse_draft_submission(
            "DRAFT SUBMISSION HANDOFF\nArticle: T\n\n"
            "PRIMARY CLAIM\nA claim.\n\nDRAFT\nBody.\n"
        )
        assert handoff["out_of_scope"] == ""
        assert ScopeRules.from_run("Body.", handoff, {}).exclusions == []


# ---------------------------------------------------------------------------
# Publication config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_exclude_passages_accepts_a_bare_string_or_a_mapping(self):
        rules = ScopeRules.from_run(
            "",
            {},
            {
                "fact_check_scope": {
                    "exclude_passages": [
                        "This site is a real expense",
                        {"passage": "I have a side job", "reason": "personal"},
                    ]
                }
            },
        )
        assert [e["passage"] for e in rules.exclusions] == [
            "This site is a real expense",
            "I have a side job",
        ]
        assert rules.exclusions[1]["reason"] == "personal"
        assert rules.exclusions[0]["origin"] == fact_check_scope.ORIGIN_CONFIG

    def test_the_default_honours_every_category_except_the_catch_all(self):
        """`other` is where an unjustified exclusion would arrive.

        A model with no fitting category reaches for the catch-all, so honouring
        it by default would hand a model the power to decline any claim.
        """
        rules = ScopeRules.from_run("", {}, {})
        assert rules.honours("first_person")
        assert rules.honours("internal_arithmetic")
        assert not rules.honours("other")

    def test_exclude_types_narrows_what_a_model_may_rule_out(self):
        rules = ScopeRules.from_run(
            "", {}, {"fact_check_scope": {"exclude_types": ["first_person"]}}
        )
        assert rules.honours("first_person")
        assert not rules.honours("future_prediction")

    def test_an_unknown_type_name_is_warned_about_rather_than_swallowed(self, caplog):
        """A typo here looks exactly like the feature being switched off."""
        with caplog.at_level(logging.WARNING):
            ScopeRules.from_run(
                "", {}, {"fact_check_scope": {"exclude_types": ["frist_person"]}}
            )
        assert "frist_person" in caplog.text

    def test_trust_model_classification_false_disables_the_model_path_only(self):
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->I have a side job.<!-- /ci:no-verify -->",
            {},
            {"fact_check_scope": {"trust_model_classification": False}},
        )
        assert not rules.honours("first_person")
        assert rules.author_exclusion_for("I have a side job.") is not None


# ---------------------------------------------------------------------------
# Matching a claim to a marked passage
# ---------------------------------------------------------------------------


class TestMatching:
    @pytest.fixture
    def rules(self):
        return ScopeRules.from_run(
            "<!-- ci:no-verify -->None of it is close to my top priority. I have a "
            "day job running network infrastructure. I have a side job. I have a "
            "family.<!-- /ci:no-verify -->",
            {},
            {},
        )

    def test_a_sentence_quoted_out_of_a_marked_paragraph_matches(self, rules):
        assert rules.author_exclusion_for("I have a side job.") is not None

    def test_a_paragraph_quoted_around_a_marked_sentence_matches(self):
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->I have a side job.<!-- /ci:no-verify -->", {}, {}
        )
        assert (
            rules.author_exclusion_for(
                "I have a day job running network infrastructure. I have a side "
                "job. I have a family."
            )
            is not None
        )

    def test_html_entities_in_a_model_quote_still_match_the_draft(self, rules):
        """One provider returns `&#39;` where the draft has an apostrophe.

        Without unescaping, a model's own quote of the draft fails to match the
        draft — and the failure is a claim silently escaping its exclusion.
        """
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->This site is a real expense, and there's no way "
            "to recoup it.<!-- /ci:no-verify -->",
            {},
            {},
        )
        assert (
            rules.author_exclusion_for(
                "This site is a real expense, and there&#39;s no way to recoup it."
            )
            is not None
        )

    def test_an_unrelated_claim_does_not_match(self, rules):
        assert (
            rules.author_exclusion_for(
                "Fourteen pieces are sitting in the pipeline right now."
            )
            is None
        )

    def test_a_very_short_marking_does_not_match_by_containment(self):
        """A three-character marking would otherwise match half the draft."""
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->I am<!-- /ci:no-verify -->", {}, {}
        )
        assert rules.author_exclusion_for("I am not sure the figure is right") is None

    def test_an_empty_claim_matches_nothing(self, rules):
        assert rules.author_exclusion_for("") is None
        assert rules.author_exclusion_for(None) is None

    def test_a_paraphrase_matches_on_token_overlap(self):
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->Fourteen pieces are sitting in the pipeline "
            "right now.<!-- /ci:no-verify -->",
            {},
            {},
        )
        assert (
            rules.author_exclusion_for(
                "Fourteen pieces are sitting in the pipeline right now"
            )
            is not None
        )


# ---------------------------------------------------------------------------
# apply() — moving claims out of the verdict buckets
# ---------------------------------------------------------------------------


def _fc(**buckets):
    base = {
        "confirmed": [],
        "outdated": [],
        "contradicted": [],
        "unverifiable": [],
        "primary_source_needed": [],
        "out_of_scope": [],
        "additional_observations": [],
    }
    base.update(buckets)
    return base


class TestAuthorMarkingsSweepEveryBucket:
    @pytest.fixture
    def rules(self):
        return ScopeRules.from_run(
            "<!-- ci:no-verify: personal -->I have a side job.<!-- /ci:no-verify -->",
            {},
            {},
        )

    @pytest.mark.parametrize(
        "bucket",
        [
            "confirmed",
            "outdated",
            "contradicted",
            "unverifiable",
            "primary_source_needed",
        ],
    )
    def test_every_verdict_bucket_is_swept(self, rules, bucket):
        """`unverifiable` matters as much as `confirmed`.

        A first-person claim filed as `unverifiable` still reached citation
        resolution and still came back "the source does not support this".
        """
        out = rules.apply(_fc(**{bucket: [{"claim": "I have a side job."}]}))
        assert out[bucket] == []
        assert len(out["out_of_scope"]) == 1
        assert out["out_of_scope"][0]["excluded"] is True

    def test_the_reason_and_origin_reach_the_entry(self, rules):
        out = rules.apply(_fc(confirmed=[{"claim": "I have a side job."}]))
        entry = out["out_of_scope"][0]
        assert entry["reason"] == "personal"
        assert entry["excluded_by"] == fact_check_scope.ORIGIN_INLINE

    def test_an_overridden_verdict_is_recorded_not_discarded(self, rules):
        """The verdict is the reader's to judge, so it has to survive.

        This is the measured case: openai reported `confirmed` for "I have a
        side job." citing a team page about a different person.
        """
        out = rules.apply(
            _fc(
                confirmed=[
                    {
                        "claim": "I have a side job.",
                        "source_model": "openai",
                        "source": "FD-IX, Meet the Team",
                        "source_url": "https://fd-ix.com/about/team/",
                    }
                ]
            )
        )
        (withdrawn,) = out["out_of_scope"][0]["withdrawn_verdicts"]
        assert withdrawn["model"] == "openai"
        assert withdrawn["bucket"] == "confirmed"
        assert withdrawn["source_url"] == "https://fd-ix.com/about/team/"

    def test_several_models_on_one_claim_collapse_to_one_entry(self, rules):
        out = rules.apply(
            _fc(
                confirmed=[{"claim": "I have a side job.", "source_model": "openai"}],
                unverifiable=[
                    {"claim": "I have a side job.", "source_model": "perplexity"}
                ],
            )
        )
        assert len(out["out_of_scope"]) == 1
        assert [w["model"] for w in out["out_of_scope"][0]["withdrawn_verdicts"]] == [
            "openai",
            "perplexity",
        ]

    def test_claims_the_author_did_not_mark_are_untouched(self, rules):
        out = rules.apply(_fc(confirmed=[{"claim": "The 2019 figure was 33 percent."}]))
        assert len(out["confirmed"]) == 1
        assert out["out_of_scope"] == []

    def test_a_rules_object_with_nothing_marked_changes_nothing(self):
        rules = ScopeRules.from_run("", {}, {})
        fc = _fc(confirmed=[{"claim": "The 2019 figure was 33 percent."}])
        assert rules.apply(fc)["confirmed"] == fc["confirmed"]


class TestModelClassification:
    def test_an_honoured_category_is_excluded(self):
        rules = ScopeRules.from_run("", {}, {})
        out = rules.apply(
            _fc(
                out_of_scope=[
                    {
                        "claim": "I have a side job.",
                        "claim_type": "first_person",
                        "reason": "only the author can confirm it",
                        "source_model": "gemini",
                    }
                ]
            )
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is True
        assert entry["excluded_by"] == fact_check_scope.ORIGIN_MODEL
        assert entry["classified_by"] == ["gemini"]

    def test_the_catch_all_category_is_reported_but_still_verified(self):
        """`other` is reported, not obeyed — and the entry says why."""
        rules = ScopeRules.from_run("", {}, {})
        out = rules.apply(
            _fc(
                out_of_scope=[
                    {
                        "claim": "Citations get checked against primary sources.",
                        "claim_type": "other",
                        "reason": "arguably checkable once the repo is public",
                        "source_model": "gemini",
                    }
                ]
            )
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is False
        assert "does not exclude on a model's say-so" in entry["reason"]

    def test_trust_model_classification_false_reports_without_excluding(self):
        rules = ScopeRules.from_run(
            "", {}, {"fact_check_scope": {"trust_model_classification": False}}
        )
        out = rules.apply(
            _fc(
                out_of_scope=[
                    {
                        "claim": "I have a side job.",
                        "claim_type": "first_person",
                        "reason": "r",
                        "source_model": "gemini",
                    }
                ]
            )
        )
        assert out["out_of_scope"][0]["excluded"] is False

    def test_an_honoured_classification_withdraws_another_models_verdict(self):
        """Models disagree about the same sentence, and it must read as one finding.

        Leaving perplexity's `confirmed` in place while holding the claim out of
        Section 9 would print the same claim twice with nothing connecting the
        two — the silent disagreement this feature exists to prevent.
        """
        rules = ScopeRules.from_run("", {}, {})
        out = rules.apply(
            _fc(
                confirmed=[
                    {
                        "claim": "Accuracy is the entire premise of this project.",
                        "source_model": "perplexity",
                        "source_url": "https://example.com/post",
                    }
                ],
                out_of_scope=[
                    {
                        "claim": "Accuracy is the entire premise of this project.",
                        "claim_type": "subjective_judgment",
                        "reason": "an editorial stance",
                        "source_model": "gemini",
                    }
                ],
            )
        )
        assert out["confirmed"] == []
        entry = out["out_of_scope"][0]
        assert entry["classified_by"] == ["gemini"]
        assert entry["withdrawn_verdicts"][0]["model"] == "perplexity"
        assert (
            entry["withdrawn_verdicts"][0]["source_url"] == "https://example.com/post"
        )

    def test_a_classification_that_is_not_honoured_leaves_verdicts_alone(self):
        rules = ScopeRules.from_run("", {}, {})
        out = rules.apply(
            _fc(
                confirmed=[
                    {"claim": "A checkable claim.", "source_model": "perplexity"}
                ],
                out_of_scope=[
                    {
                        "claim": "A checkable claim.",
                        "claim_type": "other",
                        "reason": "r",
                        "source_model": "gemini",
                    }
                ],
            )
        )
        assert len(out["confirmed"]) == 1

    def test_an_author_marking_outranks_a_model_classification(self):
        """The author's reason and origin win, whatever category a model chose."""
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify: my own life -->I have a side job.<!-- /ci:no-verify -->",
            {},
            {"fact_check_scope": {"trust_model_classification": False}},
        )
        out = rules.apply(
            _fc(
                out_of_scope=[
                    {
                        "claim": "I have a side job.",
                        "claim_type": "other",
                        "reason": "model's reason",
                        "source_model": "gemini",
                    }
                ]
            )
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is True
        assert entry["excluded_by"] == fact_check_scope.ORIGIN_INLINE
        assert entry["reason"] == "my own life"

    def test_a_classification_with_no_claim_text_is_skipped(self):
        rules = ScopeRules.from_run("", {}, {})
        out = rules.apply(
            _fc(
                out_of_scope=[
                    {"claim": "", "claim_type": "first_person", "reason": "r"}
                ]
            )
        )
        assert out["out_of_scope"] == []


def _verdict(claim, model, url, bucket="confirmed"):
    return {
        "claim": claim,
        "source_model": model,
        "source": f"{model} source",
        "source_url": url,
        "bucket": bucket,
    }


def _scope_call(claim, model, claim_type="first_person", reason="r"):
    return {
        "claim": claim,
        "claim_type": claim_type,
        "reason": reason,
        "source_model": model,
    }


def _by_bucket(*verdicts):
    """`_fc` buckets from verdict dicts, each filed under its own `bucket`."""
    buckets = {}
    for verdict in verdicts:
        verdict = dict(verdict)
        buckets.setdefault(verdict.pop("bucket"), []).append(verdict)
    return buckets


class TestSourcedVerdictsOutvoteALoneScopeCall:
    """One model's scope label must not erase other models' sourced verdicts (#336).

    Option 2 of the issue, K=2: two or more distinct sourced verdicts outvote a
    scope call made by a single model. One does not, because the case PR #167
    was written for is a single false `confirmed` that carried a real URL.
    """

    CLAIM = "Accuracy is the entire premise of this project."

    def _apply(self, verdicts, calls, pub_config=None):
        rules = ScopeRules.from_run("", {}, pub_config or {})
        return rules.apply(_fc(**_by_bucket(*verdicts), out_of_scope=list(calls)))

    @pytest.mark.parametrize(
        "claim_type", sorted(fact_check_scope.DEFAULT_HONOURED_TYPES)
    )
    def test_two_sourced_verdicts_keep_the_claim_in_verification(self, claim_type):
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
                _verdict(self.CLAIM, "mistral", "https://example.org/b"),
            ],
            [_scope_call(self.CLAIM, "gemini", claim_type)],
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is False
        assert entry["withdrawn_verdicts"] == []
        assert [v["source_model"] for v in out["confirmed"]] == [
            "perplexity",
            "mistral",
        ]
        # Still reported, so the disagreement is visible and says why.
        assert entry["classified_by"] == ["gemini"]
        assert entry["claim_type"] == claim_type
        assert "outvote" in entry["reason"]
        assert "https://example.com/a" in entry["reason"]
        assert "https://example.org/b" in entry["reason"]
        assert "does not exclude on a model's say-so" not in entry["reason"]

    def test_the_verdicts_may_sit_in_different_buckets(self):
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
                _verdict(
                    self.CLAIM,
                    "mistral",
                    "https://example.org/b",
                    bucket="contradicted",
                ),
            ],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
        )
        assert out["out_of_scope"][0]["excluded"] is False
        assert len(out["confirmed"]) == 1 and len(out["contradicted"]) == 1

    def test_one_sourced_verdict_does_not_outvote_the_case_pr_167_was_for(self):
        """A false `confirmed` with a real URL, for a claim about the author's life."""
        claim = "I have a side job."
        out = self._apply(
            [_verdict(claim, "openai", "https://fd-ix.com/about/team/")],
            [_scope_call(claim, "gemini", "first_person")],
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is True
        assert out["confirmed"] == []
        assert entry["withdrawn_verdicts"][0]["source_url"].startswith("https://fd-ix")

    def test_two_models_citing_one_page_count_once(self):
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", "https://example.com/a#intro"),
                _verdict(self.CLAIM, "mistral", "HTTPS://Example.com/a/"),
            ],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
        )
        assert out["out_of_scope"][0]["excluded"] is True

    def test_one_model_citing_two_pages_counts_once(self):
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
                _verdict(
                    self.CLAIM,
                    "perplexity",
                    "https://example.org/b",
                    bucket="outdated",
                ),
            ],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
        )
        assert out["out_of_scope"][0]["excluded"] is True

    def test_a_verdict_with_no_real_url_is_not_a_sourced_verdict(self):
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", ""),
                _verdict(self.CLAIM, "mistral", "Manual Calculation"),
                _verdict(
                    self.CLAIM,
                    "openai",
                    "https://example.com/a",
                    bucket="primary_source_needed",
                ),
                _verdict(
                    self.CLAIM, "claude", "https://example.org/b", bucket="unverifiable"
                ),
            ],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
        )
        assert out["out_of_scope"][0]["excluded"] is True

    def test_two_models_making_the_scope_call_is_not_a_lone_call(self):
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
                _verdict(self.CLAIM, "mistral", "https://example.org/b"),
            ],
            [
                _scope_call(self.CLAIM, "gemini", "subjective_judgment"),
                _scope_call(self.CLAIM, "openai", "subjective_judgment"),
            ],
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is True
        assert entry["classified_by"] == ["gemini", "openai"]
        assert {w["model"] for w in entry["withdrawn_verdicts"]} == {
            "perplexity",
            "mistral",
        }

    def test_a_call_that_is_not_honoured_does_not_count_towards_the_quorum(self):
        """`other` is reported, not obeyed; it cannot help a lone honoured call."""
        out = self._apply(
            [
                _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
                _verdict(self.CLAIM, "mistral", "https://example.org/b"),
            ],
            [
                _scope_call(self.CLAIM, "gemini", "subjective_judgment"),
                _scope_call(self.CLAIM, "openai", "other"),
            ],
        )
        assert out["out_of_scope"][0]["excluded"] is False

    def test_an_author_marking_is_never_outvoted(self):
        rules = ScopeRules.from_run(
            f"<!-- ci:no-verify: my own view -->{self.CLAIM}<!-- /ci:no-verify -->",
            {},
            {},
        )
        out = rules.apply(
            _fc(
                confirmed=[
                    _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
                    _verdict(self.CLAIM, "mistral", "https://example.org/b"),
                    _verdict(self.CLAIM, "openai", "https://example.net/c"),
                ],
                out_of_scope=[_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
            )
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is True
        assert entry["excluded_by"] == fact_check_scope.ORIGIN_INLINE
        assert out["confirmed"] == []

    def test_the_threshold_is_configurable(self):
        verdicts = [
            _verdict(self.CLAIM, "perplexity", "https://example.com/a"),
            _verdict(self.CLAIM, "mistral", "https://example.org/b"),
        ]
        call = [_scope_call(self.CLAIM, "gemini", "subjective_judgment")]
        strict = self._apply(
            verdicts, call, {"fact_check_scope": {"min_sources_to_overrule": 3}}
        )
        assert strict["out_of_scope"][0]["excluded"] is True
        loose = self._apply(
            verdicts[:1], call, {"fact_check_scope": {"min_sources_to_overrule": 1}}
        )
        assert loose["out_of_scope"][0]["excluded"] is False

    @pytest.mark.parametrize("bad", [0, -1, True, "two", 2.5])
    def test_an_unusable_threshold_is_warned_about_and_defaults(self, bad, caplog):
        with caplog.at_level(logging.WARNING):
            rules = ScopeRules.from_run(
                "", {}, {"fact_check_scope": {"min_sources_to_overrule": bad}}
            )
        assert rules.min_sources_to_overrule == (
            fact_check_scope.DEFAULT_MIN_SOURCES_TO_OVERRULE
        )
        assert "min_sources_to_overrule" in caplog.text

    def test_trust_model_classification_false_still_excludes_nothing(self):
        out = self._apply(
            [_verdict(self.CLAIM, "perplexity", "https://example.com/a")],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
            {"fact_check_scope": {"trust_model_classification": False}},
        )
        entry = out["out_of_scope"][0]
        assert entry["excluded"] is False
        assert len(out["confirmed"]) == 1
        assert "does not exclude on a model's say-so" in entry["reason"]

    def test_exclude_types_still_bounds_what_a_lone_call_can_do(self):
        pub = {"fact_check_scope": {"exclude_types": ["first_person"]}}
        out = self._apply(
            [_verdict(self.CLAIM, "perplexity", "https://example.com/a")],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
            pub,
        )
        assert out["out_of_scope"][0]["excluded"] is False
        narrow = self._apply(
            [_verdict("I have a side job.", "openai", "https://example.com/a")],
            [_scope_call("I have a side job.", "gemini", "first_person")],
            pub,
        )
        assert narrow["out_of_scope"][0]["excluded"] is True

    def test_another_claims_verdicts_are_not_counted(self):
        out = self._apply(
            [
                _verdict(
                    "A different claim entirely.", "perplexity", "https://e.com/a"
                ),
                _verdict(
                    "Another unrelated sentence here.", "mistral", "https://e.org/b"
                ),
            ],
            [_scope_call(self.CLAIM, "gemini", "subjective_judgment")],
        )
        assert out["out_of_scope"][0]["excluded"] is True


class TestTheReasonFollowsTheDecision:
    """An excluded claim must not carry text saying it stayed in verification (#337)."""

    CLAIM = "Citations get checked against primary sources."

    def _entry(self, *calls):
        rules = ScopeRules.from_run("", {}, {})
        return rules.apply(_fc(out_of_scope=list(calls)))["out_of_scope"][0]

    def test_a_later_honoured_call_replaces_the_stayed_in_verification_text(self):
        entry = self._entry(
            _scope_call(self.CLAIM, "openai", "other", "arguably checkable"),
            _scope_call(self.CLAIM, "gemini", "future_prediction", "no source yet"),
        )
        assert entry["excluded"] is True
        assert "stayed in verification" not in entry["reason"]
        assert entry["reason"] == "no source yet"
        # The category shown is the one that decided it, not the first writer's.
        assert entry["claim_type"] == "future_prediction"
        assert entry["classified_by"] == ["openai", "gemini"]

    def test_an_honoured_call_arriving_first_keeps_its_own_reason(self):
        entry = self._entry(
            _scope_call(self.CLAIM, "gemini", "future_prediction", "no source yet"),
            _scope_call(self.CLAIM, "openai", "other", "arguably checkable"),
        )
        assert entry["excluded"] is True
        assert entry["reason"] == "no source yet"
        assert entry["claim_type"] == "future_prediction"

    def test_a_claim_nobody_honoured_still_says_it_stayed_in_verification(self):
        entry = self._entry(_scope_call(self.CLAIM, "openai", "other", "arguable"))
        assert entry["excluded"] is False
        assert entry["reason"].startswith("arguable")
        assert "stayed in verification" in entry["reason"]

    def test_an_author_marking_arriving_over_an_unhonoured_call(self):
        rules = ScopeRules.from_run(
            f"<!-- ci:no-verify -->{self.CLAIM}<!-- /ci:no-verify -->", {}, {}
        )
        entry = rules.apply(
            _fc(out_of_scope=[_scope_call(self.CLAIM, "openai", "other", "arguable")])
        )["out_of_scope"][0]
        assert entry["excluded"] is True
        assert "stayed in verification" not in entry["reason"]

    def test_an_excluded_entry_without_a_reason_gets_none_invented(self):
        entry = self._entry(
            _scope_call(self.CLAIM, "openai", "other", "arguable"),
            _scope_call(self.CLAIM, "gemini", "future_prediction", ""),
        )
        assert entry["excluded"] is True
        assert entry["reason"] == ""


class TestConsolidationWiring:
    """`build_report` has to hand the rules down, or none of the above runs."""

    def _results(self, data, model="gemini"):
        return {(model, "fact_check"): {"failed": False, "data": data}}

    def test_a_single_model_run_applies_the_rules(self):
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->I have a side job.<!-- /ci:no-verify -->", {}, {}
        )
        section = consolidation._build_fact_check(
            self._results(_fc(confirmed=[{"claim": "I have a side job."}])), {}, rules
        )
        assert section["confirmed"] == []
        assert len(section["out_of_scope"]) == 1

    def test_a_single_model_run_tags_its_scope_calls_with_the_model(self):
        section = consolidation._build_fact_check(
            self._results(
                _fc(
                    out_of_scope=[
                        {
                            "claim": "I have a side job.",
                            "claim_type": "first_person",
                            "reason": "r",
                        }
                    ]
                )
            ),
            {},
            ScopeRules.from_run("", {}, {}),
        )
        assert section["out_of_scope"][0]["classified_by"] == ["gemini"]

    def test_a_multi_model_run_applies_the_rules(self):
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->I have a side job.<!-- /ci:no-verify -->", {}, {}
        )
        results = {
            ("gemini", "fact_check"): {
                "failed": False,
                "data": _fc(confirmed=[{"claim": "I have a side job."}]),
            },
            ("openai", "fact_check"): {
                "failed": False,
                "data": _fc(unverifiable=[{"claim": "I have a side job."}]),
            },
        }
        section = consolidation._build_fact_check(results, {}, rules)
        assert section["confirmed"] == []
        assert section["unverifiable"] == []
        assert len(section["out_of_scope"]) == 1
        assert {
            w["model"] for w in section["out_of_scope"][0]["withdrawn_verdicts"]
        } == {"gemini", "openai"}

    def test_no_rules_leaves_the_section_alone_but_still_declares_the_bucket(self):
        """An older capture replayed must not crash the renderer on a missing key.

        Calls ``_build_fact_check`` directly, so the evidence rules
        (``_demote_unevidenced_verdicts``, applied by ``build_report`` before
        this section is built) do not run and the verdict stays where it is.
        """
        section = consolidation._build_fact_check(
            self._results(
                {
                    "confirmed": [
                        {
                            "claim": "A claim.",
                            "source": "Some Agency, Annual Report",
                            "source_url": "https://example.gov/report",
                        }
                    ]
                }
            ),
            {},
            None,
        )
        assert [c["claim"] for c in section["confirmed"]] == ["A claim."]
        assert section["out_of_scope"] == []

    def test_other_domains_are_untouched(self):
        """red_team and argument_integrity keep every finding.

        A first-person claim can still be an argumentative weakness. Hiding it
        from every reviewer to spare it from one would cost real findings, so
        the scope rules must not reach these sections at all.
        """
        rules = ScopeRules.from_run(
            "<!-- ci:no-verify -->I have a side job.<!-- /ci:no-verify -->", {}, {}
        )
        results = {
            ("gemini", "red_team"): {
                "failed": False,
                "data": {
                    "most_vulnerable_claim": {
                        "passage": "I have a side job.",
                        "attack_vector": "unverifiable to a reader",
                    }
                },
            },
            ("gemini", "argument_integrity"): {
                "failed": False,
                "data": {
                    "flags": [{"passage": "I have a side job.", "logical_problem": "x"}]
                },
            },
        }
        report = consolidation.build_report(
            article_title="T",
            publication_name="p",
            run_number=1,
            corrected_draft="I have a side job.",
            lt_result=None,
            results=results,
            ensemble_cfg={},
            api_call_log=[],
            fact_check_scope=rules,
        )
        assert report["section_6_red_team"]["most_vulnerable_claim"]["passage"]
        assert len(report["section_4_argument"]) == 1


# ---------------------------------------------------------------------------
# Citation resolution
# ---------------------------------------------------------------------------


class TestCitationClaims:
    def test_an_excluded_claim_never_enters_the_claim_list(self):
        claims = _collect_citation_claims(
            _fc(
                out_of_scope=[
                    {
                        "claim": "I have a side job.",
                        "excluded": True,
                        "excluded_by": "x",
                    }
                ]
            ),
            "I have a side job.",
        )
        assert claims == []

    def test_another_models_verdict_cannot_reintroduce_an_excluded_claim(self):
        """The dedup set is seeded with the exclusions before the buckets run.

        Without that, a claim excluded because gemini classified it would come
        straight back in through perplexity's `confirmed` entry, and Section 9
        would resolve it after all.
        """
        claims = _collect_citation_claims(
            _fc(
                confirmed=[
                    {"claim": "I have a side job.", "source_model": "perplexity"}
                ],
                out_of_scope=[
                    {
                        "claim": "I have a side job.",
                        "excluded": True,
                        "excluded_by": "x",
                    }
                ],
            ),
            "I have a side job.",
        )
        assert claims == []

    def test_a_reported_but_not_excluded_claim_is_still_resolved(self):
        """`other` is reported, not obeyed — so it must still be checked."""
        claims = _collect_citation_claims(
            _fc(
                out_of_scope=[
                    {
                        "claim": "Citations get checked against primary sources.",
                        "excluded": False,
                        "excluded_by": "x",
                    }
                ]
            ),
            "Citations get checked against primary sources.",
        )
        assert [c["claim"] for c in claims] == [
            "Citations get checked against primary sources."
        ]
        assert claims[0]["fact_check_bucket"] == "out_of_scope"

    def test_claims_outside_the_exclusions_are_unaffected(self):
        claims = _collect_citation_claims(
            _fc(
                confirmed=[{"claim": "The 2019 figure was 33 percent.", "source": ""}],
                out_of_scope=[
                    {
                        "claim": "I have a side job.",
                        "excluded": True,
                        "excluded_by": "x",
                    }
                ],
            ),
            "draft text",
        )
        assert [c["claim"] for c in claims] == ["The 2019 figure was 33 percent."]

    def test_a_section_with_no_bucket_at_all_still_collects(self):
        """A report from before this existed replays without a KeyError."""
        claims = _collect_citation_claims(
            {"confirmed": [{"claim": "A claim.", "source": ""}]}, "draft"
        )
        assert len(claims) == 1


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


class TestPromptBlock:
    def test_nothing_marked_renders_nothing(self):
        assert ScopeRules.from_run("", {}, {}).prompt_block() == ""

    def test_a_marked_passage_is_quoted_with_its_reason(self):
        block = ScopeRules.from_run(
            "<!-- ci:no-verify: personal -->I have a side job.<!-- /ci:no-verify -->",
            {},
            {},
        ).prompt_block()
        assert "I have a side job." in block
        assert "personal" in block
        assert "out_of_scope" in block

    def test_a_long_passage_is_truncated_rather_than_duplicating_the_draft(self):
        block = ScopeRules.from_run(
            "<!-- ci:no-verify -->" + ("word " * 200) + "<!-- /ci:no-verify -->",
            {},
            {},
        ).prompt_block()
        assert len(block) < 600

    def test_the_marker_syntax_never_leaks_into_the_prompt(self):
        block = ScopeRules.from_run(
            "<!-- ci:no-verify -->A marked passage here.<!-- /ci:no-verify -->", {}, {}
        ).prompt_block()
        assert "<!--" not in block

    def test_the_fact_check_prompt_has_the_placeholder_and_the_others_do_not(self):
        """A placeholder in another domain's prompt would render as a stray blank.

        `_render_prompt` substitutes by name across whatever template it is
        given, so the variable only belongs where the bucket does.
        """
        from ci_article_review.pipeline import _DOMAIN_PROMPTS, _load_prompt

        assert "{out_of_scope_passages}" in _load_prompt(_DOMAIN_PROMPTS["fact_check"])
        for domain, name in _DOMAIN_PROMPTS.items():
            if domain != "fact_check":
                assert "{out_of_scope_passages}" not in _load_prompt(name)

    def test_the_prompt_names_every_category_the_schema_allows(self):
        """A category the schema permits but the prompt never explains is unusable."""
        from ci_article_review.pipeline import _DOMAIN_PROMPTS, _load_prompt

        text = _load_prompt(_DOMAIN_PROMPTS["fact_check"])
        for claim_type in fact_check_scope.CLAIM_TYPES:
            assert claim_type in text, f"{claim_type} is not explained in the prompt"

    def test_the_rendered_prompt_carries_the_block(self):
        from ci_article_review.pipeline import (
            _DOMAIN_PROMPTS,
            _load_prompt,
            _render_prompt,
        )

        rendered = _render_prompt(
            _load_prompt(_DOMAIN_PROMPTS["fact_check"]),
            out_of_scope_passages=ScopeRules.from_run(
                "<!-- ci:no-verify -->I have a side job.<!-- /ci:no-verify -->", {}, {}
            ).prompt_block(),
        )
        assert "I have a side job." in rendered
        assert "{out_of_scope_passages}" not in rendered


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestRendering:
    def _entry(self, **kw):
        entry = {
            "claim": "I have a side job.",
            "excluded": True,
            "excluded_by": fact_check_scope.ORIGIN_INLINE,
            "claim_type": "",
            "reason": "personal",
            "classified_by": [],
            "withdrawn_verdicts": [],
        }
        entry.update(kw)
        return entry

    def test_an_excluded_claim_is_visible_with_its_reason_and_decider(self):
        out = "\n".join(report_markdown._render_out_of_scope([self._entry()]))
        assert "I have a side job." in out
        assert "personal" in out
        assert fact_check_scope.ORIGIN_INLINE in out
        assert "Status: excluded" in out

    def test_a_withdrawn_verdict_is_printed_with_its_source(self):
        out = "\n".join(
            report_markdown._render_out_of_scope(
                [
                    self._entry(
                        withdrawn_verdicts=[
                            {
                                "model": "openai",
                                "bucket": "confirmed",
                                "source": "FD-IX",
                                "source_url": "https://fd-ix.com/about/team/",
                            }
                        ]
                    )
                ]
            )
        )
        assert "openai" in out
        assert "confirmed" in out
        assert "https://fd-ix.com/about/team/" in out

    def test_a_reported_but_not_excluded_claim_says_it_was_still_checked(self):
        out = "\n".join(
            report_markdown._render_out_of_scope(
                [self._entry(excluded=False, claim_type="other")]
            )
        )
        assert "still checked" in out
        assert "1 more was classified" in out

    def test_the_not_excluded_note_allows_for_an_outvote(self):
        """A first_person call outvoted by sources was not held back by its category."""
        out = "\n".join(
            report_markdown._render_out_of_scope(
                [
                    self._entry(
                        excluded=False,
                        excluded_by=fact_check_scope.ORIGIN_MODEL,
                        claim_type="first_person",
                        reason="r [two sourced verdicts outvote a single model's call]",
                    )
                ]
            )
        )
        assert "1 more was classified" in out
        assert "outvoted the call" in out
        assert "- Reason: r [two sourced verdicts outvote" in out

    def test_the_category_is_explained_not_just_named(self):
        out = "\n".join(
            report_markdown._render_out_of_scope(
                [
                    self._entry(
                        claim_type="internal_arithmetic", classified_by=["gemini"]
                    )
                ]
            )
        )
        assert "internal_arithmetic" in out
        assert "figures already in the draft" in out
        assert "Classified out of scope by: gemini" in out

    def test_every_category_has_a_reader_facing_phrase(self):
        """A category with no phrase would render as a bare enum name."""
        for claim_type in fact_check_scope.CLAIM_TYPES:
            assert claim_type in report_markdown._CLAIM_TYPE_PHRASES

    def test_an_empty_bucket_renders_nothing(self):
        assert report_markdown._render_out_of_scope([]) == []

    def test_section_2_includes_the_bucket(self):
        out = "\n".join(
            report_markdown._render_section_2(_fc(out_of_scope=[self._entry()]))
        )
        assert "Out of scope for verification" in out

    def test_section_9_says_how_many_claims_never_entered_resolution(self):
        """Its total is smaller than the fact-check pass raised, and silently so.

        A reader who works that out unaided reads it as claims going missing.
        """
        out = "\n".join(report_markdown._render_section_9([], [self._entry()]))
        assert "1 claim(s) never entered resolution" in out
        assert "Section 2" in out

    def test_section_9_says_nothing_when_nothing_was_held_out(self):
        out = "\n".join(report_markdown._render_section_9([], []))
        assert "never entered resolution" not in out

    def test_a_reported_but_not_excluded_claim_is_not_counted_as_held_out(self):
        out = "\n".join(
            report_markdown._render_section_9([], [self._entry(excluded=False)])
        )
        assert "never entered resolution" not in out

    def test_the_full_report_renders_with_the_bucket_present(self):
        out = report_markdown.render_report_markdown(
            {
                "article_title": "T",
                "section_2_fact_check": _fc(out_of_scope=[self._entry()]),
                "section_9_citations": [],
            }
        )
        assert "Out of scope for verification" in out
        assert "never entered resolution" in out
