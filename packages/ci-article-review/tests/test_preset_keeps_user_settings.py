"""A cost preset must not drop the per-model settings that belong to the user.

``_apply_cost_preset`` rebuilds each configured model from presets.yaml and
copies back only ``_INFRA_KEYS``. Every per-model setting that list left out was
dropped under every preset, which is every real configuration: ``max_tokens``,
then ``web_search``, then the two stream budgets and four search controls. The
last were found from a live ``wide`` run on 2026-09-20: gemini's ``stream_timing``
recorded the client's 160s grounded default, and user.yaml said 300.

Survival is tested for every preset in presets.yaml, so a new preset is covered
without an edit here. The guard at the bottom would have caught all three
incidents. It records every key the real consumers read from a model config, and
requires each one to be either preserved or owned by the preset.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ci_article_review.config_loader import (
    _INFRA_KEYS,
    _apply_cost_preset,
    _load_presets_from_yaml,
    merge_configs,
    preset_names,
)
from ci_core.llm import client, output_tokens, timeout_model

#: What a preset replaces whatever user.yaml says: which model runs, how hard it
#: reasons, and whether it runs at all. To change one of these under a preset,
#: the user sets it in ``pipeline.preset_overrides``. Every other key a consumer
#: reads belongs to the user and has to be in ``_INFRA_KEYS``.
_PRESET_OWNED = frozenset(
    {
        "model",
        "reasoning_effort",
        "effort",
        "thinking_budget",
        # Gemini 3.x's reasoning knob, in place of thinking_budget, and stated
        # by every tier — a reasoning flag, so the preset owns it as it owns
        # the budget it replaced.
        "thinking_level",
        # How many search-and-reason rounds Perplexity's Agent API may take:
        # how hard the seat works, set per tier like reasoning_effort.
        "max_steps",
        "enabled",
    }
)

_PRESETS = preset_names()

#: The two streaming budgets, which a preset may set as well as the user.
_BUDGET_KEYS = ("stream_read_timeout", "stream_gap_timeout")


def _running(preset):
    """The providers ``preset`` runs: every one it does not switch off."""
    models = _load_presets_from_yaml()[preset]["models"]
    return [p for p, cfg in models.items() if cfg.get("enabled") is not False]


def _merge(caplog, preset, models, overrides=None):
    """The merged model configs, with config_loader's warnings in ``caplog``."""
    pipeline = {"cost_preset": preset}
    if overrides:
        pipeline["preset_overrides"] = overrides
    user = {"pipeline": pipeline, "models": models}
    with caplog.at_level(logging.WARNING):
        return merge_configs(user, {})["models"]


def _stream_for(provider, model=None):
    """A minimal well-formed stream in the shape ``provider``'s surface reads."""
    if client._uses_agent_api(provider, model):
        return [
            SimpleNamespace(
                type="response.output_text.delta", delta='{"a": 1}', output_index=0
            ),
            SimpleNamespace(
                type="response.completed",
                response={"status": "completed", "output": [], "usage": None},
            ),
        ]
    if provider == "openai":
        return [
            SimpleNamespace(type="response.output_text.delta", delta='{"a": 1}'),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    usage=None, status="completed", incomplete_details=None
                ),
            ),
        ]
    choice = SimpleNamespace(
        delta=SimpleNamespace(content='{"a": 1}'), finish_reason="stop"
    )
    return [
        SimpleNamespace(
            choices=[choice],
            usage=None,
            citations=None,
            search_results=None,
            vertex_ai_grounding_metadata=None,
        )
    ]


def _call(provider, model_cfg):
    """Run ``client.call`` against a stubbed litellm, and a stubbed Perplexity
    Agent API for the ids that go there: (result, timeout it got)."""
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _stream_for(provider)

    def _agent(api_key, timeout, params):
        seen.update(params, timeout=timeout)
        return _stream_for(provider, params["model"])

    target = "responses" if provider == "openai" else "completion"
    with (
        patch.object(client.litellm, target, side_effect=_capture),
        patch.object(client, "_perplexity_agent", side_effect=_agent),
    ):
        result = client.call(
            provider,
            "sys",
            "user",
            "key",
            retry=False,
            retry_delay=0,
            provider_config=model_cfg,
        )
    return result, seen["timeout"]


