"""Tests for analysis.cost."""

from ci_core.llm import cost
from ci_core.llm.cost import calculate, _price_for_model


class TestPriceForModel:
    def test_known_model_exact(self):
        in_p, out_p = _price_for_model("gpt-5.4")[:2]
        assert in_p == 2.50
        assert out_p == 15.00

    def test_prefix_match(self):
        # A versioned variant like gemini-2.5-flash-0520 should match gemini-2.5-flash
        in_p, out_p = _price_for_model("gemini-2.5-flash-0520")[:2]
        assert in_p == 0.30

    def test_unknown_model_returns_fallback(self):
        from ci_core.llm.cost import _UNKNOWN_PRICE

        result = _price_for_model("some-hypothetical-model-9999")
        assert result == _UNKNOWN_PRICE

    def test_none_returns_fallback(self):
        from ci_core.llm.cost import _UNKNOWN_PRICE

        assert _price_for_model(None) == _UNKNOWN_PRICE


class TestCalculate:
    def _log(self, model, prompt_tok, completion_tok, failed=False):
        return {
            "pass": "openai:fact_check",
            "model": model,
            "failed": failed,
            "tokens": {"prompt": prompt_tok, "completion": completion_tok},
            "elapsed_seconds": 1.0,
        }

    def test_empty_log_zero_cost(self):
        result = calculate([])
        assert result["total_usd"] == 0.0
        assert result["by_pass"] == []

    def test_none_log_zero_cost(self):
        result = calculate(None)
        assert result["total_usd"] == 0.0

    def test_single_entry_cost(self):
        # gpt-5.4: $2.50/MTok in, $15.00/MTok out
        # 1000 prompt + 500 completion
        entry = self._log("gpt-5.4", 1000, 500)
        result = calculate([entry])
        expected_in = 1000 / 1_000_000 * 2.50
        expected_out = 500 / 1_000_000 * 15.00
        assert abs(result["total_usd"] - round(expected_in + expected_out, 4)) < 0.0001

    def test_a_failure_that_generated_nothing_costs_nothing(self):
        """The ordinary failure: a transport error or a timeout. The client
        reports zero tokens for these, so they cost nothing without needing a
        flag to say so."""
        entry = self._log("gpt-5.4", 0, 0, failed=True)
        assert calculate([entry])["total_usd"] == 0.0

    def test_a_failure_that_did_generate_output_is_still_billed(self):
        """The provider bills for tokens it generated, whatever the pipeline
        made of them.

        A response whose JSON will not parse is complete and charged for, and
        the client returns it with real token counts alongside `failed: True`.
        A `failed` short-circuit in the cost layer priced that at $0.00 — the
        same undercount as the discarded retry attempts, reached by a different
        route."""
        entry = self._log("gpt-5.4", 100_000, 50_000, failed=True)
        assert calculate([entry])["total_usd"] > 0

    def test_pricing_known_true_for_known_models(self):
        entry = self._log("gpt-5.4", 1000, 500)
        result = calculate([entry])
        assert result["pricing_known"] is True

    def test_pricing_known_false_for_unknown_model(self):
        entry = self._log("some-unknown-model-x", 1000, 500)
        result = calculate([entry])
        assert result["pricing_known"] is False

    def test_by_pass_has_all_keys(self):
        entry = self._log("claude-opus-4-8", 2000, 800)
        result = calculate([entry])
        row = result["by_pass"][0]
        for k in ("pass", "model", "input_usd", "output_usd", "total_usd"):
            assert k in row, f"missing key: {k}"

    def test_model_annotation_stripped(self):
        # Model strings from api_call_log may include "[FALLBACK from ...]"
        entry = self._log("gpt-5.4 [FALLBACK from gpt-5.5]", 1000, 500)
        result = calculate([entry])
        assert result["pricing_known"] is True

    def test_tolerates_calibration_fields(self):
        # api_call_log entries now carry calibration fields (effort, timeout budget,
        # headroom, char_count, status). Cost calculation must ignore the extras.
        entry = self._log("gpt-5.5", 18843, 17218)
        entry.update(
            {
                "effort": "xhigh",
                "timeout_budget_seconds": 819,
                "headroom_seconds": 478.9,
                "char_count": 73786,
                "status": "ok",
            }
        )
        result = calculate([entry])
        assert result["total_usd"] > 0
        assert result["pricing_known"] is True


