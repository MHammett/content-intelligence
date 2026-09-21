"""``provider: azure`` for openai and mistral, from user.yaml to what leaves.

From the litellm migration (271eb75, 2026-08-14) until this was fixed, the client
ignored ``provider: azure``: openai calls went to api.openai.com and mistral calls
to api.mistral.ai, each carrying the Azure key as a Bearer token. Where a call
goes is pinned in ci-core (TestAzureRoutesOnTheWire). These pin every step
between user.yaml and the client that could still lose the route or send the key
elsewhere: config loading, the cost presets, ci-check, and the model listings
that ci-discover and the live model check make with the same key.

Wire-verified only. No Azure resource has received any of these requests.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from ci_article_review import check, config_loader, discover, live_model_check
from ci_article_review.config_loader import (
    load_user_config,
    merge_configs,
    preset_names,
)
from ci_core.llm import client

pytestmark = pytest.mark.filterwarnings(
    # litellm 1.96.2's, not ours, and they would bury the socket guard's in the
    # summary: it reads model_fields off an instance while it parses a Chat
    # Completions stream, which pydantic 2.11 deprecates, and it serialises a
    # Responses usage dict as its own ResponseAPIUsage type.
    "ignore::pydantic.warnings.PydanticDeprecatedSince211",
    "ignore:Pydantic serializer warnings:UserWarning",
)

_KEYS = """api_keys:
  openai:
    api_key: azure-openai-key
  gemini:
    api_key: k
  mistral:
    api_key: azure-mistral-key
"""

_AZURE_OPENAI = """  openai:
    provider: azure
    model: gpt-5.6-terra
    endpoint: https://my-resource.openai.azure.com
    deployment: my-deployment
"""

_AZURE_MISTRAL = """  mistral:
    provider: azure
    model: mistral-large-latest
    endpoint: https://Mistral-Large-abc.eastus2.inference.ai.azure.com
"""


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    # Nothing from the real environment resolves into these configs.
    monkeypatch.setattr(config_loader, "_EFFECTIVE_ENV", {})
    return tmp_path


def _load(config_dir, text):
    (config_dir / "user.yaml").write_text(text, encoding="utf-8")
    return load_user_config(str(config_dir))


def _responses_sse(text):
    """A Responses API stream carrying ``text``: what a deployment answers."""
    message = {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    base = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5.6-terra",
        "status": "in_progress",
        "output": [],
    }
    usage = {
        "input_tokens": 20,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 5,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 25,
    }
    events = [
        {"type": "response.created", "response": base},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.completed",
            "response": dict(base, status="completed", output=[message], usage=usage),
        },
    ]
    for n, event in enumerate(events):
        event["sequence_number"] = n
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


def _chat_sse(text):
    """A Chat Completions stream carrying ``text``: what Azure's Mistral sends."""
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"content": text}}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {
            **base,
            "choices": [],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        },
    ]
    events = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(events).encode()


@pytest.fixture
def wire(monkeypatch):
    """Every request litellm sends, answered with ``{"ok": true}``.

    The client registers an openai deployment with litellm process-wide (see
    ci_core.llm.client._stream_azure_deployment); each test gets its own record
    and its own copy of litellm's model map, and what litellm cached from that
    copy is dropped before the original comes back.
    """
    litellm = client._litellm()
    monkeypatch.setattr(client, "_azure_streaming", set())
    monkeypatch.setattr(litellm, "model_cost", dict(litellm.model_cost))
    sent = []

    def _send(http_client, request, *args, **kwargs):
        sent.append(request)
        responses = request.url.path.endswith("/responses")
        body = '{"ok": true}'
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=_responses_sse(body) if responses else _chat_sse(body),
        )

    monkeypatch.setattr(httpx.Client, "send", _send)
    yield sent
    litellm.utils._invalidate_model_cost_lowercase_map()


