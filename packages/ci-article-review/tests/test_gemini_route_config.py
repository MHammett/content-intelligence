"""models.gemini.provider, from user.yaml to the request that leaves.

From the litellm migration (271eb75, 2026-08-14) until this was fixed, the client
ignored ``provider: vertex_ai`` and sent every gemini call to AI Studio. Where a
call goes is pinned in ci-core (TestGeminiRoutesOnTheWire). These pin the two
steps between user.yaml and the client that could still lose the setting:
config loading, which refuses a value no endpoint answers to, and the cost
preset, which rebuilds every model config and has to carry the Vertex keys
across. The last test runs the whole chain.
"""

import json

import httpx
import pytest

from ci_article_review import config_loader
from ci_article_review.config_loader import (
    load_user_config,
    merge_configs,
    preset_names,
)
from ci_core.llm import client

_KEYS = """api_keys:
  openai:
    api_key: k
  mistral:
    api_key: k
"""

_VERTEX = """models:
  gemini:
    provider: vertex_ai
    model: gemini-2.5-flash
    project: test-project
    location: europe-west4
    credentials_file: '{credentials}'
"""


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    # Nothing from the real environment resolves into these configs.
    monkeypatch.setattr(config_loader, "_EFFECTIVE_ENV", {})
    return tmp_path


def _load(config_dir, text):
    (config_dir / "user.yaml").write_text(text, encoding="utf-8")
    return load_user_config(str(config_dir))


class TestLoadingRefusesAnUnknownEndpoint:
    def test_a_misspelt_vertex_is_refused_by_name(self, config_dir):
        """Reported as what it is. This config has no AI Studio key, so the
        check that runs after it would otherwise have called that the problem."""
        text = _KEYS + "models:\n  gemini:\n    provider: vertex\n    project: p\n"
        with pytest.raises(ValueError, match="'vertex'") as e:
            _load(config_dir, text)
        assert "vertex_ai" in str(e.value)
        assert "missing required fields" not in str(e.value)

    def test_vertex_loads_with_no_ai_studio_key(self, config_dir):
        config = _load(config_dir, _KEYS + _VERTEX.format(credentials="sa.json"))
        assert config["models"]["gemini"]["provider"] == "vertex_ai"

    def test_a_plain_model_string_takes_the_default(self, config_dir):
        text = (
            _KEYS + "  gemini:\n    api_key: k\nmodels:\n  gemini: gemini-2.5-flash\n"
        )
        config = _load(config_dir, text)
        assert config["models"]["gemini"] == "gemini-2.5-flash"


class TestEveryPresetKeepsTheRoute:
    """A cost preset rebuilds each model config from presets.yaml and copies back
    only config_loader._INFRA_KEYS. Lose one of these four and Vertex breaks on
    that preset alone, which nothing else here would notice."""

    @pytest.mark.parametrize("preset", preset_names())
    def test_the_vertex_keys_survive(self, config_dir, preset):
        text = (
            _KEYS
            + _VERTEX.format(credentials="sa.json")
            + f"pipeline:\n  cost_preset: {preset}\n"
        )
        gemini = merge_configs(_load(config_dir, text), {})["models"]["gemini"]
        assert {
            key: gemini.get(key)
            for key in ("provider", "project", "location", "credentials_file")
        } == {
            "provider": "vertex_ai",
            "project": "test-project",
            "location": "europe-west4",
            "credentials_file": "sa.json",
        }


class TestAVertexUserYamlReachesVertex:
    """user.yaml, config loading, a preset, then the client, as a run chains them.
    Only the transport and litellm's token exchange are replaced."""

    def test_under_the_wide_preset(self, config_dir, monkeypatch):
        key_file = config_dir / "sa.json"
        key_file.write_text("{}", encoding="utf-8")
        text = (
            _KEYS
            + _VERTEX.format(credentials=key_file)
            + "pipeline:\n  cost_preset: wide\n"
        )
        gemini = merge_configs(_load(config_dir, text), {})["models"]["gemini"]

        client._litellm()
        from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

        sent, exchanged = [], []
        chunk = {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"flags": []}'}], "role": "model"},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
        }

        def _send(http_client, request, *args, **kwargs):
            sent.append(request)
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(chunk)}\r\n\r\n".encode(),
            )

        def _token(vertex_base, credentials, project_id, custom_llm_provider):
            exchanged.append(credentials)
            return "ya29.stub-token", project_id

        monkeypatch.setattr(httpx.Client, "send", _send)
        monkeypatch.setattr(VertexBase, "_ensure_access_token", _token)

        result = client.call(
            "gemini",
            "sys",
            "user",
            None,
            retry=False,
            retry_delay=0,
            provider_config=gemini,
        )

        assert result["failed"] is False, result
        (request,) = sent
        assert request.url.host == "europe-west4-aiplatform.googleapis.com"
        assert request.url.path == (
            "/v1/projects/test-project/locations/europe-west4/publishers/google/"
            "models/gemini-2.5-flash:streamGenerateContent"
        )
        assert exchanged == [str(key_file)]
