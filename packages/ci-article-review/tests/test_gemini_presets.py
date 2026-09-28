"""What every cost preset's gemini entry has to be, in both packages.

Gemini 2.5 thinks by a budget and Gemini 3 by a level, and the client sends only
the one its model takes. A 3.x entry that carries a budget asks for nothing,
silently, and a 3.x model with no row in pricing.yaml prices at a shorter
prefix's (gemini-3.5-flash-lite once billed at gemini-3.5-flash's rate and
reported the price as known). These fail at the edit rather than in a report.
"""

import pytest
import yaml

from ci_article_review.config_loader import _load_presets_from_yaml
from ci_core.llm import cost, vertex
from ci_core.llm.output_tokens import _THINKING_LEVELS


def _review_presets():
    return _load_presets_from_yaml()


def _style_presets():
    from importlib import resources

    text = (
        resources.files("ci_style_profile")
        .joinpath("configs/presets.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def _gemini_entries():
    for package, presets in (
        ("ci-article-review", _review_presets()),
        ("ci-style-profile", _style_presets()),
    ):
        for tier, preset in presets.items():
            cfg = (preset.get("models") or {}).get("gemini")
            if isinstance(cfg, dict) and cfg.get("enabled", True) is not False:
                yield pytest.param(package, tier, cfg, id=f"{package}:{tier}")


@pytest.mark.parametrize("package,tier,cfg", list(_gemini_entries()))
class TestEveryGeminiPresetEntry:
    def test_it_names_a_model(self, package, tier, cfg):
        assert cfg.get("model"), f"{package} {tier} has no gemini model"

    def test_its_model_has_a_price_of_its_own(self, package, tier, cfg):
        """Not the fallback, and not a shorter id's row reached by prefix."""
        model = cfg["model"]
        assert model in cost._PRICING, (
            f"{package} {tier}: {model} has no row in pricing.yaml, so it prices "
            f"at {cost.known_price(model)} by prefix, or at the fallback"
        )

    def test_a_3x_model_states_a_level_and_no_budget(self, package, tier, cfg):
        if not vertex.is_modern(cfg["model"]):
            pytest.skip("a 2.5 model")
        level = cfg.get("thinking_level")
        assert level in _THINKING_LEVELS, (
            f"{package} {tier}: {cfg['model']} thinks by level, and the level "
            f"is what turns the thought summaries on; got {level!r}"
        )
        assert "thinking_budget" not in cfg, (
            f"{package} {tier}: litellm drops a thinking_budget on {cfg['model']}"
        )

    def test_a_2_5_model_states_a_budget_or_nothing_and_no_level(
        self, package, tier, cfg
    ):
        if vertex.is_modern(cfg["model"]):
            pytest.skip("a 3.x model")
        assert "thinking_level" not in cfg, (
            f"{package} {tier}: {cfg['model']} has no thinking levels"
        )

    def test_it_is_not_a_model_google_has_scheduled_to_retire(self, package, tier, cfg):
        """2.5 retires on Vertex AI on 2026-10-20, 3.1 Flash-Lite on the Gemini
        API on 2027-05-07 (pricing.yaml and presets.yaml have the sources)."""
        assert not cfg["model"].startswith("gemini-2.5"), (
            f"{package} {tier}: {cfg['model']} retires on Vertex AI 2026-10-20"
        )
        assert cfg["model"] != "gemini-3.1-flash-lite", (
            f"{package} {tier}: gemini-3.1-flash-lite shuts down 2027-05-07"
        )


class TestTheClientsFallbackChain:
    """The chain a capacity error walks, which is not in any preset."""

    def test_every_model_in_it_is_priced_and_current(self):
        from ci_core.llm import client

        spec = client._PROVIDERS["gemini"]
        for model in [spec["default_model"], *spec["fallbacks"]]:
            assert model in cost._PRICING, model
            assert not model.startswith("gemini-2.5"), model

    def test_each_tier_has_somewhere_to_fall_to(self):
        """The chain skips the requested model, so a primary that is also the
        only fallback would leave nothing."""
        from ci_core.llm import client

        spec = client._PROVIDERS["gemini"]
        for cfg in (c.values[2] for c in _gemini_entries()):
            others = [m for m in spec["fallbacks"] if m != cfg["model"]]
            assert others, f"{cfg['model']} has no fallback"
