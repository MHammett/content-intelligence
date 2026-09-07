"""A domain that loses every model it was assigned gets a different provider.

Measured 2026-09-05, `dc-environment-v26` at `--cost-preset standard`:
`gemini:fact_check` returned *"stream stalled before the first chunk: nothing
received for 160.0s"*, the recovery pass retried the same model and it stalled
identically, and the run exited 0 having spent $0.64. At `standard` thoroughness
fact_check is a single model and it is the only source of claims, so Section 2
came back empty, no claim reached citation resolution, and Section 9 was empty
too — the two sections that justify the pipeline, gone to one provider timeout.

Recovery cannot fix that shape: it retries the model that failed, which is the
right move for a flake and useless for an outage. Substitution is the other
half — a *different* provider, once, only for a domain that has nothing.
"""

from unittest.mock import patch

from ci_article_review import pipeline


def _ok(model, domain):
    return {
        "failed": False,
        "data": {"flags": [{"passage": "p"}]},
        "model": model,
        "tokens": {},
        "_model": model,
        "_domain": domain,
    }


def _failed(model, domain, error="stream stalled before the first chunk"):
    return {
        "failed": True,
        "error": error,
        "model": model,
        "tokens": {},
        "_model": model,
        "_domain": domain,
    }


_KEYS = {
    m: {"api_key": "k"}
    for m in ("gemini", "perplexity", "openai", "mistral", "grok", "claude")
}


class TestWhichDomainsAreEmpty:
    def test_a_domain_with_one_failed_model_is_empty(self):
        results = {"gemini:fact_check": _failed("gemini", "fact_check")}
        assert pipeline._domains_with_nothing_usable(results) == ["fact_check"]

    def test_a_domain_with_one_survivor_is_not_empty(self):
        """Losing one of two models is lost coverage, not a lost domain, and the
        report already reports it as such."""
        results = {
            "mistral:red_team": _failed("mistral", "red_team"),
            "grok:red_team": _ok("grok", "red_team"),
        }
        assert pipeline._domains_with_nothing_usable(results) == []

    def test_a_success_carrying_no_data_does_not_count_as_usable(self):
        """`failed: False` with empty data is the shape a truncated or salvaged
        response leaves behind; it contributes nothing to the section."""
        results = {
            "gemini:fact_check": {
                "failed": False,
                "data": None,
                "_model": "gemini",
                "_domain": "fact_check",
            }
        }
        assert pipeline._domains_with_nothing_usable(results) == ["fact_check"]

    def test_a_clean_run_has_none(self):
        results = {
            "gemini:fact_check": _ok("gemini", "fact_check"),
            "openai:voice_style": _ok("openai", "voice_style"),
        }
        assert pipeline._domains_with_nothing_usable(results) == []


class TestChoosingTheSubstitute:
    def test_the_failed_model_is_never_chosen_again(self):
        """Retrying it is exactly what the recovery pass already did."""
        got = pipeline._substitute_candidates("fact_check", {"gemini"}, {}, _KEYS, None)
        assert "gemini" not in got
        assert got, "some other configured model should be available"

    def test_fact_check_prefers_a_search_grounded_model(self):
        """Grounding is the whole reason gemini is in the fact_check ensemble;
        a substitute that cannot search is answering from training recall."""
        got = pipeline._substitute_candidates("fact_check", {"gemini"}, {}, _KEYS, None)
        assert got[0] == "perplexity"

    def test_an_ungrounded_model_is_still_better_than_an_empty_section(self):
        got = pipeline._substitute_candidates(
            "fact_check", {"gemini", "perplexity"}, {}, _KEYS, None
        )
        assert got, "should fall back past the grounded models rather than give up"
        assert not set(got) & {"gemini", "perplexity"}

    def test_a_model_without_credentials_is_not_offered(self):
        got = pipeline._substitute_candidates(
            "fact_check", {"gemini"}, {}, {"perplexity": {"api_key": "k"}}, None
        )
        assert got == ["perplexity"]

    def test_the_drafting_model_stays_excluded_from_voice_style(self):
        """Substituting must not quietly undo the drafter exclusion — the whole
        point of which is that a model under-reports its own habits."""
        got = pipeline._substitute_candidates(
            "voice_style", {"openai"}, {}, _KEYS, "claude"
        )
        assert "claude" not in got

    def test_a_prompt_override_is_respected(self):
        configs = {"perplexity": {"prompts": ["red_team"]}}
        got = pipeline._substitute_candidates(
            "fact_check", {"gemini"}, configs, _KEYS, None
        )
        assert "perplexity" not in got

    def test_no_candidates_when_nothing_else_is_configured(self):
        got = pipeline._substitute_candidates(
            "fact_check", {"gemini"}, {}, {"gemini": {"api_key": "k"}}, None
        )
        assert got == []


