"""Tests for probe — checking preset models answer before a run is paid for.

The module was rewritten on 2026-09-04 after a live run showed it could not do
its job. It had no console-script entry and 0% coverage; its model lists were
hardcoded and had drifted to include `o4-mini`, `o3`, `gpt-4o` and
`gemini-3.5-flash` while carrying no claude model at all; it read credentials
straight from `os.getenv`, bypassing the project's precedence; and it sent a
payload of its own design, so it reported `gpt-5.6-terra OK` for a model the
pipeline could not call — the run sent `temperature` and the model refused it,
but the probe never sent one.

It now resolves each preset through the same loader the pipeline uses and calls
through `ci_core.llm.client.call`, so what it verifies is what a run will do.

Everything here is offline. Nothing in this file makes a provider call.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ci_article_review import probe

_PRESETS_PATH = Path(probe.__file__).parent / "configs" / "presets.yaml"


def _presets():
    return yaml.safe_load(_PRESETS_PATH.read_text(encoding="utf-8")) or {}


class TestPresetDiscovery:
    """Presets and providers are read from the shipped config, not listed here.

    The previous version hardcoded both and drifted until it was probing models
    no preset used while missing the three most expensive ones a maximum run
    calls.
    """

    def test_every_preset_in_the_config_is_offered(self):
        assert set(probe.preset_names()) == set(_presets())

    def test_every_provider_named_by_any_preset_is_offered(self):
        expected = set()
        for cfg in _presets().values():
            expected.update(cfg.get("models") or {})
        assert set(probe.known_providers()) == expected

    def test_claude_is_among_them(self):
        """It was absent entirely while running four review domains."""
        assert "claude" in probe.known_providers()

    def test_the_expensive_maximum_models_are_reachable(self):
        maximum = (_presets().get("maximum", {}).get("models")) or {}
        named = {c.get("model") for c in maximum.values() if isinstance(c, dict)}
        for model in ("gpt-5.6-sol", "claude-opus-5", "gemini-2.5-pro"):
            assert model in named, model


class TestCredentialsComeFromTheProjectsOwnResolution:
    """A probe testing a different credential than the run uses is worse than
    no probe: it reports success about a key nothing will call.

    It read `os.getenv` directly, so on a machine with a provider key set in
    both `.env` and the OS environment — which this repo already warns about
    for `WP_USER` — the two silently diverged.
    """

    def test_probe_no_longer_reads_the_environment_itself(self):
        """Checks for the *call*, not the string — the module docstring
        explains the old behaviour and legitimately names it."""
        import re

        source = Path(probe.__file__).read_text(encoding="utf-8")
        calls = re.findall(r"os\.getenv\s*\(", source)
        assert not calls, (
            "probe reads the environment directly again, bypassing the "
            "precedence load_user_config implements"
        )

    def test_keys_are_taken_from_the_resolved_config(self):
        keys = {"openai": {"api_key": "sk-resolved"}}
        assert probe._api_key_for("openai", keys) == "sk-resolved"

    def test_a_missing_key_is_empty_rather_than_an_error(self):
        assert probe._api_key_for("grok", {}) == ""
        assert probe._api_key_for("grok", {"grok": {}}) == ""


class TestWhatTheProbeSends:
    """It must send what the pipeline sends, or it cannot catch what the
    pipeline hits."""

    def _capture(self, model_cfg):
        seen = {}

        def _fake_call(provider, system, prompt, api_key, **kwargs):
            seen.update(kwargs)
            seen["provider"] = provider
            seen["api_key"] = api_key
            return {"failed": False, "data": {"ok": True}, "model": "m", "tokens": {}}

        with patch.object(probe.client, "call", side_effect=_fake_call):
            probe.probe_provider("openai", model_cfg, "sk-test")
        return seen

    def test_it_goes_through_the_pipelines_client(self):
        seen = self._capture({"model": "gpt-5.6-terra"})
        assert seen["model"] == "gpt-5.6-terra"
        assert seen["api_key"] == "sk-test"

    def test_the_resolved_reasoning_effort_is_passed_through(self):
        """The temperature bug turned on exactly this: openai calls with no
        reasoning effort took a different branch and were rejected."""
        seen = self._capture({"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"})
        assert seen["provider_config"]["reasoning_effort"] == "xhigh"

    def test_live_search_is_disabled(self):
        """The pipeline resolves web_search to a bool per domain; the raw config
        holds a list, which is truthy. Passing it through made every openai
        probe run a real search — 4,487 prompt tokens for a one-line question."""
        seen = self._capture({"model": "gpt-5.6-sol", "web_search": ["fact_check"]})
        assert seen["provider_config"]["web_search"] is False

    def test_it_does_not_retry(self):
        """A probe wants the first answer. Retrying hides the transient the run
        is about to hit."""
        seen = self._capture({"model": "gpt-5.6-terra"})
        assert seen["retry"] is False

    def test_a_schema_is_requested_in_the_shape_the_client_expects(self):
        seen = self._capture({"model": "gpt-5.6-terra"})
        schema = seen["response_schema"]
        assert set(schema) == {"name", "schema"}
        assert schema["schema"]["required"] == ["ok"]

    def test_the_caller_config_is_not_mutated(self):
        """Disabling search must not edit the dict the caller still holds."""
        cfg = {"model": "gpt-5.6-sol", "web_search": ["fact_check"]}
        self._capture(cfg)
        assert cfg["web_search"] == ["fact_check"]

    def test_the_prompt_stays_ordinary(self):
        """A terser instruction tripped xAI's content filter outright and
        reported grok unreachable when it was fine."""
        assert "Please reply" in probe.PROBE_PROMPT


class TestReporting:
    def _run(self, result):
        with patch.object(probe.client, "call", return_value=result):
            return probe.probe_provider("openai", {"model": "m"}, "k")

    def test_a_good_answer_passes(self):
        assert self._run(
            {"failed": False, "data": {"ok": True}, "model": "m", "tokens": {}}
        )

    def test_a_failed_call_fails(self):
        assert not self._run({"failed": True, "error": "HTTP 400", "model": "m"})

    def test_a_raised_exception_is_reported_rather_than_escaping(self):
        with patch.object(probe.client, "call", side_effect=RuntimeError("boom")):
            assert not probe.probe_provider("openai", {"model": "m"}, "k")

    def test_a_schema_pass_with_wrong_content_still_warns(self, capsys):
        self._run({"failed": False, "data": {"ok": False}, "model": "m", "tokens": {}})
        assert "not what was asked for" in capsys.readouterr().out


class TestArgumentParsing:
    """`ci-probe` with no arguments must probe everything.

    It never could: with `nargs="*"` argparse validates a list default as a
    single value, so the documented default invocation exited 2 with
    `invalid choice: "['all']"`.
    """

    def _run(self, argv, monkeypatch):
        seen = []

        def _fake_preset(preset, providers, pub, cfg):
            seen.append((preset, tuple(providers)))
            return True

        monkeypatch.setattr(probe, "probe_preset", _fake_preset)
        monkeypatch.setattr("sys.argv", ["ci-probe", *argv])
        with pytest.raises(SystemExit) as exc:
            probe.main()
        return seen, exc.value.code

    def test_no_arguments_probes_every_preset_and_provider(self, monkeypatch):
        seen, code = self._run([], monkeypatch)
        assert code == 0
        assert {p for p, _ in seen} == set(probe.preset_names())
        assert set(seen[0][1]) == set(probe.known_providers())

    def test_a_single_preset_can_be_chosen(self, monkeypatch):
        seen, _ = self._run(["--preset", "maximum"], monkeypatch)
        assert [p for p, _ in seen] == ["maximum"]

    def test_a_single_provider_can_be_chosen(self, monkeypatch):
        seen, _ = self._run(["claude", "--preset", "wide"], monkeypatch)
        assert seen[0][1] == ("claude",)

    def test_an_unknown_provider_is_rejected(self, monkeypatch):
        # _run already expects the SystemExit; argparse exits 2 on a bad value.
        _seen, code = self._run(["nope"], monkeypatch)
        assert code == 2

    def test_an_unknown_preset_is_rejected(self, monkeypatch):
        _seen, code = self._run(["--preset", "nope"], monkeypatch)
        assert code == 2

    def test_a_failing_probe_exits_nonzero(self, monkeypatch):
        monkeypatch.setattr(probe, "probe_preset", lambda *a: False)
        monkeypatch.setattr("sys.argv", ["ci-probe", "--preset", "wide"])
        with pytest.raises(SystemExit) as exc:
            probe.main()
        assert exc.value.code == 1
