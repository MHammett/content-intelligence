"""tools/publish_rehearsal.py rehearses a config whose ``${VAR}`` values are unset.

``--publish`` makes no model call, but config loading resolves every ``${VAR}`` in
user.yaml and the publication file and stops at the first with no value. A
worktree with a stub ``.env`` therefore failed three runs in a row, one variable
each, before it reached the part being rehearsed (issue #375).

The end-to-end test runs the tool as a child process, as it is run: it blocks
sockets and patches the process it lives in, and the config loader takes its
snapshot of the environment when it is imported, so only a fresh interpreter
shows the order of those steps.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[3]
_TOOL = _REPO / "tools" / "publish_rehearsal.py"
_CONFIGS = (
    _REPO / "packages" / "ci-article-review" / "src" / "ci_article_review" / "configs"
)

#: Named so that no .env, shell or CI job anywhere defines it.
UNSET = "CI_REHEARSAL_TEST_NEVER_SET_ANYWHERE"
UNSET_TOO = "CI_REHEARSAL_TEST_NEVER_SET_EITHER"

HANDOFF = """PUBLICATION HANDOFF
Generated: 2026-10-24
Article: Rehearsal Post
Publication: testpub

PUBLICATION PARAMETERS
Status: draft
Post type: post
WordPress category: technical-analysis
Tags: grid
WordPress author: admin

SEO METADATA
Focus keyword: rehearsal
SEO title: Rehearsal Post for Testing the Push
Meta description: A rehearsal post that is never published, used to check the push.

FINAL DRAFT
# Rehearsal Post

A paragraph of the rehearsal body.
"""


def _load():
    spec = importlib.util.spec_from_file_location("publish_rehearsal_tool", _TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["publish_rehearsal_tool"] = module
    spec.loader.exec_module(module)
    return module


rehearsal = _load()


def _config_dir(tmp_path):
    """The shipped example configs, with two api keys left as ``${VAR}`` references."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    user = (_CONFIGS / "user.example.yaml").read_text(encoding="utf-8")
    assert user.count("api_key: your_key_here") >= 2
    user = user.replace("api_key: your_key_here", f"api_key: ${{{UNSET}}}", 1)
    user = user.replace("api_key: your_key_here", f"api_key: ${{{UNSET_TOO}}}", 1)
    (config_dir / "user.yaml").write_text(user, encoding="utf-8")
    publication = (_CONFIGS / "publication.example.yaml").read_text(encoding="utf-8")
    (config_dir / "testpub.yaml").write_text(publication, encoding="utf-8")
    return config_dir


class TestPlaceholders:
    def test_a_whole_string_placeholder_is_found(self, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "api_keys:\n  x:\n    api_key: ${ONE}\nlist:\n  - ${TWO}\n",
            encoding="utf-8",
        )
        assert rehearsal.config_placeholders(tmp_path) == ["ONE", "TWO"]

    def test_a_comment_or_a_partial_string_is_not_one(self, tmp_path):
        """The loader resolves a string that is wholly ``${NAME}`` and no other."""
        (tmp_path / "a.yaml").write_text(
            "# set it as ${IN_A_COMMENT}\nurl: https://x/${PART}/y\nkey: ${REAL}\n",
            encoding="utf-8",
        )
        assert rehearsal.config_placeholders(tmp_path) == ["REAL"]

    def test_every_yaml_file_is_read_and_nothing_else(self, tmp_path):
        (tmp_path / "user.yaml").write_text("a: ${A}\n", encoding="utf-8")
        (tmp_path / "pub.yml").write_text("b: ${B}\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.yaml").write_text("c: ${C}\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("d: ${D}\n", encoding="utf-8")
        assert rehearsal.config_placeholders(tmp_path) == ["A", "B", "C"]

    def test_a_file_that_does_not_parse_is_left_to_the_cli(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("a: [unclosed\n", encoding="utf-8")
        (tmp_path / "good.yaml").write_text("a: ${A}\n", encoding="utf-8")
        assert rehearsal.config_placeholders(tmp_path) == ["A"]

    def test_a_missing_directory_has_none(self, tmp_path):
        assert rehearsal.config_placeholders(tmp_path / "nope") == []


class TestSetDummyEnv:
    def test_an_unset_variable_gets_a_dummy(self, tmp_path, monkeypatch):
        monkeypatch.delenv(UNSET, raising=False)
        (tmp_path / "a.yaml").write_text(f"k: ${{{UNSET}}}\n", encoding="utf-8")
        assert rehearsal.set_dummy_env(tmp_path) == [UNSET]
        assert os.environ[UNSET] == rehearsal.DUMMY_ENV_VALUE
        monkeypatch.delenv(UNSET)

    def test_a_variable_with_a_value_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv(UNSET, "real-value")
        (tmp_path / "a.yaml").write_text(f"k: ${{{UNSET}}}\n", encoding="utf-8")
        assert rehearsal.set_dummy_env(tmp_path) == []
        assert os.environ[UNSET] == "real-value"


class TestRehearsalEndToEnd:
    def _run(self, tmp_path, config_dir):
        handoff = tmp_path / "handoff.md"
        handoff.write_text(HANDOFF, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in (UNSET, UNSET_TOO)}
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [
                sys.executable,
                str(_TOOL),
                str(handoff),
                "--publication",
                "testpub",
                "--config-dir",
                str(config_dir),
                "--out-dir",
                str(tmp_path),
                "--",
                "--no-seo-suggestions",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=tmp_path,
            timeout=300,
        )

    def test_a_config_naming_unset_variables_still_rehearses(self, tmp_path):
        done = self._run(tmp_path, _config_dir(tmp_path))
        out = done.stdout + done.stderr
        assert "is not set" not in out, out
        assert "exit code:       None" in done.stdout, out
        assert "POST https://yoursite.com/wp-json/wp/v2/posts" in done.stdout, out
        # Said, so a rehearsal that ran on dummies does not look like one that
        # read the real keys.
        assert UNSET in done.stdout and UNSET_TOO in done.stdout


class TestSplitForward:
    """Flags after a bare ``--`` are forwarded, split before argparse sees them.

    CPython 3.12.3, which CI runs, rejects ``-- --flag`` after the options when
    argparse is left to do it (3.13 accepts it), and the end-to-end tests above
    were the first thing to put it in front of that version.
    """

    def test_flags_after_a_bare_double_dash_are_forwarded(self):
        own, forward = rehearsal.split_forward(
            ["h.md", "--publication", "p", "--", "--no-seo-suggestions", "--x", "y"]
        )
        assert own == ["h.md", "--publication", "p"]
        assert forward == ["--no-seo-suggestions", "--x", "y"]

    def test_without_one_nothing_is_forwarded(self):
        own, forward = rehearsal.split_forward(["h.md", "--publication", "p"])
        assert (own, forward) == (["h.md", "--publication", "p"], [])

    def test_only_the_first_double_dash_splits(self):
        own, forward = rehearsal.split_forward(["h.md", "--", "--a", "--", "--b"])
        assert (own, forward) == (["h.md"], ["--a", "--", "--b"])

    def test_a_trailing_double_dash_forwards_nothing(self):
        own, forward = rehearsal.split_forward(["h.md", "--"])
        assert (own, forward) == (["h.md"], [])
