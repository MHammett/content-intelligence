"""Every openai model this package's presets name must be one litellm streams.

openai is the one provider this repo calls through ``litellm.responses`` rather
than ``litellm.completion``, because Chat Completions sends zero bytes while a
reasoning model thinks and the first-byte allowance is all that stands between a
slow call and a hung one. But ``responses(stream=True)`` streams only for a
model litellm's model map says streams. For any other it sends ONE
non-streaming request and replays the finished answer as a fake stream
(UPSTREAM.md #9): nothing arrives until the model is done, the allowance cuts
the call off, and it is reported as a timeout rather than as a model choice.

ci-article-review holds its own presets.yaml to that rule in
``test_preset_native_streaming.py``. This file is the same rule for the presets
this package ships, which name openai models of their own: a tier re-pointed at
``gpt-5-pro`` or ``o1-pro`` — the two openai models both the bundled and the
live map mark ``supports_native_streaming: false`` — would take out every openai
call a style-profile run makes, quietly, and the log would blame the provider.

What this does and does not stand in front of:

* it reads the shipped file through ``bootstrap._load_presets``, this package's
  own loader, so a tier added or re-pointed there is covered without editing
  this file;
* it runs against the map litellm *bundles* (the package conftest forces it),
  which is the copy production falls back to when its import-time fetch of the
  live map fails. The live map can be more generous, so the bundled map is the
  floor, and the floor is what a test should hold;
* it does not cover the model a user.yaml names, nor openai on Azure, where
  ``_apply_preset_models`` leaves ``model`` alone and the call is routed by
  deployment. Those are the runtime guard's, below.

The assertion is the runtime guard itself — ``ci_core.llm.client``'s
``_refuse_fake_stream``, which ``client.call`` runs before every ``responses()``
request and which this package's calls already go through. Asking it here, of a
model nothing is about to send, is how a bad preset edit fails in CI instead of
on the wire. Its own behaviour is covered by ci-core's
``test_llm_fake_stream_guard.py``.
"""

import pytest

from ci_core.llm import client
from ci_style_profile import bootstrap


def _openai_models():
    """``(where, model)`` for every openai model the shipped presets name."""
    named = []
    for preset, body in bootstrap._load_presets().items():
        cfg = (body.get("models") or {}).get("openai")
        # `enabled: false` is honoured by callers.py and synthesize.py, so a
        # tier that switches openai off names nothing that can be sent.
        if cfg is None or cfg.get("enabled") is False:
            continue
        # Through _resolve_model, so this is the name the client would send:
        # the tier's own, or the client's default where a tier names none.
        named.append(
            (
                f"ci-style-profile presets.yaml {preset}",
                client._resolve_model("openai", None, cfg),
            )
        )
    return named


def _assert_streams_natively(where, model):
    """Assert litellm streams ``model`` natively, or explain what would happen.

    ``_refuse_fake_stream`` is the client's own check, so a preset that passes
    here is one the runtime guard will not refuse. What this adds is where the
    model was named, which the guard cannot know.
    """
    name = client._qualified("openai", model)
    try:
        client._refuse_fake_stream(name)
    except client.FakeStreamRefused as refused:
        raise AssertionError(
            f"{where} names openai model {model!r}, and litellm's bundled model "
            f"map gives it no native streaming. litellm.responses(stream=True) "
            f"would then send one non-streaming request and replay the finished "
            f"answer as a fake stream, so nothing would arrive while the model "
            f"worked and stream_read_timeout would cut every openai call a "
            f"style-profile run makes short, and report a timeout rather than "
            f"this. Name a model the bundled map streams, or upgrade litellm so "
            f"that its bundled map lists this one. See UPSTREAM.md #9.\n\n"
            f"litellm's own verdict, from the guard that would refuse the call "
            f"at runtime (ci_core.llm.client._refuse_fake_stream): {refused}"
        ) from refused


def test_the_map_under_test_is_the_one_litellm_bundles():
    """The premise of the checks below, asserted rather than assumed.

    ``LITELLM_LOCAL_MODEL_COST_MAP`` is set in this package's conftest. Without
    it litellm would answer from whatever it could fetch, and a model only the
    live map lists would pass here while production's fallback map faked its
    stream.
    """
    from litellm.litellm_core_utils.get_model_cost_map import (
        get_model_cost_map_source_info,
    )

    client._litellm()  # the import that loads the map
    assert get_model_cost_map_source_info()["source"] == "local"


@pytest.mark.parametrize("where,model", _openai_models())
def test_the_openai_models_the_presets_name_stream_natively(where, model):
    _assert_streams_natively(where, model)


def test_the_check_fails_for_a_model_litellm_will_not_stream(monkeypatch):
    """Proof that the assertion above can fail, and that it names the model.

    A model invented here and marked the way litellm marks ``gpt-5-pro``, rather
    than ``gpt-5-pro`` itself: what is being tested is the check, which should
    keep meaning the same when the map's own flags move.
    """
    litellm = client._litellm()
    invented = "gpt-ci-does-not-stream"
    monkeypatch.setitem(
        litellm.model_cost,
        invented,
        {
            "litellm_provider": "openai",
            "mode": "responses",
            "supports_native_streaming": False,
        },
    )
    # litellm caches lookups off model_cost, so both the added entry and its
    # removal have to be made visible.
    litellm.utils._invalidate_model_cost_lowercase_map()
    try:
        with pytest.raises(AssertionError) as caught:
            _assert_streams_natively("ci-style-profile presets.yaml maximum", invented)
    finally:
        monkeypatch.undo()
        litellm.utils._invalidate_model_cost_lowercase_map()
    assert invented in str(caught.value)
    assert "fake stream" in str(caught.value)
