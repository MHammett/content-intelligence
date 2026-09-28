---
name: landing-a-pr
description: Use before telling the maintainer a PR is ready, when a PR goes BEHIND master or its checks look odd, and when merging a PR in this repository. Covers verifying the head SHA, keeping a branch current under strict protection, one pinned merge, proving it landed, stacked PRs, and what to do when your tool refuses the merge.
---

The procedure lives in `docs/LANDING-A-PR.md`; read it and follow it. It is the single source, so this file stays short.

The short version:

- The PR head must equal your `HEAD` before you say "ready".
- `master` requires branches to be up to date and auto-merge is off, so keep the branch current, then merge once with `gh pr merge N --merge --match-head-commit <full sha>`.
- Prove it landed with `git merge-base --is-ancestor <sha> origin/master`; a PR list saying MERGED is not proof.
- Merge a stack top-down, never bottom-up.
- If your tool refuses the merge, stop and give the maintainer the exact pinned command.
