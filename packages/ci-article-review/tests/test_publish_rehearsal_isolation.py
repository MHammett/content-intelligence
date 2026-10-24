"""tools/publish_rehearsal.py leaves the shared history alone and shows the whole body.

Three things found by rehearsing for real (issue #324): the post body was cut at
2,000 characters of its JSON-escaped form, so a long article could not be checked
from the output; every rehearsal appended lines to the real daily log under
``CI_HISTORY_ROOT``; and a ``.env`` found by walking up from the package, which
from a worktree inside the main checkout is the real one, put every provider key
in the process's environment although the tool said credentials are never read.

The tool blocks sockets and patches the process it lives in, so each test runs it
as a child process, as it is run. The shipped example configs are used as they
are: nothing in them needs a value.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_TOOL = _REPO / "tools" / "publish_rehearsal.py"
_CONFIGS = (
    _REPO / "packages" / "ci-article-review" / "src" / "ci_article_review" / "configs"
)

#: Nothing like the real thing: it only has to be findable in a log line.
LAST = "THE-LAST-PARAGRAPH-OF-A-LONG-POST"


def _handoff(paragraphs):
    body = "\n\n".join(
        f'Paragraph {i} of a long rehearsal post, with "quotes" and enough words '
        f"to make the JSON form of the body a good deal longer than 2,000 characters."
        for i in range(paragraphs)
    )
    return f"""PUBLICATION HANDOFF
Generated: 2026-10-24
Article: Rehearsal Post
Publication: testpub

PUBLICATION PARAMETERS
Status: draft
Post type: post
WordPress category: technical-analysis
Tags: grid

SEO METADATA
Focus keyword: rehearsal
SEO title: Rehearsal Post for Testing the Push
Meta description: A rehearsal post that is never published, used to check the push.

FINAL DRAFT
# Rehearsal Post

{body}

{LAST}
"""


def _config_dir(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for src, dst in (
        ("user.example.yaml", "user.yaml"),
        ("publication.example.yaml", "testpub.yaml"),
    ):
        (config_dir / dst).write_text(
            (_CONFIGS / src).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return config_dir


def _rehearse(tmp_path, paragraphs=60, history=None):
    work = tmp_path / "work"
    work.mkdir()
    handoff = work / "handoff.md"
    handoff.write_text(_handoff(paragraphs), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("CI_HISTORY_ROOT", None)
    if history is not None:
        env["CI_HISTORY_ROOT"] = str(history)
    done = subprocess.run(
        [
            sys.executable,
            str(_TOOL),
            str(handoff),
            "--publication",
            "testpub",
            "--config-dir",
            str(_config_dir(tmp_path)),
            "--out-dir",
            str(work / "out"),
            "--",
            "--no-seo-suggestions",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=work,
        timeout=300,
    )
    return done, work


class TestPostBody:
    def test_a_long_body_is_written_whole(self, tmp_path):
        done, work = _rehearse(tmp_path)
        out = done.stdout + done.stderr
        assert "exit code:       None" in done.stdout, out
        body = (work / "out" / "rehearsal-post-1.html").read_text(encoding="utf-8")
        assert len(body) > 4000, "the draft should outgrow the old 2,000 cut"
        assert LAST in body
        assert body.count("<!-- wp:paragraph -->") >= 60

    def test_the_output_says_where_it_went_and_stays_short(self, tmp_path):
        done, work = _rehearse(tmp_path)
        target = work / "out" / "rehearsal-post-1.html"
        assert f"content written to {target}" in done.stdout
        assert " characters>" in done.stdout
        # The other fields are still there to read, and the body is not.
        assert '"status": "draft"' in done.stdout
        assert LAST not in done.stdout


class TestSharedHistory:
    def test_a_rehearsal_writes_nothing_to_the_history_root(self, tmp_path):
        shared = tmp_path / "shared-history"
        shared.mkdir()
        done, work = _rehearse(tmp_path, history=shared)
        assert "exit code:       None" in done.stdout, done.stdout + done.stderr
        assert list(shared.iterdir()) == [], "the daily log was written to it"

    def test_nor_to_pipeline_history_in_the_working_directory(self, tmp_path):
        done, work = _rehearse(tmp_path)
        assert "exit code:       None" in done.stdout, done.stdout + done.stderr
        assert not (work / "pipeline_history").exists()


class TestDotenv:
    """A ``.env`` that ``find_dotenv`` finds is not loaded.

    The child pretends ``find_dotenv`` found a file with a probe variable in it,
    which is the only way to plant one: it is looked for from the package, not
    from the working directory. The run without ``disable_dotenv`` is the control,
    because a check that never sees the variable load would pass on anything.
    """

    def _probe(self, tmp_path, disable):
        env_file = tmp_path / "planted.env"
        env_file.write_text("REHEARSAL_DOTENV_PROBE=from-the-env-file\n", "utf-8")
        script = textwrap.dedent(
            f"""
            import importlib.util, os, sys
            import dotenv
            spec = importlib.util.spec_from_file_location("rehearsal", {str(_TOOL)!r})
            tool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tool)
            dotenv.find_dotenv = lambda *a, **k: {str(env_file)!r}
            if {disable!r}:
                tool.disable_dotenv()
            import ci_article_review.config_loader
            print("PROBE=" + repr(os.environ.get("REHEARSAL_DOTENV_PROBE")))
            """
        )
        env = {k: v for k, v in os.environ.items() if k != "REHEARSAL_DOTENV_PROBE"}
        done = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=tmp_path,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    def test_without_the_guard_the_file_is_loaded(self, tmp_path):
        assert "PROBE='from-the-env-file'" in self._probe(tmp_path, disable=False)

    def test_with_it_nothing_is_loaded(self, tmp_path):
        assert "PROBE=None" in self._probe(tmp_path, disable=True)
