DRAFT SUBMISSION HANDOFF
Generated: YYYY-MM-DD
Pipeline run: 1
Article: [Your article title]
Publication: [your publication config name, i.e. the NAME in configs/NAME.yaml]
Author: [optional — who "I" refers to in the draft. Only needed when it is not
         the publication's usual byline; citation verification uses it to check
         first-person claims against a source page.]
History key: [Optional but recommended. A short stable name for this piece, used
as its history directory. Without it the title is the key, so revising the title
starts a fresh history and the run loses its delta baseline. Set it once and
never change it, however much the headline moves.]
Drafted with: [Optional. The model you drafted this with — claude, openai, gemini,
mistral, grok or perplexity. That model is then dropped from the voice review,
because a model asked to flag AI phrasing in its own output is being asked to
notice its own habits. Delete this line if you wrote the piece yourself.]

PRIMARY CLAIM
[One or two sentences: the single thing this article argues. Not the topic —
the claim. The review models are asked whether the draft establishes this, so a
vague claim here produces vague findings.]

TARGET AUDIENCE
Primary: [Who you are writing for, specifically enough that a model can tell
when the draft talks past them.]
Secondary: [A more technical group who will check your sources, if there is
one. Delete this line if there is not.]

PRE-DRAFT ANALYSIS SUMMARY
[Optional, and the highest-leverage thing you can fill in — it tells the review
models what you already considered, so they stop re-raising it. Delete any line
you have nothing for.]

Steelmanned position: [The strongest version of your own argument.]

Strawmanned position: [Your weakest link — where you expect to be challenged.
Being honest here is what makes the red-team pass useful rather than generic.]

Steelmanned opposition: [The strongest argument against you.]

Strawmanned opposition: [The weakest version of the opposing case, and why it
does not hold.]

Counterarguments addressed in this piece:
- [Objection, and how the draft handles it.]

Counterarguments dismissed:
- [Objection, and why you are not engaging with it.]

SOURCES ALREADY CITED
[list or summarize; if none provided, write "None provided."]

UNCERTAIN SECTIONS
[Passages you are not confident about — the review models focus scrutiny here.
If none, write "None identified by author."]

OUT OF SCOPE FOR FACT-CHECK
[Passages no outside source can settle — first-person statements about your own
life or work, arithmetic the article derives from its own figures, conjectures
the piece frames as your own reading. One per line, starting with "- ", with an
optional reason after an em dash.

Excluded from fact-checking and citation resolution only; every other review
pass still sees them. You can also mark them in the draft with
<!-- ci:no-verify --> ... <!-- /ci:no-verify -->, which is the only way that
works when the draft has no metadata file at all.

If none, write "None identified by author."]

KNOWN GAPS
[What you know is missing, so the models judge whether the gap is acceptable
rather than just reporting it back. Keep this current when you revise: a stale
entry gets re-flagged on every subsequent run.
If none, write "None identified by author."]

ADDITIONAL CONTEXT FOR REVIEW MODELS
[Prior articles: ...

Known reader objections: ...

Intended use: ...

If none, write "None provided."]
