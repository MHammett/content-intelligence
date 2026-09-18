"""Tests for config_loader._apply_preset_overrides and cost preset interaction."""

import pytest


from ci_article_review.config_loader import _apply_preset_overrides, _apply_cost_preset


class TestApplyPresetOverrides:
    def test_no_overrides_returns_models_unchanged(self):
        models = {"openai": {"model": "gpt-5.4"}}
        result = _apply_preset_overrides({"cost_preset": "balanced"}, models)
        assert result["openai"]["model"] == "gpt-5.4"

    def test_override_single_key(self):
        models = {"openai": {"model": "gpt-5.4", "reasoning_effort": "low"}}
        pipeline = {
            "cost_preset": "balanced",
            "preset_overrides": {"openai": {"reasoning_effort": "high"}},
        }
        result = _apply_preset_overrides(pipeline, models)
        assert result["openai"]["reasoning_effort"] == "high"
        assert result["openai"]["model"] == "gpt-5.4"  # unchanged

    def test_override_model_and_effort(self):
        # Overrides add/change keys; they do NOT remove keys not mentioned.
        # To neutralize thinking_budget, set it explicitly to null.
        models = {"claude": {"model": "claude-sonnet-4-6", "thinking_budget": 8000}}
        pipeline = {
            "preset_overrides": {
                "claude": {"model": "claude-opus-4-8", "effort": "high"},
            }
        }
        result = _apply_preset_overrides(pipeline, models)
        assert result["claude"]["model"] == "claude-opus-4-8"
        assert result["claude"]["effort"] == "high"
        assert (
            result["claude"]["thinking_budget"] == 8000
        )  # still present; set to null to neutralize

    def test_override_null_removes_effect_of_key(self):
        # Setting a key to None (YAML: null) effectively disables it —
        # all adapters guard with "if cfg.get('key'):" so None == disabled.
        models = {"claude": {"model": "claude-sonnet-4-6", "thinking_budget": 8000}}
        pipeline = {
            "preset_overrides": {
                "claude": {
                    "model": "claude-opus-4-8",
                    "effort": "high",
                    "thinking_budget": None,
                },
            }
        }
        result = _apply_preset_overrides(pipeline, models)
        assert result["claude"]["model"] == "claude-opus-4-8"
        assert result["claude"]["thinking_budget"] is None  # disabled via null

    def test_override_ignores_unconfigured_provider(self):
        models = {"openai": {"model": "gpt-5.4"}}
        pipeline = {
            "preset_overrides": {
                "claude": {"model": "claude-opus-4-8"},  # claude not in models
            }
        }
        result = _apply_preset_overrides(pipeline, models)
        assert "claude" not in result

    def test_override_preserves_other_providers(self):
        models = {
            "openai": {"model": "gpt-5.4"},
            "mistral": {"model": "mistral-large-latest"},
        }
        pipeline = {"preset_overrides": {"openai": {"reasoning_effort": "xhigh"}}}
        result = _apply_preset_overrides(pipeline, models)
        assert result["mistral"]["model"] == "mistral-large-latest"

    def test_override_can_disable_provider(self):
        models = {"grok": {"model": "grok-4.3"}}
        pipeline = {"preset_overrides": {"grok": {"enabled": False}}}
        result = _apply_preset_overrides(pipeline, models)
        assert result["grok"]["enabled"] is False

    def test_none_overrides_is_noop(self):
        models = {"openai": {"model": "gpt-5.4"}}
        result = _apply_preset_overrides({"cost_preset": "balanced"}, models)
        assert result == models

    def test_empty_overrides_is_noop(self):
        models = {"openai": {"model": "gpt-5.4"}}
        result = _apply_preset_overrides({"preset_overrides": {}}, models)
        assert result == models

    def test_override_on_top_of_preset(self):
        """Full flow: preset sets balanced, override bumps openai to high reasoning."""
        models_raw = {
            "openai": "gpt-5.4",
            "gemini": "gemini-2.5-flash",
            "mistral": "mistral-large-latest",
        }
        pipeline = {
            "cost_preset": "balanced",
            "preset_overrides": {
                "openai": {"reasoning_effort": "high"},
            },
        }
        pipeline, models_after_preset = _apply_cost_preset(pipeline, models_raw)
        models_final = _apply_preset_overrides(pipeline, models_after_preset)

        # Preset set low; override should have bumped it to high
        assert models_final["openai"]["reasoning_effort"] == "high"
        # Preset's model selection should still be in effect
        assert models_final["openai"]["model"] == "gpt-5.6-terra"

    def test_invalid_overrides_type_raises(self):
        with pytest.raises(ValueError, match="preset_overrides must be a mapping"):
            _apply_preset_overrides({"preset_overrides": "not-a-dict"}, {})


class TestPresetPreservesUserCapabilityFlags:
    """A cost preset picks model variants and reasoning depth — not permissions.

    The preset rebuilds each configured model's dict, so any key it does not
    know about is dropped. ``web_search`` was one of those: setting it alongside
    a ``cost_preset`` — which is every non-default configuration — silently did
    nothing, and the failure was invisible because the flag simply wasn't there
    by the time the adapter looked for it.
    """

    def test_web_search_survives_a_cost_preset(self):
        models_raw = {"openai": {"model": "gpt-5.4", "web_search": ["fact_check"]}}
        _, models = _apply_cost_preset({"cost_preset": "maximum"}, models_raw)
        assert models["openai"]["web_search"] == ["fact_check"]

    def test_bool_form_survives_too(self):
        models_raw = {"openai": {"model": "gpt-5.4", "web_search": True}}
        _, models = _apply_cost_preset({"cost_preset": "balanced"}, models_raw)
        assert models["openai"]["web_search"] is True

    def test_preset_still_owns_the_model_variant(self):
        """Preserving the flag must not freeze the rest of the model config."""
        models_raw = {"openai": {"model": "gpt-5.4", "web_search": ["fact_check"]}}
        _, models = _apply_cost_preset({"cost_preset": "maximum"}, models_raw)
        assert models["openai"]["model"] != "gpt-5.4"

    def test_prompts_routing_survives_as_well(self):
        """The neighbouring per-model routing key, guarded for the same reason."""
        models_raw = {"claude": {"model": "claude-opus-4-8", "prompts": ["red_team"]}}
        _, models = _apply_cost_preset({"cost_preset": "maximum"}, models_raw)
        assert models["claude"]["prompts"] == ["red_team"]

    def test_an_explicit_output_ceiling_survives_like_the_timeout_does(self):
        """client.py's own comments said to raise `max_tokens` if a domain
        still truncated. Under any cost_preset — every real configuration — the
        preset rebuilt the dict and dropped it, so the advice did nothing."""
        models_raw = {
            "claude": {"model": "claude-opus-4-8", "max_tokens": 48000},
            "mistral": {"model": "mistral-large-latest", "timeout_seconds": 400},
        }
        _, models = _apply_cost_preset({"cost_preset": "maximum"}, models_raw)
        assert models["claude"]["max_tokens"] == 48000
        assert models["mistral"]["timeout_seconds"] == 400
        # The preset still owns the model and its reasoning depth.
        assert models["claude"]["model"] == "claude-opus-5"
        assert models["claude"]["effort"] == "high"
