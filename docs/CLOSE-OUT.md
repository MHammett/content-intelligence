# Close-out notes: handing an article between Code and the drafting chat

An article drafted in a claude.ai project and reviewed by this pipeline changes
hands more than once before it publishes. The Code session says where the piece
stands, and the drafting side sends back what it holds that Code does not. Each
hand-off is a **close-out note**, written from
[`close_out.md`](../packages/ci-article-review/src/ci_article_review/handoff_templates/close_out.md)
(Template D). One template serves both directions; its `Direction:` line says
which. "The author" below means the person who publishes.

Nothing in this repo parses a close-out note. Template A (the draft submission,
with `metadata_only.md` as its two-file form) and Template C (publication) are
read by `handoff_parser.py`; D is read only by a model on the other side. Its
shape is held by convention, which makes the drafting side's own copy of it (see
[The drafting project's copy](#the-drafting-projects-copy)) the thing to keep
in step.

## Where the notes live

In the article's own folder in the shared Drive (`Claude Docs/<slug>/` in this
deployment), next to the article's metadata file. The Drive file is the record.
Pasting it into the other session is transport: a paste can be cut off, and a
summary of it is not the note. On 2026-09-27 a paraphrase given in chat had to be
replaced by the Drive file before anything could safely be edited, because an
all-or-nothing edit needs the literal text.

Name each note:

```
<slug>-close-out-r<N>-to-<code|chat>[-v<M>].md

pjm-capacity-auction-close-out-r3-to-chat.md
pjm-capacity-auction-close-out-r3-to-code.md
pjm-capacity-auction-close-out-r3-to-code-v2.md
```

- `<slug>` is the article's Drive folder name, the stem the drafting project
  already gives `<slug>-draft-rN.md` and `<slug>-metadata-rN.md`.
- `close-out` is the kind, the only new word beside that project's `draft`,
  `metadata`, `handoff`, `review` and `publication`.
- `r<N>` is the last pipeline run the note takes into account, the N of
  `<slug>-review-rN.md`.
- `to-code` or `to-chat` says who has to act, so a listing of the folder shows
  which notes are waiting on whom.
- `-v<M>` marks a rewrite of the same round and direction. Its `Supersedes:` line
  names what it replaces, and the highest `M` counts. Earlier notes are never
  overwritten or deleted.

There is no date in the name. The first two notes were written about 16 minutes
apart on one day, so a date could not tell them apart, and the drafting project
already numbers rounds. The date is in `Prepared:` and in Drive's own timestamps.

## Check the names first

An article has two names that are meant to be one string. The Drive folder is
named for the slug, and the drafting project uses that slug as the stem of every
file in it and never renames the folder. The pipeline files its runs under
`pipeline_history/<key>/`, and the delta baseline, the reproducibility context
and the phrasing-habit counts all hang on that key (`_history_key` in
`pipeline.py`). The first article to go through this process had two different
names, and both sides found out only at the end: the folder had been created
under the drafting side's early slug, and the pipeline's first two runs, made
with no `History key:`, had filed themselves under the 60-character slug of the
title. Keeping the pipeline's key was the only choice that did not split the
history, so the folder was left as it was.

So `Names match:` is the first thing a note records, and the comparison is made
at the first run, not at close-out:

1. **Before the first run.** The slug is confirmed with the author (the drafting
   project already does this before it creates the folder) and goes into the
   metadata's `History key:` line. The run's handoff-gap report flags a missing
   line. Then nothing needs reconciling.
2. **After the first run.** Read the directory name from disk (`ls
   pipeline_history/`) and compare it with the Drive folder name. Read it from
   disk, not from the metadata: the pipeline normalizes a `History key:`
   (`history._slug`: lowercase, punctuation dropped, cut to 60 characters, and
   fewer than 8 letters or digits files under `untitled/`), so the line and the
   directory can differ. A run with no `History key:` names the directory it used
   (`filed itself under the title (...)` in its handoff-gap report).
3. **If they differ, record a decision.** The pipeline directory is the fixed
   point, because history cannot be split, so the choice is about the Drive
   folder and it is the author's. Rename it: anything that refers to the folder by
   name breaks, and the drafting project's own rule is never to rename an article
   folder, so that rule needs an explicit exception. Or leave it, and carry the
   mismatch, the reason and who decided in `Names match:` on every note. "Not yet
   decided" is not a value; it is the first item under DECISIONS.

Every file added to a wrong-named folder, and every note that cites its name,
makes the rename dearer. That is why this is a step at the first run.

## Writing a note

- List the folder first. If a note for this round and direction exists, you are
  writing its `-v` successor and `Supersedes:` names it. The first close-out came
  out twice, about 16 minutes apart, and neither note mentions the other.
- Copy the identity block forward from the last note and re-derive only what may
  have changed.
- Code has no standing permission to write to the author's Drive. It saves a
  note to the article's folder when the author has said it may; otherwise it
  hands the file to the author, who saves it there.
- The rules for each section are in the template's bracketed notes. They are the
  ones the drafting side needs at the moment of writing, so they live there and
  not here.

## Receiving a note

