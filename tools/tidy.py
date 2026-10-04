"""List what is safe to clean up in this repository, and print the commands.

Read-only. It runs ``git`` and, unless told not to, ``gh pr list``, and it
deletes nothing: the last thing it prints is a block of commands for a person to
read and run. Deleting waits for the maintainer's word in this repository, and a
script that decides for itself would be one more thing that does.

Many sessions share this checkout, and each one leaves something behind. Four
kinds of mess have accumulated here, and this reports each:

* **Merged branches, local and on origin.** GitHub does not delete a PR's head
  branch unless "Automatically delete head branches" is on, and
  ``gh pr merge --delete-branch`` is off the table from a worktree
  (docs/LANDING-A-PR.md). A branch is reported when its tip is an ancestor of
  ``origin/master`` (deleting it loses no commit) or is the head of a merged PR
  (a squash merge leaves the branch tip out of ``master``'s history, so
  ``--is-ancestor`` alone calls a merged branch unmerged).
* **Worktrees.** Reported removable only when clean, with nothing unpushed, on a
  merged branch, holding no run history, and untouched for ``--idle-hours``. The
  gitignored ``pipeline_history/`` is what a removal loses, so any file in it
  keeps the worktree on the KEEP list (docs/REPLAY-AND-HISTORY.md).
* **Orphan directories.** A worktree that git no longer knows about leaves its
  directory behind -- usually just a ``.venv``, whose hardlinked ``.pyd`` files
  Windows will not delete while anything holds them. Such a directory has no
  ``.git``, so ``git status`` inside it reports the *main checkout* instead.
* **A stale local ``master``** in the main checkout, which makes ``git branch -d``
  refuse branches that ``origin/master`` already contains.

What it cannot see is a running session. A session whose worktree is clean and
merged looks exactly like one that finished; ``--idle-hours`` (default 2) only
guards against the ones that touched git recently. Check the session list
before running any removal it prints.

    uv run python tools/tidy.py [--no-github] [--idle-hours N] [--fetch]

``--fetch`` runs ``git fetch --prune origin`` first; otherwise it reasons from
whatever ``origin/*`` you last fetched, and says so.
"""

import argparse
import json
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field

