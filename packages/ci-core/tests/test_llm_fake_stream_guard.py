"""The shim refuses a ``responses()`` call litellm would answer with a fake stream.

litellm streams a ``responses()`` call natively only for a model its model map
says streams. For any other it sends one non-streaming request and replays the
finished answer (UPSTREAM.md #9), so nothing arrives until the model is done —
the exact silence this client puts openai on ``responses()`` to avoid, and one
that a reasoning call cannot survive: the first-byte allowance cuts it off and
the failure is reported as a timeout, which points at ``stream_read_timeout``
rather than at the model.

``client._refuse_fake_stream`` therefore asks litellm the question
``responses()`` is about to ask it, before anything is sent. These tests cover
the three answers that matter — a model the map streams, one it marks as not
streaming, one it does not list — and that nothing reaches the wire for the last
two. Each presets.yaml that names openai models is held to the same rule in
CI by its own package's ``test_preset_native_streaming.py`` (ci-article-review's
and ci-style-profile's); this file is about what happens at runtime, where the
map is whichever one litellm managed to load.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from ci_core.llm import client


#: Invented here, and marked the way litellm's map marks ``gpt-5-pro``: what is
#: under test is the shim's behaviour on that answer, not that answer's current
#: owner. ``o1-pro`` and ``gpt-5-pro`` are the openai models carrying it today.
UNSTREAMABLE = "gpt-ci-does-not-stream"
UNLISTED = "not-a-listed-model"


def _call(model=None, provider="openai", **kwargs):
    defaults = dict(
        system_prompt="sys",
        user_prompt="user",
        api_key="key",
        retry=False,
        retry_delay=0,
    )
    if model is not None:
        defaults["provider_config"] = {"model": model}
    defaults.update(kwargs)
    return client.call(provider, **defaults)


@pytest.fixture
def litellm_map():
    """litellm with its model map restored afterwards, caches and lists included.

    ``register_model`` mutates process-wide state that litellm caches lookups
    off, so every test that touches it gets its own copy of the map, its own
    copy of the openai model list ``get_llm_provider`` reads, and its own record
    of which Azure deployments were registered.
    """
    litellm = client._litellm()
    original = litellm.model_cost
    listed = frozenset(litellm.open_ai_chat_completion_models)
    registered = client._azure_streaming
    litellm.model_cost = dict(original)
    client._azure_streaming = set()
    litellm.utils._invalidate_model_cost_lowercase_map()
    try:
        yield litellm
    finally:
        litellm.model_cost = original
        litellm.open_ai_chat_completion_models.intersection_update(listed)
        client._azure_streaming = registered
        litellm.utils._invalidate_model_cost_lowercase_map()


@pytest.fixture
def unstreamable(litellm_map):
    """A model litellm's map lists, routes, and marks as not streaming natively.

    Registered rather than written straight into ``model_cost``, because that is
    how litellm learns a model: ``register_model`` also adds it to the openai
    list ``get_llm_provider`` reads, so a bare name routes — which is what makes
    ``gpt-5-pro`` reach the fake stream rather than fail at routing.
    """
    litellm_map.register_model(
        {
            UNSTREAMABLE: {
                "litellm_provider": "openai",
                "mode": "responses",
                "supports_native_streaming": False,
            }
        }
    )
    assert litellm_map.get_llm_provider(model=UNSTREAMABLE)[1] == "openai"
    return UNSTREAMABLE


@pytest.fixture
def wire(monkeypatch):
    """Every request httpx would send, and nothing actually sent."""
    sent = []

    def _send(http_client, request, *args, **kwargs):
        sent.append(request)
        return httpx.Response(
            400, request=request, json={"error": {"message": "should not be sent"}}
        )

    monkeypatch.setattr(httpx.Client, "send", _send)
    return sent


class TestWhatLitellmWouldDo:
    """The premise the refusal and the preset test both rest on."""

    def test_the_fake_stream_still_follows_supports_native_streaming(self, litellm_map):
        """If litellm starts deciding this some other way, the checks built on
        ``supports_native_streaming`` would pass while production went silent.

        Azure's responses configs subclass this one without overriding the
        decision, which is why one check covers both routes.
        """
        config = litellm_map.OpenAIResponsesAPIConfig()
        for model in ("gpt-5.4", "gpt-5-pro", UNLISTED):
            streams = litellm_map.utils.supports_native_streaming(
                model=model, custom_llm_provider="openai"
            )
            faked = config.should_fake_stream(
                model=model, stream=True, custom_llm_provider="openai"
            )
            assert faked is (not streams), model
        assert issubclass(
            litellm_map.AzureOpenAIResponsesAPIConfig,
            litellm_map.OpenAIResponsesAPIConfig,
        )

    def test_a_bare_name_the_map_does_not_list_cannot_even_be_routed(self, litellm_map):
        """Which is why the refusal covers routing too, and says not to reach for
        the prefix litellm's own error suggests."""
        with pytest.raises(Exception, match="LLM Provider NOT provided"):
            litellm_map.get_llm_provider(model="gpt-5.7-nova")


