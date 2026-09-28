---
name: starting-a-task
description: Use at the start of any task in this repository, and when a fresh worktree misbehaves (missing configs, a stub .env, git resolving to the wrong checkout), when a test touches the network or pipeline_history, or when a dependency lock changes. Covers checking nobody else is doing the task, verifying a brief's claims and the test baseline, the test-suite guard rails, and public-repository hygiene.
---

The details live in `docs/DEVELOPMENT.md`; read it and follow it. It is the single source, so this file stays short.

The short version:

- Before writing code, check that nobody else is already doing the task: fetch, sibling branches, open PRs and issues, and uncommitted work in other worktrees (no scan of refs can see that).
- A brief's claims about what is merged, and its test counts, go stale: check a SHA with `git merge-base --is-ancestor`, and measure the baseline yourself, per package and repo-wide, from the base you started on.
- A fresh worktree needs `uv sync`, a `--config-dir`, and a look at which `.env` will load.
- Tests must not touch the real `pipeline_history/` or the network; new guards need a positive control on the unfixed base.
- Stage files by explicit path, and never name an unpublished draft.
