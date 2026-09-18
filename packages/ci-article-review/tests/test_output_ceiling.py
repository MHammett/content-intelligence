"""The output-token ceiling, from the pipeline's side.

ci_core.llm.output_tokens sizes the ceiling; these check that the pipeline sends
it per pass, leaves the providers that take none alone, respects an explicit
value, and — across every shipped preset — can always give a capped model the
wall-clock it needs to reach its ceiling.
"""

from unittest.mock import patch

import pytest
import yaml

from ci_article_review import config_loader
from ci_article_review.pipeline import _run_domain
from ci_core.llm import output_tokens, timeout_model

#: user.example.yaml's task_timeout_seconds — the ceiling the budgets clamp to.
SHIPPED_TASK_CEILING = 1100


@pytest.fixture(autouse=True)
def _no_model_map(monkeypatch):
    """No litellm lookups: the model's own limit is not what these test."""
    monkeypatch.setattr(output_tokens, "model_output_limit", lambda p, m: None)


def _sent_config(provider, domain, model_cfg, draft="x" * 27113):
    """Run one domain and return the provider_config the LLM layer received."""
    captured = {}

    def _fake_call(provider_name, system, user, api_key, **kwargs):
        captured.update(kwargs["provider_config"])
        return {"raw": "{}"}

    model_configs = {provider: dict(model_cfg)}
    with patch("ci_article_review.pipeline.llm.call_provider", side_effect=_fake_call):
        _run_domain(
            provider,
            domain,
            draft,
            {"title": "T"},
            {},
            {provider: {"api_key": "k"}},
            {},
            model_configs,
        )
    return captured, model_configs


OPUS_HIGH = {"model": "claude-opus-5", "effort": "high"}


class TestEachPassIsSentItsOwnCeiling:
    def test_a_reasoning_pass_gets_the_computed_ceiling(self):
        sent, _ = _sent_config("claude", "fact_check", OPUS_HIGH)
        expected = output_tokens.compute_max_tokens(
            "claude", OPUS_HIGH, "fact_check", 27113
        )
        assert sent["max_tokens"] == expected
        assert expected > 16000, "the flat ceiling it replaces"

    def test_the_ceiling_is_sized_per_domain_not_per_provider(self):
        """fact_check writes one entry per claim; red_team, three findings."""
        fact, _ = _sent_config("claude", "fact_check", OPUS_HIGH)
        red, _ = _sent_config("claude", "red_team", OPUS_HIGH)
        assert fact["max_tokens"] > red["max_tokens"]

    def test_it_scales_with_the_draft(self):
        short, _ = _sent_config("claude", "fact_check", OPUS_HIGH, draft="x" * 3000)
        long, _ = _sent_config("claude", "fact_check", OPUS_HIGH, draft="x" * 140000)
        assert long["max_tokens"] > short["max_tokens"]

    def test_a_provider_that_takes_no_ceiling_is_sent_none(self):
        sent, _ = _sent_config(
            "openai",
            "fact_check",
            {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
        )
        assert "max_tokens" not in sent

    def test_a_pass_with_no_reasoning_keeps_the_client_default(self):
        sent, _ = _sent_config(
            "claude", "fact_check", {"model": "claude-haiku-4-5-20251001"}
        )
        assert "max_tokens" not in sent

    def test_an_explicit_ceiling_wins(self):
        sent, _ = _sent_config(
            "mistral",
            "fact_check",
            {
                "model": "mistral-medium-3-5",
                "reasoning_effort": "high",
                "max_tokens": 9000,
            },
        )
        assert sent["max_tokens"] == 9000

    def test_the_shared_model_config_is_not_written_to(self):
        """One dict serves every domain the provider runs; a fact_check-sized
        ceiling written into it would be sent to red_team too."""
        _, model_configs = _sent_config("claude", "fact_check", OPUS_HIGH)
        assert "max_tokens" not in model_configs["claude"]


def _preset_models():
    """Every (preset, provider, model config) a shipped preset gives a ceiling.

    Decided from the effort alone rather than by computing a ceiling: this runs
    at collection, before the fixture above keeps litellm out of the lookup.
    """
    presets = config_loader._load_presets_from_yaml()
    for preset_name, preset in presets.items():
        for provider, cfg in preset["models"].items():
            if cfg.get("enabled") is False:
                continue
            if provider in output_tokens.CAPPED_PROVIDERS and output_tokens.effort_of(
                provider, cfg
            ):
                yield preset_name, provider, cfg


_CAPPED = list(_preset_models())


@pytest.mark.parametrize(
    "preset_name,provider,cfg", _CAPPED, ids=[f"{p}:{v}" for p, v, _ in _CAPPED]
)
def test_every_shipped_capped_model_can_reach_its_ceiling(preset_name, provider, cfg):
    """The wall clock must never be what stops a call short of its ceiling.

    compute_budget raises the budget to the time a healthy call needs to reach
    the ceiling, but only up to the task ceiling. If a preset's ceiling needs
    more than that, raising the token ceiling has quietly turned truncations
    into timeouts again — which keep no findings at all.
    """
    for chars in (1000, 18167, 27113, 62000, 135514, 250000):
        need = output_tokens.seconds_to_fill(provider, cfg, chars)
        budget = timeout_model.compute_budget(
            chars, provider, cfg, SHIPPED_TASK_CEILING
        )
        assert need <= SHIPPED_TASK_CEILING - 15, (preset_name, provider, chars, need)
        assert budget >= need, (preset_name, provider, chars, budget, need)


def test_the_shipped_presets_do_give_some_model_a_ceiling():
    """The parametrised test above proves nothing if this list is empty."""
    capped = {(p, prov) for p, prov, _ in _CAPPED}
    assert ("maximum", "claude") in capped
    assert ("maximum", "mistral") in capped


def test_the_task_ceiling_used_above_is_the_one_shipped():
    """Guards the constant above against drifting from the example config."""
    import pathlib

    import ci_article_review

    example = (
        pathlib.Path(ci_article_review.__file__).parent
        / "configs"
        / "user.example.yaml"
    )
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert data["pipeline"]["task_timeout_seconds"] == SHIPPED_TASK_CEILING
