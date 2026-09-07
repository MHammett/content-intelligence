# AI-detection tools: evaluated, not adopted

**Question:** should this pipeline call a commercial AI-text detector (GPTZero,
Originality.ai, Pangram, Turnitin, Copyleaks) alongside or instead of the
`voice_style` pass?

**Verdict: no.** Not on cost, and not on accuracy — on *information*. The
pipeline already knows the answer a detector sells, and the one thing a detector
could add is the thing detectors are measurably worst at.

Investigated 2026-09-05. This document exists so the question is not
re-investigated from zero. If you are about to re-open it, read
[What would change this answer](#what-would-change-this-answer) first — that
section names the specific evidence that would flip the verdict, and none of it
existed as of this writing.

---

## Scope: which instrument this is about

Three different things get called "AI detection." This document rejects exactly
one of them. Do not read it as rejecting the other two.

| Instrument | What it does | Status here |
|---|---|---|
| **Heuristic classifier** — GPTZero, Originality.ai, Pangram, Turnitin, Copyleaks | Guesses authorship from surface style. No key, no ground truth | **Rejected. This document.** |
| **Keyed watermark detection** — e.g. Anthropic's detection API | Re-runs a keyed pseudorandom function over the token stream. Actually reliable, by design | Different instrument. See README, "Authorship provenance" |
| **Typographic residue** — `ci-markers` | Counts characters a keyboard does not produce. Deterministic, free, no guessing | Already shipped (PR #135) |

The distinction that matters: a classifier *infers* from style and can be wrong
in both directions; a keyed watermark detector *verifies* a signal that is
provably there or not. The evidence below is about inference, and none of it
transfers to verification.

Two consequences worth holding onto:

- **The README's "Authorship provenance" section is the right neighbour to this
  one.** It records that Claude has carried a statistical watermark since
  August 2026, that nothing in this repo can detect it without the provider's
  key, and that Anthropic runs a third-party detection API — in private preview,
  with media and fact-checkers among the eligible categories. If detection is
  ever genuinely wanted here, **that is the door to knock on, not Pangram's.**
  It answers a question a classifier can only guess at.
- **`ci-markers` already occupies the "statistical, not rhetorical" slot** that a
  detector was imagined to fill, and it does so without an API, a subscription,
  or a false-positive rate. Its README section notes that stripping typography
  "will not fool a classifier, which keys on sentence rhythm and word choice far
  more than on punctuation." That is broadly right and now has a citation
  (arXiv 2603.23146) — with one correction: punctuation patterns *are* among the
  features classifiers key on, alongside formality, vocabulary distribution, and
  sentence-length uniformity. The conclusion stands; the mechanism is slightly
  wider than "far more than punctuation" implies.

---

## The short version

A detector answers *"was this text machine-generated?"* The
[`voice_style`](../packages/ci-article-review/src/ci_article_review/prompts/ai_speak.txt)
pass answers *"which sentence is doing which specific machine-ish thing, and what
should it say instead?"*

Those look like the same question. They are not, and the gap is not a matter of
detector quality:

1. **The pipeline already declares the drafting model.** The handoff carries a
   `Drafted with:` line; `pipeline._drafting_model()` reads it and excludes that
   model from `voice_style`. A document-level detector score on a draft we have
   already been *told* was AI-drafted carries no information — the prior is
   already 1.0. There is no belief left for the score to update.
2. **So only passage-level localization could add anything** — "*this* paragraph
   still reads machine-written after your revision." That is precisely the regime
   where detectors are weakest, where their scores stop being probabilities, and
   where the published accuracy numbers do not apply.
3. **And even a correct passage-level flag is not a finding.** Section 3 entries
   require `passage`, `problem`, `suggested_rewrite`, `steelman_considered`. A
   detector supplies a span and a number. Turning that span into a finding needs
   an LLM pass over the passage — which is `voice_style`, run again, now primed
   to confirm the flag.

---

## Q1: How accurate are they on the three categories that matter?

Detectors are genuinely good at the easy case and genuinely bad at ours.

### Pure human vs. pure LLM — solved, and the numbers are real

The strongest evidence is an *independent* academic audit, not a vendor
benchmark: Jabarian & Imas, **"Artificial Writing and Automated Detection"**
(Chicago Booth / BFI Working Paper 2025-116, August 2025). They built ~1,992
pre-2020 human texts plus ~1,992 AI texts across six genres (blogs, consumer
reviews, news, novels, restaurant reviews, résumés) from four frontier models,
and tested GPTZero, Originality.ai, Pangram, and a RoBERTa baseline.

| Detector | False positives (human flagged AI) | False negatives (AI missed) |
|---|---|---|
| Pangram | ~0% | 2–4% |
| GPTZero | <1% | 0–2% |
| Originality.ai | <1% | 10–40% |
| RoBERTa baseline | — | "unsuitable for high-stakes applications" |

That is a real result and it deserves to be stated plainly: **on long, unedited,
general-interest prose, the best detectors are accurate.** Pangram's own
technical report claims a domain-weighted 0.02% FPR, and the independent audit
broadly corroborates the order of magnitude.

Note what the corpus is not: no technical trade press, no infrastructure
analysis, no industry commentary. Six genres, none of them ours.

### LLM-assisted-then-edited — the category we actually have, and it collapses

This is the one that matters, and every measurement of it is bad.

**Hadra, Cambridge & Mesbah, "Evaluating the Accuracy and Reliability of AI
Content Detectors in Academic Contexts"** (*International Journal for
Educational Integrity*, 2 Feb 2026; preprint Sept 2025). 192 texts, balanced
across human, AI, and 50/50 hybrid authorship. They tested Turnitin and
Originality:

| Metric | Turnitin | Originality |
|---|---|---|
| Overall accuracy | 0.61 | 0.69 |
| **Recall on hybrid text** | **0.31** | **0.02** |
| Hybrid F1 | 0.34 | 0.04 |

Originality identified **2%** of mixed human/AI texts. The authors' own reading:
the hybrid class has "near-zero recall (0.02)," which "heavily impacts these
averages, exposing a critical" weakness.

**ARB: A Matched Authorship-Rewriting Benchmark** (arXiv 2607.29539) isolates
content origin from linguistic surface across 23,400 texts. Measuring TPR at 1%
FPR, moving from directly-generated LLM text to LLM-rewritten *human* text:

| Detector | Human vs. Free-LLM | Human vs. LLM-rewritten-human | Δ |
|---|---|---|---|
| FastDetectGPT | 91.2% | 30.8% | −60.5 |
| Binoculars | 93.5% | 15.1% | −78.4 |
| RADAR | 66.8% | 12.2% | −54.6 |

**HACo-Det** (arXiv 2506.02959) concludes fine-grained co-authored detection is
"far from solved," with metric-based methods at 0.462 average F1.

A caveat I want to be honest about: **none of these measure our exact direction.**
ARB, HACo-Det, and Pangram's own EditLens all measure *AI editing of human text*.
Our case is the reverse — *human editing of AI text*. The entire literature and
every shipping product is built around the academic-integrity question ("did a
human's submission get AI help?"), not the craft question ("does my AI draft
still carry machine tells after I revised it?"). That asymmetry is itself a
finding: the instrument is not aimed at our question.

The closest thing to a direct test of our workflow is anecdotal but pointed.
Michael G. Wagner (*The Augmented Educator*) ran his own essays through Pangram.
His process — draft with Claude assisting phrasing and revision, then multiple
rounds of human editing — is close to ours. **Pangram classified them "100%
human-generated."** His summary of the general case: standard detectors
misclassify AI-polished text as fully human between 10% and 75% of the time.

Pangram's ICLR 2026 paper **EditLens** ("Quantifying the Extent of AI Editing in
Text," arXiv 2510.03154) is real progress here — a regression model scoring
*degree* of AI intervention, F1 94.7% binary / 90.4% ternary, and it powers the
"Lightly AI-Assisted" label in the product. But it is trained and evaluated on
AI editing of human originals, and standard detectors on that same ternary task
score 73.0%.

---

## Q2: False positives on careful, formal, technical writing

The 2023 concern is partly obsolete, and partly worse than advertised. Both
halves matter.

**The non-native-speaker bias has substantially improved.** Liang et al.
(*Patterns*, 2023) found seven detectors flagged 61.3% of TOEFL essays as AI.
That result is real but it indicted 2023-era *perplexity-based* detectors.
Recent replications report no false positives for Pangram, Copyleaks, or
Turnitin on non-native writing, and a 2026 EACL replication in a Czech setting
found the bias did not reproduce. Pangram's technical report claims it is "not
biased against nonnative English speakers" on TOEFL data. **Do not cite Liang
et al. as a current objection to modern trained classifiers** — it is a fair
description of 2023 perplexity detectors and unfair to Pangram 3/4.

**The technical-writing penalty, however, is measured and large.** Hadra et al.
broke results out by genre (p < 0.0001 for both detectors):

| Detector | Humanities accuracy | Science accuracy | Drop |
|---|---|---|---|
| Turnitin | 0.86 | 0.51 | −35 pp |
| Originality | 0.96 | 0.58 | −38 pp |

Originality goes from 96% to a coin flip when the subject matter turns
technical. This is the closest published proxy for the writing this pipeline
reviews.

**And the mechanism explains why.** "Why AI-Generated Text Detection Fails:
Evidence from Explainable AI Beyond Benchmark Accuracy" (arXiv 2603.23146) finds
detectors do not key on authorship at all — they key on **formality markers,
vocabulary distribution, punctuation patterns, and sentence-length uniformity**.
The paper's conclusion about who gets hurt is precise: human authors producing
edited, polished content — academic papers, legal documents, technical
specifications — exhibit stylistic patterns overlapping AI outputs, so detection
"becomes unreliable precisely where it's most needed."

That is the failure mode stated exactly: **the detector penalizes the surface
signature of rigor.** An author whose brand is rigor gets a tool that taxes it.
The same paper reports that constraining FPR below 0.5% — the level you need
before showing a flag to an author — collapses detector effectiveness.

One more granularity trap: published FPRs are almost always **document-level**.
Turnitin reports <1% at document level; third-party testing puts its
*sentence-level* rate near 4%. Passage-level flagging is what we would need, and
it is several times noisier than the headline. On a 10,000-word draft that is a
handful of false accusations per run — each one landing in Section 3 as a
finding with a confident-looking score attached.

---

## Q3: APIs, cost, and whether the number is a probability

Integration is mechanically possible. Both leading APIs return span-level output.

| Service | API cost | Notes |
|---|---|---|
| **Pangram 4** | **$0.05 / 100 words** (−20% bulk) | Window-level output with `start_index`/`end_index`, `fraction_ai`, `fraction_ai_assisted`, `ai_assistance_score`, `is_humanized` |
| Pangram 3 | $0.05 / 1,000 words | Older model, 10× cheaper per word |
| GPTZero | ~$0.15 / 1,000 words | Sentence + paragraph + document probabilities, `completely_generated_prob`, per-sentence `generated_prob`, perplexity, burstiness |
| Originality.ai | ~$0.01 / 1,000 words | API gated behind a plan from **$136.58/month annual-billed** |

### The cost is disqualifying on its own

Ground it in this repo's own numbers. `draft_submission.filled-example.md` is
**10,011 words** / 72,520 characters, and the README puts a `maximum`-preset run
against it at **$3–5** — that is five domains × up to six models, ~30 LLM calls.
A `standard` run is **under $1.00**.

One Pangram 4 call on that same draft: 101 × $0.05 = **$5.01** ($4.01 bulk).

**The detector costs more than the entire six-model, five-domain review it would
be a footnote inside** — and roughly 5× a standard run, to add one number.
GPTZero at $1.50 is still above a full standard run. Originality's per-call cost
is trivial but its floor is $1,639/year, against a workload of a few articles a
month.

### The score is not a probability

Directly relevant to "a number worth acting on, or one that only looks like
one." Pangram's own v3 migration guide:

> "The interpretation of `ai_assistance_score` differs from `ai_likelihood`.
> These fields are not one-to-one. Expect more predictions in the middle range
> (0–1). Use `label` to understand our recommended interpretation."

The vendor is saying: this is a **magnitude of AI assistance**, not a calibrated
confidence, it deliberately piles up in the ambiguous middle, and you should read
the categorical label instead of the number. GPTZero's per-sentence
`generated_prob` is derived from perplexity and burstiness — and "Detecting the
Machine" (arXiv 2603.17522) reports perplexity methods now show **polarity
inversion**, because modern LLM output has *lower* perplexity than human text,
inverting the assumption the score rests on.

A number we cannot threshold, that clusters in the middle, that the vendor tells
us not to read as a probability, is not something to put in front of an author
as a finding.

---

## Q4: Does it surface anything `voice_style` does not?

No — and the two instruments are not interchangeable in the way the original
question assumed.

**What `ai_speak.txt` produces.** Named, diagnosable failure modes: hedging,
throat-clearing, vague significance gesturing, the problem→cause→solution
skeleton, listicle inflation, restatement conclusions, audience condescension.
Each finding carries `passage`, `problem`, `suggested_rewrite`, and
`steelman_considered`. It is an *editor*.

**What a detector produces.** A span and a score. It is a *classifier*. It cannot
say which habit the passage exhibits or what it should say instead, because it
does not model habits — per arXiv 2603.23146 it models formality, punctuation,
and sentence-length uniformity.

Three consequences:

1. **A detector flag cannot become a Section 3 finding without an LLM pass**, and
   that pass is `voice_style` — now running with a prior that biases it toward
   confirming the flag. Strictly worse than running it clean.
2. **It has no steelman.** `ai_speak.txt` requires the reviewer to ask whether a
   passage is an intentional stylistic choice before flagging it. That filter is
   the specific defense against penalizing deliberate rigor — exactly the failure
   mode detectors exhibit. A detector emits its number regardless.
3. **The drafting-model-exclusion rationale does not transfer.** The drafter is
   excluded from `voice_style` because a model under-reports its own habits — a
   known, bounded, one-directional bias. A detector does not fix that; it
   substitutes a different bias aimed squarely at technical prose. "Statistical
   rather than rhetorical" is true, but the statistics are of surface style, not
   of authorship.

**We also already have the longitudinal instrument.**
[`voice_pattern_report.py`](../packages/ci-article-review/src/ci_article_review/voice_pattern_report.py)
clusters voice flags across `pipeline_history/` and reports patterns recurring in
≥3 distinct articles as candidates for `banned_words` / `banned_phrases`. That is
the cross-article "is this a real habit or a one-off?" signal a detector's
aggregate score would only approximate — and it is already diagnostic and already
free.

---

## What would change this answer

Concrete triggers. Absent these, do not re-open:

1. **A published evaluation in our direction** — LLM-drafted, then human-revised
   (not human-drafted, then LLM-edited) — showing passage-level precision high
   enough to survive a <0.5% FPR constraint. Today this direction is essentially
   unmeasured.
2. **Genre evidence on technical/trade prose.** Every public benchmark uses
   student essays, news, fiction, reviews, or academic humanities. The one study
   that split by genre found a 35–38 pp collapse on science writing.
3. **A calibrated score.** Something the vendor is willing to call a probability
   and to publish a reliability diagram for — not a magnitude that "clusters in
   the middle" with a warning to read the label instead.
4. **Pangram-4-class pricing at Pangram-3-class rates**, or roughly a 10×
   reduction. At $0.05/100 words the detector costs more than the whole review.
5. **A cheap document-level gate becoming useful** — which requires us to *stop*
   knowing the drafting model. As long as the handoff declares `Drafted with:`,
   a document-level score is information-free.

### The one narrow "yes" worth remembering

If a detector is ever used here, the defensible shape is **one document-level
call, once, at final-submission time** — not per-review, not per-passage. That
is the regime where the independent evidence is strongest (long passages, ~0%
FPR) and the cost is a single call. Its value would not be reviewing the draft;
it would be answering "would a third party's detector flag this?" — a
*publisher-risk* question, not a craft question.

That is a different feature from `voice_style`, and if it is ever wanted it
should be scoped separately. It is not a reason to put a detector in the review
loop.

---

## A note on sourcing

Vendor benchmarks in this space are contested and should not be taken at face
value. GPTZero and Pangram each publish pages claiming to beat the other on the
same Chicago Booth benchmark; in January 2026 GPTZero disputed the study's
methodology, arguing the researchers queried the wrong API field. Pangram's own
technical report excluded Claude outputs from its benchmark due to rejected
requests. Where this document cites a number, it prefers the peer-reviewed or
independent source and says which is which.

Two claims circulating in secondary coverage were **not** verifiable to a primary
source and are deliberately excluded: a widely-repeated "12% of Pulitzer-nominated
articles flagged" figure attributed to a *Washington Post* investigation, and
various "technical content FPR 10–20%" numbers appearing only on SEO/affiliate
sites.

## Sources

**Independent / peer-reviewed**
- Jabarian & Imas, "Artificial Writing and Automated Detection," Chicago Booth /
  BFI WP 2025-116 (Aug 2025) — [lay summary](https://www.chicagobooth.edu/review/do-ai-detectors-work-well-enough-trust)
- Hadra, Cambridge & Mesbah, "Evaluating the Accuracy and Reliability of AI
  Content Detectors in Academic Contexts," *Int. J. Educational Integrity*
  (Feb 2026) — [doi:10.1007/s40979-026-00213-1](https://doi.org/10.1007/s40979-026-00213-1)
- Liang et al., "GPT detectors are biased against non-native English writers,"
  *Patterns* (2023) — superseded for modern classifiers; see Q2
- "Different Time, Different Language: Revisiting the Bias Against Non-Native
  Speakers in GPT Detectors," EACL 2026

**Benchmarks / preprints**
- [ARB: A Matched Authorship-Rewriting Benchmark](https://arxiv.org/html/2607.29539v1) (arXiv 2607.29539)
- [HACo-Det: Fine-Grained Detection under Human-AI Coauthoring](https://arxiv.org/pdf/2506.02959) (arXiv 2506.02959)
- [Why AI-Generated Text Detection Fails: Evidence from Explainable AI](https://arxiv.org/pdf/2603.23146) (arXiv 2603.23146)
- [Detecting the Machine: A Comprehensive Benchmark](https://arxiv.org/abs/2603.17522) (arXiv 2603.17522)
- [RAID: A Shared Benchmark for Robust Evaluation](https://arxiv.org/abs/2405.07940) (arXiv 2405.07940)
- [EditLens: Quantifying the Extent of AI Editing in Text](https://arxiv.org/abs/2510.03154) (arXiv 2510.03154), ICLR 2026
- [Span-level Detection of AI-generated Scientific Text](https://arxiv.org/pdf/2510.00890) (arXiv 2510.00890)

**Vendor documentation (used for API shape and pricing only)**
- [Pangram AI Detection API reference](https://docs.pangram.com/api-reference/ai-detection)
- [Pangram v3 API migration guide](https://www.pangram.com/blog/v3-api-migration-guide) — the `ai_assistance_score` interpretation warning
- [Pangram pricing](https://www.pangram.com/pricing) · [Pangram API](https://www.pangram.com/solutions/api)
- [Pangram technical report](https://arxiv.org/html/2402.14873v3) (arXiv 2402.14873)
- [GPTZero API](https://gptzero.stoplight.io/docs/gptzero-api/5bf295g49gwxp-gpt-zero-api)

**First-hand accounts**
- Michael G. Wagner, ["Pangram and the All-Clear"](https://www.theaugmentededucator.com/p/pangram-and-the-all-clear), *The Augmented Educator*
