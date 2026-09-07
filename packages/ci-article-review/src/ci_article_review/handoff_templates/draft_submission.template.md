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
[One or two sentences: the single thing this article argues. Not the topic — the
claim. "Data center water use" is a topic; "applying arid-geography water
figures to a Great Lakes site is not analysis" is a claim.

The review models are asked to judge whether the draft actually establishes
this, so a vague claim here produces vague findings.]

TARGET AUDIENCE
Primary: [Who you are writing for, specifically enough that a model can tell
when the draft talks past them. Job titles and what they will do with the piece
beat demographics.]
Secondary: [Who else will read it — often a more technical group who will check
your sources. Optional; delete this line if there is only one audience.]

PRE-DRAFT ANALYSIS SUMMARY
[This section is optional but it is the highest-leverage thing you can fill in.
It tells the review models what you already considered, so they stop re-raising
it and start finding what you missed. Delete any line you have nothing for.]

Steelmanned position: [The strongest version of your own argument.]

Strawmanned position: [The weakest link in your argument — where you expect to
be challenged, and why. Being honest here is what makes the red-team pass useful
rather than generic.]

Steelmanned opposition: [The strongest version of the argument against you.]

Strawmanned opposition: [The weakest version of the opposing case, and why it
does not hold.]

Counterarguments addressed in this piece:
- [Objection, and how the draft handles it.]

Counterarguments dismissed:
- [Objection, and why you are not engaging with it.]

SOURCES ALREADY CITED
[List the sources the draft cites, or summarize them. The fact-check pass reads
this so it does not spend its budget re-discovering what you already have.
If none, write "None provided."]

UNCERTAIN SECTIONS
[Passages you are not confident about. The review models are told to focus
scrutiny here, so this directs effort where you want it.
If none, write "None identified by author."]

OUT OF SCOPE FOR FACT-CHECK
[Passages no outside source can settle, so fact-checking them can only produce a
false negative. One per line, starting with "- ". A short quote is enough; add a
reason after an em dash if it helps you remember why.

The three that come up constantly:
- first-person statements about your own life or work ("I have a side job")
- arithmetic the article derives from figures already in it
- conjectures the piece explicitly frames as your own reading

These are excluded from fact-checking and from citation resolution ONLY. Every
passage still goes to the red-team, argument, voice and completeness passes —
a claim only you can confirm can still be an argumentative weakness, and hiding
it from every reviewer would cost real findings.

You can mark passages in the draft itself instead, which also works with
--raw-draft: wrap them in <!-- ci:no-verify --> and <!-- /ci:no-verify -->. The
markers are HTML comments, so a reader never sees them.

If none, write "None identified by author."]

KNOWN GAPS
[What you know is missing, so the models assess whether the gap is acceptable
rather than simply reporting it back to you.

Keep this current when you revise — a stale entry gets re-flagged on every
subsequent run. The revise-after-review prompt regenerates this section for
exactly that reason.
If none, write "None identified by author."]

ADDITIONAL CONTEXT FOR REVIEW MODELS
[Anything else that changes how the draft should be judged. Common ones:

Prior articles: [pieces this builds on or references]

Known reader objections: [what your audience has pushed back on before]

Intended use: [where this will be read and what it needs to survive — a
conference talk and a planning-commission citation have different failure modes]

If none, write "None provided."]

DRAFT
[Your full article text goes here, below this header.

Everything from this line to the end of the file is treated as the article body,
so this section must come last. Markdown headings are preserved and used for the
structure checks.]
