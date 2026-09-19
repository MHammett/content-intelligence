"""presets.yaml must be the file a style-profile run really applies.

``bootstrap._load_presets`` read ``configs/presets.yaml`` beside bootstrap.py,
which is where it stopped being when the June relocation moved bootstrap.py into
``src/`` and left the YAML at the package root. On a miss it returned a
hardcoded table with no ``models`` block, so the preset-model merge in main()
never ran and every ``--preset`` used user.yaml's models as written. Nothing
errored, and the tier names still printed.

Everything here reads the real file. The CLI-level check, which drives main()
through it, is in test_bootstrap.py.
"""

from __future__ import annotations

import copy

import pytest

from ci_core.config_helpers import PackagedConfigError
from ci_style_profile import bootstrap


def _cli_tiers():
    """Every tier ``--preset`` accepts."""
    (action,) = [
        a for a in bootstrap._build_parser()._actions if "--preset" in a.option_strings
    ]
    return list(action.choices)


class TestTheShippedFileIsLoaded:
    def test_the_cli_and_the_file_name_the_same_tiers(self):
        """A tier added to one and not the other is rejected by argparse, or
        accepted and silently unmatched, so they have to agree."""
        assert set(bootstrap._load_presets()) == set(_cli_tiers())

    @pytest.mark.parametrize("tier", _cli_tiers())
    def test_a_tier_names_the_models_it_runs(self, tier):
        """The table this replaced had no `models` key, which was the bug: a
        tier that names no models is indistinguishable from a file never read."""
        models = bootstrap._load_presets()[tier].get("models")
        assert models, f"the shipped {tier!r} preset names no models"
        for provider, cfg in models.items():
            assert isinstance(cfg, dict) and cfg.get("model"), (tier, provider)

    def test_it_is_found_from_any_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert bootstrap._load_presets()["balanced"]["models"]


class TestAMissingOrBrokenFileRaises:
    """No fallback: one that answers with different data is how this went
    unnoticed. A packaged file that is absent is a broken install, and
    ci_core.config_helpers.load_packaged_yaml says so for every other packaged
    config."""

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(PackagedConfigError, match="missing"):
            bootstrap._load_presets(config_dir=tmp_path)

    def test_a_file_that_is_not_a_mapping_raises(self, tmp_path):
        (tmp_path / "presets.yaml").write_text(
            "- economy\n- maximum\n", encoding="utf-8"
        )
        with pytest.raises(PackagedConfigError, match="not a YAML mapping"):
            bootstrap._load_presets(config_dir=tmp_path)

    def test_a_tier_that_is_not_a_mapping_raises(self, tmp_path):
        (tmp_path / "presets.yaml").write_text("economy: [1, 2]\n", encoding="utf-8")
        with pytest.raises(PackagedConfigError, match="economy"):
            bootstrap._load_presets(config_dir=tmp_path)

    def test_a_models_block_that_is_not_a_mapping_raises(self, tmp_path):
        (tmp_path / "presets.yaml").write_text(
            "economy:\n  models: [claude]\n", encoding="utf-8"
        )
        with pytest.raises(PackagedConfigError, match="economy.*models"):
            bootstrap._load_presets(config_dir=tmp_path)


#: A user.yaml's models block, shaped like a real one: the keys a preset does not
#: name (provider, project, stream_read_timeout, web_search, enabled) are the ones
#: that have to survive the merge.
USER = {
    "claude": {"model": "claude-user-pick", "enabled": True},
    "openai": {"model": "gpt-user-pick", "web_search": ["fact_check"]},
    "gemini": {
        "provider": "vertex_ai",
        "model": "gemini-user-pick",
        "project": "p",
        "location": "us-central1",
        "stream_read_timeout": 300,
    },
    "grok": "grok-user-pick",
}


class TestApplyPresetModels:
    """The merge keeps what the preset leaves unset, and the preset wins the rest.

    Deliberate, and not ci-article-review's ``_INFRA_KEYS`` rule: the
    docstring of _apply_preset_models says why.
    """

    def test_the_presets_keys_win(self):
        out = bootstrap._apply_preset_models(
            USER, {"claude": {"model": "haiku", "effort": "low"}}
        )
        assert out["claude"]["model"] == "haiku"
        assert out["claude"]["effort"] == "low"

    def test_keys_the_preset_does_not_name_stay_the_users(self):
        out = bootstrap._apply_preset_models(
            USER, {"gemini": {"model": "flash"}, "openai": {"model": "terra"}}
        )
        assert out["gemini"] == {**USER["gemini"], "model": "flash"}
        assert out["openai"]["web_search"] == ["fact_check"]

    def test_a_key_the_preset_leaves_unset_is_kept_not_cleared(self):
        """An `effort: none` the preset does not overrule rides into its model,
        which is what the warning in main() is for."""
        user = {"claude": {"model": "opus", "effort": "none"}}
        out = bootstrap._apply_preset_models(user, {"claude": {"model": "sonnet"}})
        assert out["claude"] == {"model": "sonnet", "effort": "none"}

    def test_provider_is_never_overwritten(self):
        out = bootstrap._apply_preset_models(
            USER, {"gemini": {"model": "flash", "provider": "gemini"}}
        )
        assert out["gemini"]["provider"] == "vertex_ai"

    def test_a_provider_the_user_did_not_configure_is_not_added(self):
        out = bootstrap._apply_preset_models(USER, {"mistral": {"model": "large"}})
        assert "mistral" not in out

    def test_a_provider_the_preset_does_not_name_is_untouched(self):
        out = bootstrap._apply_preset_models(USER, {"claude": {"model": "haiku"}})
        assert out["grok"] == "grok-user-pick"
        assert out["openai"] == USER["openai"]

    def test_a_string_entry_becomes_a_mapping_when_the_preset_names_it(self):
        out = bootstrap._apply_preset_models(USER, {"grok": {"model": "grok-next"}})
        assert out["grok"] == {"model": "grok-next"}

    def test_a_provider_configured_with_no_value_is_skipped(self):
        """`claude:` left empty in user.yaml loads as None."""
        out = bootstrap._apply_preset_models(
            {"claude": None}, {"claude": {"model": "haiku"}}
        )
        assert out == {"claude": None}

    def test_a_preset_entry_with_no_value_is_skipped(self):
        out = bootstrap._apply_preset_models(USER, {"claude": None})
        assert out == USER

    def test_the_callers_dict_is_not_modified(self):
        before = copy.deepcopy(USER)
        bootstrap._apply_preset_models(USER, {"claude": {"model": "haiku"}})
        assert USER == before
