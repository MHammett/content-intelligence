# Working in this repo

**Multiple Claude Code sessions can be active in this checkout at the same time** (different tools/windows/tasks Mike is running concurrently). `HEAD` and the git stash stack are both single, repo-wide state — not scoped to a session or even to a `git worktree`.

**Always start any git work in a dedicated worktree — never edit, branch, commit, or stash directly in this main checkout,** even for what looks like a one-line change:

```bash
git worktree list                                  # see what's already isolated
git worktree add ../content-intelligence-<slug> -b <branch>
```

This has caused real collisions more than once: a branch-switch by one session moved `HEAD` out from under another session mid-task (2026-08-09), and a `git stash` from one session was popped into a different session's working tree with conflicts (2026-08-09, 2026-08-11) — because the stash stack and `HEAD` are shared across every worktree of the repo, not scoped to one. Being in a worktree protects against the branch-switch problem but **not** the stash-collision one.

**Avoid `git stash` entirely when there's any other way to achieve the goal** — e.g. `git show <ref>:<path>` to inspect another ref's version of a file without touching the working tree. If `git stash` is unavoidable, never chain `push`/`pop` in one command; run `git stash list` immediately before *and* after any stash operation to confirm nothing unexpected was touched.

**A new worktree needs its own `uv sync` before its tests mean anything.** The venv's editable install resolves `ci_article_review`/`ci_core` to whichever checkout ran `uv sync` last — usually the main checkout — so a fresh worktree's tests can silently run against the *main checkout's* source, not the worktree's edits, and still show green. After creating a worktree:

```bash
uv sync
uv run python -c "import ci_article_review; print(ci_article_review.__file__)"   # must print a path under THIS worktree
```

**Before treating a PR as done, recheck its mergeability, not just its status at open time:**

```bash
gh pr view <n> --json mergeable,mergeStateStatus,statusCheckRollup
```

This repo sees frequent concurrent PRs — a branch that opened cleanly can go `CONFLICTING` by the time you come back to it if other PRs landed on `main` in the meantime.

## Verifying a change without spending `maximum` money

`configs/user.yaml` sets `cost_preset: maximum`, so a bare `ci-review` makes 30 calls at **$2.50–5.00**. That is the right preset for reviewing a real article and the wrong one for checking that your code works. With several sessions active at once, each verifying once, that default is the single largest avoidable cost in this repo.

**Most changes need no model calls at all.** `--replay` re-runs everything downstream of ensemble dispatch — re-keying, consolidation, citations, the report — over a previously captured ensemble, and makes zero API calls. Every run writes its own capture to `pipeline_history/<key>/run_N_..._results.json`, so one live run per worktree buys unlimited free re-verification:

```bash
uv run ci-review --draft packages/ci-article-review/src/ci_article_review/handoff_templates/examples/draft_submission.short-example.full-coverage.md --publication mikehammett --cost-preset wide
```

then, for every iteration after that:

```bash
uv run ci-review --draft packages/ci-article-review/src/ci_article_review/handoff_templates/examples/draft_submission.short-example.full-coverage.md --publication mikehammett --replay pipeline_history/short-example-smoke-test/run_1_<ts>_results.json --offline
```

`--offline` additionally skips link validation, Wayback, citation resolution and the two SEO model calls. Note that a replay still prints `Estimated cost:` from the *captured* run's call log — it did not spend that; the "No model calls made" line above it is the true one.

Replay is a real verification for anything in consolidation, scoring, the report, citations or history. It is **not** sufficient for changes to assignment, dispatch, retry, recovery or substitution — those decide which calls get made, and a replay makes none. Verify those live, at `--cost-preset wide` (12 calls, ~$0.12), not at `maximum`.

Measured 2026-09-05 over 12 live runs: `wide` beat the retired `standard` preset on every axis at 55% of the cost, so `wide` is a sound working default and not a degraded one. Nothing above `wide` has been measured — see `configs/presets.yaml` for what is and is not evidenced.
