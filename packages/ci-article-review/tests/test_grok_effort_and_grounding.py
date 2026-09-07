"""Two settings that were doing something other than what they said.

**grok's reasoning effort.** Every tier left it unset, which reads as "off" and
is not: it resolves to the provider default, and grok-4.6's default is high.
`balanced`, whose description is "light reasoning on key models" and which sets
openai to low and claude to medium by hand, was running grok at high. Verified
live 2026-09-05 that litellm passes the parameter through to xAI with no escape
hatch (unlike mistral) and that grok honours it — low produced 345 and 415
completion tokens against 1025 and 1101 at high, on the same prompt.

**Which models count as grounded.** `_SEARCH_GROUNDED_MODELS` is a static
tuple, but grounding is half configuration: `maximum` sets
`web_search: [fact_check]` on claude precisely so that domain searches. Ranking
fact_check substitutes by the tuple alone put an ungrounded model ahead of one
the config had explicitly grounded, on the one domain where grounding is the
reason the ensemble is shaped as it is.
"""

from ci_article_review import pipeline
from ci_article_review.config_loader import _apply_cost_preset, _load_presets_from_yaml


_ALL = ["openai", "gemini", "mistral", "perplexity", "grok", "claude"]
_KEYS = {m: {"api_key": "k"} for m in _ALL}


def _preset_models(tier):
    raw = {m: {"model": "x"} for m in _ALL}
    _pipe, models = _apply_cost_preset({"cost_preset": tier}, raw, user_set={})
    return models


class TestGrokEffortIsStatedNotInherited:
    def test_every_tier_running_grok_4_6_says_what_it_wants(self):
        """Unset is not off — it is the provider default, which is high."""
        for tier, body in _load_presets_from_yaml().items():
            grok = _preset_models(tier).get("grok") or {}
            if grok.get("enabled") is False or grok.get("model") != "grok-4.6":
                continue
            assert grok.get("reasoning_effort"), (
                f"{tier} runs grok-4.6 with no reasoning_effort, which silently "
                f"means high"
            )

    def test_balanced_asks_for_the_light_reasoning_it_advertises(self):
        assert _preset_models("balanced")["grok"]["reasoning_effort"] == "low"

    def test_the_deep_tiers_ask_for_high(self):
        for tier in ("thorough", "maximum"):
            assert _preset_models(tier)["grok"]["reasoning_effort"] == "high"

    def test_grok_4_3_is_left_alone(self):
        """It predates the parameter; sending one risks a 400 for no gain."""
        grok = _preset_models("wide")["grok"]
        assert grok["model"] == "grok-4.3"
        assert "reasoning_effort" not in grok


class TestWhatCountsAsGrounded:
    def test_the_always_grounded_models_need_no_config(self):
        for m in ("gemini", "perplexity"):
            assert pipeline._is_grounded_for(m, "fact_check", {})

    def test_a_configured_model_counts_for_the_domain_it_covers(self):
        cfg = {"web_search": ["fact_check"]}
        assert pipeline._is_grounded_for("claude", "fact_check", cfg)

    def test_and_not_for_a_domain_it_does_not(self):
        cfg = {"web_search": ["fact_check"]}
        assert not pipeline._is_grounded_for("claude", "red_team", cfg)

    def test_an_unconfigured_model_is_not_grounded(self):
        assert not pipeline._is_grounded_for("claude", "fact_check", {})
        assert not pipeline._is_grounded_for("openai", "fact_check", {})

    def test_web_search_true_still_means_every_domain(self):
        """The original meaning, kept so existing configs are unaffected."""
        assert pipeline._is_grounded_for("grok", "red_team", {"web_search": True})

    def test_the_simple_config_form_fails_soft(self):
        """`openai: gpt-5.5` reaches here on the post-failure path; raising
        AttributeError at that moment would be the worst available outcome."""
        assert not pipeline._is_grounded_for("openai", "fact_check", "gpt-5.5")


class TestGroundedSubstitutesOutrankUngroundedOnes:
    def test_a_configured_model_is_preferred_for_fact_check(self):
        """The defect: claude carries `web_search: [fact_check]` at `maximum`,
        and was still ranked behind openai, which does not search."""
        configs = {m: {} for m in _ALL}
        configs["claude"] = {"web_search": ["fact_check"]}
        got = pipeline._substitute_candidates(
            "fact_check", {"gemini", "perplexity"}, configs, _KEYS, None
        )
        assert got.index("claude") < got.index("openai")

    def test_without_that_config_the_order_is_unchanged(self):
        configs = {m: {} for m in _ALL}
        got = pipeline._substitute_candidates(
            "fact_check", {"gemini", "perplexity"}, configs, _KEYS, None
        )
        assert got.index("openai") < got.index("claude")

    def test_the_always_grounded_models_still_come_first(self):
        configs = {m: {} for m in _ALL}
        configs["claude"] = {"web_search": ["fact_check"]}
        got = pipeline._substitute_candidates("fact_check", set(), configs, _KEYS, None)
        assert got[0] in pipeline._SEARCH_GROUNDED_MODELS

    def test_other_domains_are_not_reordered(self):
        """Only fact_check has any use for grounding, and only it reorders."""
        configs = {m: {} for m in _ALL}
        configs["claude"] = {"web_search": True}
        got = pipeline._substitute_candidates("red_team", set(), configs, _KEYS, None)
        expected = [
            m
            for m in pipeline._THOROUGHNESS_PRESETS["maximum"]["red_team"]
            if m in configs
        ]
        assert got == expected
