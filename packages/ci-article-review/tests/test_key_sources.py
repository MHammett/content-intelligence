"""Tests for config_loader's masked API-key-source diagnostics.

Exists because of a real incident: a persistent OS-level OPENAI_API_KEY
silently overrode whatever was in .env, and nothing in the pipeline's output
said so for days (see ci_core.env_provenance's docstring). These cover the
`ci-check --show-keys` diagnostic (describe_api_key_sources / print_key_sources)
and the passive, no-flag-needed warning (warn_env_shadowing) that catches the
same mismatch unprompted.

All tests pass an explicit env_snapshot rather than relying on
config_loader's module-level snapshot, so they are independent of whatever
.env / OS environment happens to exist on the machine running the suite.
"""

from unittest.mock import patch

import pytest

from ci_article_review import check as check_mod
from ci_article_review.config_loader import (
    describe_api_key_sources,
    warn_env_shadowing,
)
from ci_core.env_provenance import snapshot

USER_YAML = """
api_keys:
  openai:
    api_key: ${OPENAI_API_KEY}
  gemini:
    api_key: ${GEMINI_API_KEY}
  mistral:
    api_key: literal_value_in_yaml
  languagetool:
    username: ${LT_USER}
    api_key: ${LT_KEY}
"""


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "user.yaml").write_text(USER_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def dotenv_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OPENAI_API_KEY=file_placeholder\n", encoding="utf-8")
    return str(p)


class TestDescribeApiKeySources:
    def test_missing_user_yaml_raises_with_setup_instructions(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="ci-setup") as excinfo:
            describe_api_key_sources(str(tmp_path), env_snapshot=snapshot(None))
        # The example ships in the package; the message names its real path.
        # test_config_data.py asserts that path exists.
        assert "user.example.yaml" in str(excinfo.value)

    def test_non_mapping_yaml_raises(self, tmp_path):
        (tmp_path / "user.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="valid YAML mapping"):
            describe_api_key_sources(str(tmp_path), env_snapshot=snapshot(None))

    def test_literal_value_reported_without_a_var_name(self, config_dir):
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snapshot(None))
        mistral = next(e for e in entries if e["provider"] == "mistral")
        assert mistral["var_name"] is None
        assert mistral["source"] == "literal"
        assert "literal_value_in_yaml" not in mistral["masked_value"]

    def test_unset_var_is_reported_as_unset(self, config_dir, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snapshot(None))
        gemini = next(e for e in entries if e["provider"] == "gemini")
        assert gemini["source"] == "unset"
        assert gemini["masked_value"] == "(NOT SET)"

    def test_value_from_dotenv_file_is_reported_as_env(
        self, config_dir, monkeypatch, dotenv_file
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        snap = snapshot(dotenv_file)
        monkeypatch.setenv(
            "OPENAI_API_KEY", "file_placeholder"
        )  # load_dotenv() applied it
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snap)
        openai = next(e for e in entries if e["provider"] == "openai")
        assert openai["source"] == "env"

    def test_differing_os_env_var_is_flagged_but_dotenv_still_wins(
        self, config_dir, monkeypatch, dotenv_file
    ):
        """The exact incident, as it now resolves: a pre-existing OS var
        differs from .env, but .env wins the effective value — flagged as an
        FYI, not the old silent-override alarm."""
        monkeypatch.setenv("OPENAI_API_KEY", "stale_real_key_value_1234567890")
        snap = snapshot(dotenv_file)  # captures OPENAI_API_KEY as pre-existing
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snap)
        openai = next(e for e in entries if e["provider"] == "openai")
        assert openai["source"] == "env_overrides_os_env"

    def test_shadowing_os_env_var_that_happens_to_match_is_not_flagged_loudly(
        self, config_dir, monkeypatch, dotenv_file
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "file_placeholder")  # same value as .env
        snap = snapshot(dotenv_file)
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snap)
        openai = next(e for e in entries if e["provider"] == "openai")
        assert openai["source"] == "env"

    def test_masked_value_never_contains_the_real_secret(self, config_dir, monkeypatch):
        secret = "sk-proj-REALSECRETVALUEDONOTLEAK9999"
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snapshot(None))
        openai = next(e for e in entries if e["provider"] == "openai")
        assert secret not in openai["masked_value"]

    def test_nested_field_names_are_dotted(self, config_dir, monkeypatch):
        monkeypatch.setenv("LT_USER", "me@example.com")
        entries = describe_api_key_sources(str(config_dir), env_snapshot=snapshot(None))
        lt_user = next(
            e
            for e in entries
            if e["provider"] == "languagetool" and e["field"] == "username"
        )
        assert lt_user["var_name"] == "LT_USER"


