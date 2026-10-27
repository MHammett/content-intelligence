# GitHub as a document store (design)

Status: **built**, except the optional last phase. The design is
[#400](https://github.com/MHammett/content-intelligence/pull/400); it was built
in phases, each a GitHub issue labelled `github-documents`:

| Phase | What | PR |
|---|---|---|
| client | `ci_core.github_repo`, the REST client with the never-clobber commit | [#407](https://github.com/MHammett/content-intelligence/pull/407) |
| `ci-docs init` | creates the document repository and keeps its onboarding files current | [#408](https://github.com/MHammett/content-intelligence/pull/408) |
| `--github SLUG` | reads a round from the repository | [#409](https://github.com/MHammett/content-intelligence/pull/409) |
| `--write-back`, `ci-docs push-run` | commits the review back | [#410](https://github.com/MHammett/content-intelligence/pull/410) |
| `ci-docs ls` and `put`, the docs, the claude.ai check | close-out notes from a Code session | [#PRNUM](https://github.com/MHammett/content-intelligence/pull/PRNUM) (issue #404) |

Copying runs from the document repository into a history root (#405) is not
built. The issues are the specification a builder worked from, and where this
page and an issue differed the issue won; this page is the reasoning behind
them and says below where the build changed it.

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
file and makes one commit per file; `push_files`, several files in one commit;
`create_branch`; `create_pull_request`), and users report it working as a
claude.ai custom connector since June 2026, though not for repositories owned by
an organization ([github/github-mcp-server#549](https://github.com/github/github-mcp-server/issues/549)).
It does work on this deployment's account, but only with the setup in
[Setting up claude.ai](#setting-up-claudeai), which differs from what the
connector's defaults suggest. If it fails, the author commits the drafting side's
files by hand on github.com, and nothing below changes.

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
(message, `Reviewed:` line, `document_source` in the report). Phase 5 (#404)
added three things the drafting side must do, in the packaged `AGENTS.md.tmpl`
and nowhere else:

- **Close-out notes:** list the folder before writing one, name it
  `<slug>-close-out-rN-to-(code|chat)[-vM].md` (a `-vM` needs `-v(M-1)`), and
  never overwrite one.
- **One commit for a draft and its metadata** needs the connector's `push_files`
  tool; `create_or_update_file` makes one commit per file.
- **The history key:** a handoff or metadata file with no `History key:` gets the
  article's folder name under `--github`, not the title that the templates'
  bracketed note describes (see [The history key](#the-history-key)). The notes in
  `draft_submission.template.md` and `metadata_only.md` now say so; no label or
  header was renamed.

**Publication-specific instructions stay the author's.** The drafting project's
instructions in Drive (`mikehammett-project-instructions.md`) carry voice and
editorial rules, not workflow, and this code repository is public. So the
bootstrap seeds `instructions/publication.md` from a file the author supplies, once,
and never touches it again; it is edited in the document repository like an
article. The workflow half of those instructions (file storage, naming, the
close-out list CLOSE-OUT.md asks for) moves into `AGENTS.md`, and the Drive copy
retires.

## Setting up claude.ai

Checked live on 2026-10-27 against a claude.ai project and a scratch repository.
**Reading and writing are two separate setups**, and the first is enough for a
project that only needs to see the pipeline's reviews.

**Reading: the built-in GitHub integration.** It is read-only: it syncs the files
you pick into the project's knowledge. A test project with the
`instructions/claude-project.md` stanza at the top of its instructions, and
`AGENTS.md` and `templates/` attached as knowledge, answered "how do I submit a
draft?" correctly from `AGENTS.md`. Use "Sync now" after a review has been
committed.

**Writing: a custom connector.** claude.ai can only commit through GitHub's remote
MCP server, added as a custom connector at `https://api.githubcopilot.com/mcp/`.
The authentication has to be set up like this:

1. On github.com, register an OAuth App (Settings, Developer settings, OAuth
   Apps, New OAuth App). Give it any name. GitHub's form requires a homepage URL;
   any URL will do. Set **Authorization callback URL** to
   `https://claude.ai/api/mcp/auth_callback`. Create it, then generate a client
   secret. Keep the **client ID** and the **client secret**.
2. In claude.ai, add the custom connector with the URL above. For its OAuth
   setting choose **Use your own OAuth client** and enter that client ID and
   secret.
3. Connect, and approve the sign-in at GitHub.

Why the other options do not work, so nobody spends time on them:

- **"Register automatically"** needs the server to accept dynamic client
  registration, and **"Use Claude's published identity"** needs it to accept a
  client-ID metadata document. GitHub's OAuth server metadata
  (`https://github.com/.well-known/oauth-authorization-server/login/oauth`)
  advertises neither: it has no registration endpoint and no client-ID-metadata
  support. Neither option can work against GitHub.
- **A fine-grained token in an `Authorization: Bearer` header, with "No
  sign-in"**, fails with "Couldn't connect", although the same request works from
  curl. That is a claude.ai bug
  ([anthropics/claude-ai-mcp#1109](https://github.com/anthropics/claude-ai-mcp/issues/1109)).
  It would have been the better choice, because a fine-grained token can be
  limited to the one document repository.

**The cost of the OAuth App route.** The connector asks GitHub for the broad
`repo` scope, and an OAuth App token cannot be narrowed to one repository, so
what the connector can reach is **every repository the account can**, private
ones included. `AGENTS.md` tells the drafting side to commit to `main` and never
to modify a numbered file, but that is an instruction to the model, not a
permission GitHub enforces. This is the price of writing from claude.ai; if it is
too high, use the fallback below. The App's grant can be revoked at any time on
github.com (Settings, Applications). Use a personal-account repository: the
connector has been reported not to reach organization-owned ones
([github/github-mcp-server#549](https://github.com/github/github-mcp-server/issues/549)).

**Verified through the connector:** a project chat read `AGENTS.md` from the
articles repository, and committed a file to a scratch repository with
`create_or_update_file` (commit `acd7f54`).

**Not yet verified**, to do once by the maintainer:

- [ ] `push_files` commits a draft and its metadata together in **one** commit.
  `AGENTS.md` tells the drafting side to use it for that pair, because
  `create_or_update_file` makes one commit per file.
- [ ] "Sync now" in the project's built-in integration shows the pipeline's
  `<slug>-review-rN.md` after a `ci-review --github SLUG --write-back` run.

**Fallback.** If the connector is not worth its scope, or stops working, the
author commits the drafting side's files on github.com by hand: Add file, then
Upload files (or Create new file), putting a draft and its metadata in one commit
with the message `draft: <slug> rN`. Nothing else in this design changes, and the
review still comes back by `ci-review --github SLUG --write-back`.

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

**Built in #402.** The defaults are `config_loader.GITHUB_DOCUMENTS_DEFAULTS`, and
the default repository is the one constant `DEFAULT_GITHUB_DOCUMENTS_REPO`.
`merge_configs` returns the block normalised: every key filled, a key left empty
in YAML read as unset, and a block that is not a mapping ignored, so a mistake in
it never stops a run that does not use `--github`. `repo: ""` is the exception:
it is an instruction, and `--github` refuses it. `test_github_documents_wiring.py`
proves the block survives `merge_configs` under every cost preset. `write_back`,
`include_capture` and `allow_public_repo` are read by write-back (#403).

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
| `--write-back` / `--no-write-back` | With `--github`: commit the review back, or don't, overriding `write_back`. **Built in #403**, below. |

`--round`, `--github-ref` and `--github-repo` are usage errors without `--github`,
`--round` must be 1 or more, and `--github` is also a usage error with `--metadata`
(the round's metadata file is read from the repository) or `--archive-only`.

```bash
uv run ci-review --github pjm-capacity-auction --publication mikehammett --write-back
```

**Which files a round reads.** For round N, in order: `<slug>-handoff-rN.md`
(parsed as `--draft` parses it); else `<slug>-draft-rN.md` with
`<slug>-metadata-rN.md` (as `--raw-draft --metadata`); else the draft alone (as
`--raw-draft`, with the same warning about lost context). The handoff is built in
`main()` and passed to `run_draft_pipeline(handoff=...)`, the route `--url` and
`--raw-draft` already take, so the review itself does not change. When the handoff
has no `Pipeline run:` line, the run number defaults to N.

As built (#402, `github_documents.py`):

- **Rounds** are read from the folder listing: names matching
  `^<slug>-(handoff|draft)-r([1-9][0-9]*)\.md$`. A lone metadata file is not a
  round, and `<slug>-draft-r4-old.md` or `-r04` are ignored. The highest round is
  numeric, so r10 follows r9.
- **One commit.** The ref is resolved to a commit SHA once, and every listing and
  read is at that SHA, so a draft and its metadata cannot come from two commits.
- **"No `Pipeline run:` line"** is read from the text
  (`handoff_parser.declares_run_number`), because the parsers answer 1 both for a
  missing line and for a line that says 1. The usual bump past run numbers already
  in local history still applies afterwards.
- **A public repository** logs one WARNING and the read continues; refusing a
  public repository is write-back's rule (`allow_public_repo`, #403).
- **Failures** are one plain message and exit 1, before any model call: an empty
  `repo`, no token for a private repository, a rejected token (401), a repository
  that is "missing, or not visible to this token", a missing ref, a missing folder
  (the message lists the article folders that exist), a missing round (it lists
  the rounds that exist), and a rate limit (it names the reset time).
- **`document_source`** in `report.json` is
  `{kind: "github", repo, ref, commit_sha, round, files: [{path, blob_sha}]}`, and
  is also present in a replay's report.

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

**As built (#403)**, in `github_writeback.py`. Where this section and the issue
differ, the issue won:

- **One code path.** `ci-review --github SLUG --write-back` and
  `ci-docs push-run REPORT_JSON` both call `push_saved_run`. Neither reads the
  other's state: the folder, the branch, the run number and the timestamp come
  from the saved report and its file name, so the retry needs no `path_prefix`.
  `ci-docs` already existed (#408), so `push-run` is a subcommand of it, not a
  new console script.
- **Pre-flight is part of the read.** `github_documents.load_handoff(...,
  write_back=True)` already holds the client, the repository info and the folder
  listing, so it checks the token, `can_push`, a private repository (or
  `allow_public_repo`) and that the branch is a branch there
  (`GitHubRepo.has_branch`; a tag or a SHA given to `--github-ref` cannot be
  committed onto) without a second client. The taken `<slug>-review-rN.md` names
  go to the handoff as `taken_run_numbers`, and `run_draft_pipeline` unions them
  with `hist.existing_run_numbers` before its one bump and its one warning.
  Only the review names are counted, as the issue says; a worklist or report
  name that is taken in `pipeline/` fails at write-back with `exists`.
- **Calibration flags.** The issue makes `--write-back` with `--only-model`,
  `--only-domain` or `--no-timeout` a usage error as well as `--replay` and
  `--offline`; the sentence above that says calibration runs are "treated like a
  replay" is now exact. With config's `write_back: true` those runs ignore it and
  log one INFO line. `--no-write-back` is accepted with any of them.
- **When it runs.** After `hist.save_run`, after the capture is saved and after
  the summary is printed, so the commit URL is the last line. A failure raises
  `GitHubDocumentsError`, which `main()` turns into one logged message and exit
  1; the message ends with the saved report's path and the `ci-docs push-run`
  line to retry, with `--config-dir` when it is not `configs`.
- **What push-run refuses.** A report with `_replay` in its path, one not named
  `run_N_<timestamp>_report.json`, one without a GitHub `document_source`, a
  missing `_review.md` or report, a branch that has gone, a public repository
  without `allow_public_repo`, and no push permission. A missing `_worklist.md`
  (or `_results.json` with `include_capture`) is a warning and the file is left
  out. A report that carries `replayed_from` but sits outside `_replay/` is
  pushed: copying it out is the explicit act the check is for.
- **Secrets.** `refuse_if_contains` is every non-empty string in the merged
  `api_keys`. `push-run` rebuilds that table from `--config-dir`, using the
  publication named in the report, and stops if the configuration cannot be
  loaded rather than skipping the scan.
- **The note about an edited draft** lists each draft file whose blob at the
  branch head differs from the blob the report recorded, one line each:
  `Note: <name> changed at its own path since it was read (blob a1b2c3d then,
  e4f5a6b now).`

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
permission, limited to adding new files, through `ci-docs put`. Built in #404
(`github_put.py`), where the issue's list is the specification and this page's
earlier one-line sketch (a close-out note only) was widened to match it:

```bash
uv run ci-docs ls example-article
uv run ci-docs put example-article example-article-close-out-r4-to-chat.md
uv run ci-docs put example-article example-article-close-out-r4-to-chat-v2.md
```

- **`ls SLUG`** lists the article folder, top level and then `pipeline/`, with each
  file's round and kind read from its name (`close-out to-chat v2`, `review`,
  `worklist`, `report`; anything else is `other`). It reads only, and a missing
  folder is reported with the article folders that do exist.
- **`put SLUG FILE`** commits the one file, by its own name, into the article
  folder, in one commit, with `GitHubRepo.commit_files`. The name has to be
  `<slug>-(draft|metadata|handoff|publication)-rN.md` or
  `<slug>-close-out-rN-to-(code|chat)[-vM].md` with `<slug>` equal to `SLUG`, `N`
  from 1 and `M` from 2; the pipeline's own `review` and `worklist` names and
  anything else are refused, before a request is sent.
- **Close-out `-vM` rules.** A `-vM` whose `-v(M-1)` (for `-v2`, the plain note) is
  not in the folder is refused. A note for a round and direction that already has
  one, written without `-v`, is refused and the next free `-vM` is suggested; the
  same for a `-vM` that exists with other bytes. A note for an article whose folder
  does not exist is refused (a typo in the slug would otherwise start a new
  folder). A draft, metadata, handoff or publication file may start the folder.
- **Never overwrite.** A path that exists with the same bytes is a no-op and makes
  no commit; with other bytes it is refused. Line endings are normalised to LF, so
  a CRLF file saved on Windows matches its LF copy.
- **Commit message** `<kind>: <slug> r<N>[ to-<dir>][ v<M>]`, for example
  `close-out: example-article r4 to-chat`; the kind is the file's own, so a lone
  metadata file is `metadata: ...` and `draft: <slug> rN` is the message for the
  pair the drafting side commits together.
- **Safety.** The file is scanned for the token and for every configured API key
  value, as write-back is (`secret_values`; the keys come from `--config-dir`,
  plus the publication's with `--publication`, and an unreadable configuration
  stops the command). A public repository is refused unless `allow_public_repo`
  is set, and so is a token that cannot push. `--github-repo` and `--branch` stand
  in for `github_documents.repo` and `branch`; the branch must be a branch.

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
   claude.ai project follows the repository's `AGENTS.md` (built; two of the
   checks are left to the maintainer, under [Setting up claude.ai](#setting-up-claudeai)).
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