# ---------------------------------------------------------------------------
# Survival
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", [None, *_PRESETS])
def test_the_reported_gemini_config_keeps_its_300s(preset, caplog):
    """The check behind the finding, run as it was reported."""
    gemini = {
        "provider": "vertex_ai",
        "project": "p",
        "model": "gemini-2.5-flash",
        "stream_read_timeout": 300,
    }
    merged = _merge(caplog, preset, {"gemini": gemini})
    assert merged["gemini"]["stream_read_timeout"] == 300


@pytest.mark.parametrize("preset", _PRESETS)
@pytest.mark.parametrize("key", sorted(_INFRA_KEYS))
def test_every_preserved_key_survives_every_preset(preset, key):
    marker = f"user's {key}"
    models = {p: {"model": "anything", key: marker} for p in client.PROVIDERS}
    _, merged = _apply_cost_preset({"cost_preset": preset}, models)
    for provider in _running(preset):
        assert merged[provider][key] == marker, provider


@pytest.mark.parametrize("preset", _PRESETS)
def test_the_users_budgets_reach_the_socket_under_every_preset(preset, caplog):
    """End to end, as the finding was observed: through merge_configs and the
    real client, to the timeout litellm is handed and the ``stream_timing``
    record the run report keeps."""
    budgets = {"stream_read_timeout": 777, "stream_gap_timeout": 88}
    models = {p: {"model": "anything", **budgets} for p in client.PROVIDERS}
    merged = _merge(caplog, preset, models)
    for provider in _running(preset):
        result, timeout = _call(provider, merged[provider])
        assert timeout.read == 777, provider
        (timing,) = result["stream_timing"]
        assert timing["stream_read_timeout"] == 777, provider
        assert timing["stream_gap_timeout"] == 88, provider


# ---------------------------------------------------------------------------
# When the preset sets a budget too: the user's wins, and a lower one warns
# ---------------------------------------------------------------------------


def _budgets_the_presets_set():
    """(preset, provider, key, value) for every stream budget presets.yaml sets
    on a provider it runs."""
    found = []
    for name, body in _load_presets_from_yaml().items():
        for provider, cfg in body["models"].items():
            if cfg.get("enabled") is False:
                continue
            for key in _BUDGET_KEYS:
                if key in cfg:
                    found.append((name, provider, key, cfg[key]))
    return found


_BUDGETS = _budgets_the_presets_set()


def test_the_presets_set_budgets_for_the_tests_below_to_check():
    """Keeps the parametrized tests below from passing on an empty list."""
    assert _BUDGETS


@pytest.mark.parametrize("preset,provider,key,theirs", _BUDGETS)
class TestAUserBudgetUnderAPresetThatSetsOne:
    def test_a_lower_one_still_runs_and_is_warned_about(
        self, preset, provider, key, theirs, caplog
    ):
        mine = theirs - 1
        merged = _merge(caplog, preset, {provider: {key: mine}})
        assert merged[provider][key] == mine
        assert f"models.{provider}.{key} is {mine} in user.yaml" in caplog.text
        assert f"below the {preset} preset's {theirs}" in caplog.text

    def test_an_equal_or_higher_one_runs_silently(
        self, preset, provider, key, theirs, caplog
    ):
        for mine in (theirs, theirs + 40):
            merged = _merge(caplog, preset, {provider: {key: mine}})
            assert merged[provider][key] == mine
        assert key not in caplog.text

    def test_an_empty_one_discards_the_presets_and_says_so(
        self, preset, provider, key, theirs, caplog
    ):
        merged = _merge(caplog, preset, {provider: {key: None}})
        assert merged[provider][key] is None
        assert f"models.{provider}.{key} is empty in user.yaml" in caplog.text
        assert f"the {preset} preset's {theirs}" in caplog.text

    def test_without_one_the_presets_runs(self, preset, provider, key, theirs, caplog):
        merged = _merge(caplog, preset, {provider: {"model": "anything"}})
        assert merged[provider][key] == theirs
        assert key not in caplog.text

    def test_preset_overrides_has_the_last_word_and_no_warning(
        self, preset, provider, key, theirs, caplog
    ):
        merged = _merge(
            caplog,
            preset,
            {provider: {key: theirs - 1}},
            overrides={provider: {key: theirs - 2}},
        )
        assert merged[provider][key] == theirs - 2
        assert key not in caplog.text

    def test_a_provider_switched_off_is_not_warned_about(
        self, preset, provider, key, theirs, caplog
    ):
        _merge(caplog, preset, {provider: {"enabled": False, key: theirs - 1}})
        _merge(
            caplog,
            preset,
            {provider: {key: theirs - 1}},
            overrides={provider: {"enabled": False}},
        )
        assert key not in caplog.text


