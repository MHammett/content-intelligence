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

`configs/user.yaml` sets `cost_preset: maximum`, so a bare `ci-review` is the most expensive thing you can run. **Cost scales with draft size, so never quote it without one.** The old line here — "30 calls at $2.50–5.00" — was unsized, undercounted the calls, and used a stale price. Two `maximum` runs, both 2026-09-09, both re-costed 2026-09-17:

| draft | API calls | reported then | **corrected** |
|-------|----------:|--------------:|--------------:|
| 18,167 chars (~2,900 words) | 100 | $7.37 | **$6.09** |
| 135,514 chars (19,457 words) | 217 | $10.96 | **$9.56** |

Three things to carry from that table:

- **"30 calls" counted only the ensemble** (6 models x 5 domains). The real total is 3–7x that once citation verification, citation re-asks and the two SEO passes are counted — 100 calls on the small draft, 217 on the large one.
- **Every figure is a floor, not a total.** Retried attempts that the provider billed while reporting zero usage are priced at $0.00 (`uncosted_calls` in `cost_summary`: 3 on the small run, 8 on the large). The report says so itself — "at least — 3 retried attempt(s) were billed by the provider with no usage reported". The true bill is above the corrected number by an unknown amount; closing that gap needs provider billing, not the call log.
- **Cost is sub-linear in draft size.** 7.5x the characters cost 1.6x the money, because generation time tracks output length and output is bounded by what each domain prompt asks for — the same reason `configs/timeouts.yaml` uses a sub-linear size multiplier.

Both corrected figures come from re-pricing `gpt-5.6-sol`, which sat at a stale pre-price-cut rate until 2026-09-17 (see its block in `configs/pricing.yaml`). **Only `maximum` uses sol** — `thorough`/`balanced` run terra, `wide`/`economy` run luna — so of the four measured rows in the table below, only the `maximum` one is overstated; that $10.27 average is uncorrected and its corrected value is near $9.56. Any other pre-2026-09-17 cost figure in this repo is high only if sol ran.

That is the right preset for reviewing a real article and the wrong one for checking that your code works. With several sessions active at once, each verifying once, that default is the single largest avoidable cost in this repo.

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

Measured 2026-09-05 over 12 live runs: `wide` beat the retired `standard` preset on every axis at 55% of the cost, so `wide` is a sound working default and not a degraded one.

Measured 2026-09-08 on a 19,457-word real article (dc-environment-v26), 3 isolated runs per condition, cross-run reproducibility scored against `wide` and `maximum` as anchors — the first measurement of anything above `wide`:

|            | cost (avg) | overall repro% | fact-check repro% |
|------------|-----------:|----------------:|--------------------:|
| `wide`     | $0.38      | 6.8%            | 0.0%                |
| `balanced` | $1.28      | 6.4%            | 1.1%                |
| `thorough` (claude-sonnet-5) | $1.74 | 16.9% | 22.1%      |
| `maximum`  | $10.27 †   | 20.0%           | 29.6%               |

† Priced with the stale `gpt-5.6-sol` rate — see above; corrected it is near $9.56. The other three rows do not use sol and are unaffected, so the *ratios* this table was built to compare still hold; `maximum` is ~25x `wide`, not 27x.

Two conclusions came out of this and are now reflected in `configs/presets.yaml`:

- **`thorough` was upgraded**: its Claude model changed from `claude-opus-5` to `claude-sonnet-5` (same `effort: high`), because the swap alone captures 84% of `maximum`'s overall reproducibility and 75% of its fact-check reproducibility at 17% of the cost — a far better marginal trade than `maximum` represents over `wide`. `maximum` still exists for the cases that need that last stretch of fact-check reliability and can afford 6x more for it; `thorough` is now the reasonable middle tier this repo didn't have before.
- **`balanced` is not a good value** — 6.4% repro is statistically indistinguishable from `wide`'s 6.8% at 3.4x the cost — but it has not been retired or changed; it is still the shipped default in `configs/user.example.yaml`. That's a separate, larger decision than this measurement made on its own.

See `configs/presets.yaml` (the note above the `maximum:` block) for the full comparison, including why stock opus-5 `thorough` was scouted but not taken to a full study once the sonnet-5 numbers came back this strong. Full research writeup — methodology, the historical cost-drift check, vendor/system currency audit, and the revision-cycle practice this implies — is in `PLAN.md` §5.6, not just this summary.
