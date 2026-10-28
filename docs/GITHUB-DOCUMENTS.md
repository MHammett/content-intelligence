# GitHub as a document store (design)

Status: **redesigned; the redesign is not built yet.** The first design
([#400](https://github.com/MHammett/content-intelligence/pull/400)) gave every
round its own file (`<slug>-draft-r4.md`, `<slug>-review-r4.md`) and committed
everything straight to `main`. It was built:

| Phase | What | PR |
|---|---|---|
| client | `ci_core.github_repo`, the REST client with the never-clobber commit | [#407](https://github.com/MHammett/content-intelligence/pull/407) |
| `ci-docs init` | creates the document repository and keeps its onboarding files current | [#408](https://github.com/MHammett/content-intelligence/pull/408) |
| `--github SLUG` | reads a round from the repository | [#409](https://github.com/MHammett/content-intelligence/pull/409) |
| `--write-back`, `ci-docs push-run` | commits the review back | [#410](https://github.com/MHammett/content-intelligence/pull/410) |
| `ci-docs ls` and `put`, the claude.ai setup | close-out notes from a Code session | [#411](https://github.com/MHammett/content-intelligence/pull/411) |

The maintainer then asked for git to do the versioning instead of file names, for
issues and pull requests to carry the work on an article, and for the repository
to hold an article's **complete** history, including rounds and edits made before
it existed. This page is that redesign. Until its phases land, the code still
behaves as the first design says; that text is `git show
28dc0a8:docs/GITHUB-DOCUMENTS.md`, and [Today's behaviour](#todays-behaviour-until-the-redesign-lands)
below summarises it. The phases are issues labelled `github-documents`, listed
under [Build phases](#build-phases); where an issue and this page differ, the
issue wins, and the builder says so in the PR.

An article is drafted in a chat project and reviewed by `ci-review`. The two sides
exchange files through a shared Drive folder and by pasting (see
[CLOSE-OUT.md](CLOSE-OUT.md)), or, optionally, through a private GitHub repository,
`MHammett/mikehammett.net-articles`, separate from this code repository. It is
**optional and additive**: selected per run with `--github`, and `--draft`,
`--raw-draft`, `--url` and the Drive-and-paste workflow keep working exactly as
they do (see [What does not change](#what-does-not-change)).

## The shape of it

- Every article is one folder, `articles/<slug>/`, of files with **fixed names**:
  `draft.md`, `metadata.md`, `review.md`, `publication.md`, two close-out notes and
  `pipeline/`. A new round changes the files; git keeps every version.
- A **round** is a branch, `round/<slug>/rN`, and a pull request into `main`,
  opened by the drafting chat.
- `ci-review --github <slug>` reviews the pull request's head commit, commits the
  full review to the branch as `review.md`, and posts one **pull request review**
  whose body is a summary and a checklist of the review's consensus findings
  (F1, F2, …). No line comments.
- The chat revises on the branch, with commit messages that say which review run
  each revision answers and which findings it addressed or deferred, and may ask
  for another review. **The chat merges the round when the maintainer says so in
  that chat**; the next command that writes to the article (a write-back, `ci-docs
  put` or `accept`) tags the merge `<slug>/rN`. Nobody has to do anything on github.com.
- **The history is the changelog.** The maintainer expects to read GitHub rarely;
  it exists so that any AI client can read an article's history (what changed in
  each round, why, in answer to which review) and to replace carrying files
  through Drive or Dropbox. So pull request descriptions and commit messages are
  held to a standard (`AGENTS.md`, [below](#what-agentsmd-tells-the-drafting-side)).
- **Issues** hold an article's open work across rounds: known gaps, research,
  decisions for the author, post-publication corrections.
- An **import** rebuilds an article's earlier rounds, from local files, saved runs,
  Drive and a claude.ai data export, as merged pull requests in date order with
  their original dates, so the repository starts with the article's whole history.

## What already exists, and the decisions taken

**The drafting side's access to GitHub** is in [Setting up claude.ai](#setting-up-claudeai):
a read-only built-in integration that syncs the **default branch only** into
project knowledge, and a custom connector to GitHub's MCP server that can do
everything else. Both were verified live on 2026-10-27. The redesign depends on
the connector for every round: the built-in integration cannot see a branch.

**Clone or API.** Chosen in #400 and unchanged: **the REST API, with no local
clone.** A clone is shared mutable state on disk, which this repository's
concurrent sessions keep colliding on (`CLAUDE.md`); it would be CRLF-converted
under `core.autocrlf=true`; and the API gives the compare-and-swap this design
needs. A multi-file commit through the Git Data API (blobs → tree on the head's
tree → commit with the head as parent → update the ref with `force: false`) lands
**only as a fast-forward**, so it cannot clobber a commit the other side made in
between ([refs API](https://docs.github.com/en/rest/git/refs)).

**Library, revisited.** #400 chose a thin client on `requests` through
`ci_core.http`'s OS-trust session, and said to revisit past about a dozen
endpoints, with PyGithub as the candidate. This redesign takes the surface to
about twenty (pull requests, reviews, tags, matching refs, labels, issues, merges, dated commits). Considered again: PyGithub
(LGPL-3.0, PyNaCl and PyJWT), githubkit (MIT, pydantic, httpx, hishel) and `gh api`
in a subprocess. **Kept: the thin client.** Every new endpoint is one JSON request
with no state of its own; the parts that are hard here (compare-and-swap,
backdated commits, a merge commit built by hand, tagging without a race) are ours under any library; a library would bring a second HTTP stack that
does not use the OS trust store, which this repository had to fix once already
(certifi dropping a root Windows trusts); and the in-process fake the suite
already has (`FakeGitHub`) extends naturally to the new endpoints. Pagination
(`Link` headers) is the one piece of real new transport code, and it is about
twenty lines.

**Size.** The Contents API returns a file inline only up to 1 MB and up to 100 MB
with the raw media type ([contents API](https://docs.github.com/en/rest/repos/contents)),
so the client reads raw and writes through the blobs API. `review.md` averages
451 KB and reaches 1.2 MB; `report.json` reaches 2 MB.

## The document repository

**`MHammett/mikehammett.net-articles`**: private, on the maintainer's personal
account (the connector has been reported not to reach organization-owned
repositories, [github/github-mcp-server#549](https://github.com/github/github-mcp-server/issues/549)),
default branch `main`. It exists, bootstrapped by `ci-docs init`, and holds no
article yet.

```
AGENTS.md              the working rules for any AI client or person (managed)
CLAUDE.md              one line, `@AGENTS.md`, for Claude Code (managed)
README.md              the layout and the round loop, for people (managed)
.gitattributes         `* text=auto eol=lf` (managed)
templates/             Templates A, C and D and the revise prompt (managed copies)
instructions/
  claude-project.md    the stanza pasted into the chat project's instructions (managed)
  publication.md       the publication's drafting rules (seeded once, then the author's)
.ci-docs/manifest.json what the bootstrap wrote, from which source commit
articles/<slug>/
  draft.md             the draft                                  drafting side
  metadata.md          its metadata (Template A's two-file form)  drafting side
  publication.md       the publication handoff (Template C)       drafting side
  close-out-to-code.md the latest close-out note to a Code session  either side
  close-out-to-chat.md the latest close-out note to the drafting chat  either side
  review.md            the latest ci-review review on this branch  ci-review
  pipeline/
    run.json           the run that wrote these files: number, history name, reviewed commit, PR
    report.json        that run's report.json
    worklist.md        that run's worklist.md (the author's; never read into a revision)
    results.json       that run's ensemble capture, only with include_capture
```

### Decision 1: fixed names, and where everything else goes

**Fixed names.** The round, the run and the version are in git (the branch, the
tag, the commit, its message), not in the file name. A reader at any ref sees one
draft and one review, and `git log -- articles/<slug>/draft.md` is the draft's
history. The pull request's diff of `draft.md` is exactly what changed in the
round, which is the view the per-round names could not give. The repository has
no articles yet, so this is a **clean switch**: nothing reads the per-round names
after the redesign, and no article needs migrating.

**The two-file form only, going forward.** `draft.md` plus `metadata.md` is
Template A's two-file form (`metadata_only.md`). The one-file handoff is not a
name in the new layout: a pull request's diff of `draft.md` is the round's
changelog, and a handoff mixes the draft with its metadata. The reader still accepts a
`handoff.md` when the folder has no `draft.md` (the import writes one for a
historical round that was submitted that way); a round that moves to the two-file
form deletes `handoff.md` in its own pull request.

**Close-out notes become two files, one per direction.** A note is a long,
structured document whose `EDITS TO APPLY` must be matched byte for byte
([CLOSE-OUT.md](CLOSE-OUT.md), "Receiving a note"), so it stays a file and does
not become a comment: a comment is capped at 65,536 characters and arrives as
rendered Markdown. With git doing the versioning, the `-rN` and `-vM` in the name
go: `close-out-to-chat.md` is the latest note to the chat, its previous versions
are its git history, and `Supersedes:` names the commit of the version it
replaces. A note is committed to the open round's branch, never to `main`, and the
side that commits it also leaves a one-line comment on the pull request
(`add_issue_comment`) linking it, which is what notifies the other side. **No
label or header in `close_out.md` changes** (it has an out-of-repo consumer; see
its header comment); only the bracketed notes of `Supersedes:`, `Drive folder:`
and `Drive folder id:` say what they hold for an article kept here.

**The publication handoff** is `publication.md`, written by the chat in the round
that prepares publication. `--publish` still reads a local file
([Not decided here](#not-decided-here)).

**`pipeline/` under fixed names.** `report.json`, `worklist.md` and, with
`include_capture`, `results.json` are replaced by each run that writes back, and
git keeps every version. `run.json` is small and says which run they belong to:

```json
{"run_number": 7, "history_key": "example-article", "history_name": "run_7_20261026_143022",
 "reviewed_commit": "3f2a91c…", "round": 3, "pull_request": 12,
 "preset": "wide", "cost_reported_usd": 0.73}
```

so a reader finds the run number without opening a 2 MB report, and
`ci-docs pull-history` can name a pulled report `run_N_<ts>_report.json` again.
`report.json` keeps its full content; the top of the folder is what the chat
reads, and `pipeline/` is not.

**Ownership.** The drafting side writes `draft.md`, `metadata.md`,
`publication.md` and `close-out-to-code.md`; a Code session writes
`close-out-to-chat.md` (through `ci-docs put`); only `ci-review` writes
`review.md` and `pipeline/`. The pipeline refuses to write when a file it owns has
been changed by someone else since the commit it reviewed (the compare-and-swap
below), and `AGENTS.md` tells every other client never to touch those files.

## The round lifecycle

### Decision 2: branches, pull requests, runs and rounds

**Round N of an article** is:

1. **Open.** The chat creates the branch `round/<slug>/rN` from `main`, commits
   `draft.md` and `metadata.md` to it in **one** commit (`draft: <slug> rN`), and
   opens a pull request into `main` titled `<slug> rN`. Its body says what changed
   since the last accepted round and which issues it addresses (`Closes #n` for
   one it finishes). `N` is one more than the highest round of the article among
   its tags `<slug>/r*` and its branches `round/<slug>/r*`; round 1 is the
   article's first pull request.
2. **Review.** The author runs `ci-review --github <slug> --write-back`. It reads
   the pull request's head commit, and when the run ends it commits `review.md`
   and `pipeline/` to the branch and posts a pull request review
   ([Delivering the review](#delivering-the-review)).
3. **Revise.** The chat reads the review through the connector and commits its
   revision to the same branch (`revise: <slug> rN after run M`), saying item by
   item what it addressed and how, and what it deferred; deferred findings become
   issues. It may ask the author for another review, which is another run on the
   same pull request.
4. **Accept.** When the author says so in the chat, the chat merges the pull
   request with a merge commit (`merge_pull_request`, `merge_method: merge`). The
   round is tagged `<slug>/rN` automatically by the next command that writes to
   the article
   ([Tagging](#tagging-automatic-and-ci-docs-accept-and-tag-as-conveniences)).

**Why a branch per round.** The maintainer asked for git versioning and pull
requests; a branch per round gives each round a diff, a conversation, a review
and an explicit acceptance, and leaves `main` holding only accepted text, which
is what the built-in integration syncs. #400 left this open and argued the review
gate and merge step would cost a step per round; that step is now the point.

**Names.** The branch is `round/<slug>/rN` and the tag is `<slug>/rN`, deliberately
not the same string: a branch and a tag of one name are both legal and make every
`ref` argument ambiguous, and `GitHubRepo.resolve` would read whichever GitHub
picks. The `round/` prefix also lets a listing of branches find an article's
rounds by prefix. A slug may not contain `/`.

**Who does what.** The chat opens the branch and the pull request, revises, and
merges. The author runs `ci-review` and decides acceptance, by telling the chat to
merge. `AGENTS.md` forbids `merge_pull_request` unless the author has said, in that
chat, to merge that pull request: merging is the acceptance, so the decision is
the author's even though the chat makes the call.

**Merge commits only.** A squash merge would replace the round's commits (draft,
review, revisions) with one, losing exactly the history this redesign exists to
keep, and a rebase merge rewrites them. `ci-docs init` sets the repository to
allow merge commits only (`allow_squash_merge: false`, `allow_rebase_merge:
false`) and leaves merged branches in place (`delete_branch_on_merge: false`;
the tag and the merge commit keep the round reachable either way, but nothing in
this design deletes).

**Runs and rounds are different numbers now.** A round is a pull request; a run is
one `ci-review` invocation. A round may have several runs, and an article's run
numbers keep counting up across rounds, as `pipeline_history/<key>/run_N_*`
always has. The commit message and the review name both: `review: <slug> r3 run
7`. The run number is, in order: the handoff's `Pipeline run:` line when it has
one; otherwise one more than the highest run number in local history **and** in
`pipeline/run.json` at the pull request's head and at `main` (a run made on
another machine is known only there). The existing bump in `run_draft_pipeline`
(`taken_run_numbers` unioned with `hist.existing_run_numbers`) then applies
unchanged. The round number is no longer a run-number default.

**What `ci-review --github SLUG` reads.**

| Flags | Reads | Write-back allowed |
|---|---|---|
| none | the head commit of the article's one open pull request whose branch starts `round/<slug>/` | yes |
| `--pr N` | the head commit of pull request N, which must belong to the article | yes, if it is open |
| `--round N` | the open pull request of round N if there is one; otherwise the tag `<slug>/rN` | only for the open pull request |
| `--github-ref REF` | `REF` (a branch, tag or commit) | no: a usage error with `--write-back` |

No open pull request, with no flag, is an error that lists the article's tags and
says to open a round or pass `--round` or `--github-ref main`. Two open pull
requests for one article is an error that lists them and asks for `--pr`.
Reviewing an accepted round reads its tag and writes nothing back: the round is
closed, and its review history is fixed. The ref is resolved to one commit SHA
once, and every read is at that SHA, as #409 built.

`document_source` in `report.json` gains the pull request and both ends of it:
`{kind: "github", repo, ref, commit_sha, round, pull_request, base_sha, files:
[{path, blob_sha}]}`, where `ref` is the branch (or the tag or `REF`) and
`base_sha` is the merge base the pull request's diff is taken from. It is the
merge base of the pull request's own `base.sha` and the commit read
(`GET /compare/{base}...{head}`, `merge_base_commit`), so for a merged round it is
the base the round had, not the merge commit's. `round` is the `N` of the branch
or tag (`None` for `--github-ref`, and for a pull request whose branch is not
`rN`); `pull_request` and `base_sha` are `None` for a tag or a `REF`.

**A pull request's head pointer can lag a push.** An open pull request is read at
the tip of its branch when the two differ (GitHub's branch ref is the truth; see
the lag seen on #45), with one WARNING naming both. A closed or merged one keeps
the head it ended with.

**What a round's folder must hold** (built in #416): `draft.md`, with `metadata.md`
if present (without it the draft is read as `--raw-draft` reads one, with its
lost-context warning), or `handoff.md` alone. With both a `draft.md` and a
`handoff.md`, the draft wins and the run warns that `handoff.md` is stale.
Neither is an error naming the folder and the ref.

**When the head moves during a run.** A review takes minutes, and the chat may
commit in the meantime. The run reviews the commit it read, `S`, and nothing
else:

- The pull request review is posted with `commit_id: S`, so GitHub shows it
  against the commit it is about, not a later one.
- The pipeline's own files are committed on top of whatever the branch head is
  then, as a fast-forward (`commit_files` re-reads the head and retries, as #407
  built). `review.md` and the `pipeline/` files are written with
  `replace={path: blob at S}`, so if anyone changed one of them after `S` the
  write is refused rather than overwriting it; a file absent at `S` must still be
  absent.
- When `draft.md` or `metadata.md` at the new head differs from the blob read at
  `S`, the commit body and the review body each carry the drift line #410 built
  (`_drift_lines`): "Note: draft.md changed at its own path since it was read
  (blob a1b2c3d then, e4f5a6b now)." The review body adds that the review is of
  `S` and names it.
- If the pull request was merged or closed during the run, nothing is written
  back: the run exits 1 with the saved report's path. Committing to a merged
  round's branch would leave commits `main` never gets. `ci-docs push-run`
  refuses the same report for the same reason; the review stays in local history.

## Delivering the review

### Decision 3: `review.md` on the branch, and one pull request review holding the summary and a findings checklist

One run writes back, in this order:

1. **One commit to the round's branch**: `review.md`, `pipeline/run.json`,
   `pipeline/report.json`, `pipeline/worklist.md` (and `pipeline/results.json`
   with `include_capture`), LF line endings, secret-scanned as #410 does:

   ```
   review: example-article r3 run 7

   Reviewed: draft.md, metadata.md at 3f2a91c (PR #12)
   Run: run_7_20261026_143022 (preset wide, $0.73 reported)
   Files: review.md, pipeline/run.json, pipeline/report.json, pipeline/worklist.md
   ```

2. **One pull request review**, `POST /repos/{o}/{r}/pulls/{n}/reviews` with
   `commit_id: S`, `event: COMMENT`, a body, and **no `comments`**. `COMMENT`
   because the token that runs `ci-review` and the connector that opened the pull
   request both belong to the maintainer, and GitHub refuses `APPROVE` and
   `REQUEST_CHANGES` on one's own pull request; it would be wrong anyway, since
   acceptance is the merge.

The commit goes first, so the review body can link `review.md` at the commit that
holds it. The order makes a retry simple: `ci-docs push-run` finds the commit's
files already identical (no new commit) and posts only the review that is
missing.

**The review body** (at most 60,000 characters, under GitHub's 65,536-character
limit on a body, which is undocumented but consistently reported, e.g.
[dead-claudia/github-limits](https://github.com/dead-claudia/github-limits)):

- A first line the pipeline can find again: `<!-- ci-review run=7 commit=<S> -->`,
  with `S` the full 40-character SHA of the commit reviewed.
  `push-run` lists the pull request's reviews (`GET .../reviews`) and posts
  nothing when one starts with its marker, so a retry never duplicates a review; the
  tagging step and `ci-docs accept` read the same marker to name a round's runs
  (`review_body.parse_marker`).
- **The summary**: the run, the preset and cost, the commit reviewed and the drift
  line if any; the "Reading this report" caveat in one sentence (a single run is
  not a measurement); and one line per section with its count of findings.
- **The findings as a checklist**: each Section 1 (consensus) finding as a
  Markdown task, `- [ ] **F<k>** "<quoted passage>": <the finding, one line>`,
  where `k` is the finding's number in `review.md`'s Section 1 (which
  `report_markdown` numbers continuously across its bands), so `F3` in the body
  and item 3 in `review.md` are one finding. GitHub renders the boxes, and the
  chat's revision commits refer to findings by these numbers. The other sections'
  findings are cited by section and first words.
- **Where the whole review is**: `articles/<slug>/review.md` on the branch, as a
  link to the blob at the review commit, and the exact connector call to read it
  (`get_file_contents`, `path` that file, `ref` `refs/pull/12/head`).
- When the body would pass 60,000 characters, the per-section lines are cut
  first, then checklist items from the end (whole items); the body ends with what
  was cut and that `review.md` has it all. Nothing is cut from `review.md`.
- A checklist line clips the passage to 120 characters and the finding to 180,
  takes the finding from the first flag that says what is wrong (`problem`,
  `logical_problem`, `risk`, `attack_vector`, a fact-check's `type` and
  `current_value`), and shows a raw `<` as `&lt;` so a passage cannot open an HTML
  comment that swallows the lines after it. The numbers come from
  `report_markdown.numbered_consensus_bands`, the function `review.md`'s Section 1
  is rendered from.

**Built in #417** (`github_writeback.py`, `review_body.py`). The pre-flight is
#416's `load_handoff(write_back=True)`. At write-back the pull request is read
again; if it is merged or closed nothing is written (`PullRequestClosed`: no retry
line, since `push-run` would refuse it too) and the run exits 1. Each file the
pipeline owns that existed at `S` goes in `commit_files`'s `replace` with its blob
at `S`, and one absent at `S` is left out of it, so it must still be absent. The
body is built and scanned for the token and every configured API key *before* the
commit, so a hit sends nothing at all; the final body differs from the scanned one
only in the SHA of the link.

**The worklist stays away from the drafting chat.** README ("The worklist") and
`revise_after_review_prompt.md` keep it out of the revision model, because it lists
documents nobody has read yet, "an invitation to invent them". So it is not in the
review body. It is committed only as `pipeline/worklist.md`, which `AGENTS.md`
tells the chat never to read into a revision; it is also rendered at the top of
`review.md`, above SECTION 1, and `AGENTS.md` repeats the revise prompt's rule to
skip that WORKLIST block. The worklist is the author's, as it always was.

**Line comments were considered and dropped.** An earlier draft of this design
anchored each finding to its line in the pull request's diff. The readers here are
AI clients, which read `review.md` whole more reliably than they gather comment
threads through a connector, and the maintainer expects to review on GitHub
rarely. Comments would also have needed a quotation matcher, a diff-hunk parser, a
comment cap and a fallback for GitHub's all-or-nothing 422, for a view nobody
reads.

**`review.md` is still committed**, at the folder's top level on the round's
branch, because the full review (Sections 1 to 10, the SEO blocks) averages
451 KB and cannot be a review body. It is replaced by each run on the branch and
reaches `main` with the merge, so `main` always holds the review of the last run
before acceptance, and `git log -- articles/<slug>/review.md` holds every review.

**What the chat reads**, through the connector: `pull_request_read` with `method:
get_reviews` (the bodies: summary and findings checklist), and `get_file_contents` on
`articles/<slug>/review.md` with `ref: refs/pull/<n>/head` for everything else.
It revises with `templates/revise_after_review_prompt.md` as before, given
`review.md`'s text. Not verified: that `get_file_contents` returns a file over
1 MB whole through the connector (a check in the claude.ai phase).

## Issues

### Decision 4: issues hold an article's open work; the chat opens what a round defers; the pipeline opens none

| Label | Opened by | For |
|---|---|---|
| `article:<slug>` | whoever opens any issue or round for the article | finding everything about one article |
| `round` | the chat, on its pull request (`issue_write`, `method: update`, `labels`) | listing rounds across articles |
| `known-gap` | the chat | something the article does not cover yet and should, across rounds |
| `research` | the chat or the author | a question to answer before the article can say something |
| `decision` | either side | a choice only the author can make (a close-out note's DECISIONS, for one) |
| `correction` | the author | an error found after publication; fixed by a new round |
| `from-review` | the chat | a review finding the round could not fix, deferred so it outlives the merged pull request |

- **The pipeline opens no issue.** A run has dozens of findings, and a single run
  is about 25% reproducible; issues opened per finding would be mostly noise.
- **The chat must open one for what it defers.** A finding the round cannot fix
  (it needs a source, a decision, research, a later round) is opened by the chat as an issue (`issue_write`, `method: create`,
  labels `article:<slug>` and `from-review`, or `research` / `decision` where that
  fits), quoting the item and naming the run and the review it came from. The
  revision commit that defers it links the issue. This is what keeps deferred
  work alive after the round's pull request is merged and no longer read.
- **Issues are not a second copy of the metadata.** `metadata.md`'s KNOWN GAPS
  and UNCERTAIN SECTIONS are what the pipeline reads, and stay the source for a
  round. An issue is for work that outlives a round or needs someone outside the
  chat; when the work is done, the round's pull request closes it (`Closes #n`,
  which GitHub applies when the pull request merges into `main`).
- **Post-publication corrections**: the author opens a `correction` issue (or asks
  the chat to); the chat fixes it in a new round whose pull request closes it.
  Publishing the fix is the existing `--publish` path.
- `ci-docs init` creates the labels other than `article:<slug>` (shown first in
  `--dry-run`); the chat creates an `article:<slug>` label with the connector's
  `label_write` when it opens an article's first round, and `ci-docs` creates it
  when it needs it.

## Making the repository self-describing

Unchanged from #400: the rules live once, in the repository's `AGENTS.md`
(rendered by `ci-docs init` from `document_repo/AGENTS.md.tmpl`), with a
`CLAUDE.md` of `@AGENTS.md` for Claude Code and the stanza
`instructions/claude-project.md` for a claude.ai project. What the rules **say**
is new; see the next section.

## Setting up claude.ai

### Decision 5: the built-in sync reads `main`; the connector does every round

Checked live on 2026-10-27 against a claude.ai project and the scratch repository
`MHammett/ci-docs-scratch`.

**Reading `main`: the built-in GitHub integration.** It is read-only and syncs the
files picked as project knowledge from the default branch, refreshed by "Sync
now" ([Anthropic's page](https://claude.com/docs/connectors/github)). In the new
flow `main` holds only accepted text, so the knowledge to pick is `AGENTS.md`,
`templates/`, `instructions/publication.md`, and optionally the article's
accepted `draft.md` and `metadata.md`. It **cannot see a round in progress**: a
branch, a pull request and its review exist only through the connector. A project
chat with the stanza and `AGENTS.md` as knowledge answered "how do I submit a
draft?" from `AGENTS.md` (verified).

**Everything else: the custom connector** to GitHub's MCP server,
`https://api.githubcopilot.com/mcp/`, with the maintainer's own OAuth App:

1. On github.com, register an OAuth App (Settings, Developer settings, OAuth
   Apps, New OAuth App). Give it any name and any homepage URL. Set
   **Authorization callback URL** to `https://claude.ai/api/mcp/auth_callback`.
   Create it, then generate a client secret. Keep the **client ID** and the
   **client secret**.
2. In claude.ai, add the custom connector with the URL above. For its OAuth
   setting choose **Use your own OAuth client** and enter that client ID and
   secret.
3. Connect, and approve the sign-in at GitHub.

Why the other options do not work: **"Register automatically"** needs dynamic
client registration and **"Use Claude's published identity"** needs client-ID
metadata documents, and GitHub's OAuth server metadata
(`https://github.com/.well-known/oauth-authorization-server/login/oauth`)
advertises neither. **A fine-grained token in an `Authorization: Bearer` header,
with "No sign-in"**, fails with "Couldn't connect" although the same request works
from curl, a claude.ai bug
([anthropics/claude-ai-mcp#1109](https://github.com/anthropics/claude-ai-mcp/issues/1109));
it would have been better, because a fine-grained token can be limited to one
repository.

**The cost of the OAuth App route.** The connector gets the broad `repo` scope,
which an OAuth App token cannot narrow, so it reaches **every repository the
account can**, private ones included, and its tool list includes `delete_file`,
`delete_repository` and `merge_pull_request`. `AGENTS.md` forbids the first two and
allows a merge only on the author's word in the chat, but that is an instruction
to a model, not a permission GitHub enforces. A repository
ruleset could enforce part of it (no deleting or moving `*/r*` tags, no direct
pushes to `main` except by the bootstrap); rulesets on a private repository need a
paid plan, and whether to add one is the maintainer's decision
([Not decided here](#not-decided-here)). The grant can be revoked at any time on
github.com (Settings, Applications).

**Verified through the connector** (2026-10-27): a project chat read `AGENTS.md`
from the articles repository; committed one file to the scratch repository with
`create_or_update_file`; and committed two files in **one** commit with
`push_files`. The checks the redesign adds are in the claude.ai phase issue:
`create_branch`, `create_pull_request`, reading a review body and its findings checklist, a
merge on the author's word, a file over 1 MB through `get_file_contents`.

**The connector's tool names** used below are those of GitHub's MCP server as its
README listed them on 2026-10-27 (default toolsets `context`, `repos`, `issues`,
`pull_requests`, `users`), and match the tool list the maintainer's connector
showed. If a call fails because a name changed, the chat lists its tools and
uses the one with the same purpose; `AGENTS.md` says so.

### What `AGENTS.md` tells the drafting side

The rules below replace `AGENTS.md.tmpl`'s current text in the init phase. They
are written here so the design can be reviewed; the template is the only copy the
drafting side reads.

**Starting round N** (`owner` and `repo` are the repository's):

1. Find `N`: `list_tags` and `list_branches`; `N` is one more than the highest
   `rK` among tags `<slug>/rK` and branches `round/<slug>/rK`, or 1 for a new
   article. If a branch `round/<slug>/r…` has an open pull request
   (`list_pull_requests`, `state: open`), that round is still open: work on it
   instead of starting another.
2. `create_branch` with `branch: round/<slug>/rN`, `from_branch: main`.
3. Read the accepted text you are revising: `get_file_contents` with `path:
   articles/<slug>/draft.md` (and `metadata.md`), `ref: refs/heads/main`.
4. `push_files` with `branch: round/<slug>/rN`, `files: [{path:
   articles/<slug>/draft.md, content}, {path: articles/<slug>/metadata.md,
   content}]`, `message: draft: <slug> rN`. Both files, whole, in one call: never
   `create_or_update_file` for the pair, which makes a commit holding a draft
   without its metadata.
5. `create_pull_request` with `head: round/<slug>/rN`, `base: main`, `title:
   <slug> rN`, and a description that **states what changed since the last
   accepted round (`<slug>/r(N-1)`, or "first draft") and why**, section by
   section, and lists `Closes #n` for each issue this round finishes. Then
   `issue_write` (`method: update`, `issue_number` the pull request's number,
   `labels: [round, article:<slug>]`).
6. Ask the author to run `ci-review --github <slug> --write-back`. You cannot run
   the review.

**After a review**: `pull_request_read` with `method: get_reviews` (the summary
and the findings checklist, F1, F2, …), then `get_file_contents` for
`articles/<slug>/review.md` with `ref: refs/pull/<n>/head`. Skip the WORKLIST
block at the top of `review.md`, above SECTION 1, as the revise prompt says: it is
the author's. Revise with
`templates/revise_after_review_prompt.md` and commit the revision with
`push_files` to the same branch. **Every revision commit message** has this
shape:

```
revise: <slug> rN after run M

Responds to: run M review (<link to the review>)
Addressed:
- F2: <what the finding asked> -> <what changed, and where in the draft>
- Section 3, "<first words of the finding>": <what changed>
Deferred:
- F1: <why it cannot be done in this round> -> #<issue>
- F4: needs the author (<what exactly>)
Not acted on:
- F5: <why not, in one line>
```

Every F-numbered finding appears under Addressed, Deferred or Not acted on.
Findings of other sections are listed when the revision acted on them or decided
not to. **A deferred finding that the author will not settle in this chat is
opened as an issue first** (`issue_write`, `method: create`, labels
`article:<slug>` and `from-review`, or `research` / `decision`), quoting the
finding and naming the run, and the commit links it. Ask the author for another
review, or say the round is ready to accept.

**Accepting.** When, and only when, the author says in this chat to merge the
round, call `merge_pull_request` with `merge_method: merge` and `expectedHeadSha`
set to the head you last read, so nothing pushed after it is merged unseen. Do not
tag: the pipeline tags merged rounds itself.

**A close-out note** is `articles/<slug>/close-out-to-code.md` (or `-to-chat`),
written from `templates/close_out.md`, committed to the open round's branch with
`push_files` (`close-out: <slug> rN to-code`), then announced on the pull request
with `add_issue_comment`. Before writing one, read the current note at that path
on the branch and on `main`: a note that is there is replaced by a new commit,
and the new note's `Supersedes:` names the commit of the one it replaces
(`list_commits` with `path` gives it).

**Issues**: as in [Issues](#issues).

**Never**: commit to `main`; merge a pull request unless the author has said, in
this chat, to merge that pull request; merge any way but `merge`; force-push,
delete a branch or a tag, or call `delete_file` or `delete_repository`; edit
`review.md` or anything under `pipeline/`; read anything under `pipeline/` (the
worklist, the report) into a revision; rename an article folder or change a
slug; commit a secret; edit a managed file. A slug is lowercase words joined by hyphens and never contains `/`.

**Fallback.** If the connector is not worth its scope or stops working, the author
does a round by hand on github.com: create the branch, upload `draft.md` and
`metadata.md` in one commit, open the pull request, merge it. Nothing on the
pipeline side changes.

## Bootstrapping the repository: `ci-docs init`

Built (#406, PR #408) and kept: it creates the repository when missing (`gh repo
create --private`, refusing an existing public one), seeds an empty one with a
Contents API `PUT`, and commits the managed files in one commit with
`.ci-docs/manifest.json`, replacing a managed file only when its blob is the one
the manifest recorded (`commit_files(replace=...)`), listing edited ones, and
never touching `articles/` or `instructions/publication.md` after creating them.
`--dry-run`, `--create`, `--seed-instructions`, `--overwrite-edited`, `--repo`,
`--branch` and `--config-dir` are unchanged.

**What the redesign changes in it:**

- `AGENTS.md.tmpl`, `README.md.tmpl`, `claude-project.md.tmpl` and
  `articles_README.md.tmpl` are rewritten for the new layout and the round loop
  above. `articles/README.md` is now a managed file (in the manifest, like the
  others), because nothing else writes it and the old rule, created only while
  `articles/` was empty, would have kept the new text out of the existing
  repository. The first bootstrap wrote it before the manifest recorded it, so its
  blob is not "what the last run wrote"; `ci-docs init` knows that earlier render
  (`EARLIER_RENDERS`) and replaces it without `--overwrite-edited`, which would
  also overwrite any other file someone edited. A page the author changed is still
  left alone and listed.
- `close_out.md`'s bracketed notes (not its labels) and
  `revise_after_review_prompt.md` follow the new names; both are rendered from
  `handoff_templates/` as before.
- **Repository settings.** `init` reads `GET /repos/{o}/{r}` and, when they differ,
  sets `allow_merge_commit: true`, `allow_squash_merge: false`,
  `allow_rebase_merge: false`, `delete_branch_on_merge: false` with one
  `PATCH /repos/{o}/{r}`, shown first in `--dry-run`. The real run makes the
  commit first and the `PATCH` after it, so a commit the secret scan refuses
  stops the run before the settings change; if GitHub accepts the `PATCH` but the
  repository still reports another value (an organisation policy), the run says
  so and exits 1.
- **Labels.** It creates `round`, `known-gap`, `research`, `decision`,
  `correction` and `from-review` when missing (`POST /repos/{o}/{r}/labels`), and
  never edits or deletes a label that exists.

The string test #408 added (`AGENTS.md` names every pattern and commit message)
is rewritten for the new names, commit messages and tool names.

## Tagging: automatic, and `ci-docs accept` and `tag` as conveniences

**Automatic tagging.** Before it does its own work, every command that already
writes to an article runs one step, `tag_merged_rounds(slug)`: `ci-review --github
--write-back` (and `ci-docs push-run`, its retry), `ci-docs put` and `ci-docs
accept`; the import tags each round it writes, and `ci-docs tag` runs the step
alone. `ci-docs ls` and a `ci-review --github` run without `--write-back` stay
read-only. The step:
it finds every merged pull request of the article whose branch is
`round/<slug>/rN` and for which no tag `<slug>/rN` exists, and creates an
**annotated** tag on the pull request's merge commit (`POST /git/tags` with `type:
commit` and `tagger` dated at the merge, then `POST /git/refs` for
`refs/tags/<slug>/rN`). Annotated, because the tag then records when the round was
accepted and says what it was (`example-article r3: PR #12, runs 6 and 7`, the runs
read from the review markers), and the import can give it the original date; a
lightweight tag is a bare pointer.

- **Idempotent and safe against a race.** Creating the ref is the
  compare-and-swap: GitHub refuses a ref that exists, so two commands tagging at
  once cannot both win, and the loser reads the tag back. A tag that exists and
  points at that merge commit is success; one that points elsewhere is reported
  and left alone. No tag is ever moved or deleted.
- **Only where the command writes anyway**, so no read-only command gains a
  write, and replays, `--offline` and calibration runs (which cannot write back)
  never tag. A failure to tag is a WARNING and never stops the command's own work.
- **Why here and not in a GitHub Action**: the repository stays passive (no
  workflow file, no Actions minutes), and the next write to the article (a
  review written back, a note put, the next round accepted) runs it. A round
  merged and never written to again stays untagged until then (`ci-docs tag`
  tags it on demand); the merge commit is the record either way.

**As built (#418).** `tag_merged_rounds(client, slug)` is in
`github_rounds.py`; the hook #416 left in `github_documents.load_handoff` calls it
only when `write_back=True`. Where the build differs from the text above or from
the issue:

- `tag_merged_rounds` takes no `prefix`: the tag's name, the branch it reads and
  the review markers it parses need none. It does take `repo=` (skip a pull request
  from a fork) and `dry_run=`.
- `ci-docs accept --yes` tags twice: before the merge (a round merged earlier
  and never tagged) and after it. Without `--yes` it writes nothing and tags
  nothing. `push-run` tags from the CLI, before `push_saved_run`, by reading the
  article from the report's `document_source`, and tags nothing for a report that
  command will refuse (a replay, one not read from GitHub).
- A token that cannot read `GET /user` (a GitHub App installation token) cannot
  name a tagger; that is a warning for the round, and the command goes on.
- `ci-docs ls` and `put` lose `--branch` (`ls` gains `--ref`, `put` gains
  `--pr`).

**Conveniences, not steps.** The round loop needs neither:

```bash
uv run ci-docs tag example-article               # tag now, without doing anything else
uv run ci-docs accept example-article --yes      # merge the open round's PR from Code, then tag
```

- **`tag`** runs `tag_merged_rounds` and prints what it did.
- **`accept`** merges the article's one open round pull request with `PUT
  /repos/{o}/{r}/pulls/{n}/merge`, `merge_method: merge`, and `sha` set to the head
  it showed, so a commit pushed after that is not merged unseen (GitHub answers
  409 and nothing merges). Without `--yes` it only shows the pull request, its
  head, its last review's run and whether a commit has landed since that review.
  A merge is the author's acceptance: a Code session runs `--yes` only when the
  author has said to.

## Configuration

The `github_documents` block (#409) is kept unchanged:

```yaml
github_documents:
  repo: MHammett/mikehammett.net-articles   # the default; owner/name
  branch: main            # the base branch rounds merge into; default: the repository's default
  path_prefix: articles   # the default; folder holding the article folders
  write_back: false       # default for runs that read from GitHub; --write-back overrides
  include_capture: false  # also commit pipeline/results.json
  allow_public_repo: false  # refuse to write to a public repository unless true
```

The block is normalised by `config_loader.normalize_github_documents` and carried through
`merge_configs` explicitly. **Token**: `GITHUB_TOKEN`, else `gh auth token`, never
in YAML. The token now also needs **Pull requests: read and write** and **Issues:
read and write** if it is a fine-grained token, besides Contents; `gh`'s token has
them.

## Command line

| Flag | Meaning |
|---|---|
| `--github SLUG` | Read article `SLUG` from the configured repository (a member of the required source group) |
| `--pr N` | With `--github`: review pull request N. **New** |
| `--round N` | With `--github`: review round N, its open pull request or else its tag. **Changed**: no longer a file name's N |
| `--github-ref REF` | With `--github`: read at a branch, tag or commit; never writes back |
| `--github-repo OWNER/NAME` | Override `github_documents.repo` for this run |
| `--write-back` / `--no-write-back` | Commit the review to the round's branch and post the pull request review, or don't |

`--pr`, `--round` and `--github-ref` are mutually exclusive, and each is a usage
error without `--github`. `--github-ref` with `--write-back` is a usage error.
The rules #410 built for replays and calibration runs are unchanged: `--write-back`
with `--replay`, `--offline`, `--only-model`, `--only-domain` or `--no-timeout` is
a usage error, and config's `write_back: true` is set aside on those runs with one
INFO line.

## The history key

Unchanged: when the handoff has no `History key:`, the GitHub source sets it to
`SLUG`, so `pipeline_history/<slug>/` and the article folder are one string. A
`History key:` that differs warns and wins. The import ([below](#importing-an-articles-history))
asks which history keys belong to the article, because earlier runs were filed
under title-derived keys.

## Writing back

Write-back is the commit and the review of [Delivering the review](#delivering-the-review),
after `hist.save_run` and after the capture is saved, so the review URL is the
last line printed. **Pre-flight, before any model call**: a token; push
permission; a private repository unless `allow_public_repo`; for write-back, an
open pull request whose head is a branch of this repository (not a fork); and the
run numbers in `pipeline/run.json` at its head and at `main`.

**A write-back that fails after the review** keeps the local files, prints the
reason and `ci-docs push-run <report.json>`, and exits 1, as #410 built.
`push-run` is idempotent in both halves: the commit's files that are already
identical make no commit, and a review whose marker is already on the pull request
is not posted again. It refuses what #410's refuses (a `_replay/` report, one
without a GitHub `document_source`, a public repository without
`allow_public_repo`, no push permission), and also a report whose
`document_source` names no `pull_request` (it was read from a tag or a ref) and one
whose pull request is merged or closed. It prints the commit URL and then the
review URL.

## Replay, offline and experiments

Unchanged from #410: a replay's report is a code test and is never written back
(`--replay` with `--write-back` is a usage error, and `push-run` refuses
`_replay/`); `--offline` and the calibration flags are treated the same way;
`--retry-failed` files into real history and may write back; `--publish` and
`--archive-only` do not read from GitHub.

## Importing an article's history

### Decision 7: one command in two steps, a plan the author edits, then merged pull requests with original dates

The maintainer wants the repository to be "a complete history of an article",
including rounds and edits made before it existed. That history is scattered over
four private sources, with inconsistent names (`-vNN-handoff.md`, `-draft-vNN.md`,
`-draft-rN.md` and others across articles), duplicates, experiment runs and
title-derived history keys. No rule can sort that unattended, so the import is
**scan, then edit, then apply**:

```bash
uv run ci-docs import scan example-article \
    --history-key example-article --history-key an-older-title-derived-key \
    --files ~/private/handoff_templates --files ~/Downloads/example-article-v27 \
    --drive-dir ~/ci-import/example-article/drive \
    --chat-export ~/ci-import/claude-export --chat-map ~/ci-import/chat-map.tsv \
    --recovered ~/ci-import/example-article/recovered \
    --out ~/ci-import/example-article/plan.json
# read the timeline it prints; edit plan.json (exclude, regroup, relabel), then:
uv run ci-docs import check ~/ci-import/example-article/plan.json
uv run ci-docs import apply ~/ci-import/example-article/plan.json --repo MHammett/ci-docs-scratch --dry-run
uv run ci-docs import apply ~/ci-import/example-article/plan.json --repo MHammett/ci-docs-scratch
```

**The sources, and what each gives.**

| Source | Read how | Gives | Timestamp, and how far it is trusted |
|---|---|---|---|
| saved runs: `pipeline_history/<key>/run_N_<ts>_report.json` (and `_review.md`, `_worklist.md`, `_results.json`) in the main checkout or `$CI_HISTORY_ROOT`, for each `--history-key` | locally | each run's review files, and `corrected_draft`, the draft it reviewed after the LanguageTool pass | `generated` (UTC, written by the run): **trusted most**, it is the moment the pipeline had that text |
| claude.ai export (`conversations.json`) | locally, from the export the maintainer downloads | in-chat versions of the draft and the edits between submissions | a tool call's `start_timestamp` where it has one, else its message's `created_at` (server time, UTC): **trusted as much**, the text existed then |
| Drive article folders (since about 2026-10-25), already named `<slug>-draft-rN.md`, `-metadata-rN.md` | downloaded by a **Code session** before the scan, below | the files as submitted | Drive `createdTime`: trusted next; it is when the file was uploaded, which may be later than when it was written |
| local files: the gitignored `handoff_templates/` of the main checkout, `~/Downloads/<slug>-vNN/` | `--files DIR`, repeatable | drafts, metadata, handoffs, close-out notes, publication handoffs | file modification time: **trusted least**; a copy or an unzip resets it |
| versions a chat reproduced from its context on request | `--recovered DIR` | a version no other source kept | the label's or the chat's stated time; the **content** is trusted least of all (below) |

**Versions a chat reproduced are the least trusted source.** Some versions exist
only because a drafting chat was asked to reproduce them from its context after
the fact. A reproduction is not the original bytes and may silently differ, so
`--recovered DIR` marks every file in it `recovered: true` in `plan.json`, its
content is trusted least (below a file's, whatever its timestamp), and its time is
the one the version's label or the chat's note gives, never the file's. When a run
(`corrected_draft`) or a Drive copy holds the same version, matched by its round
or version label, that copy is used and the reproduction is listed as
`superseded_by` it; a reproduction is committed only when nothing else holds that
version, and its commit says `Recovered: reproduced by a chat from its context;
not the original bytes`. The claude.ai export, when it arrives, can confirm a
reproduction (an exact hash match, which drops the flag) or replace it with the
version it actually holds.

**De-duplication is by content.** Every candidate text is reduced to its LF bytes
with trailing whitespace stripped from each line, and hashed; one hash is one
version, whatever sources it came from, and its time is the **earliest trusted
timestamp** among them (the text existed by then). Texts that differ only by the
LanguageTool corrections a run recorded (`lt_corrections_applied`) are the same
submission: the import keeps the submitted bytes when it has them and uses
`corrected_draft` only when no source holds the original, saying so in the
commit (`Source: report.json corrected_draft, LanguageTool-corrected; the
submitted bytes were not found`). Near-duplicates beyond that are not merged; the
scan lists pairs that `passage_match.same_passage` would call the same, and the
author merges them in the plan or leaves them apart.

**Classifying a file** is by name where a name says what it is (draft, metadata,
handoff, publication, close-out, review, changes notes), by content otherwise
(`handoff_parser` recognises Template A and C), and `unknown` (excluded by
default) when neither works. Files that are not documents of the article
(scripts, prompts, posting briefs, image folders) are listed and excluded.

**Experiment runs and history keys.** The scan reads only the history keys it is
given; `_experiments/`, `_replay/` and `_nested/` are never read. Every run is
listed with its preset, cost and draft hash, and a run whose draft hash matches no
submitted version, or that is one of several runs on one version within minutes,
is marked `suspect: experiment?`. Nothing is dropped silently: the author sets
`include: false` in the plan.

**The plan** (`plan.json`, written outside any git checkout; the scan refuses an
`--out` inside this repository) is the timeline, oldest first:

```json
{"slug": "example-article", "repo": null, "rounds": [
  {"round": 1, "events": [
    {"at": "2026-06-14T18:02:11Z", "kind": "draft", "sha256": "…", "sources": ["file:…", "chat:…"], "include": true},
    {"at": "2026-06-14T18:02:11Z", "kind": "metadata", "sha256": "…", "include": true},
    {"at": "2026-06-14T19:40:52Z", "kind": "run", "run": 1, "history_key": "…", "include": true},
    {"at": "2026-06-15T09:12:30Z", "kind": "chat-edit", "sha256": "…", "include": true}],
   "accepted_at": "2026-06-16T10:00:00Z"}],
 "excluded": [ … ], "applied": {}}
```

The scan groups events into rounds: a new round starts at each submitted draft
version (a file the drafting side named as a round or version, or the draft a run
reviewed), and in-chat versions between two submissions are revisions inside the
earlier round. `accepted_at` defaults to just before the next round's first event.
`--repo` is required by `apply`, so the real repository is never written by
default.
The author can move an event, split or join rounds, exclude anything, and set
`accepted_at`; `apply` validates the plan (times in order, every included event's
content present on disk) before it writes.

**As built (`ci-docs import scan` and `check`, issue #420; the issue's spec
governs where it differs from the sketch above).** The plan is `version: 1`, with
sorted keys, UTF-8 and LF endings, and byte-identical for the same inputs. Events
carry an `id` (`e1`, `e2`, ... in plan order); a draft carries `label` (`v27`,
`r3`), `submitted` and `sources` (type, path, time, and `lt_undone` or `part`
where the text is a run's `corrected_draft` or a handoff's embedded draft); a run
carries `report`, `preset`, `cost_usd`, `draft` (the id of the version it reviewed)
and `suspect`. `near_duplicates` lists pairs of draft ids that
`passage_match.same_passage` calls the same; nothing is merged. Details the
sketch does not say:

- A file is read only when its name mentions the slug or a history key as a whole
  token (`example-article-v2-draft.md` does; `example-articles.md` does not), or
  `--include` names it, or it sits in a subfolder whose own name mentions the slug
  (`~/Downloads/example-article-v27/`, whose label `v27` it inherits).
- A handoff that is a Template A document also holds a draft: that embedded draft
  is a source of the same version as a draft file or a run with the same text.
- A run is linked to a version by the text it reviewed: `corrected_draft` with its
  `lt_corrections_applied` undone (by offset, and only when every replacement is
  found where the log says), else `corrected_draft` as it is, else a new version
  noted `LanguageTool-corrected; the submitted bytes were not found`.
- A reproduction is matched to the version it claims by label, and a run's
  `Pipeline run:` number stands in as `rN` for a version no file labels. Any
  non-reproduction source (a run, Drive, or a file) supersedes it, provided the
  texts are alike (Jaccard over content tokens at least 0.6); a label shared by
  several versions picks the most alike. A kept reproduction with no `claimed_at`
  is placed one second after the nearest lower label of the same prefix, or one
  second before the nearest higher; with neither it has no time (`at: null`) and
  `check` will not pass the plan until the author sets one.
- A file with no recognisable kind (`unknown`) is an event with `include: false`;
  scripts, `.txt` files and folders of images are not events and are listed in
  `excluded`.

**The claude.ai export's schema** (inspected 2026-10-27, issue #422; field names
and types only, no values). The download is a manifest (`manifest.json`) and one
ZIP per category; the ones that matter are `conversations-000.zip` and
`projects-000.zip`, which can be given zipped or extracted.

```text
conversations.json                    list of conversation
  conversation = {uuid: str, name: str, summary: str, created_at: str, updated_at: str,
                  account: object, chat_messages: list of message}
  message      = {uuid: str, parent_message_uuid: str, sender: "human" | "assistant",
                  created_at: str, updated_at: str, text: str, content: list of item,
                  attachments: list of attachment, files: list of {file_uuid, file_name}}
  item         = {type: "text" | "thinking" | "tool_use" | "tool_result" | "document" | "image" | ...,
                  start_timestamp: str | null, stop_timestamp: str | null, ...}
    tool_use   : + {name: str, input: object, id: str | null, integration_name: str | null, ...}
    tool_result: + {name: str, tool_use_id: str | null, content: list, is_error: bool}
  attachment   = {file_name: str, file_size: int, file_type: str, extracted_content: str}
projects/<uuid>.json                  one per project
  project      = {uuid: str, name: str, description: str, prompt_template: str,
                  docs: list of {filename: str, content: str, created_at: str}}
```

* **A conversation carries no project id**, so a conversation is matched to an
  article by what it says, never by project.
* **Timestamps** are UTC ISO-8601 with a `Z` and microseconds, on every message.
  `start_timestamp` / `stop_timestamp` on an item are present for some tool calls
  and `null` for most; a tool call without its own time takes its message's.
* **Artifacts and files appear as `tool_use` items**, whose `input` holds the
  text: `create_file` (`path`, `file_text`) and `str_replace` (`path`, `old_str`,
  `new_str`) in the chat's sandbox; `artifacts` (`id`, `command`: `create`,
  `rewrite` or `update`, `content`, or `old_str` / `new_str`, `version_uuid`);
  Claude Code's `Write` (`file_path`, `content`) and `Edit` (`file_path`,
  `old_string`, `new_string`); the Google Drive connector's `create_file`
  (`title`, `textContent`) and `update_file` (`fileId`, `textContent`); and the
  Dropbox connector's `create_file` (`path`, `content`). Each connector tool
  occurs under more than one name (`mcp__Google_Drive__create_file`,
  `Google Drive:create_file`). A file the chat's sandbox got another way (a
  shell command, an upload) has no `create_file` call, so an edit of it has no
  base to replay on. The manifest lists no artifacts category, and an artifact's
  versions are in the export only as these calls.
* **Attachments** carry their text as `extracted_content`. A project's knowledge
  files carry theirs as `content`.
* A conversation list can contain the same conversation twice when a download is
  kept both zipped and extracted; the scan reads the extracted copy only.

**Matching a chat to an article and a round (as built, `chat_export.py`).** The
export is read by `ci-docs import scan --chat-export PATH` (a directory, the ZIP,
or `conversations.json`). The matcher:

1. **links a conversation** to the article when its name or summary, a message, an
   attachment's name or text, or the file name a tool call wrote carries the slug
   or a history key as a whole token, or when the author lists it
   (`--conversation UUID`, repeatable). A conversation that only quotes the
   article's title (the `Article:` line of the newest metadata or handoff in the
   scan) is a **weak** link: what it yields is kept in the plan with
   `include: false` and a `why`. There is no `--project` filter, because there is
   no project id to filter by. A project's knowledge files whose own file name
   carries the slug are read as candidates too;
2. **rebuilds versions** by replaying each linked conversation's tool calls in
   time order: a whole file is a version, and each `str_replace`, `Edit` or
   artifact `update` is applied to the text it edited and yields the version it
   produced. A call the conversation's own result marked as failed is skipped. An
   edit is counted and left out when the file was never written by a tool call in
   the conversation (it came from a shell command, an upload or another
   conversation) or when its old text is not in the file exactly once: a base is
   never guessed. Then attachments, then a fenced block or a human message of at
   least 2,000 characters;
3. **keeps a candidate** only if it is a whole version of the draft: at least 60%
   of its content tokens (`passage_match.tokenise`) are in some known version of
   this article's draft (from the other sources, a reproduction, or a chat text
   kept earlier in time order), **and** it carries at least half of that
   version's tokens. The second condition goes beyond issue #422's wording: the
   first alone keeps every excerpt, because an excerpt's tokens are all in the
   draft. On a long real article, the tool-call texts that passed only the first
   condition carried about a fifth of a draft (sections and companion files),
   and the whole versions carried all of one, so the half is far from either.
   Everything else is counted as dropped, by where it came from;
4. **merges** a kept text with the version of the same content when another
   source holds it (the chat is added as a source, and its time becomes the
   version's if earlier), else adds it as a draft **revision inside the round
   that is open at its time**. A chat text is a *submission*, starting a round,
   only when the author named the file: an attachment or project file whose name
   carries a `vNN` / `rN` label, never a name a tool call chose. The near-duplicate
   list leaves out pairs where both are chat-only (an edit chain);
5. **confirms or replaces reproductions** (`--recovered`): a reproduction whose
   bytes the export holds is that version (the flag is dropped, the chat's time is
   used); one whose label an export version carries, with a text alike enough and
   a time that fits (within three days of the reproduction's claimed time, and
   after the nearest lower and before the nearest higher labelled version that
   another source holds), is `superseded_by` it. The summary counts both; a
   reproduction the export cannot speak to stays flagged.

A chat source in the plan reads `{"type": "chat", "path":
"chat:<conversation uuid>/<message uuid>", "via": "create_file" | "str_replace" |
"artifacts" | "write" | "edit" | "drive_create_file" | "drive_update_file" |
"dropbox_create_file" | "attachment" | "fenced_block" | "message_text" |
"project_doc", "locator": "item:N" | "attachment:N" | "fence:N" | "text" | "doc",
"time": ...}`. The plan's `chat` block names the export, the linked conversations
(id, `created_at`, message count, why linked; never a name) and the counts. `check`
rebuilds a chat-only version from the export and compares its hash, reading the
export only when no file or run holds the version. The summary prints the linked
conversations' ids, dates and message counts to the console.

**A map from conversations to articles, and versions only a chat holds
(`--chat-map`, issue #430).** Matching by what a conversation says is noisy on
a real export (one conversation named seven articles, and conversations from
outside the drafting project named research folders), and it finds nothing for
an article that has only ever lived in chats: no run, file or Drive copy exists
for a chat text to resemble. claude.ai does know which project a conversation
belongs to (`GET /api/organizations/{org}/chat_conversations` returns
`project_uuid` for each, and `GET /api/organizations/{org}/projects/{project}/conversations`
lists a project's conversations), but the export does not carry it, so the
author, or a Code session with the signed-in browser, writes the map:

```text
conversation<TAB>slug<TAB>name<TAB>confidence
<conversation uuid><TAB>example-article<TAB>anything<TAB>sure
```

* The header names the columns. `conversation` and `slug` are read; any others
  (a name, a confidence) are ignored and never reach the plan or the console.
  A row with no slug is a conversation not yet placed; one conversation on two
  rows with different slugs is an error. Slugs are compared in lowercase.
* With `--chat-map FILE` the conversations linked to `SLUG` are exactly the rows
  whose slug equals it, plus any `--conversation`; **no conversation is linked
  by what it says, and the title is not used**. A conversation mapped to another
  slug is never linked to this one (unless `--conversation` names it, which is
  the author overriding the map). The plan's `chat.linked[].linked_by` says
  `chat-map` (or `listed`). A row for this slug whose conversation is not in
  the export is a warning, not an error. A project's knowledge file whose own
  file name carries the slug is still read, because it is linked by its file
  name and the map only covers conversations. Without `--chat-map` nothing
  about linking changes.
* **A chat-only version.** From a conversation linked by the map or by
  `--conversation`, a text is kept even when no other source holds a version it
  resembles, if it is a whole document:
  the full content of a file-writing tool call (`create_file`, an artifact
  `create` or `rewrite`, Google Drive's `create_file` or `update_file`,
  Dropbox's `create_file`, `Write`), an attachment's `extracted_content`, or a
  project knowledge file; with at least `--min-chat-words N` words (default
  300; a word is a whitespace-separated piece of text with a letter in it, so a
  table's rules and bare numbers do not count); not a file whose name ends in a
  code, data or PDF suffix (`.py`, `.json`, `.csv`, `.pdf`, ...); and not a
  document the name or content says is something else (a metadata, handoff
  without a draft in it, close-out, review or changes note). A tool-written file
  that is a near-duplicate (`passage_match.same_passage`) of a version kept
  earlier from a chat is not a new version and is counted, so an assistant that
  writes its file out again each turn does not bury the others. An
  `str_replace`, `Edit` or artifact `update` rebuild is kept when the text it
  edited was kept. Fenced blocks and long messages are **not** whole documents:
  a chat discusses many texts, so they stay under the resemblance rule above
  (and now also resemble the chat-only versions kept earlier).
* Each such version has `"corroborated": false` in the plan and `chat-only` in
  the timeline: no run, file or Drive copy holds a version like it, so only the
  export vouches for it. A version that does resemble another source's keeps
  today's behaviour and has no such key. The summary prints the count per tool
  kind, how many resembled another source, and how many candidates were not
  kept and why (`not_a_whole_file`, `not_prose`, `not_a_draft`, `short`,
  `near_duplicate`, `edit_base_not_kept`).
* **Side documents and fragments (PR #435).** A linked conversation also
  holds documents that are not the article: a verification log, a research
  batch, a source excerpt, a section cut from the draft. A chat-only version is
  kept as a draft only if it **relates to the article's other versions** and is
  **not a fragment of one**, judged by 8-word shingles (the share of a text's
  8-word windows found elsewhere):
  * *Related.* At least **10%** of its windows are in the versions kept so far
    (those another source holds, then the chat's own, followed from one to the
    next). On three real articles every side document scored 0.00-0.03 and the
    lowest genuine draft 0.14.
  * *Or named like a kept version.* An article rewritten heavily shares little
    with its own first draft, so a text whose **file name stem** equals the stem
    of a kept version's file name (from the chat or from another source) is kept
    anyway, with a `note` saying so; so are the edits of a text that was kept.
    The stem is the name lowercased, without its extension, with trailing
    version markers dropped one after another: `-vN`, `-rN`, `-final`,
    `-draft`, `-superseded` and whatever follows it, and a date (`-YYYY-MM-DD`
    or `-YYYYMMDD`), after `-`, `_` or a space. The whole stem must be equal:
    `x-draft.md` and `x-v2.md` are `x`, `x-citation-check-r1.md` is
    `x-citation-check`, and nothing joins on a prefix.
  * *Not a fragment.* A text that is at least half inside a version **written
    before it (or at the same time)** while covering less than **half** of every
    version it overlaps is a fragment: a section cut from a draft covered about
    12% of it, the body-only versions 76% or more. A smaller text that a *later*
    version expanded is an earlier draft and stays.

  A text that fails is not dropped: it stays in the plan with `"include": false`
  and a `why` naming the rule and the share, and the summary lists each by
  event id, conversation and message. To put one back, set its `include` to
  `true` in `plan.json` (`check` then validates it like any event); `--include`
  does not apply, as it only names `--files`. Left-out documents do not move a
  round boundary. With no version from another source, the biggest group of
  mutually related texts is taken as the article; when no two texts share
  anything, nothing says which is the article, so all stay and a warning says
  so. A genuine early draft that was written after a larger text it sits inside
  reads as a fragment: set its `include` back to `true`.
* **`research/` holds research, and sometimes superseded drafts.** A file under
  a `research/` directory (a `--files` or `--drive-dir` entry, or a project
  knowledge file whose name has `research/` in its path, as a project mirroring
  a Drive folder does) is read exactly as it would be in the folder root when
  its **name** follows the handoff naming for a draft or metadata: the scan's own
  kind detection, so `<slug>-draft-r1-superseded-<date>.md`,
  `<slug>-draft-v4-prerun.md` and `<slug>-metadata-r1-superseded.md` are read
  (as in the root, a name must still mention the article). Any other file there
  is never a version of the article, whatever text it shares with it: notes,
  checks and data the article was written from often quote it. Each such file
  is listed under `excluded`, by path, with the reason. A `--files` directory
  named `research` is treated the same way.
* **A project document does not date a version.** A project's knowledge file
  carries only a `created_at`, which says the document existed by then in some
  earlier state; the project syncs from Drive, so its text can be a later file's
  byte for byte. A version is dated by any other source that holds it (a run,
  Drive, a file, a reproduction's claimed time); the project document's
  `created_at` is used only when nothing else has a time, and the event then
  carries a `hint` saying so.
* **Metadata goes with the draft it labels.** An author often saves
  `<slug>-metadata-rN.md` seconds or minutes before `<slug>-draft-rN.md`, which
  by time ends the round before. A metadata event whose label is the label of
  the submitted draft that starts a later round moves to the front of that
  round, unless some other event of its own round comes after it in time: then
  the move reorders nothing and the round it leaves ends where it did. A
  metadata event that does not move keeps its hint ("label rN is on a draft in
  round M; placed by time"); one that moves carries a `placement`.
* **Rounds for a chat-only article.** A round normally starts at a submitted
  version (a run's draft, or a file named with a `vNN` or `rN`). An article with
  no runs and no labelled files has none, so its versions are placed in one
  round per silence of more than 48 hours between consecutive events, or one
  round when there is no such gap. `accepted_at` defaults as before (just before
  the next round's first event), and `plan.json` stays editable. The rule is off
  as soon as the article has a run or a submitted (labelled) version.

Everything the scan is not sure of goes into the plan for the author, not into the
repository.

**What Code downloads from Drive before the scan.** The CLI cannot read Drive. A
Code session lists the article's Drive folder and, for each file, saves
`download_file_content`'s bytes (exact; `read_file_content` alters Markdown, see
[CLOSE-OUT.md](CLOSE-OUT.md), "Receiving a note") to `<dir>/<name>`, and writes
`<dir>/drive-manifest.json` with each file's `id`, `name`, `createdTime`,
`modifiedTime` and `md5Checksum` from `get_file_metadata`. The scan checks each
file's MD5 against the manifest and uses `createdTime`.

**What `apply` writes**, per included round, in order, into a repository whose
`main` does not yet have `articles/<slug>/` and that has no `round/<slug>/`
branch or `<slug>/r*` tag (the import starts an article's history; it never
interleaves with live rounds):

1. the branch `round/<slug>/rN` from `main`'s tip;
2. one commit per included event, with `author` and `committer` dates set to the
   event's time (`POST /git/commits` accepts both,
   [commits API](https://docs.github.com/en/rest/git/commits)): drafts and
   metadata as `draft: <slug> rN` or `revise: <slug> rN (chat)`, a run as
   `review: <slug> rN run M` with its `review.md` and `pipeline/` files, a
   close-out note under its fixed name. Every message ends with trailers naming
   the sources (`Imported-from: file … | drive … | chat … | run …`), never their
   contents;
3. the pull request (`<slug> rN`, its body saying it was imported and listing the
   events), and for each run a pull request review with the run's body (summary
   and findings checklist), built as write-back builds it. A pull request's own timestamps are the
   import's, which GitHub does not let anyone set; the commits carry the real
   dates;
4. the merge: a merge commit built with the Git Data API (the round branch's tree,
   parents `main`'s tip and the branch tip, dated `accepted_at`), and `main`'s ref
   moved to it as a fast-forward. GitHub marks a pull request merged when its
   commits reach the base branch by a direct push
   ([changelog, 2023-09-26](https://github.blog/changelog/2023-09-26-more-details-provided-when-a-pull-request-is-merged-indirectly-or-is-still-processing-updates/)).
   **Checked live (2026-10-27, `MHammett/ci-docs-scratch`, `ci_core.github_repo`'s
   `merge_commit`): it holds for a ref moved through the API.** A pull request
   opened on a round branch with four commits, then `main` moved to a merge
   commit built with the Git Data API (parents `main`'s old tip and the branch
   tip, `force: false`), read `state: closed`, `merged: true`, `merged_by` the
   token's user, `merged_at` the moment of the push and `merge_commit_sha` the
   hand-built commit, within five seconds (the first read, a second after the
   push, still said `open`, with GitHub's own test-merge SHA in
   `merge_commit_sha`: poll for `merged`, do not read it once). The web page shows
   the purple "Merged" badge, "merged commit 4f320ec into main" and "Pull request
   successfully merged and closed", and the timeline has a `merged` event with that
   commit. So the pull request's `merged_at` and merger are the import's, not
   `accepted_at` (the commit's own dates are), as step 3 already says. The
   `PUT .../merge` fallback was not needed;
5. the annotated tag `<slug>/rN`, `tagger.date` = `accepted_at`.

`apply` records each step's result (branch, commit SHAs, pull request number, tag)
in the plan's `applied` block and resumes from it after a failure; it never
deletes or force-moves anything, and `--dry-run` prints every write. The live
check runs on `MHammett/ci-docs-scratch`; applying to the real repository is the
maintainer's word, per article.

**As built (`ci-docs import apply`, issue #421; the issue governs where it
differs from the sketch above).** The code is `history_apply.py`. What the sketch
does not say, or says differently:

- **Commits.** One commit per included event, except that a `metadata` or
  `handoff` event rides in its draft's commit when it is `attached_to` that draft
  and sits next to it in the round. The round's first commit that carries a draft
  is `draft: <slug> rN`, later ones `revise: <slug> rN`; a run is `review: <slug>
  rN run M`; a close-out note is `close-out: <slug> rN to-chat|to-code` (by its
  `Direction:` line) and a publication handoff `publication: <slug> rN`. A commit
  that writes `draft.md` deletes a `handoff.md` the folder holds, and one that
  writes `handoff.md` deletes `draft.md` and `metadata.md`, so the folder is never
  in two forms. A commit's date is the latest `at` of its events. Trailers:
  `Imported-from:` (file name, Drive id from `drive-manifest.json`, `chat
  <conversation>/<message>`, `run <history name>`), `Imported-at:`, `Plan-event:`
  (the event id, which is how a resume recognises its own commits), and where it
  applies `Recovered:`, `Source:` (the plan's `note`) and `Corroborated: no`
  (`corroborated: false`, #430). The bytes written are the source's own, with CRLF
  turned into LF, checked against the hash the plan recorded; a chat-built text is
  the rebuilt one.
- **Run files** are `review.md`, `pipeline/report.json`, `pipeline/worklist.md` and
  `pipeline/run.json` as `--write-back` writes them (`pipeline/results.json` only
  when the plan has `"include_capture": true`). `run.json` has `pull_request:
  null`, because the pull request does not exist when the commit is made, and
  `imported: true`. A run with no `_review.md` beside its report is refused: exclude
  it in the plan.
- **Reviews** use `build_review_body` and the marker, `commit_id` the commit that
  wrote the version the run reviewed (else the round's latest commit before it). No
  line comments.
- **The merge** is `merge_commit("main", branch tip, "<slug> rN (#n)")` dated
  `accepted_at`, then `get_pull` polled (about a minute at most) until `merged`;
  this is the path #413's live check found works, and the live check of this
  phase took it for all seven rounds (`merged_at` is the import's, the merge
  commit's dates are `accepted_at`). If the flag never appears, the fallback
  `PUT .../merge` (`merge_method: merge`, dated now) is tried once, and its refusal
  is an error that leaves `main` where it is; a rerun never tries it, because by
  then `main` already holds the round.
- **The tag** is made by `tag_merged_rounds(client, slug, imported={n:
  accepted_at})`: only the rounds being imported, dated `accepted_at`, message
  `<slug> rN (imported): PR #n, runs ...`. Without `imported` the function is
  unchanged.
- **Refused before a write**, besides those in the issue: an included `review`,
  `changes` or `unknown` document (no place in the folder: set `include` false), a
  close-out note whose `Direction:` is not `to-chat` or `to-code`, a file holding a
  configured API key, a plan inside this checkout, and an `applied` block that
  names a round the plan no longer has.
- **Resuming** also adopts what GitHub has and the plan does not record: an
  unrecorded commit whose `Plan-event` trailers are the next events in the plan, an
  unrecorded pull request from the round's branch, an unrecorded review with the
  run's marker, a merged pull request, an existing tag on the merge commit. A
  recorded step that is gone, a plan whose recorded commits no longer match its
  events, or a branch with commits that are not this import's is an error.
- **Dry run** needs the token (it reads the repository to run the refusals), prints
  `would ...` for every write, and does not touch the plan file. The commit
  placeholders it shows (`dry-1`) are not SHAs.
- **Test support** (not product): ci-core gained `GitHubRepo.add_labels` and
  `get_commit`, and the in-process `FakeGitHub` now marks a pull request merged
  when its base branch is moved to a commit that holds its head (switch
  `marks_merged_on_ref_move`, delay `merge_flag_lag`), refuses a review whose
  `commit_id` is not in the pull request, and adds labels.

**Privacy.** The sources are private: unpublished drafts, chats, and slugs. They
go from local disk to the private repository and nowhere else. Tests use invented
slugs and synthetic fixtures (a made-up three-message export, made-up reports);
no real file, export, plan or slug is committed to this public repository, quoted
in an issue or PR, or written to a log a test keeps. The plan file holds paths and
hashes and is written outside the repository.

## Migration of what is built

### Decision 6: what changes, stays or retires

| Built in | Part | Fate |
|---|---|---|
| #407 `ci_core.github_repo` | `resolve`, `read`, `list_dir`, `commit_files` (CAS, fast-forward, secret scan), `seed_empty`, errors, token handling | **stays** |
| | new: pull requests (`get_pull`, `list_pulls`, `create_pull`, `merge_pull`), reviews (`list_reviews`, `create_review`), refs (`matching_refs`, `create_ref`), annotated tags (`create_tag`), labels (`ensure_label`), issues (`create_issue`, minimal), repository settings (`update_repo`), pagination, and on `commit_files`: optional `author`/`committer` with dates, `deletes={path: expected_blob}`, and a `merge_commit(...)` for the import | **added** |
| #408 `ci-docs init` | creation, seeding, manifest, never-clobber | **stays** |
| | `AGENTS.md.tmpl`, `README.md.tmpl`, `claude-project.md.tmpl`, `articles_README.md.tmpl`, the string test | **rewritten** |
| | repository merge settings, labels | **added** |
| #409 `--github` | slug validation, config block, token resolution, one-SHA pinning, error messages, `document_source`, history key | **stays** |
| | `find_rounds`, `pick_round`, the `-rN` name patterns, run number = round | **retired**: rounds come from pull requests and tags |
| | `--round` | **changed** (round = pull request or tag); `--pr` **added** |
| #410 write-back | `push_saved_run`'s refusals, secret scan, retry message, `_drift_lines`, the replay rules | **stay** |
| | target names `<slug>-review-rN.md`, `pipeline/<slug>-worklist-rN.md`, `pipeline/run_N_<ts>_report.json`, committing to `main`, `taken_run_numbers` from review names | **retired**: fixed names on the round's branch, run numbers from `pipeline/run.json` |
| | the pull request review (summary and findings checklist), `push-run` idempotency by marker, automatic tagging (`tag_merged_rounds`) | **added** |
| #411 `ci-docs ls` / `put` | `ls` | **changed (built in #418)**: shows the article's rounds (tags, open pull request, its last run) and the files at a ref |
| | `put`'s name patterns, the `-vM` rules, committing to `main` | **retired (#418)** |
| | `put` | **changed (built in #418)**: commits `close-out-to-chat.md` (or a drafting file) to the open round's branch, with `replace` on the blob it read, and comments on the pull request |
| #405 (open, not built) | `ci-docs pull-history` | **rewritten**: walks the commits that changed `pipeline/report.json` |
| #404 (open) | the last unchecked box, "Sync now shows `<slug>-review-rN.md`" | **moot**: reviews live on branches, which the built-in integration does not sync; closed |

**Backward compatibility: none, on purpose.** The repository has no articles, so
the per-round names are dropped rather than read alongside the new ones. The
scratch repository's per-round test files can stay where they are; nothing reads
them.

## Failure behaviour

| Condition | When it is caught | What happens |
|---|---|---|
| no token (private repository) | pre-flight | exit 1: set `GITHUB_TOKEN` or run `gh auth login` |
| 401 | pre-flight | exit 1: token rejected |
| 404 on the repository | pre-flight | exit 1: missing, or not visible to this token |
| no open round pull request, or two | pre-flight | exit 1, listing the article's pull requests and tags |
| write-back asked, no push permission, public repository, a tag or ref instead of a pull request | pre-flight | exit 1, before any model call |
| rate limited, primary or secondary | any call | stop and print the reset time; no retry loop |
| ref update refused (not a fast-forward) | write-back | re-read the head and retry, at most three times |
| automatic tagging fails | before the command's work | WARNING; the command goes on |
| a pipeline-owned file changed since `S` | write-back | refused, nothing written; exit 1 with `push-run` |
| pull request merged or closed during the run | write-back | nothing written; exit 1; the review is in local history |
| any write-back failure | after the review | local files kept; exit 1; `ci-docs push-run` retries |

## Security

- The token travels only in the `Authorization` header: never in a URL, a log
  line, a report or an exception message. Error bodies pass through
  `ci_core.redact`.
- Every outgoing file, **and every review body**, is scanned for the
  token and every configured API key value; a hit refuses the write.
- Drafts are unpublished work: the document repository must be private, the
  pipeline warns when it is public and refuses to write to it unless told to.
- This code repository is public. The document repository's name is in it (as a
  config default) by the author's choice; article slugs and contents are not.
  Tests and examples use invented slugs; live checks use `MHammett/ci-docs-scratch`.
- Nothing in this design force-pushes, rewrites history, or deletes a branch or a
  tag. The pipeline deletes no file; a round may delete `handoff.md` when it
  moves to the two-file form, through its own pull request.

## Today's behaviour, until the redesign lands

Until the phases below merge, the reader and the write-back already work on a
round's pull request (#416, #417) and `ci-docs ls`, `put`, `tag` and `accept` use
the fixed names (#418). What is not built yet is the import's `apply` step (#421)
and the phases marked "not yet" in the table below. No article should be put into
the repository under the retired per-round names (`<slug>-draft-rN.md`, `-vM`).

## What does not change

- `--draft`, `--raw-draft`, `--metadata`, `--url`, `--publish`, `--archive-only`,
  `--replay`, `--retry-failed` and `--offline` behave as they do today. Without
  `--github` no GitHub code runs and no token is looked for.
- `pipeline_history/`, `CI_HISTORY_ROOT`, the `run_N_*` names, the `_replay/` tree
  and `ci-history-report` are untouched.
- The Drive folder, the paste-based exchange and CLOSE-OUT.md's Drive naming keep
  working; an article can move to GitHub at any round (by importing its history)
  or never.

## Build phases

Each is an issue labelled `github-documents`, buildable from the issue alone.

| Issue | Phase | Depends on | Built in |
|---|---|---|---|
| [#413](https://github.com/MHammett/content-intelligence/issues/413) | A. client: pull requests, reviews, tags, labels, issues, dated commits, deletes, merge commits | nothing | [PR #424](https://github.com/MHammett/content-intelligence/pull/424) (merged) |
| [#414](https://github.com/MHammett/content-intelligence/issues/414) | ~~B. `review_anchor.py`~~: closed, moot once line comments were dropped | — | |
| [#415](https://github.com/MHammett/content-intelligence/issues/415) | C. `ci-docs init`: new `AGENTS.md` and templates, merge settings, labels | #413 | [PR #425](https://github.com/MHammett/content-intelligence/pull/425) |
| [#416](https://github.com/MHammett/content-intelligence/issues/416) | D. `--github` reads a round's pull request; `--pr`, `--round`; run numbers from `pipeline/run.json` | #413 | [PR #427](https://github.com/MHammett/content-intelligence/pull/427) |
| [#417](https://github.com/MHammett/content-intelligence/issues/417) | E. write-back to the round's branch and the pull request review (summary and findings checklist); `push-run` | #413, #416 | |
| [#418](https://github.com/MHammett/content-intelligence/issues/418) | F. automatic tagging in `ci-review --github` and `ci-docs`; `accept` and `tag` as conveniences; `ls`, `put`; CLOSE-OUT.md and the README | #413, #416 (#415 for the docs it points at) | [PR #428](https://github.com/MHammett/content-intelligence/pull/428) |
| [#419](https://github.com/MHammett/content-intelligence/issues/419) | G. claude.ai end to end on the scratch repository (the maintainer's checks) | #415, #417, #418 | not yet |
| [#420](https://github.com/MHammett/content-intelligence/issues/420) | H. import, step 1: `ci-docs import scan` over local files, saved runs and a Drive download | nothing (#416's names) | [PR #423](https://github.com/MHammett/content-intelligence/pull/423) (merged) |
| [#421](https://github.com/MHammett/content-intelligence/issues/421) | I. import, step 2: `ci-docs import apply` | #413, #415, #417, #418, #420 | [PR #432](https://github.com/MHammett/content-intelligence/pull/432) |
| [#422](https://github.com/MHammett/content-intelligence/issues/422) | J. import: the claude.ai export as a source | #420; the maintainer's export | [PR #426](https://github.com/MHammett/content-intelligence/pull/426) (merged) |
| [#405](https://github.com/MHammett/content-intelligence/issues/405) | `ci-docs pull-history`, rewritten (optional) | #413, #417 | not yet |

#413 and #420 can start at once. #404 (the first design's phase 5) is
closed: its last check is moot here, and #419 holds the claude.ai checks.

## Not decided here

- **A repository ruleset** to enforce what `AGENTS.md` only asks (tags `*/r*`
  immutable, no direct pushes to `main` except the bootstrap's). It needs a paid
  plan on a private repository, and the bootstrap commits straight to `main`, so it
  would need a bypass for the maintainer's own token.
- **Whether `write_back: true` in config is standing permission**, or each run
  asks with `--write-back`. The default stays the cautious one.
- **Reading a Template C publication handoff and its images from GitHub** for
  `--publish`.
