"""The stream_read_timeout overrides in presets.yaml must reach the socket.

Every override in that file was written in response to a real production
timeout — perplexity 500 after three incremental bumps were each exceeded within
days, gemini 260 after a live Vertex AI call died at 205.78s with grounding and
a 16k thinking budget stacking their silent phases, mistral 200 after a marginal
124s failure against the 120s default.

The litellm migration moved the timeout plumbing (``requests``'
``timeout=(connect, read)`` tuple became an ``httpx.Timeout``), which is exactly
the kind of change that can leave the YAML looking authoritative while nothing
reads it any more. That failure is invisible in normal operation: the run just
starts timing out again, and it looks like the provider got slower.

So this drives the real presets.yaml through the real config loader into the
real shim, and asserts on the timeout object handed to litellm.
"""

from unittest.mock import patch

import httpx
import pytest

from ci_article_review.config_loader import _load_presets_from_yaml
from ci_core.llm import client


def _model_config(preset_name, provider):
    """The merged model config a preset produces for one provider."""
    preset = _load_presets_from_yaml()[preset_name]
    return dict((preset.get("models") or {}).get(provider) or {})


def _timeout_handed_to_litellm(provider, model_cfg):
    """Call the shim with ``model_cfg`` and return the timeout litellm received."""
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        # Minimal well-formed stream: one JSON chunk, then stop.
        from types import SimpleNamespace

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

    target = "responses" if provider == "openai" else "completion"
    with patch.object(client.litellm, target, side_effect=_capture):
        client.call(
            provider,
            "sys",
            "user",
            "key",
            retry=False,
            retry_delay=0,
            provider_config=model_cfg,
        )
    return seen["timeout"]


# The overrides as presets.yaml declares them today. Written out rather than
# read from the file so that deleting an override fails this test instead of
# quietly shrinking its coverage.
_DECLARED_OVERRIDES = [
    ("thorough", "mistral", 200),
    ("thorough", "perplexity", 500),
    ("thorough", "grok", 500),
    ("maximum", "gemini", 260),
    ("maximum", "mistral", 200),
    ("maximum", "perplexity", 500),
    ("maximum", "grok", 500),
]


@pytest.mark.parametrize("preset,provider,expected", _DECLARED_OVERRIDES)
def test_preset_override_reaches_the_socket(preset, provider, expected):
    cfg = _model_config(preset, provider)
    assert cfg.get("stream_read_timeout") == expected, (
        f"presets.yaml no longer sets stream_read_timeout={expected} for "
        f"{preset}.{provider} — update this test deliberately, with the "
        f"measurement that justifies the new value."
    )

    timeout = _timeout_handed_to_litellm(provider, cfg)
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == expected


@pytest.mark.parametrize(
    "preset,provider,expected",
    [
        # OpenAI deliberately carries no override at thorough or maximum: the
        # Responses API streams reasoning-summary deltas during the thinking
        # phase, live-verified against xhigh with a worst gap of ~8s.
        ("thorough", "openai", 120),
        ("maximum", "openai", 120),
        # Grok carries one at thorough and maximum, where it runs at effort
        # high — 354.1s of silence measured before output, and the call then
        # completed. `balanced` runs it at low, which that experiment did not
        # measure, so that tier is left on the default deliberately.
        ("balanced", "grok", 120),
        # Claude has never needed one.
        ("maximum", "claude", 120),
        # Gemini at thorough is grounded but has no thinking budget stacked on
        # top, so the grounded default still covers it.
        ("thorough", "gemini", 160),
    ],
)
def test_providers_without_an_override_get_the_right_default(
    preset, provider, expected
):
    cfg = _model_config(preset, provider)
    assert "stream_read_timeout" not in cfg
    assert _timeout_handed_to_litellm(provider, cfg).read == expected


def test_every_override_in_the_file_is_covered_here():
    """A new override added to presets.yaml must be verified, not just written.

    The whole failure mode this file exists for is an override that looks
    configured and is not plumbed through.
    """
    found = set()
    for preset_name, body in _load_presets_from_yaml().items():
        for provider, cfg in (body.get("models") or {}).items():
            if isinstance(cfg, dict) and "stream_read_timeout" in cfg:
                found.add((preset_name, provider, cfg["stream_read_timeout"]))

    assert found == set(_DECLARED_OVERRIDES), (
        "presets.yaml's stream_read_timeout overrides have changed. Add the new "
        "ones to _DECLARED_OVERRIDES so they are verified end to end.\n"
        f"  in the file but not tested: {sorted(found - set(_DECLARED_OVERRIDES))}\n"
        f"  tested but not in the file:  {sorted(set(_DECLARED_OVERRIDES) - found)}"
    )