1. **Get the file, not the paste.** List the article's folder and download the
   file's bytes. The Drive connector's `read_file_content` is a rendering: on the
   first close-out it turned `## Heading` into `\#\# Heading`, added two trailing
   spaces to each line, and collapsed `FIND:    - text` to `FIND: - text`. A FIND
   copied from that can fail to match a draft that contains it. Use
   `download_file_content`, which returns the file's own bytes as base64.
2. **Check the note against what you hold.** The `Contains:` counts match what you
   see (if not, the paste was cut off), `Draft revision:` names the file you
   have, and `History key:` matches `pipeline_history/`.
3. **Apply EDITS TO APPLY all-or-nothing.** Count each FIND in its `In:` file.
   Every count must equal its `Occurrences:` line; if any does not, apply none,
   and report which failed with the nearest text you can find. Do not fuzzy-match
   and do not apply a REPLACE in spirit. Typography (curly quotes, dashes,
   non-breaking hyphens) is the usual cause. No `{{TOKEN}}` may remain afterwards.
4. **Everything else.** Apply or decline each OPTIONAL edit and say which. Answer
   each question, or say you cannot. Relay each decision to the author with its
   recommendation and apply nothing. Run the checks you own. Leave locked things
   alone; dispute with evidence instead.
5. **Reconcile the metadata.** An edit's `Metadata effect:` names the KNOWN GAPS
   or UNCERTAIN SECTIONS entries it closes or opens. Update them in the same
   pass, because a stale entry is re-flagged on every later run.
6. **Record an outcome for every id**, in the reply to the author and under
   `Outcomes of the previous note:` on the next note.

## Why one template, not two

The first close-out produced two notes on the same ground, framed differently,
and nothing in either says which one counts. Two templates, one per direction,
would keep that ambiguity on the sending side ("which is this?") and double what
the drafting project's copy has to track. The receiver's job is the
same either way: apply exact edits all-or-nothing, route decisions to the author,
honour the locks. So the sections are the same both ways and only their weight
differs; the template's header comment says which lean where.

## Where the sections came from

"The precedent" is the 2026-09-27 pair, `close-out-instructions-2026-09-27.md` and
`sync-back-from-drafting-chat-2026-09-27.md`, and the Code-side sync note that
prompted them.

| Section | Kept from the precedent | Added, and why |
|---|---|---|
| Identity block | the notes' opening lines; the history-key correction | `Names match` (found only at the end); `Supersedes` and `In reply to` (two notes, one ground); `Contains` (the close-out counts four open items and three answered without saying which, and pastes get cut off) |
| WHERE THE ARTICLE IS | "where the article actually is", and working from the files, not memory | `Outcomes of the previous note`, an outcome per id |
| LOCKED | locks with their reasons; "do not change"; accepting the other side's locks | `Locks received`; the reason is required |
| EDITS TO APPLY | FIND and REPLACE | `Occurrences`, `In`, and FIND covering the whole unit. One of the precedent's four edits was verbatim; the rest quoted an opening, a tail fragment, or nothing |
| OPTIONAL EDITS | two optional items, each resting on a stated assumption | a fixed shape (`Not established`, `If it holds`, `If it fails`); the hedge must reach the REPLACE text |
| QUESTIONS | open items only the other side could answer | split from decisions; the exact current text is quoted |
| DECISIONS | "decisions that are not mine": a recommendation and reasons | `If nobody decides`; a recommendation, or none and why |
| BEFORE PUBLISHING | the re-check for a dated claim, the archive check, a redaction note | an owner, `Gates`, `As of`, `Could not check` (stated in the first note, missing from the second) |
| INPUTS | title and meta description candidates | Template C's labels; lengths |

A check is good for the day it ran. Where a date in the article moves to a later
day, the check is re-run that day and whoever moves the date owns it; an earlier
check by either side is dated corroboration. This is a default the template
states, and a note can override it.

## The drafting project's copy

The drafting project does not read this repo. Its instructions (in this
deployment `Claude Docs/mikehammett-project-instructions.md` in Drive, last
modified 2026-09-09) list the kinds of file it writes into an article's folder,
and none is a close-out note. Until that changes, the drafting side improvises,
which is how the first two notes got two names and two shapes. As of 2026-09-27
the Drive copy has not been edited.

To bring it in line, add `close_out.md` to the project in full, and add this to
the file list under "File and artifact storage":

```
- `<slug>-close-out-rN-to-code.md` and `<slug>-close-out-rN-to-chat.md`: a
  close-out note, written from the close-out template. N is the last pipeline
  run it takes into account, the N of `<slug>-review-rN.md`. A rewrite of the
  same round and direction is `-v2`, `-v3`, and says which note it supersedes.
  List the folder before writing one. Never overwrite one.
```

After that, a change to a header, a label or the name pattern in `close_out.md`
means the same change to that copy. The template's header comment says so.

## Not built

- A tool that applies a note's edits. The format parses simply (a fenced `FIND`
  and `REPLACE` per edit, an `Occurrences` count), but no code reads it yet.
- A check of a note's shape: `Contains:` counts, ids that resolve, `Names match:`
  not blank.
- `ci-setup` does not copy `close_out.md` into a working directory
  (`_WORKING_TEMPLATES` in `setup.py`). Whether other users want it is undecided.
- `DRAFT_HEADERS` in `handoff_parser.py` has the same out-of-repo consumer and
  carries no comment saying so.
- The Drive copy, above.
