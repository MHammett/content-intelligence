"""Tests for the output-token ceiling sent to reasoning passes.

The ceiling was a flat 16,000 until 2026-09-18, and it truncated
claude:fact_check on both maximum runs of the honda-navigation article and
mistral:fact_check on four of its last seven runs — on drafts as small as 2,182
characters. The overflow was reasoning, not findings. These tests pin the
formula to that evidence.
"""

import pytest

import ci_core.llm.output_tokens as ot
import ci_core.llm.timeout_model as tm

CEILING = 1100  # the shipped task_timeout_seconds

OPUS_HIGH = {"model": "claude-opus-5", "effort": "high"}
MISTRAL_HIGH = {"model": "mistral-medium-3-5", "reasoning_effort": "high"}


@pytest.fixture(autouse=True)
def _no_model_map(monkeypatch):
    """Keep litellm's model map out of every test that does not ask for it.

    Each test that cares about the model's own limit patches its own value in.
    """
    monkeypatch.setattr(ot, "model_output_limit", lambda provider, model: None)


def _size_mult(chars):
    return tm._size_mult(chars, tm._CONFIG["size_multipliers"])


class TestFormula:
    def test_reasoning_plus_answer_scaled_by_the_timeout_size_table(self):
        cfg = ot._CONFIG
        for chars in (2182, 18167, 27113, 62000, 135514, 400000):
            expected = cfg["reasoning_tokens"]["high"] + cfg["answer_tokens"][
                "fact_check"
            ] * _size_mult(chars)
            got = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", chars)
            assert got == pytest.approx(expected, abs=1), chars

    def test_the_size_table_is_the_timeout_models_own(self):
        """Not a copy that could drift: change the timeout buckets and the
        ceiling follows."""
        before = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 62000)
        buckets = tm._CONFIG["size_multipliers"]
        original = [dict(b) for b in buckets]
        try:
            for b in buckets:
                b["mult"] = float(b["mult"]) * 2
            after = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 62000)
        finally:
            buckets[:] = original
        answer = ot._CONFIG["answer_tokens"]["fact_check"]
        assert after - before == pytest.approx(answer * _size_mult(62000), abs=1)

    def test_fact_check_gets_more_room_than_a_flag_domain(self):
        """It writes one entry per claim; the other domains a handful of flags."""
        for chars in (2182, 27113, 135514):
            fact = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", chars)
            flags = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "red_team", chars)
            assert fact > flags, chars

    def test_an_unknown_domain_uses_the_default_answer(self):
        custom = ot.compute_max_tokens("claude", OPUS_HIGH, "my_custom_domain", 27113)
        flags = ot.compute_max_tokens("claude", OPUS_HIGH, "red_team", 27113)
        assert custom == flags

    def test_it_never_shrinks_below_the_ceiling_it_replaced(self):
        """16,000 was sent at every claude effort level. Nothing here may be
        lower, at any size, for any domain."""
        for effort in ("low", "medium", "high", "xhigh", "max"):
            for chars in (500, 2182, 27113, 135514):
                for domain in ("fact_check", "voice_style", "red_team"):
                    got = ot.compute_max_tokens(
                        "claude",
                        {"model": "claude-opus-5", "effort": effort},
                        domain,
                        chars,
                    )
                    assert got > 16000, (effort, chars, domain, got)


class TestTheRecordedTruncationsWouldHaveFit:
    """Every pass the flat ceiling cut off, re-sized by the formula.

    The recorded outputs are FLOORS — each call stopped at the ceiling — so
    fitting them is not the bar. The bar is the largest uncapped natural length
    on record for the pass that truncated most: mistral:fact_check, 30,740
    output tokens, 2026-08-11, before any ceiling existed.
    """

    LARGEST_UNCAPPED_MISTRAL_FACT_CHECK = 30740

    @pytest.mark.parametrize(
        "provider,cfg,domain,chars,recorded",
        [
            ("mistral", MISTRAL_HIGH, "argument_integrity", 27113, 16000),
            ("mistral", MISTRAL_HIGH, "fact_check", 2182, 16000),
            ("mistral", MISTRAL_HIGH, "fact_check", 4121, 16000),
            ("mistral", MISTRAL_HIGH, "fact_check", 27113, 16000),
            ("claude", OPUS_HIGH, "fact_check", 18167, 18475),
            ("claude", OPUS_HIGH, "fact_check", 27113, 20215),
        ],
    )
    def test_each_one_gets_room_well_past_where_it_stopped(
        self, provider, cfg, domain, chars, recorded
    ):
        got = ot.compute_max_tokens(provider, cfg, domain, chars)
        assert got >= 1.5 * 16000, got
        assert got > recorded, (got, recorded)

    @pytest.mark.parametrize("chars", [2182, 4121, 27113])
    def test_mistral_fact_check_covers_its_largest_uncapped_run(self, chars):
        """Reasoning did not shrink with the draft, so neither may the room for
        it: the two smallest drafts truncated just as reliably."""
        got = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", chars)
        assert got >= self.LARGEST_UNCAPPED_MISTRAL_FACT_CHECK, got


