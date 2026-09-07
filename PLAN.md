# Work plan — content-intelligence, 2026-08-12

Everything proposed in this session, in the order it should happen and with the
reason for the order.

The organising principle: **litellm supersedes about a third of what was written
today.** Anything that touches the provider adapter layer is held until the
migration decision resolves. Anything above or beside that layer lands now.

> **Status, 2026-08-16 (second update).** Sections 0a, 0c and all six PRs in
> section 1 have landed, and **section 3's migration is merged** (#103), which
> closes section 5's first decision entirely. **Section 0 is now clear** — 0b's
> root cause was found and its diagnostic gap closed, leaving only a config
> judgement. Per-item status is marked inline below in the same
> `~~struck~~ **Done (PR #n).**` style section 5 already used.
>
> Two bugs found while landing #103, both merged: `ci-review` could finish its
> work and never exit ([#104](https://github.com/MHammett/content-intelligence/pull/104)
> — an abandoned call held the interpreter open through `concurrent.futures`'
> atexit join; processes were found alive two days on), and any *partial*
> `pipeline:` block discarded every default
> ([#105](https://github.com/MHammett/content-intelligence/pull/105) —
> `task_timeout_seconds` became `None`, a `TypeError` before the first call).
>
> **Correction to a finding reported during #103:** `openai:completeness`
> hitting a 285s wall-clock backstop was written up as gpt-5.5 `xhigh` needing a
> `timeouts.yaml` calibration. It does not. That run's config set
> `task_timeout_seconds: 300`, clamping every budget to 285; `user.example.yaml`
> ships **1100**, which gives that cell **819s** against 231–283s observed and
> the 456s already measured under `--no-timeout`. No calibration is needed — the
> harness was wrong, not the table.
>
> This file was written on a branch (`fix/grounding-coverage-and-run-quality`,
> PR #81, closed) and lived only there for four days while six sessions worked
> from it. It is on master now so it stops being one `git worktree remove` away
> from disappearing. `UPSTREAM.md`, its companion, landed in PR #100.

---

## 0. Fix first — broken in production, independent of everything else

### 0a. Wayback rate limiting

~~Open.~~ **Done, 2026-08-15.** Pacing, backoff and the circuit breaker landed
with the archiving revival; PR #99 then fixed three defects that stopped them
working under the resolver's thread pool (backoff slept in the failing thread
only; one worker's success reset every other worker's refusal count; the counter
incremented per attempt rather than per lookup). PR #90 says once, at the top of
Section 9, when archive status went unchecked, and PR #88 stopped `archived:
null` from rendering as "not archived". Docs in PR #91.

The archive thread is dead and has been for at least two runs.

| run | resolved citations | archived | HTTP 429 |
|---|---|---|---|
| 2026-08-12 | 32 | 7 | 25 |
| 2026-08-11 | 49 | **0** | ~49 |

`archive.org/wayback/available` throttles at IP level with a long window: 12
consecutive requests all 429, including the first, and still 429 after a 45s
cooldown at 1 request / 6s. The pipeline has **no backoff, no pacing, and no
SPN2 credentials** — `wayback.submit()` accepts `access_key`/`secret_key` and
nothing supplies them.

Until this is fixed, two things already written are inert: the stale-snapshot
resubmission and the live+archive citation pairing will render "Archive: none"
for nearly everything.

**Do:** serialise availability checks with backoff, honour `Retry-After`, and
wire authenticated SPN2 credentials. Decide first whether to adopt
`waybackpy`/`savepagenow` (see UPSTREAM.md #3 — verify whether they already
handle this before writing our own).

### 0b. Claude is configured everywhere and never runs

> **Resolved in code, 2026-08-16 — one config decision left, and it is Mike's.**
> The root cause was found while building
> [#102](https://github.com/MHammett/content-intelligence/pull/102): **`enabled:
> false` on the claude entry in `user.yaml`** — working exactly as designed. The
> two days it took to find that were the real defect, and #102 fixed *that*: a
> model the preset asks for and the run does not call now says why, once per
> model, naming the domains it did not run.
>
> Verified 2026-08-16 by enumerating every shape that can drop a model from the
> whole ensemble — empty `api_key`, missing `api_keys` entry, absent `api_key`
> field, explicit null, keyed as `anthropic` instead of `claude`, `enabled:
> false`, and an empty `prompts:` override. All seven produce a skip line. The
> preset asking for 30 calls and the run making 25 can no longer happen in
> silence.
>
> **What is left is a judgement, not a bug:** whether claude comes off
> `enabled: false`. The correlation caveat below is weaker than when it was
> written — master now excludes the drafting model from `voice_style`
> automatically (`_drafter_is_excluded`), which is precisely the case the
> caveat was about. Turning claude on costs five more calls per `maximum` run
> and buys a sixth opinion on the four reasoning domains.

The `maximum` preset lists `claude` for all five domains — 6 models x 5 domains =
**30 calls**. The 2026-08-12 run made **25**, and `claude` was never assigned.

It has a model in `configs/user.yaml`, an `api_key: ${CLAUDE_API_KEY}` reference,
and `.env` carries the key — which is valid: a direct Claude API call succeeded
during the prompt-caching test the same day. So the credential works and the
preset asks for it, but `_build_assignments` drops it.

~~Not yet pinned to a line. The two candidates are the `${...}` substitution not
resolving in `config_loader`, or the assignment filter's key-presence check.
**Start by logging what `_build_assignments` sees for each model.**~~ Pinned:
neither candidate. It was `enabled: false`, and the suggested next step — log
what `_build_assignments` sees — is what #102 turned into a permanent feature.

Impact: every `maximum` run has been paying maximum-preset prices for
five-sixths of the configured ensemble, silently.

Note when fixing: Claude drafting the article and then reviewing it correlates
blind spots, most acutely for `voice_style` — a model is a poor judge of its own
characteristic patterns, and `ai_speak.txt` exists precisely to catch those. The
reasoning domains (`fact_check`, `completeness`, `argument_integrity`) test
claims against the world rather than prose against a style, so the correlation
does not bite there. Consider per-domain weights rather than a flat enable.

### 0c. Cost model is blind to cached tokens

~~Open.~~ **Done, 2026-08-16 (PR #94).** Cached input is priced, xAI's rates
corrected, and OpenAI's cached tokens are read from the Responses API too. Note
the caveat below still holds for the prompt-cache A/B logs already captured:
they predate this, so they carry elapsed times but no cached-token accounting.

No mention of "cached" in `cost.py`, `tokens.py`, or `pricing.yaml`. Grok caches
automatically today and xAI prices cached input at **$2 vs $12.50 / MTok** — the
report bills it all at full rate. Consequence: **none of the caching work can be
measured**, including the prompt-cache layout that is currently switched off
awaiting exactly that measurement.

**Do:** add cached-token pricing. If the litellm migration goes ahead, do this
as part of it rather than twice — litellm carries its own cost tables and models
cached tokens already.

---

## 1. Land now — survives the litellm migration

> **All six landed, 2026-08-15.** PR 1 → #77 · PR 2 → #78 · PR 3 → #79 ·
> PR 4 → #80 · PR 5 → #82 · PR 6 → #83. The split-and-land-sequentially approach
> below is what actually happened, and it worked: no stacking, no chain
> collapses.
>
> Three follow-ups landed on top of them, all from the same 2026-08-11 report:
> **#74** stopped the citation resolver checking claims against a response-level
> grounded URL (one LBNL report had been stamped onto 44 unrelated claims,
> producing 47 false positives around 2 real findings) and traced claims to the
> draft's own citation markers instead; **#76** made a failed model pass say why
> it failed and which section it left short a model; **#75** was closed as
> superseded by #88/#90/#91 rather than landed.

These sit above or beside the adapter layer. Split from
`fix/grounding-coverage-and-run-quality`, which currently carries seven concerns
and ~1,850 lines. Land as separate PRs off master, sequentially, not stacked.

### PR 1 — Grok timeout budget
`configs/timeouts.yaml`, `tests/test_timeout_model.py`

Grok used 100% of its 126s budget on a 130k-char draft (completeness timed out;
two more domains finished within 14s of the ceiling). Every other provider
finished inside 39%. Multiplier 1.2 → 2.0, giving 210s. The calibration note now
records the measured budget utilisation for all five providers at the 150k size
bucket, which had never been exercised before.

### PR 2 — Same-provider call stagger
`pipeline.py` (`_stagger_offsets`, `_delay_start`), `tests/test_pipeline_timeout.py`

All 25 calls fired simultaneously, so Perplexity's five competed for one account
quota — two returned 429 within one second of each other and one failed
outright. Same-provider calls now start 3s apart; different providers still
start together. The offset is added to the budget, not taken from it.

### PR 3 — Citation claim deduplication
`pipeline.py` (`_claim_key`, `_is_duplicate_claim`), `tests/test_citation_claim_collection.py`

Dedup was exact-text, so five models paraphrasing one fact produced five claims:
29 near-duplicate pairs among 144, one differing from its twin only by a
trailing full stop. Now normalised, collapsing at ≥0.9 token overlap (144 → 137
on the real run). Threshold set high deliberately — 0.8 collapsed more but began
merging claims that differ in a material number.

**Contains a regression fix worth reviewing carefully:** the grounded-URL map is
now keyed by the same normalised key, because dedup collapsing a paraphrase
while the URL map used raw text made a registered source unreachable.

### PR 4 — Link checking
`analysis/links.py`, `ci_core/http.py`, `packages/ci-core/pyproject.toml`, `tests/test_links.py`

Three tiers: honest HEAD → honest GET on 403/405 → browser TLS fingerprint via
`curl_cffi` (new optional `unblock` extra, degrades to previous behaviour if
absent). Measured against six real blocked citations: browser *headers* alone
recovered 0/6 (the blocks are Cloudflare TLS fingerprinting), TLS impersonation
recovered 2/5 with real content, and the GET retry exposed one link reported as
"403, likely still valid" that is a genuine **404**.

The three academic publishers refuse everything and return a ~6 KB challenge
page — almost certainly subscription gates, not bot gates.

### PR 5 — Citation durability: live + archive pairing
`report_markdown.py`, `citation/resolver.py`, `tests/test_report_markdown.py`, `tests/test_resolver.py`

Snapshot URLs were collected on every run and rendered nowhere. Section 9 now
pairs every citation's live URL with its archive copy, distinguishing four
states (archived / stale / submitted this run / never archived) because each
needs different follow-up. Stale snapshots are now resubmitted for capture
alongside missing ones.

**Blocked on 0a for real effect.**

### PR 6 — Run bookkeeping and honest messages
`pipeline.py`, `history.py`, `consolidation.py`, `analysis/seo_suggest.py`,
`tests/test_history.py`, `tests/test_seo_suggest.py`, golden report

- Run numbers come from the handoff, so re-running wrote a second `run_16`.
  Collisions are detected and bumped with a warning to fix the handoff.
- Titles under 8 characters of content no longer claim a history directory
  (`pipeline_history/t/` and `title/` are how this showed up).
- The grammar-skip message named the wrong cause — it told an operator with
  working credentials in `.env` to go configure credentials, when the real
  reason was `grammar_pass: false`.
- Failed passes that degrade another section now say so next to the failure. A
  Perplexity rate-limit silently cost Section 9 all of its grounded URLs and
  dropped citation resolution from 48% to 22%, with nothing connecting the two.
- The delta warns when its baseline was itself an incomplete run.
- SEO meta-description constraint strengthened (the model was told the limit and
  overran it on consecutive runs: 157/155, 177/155). The value is still never
  truncated — that is a deliberate decision, machine truncation reads worse than
  the author trimming.

---

## 2. Hold — litellm supersedes these

Do not merge until the migration decision resolves. If litellm is adopted, most
of this is deleted rather than landed.

| work | why it is held |
|---|---|
| `fix/credit-exhaustion-detection` **entire branch** | litellm raises a proper exception on the in-band streaming error this branch exists to catch |
| ~~`quota.py`~~ — **gone with the adapters** | superseded. The narrow terminal-vs-transient check survives as `_is_terminal_quota_error` in `llm/client.py`; it is *not* going upstream (UPSTREAM.md entry 1) |
| `schema_format.py` + adapter wiring | `instructor`, or litellm's own `response_format` passthrough |
| `streaming.py` stream-error capture (4 accumulators) | litellm raises instead |
| `tokens.py` cached-token handling | litellm models cached tokens already |
| OpenAI `web_search` repair | keep the finding; re-apply only if we keep our own adapter |
| Gemini `grounding_chunks` capture | litellm exposes it as `vertex_ai_grounding_metadata` |

~~**`resolve_grounding_urls` survives regardless**~~ — **no longer true
(2026-08-16).** The URIs are still expiring `vertexaisearch...` wrappers, but
#74 removed the only thing that consumed the resolved URLs: a *response-level*
grounded URL used as a per-claim fallback, which stamped one LBNL report onto 44
unrelated claims and produced 47 false findings. Claims now trace to the draft's
own citation markers. The resolver stays unported on
`fix/grounding-coverage-and-run-quality` and only becomes interesting again if
grounded URLs get a use that is *not* per-claim attachment — which is new
design, not a port.

> **The hold on `fix/credit-exhaustion-detection` is narrower than this table
> says (2026-08-16).** The row above holds the *entire* branch on the grounds
> that litellm raises a proper exception where our adapters returned malformed
> JSON. Filing UPSTREAM.md entry 1 established the rest of that story: litellm
> does raise, but raises `RateLimitError` with `status_code=429` for an account
> with no credits — a transient error for a terminal condition, so retry logic
> retries a dead account.
>
> So adoption fixes the malformed-JSON symptom and leaves the misclassification.
> The terminal-vs-transient check is needed locally until litellm ships a fix —
> **adopted or not** — and it is *not* going upstream: entry 1 is contributed
> rather than opened as a PR, three competing PRs being open there already. Only
> the adapter wiring genuinely depended on the migration decision.
>
> **Both halves have since resolved.** The adapters no longer exist, so the
> branch needs a disposition rather than a hold (see section 5), and the useful
> half already ships as `_is_terminal_quota_error` in `llm/client.py`. The
> finding is banked upstream either way
> ([#32785](https://github.com/BerriAI/litellm/issues/32785), plus review input
> on [#32798](https://github.com/BerriAI/litellm/pull/32798)), so there is no
> urgency on our account.

---

## 3. The litellm migration

> **Phase 1 landed 2026-08-16 as [#103](https://github.com/MHammett/content-intelligence/pull/103)**
> (merge `2162f4d`), net −1,843 lines. The six adapters and `streaming.py` are
> gone; `ci_core/llm/client.py` replaces them.
>
> **Three deliberate departures from the phase list below**, each for a reason
> that was measured rather than assumed. `cost.py` was kept (it reads
> `pricing.yaml`, which we were told not to replace). `model_registry.py` was
> kept (it tracks model supersession, litellm has no equivalent, and
> `ci-discover` and `ci-style-profile` both consume it). And `tokens.py` was
> deleted, then **restored**: the premise for removing it was that litellm
> normalises usage, and it does not — a `responses()` call returns
> `input_tokens` / `input_tokens_details` with `prompt_tokens_details` absent
> entirely, so the inline reader reported **zero cached tokens for every OpenAI
> call**. That is the same blindness `8b0a9d5` had just fixed, and it is why
> phases like this need a real run and not only a green suite.
>
> Phases 2 and 3 are done as part of #103 (timeouts ported and re-verified; the
> preserved fields re-checked against the live pipeline, including a 30-call
> `maximum` run). ~~Phase 4 — the small swaps, `instructor` / `rapidfuzz` /
> `language-tool-python` — has not been started.~~ **Phase 4 — closed
> 2026-08-17, no swap made. The premise was wrong: none of the three were ever
> actual dependencies of this codebase**, so there was nothing for litellm to
> replace. Checked `uv.lock` and every `pyproject.toml` (no entries), then every
> import site:
>
> - `instructor` was never adopted. It was one of two options offered for
>   replacing `schema_format.py` in the Hold table above (`instructor`, or
>   litellm's own `response_format` passthrough) — Phase 1 took the second
>   option. `schema_format.py` is gone; structured output today goes through
>   hand-rolled `ci_core/llm/schema.py` (`schema_mod.as_request_params`),
>   calling litellm's `response_format` / `text.format` directly. Nothing left
>   to swap.
> - `rapidfuzz` was never imported anywhere. PR #79's claim dedup
>   (`pipeline.py`'s `_claim_key` / `_is_duplicate_claim`) is hand-rolled
>   Jaccard similarity over token sets — no fuzzy-matching library involved.
>   Not LLM-related and was mis-scoped into this phase.
> - `language-tool-python` was never imported either.
>   `adapters/grammar/languagetool.py` is a ~30-line `requests` client against
>   LanguageTool's public HTTP API (`api.languagetool.org/v2/check`), not the
>   `language-tool-python` package. Also not LLM-related.
>
> Documentation-only close-out: no source change. Verified with a clean `uv
> sync` in a fresh worktree (`objective-robinson-a9dd63`) confirming the
> editable install resolved to that worktree, then `uv run pytest packages/` —
> 1448 passed.

Spiked against 1.96.2 on 2026-08-12. **Verdict: migrate.** Everything the
pipeline depends on survives: Perplexity `citations`/`search_results`, Gemini
grounding, truncation via `finish_reason`, cached tokens, structured output,
Perplexity search params, and — critically — in-band streaming errors surface as
exceptions rather than as empty content.

**One hard constraint, measured:** OpenAI must go through `litellm.responses()`,
not `litellm.completion()`.

| surface | time to first byte | total | max gap |
|---|---|---|---|
| `completion(stream=True)` | 79.1s | 79.1s | 79.1s (100% silence) |
| `responses(stream=True)` | 0.8s | 76.5s | 17.2s, 1318 summary deltas |

`completion()` routes reasoning models through Chat Completions and sends zero
bytes while thinking — the exact regression the Responses migration fixed. So
the "one call shape" benefit is partial: five providers through `completion()`,
OpenAI through `responses()`.

**Phases:**

1. Replace the six adapters + `streaming.py` + `tokens.py` + `cost.py` +
   `model_registry.py` (~3,900 lines) with litellm, OpenAI on `responses()`.
2. Port the timeouts calibration — it is measured knowledge and survives as
   config, but litellm has its own timeout/retry semantics to fit it to.
3. Re-verify the five preserved fields against the real pipeline, not a spike.
4. ~~Then the small swaps: `instructor`, `rapidfuzz`, `language-tool-python`.~~
   Closed 2026-08-17 — none were actual dependencies; see the note above.

**Not recommended:** replacing `analysis/links.py` with lychee. It is a Rust
binary (subprocess, not import) and the tier semantics would need rebuilding
around its output. 239 lines is not where the pain is.

---

## 4. Upstream — see UPSTREAM.md

> **UPSTREAM.md is the live copy and landed on master in PR #100.** The table
> below is a snapshot; read it there, not here. Entry 1 is contributed and
> deliberately not a PR — litellm already has three competing PRs open for it,
> unreviewed (reasoning in UPSTREAM.md entry 1, updated 2026-08-16 in PR #114).
> Entry 2 is contributed, and entry 3 taught the lesson now written into
> UPSTREAM.md's header: it was dropped on non-adoption, and when the source was
> finally read it contained a genuine bug in one library and a claim that was
> simply wrong about the other. "We don't depend on it" is not a verification.

| # | item | status |
|---|---|---|
| 1 | litellm classifies credit exhaustion as `RateLimitError`, so retry logic retries a dead account. | contributed — twice ([#32785](https://github.com/BerriAI/litellm/issues/32785#issuecomment-5299586013), [#32798](https://github.com/BerriAI/litellm/pull/32798#issuecomment-5310640191)); deliberately **not** a PR |
| 4 | litellm `completion()` silently drops reasoning-summary streaming for OpenAI reasoning models. | ready |
| 2 | Link checkers have no TLS-impersonation tier. | verify — file against whichever checker we adopt |
| 3 | Wayback clients: 429 backoff and authenticated SPN2. | verify — check current releases first |

---

## 5. Decisions needed

1. **Adopt litellm?** Everything in section 2 depends on the answer.

   **This reads as contradicting section 3, which records "Verdict: migrate"
   from the 1.96.2 spike.** Reconciling the two (2026-08-16): the *technical*
   question is settled — everything the pipeline depends on survives, with the
   one measured constraint that OpenAI must go through `litellm.responses()`.
   What is unsettled is the *scheduling* call, which is a real one: phase 1
   replaces ~3,900 lines across six adapters plus `streaming.py`, `tokens.py`,
   `cost.py` and `model_registry.py`.

   **And the migration has since been built.** `refactor/litellm-migration`
   (worktree `ci-wt-litellm`, commit `992ddcf`) is phase 1 complete: ~3,100
   lines deleted across the six adapters, replaced by
   `packages/ci-core/src/ci_core/llm/client.py`, suite 1046 → 1047. It found
   five defects that every unit test passed and only a real run exposed —
   `temperature` to Anthropic (hard 400, all five Claude domains dead),
   litellm's allowlist blocking Mistral's `reasoning_effort` (fixed with
   `allowed_openai_params`, deliberately **not** `drop_params`, which would
   silently discard reasoning), a dead account arriving as 429, a network blip
   synthesised into a 503 that walked the fallback chain, and a dropped
   keepalive producing 500s on ~10% of calls at exactly this pipeline's 1–3s
   spacing.

   **Answered 2026-08-16: merged as #103.** Every blocker listed here cleared:

   - It was pushed, rebased onto master, and merged.
   - The **OpenAI path is verified live** — credit was restored, and a `maximum`
     run put 11k–24k output tokens at `xhigh` through `responses()` across four
     domains.
   - Rebasing it exposed the risk that made the delay expensive: master had
     fixed **four things in the very layer the branch deletes** (`8b0a9d5`,
     `cef2232`, `0d6b2cc`, `4aab95a`), and a rebase resolves modify/delete in
     the deletion's favour *without showing what the modification was*. All four
     were ported deliberately, using master's own tests as the spec. Two of them
     were fixes for bugs the branch still had. The lesson is in
     `CLAUDE.md`-shaped terms: push a branch that deletes a hot file the day it
     is written.
   - Per the note in section 2, only the adapter-wiring half of
     `fix/credit-exhaustion-detection` depended on this answer. **The adapters
     no longer exist**, so that branch now needs a disposition rather than a
     hold — its useful half (terminal-vs-transient classification) already ships
     in the shim as `_is_terminal_quota_error`, applied to every retryable
     status because a dead account arrives as a 429 directly and as a
     synthesised 503 mid-stream.
2. **Prompt-cache layout** — built, defaulted off. **Still open, blocked.** It
   moves the domain instruction from before the article to after it (76% of
   input cached on calls 2+, ~$0.56–1.01/run).

   Two things settled on 2026-08-14 that whoever cuts this PR needs:

   - **The golden report cannot verify it.** `test_pipeline_end_to_end.py` stubs
     `_run_domain`, and that is exactly where `prompt_cache_layout` is applied,
     so flipping the flag produces an *empty* golden diff by construction. An
     empty diff there is evidence the code never ran, not evidence the findings
     held. Verifying it means a live run diffed against a prior live run of the
     same article — and reading that diff means checking `model_failures` first,
     because a run that lost a provider looks exactly like a behavioural change.
   - **A paired A/B run already exists, uncommitted.** `arm_c_control_replicate.log`
     (78 KB) and `arm_e_treatment_replicate.log` (99 KB), untracked in the
     `ci-wt-cache-layout` worktree: 506.9s control vs 615.7s treatment on the
     same article. That is the live-run pair the point above says is required —
     but both predate PR #94, so they carry elapsed times and no cached-token
     accounting, which is the number the decision actually turns on. Treat them
     as a rehearsal of the method, not as the measurement. **They exist in one
     place only; decide whether to keep them before that worktree is reclaimed.**
   - **Documentation is written and waiting.** A `### Prompt cache layout`
     section for `docs/CONFIGURATION.md` and the `user.example.yaml` comment
     block were drafted for PR #84 and pulled back out, because master has no
     such setting yet. Recover them from that PR's branch history and land them
     alongside the feature so it does not ship undocumented.

   **Unblocked 2026-08-16 — both blockers cleared, and it is now the highest-value
   item left.** OpenAI credit is restored (verified by a live `maximum` run), and
   the instrument that made the earlier A/B untrustworthy is fixed: #103 reads
   cached tokens off the Responses API's `input_tokens_details`, which is where
   OpenAI actually reports them. Before that, every OpenAI call reported zero
   cached tokens — so the previous null result was measuring a blind instrument,
   not an ineffective optimisation. A live run now reports real cache hits
   (8,320 tokens on a small draft) and prices them at the cached rate.

   Two cautions carried forward: the golden report still cannot verify this (it
   stubs `_run_domain`, which is where the layout is applied), and the existing
   `arm_c` / `arm_e` logs predate #94 so they carry elapsed times and no
   cached-token accounting — a rehearsal of the method, not the measurement.

   What has *not* changed is who decides. This alters prompt structure on a
   pipeline whose output is the product, so it still wants a golden-report diff
   and Mike's judgment rather than an agent's.

   **Actually settled 2026-08-16, in a session this file didn't capture at the
   time.** A different session ran the live A/B this section calls for — four
   full runs of the same unedited article, two per condition (arms A/C off, B/E
   on), ~$32 in API spend. Result: **no detectable quality effect.** Voice
   findings were 31, 34 (off) vs. 23, 39 (on) — conditions differ by 1.5 findings
   on average, while a *single* condition varies by 16 on its own. Every other
   section showed the same pattern. Two mid-session alarms ("voice drops 26%",
   "only 3 of 11 consensus flags shared") were both retracted once a second
   same-condition run reproduced the same spread with nothing changed.

   That same test surfaced something bigger than the caching question: **only
   18 of 259 distinct findings reproduced across 3+ of the 4 runs** — a single
   run here is roughly 75% non-reproducible, independent of this setting. On
   that basis the PR carrying this work (#97, the original) was **deliberately
   closed**, not for a quality reason but a cost one: $0.55 on a ~$8 run (~7%)
   optimizes the cost of a measurement this pipeline doesn't yet make trustworthy
   in one copy, and the setting is a second prompt-assembly path to maintain for
   that return.

   **That closure never made it into persistent memory, so it didn't survive.**
   A later session (2026-08-17) found the code sitting on a stale branch, saw
   "tested, documented, off by default," and re-ported it as
   [#111](https://github.com/MHammett/content-intelligence/pull/111) /
   [#112](https://github.com/MHammett/content-intelligence/pull/112) — the
   Anthropic cache-breakpoint half, since Anthropic caches nothing implicitly and
   a matching prefix alone bought zero cached tokens until it landed — with no
   reference to the earlier A/B or the closure reasoning. Docs followed in #122,
   also without that context. Both merges are sound (suite green, code reviewed,
   matches the measured cache behaviour) — the gap was historical, not technical.

   **Final call, 2026-08-17: leave it merged, off by default, documented with
   this history.** The quality question is genuinely closed — verified harmless,
   not merely unproven. The reason to leave it off is specific to this project's
   current use (single-run reviews, where reproducibility dominates the $0.55
   saving), not a property of the feature. Someone running at higher volume, or
   already aggregating multiple runs per article — which is where the
   reproducibility finding above actually points — would see the saving scale
   with run count instead of being swamped by per-run noise, and should feel free
   to turn it on. `docs/CONFIGURATION.md`'s "Prompt cache layout" section and the
   `user.example.yaml` comment carry this reasoning so it travels with the
   setting instead of living only here.
3. ~~**Delete the junk history directories?**~~ **Done, 2026-08-14.** `t/` and
   `title/` are gone. The Jun 8 report was refiled into the article's own
   directory as `run_1_20260608_075204_report.json`. It turned out to be the
   smallest part of a larger problem: the same article occupied three more
   directories because its title kept being revised, which let one article clear
   `voice_pattern_report`'s `MIN_ARTICLES = 3` on its own. All 47 reports are now
   under `pipeline_history/dc-environment/`, and a `History key:` handoff field
   (PR #84) stops title revisions forking a history again.
4. ~~**Enable OpenAI `web_search`?**~~ **Done, 2026-08-14 (PR #84).** Scoped to
   `fact_check` rather than enabled flat — it is a per-model flag, so at
   `maximum` it was billing a search on all five domains when only `fact_check`
   can use one. Note it also never survived `cost_preset`, which rebuilds the
   model dict and dropped it; fixed in the same PR. What it buys is a
   live-fetched `source` field instead of training recall — not annotations,
   which are structurally empty under JSON-only prompts.
5. ~~**Enable Claude?**~~ **Done, 2026-08-14 (PR #84).** Superseded by a general
   drafting-model exclusion: `pipeline.drafting_model` (or `Drafted with:` in a
   handoff) drops the declared drafter from `voice_style` only, so Claude can be
   enabled without judging its own phrasing habits.

---

## Known open items, not addressed here

- ~~Only **9 of 144 claims** in the last run were verified against a document the
  pipeline actually read. The rest rest on a model asserting a source exists.
  The tier names say so; the framing invites more confidence than earned.~~
  **Framing addressed, 2026-08-15 (PRs #85, #92).** Section 9 now opens on the
  fraction that was actually checked against a fetched document, and a
  fact-check verdict reports whether it arrived with a verbatim quote and a
  direct URL. The underlying ratio is still low — the reporting is honest about
  it now rather than inviting more confidence than earned.
- ~~Rerun nondeterminism: consensus flags vary between identical runs.~~
  **Diagnosed, 2026-08-17 — not fixed.** Two things checked:

  1. *Sampling config* (`ci_core/llm/client.py`): no provider is ever called
     with a `seed`. Temperature is 0.2 (never 0) and only reaches
     gemini/mistral/grok/perplexity — Claude never gets a temperature at all
     (Anthropic 400s on non-default values for reasoning models) and OpenAI
     drops it whenever `reasoning_effort` is set (the Responses API rejects
     it). So on the `maximum` preset, the two deepest-reasoning models in the
     six-model ensemble (`gpt-5.5` xhigh, `claude-opus-4-8`) run with **zero**
     sampling control. This predates the litellm migration — verified
     identical in the old adapters — and was never a deliberate choice either
     way.
  2. *Per-domain breakdown*, computed from the same 4 runs behind the
     prompt-cache-layout A/B (`ci-wt-cache-layout`'s
     `pipeline_history/dc-environment/run_{16,17,19,20}_*_report.json` — the
     untracked `arm_c`/`arm_e` logs are gone, but these survived and are the
     same dataset). Cross-run clustering on passage+problem text, robust
     across three strictness thresholds: `fact_check` reproduces best (~5.5%
     of findings show up in 3+ of 4 runs), `completeness` worst (**0%**),
     `voice_style` (~1.8%) and `argument_integrity` (~4.2%) in between. That's
     the **opposite** of "subjective domains are noisier" — it looks like
     task shape, not provider: `fact_check` checks a finite, textually-fixed
     set of claims, while `completeness`/`voice_style` ask for an open-ended
     judgment call with an effectively unbounded answer space, so independent
     samples don't converge. Consensus flags — which already require ≥2
     models to agree within one run — reproduce at 26.3%, 5-15x better than
     raw single-model findings, which is evidence *for* multi-run aggregation
     specifically, not for a sampling-parameter fix (a `seed` wouldn't reach
     Claude or OpenAI's reasoning models anyway — neither supports it there).

  **Not fixed, and the reason is a real design problem, not inertia.** The
  fix the evidence points to — rerun N times, keep only findings that repeat
  — multiplies cost on a pipeline that's already $1-8/article at `maximum`,
  and Mike's workflow iterates a draft many times before it's final. Paying
  the N× cost on every intermediate iteration is wasteful; paying it only on
  the last run requires knowing a run is the last one before running it,
  which nothing today can tell you. Direction not yet chosen — candidates
  raised but not evaluated: an explicit `--final` flag the author sets by
  hand, a lighter/cheaper consensus-only rerun pass instead of N full
  `maximum` runs, or leaving single-run reports as a first-pass tool and only
  invoking N-run aggregation as a separate, deliberately-invoked step once a
  draft is believed final.
- ~~Grok's output volume is far below the other providers on identical domains
  (602 tokens vs 9,377 and 22,414 on `voice_style`). An explicit `max_tokens`
  now removes the provider default as a suspect; the cause is still unknown.~~
  **Does not reproduce after the litellm migration (2026-08-16).** Two
  independent `maximum` runs put grok's `voice_style` output at **4,475** and
  **4,669** tokens — above gemini (3,126 / 3,473) and claude (1,288 / 1,310) in
  both, and its `red_team` at 9,288 was the highest single output in the run.

  Being honest about what that does and does not establish: the observation
  predates #103, which replaced grok's entire call path, so "fixed incidentally"
  and "was environmental all along" both fit the data and this cannot tell them
  apart. The original cause was never identified and now cannot be. Closing on
  non-recurrence rather than on diagnosis, and worth reopening if a run ever
  shows grok an order of magnitude below the others again.

  One real thing did fall out of looking: **#103 dropped
  `response_format: {"type": "json_object"}` for grok**, which the old adapter
  sent unconditionally. That is a genuine unintended change, though not the
  explanation here — tested live, adding it back does not make grok terser. It
  belongs with the structured-output work, where it is a regression to repair
  rather than a feature to add. The other providers lost nothing: OpenAI's
  Responses API has no such parameter, Perplexity does not support it, Anthropic
  has no equivalent, and mistral/gemini only sent it in the non-reasoning and
  non-grounded paths that `maximum` does not use.

- **Seven retry/backoff implementations, no shared mechanism.**
  **Surveyed 2026-09-07 against `ac73de5` — not fixed.** Retry *policy* should
  differ between these; a 429 carrying `Retry-After` is not a stalled model
  stream, and archive.org's IP-block behaviour is not Gmail's quota. What none
  of them share is the *mechanism*, so a fix to jitter, caps or circuit-breaking
  in one never reaches the others.

  | # | site | attempts | delay | `Retry-After` | breaker | terminal-vs-transient |
  |---|------|----------|-------|---------------|---------|-----------------------|
  | 1 | `ci_core/llm/client.py` `_with_retry` | 1 | fixed `retry_delay` (10s) | no | no | **yes** — `_is_terminal_quota_error` |
  | 2 | `pipeline.py` recovery passes | `recovery_passes` (1) | fixed `recovery_delay_seconds` (30s) | no | no | yes — skips terminal |
  | 3 | `adapters/citation/wayback.py` | `_MAX_ATTEMPTS` (3) | `5.0 * 2**attempt` | **yes** | **yes** | 429 only |
  | 4 | `adapters/grammar/languagetool.py` | 1 | fixed `retry_delay` (10s) | no | no | status list only |
  | 5 | `collectors/outlook365.py` | 3 | `2**(attempt+1)` / `2**attempt` | yes | no | no |
  | 6 | `collectors/twitter.py` | 3 | `2**(retries+1)` | yes | no | no |
  | 7 | `collectors/gmail.py` | 3 | `2**attempt` | **no** | no | no |

  **Not one of them jitters.** Verified by grep for
  `jitter|random.uniform|random.random` across every file under
  `packages/*/src`: zero matches. That matters because the fan-outs are wide and
  synchronised — `analysis/links.py` runs 10 URL checks at once and
  `adapters/citation/resolver.py` 8 claim resolutions, both against whatever
  hosts an article happens to cite. When several of those hit the same host and
  back off by the same `2**attempt`, every retry lands in the same instant. The
  bound exists to be polite to the origin; un-jittered backoff spends it.

  `wayback.py` is the one place that solved this, and it solved it the *opposite*
  way on purpose: a process-wide clock (`_MIN_INTERVAL_SECONDS = 3.0`) plus a
  shared `_blocked_until` that a 429 pushes out for **every** thread, not just
  the one that hit it. That is right for a single rate-limited endpoint — and it
  is unreachable from anywhere else, because it is module-global state in a
  citation adapter.

  The two most considered pieces of policy in the codebase are also the least
  reachable:

  - `_is_terminal_quota_error` (`client.py`) knows that an exhausted account
    arrives as a 429 directly but as a synthesised 503 mid-stream, and that
    neither is worth retrying. It is private, unexported, and referenced in
    exactly one file.
  - the circuit breaker (`wayback.py`) is module-global.

  So `gmail.py` sleeps `2**attempt` on a 401 that will never succeed, and
  nothing outside `client.py` can tell a dead account from a transient limit.

  **What is verified above:** the per-site table and the absence of jitter, both
  read off the tree at `ac73de5`. **What is not:** whether any of this has cost a
  real run. No retry-storm has been measured; the argument is structural.

  **The shape of a fix, if taken:** a single `retry(fn, policy)` primitive in
  `ci_core` — attempts, delay function, jitter, cap, `Retry-After` handling, a
  retryable-vs-terminal predicate and an optional shared breaker — with each of
  the seven passing its own policy. Policy stays different everywhere; jitter,
  caps and breaking get fixed once. Ordering note: this is the same argument
  that `run_all_bounded` settled for fan-out (#162, #164), where three
  hand-rolled semaphores in `resolver.py` had independently drifted and one of
  them was silently mis-charging every job past the first wave. The retry sites
  have had no equivalent consolidation and are spread across all three packages.

  A cheap first step that is worth doing regardless of the larger consolidation:
  give `gmail.py` the `Retry-After` handling its two sibling collectors already
  have. It is the only one of the seven that ignores the header outright.
