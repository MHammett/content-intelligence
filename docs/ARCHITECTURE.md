# Architecture

How the article-review pipeline works as a whole: the passes, what flows between
them, and why the design is shaped this way. The other documents are reference
material — this one is the map.

- [CONFIGURATION.md](CONFIGURATION.md) — what every config key does
- [PROVIDERS.md](PROVIDERS.md) — account setup per service
- [CITATIONS.md](CITATIONS.md) — Section 9 confidence tiers in depth
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — error messages and fixes

---

## The shape of the thing

The pipeline exists to answer one question about a draft: **what would a careful
adversarial reader find wrong with this before it ships?** Everything else
follows from that.

It answers it by asking several models the same question from different angles,
weighting their answers by how well-suited each model is to each angle, and
reporting only what survives. A single model asked "review this article" produces
a plausible-sounding list. Five models asked five specific questions, with the
overlap treated as signal, produces something you can act on.

```
handoff document (or --url / --raw-draft)
  │
  ├─ Pass 1   grammar          LanguageTool, deterministic, optional
  │
  ├─ pre-analysis              readability · links · SEO · SEO suggestions
  │                            no ensemble calls; cheap and always runs
  │
  ├─ Pass 2   ensemble review  N models × 5 domains, in parallel
  │                            THE expensive step: ~94% of wall time and cost
  │
  ├─ consolidation             weighted merge → sections 1-8
  │                            consensus detection, contradiction detection
  │
  ├─ Pass 3   citations        claims from Pass 2 → primary sources → Section 9
  │                            fetch · checksum · verify relevance · archive
  │
  └─ output                    run_N_*_report.json  +  run_N_*_review.md
                               written to pipeline_history/<article-slug>/
```

---

## Pass 1 — grammar

LanguageTool, if credentials are configured. Deterministic, so it runs before
anything a model sees: the review models should spend their attention on
argument and evidence, not on a missing comma.

Its output feeds forward in two ways. The corrected text becomes the draft every
later pass reads, and the passages it flagged become a **partial vote** in
consensus detection (`ensemble.lt_weight`, default 0.5) — if a model and a
deterministic grammar checker independently flag the same sentence, that
agreement means something.

Skipped cleanly when unconfigured. The report says so rather than implying a
clean grammar result.

---

## Pre-analysis

Everything computable without a model call: readability (Flesch-Kincaid, word
and sentence counts), link validation (HTTP status, redirects, Wayback
availability), and SEO structure. Plus two cheap `mistral-small` calls for SEO
metadata suggestions and the search-reader structure review.

This runs before Pass 2 so its results are available to the report even if every
ensemble call fails.

---

## Pass 2 — the ensemble

The expensive step, and the one the rest of the design is arranged around.

**Five domains**, each with its own system prompt in `prompts/`:

| Domain | Question it asks |
|---|---|
| `fact_check` | Are the specific figures, dates and attributions correct? |
| `voice_style` | Does this read as AI-written or off-voice? |
| `completeness` | What is missing that this audience needs? |
| `argument_integrity` | Do the inferences actually hold? |
| `red_team` | How would a hostile critic attack this? |

Publications can add their own via `custom_domains`.

**Which models run which domains** comes from the thoroughness preset —
`standard` (one model per domain), `thorough` (two or three), `maximum` (all
models on all domains). `_build_assignments` then drops anything disabled or
lacking credentials, so a partial configuration degrades to a smaller ensemble
rather than failing.

**Every call is independent and runs in parallel.** This matters for the cost
model: wall time for the whole pass equals its *slowest single call*, not the
sum. Adding models costs money, not time. Removing them saves money, not time.

**Two timeout layers**, which are easy to conflate:

- The adapters stream (SSE), so the socket timeout is a small constant
  *inter-token gap* (`ci_core.llm.streaming`). This catches "the model stopped
  sending" in ~120s without capping how long a legitimate long generation may
  run.
- A per-task **wall-clock backstop** (`_run_with_timeout`) bounds the whole call,
  sized from draft length × model × reasoning effort by
  `ci_core.llm.timeout_model`. A global ceiling above the slowest of those
  catches anything that escapes both.