class TestCachedTokensAreBilledAtTheCachedRate:
    """Cached input was billed at the full rate, so caching was invisible.

    Grok caches automatically and xAI prices cached input at $2.00 against
    $12.50 full — the summary was overstating the cached portion sixfold, which
    is also why none of the prompt-caching work could be measured.
    """

    def _entry(self, **tokens):
        return {"pass": "openai:fact_check", "model": "gpt-5.5", "tokens": tokens}

    def test_cached_tokens_cost_less_than_uncached(self):
        full = cost.calculate([self._entry(prompt=100_000, completion=0)])
        half = cost.calculate(
            [self._entry(prompt=100_000, cached=50_000, completion=0)]
        )
        assert half["total_input_usd"] < full["total_input_usd"]

    def test_the_split_is_priced_exactly(self):
        # gpt-5.5: $5.00 input, $0.50 cached. 50k uncached + 50k cached.
        got = cost.calculate([self._entry(prompt=100_000, cached=50_000, completion=0)])
        expected = (50_000 * 5.00 + 50_000 * 0.50) / 1_000_000
        assert round(got["total_input_usd"], 6) == round(expected, 6)

    def test_no_cached_tokens_prices_exactly_as_before(self):
        got = cost.calculate([self._entry(prompt=100_000, completion=0)])
        assert round(got["total_input_usd"], 6) == round(100_000 * 5.00 / 1_000_000, 6)

    def test_a_model_with_no_cached_rate_bills_cached_at_full_rate(self):
        """Conservative: over-report rather than invent a discount."""
        entry = {
            "pass": "x",
            "model": "sonar-reasoning-pro",
            "tokens": {"prompt": 10_000, "cached": 10_000, "completion": 0},
        }
        with_cache = cost.calculate([entry])["total_input_usd"]
        entry_plain = {
            "pass": "x",
            "model": "sonar-reasoning-pro",
            "tokens": {"prompt": 10_000, "completion": 0},
        }
        assert with_cache == cost.calculate([entry_plain])["total_input_usd"]

    def test_cached_cannot_exceed_prompt(self):
        """A provider reporting more cached than total must not go negative."""
        got = cost.calculate([self._entry(prompt=1_000, cached=9_999, completion=0)])
        assert got["total_input_usd"] > 0


class TestOpenAIReportsCachedTokensUnderTwoNames:
    """OpenAI names this field differently per API, and one name was missed.

    Chat Completions returns `prompt_tokens_details`; the Responses API returns
    `input_tokens_details`. The Responses API is the pipeline's primary OpenAI
    path, so reading only the Chat Completions name reported 0 cached tokens for
    every OpenAI call in every run — while a live check on 2026-08-15 showed the
    cache serving ~95% of the prompt. The blind measurement was very nearly
    taken as evidence that prompt_cache_layout did not work.

    Both shapes below are real payloads captured from the live API that day.
    """

    def test_responses_api_shape(self):
        from ci_core.llm.tokens import normalize_tokens

        usage = {
            "input_tokens": 19142,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 18176},
            "output_tokens": 28,
        }
        assert normalize_tokens(usage) == {
            "prompt": 19142,
            "completion": 28,
            "cached": 18176,
        }

    def test_chat_completions_shape_still_works(self):
        from ci_core.llm.tokens import normalize_tokens

        usage = {
            "prompt_tokens": 24239,
            "prompt_tokens_details": {"cached_tokens": 23296},
            "completion_tokens": 16,
        }
        assert normalize_tokens(usage)["cached"] == 23296

    def test_a_cold_call_reports_no_cached_key(self):
        from ci_core.llm.tokens import normalize_tokens

        usage = {
            "input_tokens": 19142,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens": 21,
        }
        assert "cached" not in normalize_tokens(usage)

    def test_the_cached_portion_is_billed_at_the_cached_rate(self):
        from ci_core.llm import cost

        entry = {
            "model": "gpt-5.5",
            "tokens": {"prompt": 19142, "cached": 18176, "completion": 28},
        }
        in_usd, _ = cost._entry_cost(entry)
        # 966 uncached @ $5.00/M + 18,176 cached @ $0.50/M
        expected = (966 * 5.00 + 18176 * 0.50) / 1_000_000
        assert abs(in_usd - expected) < 1e-9


