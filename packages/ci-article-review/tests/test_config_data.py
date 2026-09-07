"""Integrity tests for ci-article-review's externalized config data.

  configs/presets.yaml -> config_loader.py (_COST_PRESETS)

The loader keeps a hardcoded fallback used only when the YAML is missing or
malformed.  These tests assert two things:

1. PARITY — the YAML content matches the hardcoded fallback, so a dropped or
   corrupt YAML silently using stale defaults can never go unnoticed.  If you
   intentionally change one, change the other and these tests confirm they agree.
2. STRUCTURE — the YAML parses and has the shape the loader expects, so a typo
   is caught at test time instead of during a real (billed) pipeline run.

The pricing / model_registry / timeouts data files moved to ci-core alongside
their loaders; their equivalents live in ci-core/tests/test_llm_config_data.py.
"""

import pytest

import os

import yaml
import ci_article_review

_CONFIGS = os.path.join(os.path.dirname(ci_article_review.__file__), "configs")


def _load(name):
    with open(os.path.join(_CONFIGS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestPresetsAreValid:
    """Validity, not parity (audit finding 14).

    This used to assert configs/presets.yaml matched a duplicate _COST_PRESETS
    dict in config_loader.py, so every model-name change had to be made twice.
    The duplicate is gone; the YAML is the single source of truth and the loader
    raises if it is missing or malformed. A parity test only proved two copies
    agreed, never that either was usable — these check that instead.
    """

    def test_every_documented_preset_exists(self):
        from ci_article_review.config_loader import _load_presets_from_yaml

        presets = _load_presets_from_yaml()
        expected = {"economy", "wide", "balanced", "thorough", "maximum"}
        assert expected <= set(presets), (
            f"presets.yaml is missing: {sorted(expected - set(presets))}. "
            "These are the choices offered by --cost-preset."
        )

    def test_each_preset_names_a_known_thoroughness(self):
        from ci_article_review.config_loader import _load_presets_from_yaml

        for name, body in _load_presets_from_yaml().items():
            assert body.get("thoroughness") in {"standard", "thorough", "maximum"}, (
                f"preset {name!r} has an unknown thoroughness "
                f"{body.get('thoroughness')!r}"
            )

    def test_each_preset_configures_only_real_providers(self):
        from ci_article_review.config_loader import _load_presets_from_yaml
        from ci_article_review.pipeline import _PROVIDERS

        for name, body in _load_presets_from_yaml().items():
            unknown = set(body.get("models", {})) - set(_PROVIDERS)
            assert not unknown, f"preset {name!r} names unknown providers: {unknown}"

    def test_a_missing_presets_file_raises_rather_than_degrading(self, tmp_path):
        import pytest

        from ci_core.config_helpers import PackagedConfigError
        from ci_article_review.config_loader import _load_presets_from_yaml

        with pytest.raises(PackagedConfigError):
            _load_presets_from_yaml(config_dir=tmp_path)


class TestPresetsStructure:
    _EXPECTED = {"economy", "wide", "balanced", "thorough", "maximum"}
    _VALID_THOROUGHNESS = {"standard", "thorough", "maximum"}

    def test_all_live_presets_present(self):
        data = _load("presets.yaml")
        assert self._EXPECTED.issubset(set(data.keys()))

    def test_each_preset_has_valid_thoroughness_and_models(self):
        data = _load("presets.yaml")
        for name in self._EXPECTED:
            body = data[name]
            assert body["thoroughness"] in self._VALID_THOROUGHNESS, (
                f"{name}: bad thoroughness"
            )
            assert isinstance(body["models"], dict) and body["models"], (
                f"{name}: no models"
            )

    #: Grok models that accept reasoning_effort. Before grok-4.6 (2026-08-12)
    #: Grok reasoning was model-selection only and the parameter is not valid,
    #: which is what this guard originally banned outright.
    _GROK_TAKES_EFFORT = {"grok-4.6"}

    def test_grok_reasoning_effort_only_on_models_that_accept_it(self):
        """Was a blanket ban, on the grounds that no Grok took the parameter.

        grok-4.6 does. Verified live 2026-09-05: litellm forwards it to xAI with
        no client-side reject and none of the `allowed_openai_params` hatch
        mistral needs, and grok honours it — ~380 completion tokens at low
        against ~1060 at high on one prompt. The ban is therefore narrowed to
        the models it is actually true of rather than dropped, because sending
        the parameter to grok-4.3 would still be a 400 for no gain.
        """
        data = _load("presets.yaml")
        for name, body in data.items():
            grok = body.get("models", {}).get("grok") or {}
            if "reasoning_effort" not in grok:
                continue
            assert grok.get("model") in self._GROK_TAKES_EFFORT, (
                f"{name}: grok {grok.get('model')!r} predates reasoning_effort"
            )
            assert grok["reasoning_effort"] in ("low", "medium", "high", "xhigh"), (
                f"{name}: grok reasoning_effort={grok['reasoning_effort']!r} invalid"
            )

    def test_mistral_reasoning_effort_is_high_or_none_only(self):
        # mistral-medium-3-5 only accepts "high" or "none"; "low"/"medium" return 400.
        data = _load("presets.yaml")
        for name, body in data.items():
            mistral = body.get("models", {}).get("mistral") or {}
            effort = mistral.get("reasoning_effort")
            if effort is not None:
                assert effort in ("high", "none"), (
                    f"{name}: mistral reasoning_effort={effort!r} invalid"
                )


class TestPartialPipelineBlockKeepsDefaults:
    """A partial `pipeline:` block must not discard the keys it omits.

    The defaults used to be the fallback argument of
    `user_config.get("pipeline", {...})`, which applies only when the key is
    absent entirely. So a config as ordinary as

        pipeline:
          cost_preset: maximum

    silently dropped every default: `task_timeout_seconds` became None, which is
    a TypeError inside timeout_model.compute_timeout, and `retry_on_failure`
    became None, disabling retries without saying so.
    """

    def _pipeline(self, user):
        from ci_article_review.config_loader import merge_configs

        return merge_configs(user, {})["pipeline"]

    def test_absent_block_gets_every_default(self):
        from ci_article_review.config_loader import PIPELINE_DEFAULTS

        assert self._pipeline({}) == PIPELINE_DEFAULTS

    @pytest.mark.parametrize(
        "partial",
        [
            {"cost_preset": "maximum"},
            {"grammar_pass": False},
            {"link_validation": False},
        ],
    )
    def test_a_partial_block_keeps_the_keys_it_did_not_mention(self, partial):
        merged = self._pipeline({"pipeline": dict(partial)})
        assert merged["task_timeout_seconds"] == 180
        assert merged["retry_on_failure"] is True
        assert merged["retry_delay_seconds"] == 10
        # ...and still carries what the user did say.
        for key, value in partial.items():
            assert merged[key] == value

    def test_an_explicit_null_block_is_not_a_crash(self):
        """`pipeline:` with nothing under it parses as None, not {}."""
        assert self._pipeline({"pipeline": None})["task_timeout_seconds"] == 180

    def test_user_values_still_win(self):
        merged = self._pipeline(
            {"pipeline": {"task_timeout_seconds": 1100, "retry_on_failure": False}}
        )
        assert merged["task_timeout_seconds"] == 1100
        assert merged["retry_on_failure"] is False

    def test_the_computed_backstop_survives_a_partial_block(self):
        """The actual failure: None reached compute_timeout and raised TypeError."""
        from ci_core.llm import timeout_model

        merged = self._pipeline({"pipeline": {"cost_preset": "maximum"}})
        budget = timeout_model.compute_all(
            2054,
            {"openai": {"model": "gpt-5.5", "reasoning_effort": "xhigh"}},
            merged["task_timeout_seconds"],
        )["openai"]
        assert budget > 0


class TestCostPresetSetsThoroughness:
    """A preset picks models AND ensemble size; both have to arrive.

    These two features interact badly and the failure is silent. #105 changed
    the pipeline defaults from a whole-block fallback to a per-key merge, which
    made `thoroughness` always present in the merged config — so
    `_apply_cost_preset`'s "only if the user did not set it" guard never fired
    again. Every preset kept picking its models correctly and then ran a
    `standard`-sized ensemble: `maximum` made 7 calls where it should make 30,
    at maximum-model prices, saying nothing.

    That is the same shape as the 0b bug the project spent two days on, so it
    gets a test rather than a comment.
    """

    def _pipeline(self, pipe):
        from ci_article_review.config_loader import merge_configs

        models = {
            p: {}
            for p in ("openai", "gemini", "mistral", "perplexity", "grok", "claude")
        }
        return merge_configs({"pipeline": pipe, "models": models}, {})["pipeline"]

    @pytest.mark.parametrize(
        "preset,expected",
        [
            ("economy", "standard"),
            ("wide", "thorough"),
            ("balanced", "thorough"),
            ("thorough", "thorough"),
            ("maximum", "maximum"),
            # Retired, and therefore carrying its replacement's thoroughness
            # rather than its own former `standard`. This pair is the whole
            # behaviour change the retirement warning exists to announce.
            ("standard", "thorough"),
        ],
    )
    def test_each_preset_brings_its_own_thoroughness(self, preset, expected):
        assert self._pipeline({"cost_preset": preset})["thoroughness"] == expected

    def test_an_explicit_thoroughness_still_wins(self):
        """The guard's real purpose, which must survive the fix."""
        merged = self._pipeline({"cost_preset": "maximum", "thoroughness": "standard"})
        assert merged["thoroughness"] == "standard"

    def test_no_preset_falls_back_to_the_default(self):
        assert self._pipeline({})["thoroughness"] == "standard"

    def test_the_maximum_preset_actually_schedules_the_full_ensemble(self):
        """The symptom, asserted end to end rather than via the config alone."""
        from ci_article_review.config_loader import merge_configs
        from ci_article_review.pipeline import _build_assignments

        models = {
            p: {}
            for p in ("openai", "gemini", "mistral", "perplexity", "grok", "claude")
        }
        merged = merge_configs(
            {"pipeline": {"cost_preset": "maximum"}, "models": models}, {}
        )
        keys = {p: {"api_key": "k"} for p in models}
        assignments = _build_assignments(
            merged["pipeline"]["thoroughness"], merged["models"], keys
        )
        assert len(assignments) == 30, (
            f"maximum scheduled {len(assignments)} calls, not 30 — the preset's "
            f"thoroughness is not reaching the assignment builder"
        )


class TestRetiredPresets:
    """A retired preset name keeps working, loudly.

    `standard` was retired 2026-09-05: `wide` beat it on every axis measured
    over three isolated runs each, at 55% of the cost. Deleting the name outright
    would turn a `cost_preset: standard` that had been working for months into a
    crash at config-load, so it maps to its replacement instead -- and warns,
    because six models over twelve calls is not what `standard` used to do.
    """

    def test_the_retired_name_is_gone_from_the_live_tiers(self):
        from ci_article_review.config_loader import preset_names

        assert "standard" not in preset_names()
        assert "wide" in preset_names()

    def test_it_resolves_to_its_replacement(self):
        from ci_article_review.config_loader import resolve_preset_name

        name, note = resolve_preset_name("standard")
        assert name == "wide"
        assert note and "retired" in note

    def test_a_live_preset_resolves_to_itself_without_a_note(self):
        from ci_article_review.config_loader import resolve_preset_name

        assert resolve_preset_name("wide") == ("wide", None)

    def test_an_existing_config_still_runs_and_warns(self, caplog):
        import logging

        from ci_article_review.config_loader import _apply_cost_preset

        models = {m: {"model": "x"} for m in ("openai", "gemini", "mistral")}
        with caplog.at_level(logging.WARNING):
            pipe, resolved = _apply_cost_preset(
                {"cost_preset": "standard"}, models, user_set={}
            )

        assert "retired" in caplog.text
        assert "wide" in caplog.text
        # The models really are wide's, not a no-op pass-through.
        assert resolved["openai"]["model"] == "gpt-5.6-luna"
        assert pipe["thoroughness"] == "thorough"

    def test_the_report_names_the_preset_that_actually_ran(self):
        """Otherwise Ensemble Width would print a tier whose models are absent."""
        from ci_article_review.config_loader import _apply_cost_preset

        pipe, _models = _apply_cost_preset(
            {"cost_preset": "standard"}, {"openai": {"model": "x"}}, user_set={}
        )
        assert pipe["cost_preset"] == "wide"

    def test_a_genuinely_unknown_preset_still_raises(self):
        import pytest

        from ci_article_review.config_loader import _apply_cost_preset

        with pytest.raises(ValueError, match="Unknown cost_preset"):
            _apply_cost_preset({"cost_preset": "nonsense"}, {}, user_set={})

    def test_the_cli_still_accepts_the_retired_name(self):
        """A saved script or shell alias must not break on a naming decision."""
        from ci_article_review.pipeline import build_parser

        args = build_parser().parse_args(
            ["--draft", "d.md", "--publication", "p", "--cost-preset", "standard"]
        )
        assert args.cost_preset == "standard"

    def test_the_cli_help_advertises_only_live_tiers(self):
        from ci_article_review.pipeline import build_parser

        for action in build_parser()._actions:
            if action.dest == "cost_preset":
                assert "standard" not in (action.metavar or "")
                assert "wide" in (action.metavar or "")
                break
        else:
            raise AssertionError("no --cost-preset argument found")


class TestAuthorNameIsADeclaredPublicationKey:
    """`author_name` was read by the pipeline before anything declared it.

    ``pipeline`` falls back to ``pub_config["author_name"]`` to tell citation
    verification who "I" is. That key appeared in no example config, no
    scaffolding, and no documentation — it worked only for the one config that
    happened to set it by hand. A second user had no way to discover it.
    """

    def _example(self):
        from pathlib import Path

        import yaml

        for parent in Path(__file__).resolve().parents:
            candidate = (
                parent
                / "packages"
                / "ci-article-review"
                / "src"
                / "ci_article_review"
                / "configs"
                / "publication.example.yaml"
            )
            if candidate.is_file():
                return yaml.safe_load(candidate.read_text(encoding="utf-8"))
        raise AssertionError("publication.example.yaml not found")

    def test_the_example_config_declares_it(self):
        """setup.py copies this file verbatim, so this is the scaffolding too."""
        assert "author_name" in self._example()

    def test_it_is_documented_in_the_configuration_reference(self):
        from pathlib import Path

        for parent in Path(__file__).resolve().parents:
            doc = parent / "docs" / "CONFIGURATION.md"
            if doc.is_file():
                assert "author_name" in doc.read_text(encoding="utf-8")
                return
        raise AssertionError("docs/CONFIGURATION.md not found")

    def test_the_pipeline_reads_the_key_the_example_declares(self):
        """Guards the two drifting apart — the whole failure being fixed."""
        from pathlib import Path

        for parent in Path(__file__).resolve().parents:
            src = (
                parent
                / "packages"
                / "ci-article-review"
                / "src"
                / "ci_article_review"
                / "pipeline.py"
            )
            if src.is_file():
                assert 'pub_config.get("author_name")' in src.read_text(
                    encoding="utf-8"
                )
                return
        raise AssertionError("pipeline.py not found")


# The near-miss *warning* these lines used to cover is gone: an unrecognised
# publication key is now an error, not a log line, and the near-miss spelling
# is folded into that error as a "did you mean" hint. One case here was
# deliberately reversed rather than moved — an unrelated custom key such as
# `author_bio` used to be explicitly allowed and is now rejected, with `x_` as
# the way to keep one on purpose. See tests/test_publication_config_strictness.py.