class TestSubstitutionRuns:
    def _make_runner(self, model, domain):
        return (f"{model}:{domain}", lambda: _ok(model, domain))

    def _run(self, results, cfg=None, **kw):
        return pipeline._substitute_for_empty_domains(
            results,
            self._make_runner,
            cfg if cfg is not None else {},
            {},
            _KEYS,
            60,
            **kw,
        )

    def test_the_measured_failure_is_repaired(self):
        results = {"gemini:fact_check": _failed("gemini", "fact_check")}
        with patch.object(
            pipeline,
            "_run_reviews_for_names",
            side_effect=lambda names, runners, *a: {n: f() for n, f in runners},
        ):
            out = self._run(results)
        assert "perplexity:fact_check" in out
        assert pipeline._domains_with_nothing_usable(out) == []

    def test_the_original_failure_is_kept_alongside_it(self):
        """The report still has to be able to say gemini failed; a substitute
        that erased the failure would make the run look clean."""
        results = {"gemini:fact_check": _failed("gemini", "fact_check")}
        with patch.object(
            pipeline,
            "_run_reviews_for_names",
            side_effect=lambda names, runners, *a: {n: f() for n, f in runners},
        ):
            out = self._run(results)
        assert out["gemini:fact_check"]["failed"] is True

    def test_a_clean_run_makes_no_extra_call(self):
        results = {"gemini:fact_check": _ok("gemini", "fact_check")}
        with patch.object(pipeline, "_run_reviews_for_names") as spy:
            self._run(results)
        assert not spy.called

    def test_only_one_substitute_is_tried_per_domain(self):
        """Otherwise a provider outage buys call after call while nothing works."""
        results = {"gemini:fact_check": _failed("gemini", "fact_check")}
        calls = []

        def _run_names(names, runners, *a):
            calls.append(names)
            return {n: _failed(n.split(":")[0], "fact_check") for n, _ in runners}

        with patch.object(pipeline, "_run_reviews_for_names", side_effect=_run_names):
            out = self._run(results)
        assert len(calls) == 1
        assert pipeline._domains_with_nothing_usable(out) == ["fact_check"]

    def test_it_can_be_turned_off(self):
        results = {"gemini:fact_check": _failed("gemini", "fact_check")}
        with patch.object(pipeline, "_run_reviews_for_names") as spy:
            self._run(results, cfg={"substitute_failed_domains": False})
        assert not spy.called

    def test_nothing_happens_when_no_substitute_exists(self):
        results = {"gemini:fact_check": _failed("gemini", "fact_check")}
        with patch.object(pipeline, "_run_reviews_for_names") as spy:
            pipeline._substitute_for_empty_domains(
                results,
                self._make_runner,
                {},
                {},
                {"gemini": {"api_key": "k"}},
                60,
            )
        assert not spy.called


class TestThePipelineActuallySubstitutes:
    """The unit tests above prove the logic; this proves the pipeline reaches it.

    Every defect this session that survived a green suite was of the other kind
    -- correct code the pipeline never called with real input -- so the wiring
    gets asserted rather than assumed.
    """

    def test_a_failing_only_model_is_replaced_and_the_sections_survive(self, tmp_path):
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        attempted = []

        def _stub(model_name, domain, *a, **kw):
            attempted.append(f"{model_name}:{domain}")
            if model_name == "gemini" and domain == "fact_check":
                return _failed("gemini", "fact_check")
            return _fake_run_domain(model_name, domain, *a, **kw)

        patches = [patch("ci_article_review.pipeline._run_domain", side_effect=_stub)]
        with _stubbed_run(tmp_path, extra_patches=patches) as report:
            pass

        assert "gemini:fact_check" in attempted, "the original call should be tried"
        substitutes = [
            a
            for a in attempted
            if a.endswith(":fact_check") and not a.startswith("gemini")
        ]
        assert substitutes, (
            "no substitute was attempted for the domain that lost its only model"
        )

        # The point of the whole thing: Section 2 is not empty, so claims exist
        # and Section 9 has something to resolve.
        assert report["section_2_fact_check"], "Section 2 came back empty anyway"
        assert report["section_9_citations"], "Section 9 came back empty anyway"

    def test_a_clean_run_adds_no_calls(self, tmp_path):
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        attempted = []

        def _stub(model_name, domain, *a, **kw):
            attempted.append(f"{model_name}:{domain}")
            return _fake_run_domain(model_name, domain, *a, **kw)

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch("ci_article_review.pipeline._run_domain", side_effect=_stub)
            ],
        ):
            pass

        assert len(attempted) == len(set(attempted)), "a call was made twice"
        assert sorted(a for a in attempted if a.endswith(":fact_check")) == [
            "gemini:fact_check"
        ]