class TestLoadingRefusesWhatAzureCannotTake:
    """Refused before a run spends anything, and named for what it is."""

    def test_openai_needs_a_deployment(self, config_dir):
        text = (
            _KEYS
            + "models:\n"
            + _AZURE_OPENAI.replace("    deployment: my-deployment\n", "")
        )
        with pytest.raises(ValueError, match="needs deployment") as e:
            _load(config_dir, text)
        assert "models.openai.provider" in str(e.value)

    def test_mistral_needs_an_endpoint(self, config_dir):
        text = (
            _KEYS
            + "models:\n"
            + _AZURE_MISTRAL.replace(
                "    endpoint: https://Mistral-Large-abc.eastus2.inference.ai.azure.com\n",
                "",
            )
        )
        with pytest.raises(ValueError, match="needs endpoint"):
            _load(config_dir, text)

    @pytest.mark.parametrize("written", ['"2024-02-01"', "2024-02-01"])
    def test_the_old_docs_api_version_is_refused(self, config_dir, written):
        """What configs/user.example.yaml told Azure users to write, back when
        the old adapter sent Chat Completions. Unquoted, YAML reads it as a
        date."""
        text = _KEYS + "models:\n" + _AZURE_OPENAI + f"    api_version: {written}\n"
        with pytest.raises(ValueError, match="2025-03-01-preview"):
            _load(config_dir, text)

    @pytest.mark.parametrize(
        "provider,value", [("openai", "azure_openai"), ("mistral", "Azure")]
    )
    def test_an_endpoint_the_client_does_not_have_is_refused(
        self, config_dir, provider, value
    ):
        text = _KEYS + f"models:\n  {provider}:\n    provider: {value}\n"
        with pytest.raises(ValueError, match=f"'{value}'") as e:
            _load(config_dir, text)
        assert "azure" in str(e.value)

    @pytest.mark.parametrize("provider,value", [("claude", "bedrock"), ("grok", "xai")])
    def test_a_provider_with_one_endpoint_refuses_any_other(
        self, config_dir, provider, value
    ):
        """The same silence, one provider over: these were ignored too."""
        text = _KEYS + f"models:\n  {provider}:\n    provider: {value}\n"
        with pytest.raises(ValueError, match=f"'{value}'"):
            _load(config_dir, text)

    def test_a_complete_azure_block_loads(self, config_dir):
        config = _load(config_dir, _KEYS + "models:\n" + _AZURE_OPENAI + _AZURE_MISTRAL)
        assert config["models"]["openai"]["deployment"] == "my-deployment"
        assert config["models"]["mistral"]["provider"] == "azure"


class TestEveryPresetKeepsTheAzureRoute:
    """A cost preset rebuilds each model config from presets.yaml and copies back
    only config_loader._INFRA_KEYS, which the route keys are among. The model is
    the other half: on Azure the deployment decides what runs, so a preset's
    model would only misname it."""

    @pytest.mark.parametrize("preset", preset_names())
    def test_the_route_and_the_model_survive(self, config_dir, preset):
        text = (
            _KEYS
            + "models:\n"
            + _AZURE_OPENAI
            + "    api_version: 2025-04-01-preview\n"
            + _AZURE_MISTRAL
            + f"pipeline:\n  cost_preset: {preset}\n"
        )
        models = merge_configs(_load(config_dir, text), {})["models"]
        route_keys = ("provider", "model", "endpoint", "deployment", "api_version")
        assert {key: models["openai"].get(key) for key in route_keys} == {
            "provider": "azure",
            "model": "gpt-5.6-terra",
            "endpoint": "https://my-resource.openai.azure.com",
            "deployment": "my-deployment",
            "api_version": "2025-04-01-preview",
        }
        assert models["mistral"]["model"] == "mistral-large-latest"
        assert models["mistral"]["endpoint"] == (
            "https://Mistral-Large-abc.eastus2.inference.ai.azure.com"
        )

    @pytest.mark.parametrize("preset", preset_names())
    def test_the_preset_still_sets_everything_else(self, config_dir, preset):
        """Only the model is kept: effort, and whether a provider runs at all,
        are still the preset's."""
        text = (
            _KEYS
            + "models:\n"
            + _AZURE_OPENAI
            + f"pipeline:\n  cost_preset: {preset}\n"
        )
        openai = merge_configs(_load(config_dir, text), {})["models"]["openai"]
        preset_openai = config_loader._load_presets_from_yaml()[preset]["models"][
            "openai"
        ]
        assert openai.get("reasoning_effort") == preset_openai.get("reasoning_effort")

    def test_with_no_model_in_user_yaml_the_presets_is_dropped(self, config_dir):
        """So the client names the call after the deployment, which Azure names
        after its model unless told otherwise."""
        text = (
            _KEYS
            + "models:\n"
            + _AZURE_OPENAI.replace("    model: gpt-5.6-terra\n", "")
            + "pipeline:\n  cost_preset: maximum\n"
        )
        openai = merge_configs(_load(config_dir, text), {})["models"]["openai"]
        assert "model" not in openai

    def test_a_route_that_is_not_azure_still_takes_the_presets_model(self, config_dir):
        text = (
            _KEYS
            + "models:\n  openai:\n    model: gpt-5.4\n"
            + "pipeline:\n  cost_preset: maximum\n"
        )
        openai = merge_configs(_load(config_dir, text), {})["models"]["openai"]
        assert openai["model"] == "gpt-5.6-sol"


