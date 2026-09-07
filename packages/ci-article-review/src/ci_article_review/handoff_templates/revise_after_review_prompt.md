REVISE DRAFT AND METADATA AFTER A REVIEW PASS
Use this prompt after a pipeline.py review run, to have the chat model revise
the draft AND regenerate the metadata sections consistently in the same pass —
so the two never drift apart across a Chat -> script -> Chat -> script loop.

Why this exists: PRIMARY CLAIM, PRE-DRAFT ANALYSIS SUMMARY, SOURCES ALREADY
CITED, UNCERTAIN SECTIONS, and KNOWN GAPS all reach the review models directly
(see pipeline.py's _build_user_prompt). If you hand-patch just the DRAFT
section after a revision, those sections silently go stale — a KNOWN GAPS
entry the revision already closed keeps getting re-flagged, or an UNCERTAIN
SECTIONS entry the reviewers already resolved keeps drawing extra scrutiny.
This prompt has the model that made the revision also reconcile the metadata,
in the same exact header format every round, so the file stays parseable and
accurate without you doing it by hand.

Copy everything between the dashed lines into the SAME chat thread that has
the article's context (or paste the CURRENT metadata file if starting fresh),
then open the pipeline run's `run_N_<timestamp>_review.md` file (saved next
to the JSON report in `pipeline_history/<article-slug>/`) and paste its
SECTION 1 through SECTION 9 content below it, plus the SEO SUGGESTIONS and
SEO STRUCTURE REVIEW blocks at the end of that file if the run produced them.

Paste SECTION 9 too — it is easy to skip because it is long, and it is where
the citation work lands. In particular its "Unresolved" block contains
content-mismatch entries: sources that were fetched and read, where a model
found the page does not actually support the claim it was cited for. Those are
among the most actionable findings a run produces, and they are invisible in
SECTIONS 1-8.

Do NOT paste the WORKLIST block. It sits above SECTION 1 in the same file (and
in `run_N_<timestamp>_worklist.md` on its own), so it is the first thing you
meet when you open the review — but it is your list, not the model's. It is a
set of documents nobody has read yet, named down to bulletin numbers and URLs,
and handing that to a model asks it to fill in what those documents say. That
is the failure this whole pipeline exists to catch. Start the paste at
"## SECTION 1".

──────────────────────────────────────────────────────────────────────────────

You previously helped me with a draft article. I ran it through a multi-model
review pipeline and I'm pasting the consolidated findings below. Revise the
draft to address the findings you agree are valid, then output BOTH the
revised draft and an updated metadata block — in the exact format below, with
no additional commentary outside the two files.

Rules for the metadata update:
- PRIMARY CLAIM: leave unchanged UNLESS a finding directly challenges the core
  thesis itself (not a supporting detail). If you do change it, say so in one
  sentence before the output blocks — this is a rare, high-impact edit.
- PRE-DRAFT ANALYSIS SUMMARY: update only the specific steelman/strawman/
  counterargument bullets that the findings affected. Leave the rest as-is.
- SOURCES ALREADY CITED: append any new citations you added while addressing
  the findings. Don't touch existing entries you didn't change.
- UNCERTAIN SECTIONS: remove any entry the findings resolved (cite which
  section finding resolved it). Add any new ones you introduced by revising
  the draft, or that the reviewers surfaced but you're choosing not to fully
  resolve this round.
- KNOWN GAPS: same rule as UNCERTAIN SECTIONS — remove what you closed, add
  what you didn't.
- TARGET AUDIENCE and ADDITIONAL CONTEXT FOR REVIEW MODELS: leave unchanged
  unless a finding specifically implicates audience fit or context accuracy.
