# Landing a pull request

How to get a pull request onto `master` in this repository without dropping commits. It is written for people and for coding agents alike. Every rule below was learned from a real failure; the PR-by-PR stories stay in those PRs.

## Repository facts

- `master` is protected with **"require branches to be up to date"** (strict): a PR can only merge when it contains the current tip of `master`.
- The required status checks, by name, are `ci (ci-core)`, `ci (ci-article-review)` and `ci (ci-style-profile)`. `gitleaks` also runs on every PR but is not required.
- **Auto-merge is off** for the repository (`allow_auto_merge` is false), so nothing merges itself: someone runs the merge, and `gh pr merge --auto` fails with "Auto merge is not allowed for this repository".
- Merge commits, squash merges and rebase merges are all enabled, and recent history uses both merge commits and squashes. Use the method the maintainer asks for; `--merge` is the default here.
- Several PRs often land within minutes of each other, so a branch that was current when you last looked is frequently behind by the time you merge.

## Before you call a PR ready

1. **The PR head must equal your `HEAD`.** After a push, GitHub's PR head can lag for a few seconds. Check it with the API, and wait until it matches:

   ```bash
   gh api --cache 0 repos/OWNER/REPO/pulls/N --jq '.head.sha, .commits, .changed_files'
   ```

   Merging a lagging head drops the newer commits without any error.

2. **After a rebase or merge over a moved `master`, read every function both sides touched.** Git merges a rewritten function without a conflict marker and can drop upstream's lines or leave yours unreachable, and a green suite does not prove it did not. `git merge-tree --write-tree A B` tests a merge without touching any working tree; re-run the combined suite on the result.

3. **Merge a stack top-down.** If PR B is based on PR A's branch, merging A first marks every PR in the stack MERGED while `master` gets one commit. Ask `master`, not the PR list: `git merge-base --is-ancestor <sha> origin/master`. To recover, open one PR from the top branch to `master`.

## Keep it current, then merge once

- **Behind `master`.** `git fetch`, list what landed and grep it against the files you changed (any overlap: stop and read it), then rebase. For a branch that is checked out in another worktree, ask GitHub to do it: `gh api -X PUT repos/OWNER/REPO/pulls/N/update-branch -f expected_head_sha=<head>`. That call returns at once and `mergeStateStatus` is `UNKNOWN` for a few seconds, so poll `gh pr view N --json headRefOid,mergeable,mergeStateStatus`.
- **Pushing.** `git push --force-with-lease=<branch>:<sha the remote holds now>`. If the lease is rejected, run `git ls-remote origin refs/heads/<branch>` first: someone else may have updated your branch. If the tree is identical, adopt it (`git reset --hard <remote-sha>`) and re-verify.
- **Fast gate, then the full suite.** Run the checks that take seconds first (`ruff`, `pytest packages/ --collect-only -qq`, `uv lock --check`), push, then run the full local suite while `gh pr checks N --watch` runs. Never rebase while the suite is running. Put loops in a script file: a trailing `&` once backgrounded a whole `&&` chain.
- **Read the checks by name.** An empty `statusCheckRollup` right after a push looks like "nothing failing". `gh pr checks` lists a matrix job that fail-fast cancelled as "fail". `--watch` exits non-zero when the connection drops. A `mergeStateStatus` of `UNKNOWN`, or "base branch policy prohibits the merge" with green checks, is lag: poll for about 30 seconds, and treat anything other than `CLEAN` as "update the branch again".
- **Merge with one direct call, pinned to the head you verified:**

  ```bash
  gh pr merge N --merge --match-head-commit <full sha>
  ```

  Do not add `--delete-branch` from a worktree: `gh` tries to switch the local checkout and delete the local branch, which fails or misfires when either is checked out in another worktree.
- **Prove it landed.** A successful merge prints nothing. Check `merged` and `merged_at` from the API (`merge_commit_sha` is also set on open PRs, so it proves nothing), `git merge-base --is-ancestor <sha> origin/master`, and an empty `git diff origin/master <branch> -- <files>`. For the CI run on `master`: `gh run list --branch master --commit <FULL sha>`.
- **Dependency-line conflicts in `pyproject.toml`.** Keep both blocks, `git checkout origin/master -- uv.lock`, run `uv lock`, and check that the lock diff is only insertions of your packages: a fresh resolve can bump your own dependency, so re-run its test.

## After it lands: clean up after yourself

Nothing else removes what a task leaves behind, and it piles up: merged branches on `origin`, local branches, worktrees, and the directories git forgets about. Once "Prove it landed" has passed:

1. **Remote branch.** If `gh api repos/OWNER/REPO --jq .delete_branch_on_merge` prints `true`, GitHub already deleted it. If it prints `false`, delete it yourself with `git push origin --delete <branch>`, after the merge, never with `gh pr merge --delete-branch` (see above).
2. **Run history first.** Anything in your worktree's `pipeline_history/` that is not under `CI_HISTORY_ROOT` is lost when the worktree goes. Copy what you still need ([REPLAY-AND-HISTORY.md](REPLAY-AND-HISTORY.md), "Before you remove a worktree").
3. **Local branch and worktree.** You cannot remove the worktree you are standing in. If a session is the only thing using it, say in your closing message that the worktree is ready to remove and why, so the maintainer can archive the session. Otherwise, from the main checkout: `git worktree remove <path>`, then `git branch -D <branch>`. Use `-D`: a squash-merged branch is not an ancestor of `master`, so `-d` refuses it, and so does a local `master` that is behind `origin/master`.
4. **Never `rm -rf` a worktree.** That deregisters nothing and leaves a directory with no `.git`, where `git status` quietly reports the *main checkout* instead. If `git worktree remove` fails on a `.venv` (hardlinked `.pyd` files that Windows will not delete while anything has them open), find what holds it and report that. Do not force it, and do not leave the half-deleted directory behind without saying so.

**To clean up other sessions' leftovers, run `uv run python tools/tidy.py`.** It lists merged branches (local and on `origin`), worktrees that are clean, merged, idle and free of run history, and directories git no longer tracks, then prints the commands. It deletes nothing: deleting waits for the maintainer's word, and it cannot see a running session, so check the session list before running what it prints. Do not run its commands for worktrees or branches you did not create unless the maintainer has said to.

## If your tool's approval layer refuses the merge

Some agent tools put a classifier or an approval step in front of `gh pr merge`, and it can refuse the merge even after the maintainer said "merge", and not deterministically (the same command has been allowed for one PR and refused for the next). Do all the read-only preparation first and make one direct attempt. If it is refused, stop: refresh the head SHA so the pinned command is current, then give the maintainer the exact command to run themselves, noting that `master` may move before they do. Do not retry through a script, `gh api .../merge`, `--auto` or an auto-merge setting. When the maintainer repeats "merge", refresh the head first and make one more attempt.

## A keep-current loop that never merges

One script can keep ONE PR current and print `READY <sha>` when it reaches `CLEAN`. On `BEHIND` it fetches and stops (exit 2) if what landed touches a regular expression of your files; otherwise it calls `update-branch` and polls. On every new head, the commit you reviewed must still be an ancestor, and `git diff --name-only $(git merge-base origin/master <head>) <head>` must equal the PR's original file list (otherwise exit 5). The merge itself is a separate direct call, seconds later.