A timed-out or failed call becomes a synthetic failure result rather than an
exception, so one dead provider degrades the ensemble instead of killing the run.

---

## Consolidation — where the ensemble becomes a report

`consolidation.py` merges the per-model results into sections 1–8. Two mechanisms
carry most of the value.

**Weighted consensus (Section 1).** Every (model, domain) pair has a weight
reflecting how suited that model is to that domain — search-grounded models score
higher on `fact_check`, for instance. Findings are keyed by a normalised passage
string, weights accumulate, and a passage crossing `consensus_threshold`
(default 2.0) becomes a consensus flag. LanguageTool contributes its partial vote
here. Optionally, a model's own stated `confidence` damps its contribution
(`ensemble.confidence_weights`, off by default — see
[CONFIGURATION.md](CONFIGURATION.md)).

The point is not that consensus is truth. It is that *independent agreement is
cheaper to check than a long list*, and Section 1 is the list you read first.

**Contradiction detection.** A claim one fact-check model marks `confirmed` and
another marks `outdated` or `contradicted` is surfaced explicitly. Two models
disagreeing about a fact is a stronger signal than either verdict alone, and
averaging it away would destroy exactly the information you want.

Everything below the consensus threshold still appears, attributed to the single
model that raised it. Nothing is discarded for lack of agreement — it is ranked
lower.

---

## Pass 3 — citations

Takes the claims Pass 2 produced and tries to trace each to a primary source.
This is the part the project's value rests on, so its honesty rules are strict
and are documented in full in [CITATIONS.md](CITATIONS.md).

The short version: a citation reaches the **verified** tier only when the source
was fetched, checksummed, *and* a model read the extracted text and affirmed it
supports the claim — with a supporting quote that was checked against the
document. Everything else lands in a weaker tier that says what it does and does
not establish. "We could not read it" is never reported as "the source does not
support this."

Resolution happens two ways, and the first is the general one:

1. **A URL the claim already names** — from the fact-check model's `source`
   field, or a provider's live-search citation. Fetched through the SSRF guard,
   extracted, checksummed, relevance-verified.
2. **A configured source adapter** — `citation_sources` in the publication
   config. All shipped adapters target US government data; most publications
   need none, and `citation_sources: []` is a normal configuration.

Resolved URLs are checked against the Wayback Machine and submitted for
archiving if absent, so a source that vanishes later was captured at the time it
was cited. Checksums are compared across runs to flag sources whose content
changed since they were last cited.

---

## Output, and the loop back in

Each run writes two files to `pipeline_history/<article-slug>/`:

- `run_N_<timestamp>_report.json` — the machine-readable report
- `run_N_<timestamp>_review.md` — the same findings as prose, Sections 1–9

**The markdown is the artifact you work from**, and the loop it belongs to is the
actual design intent:

```
draft ──▶ pipeline ──▶ review.md ──▶ chat model + revise prompt ──▶ revised draft
  ▲                                                                      │
  └──────────────────────────────────────────────────────────────────────┘
```

Three things about that loop are easy to miss:

**The metadata is regenerated with the draft, deliberately.** The revise prompt
rewrites `PRIMARY CLAIM`, `UNCERTAIN SECTIONS` and `KNOWN GAPS` alongside the
text. Those sections are fed straight to the review models, so a stale
`KNOWN GAPS` entry gets re-flagged every single run — the metadata has to move
with the draft or the review degrades.

**Run numbers are author-declared, not an execution counter.** `Pipeline run: N`
comes from the handoff document. Running the same handoff twice writes two
reports at the same N, which is why prior-run selection keys on actual execution
timestamp rather than `N - 1`.

**History is keyed by article title.** Change the title between runs and
continuity tracking silently starts over. The pipeline warns when a handoff
declares run > 1 but no earlier report is found.

**The delta block** compares against the previous run: word change, how many
prior consensus flags you resolved, how many new ones appeared, whether the
primary claim or heading structure moved. It answers "is this converging?"

---

## Cross-run analytics

A single run tells you about one article. `pipeline_history/` accumulates, and
two tools read it:

- **`ci-history-report`** — provider reliability (recent vs. baseline success
  rate, with a `DEGRADED` flag), cost trends, quality trends. This is what
  catches an expired API key failing across every domain.