- SECTION 9 (Citations): the dispositions mean different things and must be
  treated differently. Read the opening line first — it says how many claims
  were actually checked against a fetched document, and it is usually a small
  fraction of the total, and it counts a source that was read and did NOT back
  the claim as checked too. "Read, and supports the claim" means a model read the source and
  confirmed it backs the claim, quoting the supporting sentence — those need
  nothing from you. "Read, and does NOT support the claim" means the source was
  fetched and read and does not back it; check the verdict, because
  `contradicts` means the source says otherwise (re-source the claim, soften it
  to what the source actually says, or drop it) while `not_addressed` usually
  means the wrong URL was checked (fix the citation, not the sentence).
  "Source URL identified, but the fetch was refused" names a document that
  exists but did not load — a 404, an unreachable host, or a 403 that survived
  a browser-shaped retry, which in practice means a subscription gate or a
  JS/CAPTCHA challenge rather than a bot policy. Some are still readable to a
  logged-in person, or via the archive copy listed beside it; those are worth
  opening by hand rather than treating as unsourced. A citation whose entry
  carries a "Reader access" line was read successfully — that line describes
  friction the reader may meet, not a weaker verification, and the archive link
  beside it is the durable half of the pairing. "Pointer only",
  "Fetched, but could not be read", and "No source identified"
  establish nothing either way — do not treat them as either confirmation or
  refutation; flag them in your summary as needing a human check. In
  particular, a claim the fact-check pass called "confirmed" may still have had
  no source retrieved at all; that is not corroboration. Never present an
  unverified citation as verified in the revision.
- SEO STRUCTURE REVIEW (if present): heading, opening, and title-promise
  findings from a search-reader's perspective. Apply the ones you agree with.
  A title_promise finding is worth real attention — it means the piece does not
  deliver what its title claims, which no amount of body editing fixes.
- SEO SUGGESTIONS (if the review file has that block): treat it as reading
  material, not instructions. Do NOT pick a focus keyword, and do not add SEO
  fields to the metadata — the metadata format has no SEO section, and which
  phrase to rank for is the author's strategic call. What IS worth flagging in
  your one-paragraph summary: if a candidate phrase looks right for this piece
  but the draft never actually uses it, say so, and say where it would fit
  naturally. Only work a phrase into the draft if the author names one.
- Increment "Pipeline run" by 1.

Here are the consolidated review findings:
[paste SECTION 1 through SECTION 9 from the run's `run_N_<timestamp>_review.md`
file here]

Here is the current metadata (paste your metadata_only.md content, or the
full handoff document's non-DRAFT sections):
[paste current metadata here]

Here is the current draft:
[paste current draft here — or omit this and the DRAFT block below if you're
using the --raw-draft / --metadata two-file workflow and will keep the
revised draft in its own file separately]

──────────────────────────────────────────────────────────────────────────────
REQUIRED OUTPUT FORMAT — two blocks, in this order:

1) A one-paragraph summary of what changed and why, OUTSIDE any file content
   (for your own sanity-check — not parsed by the pipeline).

2) The metadata file, exactly matching metadata_only.md's header order:

DRAFT SUBMISSION HANDOFF
Generated: [today's date]
Pipeline run: [incremented number]
Article: [title]
Publication: [publication name]

PRIMARY CLAIM
[unchanged or updated]

TARGET AUDIENCE
[unchanged unless implicated]

PRE-DRAFT ANALYSIS SUMMARY
[updated bullets only where findings applied]

SOURCES ALREADY CITED
[existing + any newly added]

UNCERTAIN SECTIONS
[resolved entries removed, new ones added]

KNOWN GAPS
[resolved entries removed, new ones added]

ADDITIONAL CONTEXT FOR REVIEW MODELS
[unchanged unless implicated]

3) If you pasted the draft in step above (not using the two-file workflow),
   also include:

DRAFT
[the fully revised article text]

──────────────────────────────────────────────────────────────────────────────

AFTER GENERATING: save the metadata block as your-article-metadata.md
(overwriting the prior round's version) and, if using the two-file workflow,
save the revised draft as your-article-draft.md. Run:

    uv run ci-review --raw-draft your-article-draft.md --metadata your-article-metadata.md --publication your_publication_name

Or, if you used the single-file format (draft included in this same
response), save the whole thing as your-article-handoff.md and run:

    uv run ci-review --draft handoff_templates/your-article-handoff.md --publication your_publication_name