class TestWhoGetsACeiling:
    @pytest.mark.parametrize("provider", ["openai", "grok", "gemini", "perplexity"])
    def test_providers_that_send_none_are_left_alone(self, provider):
        cfg = {"model": "whatever", "reasoning_effort": "high", "effort": "high"}
        assert ot.compute_max_tokens(provider, cfg, "fact_check", 27113) is None

    def test_a_pass_with_no_reasoning_keeps_the_client_default(self):
        assert (
            ot.compute_max_tokens(
                "claude", {"model": "claude-haiku-4-5-20251001"}, "fact_check", 27113
            )
            is None
        )
        assert (
            ot.compute_max_tokens(
                "mistral", {"model": "mistral-small-latest"}, "fact_check", 27113
            )
            is None
        )

    def test_mistral_reasoning_effort_none_is_no_reasoning(self):
        """A real value mistral-medium-3-5 accepts, meaning reasoning off."""
        cfg = {"model": "mistral-medium-3-5", "reasoning_effort": "none"}
        assert ot.compute_max_tokens("mistral", cfg, "fact_check", 27113) is None

    def test_each_provider_reads_the_key_its_request_is_built_from(self):
        """client._provider_params reads `effort` for claude and
        `reasoning_effort` for mistral; the wrong key sends no reasoning, so on a
        model that thinks only when asked it must not buy a reasoning-sized
        ceiling either. (claude-opus-5 thinks anyway — see
        TestAnUnsetEffortIsTheModelsDefault.)"""
        assert (
            ot.compute_max_tokens(
                "claude",
                {"model": "claude-opus-4-8", "reasoning_effort": "high"},
                "fact_check",
                27113,
            )
            is None
        )
        assert (
            ot.compute_max_tokens(
                "mistral",
                {"model": "mistral-medium-3-5", "effort": "high"},
                "fact_check",
                27113,
            )
            is None
        )