class TestAnAzureUserYamlReachesAzure:
    """user.yaml, config loading, a preset, then the client, as a run chains
    them. Only the transport is replaced."""

    def _models(self, config_dir, preset="wide"):
        text = (
            _KEYS
            + "models:\n"
            + _AZURE_OPENAI
            + _AZURE_MISTRAL
            + f"pipeline:\n  cost_preset: {preset}\n"
        )
        return merge_configs(_load(config_dir, text), {})

    def test_openai_under_the_maximum_preset(self, config_dir, wire):
        """maximum sends xhigh effort; it goes to the deployment with it."""
        config = self._models(config_dir, "maximum")
        result = client.call(
            "openai",
            "sys",
            "user",
            config["api_keys"]["openai"]["api_key"],
            retry=False,
            retry_delay=0,
            provider_config=config["models"]["openai"],
        )
        assert result["failed"] is False, result
        assert result["model"] == "gpt-5.6-terra"
        (request,) = wire
        assert str(request.url) == (
            "https://my-resource.openai.azure.com/openai/v1/responses"
            "?api-version=preview"
        )
        assert request.headers["api-key"] == "azure-openai-key"
        body = json.loads(request.content)
        assert body["model"] == "my-deployment"
        assert body["reasoning"]["effort"] == "xhigh"
        assert body["stream"] is True

    def test_mistral_under_the_wide_preset(self, config_dir, wire):
        config = self._models(config_dir)
        result = client.call(
            "mistral",
            "sys",
            "user",
            config["api_keys"]["mistral"]["api_key"],
            retry=False,
            retry_delay=0,
            provider_config=config["models"]["mistral"],
        )
        assert result["failed"] is False, result
        (request,) = wire
        assert request.url.host == "mistral-large-abc.eastus2.inference.ai.azure.com"
        assert request.headers["authorization"] == "Bearer azure-mistral-key"
        assert json.loads(request.content)["model"] == "mistral-large-latest"