class TestTheRefusal:
    def test_a_model_the_map_streams_is_not_refused(self, litellm_map):
        assert client._refuse_fake_stream("gpt-5.6-luna") is None

    def test_a_model_the_map_will_not_stream_is_refused(self, unstreamable):
        with pytest.raises(client.FakeStreamRefused) as caught:
            client._refuse_fake_stream(unstreamable)
        message = str(caught.value)
        assert unstreamable in message
        assert "marks it as not streaming natively" in message
        assert "fake the stream" in message

    def test_a_model_the_map_does_not_list_is_refused(self, litellm_map):
        with pytest.raises(client.FakeStreamRefused) as caught:
            client._refuse_fake_stream(f"openai/{UNLISTED}")
        assert "does not list it" in str(caught.value)

    def test_an_unroutable_name_is_refused_with_what_not_to_do(self, litellm_map):
        with pytest.raises(client.FakeStreamRefused) as caught:
            client._refuse_fake_stream("gpt-5.7-nova")
        message = str(caught.value)
        assert "cannot route" in message
        assert "openai/" in message

    def test_the_message_names_the_map_it_asked(self, litellm_map):
        """Which map answered is not this repo's choice: litellm fetches the live
        one at import and falls back to the copy in its wheel. The suite forces
        the bundled one, and the message has to be able to say so."""
        with pytest.raises(client.FakeStreamRefused) as caught:
            client._refuse_fake_stream(f"openai/{UNLISTED}")
        assert "bundled model map" in str(caught.value)
        assert client._model_map_source() == "litellm's bundled model map"


class TestNothingIsSent:
    def test_the_call_fails_without_a_request(self, unstreamable, wire):
        result = _call(unstreamable)
        assert wire == []
        assert result["failed"] is True
        assert unstreamable in result["error"]
        assert result["model"] == unstreamable
        assert result["tokens"] == {"prompt": 0, "completion": 0}

    def test_it_is_not_retried_and_no_fallback_is_tried(self, unstreamable):
        """A model that cannot stream is a misconfiguration, not a capacity
        error: retrying it spends the delay to learn the same thing, and walking
        the fallback chain would review the domain with a model nobody chose."""
        with patch.object(client.litellm, "responses") as responses:
            result = _call(unstreamable, retry=True, retry_delay=0)
        assert responses.call_args_list == []
        assert result["failed"] is True
        assert "fallback_from" not in result
        assert "discarded_attempts" not in result

    def test_no_stream_is_recorded_as_having_died(self, unstreamable):
        """The refusal happens ahead of the timing record, as a missing
        service-account file does: there was no stream to have stalled."""
        result = _call(unstreamable)
        assert "stream_timing" not in result

    def test_the_other_providers_are_untouched(self, litellm_map):
        """The fake stream is a ``responses()`` behaviour, so the five providers
        on ``completion()`` still call whatever model they are given — litellm
        prefixes those names, and an unlisted one is the provider's problem to
        report."""
        with patch.object(client.litellm, "completion") as completion:
            completion.side_effect = RuntimeError("reached litellm")
            result = _call(UNLISTED, provider="mistral")
        assert completion.call_args_list, "the completion surface was not reached"
        assert result["failed"] is True
        assert "reached litellm" in result["error"]


