"""Every openai model this repo names must be one litellm streams natively.

openai is the one provider on ``litellm.responses`` rather than
``litellm.completion``, because Chat Completions sends zero bytes while a
reasoning model thinks — 79.1s of total silence, measured — and the first-byte
allowance is all that stands between a slow call and a hung one. But
``responses(stream=True)`` streams only for a model litellm's model map says
streams. For any other it sends ONE non-streaming request and replays the
finished answer as a fake stream (UPSTREAM.md #9), which is the same silence by
another route: nothing arrives until the model is done, the allowance cuts the
call off, and it is reported as a timeout rather than as a model choice.

So a cost preset moving openai to a model litellm's map has not caught up with —
or to one it marks ``supports_native_streaming: false``, as it does ``gpt-5-pro``
and ``o1-pro`` — takes out every openai call in the ensemble, quietly, and the
report blames the provider. That is what these tests stand in front of.

Two things they deliberately do:

* they read presets.yaml through the config loader, so a preset added or
  re-pointed there is covered without editing this file;
* they run against the map litellm *bundles* (the package conftest forces it),
  which is the copy production falls back to when its import-time fetch of the
  live map fails. The live map can be more generous — on 2026-09-27 it streamed
  ``openai/gpt-5.7-nova``, which the bundled map faked — so the bundled map is
  the floor, and the floor is what a test should hold.

Because that map is chosen at runtime, and because user.yaml can name a model of
its own, the client asks litellm the same question per call and refuses before
sending: ``ci_core.llm.client._refuse_fake_stream``, covered by ci-core's
``test_llm_fake_stream_guard.py``.
"""

import pytest

from ci_article_review.config_loader import _load_presets_from_yaml
from ci_core.llm import client


def _openai_models():
    """``(where, model)`` for every openai model this repo itself names."""
    named = []
    for preset, body in _load_presets_from_yaml().items():
        cfg = (body.get("models") or {}).get("openai")
        if cfg is None or cfg.get("enabled") is False:
            continue
        # Through _resolve_model, so this is the name the client would send:
        # the preset's own, or the client's default where a preset names none.
        named.append(
            (f"presets.yaml {preset}", client._resolve_model("openai", None, cfg))
        )
    spec = client._PROVIDERS["openai"]
    named.append(("client._PROVIDERS default_model", spec["default_model"]))
    named += [("client._PROVIDERS fallback", m) for m in spec["fallbacks"]]
    return named


def _assert_streams_natively(where, model):
    """Assert litellm streams ``model`` natively, or explain what would happen."""
    litellm = client._litellm()
    name = client._qualified("openai", model)
    streams = litellm.utils.supports_native_streaming(
        model=name, custom_llm_provider="openai"
    )
    assert streams is True, (
        f"{where} names openai model {model!r}, and litellm's bundled model map "
        f"gives it no native streaming: supports_native_streaming({name!r}, "
        f"'openai') returned {streams!r}. litellm.responses(stream=True) would "
        f"then send one non-streaming request and replay the finished answer as a "
        f"fake stream, so nothing would arrive while the model worked and "
        f"stream_read_timeout would cut every openai call in the ensemble short, "
        f"and report a timeout rather than this. (If the map does not list the model "
        f"at all, a bare name fails sooner still: litellm cannot route it.) Name "
        f"a model the bundled map streams, or upgrade litellm so that its bundled "
        f"map lists this one. See UPSTREAM.md #9 and "
        f"ci_core.llm.client._refuse_fake_stream."
    )


def test_the_map_under_test_is_the_one_litellm_bundles():
    """The premise of the checks below, asserted rather than assumed.

    ``LITELLM_LOCAL_MODEL_COST_MAP`` is set in the package conftest. Without it
    litellm would answer from whatever it could fetch, and a model only the live
    map lists would pass here while production's fallback map faked its stream.
    """
    from litellm.litellm_core_utils.get_model_cost_map import (
        get_model_cost_map_source_info,
    )

    client._litellm()  # the import that loads the map
    assert get_model_cost_map_source_info()["source"] == "local"


@pytest.mark.parametrize("where,model", _openai_models())
def test_the_openai_models_this_repo_names_stream_natively(where, model):
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
            _assert_streams_natively("presets.yaml maximum", invented)
    finally:
        monkeypatch.undo()
        litellm.utils._invalidate_model_cost_lowercase_map()
    assert invented in str(caught.value)
    assert "fake stream" in str(caught.value)