- **`ci-voice-patterns`** — voice findings recurring across *different* articles,
  i.e. the ones that have earned a permanent `banned_phrases` entry instead of
  being re-litigated every run.

Both read the JSON fresh; no database, no index.

---

## Package boundaries

```
ci-core  ◄── ci-article-review
   ▲
   └─────── ci-style-profile
```

Dependencies flow one way and the applications never import each other
([NAMING.md](NAMING.md#dependency-direction)).

`ci-core` owns anything more than one application needs: the six streaming
provider adapters and their SSE handling, JSON extraction and truncation
salvage, cost estimation, the timeout model, the model registry, text
extraction, secret redaction, and SSRF-guarded HTTP.

That last one is worth stating as a rule, because getting it wrong caused a real
bug: **a shared safety primitive belongs in `ci-core`, not in whichever
application happened to need it first.** The SSRF guard originally lived in
`ci-article-review/analysis/links.py`, where `adapters/citation/` could not
import it without an import cycle — so the citation resolver fetched
model-supplied URLs with no validation while user-supplied URLs were checked.
The fix was to make the safe call the reachable one.

---

## Extension points

| To add… | Do this |
|---|---|
| A review domain | `custom_domains` in the publication config — inline prompt or `prompt_file`, plus which models run it |
| A citation source | A module in `adapters/citation/sources/` exposing `resolve(claim)`, registered in `ADAPTER_MAP` |
| A provider | An entry in `_PROVIDERS` in `ci_core/llm/client.py` (litellm route prefix, call surface, fallback chain, read-gap default), plus `pricing.yaml` and `cached_input` |
| A cost preset | `configs/presets.yaml` — the YAML is the single source of truth |

---

## What guards this design

Three tests exist specifically to keep the above true, and all three were
written in response to real failures.

**`test_pipeline_end_to_end.py`** runs the whole pipeline against stubs and
compares the report to a committed golden file. PR #43 refactored the LLM layer
and silently dropped three features because the suite tested units in isolation
while the refactor changed how they were *wired*. A wiring change now shows up as
a golden diff. Regenerate deliberately, and read the diff:

```bash
CI_REGENERATE_GOLDEN=1 uv run pytest packages/ci-article-review/tests/test_pipeline_end_to_end.py
```

**`test_docs_current.py`** fails when documentation drifts from the code — CLI
flags missing from the README, adapter counts, dead doc links, the module-vs-
console-script invocation form, and the same checks over strings the code
*prints*, since a wrong command in `ci-setup`'s output reaches users just as
directly as one in the README.

**`test_concurrency_doctrine.py`** fails when any shipped module uses a
`concurrent.futures` executor. `concurrent.futures.thread` registers
`_python_exit` through `threading._register_atexit`, and that hook joins every
worker with a bare, untimed `t.join()` — so a pool worker still inside a call
when the run ends blocks interpreter exit for exactly as long as that call keeps
running. Measured 2026-08-16: six `ci-review` processes still alive, two of them
two days after writing their report, each holding a file handle on its own log,
which is also what made `git worktree remove` fail.

That rationale was written down in `ci_core.concurrency` at the time, and it did
not hold. Three more `ThreadPoolExecutor` uses appeared in ci-style-profile and
one in ci-article-review, because a rule documented in one module's docstring is
only read by people already editing that module. All four were migrated on
2026-09-05 and the rule now runs in CI, with an allowlist (currently empty) as
the only escape hatch — adding an entry means writing the reason next to it.

Use `ci_core.concurrency` instead:

| Need | Use |
| --- | --- |
| One call, one wall-clock budget | `run_with_timeout` |
| N calls, all at once, per-call budgets + a group deadline | `run_all_with_timeout` |
| N calls, at most K running at once — the `ThreadPoolExecutor(max_workers=K)` replacement | `run_all_bounded` |

The difference that matters: `max_workers` caps how many threads *exist*, while
`run_all_bounded` gives every job its own daemon thread and caps how many are
*working*. A daemon parked on a semaphore cannot hold the interpreter open; a
pool worker parked inside a call is joined untimed at exit.