class TestAzureKeepsStreaming:
    """The Azure workaround registers the deployment; the refusal must not undo
    it, and must still catch a deployment serving a model the map will not
    stream."""

    def _azure(self, **overrides):
        return {
            "provider": "azure",
            "model": "gpt-5.6-terra",
            "endpoint": "https://my-resource.openai.azure.com",
            "deployment": "my-deployment",
            **overrides,
        }

    def test_a_registered_deployment_is_not_refused(self, litellm_map):
        client._stream_azure_deployment("my-deployment", "gpt-5.6-terra")
        assert client._refuse_fake_stream("azure/my-deployment") is None

    def test_a_deployment_reaches_the_wire(self, litellm_map, wire):
        result = _call(provider="openai", provider_config=self._azure())
        (request,) = wire
        assert request.url.host == "my-resource.openai.azure.com"
        assert json.loads(request.content)["stream"] is True
        # The 400 the wire fixture answers with, not a refusal.
        assert result["failed"] is True
        assert "should not be sent" in result.get("error_body", "")

    def test_a_deployment_serving_an_unstreamable_model_is_refused(
        self, unstreamable, wire
    ):
        """``_stream_azure_deployment`` copies the served model's entry, so a
        deployment inherits its answer rather than overriding it."""
        result = _call(
            provider="openai", provider_config=self._azure(model=unstreamable)
        )
        assert wire == []
        assert result["failed"] is True
        assert "azure/my-deployment" in result["error"]


class TestPendingModels:
    """Models litellm's main branch lists and its released wheel does not.

    ``client._register_pending_models`` hands litellm its own entries for them at
    import, so a preset naming one streams whichever map litellm loaded. These
    run under the bundled map (the package conftest forces it), the one that
    lacks them.
    """

    def _pending(self):
        with open(client._PENDING_MODELS, encoding="utf-8") as fh:
            return json.load(fh)

    def test_each_routes_and_streams_natively(self, litellm_map):
        for model in self._pending():
            assert litellm_map.get_llm_provider(model=model)[1] == "openai", model
            assert client._refuse_fake_stream(model) is None, model

    def test_a_pending_model_reaches_the_wire_streaming(self, litellm_map, wire):
        _call(
            model="gpt-6.1-sol",
            provider_config={"model": "gpt-6.1-sol", "reasoning_effort": "max"},
        )
        (request,) = wire
        body = json.loads(request.content)
        assert request.url.host == "api.openai.com"
        assert body["model"] == "gpt-6.1-sol"
        assert body["stream"] is True
        assert body["reasoning"]["effort"] == "max"

    def test_an_entry_the_loaded_map_already_has_wins(self, litellm_map):
        """A live map, or a later wheel, knows better than a copied entry."""
        mine = {"litellm_provider": "openai", "mode": "responses", "marker": 1}
        litellm_map.model_cost["gpt-6-luna"] = mine
        client._register_pending_models(litellm_map)
        assert litellm_map.model_cost["gpt-6-luna"] is mine

    def test_the_installed_wheel_still_lacks_one(self):
        """When this fails, the shim has outlived its reason: delete
        ``_register_pending_models``, its JSON file, and this class."""
        import importlib.resources

        bundled = json.loads(
            importlib.resources.files("litellm")
            .joinpath("model_prices_and_context_window_backup.json")
            .read_text(encoding="utf-8")
        )
        missing = set(self._pending()) - set(bundled)
        assert missing, (
            "litellm's bundled map now lists every model in "
            "litellm_pending_models.json; remove the shim"
        )

    def test_the_copied_entries_carry_litellms_license_notice(self):
        """The file is litellm's data, copied verbatim, and MIT asks that the
        copyright and permission notice travel with a copy. JSON cannot hold a
        comment, so the notice is the file beside it; it goes when the file does."""
        notice = client._PENDING_MODELS.with_name(
            client._PENDING_MODELS.stem + ".NOTICE.md"
        )
        text = notice.read_text(encoding="utf-8")
        assert "Copyright (c) 2023 Berri AI" in text
        assert "Permission is hereby granted, free of charge" in text
        assert "BerriAI/litellm" in text
        for model in self._pending():
            assert model in text, f"the notice does not name {model}"
