"""Tests for ci_core.config_helpers.

These helpers are the supported cross-package contract for YAML/env config
loading — ci-article-review and ci-style-profile both build their configs on
them, so a change here is a change to both callers.
"""

import os

import pytest


class TestConfigHelpers:
    def test_normalize_simple_string_gemini(self):
        from ci_core.config_helpers import normalize_model_configs

        result = normalize_model_configs({"gemini": "gemini-2.5-flash"})
        assert result["gemini"]["provider"] == "ai_studio"
        assert result["gemini"]["model"] == "gemini-2.5-flash"

    def test_normalize_simple_string_openai(self):
        from ci_core.config_helpers import normalize_model_configs

        result = normalize_model_configs({"openai": "gpt-4o"})
        assert result["openai"]["provider"] == "openai"
        assert result["openai"]["model"] == "gpt-4o"

    def test_normalize_extended_dict_passthrough(self):
        from ci_core.config_helpers import normalize_model_configs

        cfg = {
            "gemini": {
                "provider": "vertex_ai",
                "model": "gemini-2.5-flash",
                "project": "my-proj",
            }
        }
        result = normalize_model_configs(cfg)
        assert result["gemini"]["provider"] == "vertex_ai"
        assert result["gemini"]["project"] == "my-proj"

    def test_normalize_dict_without_provider_gets_default(self):
        from ci_core.config_helpers import normalize_model_configs

        result = normalize_model_configs({"mistral": {"model": "mistral-large-latest"}})
        assert result["mistral"]["provider"] == "mistral"

    def test_normalize_mixed_forms(self):
        from ci_core.config_helpers import normalize_model_configs

        raw = {
            "openai": "gpt-4o",
            "gemini": {
                "provider": "vertex_ai",
                "model": "gemini-2.5-flash",
                "project": "p",
            },
        }
        result = normalize_model_configs(raw)
        assert result["openai"]["provider"] == "openai"
        assert result["gemini"]["provider"] == "vertex_ai"

    def test_normalize_simple_string_claude(self):
        from ci_core.config_helpers import normalize_model_configs

        result = normalize_model_configs({"claude": "claude-opus-4-5"})
        assert result["claude"]["provider"] == "anthropic"
        assert result["claude"]["model"] == "claude-opus-4-5"

    def test_normalize_preserves_enabled_flag(self):
        from ci_core.config_helpers import normalize_model_configs

        result = normalize_model_configs(
            {"grok": {"model": "grok-3-latest", "enabled": False}}
        )
        assert result["grok"]["enabled"] is False
        assert result["grok"]["provider"] == "grok"

    def test_normalize_enabled_defaults_absent_for_simple_form(self):
        from ci_core.config_helpers import normalize_model_configs

        result = normalize_model_configs({"openai": "gpt-4o"})
        # Simple form has no enabled key — caller should default to True
        assert "enabled" not in result["openai"]

    def test_normalize_empty_input(self):
        from ci_core.config_helpers import normalize_model_configs

        assert normalize_model_configs({}) == {}
        assert normalize_model_configs(None) == {}

    def test_env_var_missing_gives_helpful_error(self):
        from ci_core.config_helpers import resolve_env

        env_key = "PIPELINE_TEST_MISSING_VAR_XYZ"
        if env_key in os.environ:
            del os.environ[env_key]
        with pytest.raises(ValueError, match="not set"):
            resolve_env(f"${{{env_key}}}")

    def test_resolve_env_defaults_to_os_environ(self, monkeypatch):
        from ci_core.config_helpers import resolve_env

        monkeypatch.setenv("PIPELINE_TEST_VAR_ABC", "from_os_environ")
        assert resolve_env("${PIPELINE_TEST_VAR_ABC}") == "from_os_environ"

    def test_resolve_env_prefers_the_given_mapping_over_os_environ(self, monkeypatch):
        """The whole point of the ``env`` parameter: a caller-supplied mapping
        (e.g. ci_core.env_provenance.effective_env's .env-wins merge) takes
        priority over whatever resolve_env would find in os.environ."""
        from ci_core.config_helpers import resolve_env

        monkeypatch.setenv("PIPELINE_TEST_VAR_ABC", "from_os_environ")
        assert (
            resolve_env(
                "${PIPELINE_TEST_VAR_ABC}", env={"PIPELINE_TEST_VAR_ABC": "from_dotenv"}
            )
            == "from_dotenv"
        )

    def test_resolve_env_recursive_threads_env_through(self, monkeypatch):
        from ci_core.config_helpers import resolve_env_recursive

        monkeypatch.delenv("PIPELINE_TEST_VAR_ABC", raising=False)
        result = resolve_env_recursive(
            {"api_keys": {"openai": {"api_key": "${PIPELINE_TEST_VAR_ABC}"}}},
            env={"PIPELINE_TEST_VAR_ABC": "from_dotenv"},
        )
        assert result["api_keys"]["openai"]["api_key"] == "from_dotenv"


class TestHasCredentials:
    """Which models can be called: a key, or gemini on Vertex with a project."""

    def test_a_key_is_enough(self):
        from ci_core.config_helpers import has_credentials

        assert has_credentials("mistral", "k", {"model": "m"})
        assert has_credentials("gemini", "k", None)

    def test_no_key_is_not(self):
        from ci_core.config_helpers import has_credentials

        assert not has_credentials("mistral", "", {"model": "m"})
        assert not has_credentials("gemini", None, {"provider": "ai_studio"})

    def test_gemini_on_vertex_needs_a_project_and_no_key(self):
        from ci_core.config_helpers import has_credentials

        assert has_credentials(
            "gemini", None, {"provider": "vertex_ai", "project": "p"}
        )
        assert not has_credentials("gemini", "k", {"provider": "vertex_ai"})

    def test_only_gemini_has_a_vertex_route(self):
        """A stray provider: vertex_ai elsewhere does not excuse a missing key."""
        from ci_core.config_helpers import has_credentials

        vertex = {"provider": "vertex_ai", "project": "p"}
        assert not has_credentials("claude", None, vertex)