class TestCachedTokensAreNotCountedTwice:
    """litellm and Anthropic disagree about what `prompt_tokens` includes.

    Anthropic's own API reports the *uncached remainder* and puts the rest in
    separate cache fields, so those have to be added back. litellm normalises to
    an *inclusive* `prompt_tokens` and leaves the cache fields in place, so
    adding them there counts the same tokens twice.

    This was invisible until prompt caching actually started working — every
    cache field was zero, so the addition was a no-op. The bug and the feature
    arrive together, which is why it is pinned here with real payloads rather
    than left to the next person to rediscover from a doubled bill.

    All four payloads below were captured from live calls on 2026-08-16.
    """

    def _n(self, usage):
        from ci_core.llm.tokens import normalize_tokens

        return normalize_tokens(usage)

    def test_litellm_cache_write_is_not_doubled(self):
        usage = {
            "completion_tokens": 4,
            "prompt_tokens": 5424,
            "prompt_tokens_details": {"cache_write_tokens": 5416, "cached_tokens": 0},
            "cache_creation_input_tokens": 5416,
        }
        assert self._n(usage)["prompt"] == 5424  # not 10840

    def test_litellm_cache_read_is_not_doubled(self):
        usage = {
            "completion_tokens": 4,
            "prompt_tokens": 5424,
            "prompt_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 5416},
            "cache_read_input_tokens": 5416,
        }
        out = self._n(usage)
        assert out["prompt"] == 5424  # not 10840
        assert out["cached"] == 5416  # not 10832 — two spellings of one number

    def test_the_raw_anthropic_shape_still_adds_its_cache_fields(self):
        """The case the addition exists for, and which must not regress.

        Here `input_tokens` really is only the uncached remainder: a
        4,800-token cached prompt arrives as 20.
        """
        usage = {
            "input_tokens": 20,
            "cache_read_input_tokens": 4800,
            "output_tokens": 100,
        }
        out = self._n(usage)
        assert out["prompt"] == 4820
        assert out["cached"] == 4800

    def test_openai_responses_shape_is_unaffected(self):
        usage = {
            "input_tokens": 19142,
            "input_tokens_details": {"cached_tokens": 18176},
            "output_tokens": 28,
        }
        out = self._n(usage)
        assert out["prompt"] == 19142
        assert out["cached"] == 18176

    def test_a_doubled_prompt_would_double_the_bill(self):
        """Why this matters beyond tidiness: cost is computed from these."""
        from ci_core.llm import cost

        honest = {
            "model": "claude-opus-4-8",
            "tokens": {"prompt": 5424, "cached": 5416},
        }
        doubled = {
            "model": "claude-opus-4-8",
            "tokens": {"prompt": 10840, "cached": 10832},
        }
        assert cost._entry_cost(doubled)[0] > cost._entry_cost(honest)[0] * 1.9


class TestDiscardedAttemptsAreBilled:
    """A retry throws away an attempt the provider already charged for.

    ``_with_retry`` replaced the failed attempt's result with the next one's, so
    those tokens left the accounting entirely. Seven attempts went unrecorded on
    2026-09-03 under a summary line reading "Estimated cost: $3.4878 (exact)".
    """

    def _entry(self, discarded=None):
        entry = {
            "pass": "openai:fact_check",
            "model": "gpt-5.4",
            "tokens": {"prompt": 1000, "completion": 1000},
        }
        if discarded is not None:
            entry["discarded_attempts"] = discarded
        return entry

    def test_a_discarded_attempt_with_usage_is_added_to_the_cost(self):
        without = calculate([self._entry()])
        with_discard = calculate(
            [
                self._entry(
                    {
                        "count": 1,
                        "costed": 1,
                        "reasons": ["MalformedJSONError"],
                        "tokens": {"prompt": 1000, "completion": 1000},
                    }
                )
            ]
        )
        # The discarded attempt used the same tokens again, so it doubles.
        assert with_discard["total_usd"] == round(without["total_usd"] * 2, 4)
        assert with_discard["discarded_calls"] == 1
        assert with_discard["uncosted_calls"] == 0

    def test_an_attempt_with_no_usage_is_counted_but_not_priced(self):
        summary = calculate(
            [
                self._entry(
                    {
                        "count": 1,
                        "costed": 0,
                        "reasons": ["StreamStalled"],
                        "tokens": {"prompt": 0, "completion": 0},
                    }
                )
            ]
        )
        assert summary["discarded_calls"] == 1
        # A stalled stream reports no usage, so the total is a floor and the
        # caller must stop describing it as exact.
        assert summary["uncosted_calls"] == 1
        assert summary["total_usd"] == calculate([self._entry()])["total_usd"]

    def test_no_retries_leaves_the_counters_at_zero(self):
        summary = calculate([self._entry()])
        assert summary["discarded_calls"] == 0
        assert summary["uncosted_calls"] == 0


