"""Tests for the output-token ceiling sent to reasoning passes.

The ceiling was a flat 16,000 until 2026-09-18, and it truncated
claude:fact_check on both maximum runs of the honda-navigation article and
mistral:fact_check on four of its last seven runs — on drafts as small as 2,182
characters. The overflow was reasoning, not findings. These tests pin the
formula to that evidence.

Two more findings reshaped it on 2026-10-24. The answer term stopped growing at
the size table's 1.8x while fact_check's output did not: on a 190,170-char
draft at `maximum`, mistral:fact_check wrote 154 `confirmed` entries (109,656
chars of JSON) and hit its 50,000 ceiling before any other bucket, so the
answer term is now also a line through the origin (0.25 tokens per character)
and the larger of the two wins. And a no-effort pass kept client.py's flat
8000, which cut claude-haiku-4-5:argument_integrity off at exactly 8000 tokens
twice at `wide` (2026-09-28, 2026-10-04; 38,021 chars of flags written), so
every capped pass is sized here now, with the reasoning term 0 when nothing
reasons.
"""

import pytest
import yaml

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
    monkeypatch.setattr(ot, "model_context_limit", lambda provider, model: None)


def _size_mult(chars):
    return tm._size_mult(chars, tm._CONFIG["size_multipliers"])


def _answer(domain, chars, table=None):
    """The answer term as the yaml defines it: table, line, floor; largest wins."""
    cfg = table or ot._CONFIG
    scaled = cfg["answer_tokens"].get(domain, cfg["answer_tokens"]["default"])
    rate = cfg.get("answer_tokens_per_char", {}).get(domain, 0)
    return max(scaled * _size_mult(chars), rate * chars, cfg["answer_floor_tokens"])


def _no_effort_ceiling(domain, chars):
    import math

    return math.ceil(_answer(domain, chars))


class TestFormula:
    def test_reasoning_plus_answer_scaled_by_the_timeout_size_table(self):
        cfg = ot._CONFIG
        for chars in (2182, 18167, 27113, 62000, 135514, 400000):
            expected = cfg["reasoning_tokens"]["high"] + _answer("fact_check", chars)
            got = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", chars)
            assert got == pytest.approx(expected, abs=1), chars

    def test_the_table_decides_below_the_crossover(self):
        """Up to the size where the per-character line overtakes it, the answer
        term is the table's figure exactly; nothing under that size moved when
        the line was added."""
        cfg = ot._CONFIG
        for chars in (6000, 18167, 27113, 62000):
            expected = cfg["reasoning_tokens"]["high"] + cfg["answer_tokens"][
                "fact_check"
            ] * _size_mult(chars)
            got = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", chars)
            assert got == pytest.approx(expected, abs=1), chars

    def test_the_size_table_is_the_timeout_models_own(self):
        """Not a copy that could drift: change the timeout buckets and the
        ceiling follows. At 27,113 chars the table is the deciding term."""
        before = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 27113)
        buckets = tm._CONFIG["size_multipliers"]
        original = [dict(b) for b in buckets]
        try:
            for b in buckets:
                b["mult"] = float(b["mult"]) * 2
            after = ot.compute_max_tokens("claude", OPUS_HIGH, "fact_check", 27113)
        finally:
            buckets[:] = original
        answer = ot._CONFIG["answer_tokens"]["fact_check"]
        assert after - before == pytest.approx(answer * _size_mult(27113), abs=1)

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

    def test_a_pass_with_no_reasoning_gets_the_answer_term_alone(self):
        """No reasoning, no reasoning term — but a ceiling, sized for the answer.
        Until 2026-10-24 these returned None and the client sent a flat 8000."""
        expected = _no_effort_ceiling("fact_check", 27113)
        assert expected > 8000
        assert (
            ot.compute_max_tokens(
                "claude", {"model": "claude-haiku-4-5-20251001"}, "fact_check", 27113
            )
            == expected
        )
        assert (
            ot.compute_max_tokens(
                "mistral", {"model": "mistral-small-latest"}, "fact_check", 27113
            )
            == expected
        )
        assert expected < ot.compute_max_tokens(
            "mistral", MISTRAL_HIGH, "fact_check", 27113
        )

    def test_mistral_reasoning_effort_none_is_no_reasoning(self):
        """A real value mistral-medium-3-5 accepts, meaning reasoning off."""
        cfg = {"model": "mistral-medium-3-5", "reasoning_effort": "none"}
        assert ot.compute_max_tokens(
            "mistral", cfg, "fact_check", 27113
        ) == _no_effort_ceiling("fact_check", 27113)

    def test_each_provider_reads_the_key_its_request_is_built_from(self):
        """client._provider_params reads `effort` for claude and
        `reasoning_effort` for mistral; the wrong key sends no reasoning, so on a
        model that thinks only when asked it must not buy a reasoning-sized
        ceiling either. (claude-opus-5 thinks anyway — see
        TestAnUnsetEffortIsTheModelsDefault.)"""
        no_reasoning = _no_effort_ceiling("fact_check", 27113)
        assert (
            ot.compute_max_tokens(
                "claude",
                {"model": "claude-opus-4-8", "reasoning_effort": "high"},
                "fact_check",
                27113,
            )
            == no_reasoning
        )
        assert (
            ot.compute_max_tokens(
                "mistral",
                {"model": "mistral-medium-3-5", "effort": "high"},
                "fact_check",
                27113,
            )
            == no_reasoning
        )


