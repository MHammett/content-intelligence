"""Tests for model_registry.check_model_currency."""

import datetime


from ci_core.llm.model_registry import (
    check_model_currency,
    REGISTRY_DATE,
    _SUPERSEDED,
)


class TestCheckModelCurrency:
    def test_current_models_no_warnings(self):
        models = {
            "openai": {"model": "gpt-6.1-sol", "provider": "openai"},
            "gemini": {"model": "gemini-3.5-flash-lite", "provider": "ai_studio"},
            "grok": {"model": "grok-4.3", "provider": "grok"},
            "claude": {"model": "claude-opus-5", "provider": "anthropic"},
            "mistral": {"model": "mistral-large-latest", "provider": "mistral"},
            "perplexity": {"model": "perplexity/sonar", "provider": "perplexity"},
        }
        result = check_model_currency(models)
        assert result["warnings"] == [], f"unexpected warnings: {result['warnings']}"

    def test_every_sonar_id_the_presets_ran_points_at_the_agent_api(self):
        """Sonar Chat Completions was supported until 2026-09-27. A config
        still naming one of its ids is told what replaced it, in one step."""
        for model in ("sonar", "sonar-pro", "sonar-reasoning-pro", "pplx-7b-online"):
            result = check_model_currency({"perplexity": {"model": model}})
            (warning,) = result["warnings"]
            assert warning["replacement"] == "perplexity/sonar", model
            assert warning["replacement"] not in _SUPERSEDED

    def test_superseded_model_triggers_warning(self):
        models = {
            "openai": {"model": "gpt-4o", "provider": "openai"},
        }
        result = check_model_currency(models)
        assert len(result["warnings"]) == 1
        w = result["warnings"][0]
        assert w["provider"] == "openai"
        assert w["model"] == "gpt-4o"
        assert w["replacement"] == "gpt-6.1-sol"

    def test_multiple_superseded(self):
        models = {
            "openai": {"model": "gpt-4o", "provider": "openai"},
            "grok": {"model": "grok-3-latest", "provider": "grok"},
            "claude": {"model": "claude-opus-4-5", "provider": "anthropic"},
        }
        result = check_model_currency(models)
        assert len(result["warnings"]) == 3

    def test_disabled_model_skipped(self):
        models = {
            "openai": {"model": "gpt-4o", "provider": "openai", "enabled": False},
        }
        result = check_model_currency(models)
        assert result["warnings"] == []

    def test_newer_available_is_notice_not_warning(self):
        models = {
            "openai": {"model": "gpt-6-luna", "provider": "openai"},
        }
        result = check_model_currency(models)
        assert result["warnings"] == []
        assert len(result["notices"]) == 1
        assert result["notices"][0]["newer"] == "gpt-6.1-sol"

    def test_empty_config_no_crash(self):
        result = check_model_currency({})
        assert result["warnings"] == []
        assert result["notices"] == []

    def test_none_config_no_crash(self):
        result = check_model_currency(None)
        assert result["warnings"] == []

    def test_registry_age_fields_present(self):
        result = check_model_currency({})
        assert "registry_date" in result
        assert "registry_age_days" in result
        assert isinstance(result["registry_age_days"], int)
        assert result["registry_age_days"] >= 0

    def test_registry_date_is_valid_iso(self):
        result = check_model_currency({})
        parsed = datetime.date.fromisoformat(result["registry_date"])
        assert parsed == REGISTRY_DATE

    def test_the_gemini_2_5_models_are_warned_with_their_retirement(self):
        for model, replacement in (
            ("gemini-2.5-pro", "gemini-3.5-flash"),
            ("gemini-2.5-flash", "gemini-3.5-flash-lite"),
            ("gemini-2.5-flash-lite", "gemini-3.5-flash-lite"),
        ):
            result = check_model_currency(
                {"gemini": {"model": model, "provider": "vertex_ai"}}
            )
            (warning,) = result["warnings"]
            assert warning["replacement"] == replacement
            assert "2026-10-20" in warning["note"]

    def test_no_replacement_is_itself_superseded(self):
        """A warning that names a model the registry then warns about sends
        whoever follows it round in a circle."""
        for model_id, info in _SUPERSEDED.items():
            assert info["replacement"] not in _SUPERSEDED, (
                f"{model_id!r} is replaced by {info['replacement']!r}, which is "
                f"itself superseded"
            )

    def test_every_gemini_replacement_has_a_price(self):
        """The report prices the model it recommends moving to."""
        from ci_core.llm import cost

        for model_id, info in _SUPERSEDED.items():
            if model_id.startswith("gemini"):
                assert cost.known_price(info["replacement"]) is not None, model_id

    def test_all_superseded_keys_are_strings(self):
        for k in _SUPERSEDED:
            assert isinstance(k, str), f"superseded key {k!r} is not a string"

    def test_all_superseded_have_replacement(self):
        for model_id, info in _SUPERSEDED.items():
            assert "replacement" in info, f"{model_id!r} missing 'replacement'"
            assert isinstance(info["replacement"], str), (
                f"{model_id!r} replacement is not a string"
            )
