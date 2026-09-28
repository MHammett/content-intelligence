<!--
TEMPLATE D: CLOSE-OUT HANDOFF

Hands one article between a Claude Code session (this pipeline) and the
drafting-side Claude Chat project, in either direction, and closes out what is
still open before it publishes. Where the files live, how they are named, and
what the receiver does with them: docs/CLOSE-OUT.md.

IF YOU ARE EDITING THIS TEMPLATE: it has a consumer this repo cannot see. The
drafting project works from its own copy of this shape, not from this file (in
this deployment that copy is its instructions in Google Drive; docs/CLOSE-OUT.md
says what to put there). Nothing in the repo parses a close-out note, so when a
section header, a field label or the file name pattern here drifts from that
copy, nothing errors and nothing is logged: the drafting side writes a worse
note and the receiving side reads it worse. A renamed "Occurrences:" label, for
one, quietly turns an all-or-nothing edit into a guess. It is the trap
DRAFT_HEADERS sets for the drafting project's copy of Template A, except that
Template A's parser at least warns when a required field comes back empty.
Change a header, a label or the name pattern only together with that copy, and
say in the PR that you did.

IF YOU ARE FILLING IT IN: delete this comment and every bracketed note. Keep the
banner, the field labels and the RECEIVER paragraph as written. Never delete a
section: write "None this round." so an empty one can be told from a forgotten
one. The weight falls differently by direction. To-chat notes lean on WHERE THE
ARTICLE IS, LOCKED, QUESTIONS and DECISIONS. To-code notes lean on EDITS,
OPTIONAL EDITS, BEFORE PUBLISHING and INPUTS. Every section is valid both ways.
-->
CLOSE-OUT HANDOFF
Direction: [to-code | to-chat. Who has to act on this note. to-code is written
by the drafting chat for the Code session, to-chat by Code for the drafting
chat. The file name repeats it, so a listing of the article's folder shows which
notes are waiting on whom.]
Prepared: [YYYY-MM-DD, and who wrote it: Claude Code | the drafting chat | the
author]
Round: [rN, where N is the last pipeline run this note takes into account, the
same N as in `<slug>-review-rN.md`. Without it a note cannot be placed among the
drafts and reports around it, and notes about different runs look alike.]
Article: [the title as it stands today]
Publication: [your publication config name, i.e. the NAME in configs/NAME.yaml]
History key: [the directory name under pipeline_history/, read off the Code
side's disk. Do not copy it from the metadata file's History key: line: the
pipeline lowercases that value and cuts it to 60 characters, so the line and the
directory can differ. Run history hangs on this name and it never changes; a
note that guesses it is how one article ends up with two histories.]
Drive folder: [the article's folder in the shared Drive, named exactly as Drive
shows it]
Drive folder id: [the folder's id, so a session can list it without searching
by name]
Names match: [yes | no, left as it is on purpose: why, decided by whom, on what
date. Compare Drive folder with History key on the first note of every article,
in whichever direction it goes, before filling in anything else. A mismatch is
cheapest to settle while the folder holds a few files and nothing cites its
name; each file added later has to be moved or forgiven. Never leave this blank
or "unknown": if nobody has decided, the first item under DECISIONS is that
decision. Later notes copy the line forward.]
Draft revision: [the file every FIND below was copied from, and its word count,
e.g. `<slug>-draft-r4.md`, 3,210 words. A FIND that matches nothing is usually
the draft having moved on; this says which version the sender was looking at.]
In reply to: [the file name of the note this one answers, or "first note for
this article". Ids cited as answered (Q2, D1) are that note's ids.]
Supersedes: [the file name of an earlier note in this same direction and round
that this one replaces, or "none". Two notes on the same ground with no word on
which counts is how a receiver applies the wrong one.]
File: [this note's own name: `<slug>-close-out-rN-to-<code|chat>.md`, or ending
-v2, -v3 for a rewrite of the same round and direction. The slug is the
article's Drive folder name. Save it in that folder, next to the metadata file,
and nowhere else. The Drive file is the record; a pasted copy or a summary of it
is not, and a FIND taken from either may not match.]
Contains: [counts, so a truncated paste is caught: N edits, N optional, N
questions, N decisions, N checks, N locks. The receiver counts what it can see;
if the numbers differ it fetches the file and does not work from the paste.]

RECEIVER: Apply EDITS TO APPLY all-or-nothing. Each FIND must match its file
exactly as many times as its Occurrences line says; if any does not, apply none
of them and report which failed. Apply or decline each OPTIONAL edit and say
which. Do not apply anything under DECISIONS, and do not change anything under
LOCKED; dispute a lock with evidence instead. Give every id an outcome. Work
from this note and the files it names, not from memory of earlier rounds.

WHERE THE ARTICLE IS
[Two or three sentences: what has happened since the last note (runs, edits,
what the reviews found), the state the draft is in now, and what this note is
for and is not for (a merge, a rewrite). Skipped, the receiver reasons from
whatever it last saw, and the FINDs below describe text it no longer has.]
Work from: [the files this note refers to, by name]
Outcomes of the previous note: [one line per id in the note above: applied |
declined, and why | deferred to whom; or "first note". An id with no recorded
outcome looks the same as one nobody read, and comes back next round.]

LOCKED - DO NOT CHANGE
[Settled things a peer without the full history might reopen: a checked fact, a
figure, a structure the review history depends on (the History key is the
standing example). One entry each, with its reason. A bare "do not change X"
invites the reopening it is there to stop, because the receiver cannot tell a
settled fact from a preference. Locks carry forward from note to note until
someone lifts one, with a reason. A lock this side disputes stays in force until
the author rules.]
Locks received from the previous note: [accepted: ids | disputed: id, and the
evidence | "none received". Accepting means checking, not nodding: a lock this
side re-verified says so.]

L1. [what is locked]
Settled by: [the source or decision, and when]
If it comes up again: [what to check first: the source, not a reviewer's say-so
or either session's]
Retracts: [earlier advice from this side that this replaces, and where it was
given, or "none"]

EDITS TO APPLY
[Changes that are settled: the sender holds the evidence and no judgment is
left. Each is an exact find and replace or it does not belong here. If you
cannot supply the FIND text, move the item to OPTIONAL EDITS or QUESTIONS.

Copy FIND out of the file itself, never from a chat window, a summary, or a
connector's text rendering (which escapes markdown characters and rewrites
spacing). Curly quotes, dashes, non-breaking hyphens and those escapes are the
usual reasons an exact FIND matches nothing. FIND and REPLACE cover the same
unit, the whole sentence or paragraph: a fragment as FIND with a full sentence
as REPLACE leaves the sentence's head behind. An insertion is written the same
way: FIND the paragraph it goes into, REPLACE with that paragraph plus the
addition. Write REPLACE to the publication's voice rules; an exact edit is not
an exemption. If part of REPLACE cannot be known yet (a publish date), write it
as {{TOKEN}} and name the item under BEFORE PUBLISHING that fills it; a token
left in the draft is a failed edit. Repeat the block for each edit.]

E1. [label, and "answers Q2" if it does]
In: [the file the FIND was copied from]
Occurrences: [how many times FIND appears in that file; usually 1]
Evidence: [one or two sentences: what was checked, and where]
Metadata effect: [the KNOWN GAPS or UNCERTAIN SECTIONS entry this closes or
opens, or "none". A stale entry is re-flagged on every later run.]
FIND:
```
[the whole unit being replaced, verbatim]
```
REPLACE:
```
[the whole replacement, verbatim]
```

OPTIONAL EDITS
[Changes worth making that rest on something real but not fully verified. The
receiver may apply or decline each, and says which. Every entry is safe either
way: if its assumption fails the article is no worse off, and the entry says
why. If failing would make the article wrong, it is a decision; put it under
DECISIONS. Give the edit itself in the same form as EDITS TO APPLY.

Its REPLACE carries the same hedge as the entry: text that states as fact what
the entry calls an assumption puts a claim in the article that the note would
not make. A close-out note is held to the care the piece takes with its own
limits, and not less. Repeat the block for each entry.]

O1. [label]
What it changes and why: [the gain, in a sentence or two]
Not established: [the assumption, stated as a sentence and not as a hedge word:
"nobody has established that ..."]
If it holds: [what the article gains]
If it fails: [what is left, and why nothing is lost]
Guard: [what this must not be allowed to change, or "none"]
Metadata effect: [as under EDITS TO APPLY, if the receiver applies it]
In: [the file the FIND was copied from]
Occurrences: [how many times FIND appears in that file; usually 1]
FIND:
```
[the whole unit being replaced, verbatim]
```
REPLACE:
```
[the whole replacement, verbatim, hedged the way this entry is]
```

QUESTIONS FOR THE OTHER SIDE
[What this side lacks and the other holds: a photograph, a count, what the
drafting thread settled. These are for the receiving session, not the author;
choices that belong to the author go under DECISIONS. Quote the exact text the
answer will replace, so the answer can come back as an edit. A question the
receiver cannot answer is answered "cannot answer", with the reason, and not
left out. Repeat the block for each question.]

Q1. [the question]
Current text: [the exact sentence or line, in a fenced block if it is more than
a phrase]
Why it matters: [who flagged it and what it costs while it stays open]

DECISIONS FOR THE AUTHOR
[Choices that belong to the person who publishes. Neither session makes one
alone or applies one until told. Each carries a recommendation, since "your
call" alone hands the author the whole analysis to redo; if you have none, say
so and say why. The receiving session relays each to the author with the
recommendation and records the answer as that id's outcome. Repeat the block
for each decision.]

D1. [the choice, phrased so it can be answered in a word]
Live text now: [what the draft says today, and whether an alternative is drafted
and not applied; if so, give it verbatim or name the file it is in]
Recommendation: [one line], from [code | chat]
Because: [the reasons, including the strongest one against the recommendation
and why it loses]
If nobody decides: [what ships. An unanswered decision is still made, by
whatever text is live when someone publishes.]

BEFORE PUBLISHING
[Checks and tasks that must be done before the piece goes live: a claim that is
true only as of a date ("no vendor has published a fix"), a source to archive,
a figure to re-derive, a file to redact. Each names its owner, because two
sessions that are each unsure whose job it is either both do it or neither does.
A check is good for the day it ran: when a date in the article moves to a later
day, the check is re-run that day, and whoever moves the date owns it. An
earlier check by either side is corroboration, labelled with its date, and not
the check. Repeat the block for each item.]

C1. [what is checked or done]
Owner: [code | chat | the author | both, on purpose, as independent
confirmation]
Gates: [the edit or passage that stays open until this is done, by id or quote]
As of: [the date the result is good for]
Stated as: [the finding as "not found in X, Y and Z as of DATE", naming what
was searched, and never as "does not exist": a search shows what was looked at,
and only that.]
Could not check: [what was unreachable, e.g. a page that refused the fetch, or
"nothing". Carry it forward: a limit dropped from a later note becomes a claim.]
Result: [filled in by the owner when done: date, and what was found]

INPUTS FOR THE PUBLICATION HANDOFF
[Candidate values for Template C (publication.md), normally on the last note
before publishing. Use Template C's labels so a chosen value pastes across
unchanged, and give each candidate's length: SEO title is 20-60 characters,
meta description under 155. These are candidates; the author picks. Add any
other Template C field the sender has a proposal for.]

SEO title, candidate 1: [text] ([character count])
SEO title, candidate 2: [text] ([character count])
Meta description, candidate 1: [text] ([character count])
Meta description, candidate 2: [text] ([character count])
