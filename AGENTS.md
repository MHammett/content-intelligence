# AGENTS.md

Guidance for any coding agent or human contributor working in this repository. Agents that read `AGENTS.md` (Codex and many others) start here. Claude Code reads `CLAUDE.md`, which currently holds the working rules.

**Read [CLAUDE.md](CLAUDE.md) first.** Despite its name it is not specific to one tool: it has this repository's working rules (worktree and git hygiene when several sessions share a checkout, how to verify a change without spending real money, and what a live run costs). The two files are meant to be merged, so that `AGENTS.md` is the single source and `CLAUDE.md` imports it.

## Where things are written down

| You are... | Read |
|---|---|
| landing or merging a pull request, or a PR has gone behind `master` | [docs/LANDING-A-PR.md](docs/LANDING-A-PR.md) |
| changing a provider, a preset, a price or a timeout | [docs/PROVIDERS.md](docs/PROVIDERS.md), [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| debugging a run or a report | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| touching citations, archive lookups or link checks | [docs/CITATIONS.md](docs/CITATIONS.md) |
| learning how the pipeline fits together | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| naming a package, module or concept | [docs/NAMING.md](docs/NAMING.md), [docs/TERMINOLOGY.md](docs/TERMINOLOGY.md) |

## Skills

`.claude/skills/` (Claude Code) and `.agents/skills/` (Codex) hold the same skills, because the two tools read different directories. Each `SKILL.md` is a short trigger that points at a document under `docs/`, where the procedure lives. Keep the two copies identical; a test checks it.

## Conventions that hold for every tool

- **Stage files by explicit path** (`git add <path>`), never `git add -A` or `git add .`. This repository is public, and untracked working files in a checkout can be private drafts.
- **Work that cannot be done now goes in a GitHub issue**, not in a private note or a TODO comment: what is still to do, who is doing it, and what closed it belong where everyone can see them. Close the issue when the work makes it moot.