class TestAnUnsetEffortIsTheModelsDefault:
    """claude-opus-5 and claude-sonnet-5 think when the request names no effort.

    Anthropic lists thinking as "On" for both when `thinking` is omitted, and an
    omitted effort as identical to `high`; litellm sends neither for a config
    with no effort (captured on the wire in test_llm_client.py). So a bare
    `claude: model: claude-opus-5` — configs/user.yaml's own base entry, and the
    shipped user.example.yaml's — runs exactly as `effort: high` does, and was
    sent 8000 tokens for its thinking and its answer together.
    """

    THINK_BY_DEFAULT = ["claude-opus-5", "claude-sonnet-5"]
    # Anthropic's table: thinking "Off" unless the request asks for it.
    THINK_WHEN_ASKED = [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    @pytest.mark.parametrize("model", THINK_BY_DEFAULT)
    def test_sized_exactly_as_effort_high(self, model):
        for chars in (2182, 27113, 135514):
            for domain in ("fact_check", "red_team"):
                unset = ot.compute_max_tokens("claude", {"model": model}, domain, chars)
                high = ot.compute_max_tokens(
                    "claude", {"model": model, "effort": "high"}, domain, chars
                )
                assert unset == high, (model, chars, domain)

    @pytest.mark.parametrize("model", THINK_WHEN_ASKED)
    def test_a_model_that_thinks_only_when_asked_keeps_the_client_default(self, model):
        assert ot.effort_of("claude", {"model": model}) is None
        assert (
            ot.compute_max_tokens("claude", {"model": model}, "fact_check", 27113)
            is None
        )

    def test_an_explicit_effort_still_wins(self):
        low = {"model": "claude-opus-5", "effort": "low"}
        assert ot.effort_of("claude", low) == "low"
        assert ot.compute_max_tokens(
            "claude", low, "fact_check", 27113
        ) < ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 27113)

    def test_the_wrong_key_is_ignored_here_as_it_is_in_the_request(self):
        """Nothing reaches the provider from `reasoning_effort` on claude, so
        claude-opus-5 runs at its default whatever the stray key says."""
        stray = {"model": "claude-opus-5", "reasoning_effort": "low"}
        assert ot.effort_of("claude", stray) == "high"

    def test_a_claude_none_is_unset(self):
        """litellm drops a claude reasoning_effort of "none" before sending, so
        the model runs at its default — also captured in test_llm_client.py."""
        assert ot.effort_of("claude", {"model": "claude-opus-5", "effort": "none"}) == (
            "high"
        )

    def test_mistral_none_still_means_no_reasoning(self):
        """mistral's "none" is sent and honoured; the list must not reach it."""
        cfg = {"model": "mistral-medium-3-5", "reasoning_effort": "none"}
        assert ot.effort_of("mistral", cfg) is None
        assert ot.effort_when_unset("mistral", "claude-opus-5") is None

    def test_a_pinned_route_is_the_model_it_routes_to(self):
        assert ot.effort_when_unset("claude", "anthropic/claude-opus-5") == "high"
        assert ot.effort_when_unset("claude", None) is None

    def test_the_wall_clock_formula_uses_high_too(self, monkeypatch):
        """The effort multiplier on its own. Time-to-fill the ceiling is taken
        out, because at high it outweighs the formula and would hide it."""
        monkeypatch.setattr(ot, "seconds_to_fill", lambda *a, **k: None)
        unset = tm.compute_budget(27113, "claude", {"model": "claude-opus-5"}, CEILING)
        assert unset == tm.compute_timeout(27113, "claude-opus-5", "high", CEILING)
        assert unset > tm.compute_timeout(27113, "claude-opus-5", None, CEILING)

    @pytest.mark.parametrize("model", THINK_BY_DEFAULT)
    def test_the_whole_budget_matches_effort_high(self, model):
        for chars in (2182, 27113, 135514):
            assert tm.compute_budget(
                chars, "claude", {"model": model}, CEILING
            ) == tm.compute_budget(
                chars, "claude", {"model": model, "effort": "high"}, CEILING
            ), (model, chars)

    def test_litellms_model_map_cannot_tell_the_two_kinds_apart(self):
        """Why this is a list and not a map lookup: claude-opus-5 thinks by
        default and claude-opus-4-8 does not, yet the bundled map gives them the
        same thinking flags. If this fails, litellm has learned the difference,
        and _EFFORT_WHEN_UNSET can be keyed off its map instead."""
        from ci_core.llm import client

        cost_map = client._litellm().model_cost

        def _thinking_flags(model):
            return {k: v for k, v in cost_map[model].items() if "thinking" in k}

        assert _thinking_flags("claude-opus-5") == _thinking_flags("claude-opus-4-8")


class TestTheModelsOwnLimit:
    def test_a_ceiling_above_the_models_limit_is_clamped(self, monkeypatch):
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 20000)
        assert ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 27113) == 20000

    def test_an_unknown_limit_leaves_the_ceiling_as_computed(self):
        got = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 27113)
        assert got > 20000

    def test_lookup_goes_through_litellms_model_map(self, monkeypatch):
        """claude-opus-5 accepts up to 128,000 output tokens (Anthropic docs,
        and litellm's map agrees). Real lookup, with the fixture's stub
        removed."""
        monkeypatch.undo()
        ot.model_output_limit.cache_clear()
        assert ot.model_output_limit("claude", "claude-opus-5") == 128000

    def test_a_model_the_map_does_not_know_is_none(self, monkeypatch):
        monkeypatch.undo()
        ot.model_output_limit.cache_clear()
        assert ot.model_output_limit("claude", "claude-not-a-real-model-x") is None
        assert ot.model_output_limit("claude", None) is None


