"""tools/tidy.py reports what is safe to clean up, and deletes nothing.

Run against a real throwaway repository with a real bare "origin" on disk, so the
git plumbing it depends on (``merge-base --is-ancestor``, ``worktree list
--porcelain``, ``for-each-ref``) is exercised and not mocked. ``--no-github`` keeps
it off the network; the squash-merge path is covered by handing it PR records.
"""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "tidy.py"


def _load():
    spec = importlib.util.spec_from_file_location("tidy_tool", _TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tidy_tool"] = module
    spec.loader.exec_module(module)
    return module


tidy = _load()


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=True
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


@pytest.fixture
def repo(tmp_path):
    """A checkout named ``proj`` with an origin, a merged branch and an unmerged one."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(origin))
    main = tmp_path / "proj"
    _git(tmp_path, "clone", str(origin), str(main))
    _git(main, "checkout", "-b", "master")
    _commit(main, "base")
    _git(main, "push", "-u", "origin", "master")

    _git(main, "checkout", "-b", "merged-already")
    _commit(main, "m")
    _git(main, "checkout", "master")
    _git(main, "merge", "--no-ff", "-m", "merge", "merged-already")
    _git(main, "push", "origin", "master", "merged-already")

    _git(main, "checkout", "-b", "unmerged")
    _commit(main, "u")
    _git(main, "push", "origin", "unmerged")
    _git(main, "checkout", "master")
    return main


def _run(repo: Path, capsys, *extra: str, monkeypatch) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["tidy.py", "--no-github", *extra])
    assert tidy.main() == 0
    return capsys.readouterr().out


def test_reports_the_merged_branch_and_not_the_unmerged_one(repo, capsys, monkeypatch):
    out = _run(repo, capsys, monkeypatch=monkeypatch)
    assert "delete merged-already  (already in origin/master)" in out
    assert "branch -D merged-already" in out
    assert "push origin --delete merged-already" in out
    assert "unmerged" not in out.replace("merged-already", "")


def test_never_deletes_anything(repo, capsys, monkeypatch):
    _run(repo, capsys, monkeypatch=monkeypatch)
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "merged-already" in branches and "unmerged" in branches
    assert "merged-already" in _git(repo, "ls-remote", "--heads", "origin")


def test_squash_merged_branch_is_found_through_its_pr(repo, monkeypatch):
    # A squash merge leaves the branch tip out of master's history, so the
    # ancestor test says "unmerged". The merged PR whose head is this very tip is
    # the only evidence there is.
    tip = _git(repo, "rev-parse", "unmerged")
    merged = [tidy.Pr("unmerged", tip, "MERGED", 7)]
    assert (
        tidy.merged_evidence("unmerged", tip, merged, "origin/master", repo)
        == "PR #7 merged at this tip"
    )
    # ...but not when the branch has moved on since the PR merged...
    moved = [tidy.Pr("unmerged", "0" * 40, "MERGED", 7)]
    assert tidy.merged_evidence("unmerged", tip, moved, "origin/master", repo) is None
    # ...and never while a PR from it is open.
    opened = [*merged, tidy.Pr("unmerged", tip, "OPEN", 8)]
    assert tidy.merged_evidence("unmerged", tip, opened, "origin/master", repo) is None


def test_worktree_with_run_history_is_kept_even_when_its_branch_is_merged(
    repo, capsys, monkeypatch
):
    wt = repo.parent / "proj" / ".claude" / "worktrees" / "has-history"
    _git(repo, "worktree", "add", str(wt), "merged-already")
    (wt / "pipeline_history" / "key").mkdir(parents=True)
    (wt / "pipeline_history" / "key" / "run_1_report.json").write_text(
        "{}", encoding="utf-8"
    )
    out = _run(repo, capsys, "--idle-hours", "0", monkeypatch=monkeypatch)
    assert "KEEP   has-history" in out
    assert "1 file(s) of run history exist only here" in out
    assert "worktree remove" not in out


def test_clean_merged_idle_worktree_is_removable_and_its_branch_goes_with_it(
    repo, capsys, monkeypatch
):
    wt = repo / ".claude" / "worktrees" / "finished"
    _git(repo, "worktree", "add", str(wt), "merged-already")
    old = time.time() - 24 * 3600
    gitdir = Path(_git(wt, "rev-parse", "--absolute-git-dir"))
    for name in ("index", "HEAD"):
        os.utime(gitdir / name, (old, old))
    out = _run(repo, capsys, monkeypatch=monkeypatch)
    assert "REMOVE finished" in out
    assert "worktree remove" in out
    assert "branch -D merged-already" in out


def test_recently_touched_worktree_is_kept(repo, capsys, monkeypatch):
    wt = repo / ".claude" / "worktrees" / "live"
    _git(repo, "worktree", "add", str(wt), "merged-already")
    out = _run(repo, capsys, monkeypatch=monkeypatch)
    assert "KEEP   live" in out and "a session may be live" in out


def test_dirty_worktree_is_kept(repo, capsys, monkeypatch):
    wt = repo / ".claude" / "worktrees" / "dirty"
    _git(repo, "worktree", "add", str(wt), "merged-already")
    (wt / "scratch.txt").write_text("x", encoding="utf-8")
    out = _run(repo, capsys, "--idle-hours", "0", monkeypatch=monkeypatch)
    assert "KEEP   dirty" in out and "uncommitted change" in out


def test_orphan_directories(repo, capsys, monkeypatch):
    nested = repo / ".claude" / "worktrees"
    (nested / "venv-only" / ".venv" / "Lib").mkdir(parents=True)
    (nested / "venv-only" / ".venv" / "Lib" / "x.pyd").write_bytes(b"\0")
    (nested / "empty").mkdir()
    (nested / "has-work").mkdir()
    (nested / "has-work" / "notes.md").write_text(
        "not committed anywhere", encoding="utf-8"
    )
    sibling = repo.parent / "proj-leftover"
    (sibling / ".venv").mkdir(parents=True)
    out = _run(repo, capsys, monkeypatch=monkeypatch)
    assert "delete venv-only" in out
    assert "delete empty" in out
    assert "delete proj-leftover" in out
    assert "KEEP   has-work  (1 file(s) outside .venv" in out
    assert "Remove-Item" in out and "has-work'" not in out.split("== Commands")[1]


def test_stale_local_master_is_called_out(repo, capsys, monkeypatch):
    _git(repo, "checkout", "-b", "tmp")
    _git(repo, "branch", "-f", "master", "HEAD~1")
    out = _run(repo, capsys, monkeypatch=monkeypatch)
    assert "local master is" in out and "behind origin/master" in out