class TestTheAnswerGrowsWithTheDraft:
    """fact_check writes one entry per claim, and claims grow with the draft
    past where the size table stops growing.

    Measured 2026-10-23, `maximum`, run 24 of a 169,940-char article (190,170
    chars as sent): mistral:fact_check wrote 154 `confirmed` entries, 109,656
    chars of JSON, and hit its 50,000 ceiling before `outdated`, `contradicted`,
    `unverifiable`, `primary_source_needed`, `out_of_scope` or
    `additional_observations`. The table gave it 32,000 + 10,000 x 1.8.
    """

    RUN_24_CHARS = 190170
    RUN_24_CEILING = 50000

    def test_run_24s_pass_would_have_had_room(self):
        got = ot.compute_max_tokens(
            "mistral", MISTRAL_HIGH, "fact_check", self.RUN_24_CHARS
        )
        # ~27,400 tokens written for 154 entries while still in the first bucket;
        # finishing all seven at that rate is ~44,500 of answer, plus reasoning.
        assert got >= 32000 + 44500, got
        assert got > self.RUN_24_CEILING

    def test_the_line_is_per_character_through_the_origin(self):
        rate = ot._CONFIG["answer_tokens_per_char"]["fact_check"]
        for chars in (150000, 190170, 300000):
            got = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", chars)
            assert got == pytest.approx(32000 + rate * chars, abs=1), chars

    def test_the_line_only_ever_lifts(self):
        """Whichever term is larger wins, so adding the line lowered nothing."""
        for chars in (500, 2182, 27113, 62000, 135514, 190170):
            for domain in ("fact_check", "red_team"):
                got = ot.compute_max_tokens("mistral", MISTRAL_HIGH, domain, chars)
                table_only = 32000 + max(
                    ot._CONFIG["answer_tokens"].get(
                        domain, ot._CONFIG["answer_tokens"]["default"]
                    )
                    * _size_mult(chars),
                    ot._CONFIG["answer_floor_tokens"],
                )
                assert got >= table_only - 1, (domain, chars)

    def test_only_fact_check_has_a_line(self):
        """The flag domains return a handful of findings whatever the claim
        count; they keep the table."""
        for chars in (190170, 400000):
            flags = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "red_team", chars)
            assert flags == 32000 + ot._CONFIG["answer_tokens"]["default"] * _size_mult(
                chars
            )

    def test_fact_check_stays_the_largest_domain_at_every_size(self):
        """The wall-clock budget sizes off the largest ceiling and assumes it is
        fact_check's; the anchors are chosen so that stays true."""
        for chars in (500, 2182, 9456, 27113, 62000, 135514, 190170):
            for cfg, provider in (
                (MISTRAL_HIGH, "mistral"),
                ({"model": "m"}, "mistral"),
            ):
                fact = ot.compute_max_tokens(provider, cfg, "fact_check", chars)
                for domain in ("red_team", "voice_style", "argument_integrity", "x"):
                    assert fact >= ot.compute_max_tokens(provider, cfg, domain, chars)


