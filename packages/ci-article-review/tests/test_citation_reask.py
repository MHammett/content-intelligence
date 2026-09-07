"""Pass 3b — handing a refuted citation back to the model that asserted it.

A ``content_mismatch`` was terminal: the claim sat in Section 9 as refuted and
the model that asserted it was never told. In the measured cases the repair was
already in hand — a page reading "66 billion liters" against a claim of "17
billion gallons" states the correct figure (≈17.4 billion gallons) and was
reported only as a failed citation.

The model being asked is the one whose assertion just failed, so the tests that
matter most here are the ones about what it is *not* allowed to do: a re-ask
never changes ``verification``, and a URL it proposes is re-resolved rather than
reported on the model's say-so.
"""

from unittest.mock import patch

from ci_article_review.adapters.citation import reask


def _refuted(**kw):
    base = {
        "claim": "U.S. data centers consumed 17 billion gallons of water in 2023",
        "url": "https://example.org/report",
        "verification": "content_mismatch",
        "resolved": False,
        "relevance_verdict": "contradicts",
        "relevance_reason": "The page states 66 billion liters, not 17 billion gallons.",
        "source_model": "gemini",
        "fact_check_bucket": "confirmed",
    }
    base.update(kw)
    return base


def _response(data, failed=False):
    return {"failed": failed, "data": data, "tokens": {}}


class TestWhichResultsAreActedOn:
    def test_only_content_mismatch_qualifies(self):
        assert reask.is_refuted(_refuted())
        for verification in ("checksum", "unverifiable", "pointer", None):
            assert not reask.is_refuted(_refuted(verification=verification))

    def test_a_claim_with_no_asserting_model_is_skipped(self):
        """A claim traced to the draft's own citation block was asserted by the
        author. There is no model to hand it back to, and picking one would be
        asking a model to defend something it never said."""
        results = [_refuted(source_model="")]
        with patch.object(reask.llm, "call_provider") as call:
            asked = reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}})
        assert asked == 0
        assert not call.called
        assert "reask" not in results[0]

    def test_a_fallback_provider_covers_authorless_claims_when_given(self):
        results = [_refuted(source_model="")]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "withdraw", "reason": "r"}),
        ):
            asked = reask.reask_refuted(
                results,
                api_keys={"mistral": {"api_key": "k"}},
                fallback_provider="mistral",
            )
        assert asked == 1
        assert results[0]["reask"]["asked_model"] == "mistral"

    def test_a_model_with_no_key_is_skipped(self):
        results = [_refuted()]
        assert reask.reask_refuted(results, api_keys={}) == 0
        assert "reask" not in results[0]

    def test_the_asserting_model_is_the_one_asked(self):
        results = [_refuted(source_model="perplexity")]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "stand", "reason": "r"}),
        ) as call:
            reask.reask_refuted(results, api_keys={"perplexity": {"api_key": "k"}})
        assert call.call_args.args[0] == "perplexity"


class TestTheAnswerCannotUpgradeTheCitation:
    """The question invites the model to defend itself, so the answer is
    advisory by construction."""

    def test_verification_is_untouched_by_every_action(self):
        for action, extra in (
            ("correct_claim", {"corrected_claim": "17.4 billion gallons"}),
            ("different_source", {"source_url": "https://example.org/other"}),
            ("withdraw", {}),
            ("stand", {}),
        ):
            results = [_refuted()]
            payload = {"action": action, "reason": "r", **extra}
            with patch.object(
                reask.llm, "call_provider", return_value=_response(payload)
            ):
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}})
            assert results[0]["verification"] == "content_mismatch", action
            assert results[0]["resolved"] is False, action

    def test_a_source_check_does_not_leak_into_the_result(self):
        """Even a proposed source that checks out leaves the original refutation
        refuted — it was a different URL."""
        results = [_refuted()]
        results[0]["reask"] = {
            "action": "different_source",
            "source_url": "https://example.org/other",
        }
        pending = reask.proposed_source_claims(results)
        reask.attach_source_checks(
            pending, [{"verification": "checksum", "resolved": True}]
        )
        assert results[0]["verification"] == "content_mismatch"
        assert results[0]["resolved"] is False
        assert results[0]["reask"]["source_check"]["verification"] == "checksum"