# ---------------------------------------------------------------------------
# The guard: nothing a consumer reads may be dropped by accident
# ---------------------------------------------------------------------------


class _Recording(dict):
    """A model config that notes every key anything asks it for."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked = set()

    def get(self, key, default=None):
        self.asked.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.asked.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.asked.add(key)
        return super().__contains__(key)


#: A second config per provider, for the routes a plain one does not take.
#: Azure and Vertex read keys of their own, and only on their own route.
_ALTERNATE_ROUTES = {
    "openai": {
        "provider": "azure",
        "endpoint": "https://r.openai.azure.com",
        "deployment": "d",
        "api_version": "2025-04-01-preview",
    },
    "mistral": {"provider": "azure", "endpoint": "https://m.inference.ai.azure.com"},
    "gemini": {
        "provider": "vertex_ai",
        "project": "p",
        "location": "us-central1",
        "credentials_file": "no-such-file.json",
    },
}


def _keys_the_consumers_read():
    """Every key read from a model config by the client and the sizing code.

    Each provider gets ``maximum``'s model and ``web_search: true``, the
    setting under which the most branches read: claude and grok only ask for
    ``search_context_size`` when they are going to search. Then again on its
    other route, where Azure and Vertex read settings of their own.
    """
    maximum = _load_presets_from_yaml()["maximum"]["models"]
    asked = set()
    for provider in client.PROVIDERS:
        model = maximum[provider]["model"]
        for consume in (
            lambda cfg: _call(provider, cfg),
            lambda cfg: output_tokens.compute_max_tokens(
                provider, cfg, "fact_check", 20000
            ),
            lambda cfg: output_tokens.calibration_ceiling(provider, cfg),
            lambda cfg: output_tokens.effort_warnings({provider: cfg}),
            lambda cfg: timeout_model.compute_all(20000, {provider: cfg}, 1100),
            lambda cfg: timeout_model.flag_stale_overrides(
                20000, {provider: cfg}, 1100
            ),
        ):
            for extra in ({}, _ALTERNATE_ROUTES.get(provider) or {}):
                cfg = _Recording(model=model, web_search=True, **extra)
                try:
                    consume(cfg)
                except Exception:
                    # A route that refuses the config — a credentials_file
                    # naming no file — read the keys it refused on first.
                    pass
                asked |= cfg.asked
    return asked


def test_every_key_a_consumer_reads_is_the_users_or_the_presets():
    asked = _keys_the_consumers_read()
    # Proves the recorder sees reads at all: a consumer that copied the config
    # before reading it would record nothing, and pass below for that reason.
    assert {"stream_read_timeout", "stream_gap_timeout", "search_mode"} <= asked

    unclassified = asked - _INFRA_KEYS - _PRESET_OWNED
    assert not unclassified, (
        f"Read from a model config, and dropped from user.yaml by every "
        f"cost_preset: {sorted(unclassified)}. Add each to "
        f"config_loader._INFRA_KEYS if the user sets it, or to _PRESET_OWNED "
        f"in this file if the preset should replace it."
    )


def test_every_key_a_preset_sets_is_the_users_or_the_presets():
    set_by_presets = {
        key
        for body in _load_presets_from_yaml().values()
        for cfg in body["models"].values()
        for key in cfg
    }
    assert set_by_presets <= _INFRA_KEYS | _PRESET_OWNED, sorted(
        set_by_presets - _INFRA_KEYS - _PRESET_OWNED
    )


def test_no_key_is_both():
    assert not _INFRA_KEYS & _PRESET_OWNED