class TestNoEffortPassesAreCapped:
    """claude-haiku-4-5:argument_integrity was cut off at exactly 8000 tokens,
    the client's no-effort default, on 2026-09-28 and 2026-10-04 — both `wide`
    runs of the 9,456-char smoke-test draft, 38,021 chars of flags written and
    the rest lost. The flat default never saw the domain or the draft."""

    HAIKU = {"model": "claude-haiku-4-5-20251001"}
    TRUNCATED_AT = 8000
    SMOKE_TEST_CHARS = 9456

    def test_the_recorded_truncations_get_room(self):
        got = ot.compute_max_tokens(
            "claude", self.HAIKU, "argument_integrity", self.SMOKE_TEST_CHARS
        )
        # ~9,500 tokens were written before the cut (38,021 chars / 4), censored.
        assert got >= 12000, got
        assert got > self.TRUNCATED_AT

    def test_never_below_the_default_it_replaces(self):
        for chars in (100, 2182, 9456, 27113):
            for domain in ("fact_check", "red_team", "argument_integrity"):
                got = ot.compute_max_tokens("claude", self.HAIKU, domain, chars)
                assert got >= ot._CONFIG["answer_floor_tokens"] == 8000, (domain, chars)

    def test_sized_from_the_domain_and_the_draft_like_any_pass(self):
        small = ot.compute_max_tokens(
            "mistral", {"model": "mistral-small-latest"}, "fact_check", 2182
        )
        large = ot.compute_max_tokens(
            "mistral", {"model": "mistral-small-latest"}, "fact_check", 190170
        )
        assert large > small
        assert large >= 0.25 * 190170, "the per-character line applies here too"

    def test_a_thinking_budget_stands_in_for_the_reasoning_term(self):
        """The client sends the budget in place of any effort, and the thinking
        spends from the same ceiling, so the budget is added as the table's
        figure would be."""
        budget = 16000
        with_budget = ot.compute_max_tokens(
            "claude", dict(self.HAIKU, thinking_budget=budget), "red_team", 27113
        )
        without = ot.compute_max_tokens("claude", self.HAIKU, "red_team", 27113)
        assert with_budget == without + budget
        assert with_budget > budget + 4096, "the client's own fallback for a budget"

    def test_a_mistral_thinking_budget_is_not_a_thing(self):
        """Only claude's request reads a budget; mistral's does not, so one in a
        mistral config buys nothing here either."""
        cfg = {"model": "mistral-small-latest", "thinking_budget": 16000}
        assert ot.compute_max_tokens(
            "mistral", cfg, "red_team", 27113
        ) == ot.compute_max_tokens(
            "mistral", {"model": "mistral-small-latest"}, "red_team", 27113
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
    def test_a_model_that_thinks_only_when_asked_gets_no_reasoning_term(self, model):
        assert ot.effort_of("claude", {"model": model}) is None
        got = ot.compute_max_tokens("claude", {"model": model}, "fact_check", 27113)
        assert got == _no_effort_ceiling("fact_check", 27113)
        assert got < ot.compute_max_tokens(
            "claude", {"model": model, "effort": "high"}, "fact_check", 27113
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


class TestEffortNoneThatStillThinksIsWarned:
    """`effort: none` on claude-opus-5 is unset, not off, so it is said out loud.

    Whoever writes it wants no thinking, usually to save money. litellm drops the
    value, and the model thinks at its default, high, and bills for it. Nothing
    fails, so nothing else would say so. What reaches the wire for each spelling
    is pinned in test_llm_client.py; these cover who is warned, and with what.
    """

    THINK_BY_DEFAULT = sorted(ot._EFFORT_WHEN_UNSET["claude"])
    THINK_WHEN_ASKED = TestAnUnsetEffortIsTheModelsDefault.THINK_WHEN_ASKED

    @staticmethod
    def _warned(model=None, **cfg):
        if model is not None:
            cfg["model"] = model
        return ot.effort_none_warnings({"claude": cfg})

    @pytest.mark.parametrize("model", THINK_BY_DEFAULT)
    def test_names_the_model_what_it_runs_at_and_what_to_set(self, model):
        (warning,) = self._warned(model, effort="none")
        default = ot.effort_when_unset("claude", model)
        assert f"claude model {model} is set to effort: none" in warning
        assert f"thinks at its default, {default}" in warning
        assert "effort: low or medium" in warning

    @pytest.mark.parametrize("model", THINK_WHEN_ASKED)
    def test_a_model_that_thinks_only_when_asked_is_not_warned(self, model):
        """litellm drops the none here too, and these models then do not think,
        which is what was asked for."""
        assert self._warned(model, effort="none") == []

    @pytest.mark.parametrize("written", ["off", "no", "false", "False"])
    def test_yaml_false_is_warned_the_same_way(self, written):
        """YAML reads all four as false. A false effort is not sent at all, so
        the request is the same as a none that litellm dropped."""
        cfg = yaml.safe_load(f"model: claude-opus-5\neffort: {written}")
        assert cfg["effort"] is False
        (warning,) = ot.effort_none_warnings({"claude": cfg})
        assert "effort: false (YAML reads off and no as false too)" in warning
        assert "thinks at its default, high" in warning

    @pytest.mark.parametrize("written", ["None", "NONE", " none "])
    def test_another_spelling_is_warned_that_every_call_fails(self, written):
        """litellm matches "none" exactly and rejects anything else before
        sending. Promising a high-effort run here would be wrong: no request
        is made at all."""
        (warning,) = self._warned("claude-opus-5", effort=written)
        assert f"effort: {written!r}" in warning
        assert "every claude call will fail" in warning
        assert "is billed" not in warning
        assert "effort: low or medium" in warning

    def test_unset_is_not_warned(self):
        """Unset is the API default, documented and sized for (see the class
        above). It is what an empty config means, not a request for less."""
        assert self._warned("claude-opus-5") == []
        assert self._warned("claude-opus-5", effort=None) == []  # YAML null

    @pytest.mark.parametrize("effort", ["", "low", "medium", "high", "xhigh", "max"])
    def test_other_efforts_are_not_warned(self, effort):
        assert self._warned("claude-opus-5", effort=effort) == []

    def test_a_disabled_claude_is_not_warned(self):
        assert self._warned("claude-opus-5", effort="none", enabled=False) == []

    def test_a_thinking_budget_decides_instead_and_is_not_warned(self):
        """The client sends the budget and never reads the effort, so "thinks
        at its default" would be false. (On these models the budget is its own
        failure: Anthropic rejects budget_tokens on Opus 5 and Sonnet 5.)"""
        from ci_core.llm import client

        cfg = {"effort": "none", "thinking_budget": 4096}
        assert self._warned("claude-opus-5", **cfg) == []
        params = client._provider_params("claude", {"model": "claude-opus-5", **cfg})
        assert params["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        assert "reasoning_effort" not in params

    def test_only_claude_is_judged(self):
        """mistral's and openai's none are real levels, sent and honoured."""
        configs = {
            "mistral": {"model": "mistral-medium-3-5", "reasoning_effort": "none"},
            "openai": {"model": "gpt-5.6-terra", "reasoning_effort": "none"},
        }
        assert ot.effort_none_warnings(configs) == []

    def test_a_pinned_route_is_the_model_it_routes_to(self):
        (warning,) = self._warned("anthropic/claude-opus-5", effort="none")
        assert "anthropic/claude-opus-5" in warning

    def test_no_model_is_judged_on_the_model_the_client_calls(self, monkeypatch):
        from ci_core.llm import client

        default = client._PROVIDERS["claude"]["default_model"]
        expected = 1 if ot.effort_when_unset("claude", default) else 0
        assert len(self._warned(effort="none")) == expected
        monkeypatch.setitem(
            client._PROVIDERS["claude"], "default_model", "claude-opus-5"
        )
        (warning,) = self._warned(effort="none")
        assert "claude-opus-5" in warning

    @pytest.mark.parametrize(
        "configs", [None, {}, [], "claude", {"claude": "claude-opus-5"}]
    )
    def test_a_config_it_cannot_read_is_not_an_error(self, configs):
        """ci-style-profile hands over user.yaml's models as written, where the
        string form and a missing section are both legal."""
        assert ot.effort_none_warnings(configs) == []


class TestEachProviderHasOneEffortKey:
    """The table config load, the call log, the timeout budget and the probe
    read. That it matches the request is pinned in test_llm_client.py, for every
    provider; these cover what is done with it."""

    @pytest.mark.parametrize(
        "provider,key",
        [
            ("claude", "effort"),
            ("openai", "reasoning_effort"),
            ("mistral", "reasoning_effort"),
            ("grok", "reasoning_effort"),
            ("perplexity", "reasoning_effort"),
        ],
    )
    def test_the_key_a_provider_reads(self, provider, key):
        assert ot.effort_key(provider) == key

    @pytest.mark.parametrize("provider", ["gemini", "nonsense", None])
    def test_a_provider_that_reads_none_has_none(self, provider):
        """gemini thinks by thinking_budget and is sent no effort."""
        assert ot.effort_key(provider) is None

    def test_effort_of_reads_only_that_key(self):
        assert ot.effort_of("openai", {"reasoning_effort": "High"}) == "high"
        assert ot.effort_of("openai", {"effort": "high"}) is None
        assert ot.effort_of("gemini", {"reasoning_effort": "high"}) is None

    def test_reading_openais_key_does_not_give_it_a_ceiling(self):
        """The table now names every provider, but only claude and mistral send
        a ceiling (CAPPED_PROVIDERS), and effort_of must not change that."""
        cfg = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
        assert ot.effort_of("openai", cfg) == "high"
        assert ot.compute_max_tokens("openai", cfg, "fact_check", 27113) is None


class TestAnEffortUnderTheWrongKeyIsWarned:
    """`reasoning_effort` under claude, or `effort` under everyone else.

    Claude reads `effort` and the others read `reasoning_effort`; the client
    drops the other spelling, so nothing reaches the request and the model runs
    as though no effort were set. On claude-opus-5 that is high. Nothing fails,
    so nothing else says so. That the request drops it is pinned in
    test_llm_client.py; these cover who is warned, and with what.
    """

    THINK_BY_DEFAULT = sorted(ot._EFFORT_WHEN_UNSET["claude"])
    THINK_WHEN_ASKED = TestAnUnsetEffortIsTheModelsDefault.THINK_WHEN_ASKED
    READS_REASONING_EFFORT = ["openai", "mistral", "grok", "perplexity"]

    @staticmethod
    def _warned(provider="claude", **cfg):
        return ot.effort_key_warnings({provider: cfg})

    @pytest.mark.parametrize("model", THINK_BY_DEFAULT)
    def test_claude_names_the_model_the_key_and_what_it_costs(self, model):
        (warning,) = self._warned(model=model, reasoning_effort="low")
        default = ot.effort_when_unset("claude", model)
        assert f"claude model {model} sets reasoning_effort: 'low'" in warning
        assert "its effort key is effort" in warning
        assert f"{model} thinks at its default, {default}, and is billed" in warning
        assert "Rename it to effort." in warning

    @pytest.mark.parametrize("model", THINK_WHEN_ASKED)
    def test_a_model_that_thinks_only_when_asked_is_not_said_to_bill_thinking(
        self, model
    ):
        (warning,) = self._warned(model=model, reasoning_effort="low")
        assert f"{model} runs at its default." in warning
        assert "billed" not in warning

    @pytest.mark.parametrize("provider", READS_REASONING_EFFORT)
    def test_the_other_providers_are_warned_the_other_way_round(self, provider):
        (warning,) = self._warned(provider, model="some-model", effort="high")
        assert f"{provider} model some-model sets effort: 'high'" in warning
        assert "its effort key is reasoning_effort" in warning
        assert "some-model runs at its default." in warning
        assert "Rename it to reasoning_effort." in warning

    @pytest.mark.parametrize("stray", ["effort", "reasoning_effort"])
    def test_gemini_reads_neither_spelling(self, stray):
        (warning,) = self._warned("gemini", model="gemini-2.5-pro", **{stray: "high"})
        assert f"gemini model gemini-2.5-pro sets {stray}: 'high'" in warning
        assert "it sends no effort, only thinking_budget" in warning

    @pytest.mark.parametrize("stray", ["effort", "reasoning_effort"])
    def test_a_gemini_3_model_is_pointed_at_thinking_level(self, stray):
        (warning,) = self._warned("gemini", model="gemini-3.5-flash", **{stray: "high"})
        assert f"gemini model gemini-3.5-flash sets {stray}: 'high'" in warning
        assert "it sends no effort, only thinking_level" in warning
        assert "minimal, low, medium or high" in warning

    def test_both_spellings_in_one_config_names_the_inert_one(self):
        """The ordinary way to meet it: a preset's `effort` beside a
        `reasoning_effort` left in user.yaml, or in preset_overrides."""
        (warning,) = self._warned(
            model="claude-opus-5", effort="high", reasoning_effort="low"
        )
        assert "sets both effort: 'high' and reasoning_effort: 'low'" in warning
        assert "uses only effort, so reasoning_effort does nothing" in warning

    def test_both_spellings_under_another_provider(self):
        (warning,) = self._warned(
            "openai", model="gpt-5.6-terra", reasoning_effort="low", effort="high"
        )
        assert "sets both reasoning_effort: 'low' and effort: 'high'" in warning
        assert "uses only reasoning_effort, so effort does nothing" in warning

    def test_each_stray_key_is_its_own_warning(self):
        assert len(self._warned("gemini", effort="low", reasoning_effort="high")) == 2

    @pytest.mark.parametrize(
        "provider,cfg",
        [
            ("claude", {"model": "claude-opus-5", "effort": "low"}),
            ("claude", {"model": "claude-opus-5"}),
            ("openai", {"model": "gpt-5.6-terra", "reasoning_effort": "low"}),
            ("mistral", {"model": "mistral-medium-3-5", "reasoning_effort": "none"}),
            ("gemini", {"model": "gemini-2.5-pro", "thinking_budget": 8192}),
            # Asking for nothing under the wrong key is not asking for anything.
            ("claude", {"model": "claude-opus-5", "reasoning_effort": None}),
            ("claude", {"model": "claude-opus-5", "reasoning_effort": ""}),
            ("claude", {"model": "claude-opus-5", "reasoning_effort": False}),
            ("openai", {"model": "gpt-5.6-terra", "effort": None}),
            ("gemini", {"model": "gemini-2.5-pro", "reasoning_effort": None}),
        ],
    )
    def test_a_config_that_runs_as_written_is_quiet(self, provider, cfg):
        assert ot.effort_key_warnings({provider: cfg}) == []

    def test_a_disabled_provider_is_not_warned(self):
        assert self._warned(model="claude-opus-5", reasoning_effort="low") != []
        assert (
            self._warned(model="claude-opus-5", reasoning_effort="low", enabled=False)
            == []
        )

    def test_a_thinking_budget_decides_instead_and_is_not_warned(self):
        """The client sends the budget for claude and reads no effort key, so
        "No effort is sent, so it thinks at its default" would be false."""
        cfg = {"model": "claude-opus-5", "reasoning_effort": "low"}
        assert (
            ot.effort_key_warnings({"claude": {**cfg, "thinking_budget": 4096}}) == []
        )

    def test_a_pinned_route_is_the_model_it_routes_to(self):
        (warning,) = self._warned(
            model="anthropic/claude-opus-5", reasoning_effort="low"
        )
        assert "claude model anthropic/claude-opus-5 sets" in warning
        assert "anthropic/claude-opus-5 thinks at its default, high" in warning

    def test_no_model_is_judged_on_the_model_the_client_calls(self, monkeypatch):
        from ci_core.llm import client

        monkeypatch.setitem(
            client._PROVIDERS["claude"], "default_model", "claude-opus-5"
        )
        (warning,) = self._warned(reasoning_effort="low")
        assert "claude model claude-opus-5 sets" in warning
        assert "thinks at its default, high" in warning

    def test_only_the_named_providers_are_judged(self):
        assert ot.effort_key_warnings({"other": {"effort": "high"}}) == []

    def test_providers_are_walked_in_a_fixed_order(self):
        configs = {
            "gemini": {"reasoning_effort": "low"},
            "openai": {"effort": "low"},
            "claude": {"reasoning_effort": "low"},
        }
        got = [w.split(" model ")[0] for w in ot.effort_key_warnings(configs)]
        assert got == ["claude", "openai", "gemini"]

    @pytest.mark.parametrize(
        "configs", [None, {}, [], "claude", {"claude": "claude-opus-5"}]
    )
    def test_a_config_it_cannot_read_is_not_an_error(self, configs):
        assert ot.effort_key_warnings(configs) == []


class TestASpellingLitellmRejectsIsWarned:
    """`High`, `Medium`, `HIGH` and a padded `" high "` on claude.

    litellm matches the levels exactly, in lowercase, and rejects anything else
    before a request is built, on every claude model. What reaches the wire for
    each spelling is pinned in test_llm_client.py; these cover who is warned,
    and with what. Only claude is judged: every other provider is forwarded the
    value as written (pinned there too), and whether its server rejects `High`
    is not measured.
    """

    THINK_BY_DEFAULT = sorted(ot._EFFORT_WHEN_UNSET["claude"])
    THINK_WHEN_ASKED = TestAnUnsetEffortIsTheModelsDefault.THINK_WHEN_ASKED
    # The warning names the model and nothing else about it, bar the none case
    # below; one of each kind covers it.
    ONE_THAT_THINKS_BY_DEFAULT = ["claude-opus-5", "claude-sonnet-5"]
    ONE_THAT_THINKS_WHEN_ASKED = ["claude-opus-4-8", "claude-haiku-4-5-20251001"]

    @staticmethod
    def _warned(model="claude-opus-5", **cfg):
        cfg["model"] = model
        return ot.effort_spelling_warnings({"claude": cfg})

    @pytest.mark.parametrize(
        "written,fixed",
        [
            ("High", "high"),
            ("HIGH", "high"),
            ("Medium", "medium"),
            (" high", "high"),
            ("high ", "high"),
            ("XHigh", "xhigh"),
            ("Low", "low"),
        ],
    )
    def test_names_the_value_and_says_what_to_write(self, written, fixed):
        (warning,) = self._warned(effort=written)
        assert f"claude model claude-opus-5 is set to effort: {written!r}" in warning
        assert "every claude call will fail" in warning
        assert f"Write effort: {fixed}." in warning
        # A refusal, not a bill: nothing is sent, so nothing is spent.
        assert "billed" not in warning

    @pytest.mark.parametrize("model", THINK_BY_DEFAULT + THINK_WHEN_ASKED)
    def test_it_names_whichever_model_it_is(self, model):
        (warning,) = self._warned(model, effort="High")
        assert f"claude model {model} is set to effort: 'High'" in warning

    @pytest.mark.parametrize("written", ["None", "NONE", " none "])
    @pytest.mark.parametrize("model", ONE_THAT_THINKS_WHEN_ASKED)
    def test_a_none_on_a_model_that_thinks_only_when_asked(self, model, written):
        """effort_none_warnings is silent here (there is no thinking to mention),
        and every call still fails."""
        (warning,) = self._warned(model, effort=written)
        assert f"is set to effort: {written!r}" in warning
        assert "every claude call will fail" in warning
        assert "Write effort: none." in warning

    @pytest.mark.parametrize("written", ["None", "NONE", " none "])
    @pytest.mark.parametrize("model", ONE_THAT_THINKS_BY_DEFAULT)
    def test_a_none_on_a_model_that_thinks_by_default_is_left_to_effort_none(
        self, model, written
    ):
        """effort_none_warnings already says it, and what it costs; two
        warnings for one value would read as two problems."""
        assert self._warned(model, effort=written) == []
        assert (
            len(
                ot.effort_none_warnings({"claude": {"model": model, "effort": written}})
            )
            == 1
        )
        assert (
            len(ot.effort_warnings({"claude": {"model": model, "effort": written}}))
            == 1
        )

    def test_a_blank_string_has_no_level_to_write(self):
        (warning,) = self._warned(effort="  ")
        assert "Write the level in lowercase, with nothing around it" in warning
        assert "Write effort: ." not in warning

    @pytest.mark.parametrize(
        "effort", ["", "minimal", "low", "medium", "high", "xhigh", "max", "none"]
    )
    def test_the_levels_in_lowercase_are_not_warned(self, effort):
        assert self._warned(effort=effort) == []

    @pytest.mark.parametrize("effort", [None, False, True, 5, ["high"]])
    def test_a_value_that_is_not_a_string_is_not_this_checks_business(self, effort):
        assert self._warned(effort=effort) == []

    def test_a_typo_is_not_a_spelling_problem(self):
        """It fails at call time too, but calling it a spelling of a real level
        would be a guess; naming every litellm level would mean keeping its list."""
        assert self._warned(effort="hgih") == []

    def test_a_disabled_claude_is_not_warned(self):
        assert self._warned(effort="High", enabled=False) == []

    def test_a_thinking_budget_decides_instead_and_is_not_warned(self):
        assert self._warned(effort="High", thinking_budget=4096) == []

    def test_only_claude_is_judged(self):
        """The others are forwarded the value as written; whether the provider
        rejects `High` is not measured here."""
        configs = {
            "openai": {"model": "gpt-5.6-terra", "reasoning_effort": "High"},
            "mistral": {"model": "mistral-medium-3-5", "reasoning_effort": "None"},
            "grok": {"model": "grok-4.6", "reasoning_effort": " high"},
        }
        assert ot.effort_spelling_warnings(configs) == []

    def test_a_pinned_route_is_the_model_it_routes_to(self):
        (warning,) = self._warned("anthropic/claude-opus-4-8", effort="High")
        assert "claude model anthropic/claude-opus-4-8 is set to" in warning

    @pytest.mark.parametrize(
        "configs", [None, {}, [], "claude", {"claude": "claude-opus-5"}]
    )
    def test_a_config_it_cannot_read_is_not_an_error(self, configs):
        assert ot.effort_spelling_warnings(configs) == []


class TestEveryEffortWarningComesThroughOneCall:
    """The call both loaders make, so a check added to it reaches both."""

    def test_it_is_every_check_in_order(self):
        from ci_core.llm import vertex

        configs = {
            "claude": {
                "model": "claude-opus-4-8",
                "effort": "High",
                "reasoning_effort": "low",
            },
            "openai": {"model": "gpt-5.6-terra", "effort": "high"},
            "gemini": {
                "provider": "vertex_ai",
                "model": "gemini-3.5-flash",
                "location": "us-central1",
                "thinking_budget": 16000,
            },
        }
        got = ot.effort_warnings(configs)
        assert got == (
            ot.effort_none_warnings(configs)
            + ot.effort_key_warnings(configs)
            + ot.effort_spelling_warnings(configs)
            + ot.gemini_thinking_warnings(configs)
            + vertex.location_warnings(configs)
        )
        assert [w.split(" model ")[0].split(" ")[0] for w in got] == [
            "claude",
            "openai",
            "claude",
            "gemini",
            "models.gemini.location",
        ]
        assert "sets both effort: 'High'" in got[0]
        assert "is set to effort: 'High'" in got[2]

    def test_a_config_that_runs_as_written_is_quiet(self):
        configs = {
            "claude": {"model": "claude-opus-5", "effort": "medium"},
            "openai": {"model": "gpt-5.6-terra", "reasoning_effort": "low"},
            "gemini": {"model": "gemini-2.5-pro", "thinking_budget": 8192},
        }
        assert ot.effort_warnings(configs) == []

    def test_a_gemini_3_config_that_runs_as_written_is_quiet(self):
        configs = {
            "gemini": {
                "provider": "vertex_ai",
                "model": "gemini-3.5-flash",
                "location": "us",
                "thinking_level": "high",
            },
        }
        assert ot.effort_warnings(configs) == []


class TestGeminiThinkingWarnings:
    """Gemini 2.5 thinks by a budget and Gemini 3 by a level; the client sends
    the one the model takes and drops the other. That it does is pinned in
    test_llm_client.py; these cover who is warned."""

    @staticmethod
    def _warned(**cfg):
        return ot.gemini_thinking_warnings({"gemini": cfg})

    def test_a_budget_on_a_3x_model_is_named_as_dropped(self):
        (warning,) = self._warned(model="gemini-3.5-flash", thinking_budget=16000)
        assert "gemini model gemini-3.5-flash sets thinking_budget: 16000" in warning
        assert "litellm drops the number" in warning
        assert "thinking_level" in warning

    @pytest.mark.parametrize("model", ["gemini-3.5-flash-lite", "gemini-3.8-flash"])
    def test_every_3x_model_is_judged_alike(self, model):
        assert len(self._warned(model=model, thinking_budget=2000)) == 1

    def test_a_level_on_a_2_5_model_is_named_as_unsent(self):
        (warning,) = self._warned(model="gemini-2.5-pro", thinking_level="high")
        assert "gemini model gemini-2.5-pro sets thinking_level: 'high'" in warning
        assert "thinking_budget" in warning

    def test_a_level_that_is_not_one_is_named(self):
        (warning,) = self._warned(model="gemini-3.5-flash", thinking_level="extreme")
        assert "thinking_level: 'extreme'" in warning
        assert "minimal, low, medium, high" in warning

    @pytest.mark.parametrize("level", ["minimal", "low", "medium", "high", "HIGH"])
    def test_a_level_on_a_3x_model_is_quiet(self, level):
        assert self._warned(model="gemini-3.5-flash", thinking_level=level) == []

    def test_a_budget_on_a_2_5_model_is_quiet(self):
        assert self._warned(model="gemini-2.5-pro", thinking_budget=16000) == []

    def test_the_model_a_config_leaves_out_is_the_clients_default(self):
        """A config with no `model` calls the default, which is a 3.x model now,
        so a budget on it is dropped."""
        assert len(self._warned(thinking_budget=16000)) == 1

    @pytest.mark.parametrize(
        "configs",
        [
            None,
            {},
            {"gemini": "gemini-3.5-flash"},
            {
                "gemini": {
                    "model": "gemini-3.5-flash",
                    "thinking_budget": 1,
                    "enabled": False,
                }
            },
            {"openai": {"model": "gpt-5.6-terra", "thinking_budget": 1}},
        ],
    )
    def test_a_config_it_cannot_judge_is_not_an_error(self, configs):
        assert ot.gemini_thinking_warnings(configs) == []


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


class TestTheContextWindowClamp:
    """Mistral rejects prompt + max_tokens over its context window, and its map
    entry reports the window (262,144) as the output limit too, so the output
    clamp alone would let a long draft ask for more than the window has left."""

    def test_the_prompt_is_estimated_dense_and_padded(self):
        """2.5 chars per token (mistral measured 2.9-3.7) plus 10,000 for the
        prompt's non-draft parts (measured 1,400-10,000): the estimate errs high
        so the clamp errs low."""
        assert ot._prompt_estimate(190170) == 76068 + 10000
        assert ot._prompt_estimate(0) == 10000

    def test_a_ceiling_past_the_room_left_is_clamped_to_it(self, monkeypatch):
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 262144)
        monkeypatch.setattr(ot, "model_context_limit", lambda p, m: 262144)
        chars = 400000
        got = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", chars)
        assert got == 262144 - ot._prompt_estimate(chars) == 92144

    def test_it_does_not_bind_at_run_24s_size(self, monkeypatch):
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 262144)
        monkeypatch.setattr(ot, "model_context_limit", lambda p, m: 262144)
        unclamped = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", 190170)
        monkeypatch.setattr(ot, "model_context_limit", lambda p, m: None)
        assert (
            ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", 190170)
            == unclamped
        )

    def test_no_room_at_all_leaves_the_output_clamp(self, monkeypatch):
        """A prompt that overfills the window fails at the provider whatever is
        sent; a zero or negative ceiling would only hide that behind a 400 of
        this module's making."""
        monkeypatch.setattr(ot, "model_output_limit", lambda p, m: 262144)
        monkeypatch.setattr(ot, "model_context_limit", lambda p, m: 20000)
        got = ot.compute_max_tokens("mistral", MISTRAL_HIGH, "fact_check", 400000)
        assert got == ot.compute_max_tokens(
            "mistral", MISTRAL_HIGH, "fact_check", 400000, config=ot._CONFIG
        )
        assert got > 0

    def test_lookup_goes_through_litellms_model_map(self, monkeypatch):
        monkeypatch.undo()
        ot.model_context_limit.cache_clear()
        assert ot.model_context_limit("mistral", "mistral-medium-3-5") == 262144
        assert ot.model_context_limit("claude", "claude-haiku-4-5-20251001") == 200000
        assert ot.model_context_limit("mistral", None) is None


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

    def test_a_model_with_no_computed_ceiling_keeps_the_formula(self):
        cfg = {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"}
        formula = tm.compute_timeout(27113, cfg["model"], "xhigh", CEILING)
        assert tm.compute_budget(27113, "openai", cfg, CEILING) == formula

    @pytest.mark.parametrize(
        "provider,cfg",
        [
            ("claude", {"model": "claude-haiku-4-5-20251001"}),
            ("mistral", {"model": "mistral-medium-3-5", "reasoning_effort": "none"}),
        ],
    )
    def test_a_no_effort_capped_pass_gets_the_time_its_ceiling_needs(
        self, provider, cfg
    ):
        """These now carry a computed ceiling too, so the budget covers it:
        claude-haiku-4-5 at `wide` had 120s for a 12,000-token answer it writes
        at 55-73 tokens a second."""
        formula = tm.compute_timeout(27113, cfg["model"], None, CEILING)
        need = ot.seconds_to_fill(provider, cfg, 27113)
        budget = tm.compute_budget(27113, provider, cfg, CEILING)
        assert need > formula, "the case this exists for"
        assert budget >= need

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