class TestProposedSources:
    def test_a_proposal_becomes_a_claim_entry_for_re_resolution(self):
        results = [_refuted()]
        results[0]["reask"] = {
            "action": "different_source",
            "source_url": "https://example.org/other",
        }
        pending = reask.proposed_source_claims(results)
        assert len(pending) == 1
        _result, entry = pending[0]
        assert entry["known_urls"] == ["https://example.org/other"]
        assert entry["claim"] == results[0]["claim"]
        assert entry["fact_check_bucket"] == "confirmed"

    def test_other_actions_propose_nothing_to_check(self):
        for action in ("correct_claim", "withdraw", "stand"):
            results = [_refuted()]
            results[0]["reask"] = {"action": action, "source_url": ""}
            assert reask.proposed_source_claims(results) == []

    def test_the_outcome_is_recorded_under_the_reask(self):
        results = [_refuted()]
        results[0]["reask"] = {
            "action": "different_source",
            "source_url": "https://example.org/other",
        }
        pending = reask.proposed_source_claims(results)
        reask.attach_source_checks(
            pending,
            [
                {
                    "verification": "content_mismatch",
                    "resolved": False,
                    "url": "https://example.org/other",
                    "relevance_verdict": "not_addressed",
                }
            ],
        )
        check = results[0]["reask"]["source_check"]
        assert check["verification"] == "content_mismatch"
        assert check["relevance_verdict"] == "not_addressed"


class TestUnusableAnswers:
    def test_an_unrecognised_action_is_discarded(self):
        results = [_refuted()]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "argue", "reason": "r"}),
        ):
            assert (
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}}) == 0
            )
        assert "reask" not in results[0]

    def test_correct_claim_without_a_claim_is_discarded(self):
        """Otherwise it renders as a proposal with nothing proposed."""
        results = [_refuted()]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "correct_claim", "reason": "r"}),
        ):
            assert (
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}}) == 0
            )

    def test_different_source_without_a_url_is_discarded(self):
        results = [_refuted()]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "different_source", "reason": "r"}),
        ):
            assert (
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}}) == 0
            )

    def test_a_failed_call_leaves_the_refutation_alone(self):
        results = [_refuted()]
        with patch.object(
            reask.llm, "call_provider", return_value=_response(None, failed=True)
        ):
            assert (
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}}) == 0
            )
        assert results[0]["verification"] == "content_mismatch"

    def test_a_raising_call_never_propagates(self):
        """This pass is advisory; a failure here must not take down citations."""
        results = [_refuted()]
        with patch.object(reask.llm, "call_provider", side_effect=RuntimeError("boom")):
            assert (
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}}) == 0
            )


class TestCostControls:
    def test_the_limit_bounds_the_number_of_calls(self):
        results = [_refuted() for _ in range(5)]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "stand", "reason": "r"}),
        ) as call:
            asked = reask.reask_refuted(
                results, api_keys={"gemini": {"api_key": "k"}}, limit=2
            )
        assert asked == 2
        assert call.call_count == 2
        assert sum(1 for r in results if "reask" in r) == 2

    def test_no_refutations_makes_no_calls(self):
        results = [_refuted(verification="checksum")]
        with patch.object(reask.llm, "call_provider") as call:
            assert (
                reask.reask_refuted(results, api_keys={"gemini": {"api_key": "k"}}) == 0
            )
        assert not call.called

    def test_web_search_is_disabled_for_the_call(self):
        """The repairable cases are answered from the refutation already in the
        prompt, and a proposal gets fetched and checked either way."""
        results = [_refuted()]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "stand", "reason": "r"}),
        ) as call:
            reask.reask_refuted(
                results,
                api_keys={"gemini": {"api_key": "k"}},
                model_configs={"gemini": {"web_search": ["fact_check"]}},
            )
        assert call.call_args.kwargs["provider_config"]["web_search"] is False


