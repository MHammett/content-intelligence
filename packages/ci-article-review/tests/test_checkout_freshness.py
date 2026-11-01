"""The out-of-date-checkout check, against real git repositories on disk."""

import subprocess

import pytest

from ci_article_review import checkout_freshness as cf

# conftest replaces cf.commits_behind for every test; this file tests the real one.
real_commits_behind = cf.commits_behind

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd, *args):
    import os

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_ENV},
    ).stdout


def commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "master")
    commit(origin, "a")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return origin, work


def test_current_checkout_is_silent(clone):
    _, work = clone
    assert real_commits_behind(work) is None


def test_fetches_and_counts_commits_the_checkout_lacks(clone):
    origin, work = clone
    commit(origin, "b")
    commit(origin, "c")
    behind = real_commits_behind(work)
    assert behind.count == 2 and behind.fetched
    assert "add c" in behind.subjects[0]


def test_without_a_fetch_it_counts_against_what_was_last_fetched(clone):
    origin, work = clone
    commit(origin, "b")
    assert real_commits_behind(work, fetch=False) is None


def test_failed_fetch_still_counts_and_says_so(clone):
    origin, work = clone
    commit(origin, "b")
    git(work, "fetch", "-q", "origin")
    git(work, "remote", "set-url", "origin", str(work / "nowhere"))
    behind = real_commits_behind(work)
    assert behind.count == 1 and not behind.fetched
    assert "Could not fetch" in behind.describe()


def test_not_a_git_checkout_is_silent(tmp_path):
    assert real_commits_behind(tmp_path) is None


def test_no_origin_is_silent(tmp_path):
    git(tmp_path, "init", "-q", "-b", "master")
    commit(tmp_path, "a")
    assert real_commits_behind(tmp_path) is None


def test_missing_git_is_silent(tmp_path):
    def no_git(*a, **k):
        raise FileNotFoundError("git")

    assert real_commits_behind(tmp_path, run=no_git) is None


def test_describe_caps_the_list():
    text = cf.Behind(9, tuple(f"x{i} s" for i in range(9)), True).describe()
    assert "9 commit(s)" in text and "and 4 more" in text and "x5" not in text


def behind_two():
    return cf.Behind(2, ("b one", "a two"), True)


def test_write_back_is_refused_when_behind():
    lines = []
    assert not cf.enforce(
        refuse=True, allow_stale=False, err=lines.append, check=behind_two
    )
    assert "--allow-stale" in lines[-1]


def test_allow_stale_lets_a_write_back_run_but_still_warns():
    lines = []
    assert cf.enforce(refuse=True, allow_stale=True, err=lines.append, check=behind_two)
    assert "2 commit(s)" in lines[0]


def test_a_read_only_run_only_warns():
    lines = []
    assert cf.enforce(
        refuse=False, allow_stale=False, err=lines.append, check=behind_two
    )
    assert len(lines) == 1


def test_up_to_date_prints_nothing():
    lines = []
    assert cf.enforce(
        refuse=True, allow_stale=False, err=lines.append, check=lambda: None
    )
    assert lines == []
