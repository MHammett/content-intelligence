"""Unit tests for the consolidation module.

The consolidation API changed in the ensemble refactor:
  - _find_consensus(results, lt_passages, ensemble_cfg)  — results is {(model, domain): result}
  - build_report(..., results=..., ensemble_cfg=..., ...)  — no more named model args
"""

from ci_article_review.consolidation import (
    _find_consensus,
    _passage_key,
    build_report,
    rerun_recommended,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(data, model="test-model"):
    return {
        "failed": False,
        "data": data,
        "model": model,
        "tokens": {"prompt": 10, "completion": 5},
    }


def _failed(model="unknown"):
    return {"failed": True, "error": "test error", "model": model, "tokens": {}}


def _flag(passage, **extra):
    return {"passage": passage, "problem": "test issue", **extra}


# ---------------------------------------------------------------------------
# Passage key
# ---------------------------------------------------------------------------


class TestPassageKey:
    def test_normalizes_whitespace(self):
        assert _passage_key("hello   world") == _passage_key("hello world")

    def test_lowercased(self):
        assert _passage_key("Hello World") == _passage_key("hello world")

    def test_not_truncated(self):
        """The old 250-character cap merged any two passages sharing a prefix.

        Three of the fifteen consensus passages in the 2026-09-03 run were
        longer than 250 characters, so this was live, not theoretical: two
        distinct findings on one long paragraph collapsed into a single key.
        """
        long = "a" * 300
        assert len(_passage_key(long)) == 300

    def test_two_passages_sharing_a_long_prefix_stay_distinct(self):
        shared = "the pipeline checks every claim against a primary source " * 5
        assert _passage_key(shared + "and then stops.") != _passage_key(
            shared + "and then keeps going."
        )


# ---------------------------------------------------------------------------
# _find_consensus — new API: results keyed by (model, domain)
# ---------------------------------------------------------------------------


class TestFindConsensus:
    """Tests for weighted consensus detection.

    Default weights used below (from consolidation._DEFAULT_WEIGHTS):
      openai:voice_style        = 1.2
      mistral:argument_integrity = 1.2
      claude:argument_integrity  = 1.3
      Default consensus_threshold = 2.0
    """

    def _voice_result(self, passage, model="openai"):
        return _ok({"flags": [_flag(passage)], "low_confidence": []}, model=model)

    def _arg_result(self, passage, model="mistral"):
        return _ok({"flags": [_flag(passage)], "low_confidence": []}, model=model)

    def test_two_high_weight_models_reach_consensus(self):
        """Two strong models (1.2 + 1.2 = 2.4 >= 2.0) → consensus."""
        passage = "The government should utilize more resources"
        results = {
            ("openai", "voice_style"): self._voice_result(passage),
            ("mistral", "argument_integrity"): self._arg_result(passage),
        }
        consensus, single = _find_consensus(results, [], {})
        assert len(consensus) == 1
        assert len(single) == 0

    def test_three_models_same_domain_reach_consensus(self):
        """Three models on the same domain all flagging the same passage."""
        passage = "The government should utilize more resources"
        results = {
            ("openai", "voice_style"): self._voice_result(passage, "openai"),
            ("mistral", "voice_style"): self._voice_result(passage, "mistral"),
            ("claude", "voice_style"): self._voice_result(passage, "claude"),
        }
        consensus, single = _find_consensus(results, [], {})
        assert len(consensus) == 1
        assert len(single) == 0

    def test_two_model_plus_lt_reaches_consensus(self):
        """Two general models (1.0 + 1.0 = 2.0) hits threshold; LT would push it over."""
        passage = "It is worth noting that costs have risen"
        results = {
            ("grok", "voice_style"): _ok(
                {"flags": [_flag(passage)], "low_confidence": []}
            ),
            ("claude", "argument_integrity"): _ok(
                {"flags": [_flag(passage)], "low_confidence": []}
            ),
        }
        consensus, single = _find_consensus(results, [passage], {})
        assert len(consensus) == 1
        assert consensus[0]["languagetool_also_flagged"] is True

    def test_single_model_below_threshold(self):
        """One model alone (weight 1.0) is below the default threshold of 2.0."""
        results = {
            ("grok", "voice_style"): _ok(
                {"flags": [_flag("Some minor phrasing")], "low_confidence": []}
            ),
        }
        consensus, single = _find_consensus(results, [], {})
        assert len(consensus) == 0
        assert len(single) == 1

    def test_empty_passage_excluded(self):
        """Flags with empty passages must not pollute consensus."""
        results = {
            ("openai", "voice_style"): _ok(
                {"flags": [{"passage": "", "problem": "x"}], "low_confidence": []}
            ),
            ("mistral", "argument_integrity"): _ok(
                {"flags": [{"passage": "", "problem": "x"}], "low_confidence": []}
            ),
            ("claude", "argument_integrity"): _ok(
                {"flags": [{"passage": "", "problem": "x"}], "low_confidence": []}
            ),
        }
        consensus, single = _find_consensus(results, [], {})
        assert len(consensus) == 0

    def test_custom_threshold(self):
        """ensemble_cfg.consensus_threshold overrides the built-in default."""
        passage = "threshold override test"
        # Two models together: openai:voice(1.2) + mistral:arg(1.2) = 2.4
        # With threshold=3.0 they should NOT reach consensus.
        results = {
            ("openai", "voice_style"): self._voice_result(passage),
            ("mistral", "argument_integrity"): self._arg_result(passage),
        }
        consensus, single = _find_consensus(results, [], {"consensus_threshold": 3.0})
        assert len(consensus) == 0
        assert len(single) == 2

    def test_consensus_sorted_by_weight(self):
        """Higher weighted_sum passages should sort first."""
        p1 = "lower weight passage"
        p2 = "higher weight passage"
        results = {
            ("openai", "voice_style"): _ok(
                {"flags": [_flag(p1), _flag(p2)], "low_confidence": []}
            ),
            ("claude", "argument_integrity"): _ok(
                {"flags": [_flag(p1), _flag(p2)], "low_confidence": []}
            ),
            ("mistral", "argument_integrity"): _ok(
                {"flags": [_flag(p2)], "low_confidence": []}
            ),
        }
        # p2 gets three model contributions, p1 gets two.
        consensus, _ = _find_consensus(results, [], {})
        assert len(consensus) == 2
        assert consensus[0]["weight_sum"] >= consensus[1]["weight_sum"]


# ---------------------------------------------------------------------------
# build_report — new API: results={(model,domain): result}, ensemble_cfg
# ---------------------------------------------------------------------------


class TestBuildReport:
    def _base_results(self):
        return {
            ("gemini", "fact_check"): _ok(
                {
                    "confirmed": [],
                    "outdated": [],
                    "contradicted": [],
                    "unverifiable": [],
                    "primary_source_needed": [],
                }
            ),
            ("openai", "voice_style"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "argument_integrity"): _ok({"flags": [], "low_confidence": []}),
            ("openai", "completeness"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "red_team"): _ok({}),
        }

    def _lt_ok(self):
        return {
            "change_log": [],
            "flagged_matches": [],
            "failed": False,
            "corrected_text": "Test draft content.",
        }

    def test_basic_report_structure(self):
        report = build_report(
            article_title="Test Article",
            publication_name="test_pub",
            run_number=1,
            corrected_draft="Test draft content.",
            lt_result=self._lt_ok(),
            results=self._base_results(),
            ensemble_cfg={},
            api_call_log=[],
        )
        assert report["article_title"] == "Test Article"
        assert report["run_number"] == 1
        for key in (
            "section_1_consensus",
            "section_2_fact_check",
            "section_3_voice",
            "section_4_argument",
            "section_5_completeness",
            "section_6_red_team",
            "section_7_low_confidence",
            "section_8_additional",
        ):
            assert key in report
        assert report["model_failures"] == []

    def test_failed_models_logged(self):
        results = {
            ("gemini", "fact_check"): _failed("gemini"),
            ("openai", "voice_style"): _failed("openai"),
            ("mistral", "argument_integrity"): _ok({"flags": [], "low_confidence": []}),
            ("openai", "completeness"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "red_team"): _ok({}),
        }
        report = build_report(
            article_title="Test",
            publication_name="pub",
            run_number=1,
            corrected_draft="draft",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        assert "gemini:fact_check" in report["model_failures"]
        assert "openai:voice_style" in report["model_failures"]

    def test_failure_details_carry_the_reason_and_the_affected_section(self):
        """The bare pass name says a model died but not why, and not which
        section is consequently short a vote."""
        results = {
            ("gemini", "fact_check"): {
                "failed": True,
                "error": "Response ended prematurely",
                "model": "gemini-2.5-pro",
                "elapsed_seconds": 413.23,
                "tokens": {},
            },
            ("openai", "voice_style"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "argument_integrity"): _ok({"flags": [], "low_confidence": []}),
            ("openai", "completeness"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "red_team"): _ok({}),
        }
        report = build_report(
            article_title="Test",
            publication_name="pub",
            run_number=1,
            corrected_draft="draft",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        (detail,) = report["model_failure_details"]
        assert detail["pass"] == "gemini:fact_check"
        assert detail["model"] == "gemini-2.5-pro"
        assert detail["error"] == "Response ended prematurely"
        assert detail["elapsed_seconds"] == 413.23
        assert detail["section"] == "SECTION 2: Factual Verification"

    def test_a_skipped_pass_is_not_a_failure(self):
        results = {
            ("gemini", "fact_check"): {"failed": True, "skipped": True, "tokens": {}},
            ("openai", "voice_style"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "argument_integrity"): _ok({"flags": [], "low_confidence": []}),
            ("openai", "completeness"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "red_team"): _ok({}),
        }
        report = build_report(
            article_title="Test",
            publication_name="pub",
            run_number=1,
            corrected_draft="draft",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        assert report["model_failure_details"] == []

    def test_lt_failure_flagged(self):
        results = {
            k: _failed()
            for k in [
                ("gemini", "fact_check"),
                ("openai", "voice_style"),
                ("mistral", "argument_integrity"),
                ("openai", "completeness"),
                ("mistral", "red_team"),
            ]
        }
        report = build_report(
            article_title="Test",
            publication_name="pub",
            run_number=1,
            corrected_draft="draft",
            lt_result={"failed": True, "error": "API down", "change_log": []},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        assert report["lt_failed"] is True

    def test_ensemble_metadata_in_report(self):
        report = build_report(
            article_title="Test",
            publication_name="pub",
            run_number=1,
            corrected_draft="draft",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=self._base_results(),
            ensemble_cfg={"thoroughness": "thorough"},
            api_call_log=[],
        )
        assert "ensemble" in report
        assert report["ensemble"]["thoroughness"] == "thorough"
        assert len(report["ensemble"]["assignments"]) > 0


# ---------------------------------------------------------------------------
# Red team section — single vs. multi-source
# ---------------------------------------------------------------------------


class TestRedTeamSection:
    _rt_data = {
        "most_vulnerable_claim": {
            "passage": "p",
            "attack_vector": "a",
            "supporting_evidence_for_attack": "b",
        },
        "highest_audience_risk": {"passage": "p", "risk": "r", "audience_segment": "s"},
        "highest_credibility_risk": {"passage": "p", "risk": "r", "attack_vector": "a"},
    }

    def _base_non_rt(self):
        return {
            ("gemini", "fact_check"): _failed(),
            ("openai", "voice_style"): _ok({"flags": [], "low_confidence": []}),
            ("mistral", "argument_integrity"): _ok({"flags": [], "low_confidence": []}),
            ("openai", "completeness"): _ok({"flags": [], "low_confidence": []}),
        }

    def test_single_source_flattened(self):
        results = {
            **self._base_non_rt(),
            ("mistral", "red_team"): _ok(self._rt_data),
        }
        report = build_report(
            article_title="T",
            publication_name="p",
            run_number=1,
            corrected_draft="d",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        red_team = report["section_6_red_team"]
        assert "most_vulnerable_claim" in red_team

    def test_multi_source_keyed_by_model(self):
        grok_data = {
            **self._rt_data,
            "most_vulnerable_claim": {
                "passage": "grok finding",
                "attack_vector": "x",
                "supporting_evidence_for_attack": "y",
            },
        }
        results = {
            **self._base_non_rt(),
            ("mistral", "red_team"): _ok(self._rt_data),
            ("grok", "red_team"): _ok(grok_data),
        }
        report = build_report(
            article_title="T",
            publication_name="p",
            run_number=1,
            corrected_draft="d",
            lt_result={"change_log": [], "flagged_matches": [], "failed": False},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        red_team = report["section_6_red_team"]
        assert "mistral" in red_team
        assert "grok" in red_team


# ---------------------------------------------------------------------------
# Multi-model domain merging
# ---------------------------------------------------------------------------


class TestMultiModelSections:
    def test_two_models_voice_both_tagged(self):
        results = {
            ("openai", "voice_style"): _ok(
                {"flags": [_flag("ai-speak phrase")], "low_confidence": []}
            ),
            ("claude", "voice_style"): _ok(
                {"flags": [_flag("ai-speak phrase")], "low_confidence": []}
            ),
        }
        report = build_report(
            article_title="T",
            publication_name="p",
            run_number=1,
            corrected_draft="d",
            lt_result={"change_log": [], "failed": False, "flagged_matches": []},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        flags = report["section_3_voice"]
        assert len(flags) == 2
        models_seen = {f["source_model"] for f in flags}
        assert models_seen == {"openai", "claude"}

    def test_two_grounded_fact_check_models(self):
        fc = {
            "confirmed": [],
            "outdated": [],
            "contradicted": [],
            "unverifiable": [],
            "primary_source_needed": [],
        }
        results = {
            ("gemini", "fact_check"): _ok(fc),
            ("perplexity", "fact_check"): _ok(fc),
        }
        report = build_report(
            article_title="T",
            publication_name="p",
            run_number=1,
            corrected_draft="d",
            lt_result={"change_log": [], "failed": False, "flagged_matches": []},
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        fact = report["section_2_fact_check"]
        assert "_sources" in fact
        assert "gemini" in fact["_sources"]
        assert "perplexity" in fact["_sources"]


# ---------------------------------------------------------------------------
# Rerun recommendation
# ---------------------------------------------------------------------------


class TestRerunRecommended:
    def test_high_word_change(self):
        delta = {
            "word_change_pct": 20,
            "new_consensus_count": 0,
            "resolved_consensus_count": 0,
            "prior_consensus_count": 5,
            "current_consensus_count": 5,
        }
        assert rerun_recommended(delta, {"word_change_threshold_pct": 15}) is True

    def test_new_consensus_flags(self):
        delta = {
            "word_change_pct": 5,
            "new_consensus_count": 2,
            "resolved_consensus_count": 1,
            "prior_consensus_count": 3,
            "current_consensus_count": 4,
        }
        assert rerun_recommended(delta, {"word_change_threshold_pct": 15}) is True

    def test_stable_draft(self):
        delta = {
            "word_change_pct": 5,
            "new_consensus_count": 0,
            "resolved_consensus_count": 2,
            "prior_consensus_count": 3,
            "current_consensus_count": 1,
        }
        assert rerun_recommended(delta, {"word_change_threshold_pct": 15}) is False

    def test_no_delta(self):
        assert rerun_recommended(None, {}) is False

    # --- configurable triggers (previously dead config, now honored) ---
    _STABLE = {
        "word_change_pct": 1,
        "new_consensus_count": 0,
        "resolved_consensus_count": 0,
        "prior_consensus_count": 2,
        "current_consensus_count": 2,
    }

    def test_claim_change_triggers_rerun_by_default(self):
        delta = {**self._STABLE, "claim_changed": True, "structure_changed": False}
        # Default config (flag absent) treats claim_change_triggers_rerun as True
        assert rerun_recommended(delta, {"word_change_threshold_pct": 15}) is True

    def test_claim_change_suppressed_when_flag_false(self):
        delta = {**self._STABLE, "claim_changed": True, "structure_changed": False}
        cfg = {"word_change_threshold_pct": 15, "claim_change_triggers_rerun": False}
        assert rerun_recommended(delta, cfg) is False

    def test_structure_change_triggers_rerun_by_default(self):
        delta = {**self._STABLE, "claim_changed": False, "structure_changed": True}
        assert rerun_recommended(delta, {"word_change_threshold_pct": 15}) is True

    def test_structure_change_suppressed_when_flag_false(self):
        delta = {**self._STABLE, "claim_changed": False, "structure_changed": True}
        cfg = {
            "word_change_threshold_pct": 15,
            "structure_change_triggers_rerun": False,
        }
        assert rerun_recommended(delta, cfg) is False

    def test_legacy_delta_without_new_keys_does_not_crash(self):
        # A delta dict from before these keys existed must still evaluate safely.
        assert (
            rerun_recommended(dict(self._STABLE), {"word_change_threshold_pct": 15})
            is False
        )


class TestComputeDeltaDetection:
    """Direct tests for claim- and structure-change detection in _compute_delta."""

    def _prior(self, draft, claim=""):
        return {
            "corrected_draft": draft,
            "primary_claim": claim,
            "section_1_consensus": [],
        }

    def test_claim_changed_detected(self):
        from ci_article_review.consolidation import _compute_delta

        prior = self._prior(
            "# Title\n\nbody", claim="Data centers harm local water tables."
        )
        d = _compute_delta(
            "# Title\n\nbody",
            prior,
            [],
            current_claim="Data centers are carbon neutral.",
        )
        assert d["claim_changed"] is True

    def test_claim_unchanged_when_identical(self):
        from ci_article_review.consolidation import _compute_delta

        prior = self._prior(
            "# Title\n\nbody", claim="The grid cannot absorb this load."
        )
        # Whitespace/case differences must not count as a change.
        d = _compute_delta(
            "# Title\n\nbody",
            prior,
            [],
            current_claim="the grid cannot   absorb this load.",
        )
        assert d["claim_changed"] is False

    def test_claim_not_flagged_when_prior_has_none(self):
        from ci_article_review.consolidation import _compute_delta

        # Legacy report with no stored claim — cannot compare, must not trigger.
        prior = self._prior("# Title\n\nbody", claim="")
        d = _compute_delta(
            "# Title\n\nbody", prior, [], current_claim="A brand new claim."
        )
        assert d["claim_changed"] is False

    def test_structure_changed_on_added_heading(self):
        from ci_article_review.consolidation import _compute_delta

        prior = self._prior("# Title\n\n## One\n\nbody")
        d = _compute_delta("# Title\n\n## One\n\n## Two\n\nbody", prior, [])
        assert d["structure_changed"] is True

    def test_structure_unchanged_on_body_only_edit(self):
        from ci_article_review.consolidation import _compute_delta

        prior = self._prior("# Title\n\n## One\n\noriginal body text")
        d = _compute_delta(
            "# Title\n\n## One\n\ncompletely different body wording here", prior, []
        )
        assert d["structure_changed"] is False


# ---------------------------------------------------------------------------
# Regressions from the 2026-09-03 audit
# ---------------------------------------------------------------------------


class TestFactCheckReachesConsensus:
    """The buckets models actually fill must feed Section 1.

    Consensus read only ``outdated`` and ``contradicted``. Both were empty
    across all six fact-check passes on 2026-09-03 while ``unverifiable`` held
    50 claims, so the domain that cost 39% of the run contributed nothing.
    """

    def test_unverifiable_agreed_by_two_models_is_consensus(self):
        results = {
            ("gemini", "fact_check"): _ok(
                {"unverifiable": [{"claim": "The grid is complex."}]}
            ),
            ("openai", "fact_check"): _ok(
                {"unverifiable": [{"claim": "The grid is complex."}]}
            ),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert len(consensus) == 1
        assert consensus[0]["passage"] == "The grid is complex."

    def test_primary_source_needed_also_counts(self):
        results = {
            ("gemini", "fact_check"): _ok(
                {"primary_source_needed": [{"claim": "The grid is complex."}]}
            ),
            ("openai", "fact_check"): _ok(
                {"primary_source_needed": [{"claim": "The grid is complex."}]}
            ),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert len(consensus) == 1

    def test_confirmed_is_not_a_finding(self):
        """A claim that checked out is not something to fix, so it stays out of
        the section that drives the revision prompt."""
        results = {
            ("gemini", "fact_check"): _ok(
                {"confirmed": [{"claim": "The grid is complex."}]}
            ),
            ("openai", "fact_check"): _ok(
                {"confirmed": [{"claim": "The grid is complex."}]}
            ),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert consensus == []


class TestConsensusNeedsDistinctModels:
    """Weight alone let one model agree with itself into Section 1."""

    def test_one_model_flagging_twice_is_not_consensus(self):
        # Two red_team sub-findings on one passage, the exact shape that put
        # mistral alone into Section 1 at weight 2.2 against a 2.0 threshold.
        results = {
            ("mistral", "red_team"): _ok(
                {
                    "most_vulnerable_claim": {"passage": "The grid is complex."},
                    "highest_credibility_risk": {"passage": "The grid is complex."},
                }
            ),
        }
        consensus, single = _find_consensus(results, [], {})
        assert consensus == []
        assert len(single) == 2

    def test_two_models_still_reach_consensus(self):
        results = {
            ("mistral", "red_team"): _ok(
                {"most_vulnerable_claim": {"passage": "The grid is complex."}}
            ),
            ("grok", "red_team"): _ok(
                {"most_vulnerable_claim": {"passage": "The grid is complex."}}
            ),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert len(consensus) == 1

    def test_languagetool_counts_as_a_second_source(self):
        results = {
            ("claude", "voice_style"): _ok(
                {"flags": [_flag("It is important to note that the grid is complex.")]}
            ),
        }
        consensus, _ = _find_consensus(
            results,
            ["It is important to note that the grid is complex."],
            {"consensus_threshold": 1.0},
        )
        assert len(consensus) == 1
        assert consensus[0]["languagetool_also_flagged"] is True


class TestNestedQuotationsMerge:
    """One passage quoted two ways is one finding, not two."""

    def test_the_same_claim_quoted_two_ways_is_one_item(self):
        shorter = "Citations get checked against primary sources every time."
        longer = "Citations get checked against primary sources every single time."
        results = {
            ("claude", "voice_style"): _ok({"flags": [_flag(shorter)]}),
            ("openai", "voice_style"): _ok({"flags": [_flag(longer)]}),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert len(consensus) == 1
        # The fuller quotation represents the group.
        assert consensus[0]["passage"] == longer

    def test_a_sentence_inside_a_much_longer_quote_is_left_alone(self):
        """Deliberate. Containment cannot separate a fuller quote of one claim
        from a different claim in the same paragraph, and the caller treats
        Section 1 as its strongest signal, so only near-identical quotations
        merge. Merging these two cost a fabricated 27-flag consensus group on
        the real 2026-09-03 draft."""
        sentence = "Citations get checked against primary sources every time."
        paragraph = (
            sentence + " Arguments get stress-tested by a process built to find "
            "the weak points I did not catch while writing, and drafts get "
            "flagged wherever I overstated the evidence."
        )
        results = {
            ("claude", "voice_style"): _ok({"flags": [_flag(sentence)]}),
            ("openai", "voice_style"): _ok({"flags": [_flag(paragraph)]}),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert consensus == []

    def test_different_sentences_of_one_paragraph_stay_separate(self):
        """The guard against the opposite error: two models flagging different
        sentences are not agreeing with each other."""
        results = {
            ("claude", "voice_style"): _ok(
                {"flags": [_flag("The transmission queue is twelve years long.")]}
            ),
            ("openai", "voice_style"): _ok(
                {"flags": [_flag("Local officials were never consulted at all.")]}
            ),
        }
        consensus, _ = _find_consensus(results, [], {})
        assert consensus == []


class TestEmptyResultsAreRecorded:
    """A well-formed empty response is a third outcome, not a success."""

    def test_an_empty_payload_is_flagged(self):
        results = {
            ("gemini", "voice_style"): _ok(
                {"flags": [], "low_confidence": [], "additional_observations": []}
            ),
            ("openai", "voice_style"): _ok({"flags": [_flag("The grid is complex.")]}),
        }
        report = build_report(
            "T", "pub", 1, "draft text", None, results, {}, [], primary_claim=""
        )
        assert report["empty_results"] == ["gemini:voice_style"]
        assert report["model_failures"] == []
        detail = report["empty_result_details"][0]
        assert detail["section"] == "SECTION 3: Voice and AI-Speak"

    def test_a_populated_pass_is_not_flagged(self):
        results = {
            ("openai", "voice_style"): _ok({"flags": [_flag("The grid is complex.")]}),
        }
        report = build_report(
            "T", "pub", 1, "draft text", None, results, {}, [], primary_claim=""
        )
        assert report["empty_results"] == []