class TestThePrompt:
    def test_the_refutation_and_claim_are_both_shown(self):
        prompt = reask._build_prompt(_refuted())
        assert "17 billion gallons" in prompt
        assert "66 billion liters" in prompt
        assert "https://example.org/report" in prompt

    def test_a_verified_page_quote_is_included_when_present(self):
        """It was checked against the fetched page before being stored, so it is
        the part of the prompt with evidence behind it."""
        prompt = reask._build_prompt(_refuted(relevance_quote="66 billion liters"))
        assert "PAGE_QUOTE" in prompt

    def test_untrusted_page_text_is_delimited(self):
        prompt = reask._build_prompt(_refuted())
        assert "<<<REFUTATION" in prompt

    def test_the_system_prompt_refuses_guessed_urls(self):
        assert "Do not guess a URL" in reask._SYSTEM_PROMPT


class TestRendering:
    def _render(self, reask_dict):
        from ci_article_review.report_markdown import _render_reask

        return "\n".join(_render_reask(reask_dict))

    def test_nothing_renders_without_a_reask(self):
        assert self._render(None) == ""
        assert self._render({}) == ""

    def test_a_correction_shows_the_proposed_wording(self):
        out = self._render(
            {
                "action": "correct_claim",
                "asked_model": "gemini",
                "reason": "The report gives 66 billion liters.",
                "corrected_claim": "roughly 17.4 billion gallons",
            }
        )
        assert "Asked gemini again" in out
        assert "17.4 billion gallons" in out

    def test_a_stand_is_reported_as_plainly_as_a_withdrawal(self):
        """The model disagreeing with the refutation is information, not an
        outcome to absorb."""
        out = self._render({"action": "stand", "asked_model": "grok", "reason": "r"})
        assert "maintains the claim" in out

    def test_an_unchecked_proposal_is_labelled_a_lead(self):
        out = self._render(
            {
                "action": "different_source",
                "asked_model": "gemini",
                "source_url": "https://example.org/other",
                "reason": "r",
            }
        )
        assert "NOT checked" in out

    def test_a_checked_proposal_reports_what_the_page_said(self):
        out = self._render(
            {
                "action": "different_source",
                "asked_model": "gemini",
                "source_url": "https://example.org/other",
                "reason": "r",
                "source_check": {"verification": "checksum", "resolved": True},
            }
        )
        assert "does support the claim" in out
        assert "NOT checked" not in out

    def test_a_proposal_that_also_failed_says_so(self):
        out = self._render(
            {
                "action": "different_source",
                "asked_model": "gemini",
                "source_url": "https://example.org/other",
                "reason": "r",
                "source_check": {
                    "verification": "content_mismatch",
                    "relevance_verdict": "not_addressed",
                },
            }
        )
        assert "does NOT support the claim" in out