class TestCalibrationCeiling:
    """--no-timeout exists to measure a call's real length. A ceiling that cuts
    the call off records the ceiling instead."""

    def test_lifted_to_the_models_limit(self, monkeypatch):
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 64000)
        assert ot.calibration_ceiling("claude", {"model": "claude-haiku"}) == 64000

    def test_capped_below_mistrals_whole_context_window(self, monkeypatch):
        """Mistral advertises 262,144 — its context window, which the prompt
        shares. Sending that 400s."""
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 262144)
        got = ot.calibration_ceiling("mistral", MISTRAL_HIGH)
        assert got == ot.CALIBRATION_MAX_TOKENS < 262144

    def test_an_unknown_limit_is_not_guessed(self):
        """Guessing high on a model whose limit is unknown turns the
        measurement into a 400."""
        assert ot.calibration_ceiling("claude", OPUS_HIGH) is None

    def test_providers_that_send_none_get_none(self, monkeypatch):
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 128000)
        assert ot.calibration_ceiling("openai", {"model": "gpt-5.6-sol"}) is None


class TestWallClockCoversTheCeiling:
    """Raising the ceiling without raising the wall clock converts truncations
    — which keep every complete finding — into timeouts, which keep none.

    Measured case: claude:fact_check on a 27,113-char draft ran 391.39s of its
    446s budget and was still cut off at 16,000.
    """

    def test_budget_is_raised_to_reach_the_ceiling(self):
        need = ot.seconds_to_fill("claude", OPUS_HIGH, 27113)
        formula = tm.compute_timeout(27113, "claude-opus-5", "high", CEILING)
        budget = tm.compute_budget(27113, "claude", OPUS_HIGH, CEILING)
        assert need > formula, "the case this exists for"
        assert budget >= need
        assert budget > 391.39, "the measured call that was still running"

    def test_the_budget_covers_the_largest_ceiling_not_just_this_domains(self):
        """The budget is per model; fact_check is the domain with most room."""
        largest = ot.largest_ceiling("claude", OPUS_HIGH, 27113)
        assert largest == ot.compute_max_tokens(
            "claude", OPUS_HIGH, "fact_check", 27113
        )
        rate = ot._floor_rate("claude-opus-5", ot._CONFIG["floor_tokens_per_second"])
        assert ot.seconds_to_fill("claude", OPUS_HIGH, 27113) == pytest.approx(
            largest / rate
        )

    @pytest.mark.parametrize(
        "provider,cfg",
        [
            ("openai", {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"}),
            ("claude", {"model": "claude-haiku-4-5-20251001"}),
            ("mistral", {"model": "mistral-medium-3-5", "reasoning_effort": "none"}),
        ],
    )
    def test_a_model_with_no_computed_ceiling_keeps_the_formula(self, provider, cfg):
        formula = tm.compute_timeout(
            27113,
            cfg["model"],
            cfg.get("reasoning_effort") or cfg.get("effort"),
            CEILING,
        )
        assert tm.compute_budget(27113, provider, cfg, CEILING) == formula

    def test_still_clamped_to_the_task_ceiling(self):
        assert tm.compute_budget(27113, "claude", OPUS_HIGH, 300) == 300 - 15

    def test_compute_all_applies_it_and_an_explicit_override_still_wins(self):
        out = tm.compute_all(
            27113,
            {
                "claude": dict(OPUS_HIGH),
                "mistral": dict(MISTRAL_HIGH, timeout_seconds=240),
            },
            CEILING,
        )
        assert out["claude"] == tm.compute_budget(27113, "claude", OPUS_HIGH, CEILING)
        assert out["mistral"] == 240

    def test_an_override_that_cannot_reach_the_ceiling_is_flagged(self):
        """flag_stale_overrides compares against the budget the model would now
        get — which includes the time to reach its ceiling."""
        cfgs = {"claude": dict(OPUS_HIGH, timeout_seconds=240)}
        flagged = tm.flag_stale_overrides(27113, cfgs, CEILING)
        assert flagged and flagged[0][2] == tm.compute_budget(
            27113, "claude", OPUS_HIGH, CEILING
        )


class TestFloorRate:
    def test_longest_prefix_wins_and_unknown_falls_back(self):
        table = {"claude": 50, "claude-haiku": 60, "default": 40}
        assert ot._floor_rate("claude-haiku-4-5", table) == 60
        assert ot._floor_rate("claude-opus-5", table) == 50
        assert ot._floor_rate("anthropic/claude-opus-5", table) == 40
        assert ot._floor_rate(None, table) == 40
