# GitHub as a document store (design)

Status: **design, partly built.** The client (#401) and `ci-docs init` (#406)
are built; the rest is not. The build is split into phases, each a GitHub
issue labelled `github-documents`; the issues are the specification a builder
works from, and this page is the reasoning behind them.

An article is drafted in a chat project and reviewed by `ci-review`. Today the two
sides exchange files through a shared Drive folder and by pasting (see
[CLOSE-OUT.md](CLOSE-OUT.md)). This design adds a GitHub repository, separate from
this code repository, as an optional place where the drafting side commits drafts,
metadata and hand-offs, `ci-review` reads a draft, and `ci-review` commits its
review back. The repository is the private `MHammett/mikehammett.net-articles`.
This repository creates it and keeps its onboarding files current, so any AI client
attached to it learns the rules from the repository itself. Versioning becomes git's: every round is a commit, nothing is
overwritten, and either side can see what the other did and when.

It is **optional and additive.** It is selected per run with a new flag. The
file, URL and Drive-and-paste paths keep working exactly as they do now (see
[What does not change](#what-does-not-change)).

## Where documents enter and leave today

**In.** Every review starts from a handoff dictionary built in one of four ways,
all in `pipeline.py`:

| Flag | What reads it | Becomes the handoff via |
|---|---|---|
| `--draft HANDOFF` | `run_draft_pipeline` → `_read_handoff_file` | `parse_draft_submission` (Template A, one file) |
| `--raw-draft DRAFT [--metadata META]` | `main()` → `_read_handoff_file` | `build_handoff_from_raw_text`, or `build_handoff_from_raw_draft_and_metadata` (the two-file form, `metadata_only.md`) |
| `--url URL` | `main()` | `build_handoff_from_url` |
| `--publish HANDOFF` | `run_publish_pipeline` | `parse_publication_handoff` (Template C); images resolve relative to the handoff's own directory |

`--archive-only` reuses the `--draft`/`--raw-draft` path. `--replay` and
`--retry-failed` still need one of the draft-loading flags.

**Out.** `history.save_run` writes, under `pipeline_history/<key>/` (or
`$CI_HISTORY_ROOT/<key>/`; a replay under `_replay/<key>/`):

| File | Who needs it on the drafting side |
|---|---|
| `run_N_<ts>_review.md` | the drafting chat: SECTION 1–10 and the SEO blocks are pasted into it with `revise_after_review_prompt.md` |
| `run_N_<ts>_worklist.md` | the author only; deliberately **not** given to the revision model |
| `run_N_<ts>_report.json` | nobody on the drafting side; it carries `corrected_draft` (the LanguageTool-corrected text) and everything else |
| `run_N_<ts>_corrections.log` | nobody; the grammar changes, one per line |
| `run_N_<ts>_results.json` | nobody; the ensemble capture `--replay` reads |

Two further documents change hands but no code writes them: the **close-out
note** (Template D, `close_out.md`), written by a Code session or the drafting
chat, and the **publication handoff** (Template C), written by the drafting chat
and read by `--publish`. Measured over 23 recent runs: `review.md` averages 451 KB
(largest 1.2 MB), `report.json` 756 KB (largest 2.0 MB), `worklist.md` 10 KB.

## What already exists, and the decisions taken

**The drafting side's access to GitHub.** claude.ai's built-in GitHub integration
is **read-only**: it adds selected files and folders of one branch to a chat or to
a private project's knowledge, refreshed by "Sync now", and it cannot commit,
branch or open a pull request ([Anthropic's page](https://claude.com/docs/connectors/github)).
Writing needs a connector. GitHub's own remote MCP server
(`https://api.githubcopilot.com/mcp/`) has the tools (`get_file_contents` with a
`ref`; `create_or_update_file`, which needs the existing blob's `sha` to replace a
file; `push_files`, several files in one commit; `create_branch`;
`create_pull_request`), and users report it working as a claude.ai custom
connector since June 2026 with its default toolsets, though not for repositories
owned by an organization ([github/github-mcp-server#549](https://github.com/github/github-mcp-server/issues/549)).
That is **not yet verified on this deployment's account**; it is the first
acceptance check of the docs phase. If it fails, the author commits the drafting
side's files by hand (github.com's upload page), and nothing below changes.

**Clone or API.** Considered: driving a local clone with `git` or `gh`, and the
REST API. Chosen: **the REST API, with no local clone.** Reasons:

- A clone is shared mutable state on disk. This repository's worst recurring
  failures are exactly that (one `HEAD` and one stash stack shared by concurrent
  sessions; see `CLAUDE.md`). An API call has no working tree to collide in.
- The author's machine checks out with `core.autocrlf=true`. A clone would turn
  every draft CRLF on disk and depend on per-clone settings to commit LF back. The
  API moves exact bytes.
- The API has the compare-and-swap this design needs. Reads return the blob and
  commit SHAs. A multi-file commit through the Git Data API (blobs → tree on
  the head's tree → commit with the head as parent → update the branch ref with
  `force: false`) lands **only as a fast-forward**, so it cannot clobber a commit
  the drafting side made in between; GitHub refuses the ref update instead
  ([refs API](https://docs.github.com/en/rest/git/refs)). The Contents API's
  per-file `PUT` with `sha` gives the same check for one file, but one commit per
  file and GitHub warns that concurrent `PUT`s conflict.
- Testing: an in-process fake of about nine endpoints, in the style of the suite's
  `FakeWordPress`, needs no git binary and no network.

**Library.** Considered PyGithub (LGPL-3.0, pulls in PyNaCl and PyJWT), githubkit
(MIT, pulls in pydantic, httpx and hishel), ghapi (pulls in the fastcore family)
and `gh api` in a subprocess. Chosen: **a thin client on `requests` through
`ci_core.http`'s OS-trust session.** The surface is about nine endpoints
(repository, ref, commit, contents and directory listing to read; blob, tree,
commit and ref update to write), `requests` is already a ci-core dependency, and going through `ci_core.http` keeps the
certificate handling this repository already had to fix once (certifi dropping a
root Windows trusts). A library would add a dependency tree larger than the code
it replaces. Revisit if the surface grows past about a dozen endpoints; PyGithub
would then be the candidate. `gh` is used for one thing only, as a token source
(below).

**Size.** The Contents API returns a file inline only up to 1 MB, and up to
100 MB with the raw media type ([contents API](https://docs.github.com/en/rest/repos/contents)),
so the client always reads raw and writes through the blobs API.

## The document repository

**`MHammett/mikehammett.net-articles`**: private, owned by the author's personal
account (the connector above reportedly cannot reach organization repositories),
default branch `main`. It is the default of `github_documents.repo` and can be
overridden there or per run. Its root describes itself to whatever attaches to it
(next section); the articles live under `articles/`:

```
AGENTS.md            the working rules for any AI client or person (managed)
CLAUDE.md            one line, `@AGENTS.md`, for Claude Code (managed)
README.md            the layout and the review loop, for people (managed)
.gitattributes       `* text=auto eol=lf` (managed)
templates/           Templates A, C and D and the revise prompt (managed copies)
instructions/
  claude-project.md  the stanza pasted into the chat project's instructions (managed)
  publication.md     the publication-specific drafting instructions (seeded once, then the author's)
.ci-docs/manifest.json  what the bootstrap wrote, from which source commit
articles/<slug>/
  <slug>-draft-r4.md                  drafting side
  <slug>-metadata-r4.md               drafting side (the two-file form)
  <slug>-handoff-r4.md                drafting side (Template A in one file, if used instead)
  <slug>-publication-r6.md            drafting side (Template C)
  <slug>-close-out-r4-to-code.md      either side, per CLOSE-OUT.md
  <slug>-review-r4.md                 ci-review: run 4's review.md
  pipeline/
    <slug>-worklist-r4.md             ci-review: run 4's worklist.md
    run_4_20261026_143022_report.json ci-review: run 4's report, under its history name
    run_4_20261026_143022_results.json  optional: the ensemble capture
```

- `<slug>` is the article's folder name and the stem of every file in it, the same
  string the drafting project already uses in Drive.
- The `-r<N>` names are the ones [CLOSE-OUT.md](CLOSE-OUT.md) already defines:
  `r<N>` on a review or a close-out note is the pipeline run number, so
  `<slug>-review-r4.md` is run 4's review.
- **The top level is what the chat reads; `pipeline/` is not.** The worklist sits
  there because it is the author's list of documents nobody has read yet, which
  the README says never to hand to the revision model. The report keeps its
  `pipeline_history` file name so a run can be copied back into a history
  directory unchanged.
- No file under `articles/` is ever modified or deleted by the pipeline. A new
  round is a new file. Only the bootstrap rewrites anything, and only the files it
  manages.

## Making the repository self-describing

Every client that attaches to the repository has to learn the same rules: the
layout, the names, the branch, the commit messages, how a round is submitted and
where its review comes back, and what never to do. They go in **`AGENTS.md`**, the
tool-neutral name, written once. What each client reads on its own differs, so
the design meets each one where it looks:

| Client | What it reads by itself | What makes it read `AGENTS.md` |
|---|---|---|
| Codex and other agents following the AGENTS.md convention | `AGENTS.md` at the root | nothing more |
| Claude Code | `CLAUDE.md`; recent releases fall back to `AGENTS.md` only when no `CLAUDE.md` exists, behind a setting | a root `CLAUDE.md` holding `@AGENTS.md`, the import form, which works on every release |
| claude.ai project, built-in GitHub integration | **nothing automatically**: only the files and folders picked for project knowledge, refreshed by "Sync now"; project instructions are a separate field ([Anthropic's page](https://claude.com/docs/connectors/github)) | the project's instructions start with `instructions/claude-project.md`, which says to read `AGENTS.md` first; `AGENTS.md`, `templates/` and the current article's folder are the files picked for knowledge |
| claude.ai with GitHub's MCP connector | nothing until a tool call reads a file | the same stanza: it names the repository and tells the model to `get_file_contents` `AGENTS.md` before anything else |

Pick knowledge narrowly. Knowledge that outgrows the context window switches the
project to retrieval, where a rule in `AGENTS.md` is found only if a search hits
it. `AGENTS.md`, `templates/` and one article's top level stay small; the whole
repository, with `pipeline/` reports of up to 2 MB, would not.

`AGENTS.md` covers: the layout above; the names (`<slug>-draft-rN.md`,
`<slug>-metadata-rN.md`, `<slug>-handoff-rN.md`, `<slug>-publication-rN.md`,
close-out notes, `<slug>-review-rN.md`); commit only to `main`, never with force;
the commit messages (`draft: <slug> rN`, `close-out: <slug> rN to-code`;
`review: <slug> rN` is the pipeline's); **submitting a round** (commit the draft
and its metadata together in one commit, then ask the author to run
`ci-review --github <slug>`); **where the review comes back**
(`<slug>-review-rN.md` at the folder's top level, after "Sync now"; paste it with
`templates/revise_after_review_prompt.md`); **never** rename an article folder,
modify or delete a numbered file, rewrite history, force-push, commit a secret or
read `pipeline/` into a revision; and how to read the pipeline's commits
(message, `Reviewed:` line, `document_source` in the report).

**Publication-specific instructions stay the author's.** The drafting project's
instructions in Drive (`mikehammett-project-instructions.md`) carry voice and
editorial rules, not workflow, and this code repository is public. So the
bootstrap seeds `instructions/publication.md` from a file the author supplies, once,
and never touches it again; it is edited in the document repository like an
article. The workflow half of those instructions (file storage, naming, the
close-out list CLOSE-OUT.md asks for) moves into `AGENTS.md`, and the Drive copy
retires.

## Bootstrapping the repository: `ci-docs init`

Built (#406). This code repository owns the scaffolding: `ci-docs init` creates
the document repository when it is missing and keeps its onboarding files
current.

```bash
uv run ci-docs init --dry-run                           # the plan; writes nothing
uv run ci-docs init --create --seed-instructions PATH   # first run
uv run ci-docs init                                     # refresh, any time after
```

`--repo OWNER/NAME` defaults to `github_documents.repo` in `user.yaml` (read
defensively, so the block may be absent), else `MHammett/mikehammett.net-articles`;
`--branch` defaults to `github_documents.branch`, else `main`; `--config-dir`
says where `user.yaml` is. The token is `GITHUB_TOKEN`, else `gh auth token`, and
is never printed.

1. **Create if missing.** A 404 from the repository is "missing" (GitHub also
   answers 404 for a repository the token cannot see, and the message says so).
   Without `--create` the run stops and prints the command; with it, after
   `gh auth status` succeeds, it runs `gh repo create OWNER/NAME --private
   --description "..."`. It never creates a public repository, and **refuses an
   existing public one**. `--dry-run` prints the same command and runs no `gh`
   command at all.
2. **Seed if empty.** A new repository, or an existing one with no commit, answers
   the Git Data API with 409, so `README.md` goes in with one Contents API `PUT`
   (`seed_empty`), which creates the branch. Everything after uses `commit_files`.
3. **Render and commit.** The managed files are rendered from packaged sources
   (below) and committed in **one commit** with `.ci-docs/manifest.json`,
   `onboarding: refresh from content-intelligence <short sha>`. The sha is
   `git rev-parse HEAD` in a checkout (`+dirty` appended when the sources have
   uncommitted changes), else the package version.
4. **Per managed path:** absent, add it; identical bytes, skip it; its current
   blob is the one the manifest recorded, replace it (nobody edited it there);
   anything else, leave it and list it as edited in the document repository,
   unless `--overwrite-edited`. The replace is compare-and-swap in
   `commit_files` (`replace={path: expected_blob_sha}`), so a file edited between
   the run's read and its commit is refused, not overwritten. With no manifest,
   every file that differs counts as edited. A re-run changes nothing, and makes
   no commit, when nothing differs; a newer source commit alone does not.
5. **Never touched.** Nothing under `articles/` is changed, except that
   `articles/README.md` is created once, while `articles/` is empty (it is not in
   the manifest and is never replaced). `instructions/publication.md` is created
   once from `--seed-instructions PATH` if it is absent, and never touched again;
   without the flag it is not created and the run says how to seed it. That file
   holds the editorial and voice rules, goes from a local path straight to the
   private repository, and must never be committed to this one. Line endings are
   normalised to LF.

**Where the files come from.** The five templates (`draft_submission.template.md`,
`metadata_only.md`, `publication.md`, `close_out.md`,
`revise_after_review_prompt.md`) are rendered from `handoff_templates/`, the files
`ci-setup` copies, so each has one source; they differ from it only by the
marker line. `AGENTS.md`, `CLAUDE.md`, `README.md`, `.gitattributes` and
`instructions/claude-project.md` come from
`packages/ci-article-review/src/ci_article_review/document_repo/`, where each has
a `.tmpl` suffix (`AGENTS.md.tmpl`, ...): a file called `AGENTS.md` or `CLAUDE.md`
in the package would be read by Codex and Claude Code as instructions for working
on *this* repository, and a `.gitattributes` there would apply to its directory.
`{{repo}}` and `{{branch}}` in them are filled from the run.

**Markers.** Each managed Markdown file starts with an HTML comment naming its
source path in this repository and saying to edit it there. `.gitattributes` has
its marker on a `#` line after the one rule. `CLAUDE.md` is exactly `@AGENTS.md`
and has no marker; it is recorded in the manifest only. The marker names the
source *path* and not the commit, on purpose: a commit in the marker would change
every managed file on every run and make every re-run a commit. The commit is in
the manifest's `source_commit` (the commit that last changed a managed file) and
in the commit message.

A change to Template A or D then reaches the drafting side by merging it here and
re-running `ci-docs init`. That replaces the hand-kept Drive copy that
`close_out.md`'s header comment and CLOSE-OUT.md warn about.

## Configuration

A new top-level block in `configs/user.yaml`, also added, commented out, to
`packages/ci-article-review/src/ci_article_review/configs/user.example.yaml`:

```yaml
github_documents:
  repo: MHammett/mikehammett.net-articles   # the default; owner/name
  branch: main            # optional; default: the repository's default branch
  path_prefix: articles   # the default; folder holding the article folders
  write_back: false       # default for runs that read from GitHub; --write-back overrides
  include_capture: false  # also commit run_N_*_results.json under pipeline/
  allow_public_repo: false  # refuse to write to a public repository unless true
```

Every key has a default, so the block is optional; nothing reads it unless a run
passes `--github` or `ci-docs` is called. `merge_configs` builds its result key by key, so the block has to be carried
through it explicitly or it is silently dropped (the same trap as `rate_limits`).

**Token.** Not in YAML: an unset `${VAR}` stops config loading for every run, and a
token has no business in a config file. Resolution order: the `GITHUB_TOKEN`
environment variable (`.env` is loaded before this runs), then the output of
`gh auth token`. The recommended token is a fine-grained personal access token
limited to the one document repository with Contents read and write. `gh`'s token
reaches every repository the account can, which is why it comes second.

## Command line

New flags, none of which changes an existing one:

| Flag | Meaning |
|---|---|
| `--github SLUG` | A fifth member of the required source group: read the article folder `SLUG` from the configured repository |
| `--round N` | With `--github`: which round to read. Default: the highest N with a draft or handoff file |
| `--github-ref REF` | With `--github`: a branch, tag or commit SHA to read at. Default: the configured branch |
| `--github-repo OWNER/NAME` | Override `github_documents.repo` for this run (a scratch repository for testing, for one) |
| `--write-back` / `--no-write-back` | With `--github`: commit the review back, or don't, overriding `write_back` |

```bash
uv run ci-review --github pjm-capacity-auction --publication mikehammett --write-back
```

**Which files a round reads.** For round N, in order: `<slug>-handoff-rN.md`
(parsed as `--draft` parses it); else `<slug>-draft-rN.md` with
`<slug>-metadata-rN.md` (as `--raw-draft --metadata`); else the draft alone (as
`--raw-draft`, with the same warning about lost context). The handoff is built in
`main()` and passed to `run_draft_pipeline(handoff=...)`, the route `--url` and
`--raw-draft` already take, so the review itself does not change. When the handoff
has no `Run:` line, the run number defaults to N.

## The history key

The folder name becomes the history key: when the handoff has no `History key:`,
the GitHub source sets it to `SLUG`, so `pipeline_history/<slug>/` and the
document folder are one string by construction. That removes the mismatch
CLOSE-OUT.md's `Names match:` line exists to catch. When the handoff does carry a
`History key:` and `history._slug` of it differs from the folder name, the run
warns, uses the `History key:` (history cannot be split, so it stays the fixed
point) and still writes back to the folder it read from.

`pipeline_history` is unchanged: every run still saves locally first, under the
same names. Write-back is a copy taken after `save_run`. The report gains a
`document_source` block (repository, ref, the commit SHA read at, and each path
and blob SHA read), so any report says which committed draft it reviewed.

## Writing back

**What, under which names.** From one run N: `review.md` → `<slug>-review-rN.md`;
`worklist.md` → `pipeline/<slug>-worklist-rN.md`; `report.json` →
`pipeline/run_N_<ts>_report.json`; with `include_capture`, the capture beside it.
Line endings are normalized to LF before committing (files saved on Windows are
CRLF).

**One commit.** All files go in a single commit on the configured branch:

```
review: <slug> r4

Reviewed: <slug>-draft-r4.md, <slug>-metadata-r4.md at 3f2a91c
Run: run_4_20261026_143022 (preset wide, $0.73 reported)
Files: <slug>-review-r4.md, pipeline/<slug>-worklist-r4.md, pipeline/run_4_..._report.json
```

**Never clobber.** The pipeline only adds paths; it never replaces one.

1. *Before any model call* (pre-flight, so a refusal costs nothing): the token
   resolves, the repository is reachable, the token can push, and the target
   names for this run number are free. When `<slug>-review-rN.md` already exists
   the run number is moved past it, the same way it is moved past a number that
   local history already has, and with the same warning.
2. *At write-back:* read the branch head, confirm every target path is still
   absent, build the tree on the head's tree, commit with the head as parent, and
   update the ref with `force: false`. If the drafting side committed in the
   meantime the update is refused; read the new head and try again, up to three
   times. Because the paths are new, a retry never merges content.
3. A target path that now exists with **identical** bytes is a no-op (a repeated
   write-back of the same run); with **different** bytes the write-back stops and
   overwrites nothing. There is no `--force`: a run that cannot use its name has
   the wrong run number, and the fix is a new number, not an overwrite.
4. When the draft that was reviewed has changed at its own path since it was read
   (an in-place edit, against convention), the commit body says so.

**A write-back that fails after the review** (network, conflicts exhausted, a
taken path) does not lose the review: the local files are already saved. The run
prints the error and how to retry, exits 1, and the retry is
`ci-docs push-run <path to run_N_<ts>_report.json>`, which writes back a saved
run without re-running it.

## Replay, offline and experiments

A replay's report is a code test, not a review of the article (it goes to
`_replay/`), so it must never reach the document repository:

- `--replay` with `--write-back` is a usage error; `write_back: true` in config is
  ignored on a replay, with one log line saying so. Reading from GitHub still
  works, since a replay needs a draft-loading flag.
- `--offline` means no network beyond reading the input, as with `--url`. It
  skips the passes that make Section 9 and the SEO blocks, so its review is not
  one to hand back: `--offline --write-back` is a usage error and config's
  `write_back` is ignored.
- `--only-model`, `--only-domain` and `--no-timeout` are calibration runs, treated
  like a replay.
- `--retry-failed` files into the article's real history, so it may write back.
- `--publish` and `--archive-only` do not read from GitHub in this design.

## Failure behaviour

| Condition | When it is caught | What happens |
|---|---|---|
| no token (private repository) | pre-flight | exit 1: set `GITHUB_TOKEN` or run `gh auth login` |
| 401 | pre-flight | exit 1: token rejected (expired or revoked) |
| 404 on the repository | pre-flight | exit 1: missing, or not visible to this token (GitHub answers 404 for both) |
| folder or round missing | pre-flight | exit 1, listing the rounds that exist |
| write-back asked, no push permission | pre-flight | exit 1, before any model call |
| public repository, write-back asked, `allow_public_repo` false | pre-flight | exit 1: drafts committed there are published |
| rate limited (403/429 with `x-ratelimit-remaining: 0`) | any call | stop and print the reset time; no retry loop |
| ref update refused (not a fast-forward) | write-back | re-read the head and retry, at most three times |
| any write-back failure | after the review | local files kept; exit 1; `ci-docs push-run` retries |

## Security

- The token travels only in the `Authorization` header: never in a URL, a log
  line, a report or an exception message. Error bodies pass through
  `ci_core.redact` with the token as a secret, and a test asserts the token is
  absent from captured logs.
- Before committing, every outgoing file is scanned for the token and every
  configured API key value; a hit refuses the write-back. `report.json` carries
  error text from providers, which is how a secret would get there.
- Drafts are unpublished work: the document repository should be private, the
  pipeline warns when it is public and refuses to write to it unless told to.
- This code repository is public. The document repository's name is in it (as
  a config default) by the author's choice; article slugs and contents are not.
  Tests and examples use invented slugs, and live tests use a scratch repository.
- The bootstrap commits only rendered files from packaged sources and the one
  file the author supplies; it scans them for secrets like any other write.
- Commits are authored by the token's owner. Nothing in this design deletes a
  file, rewrites history or uses `force`.

## The drafting side

Everything the chat project must do is in the document repository's
`AGENTS.md` ([above](#making-the-repository-self-describing)), so the chat
follows the repository and not a copy of these rules kept somewhere else.

**Close-out notes from a Code session.** CLOSE-OUT.md says Code has no standing
permission to write to the author's Drive. Here the configured repository is that
permission, limited to adding new files. `ci-docs put <slug> <file>` commits one
note with the same never-overwrite rule and checks its name against the pattern;
`ci-docs ls <slug>` lists a folder.

## What does not change

- `--draft`, `--raw-draft`, `--metadata`, `--url`, `--publish`, `--archive-only`,
  `--replay`, `--retry-failed` and `--offline` behave as they do today. Without
  `--github` no GitHub code runs and no token is looked for.
- `pipeline_history/`, `CI_HISTORY_ROOT`, the `run_N_*` names, the `_replay/` tree
  and `ci-history-report` are untouched.
- The Drive folder, the paste-based exchange and CLOSE-OUT.md's naming keep
  working; an article can move to GitHub at any round or never.
- No config is required: without a `github_documents` block nothing changes.

## Build phases

Each phase is an issue labelled `github-documents`, buildable from the issue
alone, in this order:

1. #401: the `ci_core` GitHub client.
2. #406: `ci-docs init`, which creates the document repository and keeps its
   onboarding files and templates current.
3. #402: reading a draft with `--github`.
4. #403: write-back and `ci-docs push-run`.
5. #404: `ci-docs put` and `ls`, the README, CLOSE-OUT.md, and the check that a
   claude.ai project follows the repository's `AGENTS.md`.
6. #405 (optional): copying runs from the document repository into a history
   root for `ci-history-report`.

## Not decided here

- Commit straight to the branch the chat reads (this design), or one branch per
  round merged through a pull request. The second adds a review gate and a merge
  step to every round, and the built-in integration reads one branch only.
- Reading a Template C publication handoff and its images from GitHub for
  `--publish`.
- Whether `write_back: true` in config is standing permission, or each run should
  ask with `--write-back`. The default here is the cautious one.