class TestPermanentFailureDetection:
    """Recovery skips failures a retry cannot fix. Gemini's phrasing evaded it.

    Measured 2026-09-05 with a deliberately invalid key: recovery slept its full
    delay and re-called a provider that had already answered "API key not
    valid", because the markers looked for "invalid api key" and
    "invalid_api_key" while Gemini says it the other way round in both the prose
    and the reason code.
    """

    _GEMINI_DEAD_KEY = (
        "litellm.AuthenticationError: Vertex_ai_betaException - "
        '{"error": {"code": 400, "message": "API key not valid. Please pass a '
        'valid API key.", "status": "INVALID_ARGUMENT", "details": [{"reason": '
        '"API_KEY_INVALID", "domain": "googleapis.com"}]}}'
    )

    def test_the_measured_gemini_error_is_recognised(self):
        assert pipeline._looks_permanent(self._GEMINI_DEAD_KEY)

    def test_the_litellm_exception_name_alone_is_enough(self):
        """Catches provider phrasings nobody has written down yet, since litellm
        normalises auth failures to this class across every provider."""
        assert pipeline._looks_permanent("litellm.AuthenticationError: Whatever")

    def test_transient_failures_stay_retryable(self):
        """Over-matching here would be worse than under-matching: it would stop
        recovery retrying the flakes it exists for -- including the stall that
        motivated the substitution pass."""
        for error in (
            "stream stalled before the first chunk: nothing received for 160.0s",
            "Request timed out after 210s",
            "RateLimitError: 429 Too Many Requests",
            "APIConnectionError: connection reset by peer",
            "response was truncated (hit the output-token ceiling)",
        ):
            assert not pipeline._looks_permanent(error), error

    def test_the_older_spellings_still_match(self):
        for error in (
            "Invalid API key provided",
            "invalid_api_key",
            "401 Unauthorized",
            "insufficient_quota",
        ):
            assert pipeline._looks_permanent(error), error


class TestDomainsNeverAssignedAreRepairable:
    """A domain can end up empty two ways: its model failed, or it never ran.

    The second is the drafter-exclusion case documented under "Drafting model"
    in docs/CONFIGURATION.md -- at `standard` thoroughness `voice_style` is a
    single model, so naming that model as the drafter leaves the domain with no
    reviewer and no result key at all. Deriving the empty set from `raw_results`
    alone made that invisible to the pass built to repair it.
    """

    def test_a_domain_with_no_result_at_all_is_flagged(self):
        raw = {"gemini:fact_check": _ok("gemini", "fact_check")}
        assert pipeline._domains_with_nothing_usable(raw) == []
        assert pipeline._domains_with_nothing_usable(
            raw, {"fact_check", "voice_style"}
        ) == ["voice_style"]

    def test_a_covered_domain_is_not_flagged(self):
        raw = {"openai:voice_style": _ok("openai", "voice_style")}
        assert pipeline._domains_with_nothing_usable(raw, {"voice_style"}) == []

    def test_a_failed_domain_is_still_flagged(self):
        raw = {"gemini:fact_check": _failed("gemini", "fact_check")}
        assert pipeline._domains_with_nothing_usable(raw, {"fact_check"}) == [
            "fact_check"
        ]

    def test_the_expected_set_does_not_have_to_be_given(self):
        """Callers that only care about attempted domains keep working."""
        raw = {"gemini:fact_check": _failed("gemini", "fact_check")}
        assert pipeline._domains_with_nothing_usable(raw) == ["fact_check"]


class TestCalibrationFiltersAreRespected:
    """`--only-domain fact_check` deliberately skips four domains.

    Treating the whole preset as expected would have made every skipped domain
    look empty and bought a substitute call for each -- defeating the flag and
    costing four model calls on a run whose entire purpose is to be narrow.
    """

    def test_only_domain_does_not_substitute_for_the_skipped_domains(self, tmp_path):
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        attempted = []

        def _stub(model_name, domain, *a, **kw):
            attempted.append(f"{model_name}:{domain}")
            return _fake_run_domain(model_name, domain, *a, **kw)

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch("ci_article_review.pipeline._run_domain", side_effect=_stub)
            ],
            only_domain="fact_check",
        ):
            pass

        assert attempted, "the filtered domain should still run"
        assert all(a.endswith(":fact_check") for a in attempted), (
            f"a skipped domain was run or substituted for: {attempted}"
        )

    def test_a_calibration_run_does_not_pay_for_the_seo_passes(self, tmp_path):
        from .test_pipeline_end_to_end import _stubbed_run

        with (
            patch("ci_article_review.analysis.seo_suggest.generate") as suggest,
            patch("ci_article_review.analysis.seo_content.review") as content,
        ):
            with _stubbed_run(tmp_path, only_domain="fact_check"):
                pass
        assert not suggest.called
        assert not content.called
