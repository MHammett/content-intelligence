"""The archive gate the document repository runs on round pull requests.

``document_repo/archive_gate.py.tmpl`` is rendered into the document repository
as ``.github/scripts/archive_gate.py`` by ``ci-docs init`` and run there by
GitHub Actions with nothing installed. These tests load the template itself as a
module and run it against article folders built the way the pipeline writes them:
``archive.json`` comes from ``github_archive.render_json``, so a drift between
what the pipeline writes and what the gate reads fails here.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from ci_article_review import github_archive
from ci_core.github_repo import git_blob_sha

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ci_article_review"
    / "document_repo"
    / "archive_gate.py.tmpl"
)
SLUG = "an-article"
DRAFT = b"Body [1] [2].\n\n## Sources\n\n1. A. https://example.com/a\n2. B. https://example.com/b\n"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_loader("archive_gate", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(
        compile(TEMPLATE.read_text(encoding="utf-8"), str(TEMPLATE), "exec"),
        module.__dict__,
    )
    return module


def _results(b_wayback):
    return [
        {
            "marker": "1",
            "url": "https://example.com/a",
            "wayback": {
                "archived": True,
                "snapshot_url": "https://web.archive.org/web/1/https://example.com/a",
            },
        },
        {"marker": "2", "url": "https://example.com/b", "wayback": b_wayback},
    ]


def _article(tmp_path, *, results, draft=DRAFT, read=DRAFT, exceptions=None):
    folder = tmp_path / "articles" / SLUG
    (folder / "pipeline").mkdir(parents=True)
    (folder / "draft.md").write_bytes(draft)
    source = {
        "commit_sha": "a" * 40,
        "ref": f"round/{SLUG}/r1",
        "files": [
            {"path": f"articles/{SLUG}/draft.md", "blob_sha": git_blob_sha(read)}
        ],
    }
    rows = github_archive.status_rows(results)
    (folder / "pipeline" / "archive.json").write_text(
        github_archive.render_json({"document_source": source}, rows, generated="g"),
        encoding="utf-8",
    )
    if exceptions is not None:
        (folder / "archive-exceptions.md").write_text(exceptions, encoding="utf-8")
    return tmp_path


MISSING = {
    "archived": False,
    "archive_outcome": "submit_failed",
    "archive_outcome_detail": "refused",
}


def test_its_blob_hash_is_gits(gate):
    assert gate.blob_sha(DRAFT) == git_blob_sha(DRAFT)


def test_every_url_archived_passes(gate, tmp_path):
    root = _article(
        tmp_path,
        results=_results(
            {"archived": True, "snapshot_url": "s", "snapshot_stale": True}
        ),
    )
    ok, lines = gate.check(root, SLUG)
    assert ok, lines
    assert "2 with a snapshot" in lines[0]


def test_a_gap_fails_and_names_the_url(gate, tmp_path):
    ok, lines = gate.check(_article(tmp_path, results=_results(MISSING)), SLUG)
    assert not ok
    assert any(
        "OPEN [2] https://example.com/b (missing): refused" in line for line in lines
    )


@pytest.mark.parametrize(
    "line",
    [
        "- https://example.com/b: the publisher blocks archive.org",
        "* <https://example.com/b> — the publisher blocks archive.org",
    ],
)
def test_an_accepted_gap_passes(gate, tmp_path, line):
    root = _article(
        tmp_path, results=_results(MISSING), exceptions=f"# Accepted\n\n{line}\n"
    )
    ok, lines = gate.check(root, SLUG)
    assert ok, lines
    assert any("accepted [2]" in x and "blocks archive.org" in x for x in lines)


def test_an_exception_for_another_url_does_not_excuse_this_one(gate, tmp_path):
    root = _article(
        tmp_path,
        results=_results(MISSING),
        exceptions="- https://example.com/z: other\n",
    )
    assert not gate.check(root, SLUG)[0]


def test_a_pending_capture_is_a_gap(gate, tmp_path):
    pending = {
        "archived": False,
        "archive_outcome": "pending",
        "submission_job_id": "j",
    }
    assert not gate.check(_article(tmp_path, results=_results(pending)), SLUG)[0]


def test_a_draft_changed_since_the_pass_fails(gate, tmp_path):
    root = _article(
        tmp_path,
        results=_results({"archived": True, "snapshot_url": "s"}),
        draft=DRAFT + b"3. New. https://example.com/c\n",
    )
    ok, lines = gate.check(root, SLUG)
    assert not ok
    assert "changed after the last archive pass" in lines[0]


def test_no_archive_pass_fails(gate, tmp_path):
    folder = tmp_path / "articles" / SLUG
    folder.mkdir(parents=True)
    (folder / "draft.md").write_bytes(DRAFT)
    ok, lines = gate.check(tmp_path, SLUG)
    assert not ok
    assert "no archive pass" in lines[0]
    assert f"--github {SLUG}" in lines[1]


def test_a_branch_that_is_not_a_round_passes_without_a_check(gate, capsys):
    assert gate.main(["archive_gate.py", "claude/some-fix"]) == 0
    assert "not a round branch" in capsys.readouterr().out


def test_main_fails_a_round_with_a_gap_and_writes_the_summary(
    gate, tmp_path, monkeypatch, capsys
):
    _article(tmp_path, results=_results(MISSING))
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert gate.main(["archive_gate.py", f"round/{SLUG}/r2"]) == 1
    assert "OPEN [2]" in summary.read_text(encoding="utf-8")
    assert (
        json.loads(
            (tmp_path / "articles" / SLUG / "pipeline" / "archive.json").read_text()
        )["schema"]
        == 1
    )
