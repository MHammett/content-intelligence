# Content Intelligence

Content Intelligence is a [uv](https://docs.astral.sh/uv/) workspace of tools for
producing and reviewing written content. Code lives under `packages/`, one package
per capability (see [docs/NAMING.md](docs/NAMING.md) for the naming convention):

- **ci-core** (`ci_core`) — the shared foundation both application packages build on.
  It owns the LLM layer (`ci_core.llm`: one streaming call path to all six
  providers via litellm, robust JSON extraction, token and cost accounting, the
  sliding-scale timeout model, and the model registry), outbound HTTP identity
  (`ci_core.http`), response-body
  text extraction (`ci_core.extract`), secret redaction
  (`ci_core.redact`), and the shared config helpers (`ci_core.config_helpers`). The
  provider/model reference data those read — `pricing.yaml`, `timeouts.yaml`,
  `model_registry.yaml` — lives in `ci_core/configs/` alongside its loaders.
  Dependencies flow one way, and the applications do not depend on each other; see
  [docs/NAMING.md](docs/NAMING.md#dependency-direction).

  It also carries settings (`ci_core.config`), SQLAlchemy persistence (`ci_core.db`,
  `ci_core.models`) and structured logging (`ci_core.logging`), driven by `alembic/`.
  Those are implemented and tested but have no production consumers yet — nothing
  outside ci-core's own tests and `alembic/env.py` imports them. Because of that
  they live behind an optional extra rather than being installed for everyone:

  ```powershell
  uv sync --extra persistence
  ```

  A normal `uv sync` skips them, so a plain install no longer pulls an async
  PostgreSQL driver (`asyncpg`, which builds a C extension) or an ASGI web
  framework (`starlette`) for a command-line tool. Their tests skip cleanly when
  the extra is absent; the dev dependency group installs it, so CI coverage is
  unchanged.
- **ci-article-review** (`ci_article_review`) — the article-review pipeline: runs a
  drafted or already-published article through grammar correction and ensemble
  multi-model AI review, then publishes to WordPress on approval. The mature package.
- **ci-style-profile** (`ci_style_profile`) — style-profile bootstrapping: analyzes a writing
  corpus across multiple sources and synthesizes a structured style profile for `publication.yaml`.
  Early-stage.

The rest of this README covers **ci-article-review**, the primary tool today.

---

## Article Review

A multi-pass automated review pipeline for published web content. Takes a drafted article through deterministic grammar correction and ensemble multi-model AI review, consolidates weighted feedback into a single prioritized report, and publishes to WordPress when you approve.

At `standard` thoroughness (one model per domain), typical cost is under $1.00 per article. At `thorough` or `maximum` thoroughness with all optional providers configured, expect $2–5 per run. Grammar correction (optional — skip if you already do a manual pass yourself) can be free by self-hosting LanguageTool, or run through their hosted API starting around $40/month — see [docs/PROVIDERS.md](docs/PROVIDERS.md#languagetool-optional) for both options.

---

## Requirements

- Python 3.10 or higher — https://www.python.org/downloads/
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- Git — https://git-scm.com/downloads

---

## Installation

All commands run in **PowerShell** on Windows. macOS/Linux: use forward slashes.

```powershell
git clone https://github.com/MHammett/content-intelligence.git
cd content-intelligence
uv sync
```

`uv sync` installs all dependencies (including dev tools) into a local `.venv/` — no `pip install` or manual venv activation needed.

---

## Quick start

**1. Run setup:**

```powershell
uv run ci-setup
```

This creates `configs/`, copies the example templates, prompts for your publication name, and prints exactly what to fill in next. Run it with `--publication NAME` to skip the interactive prompt:

```powershell
uv run ci-setup --publication dnacom
```

**2. Fill in `configs/user.yaml`** with your API keys and model selection. See [docs/PROVIDERS.md](docs/PROVIDERS.md) for account setup instructions per provider. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full config reference.

**3. Fill in `configs/your_publication_name.yaml`** with your style profile, audience description, and WordPress credentials.

**4. Verify your setup:**

```powershell
uv run ci-check --publication your_publication_name
```

Makes one minimal call to each configured service and reports pass/fail with specific error messages before you run a real article through.

If a review ever seems to be using the wrong account or org, run `ci-check --publication your_publication_name --show-keys` before anything else. It prints a masked view of every configured key (`sk-proj-xV...xkA` style) and where each one actually came from. Credentials resolve through four tiers, most to least specific: a `--api-key` CLI override, then a publication config's own `api_keys` section, then `user.yaml`/`.env`, then a bare OS environment variable as the last resort — a `.env`-defined value always wins over a same-named OS variable, not the reverse, so editing `.env` always takes effect regardless of what else is set in your shell. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#api-key-precedence) for the full precedence and how to override it per publication or per run. The two sources disagreeing still prints an unprompted `NOTE` on every `ci-check`/`ci-review` run, no flag required — informational now, not a warning that something is broken.

**Check for newer models** (optional, run any time):

```powershell
uv run ci-discover
```

Queries each provider's live models API and reports what's available — so you always know when a newer model exists without reading every provider's changelog. Compares against your configured models and flags anything newer.

Each sweep is cached to `.cache/model_discovery.json`, and **the run report reads
that cache**: after running `ci-discover` once, every review for the next 24
hours carries a *Model Currency* section naming any model newer than the one the
run actually used, with no extra network calls. To refresh the cache during the
run instead of relying on a manual sweep, set `live_model_check: true` under
`pipeline:` in `configs/user.yaml`.

Both paths are advisory and neither can fail a run: a provider that cannot be
reached is reported as unchecked rather than as "you're up to date", and
`--offline` never queries at all. Newer is not necessarily better or cheaper —
the report says what shipped and whether `pricing.yaml` knows its rate, and
leaves the choice to you.

**5. Run the pipeline:**

```powershell
uv run ci-review --draft path/to/handoff.md --publication your_publication_name
```

`ci-setup` copies `handoff_templates/draft_submission.template.md` into your
working directory. Fill it in and pass it as the `--draft` argument.

A complete worked example — a real published article with every section filled
out — ships alongside it at
`handoff_templates/examples/draft_submission.filled-example.md`. Read it to see
what a good PRE-DRAFT ANALYSIS looks like; do not edit it as your starting
point. It's also ~72,000 characters, so a `maximum`-preset run against it costs
$3-5 — fine for a real submission, wasteful for "did this code change work."

For routine dev-loop testing — "did this code change work" — use
`handoff_templates/examples/draft_submission.short-example.md` instead: a
~1,000-word fixture, still with real citations, a genuine argument structure,
and one deliberately planted overreach so a healthy run has something to
catch. A `--cost-preset economy` run against it costs about $0.03-0.05,
live-verified end to end (fact_check, voice_style, completeness,
argument_integrity, and red_team all produced real findings, including
catching the planted overreach).

For smoke-testing a pipeline *code* change instead of a draft, use
`handoff_templates/examples/draft_submission.short-example.full-coverage.md` —
a similarly short fixture deliberately engineered to touch more of the
pipeline's edges in one pass: a mix of resolving, dead, and mismatched
citations, a claim that needs a live web search, an AI-speak passage several
models should flag in consensus, and a vague SEO heading.

**6. Publish an approved draft:**

```powershell
uv run ci-review --publish path/to/publication_handoff.md --publication your_publication_name
```

Always saves as a WordPress draft unless you add `--publish-live`.

**Analyze an existing web page:**

```powershell
uv run ci-review --url https://example.com/some-published-post --publication your_publication_name
```

Instead of a local handoff file, this fetches the page, extracts the main
article text (stripping nav/header/footer boilerplate), and runs the same
multi-model review on it — useful for an in-depth analysis of your own published
articles or a third-party piece.

Main-content extraction uses [`trafilatura`](https://trafilatura.readthedocs.io/),
which is installed as a standard dependency (citation verification depends on
extraction quality, so it is no longer optional). A built-in heuristic extractor
remains as a fallback. URL mode only fetches
public hosts (an SSRF guard blocks private/loopback/cloud-metadata addresses)
and infers only the title and body — it cannot supply an author's
`primary_claim`, target audience, or pre-draft analysis (see
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#url-input-mode)).

---

## The revision loop

The pipeline is built for a round trip between this tool and whatever chat model you drafted with. Each run writes two files side by side in `pipeline_history/<article-slug>/`:

- `run_N_<timestamp>_report.json` — the full machine-readable report
- `run_N_<timestamp>_review.md` — the same findings rendered as readable prose, SECTION 1 through SECTION 9

The markdown one is the artifact you actually work from. The end of every run prints its path:

```
Readable review (paste into chat): pipeline_history/my-article/run_1_20260809_143022_review.md
```

**The loop:**

1. **Run the pipeline** on your draft handoff (`--draft`) or a plain draft file (`--raw-draft`).
2. **Open the `review.md`.** Read it directly, or hand it to a model.
3. **Paste `handoff_templates/revise_after_review_prompt.md` into the same chat thread** that has the article's context, followed by the review's SECTION 1 through SECTION 9 content, plus the SEO blocks at the end of the file. Include SECTION 9 — it is long and easy to skip, but its "Read, and does NOT support the claim" block (sources read and found *not* to support the claim they were cited for) is among the most actionable findings a run produces, and it appears nowhere in SECTIONS 1-8. The section opens with the fraction of claims actually checked against a fetched document, which is the number to read first. That prompt has the model revise the draft *and* regenerate the metadata in one pass — so PRIMARY CLAIM, UNCERTAIN SECTIONS, KNOWN GAPS and the rest don't silently go stale against the revised text. Those sections are fed straight to the review models, so a stale KNOWN GAPS entry gets re-flagged on every subsequent run.
4. **Save what comes back** as two files: the revised draft, and the metadata block (the format matches `handoff_templates/metadata_only.md`).
5. **Re-run:**

   ```powershell
   uv run ci-review --raw-draft revised_draft.md --metadata metadata.md --publication your_publication_name
   ```

   The run number increments and the report gains a **Delta From Prior Run** block — word change, how many prior consensus flags you resolved, how many new ones appeared, and whether the primary claim or heading structure moved.

6. Repeat until the findings are ones you're content to ship, then publish with `--publish`.

`--raw-draft` on its own works too — it just uses the whole file as the article body and skips the metadata context, which means the review models lose the author-supplied framing. Pair it with `--metadata` whenever you have that context to give.

---

## Cross-run analytics

A single run tells you about one article. `ci-history-report` reads every `run_*_report.json` under `pipeline_history/` and reports what only shows up across many runs:

```powershell
uv run ci-history-report
```

| Flag | Purpose |
|---|---|
| `--history-root DIR` | Directory containing per-article run history (default `pipeline_history`) |
| `--article SLUG` | Scope to one article — the `pipeline_history/` subdirectory name, not the article title |
| `--recent-window N` | How many of the most recent calls/runs count as "recent" vs. baseline (default 5) |
| `--json` | Print the raw result as JSON instead of the console summary |
| `--verbose`, `-v` | DEBUG logging |

What it reports:

- **Provider reliability** — per-provider success rate over the most recent calls versus the historical baseline, with a `DEGRADED` flag when the recent failure rate is at least 40 points worse. This is the check that catches an expired API key failing across every domain, instead of noticing after several bad runs. Providers need at least 3 baseline calls before a comparison is made.
- **Cost** — total spend, average per run, and recent-versus-baseline direction (`increasing` / `decreasing` / `flat`, at a 15% relative threshold). Direction only — cost has no "better" way to editorialize.
- **Quality trends** — Flesch-Kincaid grade, SEO issue count, and broken link count, both globally (recent vs. baseline average) and per article across its own revision history (first run vs. latest, as `improved` / `worsened` / `unchanged`).

- **Per-pass contribution** — for each `model:domain` pass: how often it ran, what it cost, how many of its findings reached consensus, and how many of those *only* it raised. This is the data for deciding whether every call in a `maximum` run earns its keep. Read it to form a hypothesis and confirm with `--only-model` / `--only-domain`; a pass with many sole-source findings scores badly on cost-per-hit and may still be the most valuable one you have.

It reads `pipeline_history/` fresh every time — no database, no index. Reports missing a field are treated as "not enough history" rather than an error, since the report schema has grown over time.

### Recurring voice patterns

The review pipeline flags AI-speak in `section_3_voice` fresh on every run, so the same tic gets caught and re-litigated article after article. `ci-voice-patterns` reads the same `pipeline_history/` files and reports the patterns that keep recurring — the ones that have earned a permanent `style_rules.banned_words` / `banned_phrases` entry instead:

```powershell
uv run ci-voice-patterns --publication NAME --config configs/NAME.yaml
```

| Flag | Purpose |
|---|---|
| `--history-root DIR` | Directory containing per-article run history (default `pipeline_history`) |
| `--publication NAME` | Scope to reports whose `publication` field matches this value |
| `--config PATH` | Publication config YAML to read existing `style_rules.banned_words`/`banned_phrases` from, so already-banned patterns are excluded (read-only, never modified) |
| `--min-articles N` | Minimum distinct articles a pattern must appear in to be reported (default 3) |
| `--similarity-threshold N` | Normalized-text similarity ratio (0-1) for two findings to count as the same pattern (default 0.82) |
| `--json` | Print the raw result as JSON instead of the console summary |
| `--verbose`, `-v` | DEBUG logging |

What it reports:

- **Recurring flagged passages** — candidate `banned_phrases` entries: the actual passage text that keeps getting flagged across articles.
- **Recurring voice problems** — the same critique showing up repeatedly, which points at a pattern worth reviewing even when the wording differs each time.

Both are clustered with a normalized-text `difflib` similarity check rather than any NLP/ML clustering — the goal is surfacing obvious repeats, not perfect semantic dedup. A pattern must appear in at least `--min-articles` *distinct* articles to qualify, so one model flagging the same phrase twice in a single draft isn't cross-article evidence.

It is a suggestion report only. Nothing is written to `pipeline_history/` or to any publication config — a human decides whether each candidate actually becomes a rule.

---

## Command-line options

```powershell
uv run ci-review --draft HANDOFF --publication NAME [options]
```

Exactly one of `--draft`, `--raw-draft`, `--url`, or `--publish` is required — they are mutually exclusive.

| Flag | Purpose |
|---|---|
| `--draft PATH` | Run the review pipeline on a draft handoff document |
| `--raw-draft PATH` | Run on a plain article file with no handoff headers (e.g. pasted straight out of a chat session) — the whole file becomes the article body |
| `--url URL` | Fetch a published web page and run the review on its extracted content |
| `--publish PATH` | Publish an approved publication handoff (saves as draft unless `--publish-live`) |
| `--metadata PATH` | Metadata-only file (PRIMARY CLAIM, TARGET AUDIENCE, etc., no DRAFT section) to pair with `--raw-draft`. Requires `--raw-draft`. |
| `--publication NAME` | Publication config to use (`configs/NAME.yaml`) — **required** |
| `--publish-live` | Publish live instead of as a WordPress draft |
| `--config-dir DIR` | Config directory (default `configs`) |
| `--cost-preset PRESET` | Override `cost_preset` for this run: `economy` / `standard` / `balanced` / `thorough` / `maximum`. Doesn't modify `user.yaml`. |
| `--api-key PROVIDER[.FIELD]=VALUE` | Override one credential field for this run only — highest tier of the credential precedence (CLI > publication config > `.env`/`user.yaml` > OS environment variable). Repeatable. `PROVIDER=VALUE` is shorthand for `api_key` (`openai`, `gemini`, `mistral`, `grok`, `perplexity`, `claude`); multi-field credentials need `PROVIDER.FIELD` (`languagetool.username`, `archive_org.secret_key`, etc.). See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#api-key-precedence). |
| `--wp-user USERNAME` | Override the WordPress username for this run only (`--publish` mode) — same precedence idea as `--api-key`, applied to `publication.wordpress`. |
| `--wp-password APPLICATION_PASSWORD` | Override the WordPress application password for this run only (`--publish` mode). |
| `--no-seo-suggestions` | Skip both SEO model calls for this run — the [metadata suggestions](docs/CONFIGURATION.md#seo-suggestions) (focus keyword candidates, meta description, OG title, OG description, schema type) and the [structure review](docs/CONFIGURATION.md#seo-structure-review). Deterministic on-page checks still run. Permanent off: `seo_rules.suggestions` / `seo_rules.content_review`. |
| `--retry-failed RESULTS_JSON` | Fill in the gaps from a prior run: make model calls only for the (model, domain) pairs marked failed in a `run_N_results.json`, merging the new attempts onto everything that already succeeded. Requires the same draft-loading flags (`--draft`/`--url`/`--raw-draft`) as the original run. Mutually exclusive with `--replay`, which makes no model calls at all. |
| `--verbose`, `-v` | DEBUG logging |

**Calibration flags** (for measuring/tuning timeouts — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md#timeouts-are-automatic-sliding-scale)):

| Flag | Purpose |
|---|---|
| `--no-timeout` | Disable timeout truncation so true completion times are measured, never cut off |
| `--only-model PROVIDER` | Run only one provider (e.g. `openai`) instead of the full ensemble |
| `--only-domain DOMAIN` | Run only one domain (`fact_check`, `voice_style`, `completeness`, `argument_integrity`, `red_team`) |
| `--replay RESULTS_JSON` | Replay a captured ensemble instead of calling any models — free |
| `--offline` | Skip every pass that reaches the network (link validation, Wayback, citation resolution) |

### Iterating on the code without paying for the ensemble

The ensemble is nearly all of a run's cost, and most changes don't touch it: of the 25 PRs merged to 2026-08-15, **15 touched no live-LLM code at all**. Every run now writes a `run_N_<ts>_results.json` beside its report holding the raw ensemble output, so that run can be replayed for free:

```powershell
uv run ci-review --draft handoff.md --publication mypub --replay pipeline_history/<article>/run_16_20260815_140635_results.json --offline
```

That re-runs consolidation, claim collection, report building, markdown rendering and the history save over real captured model output, with **no model calls and no network**. It's the fast loop for anything in `consolidation.py`, `report_markdown.py`, `history.py`, `config_loader.py` or the analysis passes.

`--offline` on its own (without `--replay`) still calls the models but skips the network passes. `--replay` on its own replays the ensemble but still resolves citations and checks links, which is what you want when changing the citation adapters themselves.

Example — measure one model/domain's true latency cheaply:

```powershell
uv run ci-review --draft handoff.md --publication mypub --cost-preset maximum --only-model openai --only-domain fact_check --no-timeout
```

> On Windows `cmd.exe`, keep the whole command on one line — backslash line-continuation is a bash feature and will split the command.

---

## Running the tests

```powershell
uv run pytest packages/
```

Around 1,560 tests in roughly 20 seconds, all external API calls mocked — no
keys required.

### The `slow` marker

One test is inherently wall-clock-bound: `test_the_executor_form_really_did_hang`
proves that a `concurrent.futures` atexit join really does hold the interpreter
open after a run finishes, and the only way to show a hang is to wait one out
(~5s). It is marked `slow`.

**`slow` tests are not skipped by default** — a green `uv run pytest packages/`
means the whole suite passed, with nothing quietly sitting out. Deselect them
only when you want a tighter inner loop:

```powershell
uv run pytest packages/ -m "not slow"
```

To run just those tests:

```powershell
uv run pytest packages/ -m slow
```

The marker is registered in the root `pyproject.toml` (and in
`packages/ci-core/pyproject.toml`, which is the config pytest reads when that
package's tests are run on their own). Reach for it sparingly: almost every
slow test this suite has had was slow by accident — a real `time.sleep`, an
unstubbed network call, or a per-test fixture doing shared work — and those
should be fixed, not marked.

---

## Marking claims out of scope for fact-checking

Some claims have no external source and never will. Sending them to the fact-check
pass and to citation resolution cannot produce a finding — only one of two wrong
answers.

Both were measured on real runs, 2026-09-05. `"I have a side job."` was resolved
against a company team page, came back `not_addressed`, and every provider then
advised **withdrawing a true first-person statement**. In the other direction,
`"Divide by seven and you get 1,024. Exactly."` came back `confirmed` with the
source `"Manual Calculation"` — the model did the arithmetic and reported the
result as an established fact.

### How to mark a passage

**In the draft**, wrap it in HTML comments. This is the only method that works with
`--raw-draft`, which carries no metadata at all, and the markers are invisible to a
reader once published:

```
<!-- ci:no-verify: only I can confirm this -->
I have a day job running network infrastructure. I have a side job. I have a family.
<!-- /ci:no-verify -->
```

The reason after the colon is optional. A marker that is never closed is warned about
and **ignored** — excluding more than you meant is invisible in the report, while
excluding nothing is obvious the moment the claim turns up fact-checked, so an
ambiguous marker fails toward checking.

**In the handoff**, list them under `OUT OF SCOPE FOR FACT-CHECK`, one per line:

```
OUT OF SCOPE FOR FACT-CHECK
- I have a side job — only I can confirm this
- The 7,168-day figure is arithmetic from two dates already in the piece
```

**In the publication config**, for passages that recur across every article:

```yaml
fact_check_scope:
  exclude_passages:
    - passage: "This site is a real expense, and there's no way to recoup it"
      reason: "standing statement about the author's own finances"
```

### What the models decide on their own

The fact-check pass classifies claims itself, into one of six categories:
`first_person`, `author_hypothesis`, `internal_arithmetic`, `subjective_judgment`,
`future_prediction`, `other`. The models already made this distinction unprompted —
one described a claim as "an author's hypothesis about the internal logic, not
[verifiable]" while filing it as a finding anyway — so the output shape now has
somewhere to put it.

**An author marking outranks a model classification, and the author's categories
bound what a model may rule out alone.** `exclude_types` is the list this
publication accepts on a model's say-so; it defaults to the five specific
categories and **not** `other`, so a model reaching for the catch-all is reported
rather than obeyed. Turn model classification off entirely with
`trust_model_classification: false`:

```yaml
fact_check_scope:
  exclude_types: [first_person, internal_arithmetic]
  trust_model_classification: true
```

### What exclusion actually does

| Pass | Effect |
|---|---|
| `fact_check` verdicts | The claim moves to an `out_of_scope` bucket. It is not in `confirmed`, `unverifiable`, or any other. |
| Section 9 citation resolution | The claim never enters the list. No source is fetched for it. |
| `red_team`, `argument_integrity`, `voice_style`, `completeness` | **Unchanged.** Every pass still sees the whole draft. |

That last row is the point. A first-person claim can still be an argumentative
weakness or a credibility risk, and hiding it from every reviewer to spare it from
one would cost more findings than it saves.

**Nothing disappears.** Section 2 gets an *Out of scope for verification* block
listing every excluded claim, who decided it, the category, and the reason. Where an
exclusion overrode a verdict a model had already reached, that verdict is printed
too — "the author marked this out of scope, and openai had it as `confirmed` citing
<url>" — because that disagreement is the author's to judge. Section 9 opens by
saying how many claims never entered resolution, so its total does not silently
shrink.

## Project structure

The repository is a [uv](https://docs.astral.sh/uv/) workspace. Code lives under
`packages/`, one package per tool, each with its own `pyproject.toml`, `src/`
layout, and `tests/`. The article-review pipeline is the mature package; the
others are early-stage.

```
content-intelligence/
├── pyproject.toml                workspace root — [tool.uv.workspace] members, dev deps, mypy config
├── uv.lock                       resolved lockfile for the whole workspace
├── Makefile                      common dev tasks
├── requirements.txt              runtime dependencies
├── requirements-dev.txt          adds pytest and other dev tooling
├── .env.example                  documents all supported environment variables
├── .pre-commit-config.yaml       ruff + mypy pre-commit hooks
├── alembic.ini                   Alembic config for the ci-core database schema
├── alembic/                      migrations for ci_core.models (not used at runtime yet)
├── .github/                      CI workflows
│
├── packages/
│   ├── ci-article-review/        the article-review pipeline (the bulk of the code)
│   │   ├── pyproject.toml
│   │   ├── src/ci_article_review/
│   │   │   ├── pipeline.py            orchestration engine — start here
│   │   │   ├── setup.py               first-run scaffolding for configs/
│   │   │   ├── config_loader.py       config parsing and validation
│   │   │   ├── consolidation.py       weighted ensemble consolidation → one report
│   │   │   ├── ensemble_capture.py    saves/loads raw ensemble output for --replay
│   │   │   ├── fact_check_scope.py    claims no source can settle — author markings,
│   │   │   │                          model classification, and what they exclude
│   │   │   ├── handoff_parser.py      parses Template A and Template C documents
│   │   │   ├── history.py             saves run artifacts to pipeline_history/
│   │   │   ├── history_analytics.py   cross-run analytics over pipeline_history/ (ci-history-report)
│   │   │   ├── voice_pattern_report.py  recurring voice patterns across articles (ci-voice-patterns)
│   │   │   ├── report_markdown.py     renders the readable run_N_*_review.md from the report
│   │   │   ├── check.py               connectivity/credential check for all services
│   │   │   ├── discover.py            live model discovery — queries provider APIs
│   │   │   ├── live_model_check.py    caches that discovery and reports, in the run
│   │   │   │                          report, when a model newer than the one used shipped
│   │   │   ├── probe.py               lightweight provider reachability probe
│   │   │   │                          (the provider adapters, timeout model, model
│   │   │   │                          registry, cost, and redaction live in ci-core)
│   │   │   │
│   │   │   ├── adapters/
│   │   │   │   ├── grammar/languagetool.py   grammar correction (Pass 1)
│   │   │   │   ├── cms/wordpress.py          WordPress REST API publisher
│   │   │   │   └── citation/
│   │   │   │       ├── resolver.py           primary source resolution, checksums, confidence tiers
│   │   │   │       ├── draft_citations.py    traces a claim to the citation the draft cites for it
│   │   │   │       ├── wayback.py            Wayback archive check + Save Page Now submission
│   │   │   │       ├── topic_match.py        keyword gating for pointer-only adapters
│   │   │   │       └── sources/              10 adapters: census, crossref, eia, epa, ferc,
│   │   │   │                                 fhwa, fred, icc, ilga, pjm
│   │   │   │
│   │   │   ├── analysis/
│   │   │   │   ├── readability.py     Flesch-Kincaid grade, word count, sentence stats
│   │   │   │   ├── links.py           URL extraction, HTTP status check, Wayback archive check
│   │   │   │   ├── seo.py             title length, heading structure, meta description
│   │   │   │   ├── seo_suggest.py     proposes the whole SEO METADATA block (advisory)
│   │   │   │   ├── seo_content.py     structure review from a search reader's side
│   │   │   │   └── webpage.py         webpage fetch/extraction helpers
│   │   │   │
│   │   │   ├── prompts/               system prompts for each review domain
│   │   │   ├── schemas.py             JSON schemas mirroring each prompt's RETURN
│   │   │   │                          FORMAT, so providers enforce the shape
│   │   │   ├── configs/               committed defaults: presets.yaml + *.example.yaml
│   │   │   │                          (real user.yaml + publication.yaml are gitignored;
│   │   │   │                          pricing/timeouts/model_registry live in ci-core)
│   │   │   └── handoff_templates/     fill these out to submit drafts and publish
│   │   └── tests/                     pipeline test suite, all external calls mocked
│   │
│   ├── ci-core/                  the shared foundation both applications import
│   │   ├── pyproject.toml
│   │   ├── src/ci_core/
│   │   │   ├── llm/
│   │   │   │   ├── client.py          the litellm shim: five providers through
│   │   │   │   │                      completion(), OpenAI through responses(),
│   │   │   │   │                      all streaming under a first-byte allowance
│   │   │   │   │                      plus an independent stall detector
│   │   │   │   ├── cache.py           marks the cacheable prefix in each provider's
│   │   │   │   │                      own terms (Anthropic caches nothing without it)
│   │   │   │   ├── schema.py          puts a caller's JSON schema on the wire in each
│   │   │   │   │                      provider's own spelling
│   │   │   │   ├── json_utils.py      robust JSON extraction (fences, think-preambles,
│   │   │   │   │                      truncation salvage)
│   │   │   │   ├── tokens.py          normalizes each provider's usage shape to
│   │   │   │   │                      {prompt, completion, cached}
│   │   │   │   ├── cost.py            token-based cost estimation, incl. cache hits
│   │   │   │   ├── timeout_model.py   sliding-scale timeout from size × model × effort
│   │   │   │   └── model_registry.py  current/superseded model detection
│   │   │   ├── extract.py        HTML/PDF -> readable text, claim-centred excerpts
│   │   │   ├── http.py           USER_AGENT + DEFAULT_HEADERS for all outbound calls
│   │   │   ├── concurrency.py    run_with_timeout — the wall-clock backstop both
│   │   │   │                     applications run their provider calls under
│   │   │   ├── redact.py         scrubs API keys from error output before logging;
│   │   │   │                     mask_secret() masks a key for display (ci-check --show-keys)
│   │   │   ├── env_provenance.py detects a pre-existing OS env var silently shadowing
│   │   │   │                     a .env value (python-dotenv's override=False)
│   │   │   ├── console.py        force_utf8_stdio — every CLI calls it first, so a
│   │   │   │                     cp1252 Windows console cannot kill a report mid-print
│   │   │   ├── config_helpers.py load_yaml, resolve_env_recursive, normalize_model_configs
│   │   │   ├── configs/          pricing.yaml, timeouts.yaml, model_registry.yaml
│   │   │   ├── config.py         pydantic settings (no production consumer yet)
│   │   │   ├── db.py             async SQLAlchemy engine/session (no production consumer yet)
│   │   │   ├── models.py         ORM models (no production consumer yet)
│   │   │   └── logging.py        structured logging (no production consumer yet)
│   │   └── tests/
│   │
│   └── ci-style-profile/         style-profile bootstrapping (see PLAN.md)
│       ├── pyproject.toml
│       ├── sources.example.yaml  copy to src/ci_style_profile/sources.yaml
│       ├── configs/presets.yaml
│       ├── src/ci_style_profile/
│       │   ├── bootstrap.py      entry point (style-profile-bootstrap)
│       │   ├── collectors/       wordpress, gmail, outlook365, twitter, textfiles, custom/
│       │   ├── detect.py         style detection pass
│       │   ├── synthesize.py     profile synthesis
│       │   ├── style_consolidation.py
│       │   ├── normalize.py, callers.py, output.py, logging_config.py
│       │   ├── staging/          cached source text (gitignored)
│       │   └── profiles/         timestamped profile snapshots (gitignored)
│       └── tests/
│
├── pipeline_history/             run reports, readable reviews, and daily pipeline logs
│                                 (gitignored, local only)
└── docs/                         extended documentation
    ├── ARCHITECTURE.md           pass structure, data flow, design rationale
    ├── PROVIDERS.md              account setup for every service
    ├── CONFIGURATION.md          full config reference, thoroughness, ensemble weights
    ├── CITATIONS.md              Section 9 confidence tiers and archiving behavior
    ├── NAMING.md                 package/module naming convention
    ├── TERMINOLOGY.md            "voice" vs. "style" and other term definitions
    └── TROUBLESHOOTING.md        error messages and fixes
```

---

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — How the pipeline works as a whole: the pass structure, what flows between passes, why consolidation is weighted the way it is, the review→revise→re-run loop end to end, and the extension points. Start here to understand the design rather than the configuration.

- **[docs/PROVIDERS.md](docs/PROVIDERS.md)** — Account setup and API keys for every service: OpenAI, Gemini (AI Studio + Vertex AI), Mistral, Perplexity, Grok, Claude, LanguageTool, WordPress.

- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — Full `user.yaml` reference: model config (simple and extended forms), web search, Vertex AI, Azure, thoroughness levels, ensemble weighting.

- **[docs/CITATIONS.md](docs/CITATIONS.md)** — How Section 9 resolves claims to primary sources: the three confidence tiers (verified / pointer-only / unresolved), what each one does and doesn't prove, and the Wayback Machine archiving behavior.

- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — Error messages and fixes for every service, plus pipeline behavior edge cases.

- **[docs/NAMING.md](docs/NAMING.md)** / **[docs/TERMINOLOGY.md](docs/TERMINOLOGY.md)** — Package naming convention, and the deliberate "voice" vs. "style" distinction.
