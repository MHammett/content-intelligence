---
name: replaying-a-run
description: Use before replaying a saved run, looking for where a run's output lives, comparing cost or counts from a replay or a saved report, or checking a change against real model output for free. Covers pipeline_history per checkout, --replay and --offline, what a replay does and does not prove, and the free recipes. Also use before removing a worktree that may hold the only copy of a run.
---

The procedure lives in `docs/REPLAY-AND-HISTORY.md`; read it and follow it. It is the single source, so this file stays short.

The short version:

- `pipeline_history/` is git-ignored and per checkout: find a run by what it contains, and removing a worktree deletes its runs.
- `ci-review --draft <handoff> --publication <name> --replay <run_N_..._results.json> --offline` costs nothing. `--replay` still needs a draft-loading flag, and a fresh worktree needs `--config-dir`.
- A replay proves classification, consolidation, the report and history. It cannot prove assignment, dispatch, retry, recovery or substitution.
- A replay's report is not an independent run, and the cost it prints is not money you spent.