class TestCiCheckGoesWhereARunGoes:
    """ci-check's Azure checks used to send Chat Completions at api-version
    2024-02-01 from code of their own, and passed while every run went to
    api.openai.com and api.mistral.ai. They now make the run's own call."""

    def _openai_cfg(self, **extra):
        return {
            "provider": "azure",
            "model": "gpt-5.6-terra",
            "endpoint": "https://my-resource.openai.azure.com",
            "deployment": "my-deployment",
            **extra,
        }

    def test_openai(self, wire):
        reply = check.check_openai_azure("azure-openai-key", self._openai_cfg())
        (request,) = wire
        assert str(request.url) == (
            "https://my-resource.openai.azure.com/openai/v1/responses"
            "?api-version=preview"
        )
        assert request.headers["api-key"] == "azure-openai-key"
        assert "authorization" not in request.headers
        assert json.loads(request.content)["model"] == "my-deployment"
        assert reply == (
            "provider=azure deployment=my-deployment model=gpt-5.6-terra, "
            "replied: {'ok': True}"
        )

    def test_mistral(self, wire):
        cfg = {
            "provider": "azure",
            "model": "mistral-large-latest",
            "endpoint": "https://Mistral-Large-abc.eastus2.inference.ai.azure.com",
        }
        reply = check.check_mistral_azure("azure-mistral-key", cfg)
        (request,) = wire
        assert request.url.host == "mistral-large-abc.eastus2.inference.ai.azure.com"
        assert request.headers["authorization"] == "Bearer azure-mistral-key"
        assert (
            reply == "provider=azure model=mistral-large-latest, replied: {'ok': True}"
        )

    def test_the_presets_effort_and_search_stay_out(self, wire):
        """It checks the endpoint, the deployment and the key; ci-probe checks
        what a preset asks of them. An xhigh check would cost like a review."""
        check.check_openai_azure(
            "azure-openai-key",
            self._openai_cfg(reasoning_effort="xhigh", web_search=True),
        )
        (request,) = wire
        body = json.loads(request.content)
        assert "reasoning" not in body
        assert "tools" not in body

    def test_a_missing_endpoint_fails_before_anything_is_sent(self, wire):
        with pytest.raises(ValueError, match="endpoint"):
            check.check_openai_azure("k", {"deployment": "my-deployment"})
        assert wire == []


class TestModelListingsKeepTheAzureKey:
    """ci-discover and the live model check list models with the key under
    api_keys. On Azure that is an Azure key, and the listings are OpenAI's and
    Mistral's own."""

    _MODELS = {
        "openai": {
            "provider": "azure",
            "model": "gpt-5.6-terra",
            "endpoint": "https://my-resource.openai.azure.com",
            "deployment": "my-deployment",
        },
        "mistral": {
            "provider": "azure",
            "model": "mistral-large-latest",
            "endpoint": "https://Mistral-Large-abc.eastus2.inference.ai.azure.com",
        },
    }
    _KEYS = {"openai": {"api_key": "azure-openai-key"}, "mistral": {"api_key": "m"}}

    def test_ci_discover_asks_neither_listing(self):
        with patch.object(
            discover.requests, "get", side_effect=AssertionError("listing asked")
        ):
            got = discover.collect_available_models(self._MODELS, self._KEYS)
        for provider in ("openai", "mistral"):
            assert got[provider]["status"] == "skipped"
            assert got[provider]["reason"] == "azure"

    def test_ci_discover_says_why(self, capsys):
        config = {"api_keys": self._KEYS, "models": self._MODELS, "pipeline": {}}
        with (
            patch("sys.argv", ["ci-discover", "--provider", "openai"]),
            patch.object(discover, "load_user_config", return_value=config),
            patch.object(
                discover.requests, "get", side_effect=AssertionError("listing asked")
            ),
        ):
            try:
                discover.main()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Configured via Azure" in out
        assert "endpoint=https://my-resource.openai.azure.com" in out

    def test_the_live_model_check_asks_neither_listing(self, tmp_path):
        ran = {"openai": "gpt-5.6-terra", "mistral": "mistral-large-latest"}
        with patch.object(
            discover.requests, "get", side_effect=AssertionError("listing asked")
        ):
            live = live_model_check.check(
                ran,
                self._KEYS,
                refresh=True,
                cache_path=tmp_path / "discovery.json",
                model_configs=self._MODELS,
            )
        reasons = {u["provider"]: u["reason"] for u in live["unchecked"]}
        assert set(reasons) == {"openai", "mistral"}
        assert all("Azure" in reason for reason in reasons.values())

    def test_a_provider_on_its_own_api_is_still_listed(self, tmp_path):
        """Only the Azure route comes through from the run's config."""
        listed = []

        def _get(url, **kwargs):
            listed.append(url)
            raise ConnectionError("stubbed")

        with patch.object(discover.requests, "get", side_effect=_get):
            live_model_check.check(
                {"openai": "gpt-5.4"},
                {"openai": {"api_key": "sk-openai"}},
                refresh=True,
                cache_path=tmp_path / "discovery.json",
                model_configs={"openai": {"provider": "openai", "model": "gpt-5.4"}},
            )
        assert listed == ["https://api.openai.com/v1/models"]