class TestThePipelineActuallyRunsThePass:
    """Module tests prove the pass works; these prove the pipeline reaches it.

    Both defects this feature was built around were of the other kind — code
    that was correct in isolation and never fired on real input. The re-ask
    depends on a chain (fact-check tagging -> claim collection -> claim/result
    join -> the call) where any broken link is silent, so the chain is asserted
    here rather than inferred.
    """

    def _mismatch_resolver(self, claims, *a, **kw):
        return [
            {
                "claim": c["claim"] if isinstance(c, dict) else c,
                "url": "https://example.org/resolved",
                "resolved": False,
                "verification": "content_mismatch",
                "relevance_verdict": "contradicts",
                "relevance_reason": "The page states 66 billion liters.",
            }
            for c in claims
        ]

    def test_a_refuted_claim_reaches_the_asserting_model(self, tmp_path):
        from .test_pipeline_end_to_end import _stubbed_run

        seen = {}

        def _spy(results, **kw):
            seen["results"] = results
            seen["model_configs"] = kw.get("model_configs")
            for r in results:
                if reask.is_refuted(r):
                    r["reask"] = {
                        "action": "correct_claim",
                        "asked_model": r.get("source_model", ""),
                        "corrected_claim": "roughly 17.4 billion gallons",
                        "reason": "The report gives 66 billion liters.",
                    }
            return sum(1 for r in results if "reask" in r)

        patches = [
            patch(
                "ci_article_review.adapters.citation.resolver.resolve_citations",
                side_effect=self._mismatch_resolver,
            ),
            patch(
                "ci_article_review.adapters.citation.reask.reask_refuted",
                side_effect=_spy,
            ),
        ]
        with _stubbed_run(tmp_path, extra_patches=patches) as report:
            pass

        assert "results" in seen, "the pipeline never invoked the re-ask pass"

        # The join that makes the hand-back possible: every refuted claim knows
        # which model asserted it. An empty string here means the chain from
        # `_build_fact_check`'s per-item tag through `_collect_citation_claims`
        # is broken, and every re-ask would be skipped for want of a model.
        refuted = [r for r in seen["results"] if reask.is_refuted(r)]
        assert refuted, "the stub should have produced refutations"
        assert all(r.get("source_model") for r in refuted), (
            "refuted claims reached the re-ask with no asserting model"
        )

        # And the answer survives into the report the author reads.
        answered = [c for c in report["section_9_citations"] if c.get("reask")]
        assert answered, "no re-ask reached Section 9"
        assert answered[0]["reask"]["action"] == "correct_claim"
        assert answered[0]["verification"] == "content_mismatch", (
            "a re-ask must never upgrade the citation's verification"
        )

    def test_the_pass_is_skipped_offline(self, tmp_path):
        from .test_pipeline_end_to_end import _stubbed_run

        with patch("ci_article_review.adapters.citation.reask.reask_refuted") as spy:
            with _stubbed_run(tmp_path, offline=True):
                pass
        assert not spy.called

    def test_the_pass_can_be_turned_off(self, tmp_path):
        from .test_pipeline_end_to_end import _CONFIG, _stubbed_run

        cfg = {**_CONFIG, "pipeline": {**_CONFIG["pipeline"], "citation_reask": False}}
        patches = [
            patch(
                "ci_article_review.adapters.citation.resolver.resolve_citations",
                side_effect=self._mismatch_resolver,
            ),
            patch("ci_article_review.pipeline.merge_configs", return_value=cfg),
        ]
        with patch("ci_article_review.adapters.citation.reask.reask_refuted") as spy:
            with _stubbed_run(tmp_path, extra_patches=patches):
                pass
        assert not spy.called


class TestDefectsOnlyALiveCallFound:
    """Three things every unit test above passed while broken in production."""

    def test_every_schema_property_is_required(self):
        """OpenAI's strict structured-output mode rejects a schema whose
        ``required`` is not every key in ``properties`` — a 400 before any
        tokens are generated. Listing only the two always-meaningful fields
        looked like the more honest schema and made this pass a silent no-op for
        one of the asserting models. Measured live 2026-09-04: gpt-5.6-luna
        returned no usable answer on all three test refutations; with the full
        list it answered all three.
        """
        schema = reask._SCHEMA["schema"]
        assert set(schema["required"]) == set(schema["properties"]), (
            "OpenAI rejects a partial `required`; keep it as list(properties) "
            "and let _normalise enforce which payload an action needs."
        )

    def test_the_author_reaches_the_prompt(self):
        """Told nothing, a model treats a first-person claim as an unsourced
        assertion. Measured live: "I have a side job." came back ``withdraw``
        from every provider without an author and ``stand`` from every provider
        with one."""
        assert "Mike Hammett" in reask._build_prompt(_refuted(), author="Mike Hammett")
        assert "Mike Hammett" not in reask._build_prompt(_refuted())

    def test_the_author_is_threaded_from_reask_refuted(self):
        results = [_refuted()]
        with patch.object(
            reask.llm,
            "call_provider",
            return_value=_response({"action": "stand", "reason": "r"}),
        ) as call:
            reask.reask_refuted(
                results,
                api_keys={"gemini": {"api_key": "k"}},
                author="Mike Hammett",
            )
        assert "Mike Hammett" in call.call_args.args[2]

    def test_the_prompt_does_not_invite_withdrawal_on_a_missing_page(self):
        """``not_addressed`` was 47 of 49 refutations in the measured run, and
        usually means the wrong URL was checked rather than that the claim is
        false. Without this guidance every provider answered ``withdraw``."""
        prompt = reask._SYSTEM_PROMPT
        assert "not_addressed" in prompt
        assert "Do not " in prompt and "withdraw a claim merely because" in prompt
