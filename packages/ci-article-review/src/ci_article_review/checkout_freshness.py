"""Tell the person running a command that the code they run is out of date.

A git worktree stays at the commit it started on. Reviews run from one for days
while master gains the fixes that decide what the run does, and nothing says so:
two live reviews ran 44 commits behind and lacked the archive.org lookup cache.

``commits_behind`` counts the commits on ``origin/master`` that the checkout
this package is imported from does not contain. It answers ``None`` whenever it
cannot know (no git, an installed wheel, no ``origin``), because a check that
fails the command on those would be worse than the staleness it looks for.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

_BASE = "origin/master"
_SHOWN = 5


@dataclass(frozen=True)
class Behind:
    count: int
    subjects: tuple
    fetched: bool

    def describe(self):
        lines = [
            f"The code running this command is {self.count} commit(s) behind {_BASE}."
        ]
        if not self.fetched:
            lines.append(
                f"  (Could not fetch; counted against the last fetched {_BASE}.)"
            )
        lines.append("  Missing from this checkout, newest first:")
        lines.extend(f"    {s}" for s in self.subjects[:_SHOWN])
        if self.count > _SHOWN:
            lines.append(f"    ... and {self.count - _SHOWN} more")
        lines.append(
            "  Bring the checkout up to date (merge or rebase onto "
            f"{_BASE}), or start from a fresh worktree."
        )
        return "\n".join(lines)


def commits_behind(directory=None, *, run=subprocess.run, fetch=True):
    """Return a ``Behind`` when the checkout lags ``origin/master``, else ``None``."""
    cwd = str(directory or Path(__file__).resolve().parent)

    def git(*args, timeout=30):
        try:
            return run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    top = git("rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return None
    fetched = False
    if fetch:
        got = git("fetch", "--quiet", "origin", "master", timeout=60)
        fetched = got is not None and got.returncode == 0
    listed = git("log", "--format=%h %s", f"HEAD..{_BASE}")
    if listed is None or listed.returncode != 0:
        return None
    subjects = tuple(line for line in listed.stdout.splitlines() if line)
    if not subjects:
        return None
    return Behind(count=len(subjects), subjects=subjects, fetched=fetched)


def enforce(*, refuse, allow_stale, err, check=None):
    """Print the warning; return False when the command must stop.

    ``refuse`` is true for a command that writes to a shared repository.
    """
    behind = (check or commits_behind)()
    if behind is None:
        return True
    err(behind.describe())
    if refuse and not allow_stale:
        err(
            "  Refusing to write back from an out-of-date checkout. "
            "Pass --allow-stale to run anyway."
        )
        return False
    return True