PROTECTED = {"master", "main", "HEAD"}
# A leftover directory this small is a bare virtualenv or an empty shell, not work.
VENV_NAMES = {".venv"}
IGNORED_DIR_PARTS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def git(
    *args: str, cwd: pathlib.Path | None = None
) -> subprocess.CompletedProcess[str]:
    # --no-optional-locks: a plain `git status` can take another session's index
    # lock and make that session's next git command fail.
    return subprocess.run(
        ["git", "--no-optional-locks", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_out(*args: str, cwd: pathlib.Path | None = None) -> str:
    return git(*args, cwd=cwd).stdout.strip()


@dataclass
class Worktree:
    path: pathlib.Path
    head: str
    branch: str | None  # None when detached
    is_main: bool = False
    is_here: bool = False


@dataclass
class Pr:
    branch: str
    head_sha: str
    state: str  # OPEN, MERGED, CLOSED
    number: int


@dataclass
class Verdict:
    name: str
    removable: bool
    reasons: list[str] = field(default_factory=list)


def main_checkout(cwd: pathlib.Path) -> pathlib.Path:
    common = git_out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    return pathlib.Path(common).parent


def list_worktrees(cwd: pathlib.Path) -> list[Worktree]:
    raw = git_out("worktree", "list", "--porcelain", cwd=cwd)
    here = pathlib.Path(git_out("rev-parse", "--show-toplevel", cwd=cwd)).resolve()
    out: list[Worktree] = []
    for block in raw.split("\n\n"):
        fields = dict(
            line.split(" ", 1) if " " in line else (line, "")
            for line in block.splitlines()
        )
        if "worktree" not in fields:
            continue
        path = pathlib.Path(fields["worktree"])
        branch = fields.get("branch", "").removeprefix("refs/heads/") or None
        out.append(
            Worktree(
                path,
                fields.get("HEAD", ""),
                branch,
                is_main=not out,
                is_here=path.resolve() == here,
            )
        )
    return out


def fetch_prs(cwd: pathlib.Path) -> list[Pr] | None:
    done = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,headRefName,headRefOid,state",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if done.returncode != 0:
        print(
            f"note: gh pr list failed ({done.stderr.strip()[:120]}); squash-merged branches will be missed."
        )
        return None
    return [
        Pr(p["headRefName"], p["headRefOid"], p["state"], p["number"])
        for p in json.loads(done.stdout)
    ]


def is_ancestor(sha: str, of: str, cwd: pathlib.Path) -> bool:
    return git("merge-base", "--is-ancestor", sha, of, cwd=cwd).returncode == 0


def merged_evidence(
    branch: str, sha: str, prs: list[Pr] | None, base: str, cwd: pathlib.Path
) -> str | None:
    """Why this branch loses nothing if deleted, or None when it might."""
    mine = [p for p in prs or [] if p.branch == branch]
    if any(p.state == "OPEN" for p in mine):
        return None
    for p in mine:
        if p.state == "MERGED" and p.head_sha == sha:
            return f"PR #{p.number} merged at this tip"
    if is_ancestor(sha, base, cwd):
        return f"already in {base}"
    return None


def newest_git_touch(wt: Worktree) -> float | None:
    gitdir = git_out("rev-parse", "--absolute-git-dir", cwd=wt.path)
    times = [
        pathlib.Path(gitdir, n).stat().st_mtime
        for n in ("index", "HEAD")
        if pathlib.Path(gitdir, n).exists()
    ]
    return max(times) if times else None


def history_files(path: pathlib.Path) -> int:
    root = path / "pipeline_history"
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0


def judge_worktree(
    wt: Worktree, prs: list[Pr] | None, base: str, idle_hours: float, cwd: pathlib.Path
) -> Verdict:
    v = Verdict(wt.path.name, True)
    if wt.is_here:
        return Verdict(
            wt.path.name, False, ["this is the checkout tidy.py is running in"]
        )
    dirty = [
        line
        for line in git_out("status", "--porcelain", cwd=wt.path).splitlines()
        if line
    ]
    if dirty:
        v.removable = False
        v.reasons.append(f"{len(dirty)} uncommitted change(s)")
    unpushed = git_out("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=wt.path)
    if unpushed not in ("", "0"):
        v.removable = False
        v.reasons.append(f"{unpushed} commit(s) on no remote")
    if wt.branch is None:
        evidence = f"already in {base}" if is_ancestor(wt.head, base, cwd) else None
    else:
        evidence = merged_evidence(wt.branch, wt.head, prs, base, cwd)
    if evidence is None:
        v.removable = False
        v.reasons.append("branch not shown merged")
    files = history_files(wt.path)
    if files:
        v.removable = False
        v.reasons.append(f"{files} file(s) of run history exist only here")
    touched = newest_git_touch(wt)
    if touched is not None and (time.time() - touched) / 3600 < idle_hours:
        v.removable = False
        v.reasons.append(
            f"git touched {(time.time() - touched) / 3600:.1f}h ago (a session may be live)"
        )
    if v.removable:
        v.reasons.append(evidence or "")
    return v


def orphan_dirs(
    main: pathlib.Path, registered: set[pathlib.Path]
) -> list[tuple[pathlib.Path, int]]:
    candidates: list[pathlib.Path] = []
    nested = main / ".claude" / "worktrees"
    if nested.is_dir():
        candidates += [p for p in nested.iterdir() if p.is_dir()]
    candidates += [p for p in main.parent.glob(f"{main.name}-*") if p.is_dir()]
    found = []
    for d in sorted(candidates):
        if d.resolve() in registered:
            continue
        real = 0
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            parts = set(p.relative_to(d).parts)
            if parts & VENV_NAMES or parts & IGNORED_DIR_PARTS:
                continue
            real += 1
        found.append((d, real))
    return found


def sh(path: pathlib.Path) -> str:
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--no-github",
        action="store_true",
        help="skip `gh pr list`; squash-merged branches are then missed",
    )
    ap.add_argument(
        "--idle-hours",
        type=float,
        default=2.0,
        help="a worktree git touched more recently than this is kept",
    )
    ap.add_argument(
        "--fetch", action="store_true", help="git fetch --prune origin first"
    )
    args = ap.parse_args()

    cwd = pathlib.Path.cwd()
    if git("rev-parse", "--git-dir", cwd=cwd).returncode != 0:
        print("not inside a git checkout", file=sys.stderr)
        return 2
    main_dir = main_checkout(cwd)
    if args.fetch:
        git("fetch", "--prune", "origin", cwd=cwd)
    base = "origin/master"
    if git("rev-parse", "--verify", "--quiet", base, cwd=cwd).returncode != 0:
        print(f"no {base}; fetch first (--fetch)", file=sys.stderr)
        return 2
    prs = None if args.no_github else fetch_prs(cwd)
    cmds: list[str] = []

    worktrees = list_worktrees(cwd)
    checked_out = {w.branch for w in worktrees if w.branch}

    print(f"== Worktrees (judged against {base} as last fetched)")
    for wt in worktrees:
        if wt.is_main:
            continue
        v = judge_worktree(wt, prs, base, args.idle_hours, cwd)
        print(
            f"  {'REMOVE' if v.removable else 'KEEP  '} {v.name}  [{wt.branch or 'detached'}]  "
            + "; ".join(v.reasons)
        )
        if v.removable:
            cmds.append(f"git -C {sh(main_dir)} worktree remove {sh(wt.path)}")
            # Its branch is then free to delete in the same pass.
            checked_out.discard(wt.branch)

    print("\n== Local branches")
    local = git_out(
        "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads", cwd=cwd
    ).splitlines()
    for line in local:
        name, sha = line.split(" ")
        if name in PROTECTED or name in checked_out:
            continue
        evidence = merged_evidence(name, sha, prs, base, cwd)
        if evidence:
            print(f"  delete {name}  ({evidence})")
            cmds.append(f"git -C {sh(main_dir)} branch -D {name}")

    print("\n== Branches on origin")
    remote = git_out(
        "for-each-ref",
        "--format=%(refname:lstrip=3) %(objectname)",
        "refs/remotes/origin",
        cwd=cwd,
    ).splitlines()
    for line in remote:
        name, sha = line.split(" ")
        if name in PROTECTED:
            continue
        evidence = merged_evidence(name, sha, prs, base, cwd)
        if evidence:
            print(f"  delete {name}  ({evidence})")
            cmds.append(f"git -C {sh(main_dir)} push origin --delete {name}")

    print("\n== Directories git no longer tracks")
    registered = {w.path.resolve() for w in worktrees}
    for d, real in orphan_dirs(main_dir, registered):
        if real == 0:
            print(f"  delete {d.name}  (no files outside .venv and caches)")
            cmds.append(f"powershell -Command \"Remove-Item -Recurse -Force '{d}'\"")
        else:
            print(
                f"  KEEP   {d.name}  ({real} file(s) outside .venv; compare against the repo's objects before deleting)"
            )

    behind = git_out("rev-list", "--count", f"master..{base}", cwd=main_dir)
    if behind not in ("", "0"):
        print(
            f"\nnote: local master is {behind} commit(s) behind {base}; `git branch -d` will refuse merged branches until it is pulled."
        )
    if prs is None and not args.no_github:
        print("note: GitHub was unreachable; squash-merged branches are not shown.")

    print("\n== Commands (nothing has been run)")
    if cmds:
        print("\n".join(cmds))
        print(
            "\nCheck the session list first: a running session cannot be seen from here."
        )
    else:
        print("  nothing to clean up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