class TestReplayedSpendIsSeparated:
    """A replay re-reports the captured run's tokens; they were not spent here."""

    def test_replayed_entries_do_not_count_as_incurred(self):
        summary = calculate(
            [
                {
                    "pass": "openai:fact_check",
                    "model": "gpt-5.4",
                    "tokens": {"prompt": 1000, "completion": 1000},
                    "replayed": True,
                },
                {
                    "pass": "seo_suggestions",
                    "model": "gpt-5.4",
                    "tokens": {"prompt": 100, "completion": 100},
                },
            ]
        )
        assert summary["incurred_usd"] > 0
        assert summary["replayed_usd"] > summary["incurred_usd"]
        # Each field rounds to 4dp independently, exactly as total_input_usd and
        # total_output_usd already do, so the split reconciles to within one
        # rounding unit of the total — not to the cent. The bound is 2e-4 rather
        # than 1e-4 because binary floats put 0.0193 - 0.0192 a hair above it.
        split = summary["replayed_usd"] + summary["incurred_usd"]
        assert abs(split - summary["total_usd"]) < 0.0002

    def test_a_fully_replayed_run_incurred_nothing(self):
        summary = calculate(
            [
                {
                    "pass": "openai:fact_check",
                    "model": "gpt-5.4",
                    "tokens": {"prompt": 1000, "completion": 1000},
                    "replayed": True,
                }
            ]
        )
        assert summary["incurred_usd"] == 0.0


class TestDiscardedSummaryShape:
    """`_summarise_discarded` is what the cost layer consumes, so its shape is
    part of the contract between the two."""

    def test_usage_is_recovered_where_the_attempt_carried_it(self):
        from ci_core.llm.client import _summarise_discarded

        summary = _summarise_discarded(
            [
                {
                    "reason": "MalformedJSONError",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 250},
                },
                {"reason": "StreamStalled", "usage": None},
            ]
        )
        assert summary["count"] == 2
        # Only the malformed-JSON attempt had usage to read.
        assert summary["costed"] == 1
        assert summary["tokens"]["completion"] == 250
        assert summary["reasons"] == ["MalformedJSONError", "StreamStalled"]

    def test_an_attempt_with_no_usage_contributes_no_tokens(self):
        from ci_core.llm.client import _summarise_discarded

        summary = _summarise_discarded([{"reason": "StreamStalled", "usage": None}])
        assert summary["count"] == 1
        assert summary["costed"] == 0
        assert summary["tokens"] == {"prompt": 0, "completion": 0}

    def test_a_failed_call_still_reports_what_it_discarded(self):
        """The most expensive outcome — two full attempts and no usable answer —
        was the one the cost summary could not see, because the field was
        attached only to the success path."""
        from ci_core.llm.client import _discarded_field

        assert _discarded_field([]) == {}
        field = _discarded_field([{"reason": "StreamStalled", "usage": None}])
        assert field["discarded_attempts"]["count"] == 1


class TestCallLogEntryBuilder:
    """One builder, so a new cost field cannot be forgotten by three callers.

    Citation verification and the two SEO passes each wrote this dict by hand,
    identical apart from the pass name. When `discarded_attempts` was added so
    retried attempts could be billed, all three kept dropping it — the retries
    were counted for the ensemble and silently free everywhere else.
    """

    def test_it_carries_the_standard_fields(self):
        entry = cost.call_log_entry(
            "seo_suggestions",
            {
                "model": "mistral-small-latest",
                "failed": False,
                "tokens": {"prompt": 10, "completion": 5},
                "elapsed_seconds": 1.2,
            },
        )
        assert entry["pass"] == "seo_suggestions"
        assert entry["model"] == "mistral-small-latest"
        assert entry["failed"] is False
        assert entry["error"] is None

    def test_it_carries_discarded_attempts(self):
        entry = cost.call_log_entry(
            "citation_verification:known_url",
            {
                "model": "mistral-small-latest",
                "tokens": {"prompt": 10, "completion": 5},
                "discarded_attempts": {
                    "count": 1,
                    "costed": 1,
                    "reasons": ["StreamStalled"],
                    "tokens": {"prompt": 10, "completion": 5},
                },
            },
        )
        assert entry["discarded_attempts"]["count"] == 1
        # And the cost layer then bills it.
        assert cost.calculate([entry])["discarded_calls"] == 1

    def test_the_field_is_absent_when_nothing_was_discarded(self):
        entry = cost.call_log_entry("x", {"model": "gpt-5.4", "tokens": {}})
        assert "discarded_attempts" not in entry

    def test_the_default_model_is_used_when_the_result_has_none(self):
        entry = cost.call_log_entry("x", {"tokens": {}}, "fallback-model")
        assert entry["model"] == "fallback-model"

    def test_an_error_is_only_reported_for_a_failed_call(self):
        ok = cost.call_log_entry("x", {"failed": False, "error": "ignored"})
        assert ok["error"] is None
        bad = cost.call_log_entry("x", {"failed": True, "error": "HTTP 503"})
        assert bad["error"] == "HTTP 503"