class TestWarnEnvShadowing:
    def test_prints_nothing_when_no_mismatch(self, capsys):
        warn_env_shadowing(
            env_snapshot={"pre_existing_keys": frozenset(), "file_values": {}}
        )
        assert capsys.readouterr().out == ""

    def test_prints_a_notice_naming_the_variable(
        self, monkeypatch, capsys, dotenv_file
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "stale_real_key_value")
        snap = snapshot(dotenv_file)
        warn_env_shadowing(env_snapshot=snap)
        out = capsys.readouterr().out
        assert "NOTE" in out
        assert "OPENAI_API_KEY" in out
        assert "--show-keys" in out

    def test_does_not_leak_the_secret_value_itself(
        self, monkeypatch, capsys, dotenv_file
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-REALSECRETDONOTLEAK123")
        snap = snapshot(dotenv_file)
        warn_env_shadowing(env_snapshot=snap)
        out = capsys.readouterr().out
        assert "sk-proj-REALSECRETDONOTLEAK123" not in out


class TestShowKeysCli:
    """--show-keys on `ci-check`: prints and exits before any network call."""

    def test_show_keys_exits_zero_and_makes_no_network_call(self, config_dir, capsys):
        with (
            patch(
                "sys.argv",
                [
                    "ci-check",
                    "--publication",
                    "x",
                    "--config-dir",
                    str(config_dir),
                    "--show-keys",
                ],
            ),
            patch.object(check_mod.requests, "post") as post,
            patch.object(check_mod.requests, "get") as get,
        ):
            with pytest.raises(SystemExit) as exc_info:
                check_mod.main()
        assert exc_info.value.code == 0
        post.assert_not_called()
        get.assert_not_called()
        out = capsys.readouterr().out
        assert "API key sources" in out

    def test_show_keys_labels_the_env_override_case(
        self, config_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "some_key_value_here_12345")
        with patch(
            "sys.argv",
            [
                "ci-check",
                "--publication",
                "x",
                "--config-dir",
                str(config_dir),
                "--show-keys",
            ],
        ):
            with patch.object(
                check_mod,
                "describe_api_key_sources",
                return_value=[
                    {
                        "provider": "openai",
                        "field": "api_key",
                        "var_name": "OPENAI_API_KEY",
                        "masked_value": "some_ke...2345",
                        "source": "env_overrides_os_env",
                    }
                ],
            ):
                with pytest.raises(SystemExit) as exc_info:
                    check_mod.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert ".env" in out and "wins" in out

    def test_show_keys_never_prints_a_raw_secret(self, config_dir, monkeypatch, capsys):
        secret = "sk-proj-REALSECRETDONOTLEAK999999"
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        with patch(
            "sys.argv",
            [
                "ci-check",
                "--publication",
                "x",
                "--config-dir",
                str(config_dir),
                "--show-keys",
            ],
        ):
            with pytest.raises(SystemExit):
                check_mod.main()
        out = capsys.readouterr().out
        assert secret not in out

    def test_show_keys_with_missing_config_reports_a_config_error(
        self, tmp_path, capsys
    ):
        with patch(
            "sys.argv",
            [
                "ci-check",
                "--publication",
                "x",
                "--config-dir",
                str(tmp_path),
                "--show-keys",
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                check_mod.main()
        assert exc_info.value.code == 1
        assert "Config error" in capsys.readouterr().out
