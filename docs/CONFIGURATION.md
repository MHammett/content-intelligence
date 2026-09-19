# Configuration Reference

Configuration lives in several files:

- `configs/user.yaml` — your API keys, model selection, and pipeline behavior (gitignored, never committed)
- `configs/your_publication_name.yaml` — per-publication settings: voice profile, audience, WordPress credentials
- `configs/presets.yaml` — cost preset model assignments; edit to update model names without touching code

Three more are provider/model reference data rather than review settings, so they live in `ci-core` next to the shared LLM layer that reads them (`packages/ci-core/src/ci_core/configs/`):

- `pricing.yaml` — per-million-token pricing for cost estimation; update when providers change prices
- `model_registry.yaml` — model deprecation tracking; edit to add superseded entries and bump the date
- `timeouts.yaml` — sliding-scale timeout model (size × model × effort multipliers)

The first two are gitignored and have example templates you copy (`user.example.yaml`, `publication.example.yaml`, plus worked examples in `configs/examples/`). The rest are committed defaults — they ship with the repo and *are* their own reference; edit them in place, no copy step.

---

## Step 1: Run setup

The fastest way to scaffold configs is the built-in setup command, which also verifies dependencies:

```powershell
uv run ci-setup --publication your_publication_name
```

This creates `configs/`, copies both example templates, and prints exactly what to fill in.

**Manual copy** (if you prefer to do it yourself):

```powershell
# PowerShell
New-Item -ItemType Directory -Force configs
copy packages\ci-article-review\src\ci_article_review\configs\user.example.yaml configs\user.yaml
copy packages\ci-article-review\src\ci_article_review\configs\publication.example.yaml configs\your_publication_name.yaml
```

```bash
# macOS / Linux / Git Bash
mkdir -p configs
cp packages/ci-article-review/src/ci_article_review/configs/user.example.yaml configs/user.yaml
cp packages/ci-article-review/src/ci_article_review/configs/publication.example.yaml configs/your_publication_name.yaml
```

The `.gitignore` excludes `configs/user.yaml` and all `configs/*.yaml` files that aren't examples or committed defaults. Your keys will not be committed.

---

## user.yaml

### API keys

```yaml
api_keys:
  openai:
    api_key: sk-...

  gemini:
    api_key: AI...        # omit when using provider: vertex_ai

  mistral:
    api_key: your_mistral_key

  # Optional
  perplexity:
    api_key: your_perplexity_key

  grok:
    api_key: your_grok_key

  claude:
    api_key: your_claude_key

  languagetool:
    username: your_email@example.com
    api_key: your_languagetool_key

  # Optional — authenticates Wayback Machine "Save Page Now" submissions for
  # resolved citations that aren't archived yet
  archive_org:
    access_key: your_access_key_here
    secret_key: your_secret_key_here
```

**`archive_org`** is optional. The pipeline submits unarchived citation URLs to archive.org either way; credentials switch it from the unauthenticated capture endpoint to the authenticated SPN2 endpoint, which has higher rate limits, returns a job id, and — the part that matters for the report — lets the pipeline ask `/save/status/<job_id>` how a capture actually went. That endpoint is credential-only, so without a key pair a queued capture can never be followed up. Get a key pair at <https://archive.org/account/s3.php>. See [CITATIONS.md](CITATIONS.md#wayback-machine-behavior) for what the submission actually does.

Note that the `mistral` key does double duty: besides its review passes, it powers the citation relevance check that gates the "verified" confidence tier. Without it, citations are fetched and checksummed but not relevance-confirmed — see [CITATIONS.md](CITATIONS.md#confidence-tiers).

Instead of putting keys directly in the YAML, you can use environment variables:

```yaml
api_keys:
  openai:
    api_key: ${OPENAI_API_KEY}
```

Copy `.env.example` to `.env`, fill in the values. The pipeline loads `.env` automatically.

---

### API key precedence

Four tiers, most to least specific — a value at a higher tier overrides the same provider's value at every lower one:

1. **`--api-key` on the CLI** (`ci-review` and `ci-check`, repeatable). Highest precedence, scoped to that one invocation — nothing is written to disk. `PROVIDER=VALUE` is shorthand for the common case, the provider's `api_key` field: `openai`, `gemini`, `mistral`, `grok`, `perplexity`, `claude`. A credential with more than one field needs the explicit `PROVIDER.FIELD=VALUE` form: `languagetool.username`, `languagetool.api_key`, `archive_org.access_key`, `archive_org.secret_key`.
   ```powershell
   uv run ci-review --draft handoff.md --publication mypub --api-key openai=sk-proj-...
   uv run ci-review --draft handoff.md --publication mypub --api-key languagetool.username=me@example.com
   ```
   WordPress credentials aren't under `api_keys` (they live in `publication.wordpress`), so they get their own flags instead: `--wp-user` / `--wp-password`, valid with `--publish`.
2. **A publication config's own `api_keys` section.** Same shape as `user.yaml`'s, and it only has to name the providers/fields it overrides — anything it doesn't mention falls through to `user.yaml`. Useful when one publication bills to a different OpenAI project, say. (WordPress credentials don't have a separate override tier here — a publication config's own `wordpress.*` fields already *are* this tier, which is what `--wp-user`/`--wp-password` sit above.)
   ```yaml
   # configs/mypub.yaml
   api_keys:
     openai:
       api_key: ${MYPUB_OPENAI_API_KEY}
   ```
3. **`user.yaml`'s `api_keys` section**, `${VAR}`-resolved against `.env` (see below). The shared fallback every publication sits on top of.
4. **A bare OS environment variable**, when nothing above defines it — the pipeline still runs, it just isn't pinned to a specific `.env` entry or config.

Within tier 3, `.env` always wins over a same-named OS environment variable — not the reverse. If both define `OPENAI_API_KEY` with different values, the `.env` value is what gets used, so editing `.env` always takes effect regardless of what's already set in your shell or user profile. `ci-check --publication <name> --show-keys` prints exactly which value each provider resolved to and, if `.env` and the OS disagree, an unprompted `NOTE` names the variable — informational, since the outcome is no longer ambiguous, but worth confirming the winner is the one you meant.

This precedence isn't unique to `ci-review`/`ci-check` — `ci-discover` and `ci-style-profile` (package `ci-style-profile`) read the same `configs/user.yaml` and `.env`, and resolve `${VAR}` the same `.env`-wins way (tiers 3–4; they don't have a publication-config or `--api-key` concept of their own, so tiers 1–2 don't apply there).

This replaced an earlier design where a pre-existing OS-level variable silently beat `.env` with no override available short of unsetting the shell variable — the exact shape of a real incident where a stale `OPENAI_API_KEY` billed the wrong project for days before anyone noticed. `--api-key` and per-publication `api_keys` exist so an intentional override now has a place to go instead.

---

### Model configuration

Two forms are accepted. Mix and match — each model can use either form independently.

**Simple form:** model name as a string, uses the provider's public API.

```yaml
models:
  openai: gpt-5.6-terra       # gpt-5.6-sol for max quality, gpt-5.6-luna for economy
  gemini: gemini-2.5-flash    # best price-performance; its retirement date is in PROVIDERS.md
  mistral: mistral-large-latest
  perplexity: sonar-reasoning-pro   # Sonar retires 2026-09-27; see PROVIDERS.md
  grok: grok-4.3              # what `wide` runs; grok-4.6 adds reasoning_effort (low to xhigh)
  claude: claude-sonnet-5     # claude-opus-5 for more depth, claude-haiku-4-5-20251001 for least cost
```

**Extended form:** dict with `model`, optional `provider`, and provider-specific fields.

```yaml
models:
  gemini:
    provider: vertex_ai
    model: gemini-2.5-flash
    project: your-gcp-project-id
    location: us-central1
    # credentials_file: C:\Users\you\keys\my-project.json   # omit = ADC
```

#### Disabling a model

Use `enabled: false` to skip a model without removing its key — useful when comparing runs:

```yaml
models:
  grok:
    enabled: false
    model: grok-4.3
```

#### Restricting which prompts a model runs

Use `prompts:` to override the thoroughness preset for a specific model. This model will only run the listed domains regardless of the thoroughness setting:

```yaml
models:
  perplexity:
    model: sonar-pro
    prompts: [fact_check]   # only run fact-check; skip voice, argument, etc.
```

Valid domain names: `fact_check`, `voice_style`, `completeness`, `argument_integrity`, `red_team`

The `prompts:` list is an **infrastructure key** — it survives `cost_preset` overrides. If you set `prompts:` in `models:` for a provider, the preset will not clear it even when it overrides the model ID and reasoning flags.

Claude used to be the standard example here, kept off `fact_check` because it could not search. It can now: it searches wherever `web_search` covers the domain — the key works as in [OpenAI web search](#openai-web-search) — and the `maximum` preset sets `web_search: [fact_check]` for it. A `prompts:` list that still drops `fact_check` from Claude keeps it off that domain under every preset, so the `maximum` preset's search never runs.

#### The three timeout layers (streaming)

The review adapters stream responses (Server-Sent Events). That splits "the timeout" into three layers with distinct jobs — knowing which is which is the key to tuning:

| Layer | What it bounds | Typical size | Set by |
|---|---|---|---|
| **First-byte allowance** | How long to wait for the stream to *start* (the socket read timeout) | generous and per-model — 120s default; 160s for grounded Gemini/Perplexity (search runs before the first token); 200s/500s where a preset overrides it | `stream_read_timeout` per model, else the provider default, else the `thorough`/`maximum` preset's override |
| **Inter-chunk stall detector** | Max silence *between* chunks once the stream has started | tight and constant — **60s for every provider** | `stream_gap_timeout` per model, else the 60s default |
| **Per-task wall-clock backstop** | Total time one model+domain call may run before the pipeline thread kills it | the sliding-scale computed value (below) | `timeout_seconds` per model, else computed |
| **Global batch ceiling** | Outer bound on the whole parallel batch | slowest backstop + retry + slack | derived (`_global_ceiling()`) |

**Why streaming matters:** before streaming, a model that buffered its entire 16–30k-token reasoning+output and sent nothing until done forced the *socket* read timeout to cover the full compute time (gpt-5.5 xhigh needed an ~819s per-call timeout). With streaming, tokens arrive incrementally, so the socket timeout becomes the **gap between tokens** — small and constant regardless of total length, and a hang/stall is caught in ~120s instead of after the whole giant budget elapses.

Streaming does **not** make a long generation finish faster — that gpt-5.5 xhigh call still emits tokens for ~800s. So the **wall-clock backstop still must cover the genuine total generation time**; it just no longer has to absorb "model sent nothing for 800s, is it hung?" — the read-gap layer answers that.

**Why the first two layers are separate knobs.** They were one value until 2026-08-15, and that single value had to be large enough to survive a grounded model's search phase — which is how Perplexity's reached 500s, in four bumps (160 → 280 → 350 → 500), each one chasing a slow-but-alive call. The side effect was that a genuinely *dead* Sonar connection also took over eight minutes to notice, which removes the one thing a stall detector is for. Splitting them lets the first-byte allowance stay generous per model while the stall detector goes back to a tight 60s for everyone: once a stream has started emitting, a healthy provider keeps emitting (the worst gap ever observed here is ~8s, from gpt-5.5's xhigh reasoning-summary deltas), so a minute of silence means dead, not slow.

The stall detector is enforced outside the socket, because iterating a stream blocks *inside* a socket read and cannot be interrupted by a clock check in the consuming loop. If you raise `stream_read_timeout` for a slow-starting model, you do **not** need to touch `stream_gap_timeout` — that is the whole point of them being separate.

**Measuring them.** Every `[CALIBRATION]` log line ends with `first_byte=` and `max_gap=`: the longest the call went without receiving anything before real output began, and after — the numbers `stream_read_timeout` and `stream_gap_timeout` have to exceed. Both budgets restart on every chunk, so every chunk ends a silence, but only real output (the stall detector's own test) moves a stream from the first phase to the second. There is one value per stream, oldest first, so a call whose first attempt stalled shows two. A stream cut short in a phase can only give a lower bound for it, written `>120.02s`; `-` means the phase never began. The report's `api_call_log` has the same values under `stream_timing`, with the budgets in force and `first_output_s` (request to first real output). Do not size either knob from a single-call run: stalls track concurrency, and an isolated call reproduces the healthy case by construction — see the CONCURRENCY note in `presets.yaml`.

**Caveat found in production:** the 120s default read gap assumed only *grounded* (search) calls have a long silent period before the first token. In practice, `high`/`xhigh` reasoning effort also produces a long silent stretch — the model "thinks" with zero bytes on the wire, not even a keep-alive — before it starts streaming visible output. Observed directly: `gpt-5.5` at `xhigh` failed 5/5 calls at ~121s with 0 output tokens against the 120s default. The presets ship a `stream_read_timeout` override wherever a model has needed one: 200s for Mistral at `high` (`thorough` and `maximum`), and 260s for Gemini at `maximum`, where grounding and a thinking budget stack (below). OpenAI needs none, because its Responses API streams reasoning summaries through the silent phase, and Perplexity's 500s covers its search phase rather than reasoning (the notes in `presets.yaml` have the measurements). If you define a custom preset or override `reasoning_effort` to `high`/`xhigh` on a provider the built-in presets don't cover, set `stream_read_timeout` yourself — don't rely on the 120s default.

The two silent-period causes **stack** when a model does both at once. The `maximum` preset's Gemini entry sets `thinking_budget: 16000` on top of the model's default 160s grounded read gap; a live Vertex AI run timed out at 205.78s (search + extended thinking, both silent, ahead of the 160s default). Gemini's `maximum` entry now carries its own `stream_read_timeout: 260` for this reason — grounding and reasoning-effort overrides aren't mutually exclusive, so check whether both apply when tuning a custom config.

#### Wall-clock backstop is automatic (sliding scale)

You normally don't set timeouts at all. After pre-analysis, the pipeline sizes each model's **wall-clock backstop** from the draft's **character count**, the **model**, and the **reasoning effort**, using the multiplier tables in [`ci-core`'s `timeouts.yaml`](../packages/ci-core/src/ci_core/configs/timeouts.yaml):

```
effective = clamp( base × size_mult × model_mult × effort_mult × variance_margin,  floor,  task_timeout_seconds − 15 )
```

So a short economy run gets a small backstop and a 10k-word `maximum` run gives gpt-5.5 xhigh its full budget (~1020s) — no hand-tuning. `pipeline.task_timeout_seconds` is the absolute ceiling the formula clamps to. Edit `timeouts.yaml` to retune (the model/effort values are calibrated; the size buckets are anchored to one ~74k-char document and are reasoned estimates elsewhere).

**`variance_margin`** is a single global safety buffer (default `1.25`) multiplied onto every computed backstop. The multipliers target *typical* completion time; this margin covers run-to-run variance in **total** time (reasoning output volume swings widely). Under streaming it no longer has to cover *stall* uncertainty — the read-gap layer catches stalls directly — so it can be tuned lower than under buffered POSTs. Raise it for more headroom (longer worst-case waits), lower it to fail faster. The trade is purely worst-case wait, not typical run time — calls return the instant they finish.

To **override the read gap** for a specific model, set `stream_read_timeout` (seconds) on it — useful for a grounded model whose live search delays the first token. To **override the wall-clock backstop**, set `timeout_seconds` explicitly — that value wins and skips the formula:

#### Per-model timeout overrides

Set `timeout_seconds` per model to override the sliding-scale **wall-clock backstop**; set `stream_read_timeout` to override the **inter-token read gap**:

```yaml
models:
  gemini:
    provider: vertex_ai
    model: gemini-2.5-flash
    timeout_seconds: 540        # wall-clock backstop for live-search fact-check
    stream_read_timeout: 200    # allow a longer first-token gap while it searches

  mistral:
    model: mistral-medium-3-5
    reasoning_effort: high
    timeout_seconds: 240        # total-time headroom for reasoning on long articles

  claude:
    model: claude-opus-5
    timeout_seconds: 900        # only if the computed budget keeps cutting it off
```

Under streaming the adapter passes `timeout=(connect, read_gap)` to the HTTP request, where `read_gap` is the **inter-token** allowance (constant, from `stream_read_timeout` or the adapter default) — **not** `timeout_seconds`. The big `timeout_seconds` value is enforced separately as the pipeline's per-task thread wall-clock backstop. Set `pipeline.task_timeout_seconds` high enough to accommodate your slowest model's genuine total generation time (streaming detects stalls quickly but does not shorten a legitimately long generation).

`timeout_seconds` is an **infrastructure key** — it survives `cost_preset` overrides. If you set it in `models:` for a provider, the preset will not clear it.

#### Output-token ceiling is automatic too

Claude and Mistral are the two providers whose requests carry an output-token ceiling (`max_tokens`); the other four run to the model's own limit. On a reasoning pass (`effort` / `reasoning_effort` set, or a Claude model that thinks with none set — see [below](#claude--adaptive-vs-extended-thinking)) the pipeline sizes that ceiling per call, from [`ci-core`'s `output_tokens.yaml`](../packages/ci-core/src/ci_core/configs/output_tokens.yaml):

```
max_tokens = reasoning_tokens[effort] + answer_tokens[domain] × size_mult
```

`size_mult` is the wall-clock formula's own size table, so the two scale together. The ceiling counts **thinking**: every truncated pass on record ran out while reasoning, including on drafts under 5,000 characters, which is why reasoning is a flat term and only the answer scales with the draft. A capped model's wall-clock backstop is raised, if it has to be, to the time a healthy call needs to reach its ceiling — otherwise a longer ceiling just turns truncations into timeouts.

A truncated pass is reported as **PARTIAL**: the complete findings it wrote are kept, and the report names which of its output buckets never arrived — in the header, and above the section it feeds.

To set a ceiling yourself, put `max_tokens` on the model. Like `timeout_seconds`, it is an infrastructure key that survives `cost_preset`. `--no-timeout` lifts the ceiling to the model's own limit as well as the backstop, so a calibration run measures a call's real length instead of the ceiling.

---

### Reasoning controls

Each provider exposes reasoning differently. All can be set in the extended model config.

#### OpenAI — `reasoning_effort`

Valid for `gpt-5.x` models. Controls the depth of the model's chain-of-thought before output.

```yaml
models:
  openai:
    model: gpt-5.6-terra
    reasoning_effort: medium    # none | low | medium | high | xhigh
    timeout_seconds: 300
```

`gpt-5.6-sol`, `gpt-5.6-terra` and `gpt-5.6-luna` all default to `medium` if you omit the parameter (OpenAI's model pages, checked 2026-09-19), and the pipeline sends nothing when it is unset. So the `economy` and `wide` presets, whose `gpt-5.6-luna` entry sets no effort, reason at `medium`; it is `none` that turns reasoning off. `xhigh` is slow: `gpt-5.6-sol` calls at `xhigh` in saved runs took a median of about 370s (140–800s over 23 calls), against about 25s for `gpt-5.6-luna` with no effort set.

#### Claude — adaptive vs extended thinking

**Critical distinction:** the mode depends on the model. Using the wrong mode causes a 400 error.

| Model | Thinking mode | With no `effort` set | How to configure |
|---|---|---|---|
| claude-opus-5, claude-sonnet-5 | Adaptive | **Thinks, at `high`** | `effort: low/medium/high` — controls depth |
| claude-fable-5 | Adaptive (always on, cannot be turned off) | Thinks, at `high` | `effort:` controls depth |
| claude-opus-4-8, claude-sonnet-4-6 | Adaptive | Does not think | `effort: low/medium/high` — turns it on and controls depth |
| claude-haiku-4-5-20251001 | Extended (opt-in) | Does not think | `thinking_budget: N` — token ceiling |

Leaving `effort` unset is not "no reasoning" on Opus 5 or Sonnet 5: Anthropic's API thinks by default on those models, at its default effort of `high`, so the run is the same as `effort: high`, and the pipeline sizes its output ceiling and wall-clock budget the same way. Set `effort: low` or `medium` to spend less. (Per-model defaults: Anthropic's [thinking troubleshooting](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting) page.)

**Do not** set `thinking_budget` on Opus 5, Sonnet 5, Opus 4.8, or Fable 5. It asks for extended thinking, which Claude 4.7 and later models reject with a 400 (`"thinking.type.enabled" is not supported for this model`); Sonnet 4.6 still accepts it, but Anthropic has deprecated it there. Use `effort:` instead. Haiku 4.5 is the model `thinking_budget` is for: extended thinking is its only mode.

```yaml
# Opus 5 or Sonnet 5 — adaptive thinking, control effort level
models:
  claude:
    model: claude-sonnet-5
    effort: medium          # "low" | "medium" | "high" — unset runs at "high"

# Haiku 4.5 — extended thinking with token budget
models:
  claude:
    model: claude-haiku-4-5-20251001
    thinking_budget: 5000   # allocates up to 5K reasoning tokens
    timeout_seconds: 240
```

#### Gemini — `thinking_budget`

```yaml
models:
  gemini:
    provider: vertex_ai
    model: gemini-2.5-flash
    thinking_budget: 8192   # 0 = off (Flash models only); omit = dynamic default
    project: your-gcp-project-id
    location: us-central1
```

`thinking_budget` is the control for the 2.5 models, and the only Gemini thinking setting this pipeline sends. Google's limits (from its Vertex AI [thinking guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking)): 2.5 Flash takes 1–24,576 tokens, 2.5 Flash-Lite 512–24,576 and 2.5 Pro 128–32,768; unset, each thinks dynamically, up to 8,192. `0` turns thinking off on 2.5 Flash and Flash-Lite. **Thinking cannot be turned off on 2.5 Pro**, which is what `thorough` and `maximum` run. Gemini 3.x models are steered by a different parameter, `thinking_level`, which this pipeline does not send; Google's guide says a 3.x request that sets both returns an error, and that a model earlier than 3 rejects `thinking_level`.

#### Grok — `reasoning_effort`

Grok reasoning used to be chosen by model name. grok-4.5 and grok-4.6 now take a
real `reasoning_effort` (xAI's [reasoning guide](https://docs.x.ai/docs/guides/reasoning)
lists `low`, `medium`, `high` and `xhigh`, default `high`; `xhigh` is 4.6-only, and
4.5 treats it as `high`). Which form applies depends entirely on the model:

```yaml
models:
  grok:
    model: grok-4.6
    reasoning_effort: low     # low | medium | high | xhigh
```

**Unset is not off.** It resolves to the provider default, which for grok-4.6 is
`high` — so omitting the flag buys the most expensive setting. Every preset that
runs grok-4.6 now states what it wants for that reason.

Verified live 2026-09-05: litellm forwards the parameter to xAI with no
client-side rejection and none of the `allowed_openai_params` escape hatch
Mistral needs, and Grok honours it — ~380 completion tokens at `low` against
~1060 at `high` on the same prompt, a 3x difference in both tokens and latency.

xAI's guide lists no other Grok model as taking `reasoning_effort`, so no preset
sends it to grok-4.3 (which `wide` runs) or to the 4.20 variants, where reasoning
is chosen by picking the `-reasoning` or `-non-reasoning` model. Sending it to a
model that does not take it risks a 400 for no gain.

#### Mistral — `reasoning_effort`

Mistral reasoning runs on `mistral-medium-3-5` (not on standard models like `mistral-large-latest`). Two critical constraints:

1. **Model ID:** The reasoning model is `mistral-medium-3-5` — the `-latest` suffix variant does not exist and returns a 400 error.
2. **Accepted values:** Only `"high"` and `"none"` are accepted. `"low"` and `"medium"` return a 400 error.

Standard Mistral models (`mistral-large-latest`, `mistral-small-latest`) do not support `reasoning_effort` at all and will return a 400 error if you add it.

```yaml
# Reasoning model (replaces deprecated magistral-medium-latest)
models:
  mistral:
    model: mistral-medium-3-5
    reasoning_effort: high    # only "high" or "none" accepted on this model
    timeout_seconds: 240

# Standard model (no reasoning)
models:
  mistral:
    model: mistral-large-latest
    timeout_seconds: 240
```

The `balanced` cost preset uses `mistral-medium-3-5` without a `reasoning_effort` flag (no `"low"` tier available). `thorough` and `maximum` use `reasoning_effort: "high"`.

---

### OpenAI web search

GPT-5.x can run through the OpenAI Responses API with live web search enabled. Restrict it to the domains that can use it:

```yaml
models:
  openai:
    model: gpt-5.6-terra
    web_search: [fact_check]    # only fact_check searches
```

`web_search: true` is still accepted and means every domain, which is what you almost never want. Search bills per search on top of tokens, and among the always-on domains only `fact_check` has any use for it — there it replaces training recall with a live-fetched `source`. `voice_style` matches the draft against a voice profile, and `completeness`, `argument_integrity` and `red_team` all reason about the draft in front of them. At `maximum` thoroughness, where OpenAI runs all five domains, the list form is the difference between one paid search context per run and five.

**`expansion` is the second domain worth listing**, on the runs where you use it. It is the opt-in pass behind `--expand`, and the bucket carrying most of its value is `sources` — a model without search is reaching into training recall for URLs, which is exactly the condition that produces invented ones. If you run `--expand` with a non-grounded model, add it:

```yaml
    web_search: [fact_check, expansion]
```

Perplexity needs nothing here — `sonar` models search natively. Gemini and OpenAI do. The pipeline fetches every URL the pass returns either way and reports how many resolved, so you can see directly what search bought you.

If the Responses API is unavailable it falls back to standard chat completions silently.

One tradeoff to weigh before enabling it: Gemini and Perplexity are already search-grounded fact-checkers. Consensus scoring treats agreement between models as evidence, and three search-grounded checkers agreeing is weaker evidence than three independent ones agreeing, because they may be reading the same page. Enabling it trades an independent recall-based check for a third correlated live one.

Note also that annotations come back structurally empty under this pipeline's JSON-only prompts. Search runs and the `source` field improves; the annotation list does not populate.

---

### Vertex AI (Gemini)

Switch Gemini from AI Studio to a reserved capacity pool:

```yaml
models:
  gemini:
    provider: vertex_ai
    model: gemini-2.5-flash
    project: your-gcp-project-id       # GCP project ID (not display name)
    location: us-central1
    # credentials_file: path\to\key.json  # omit to use Application Default Credentials
```

See [PROVIDERS.md](PROVIDERS.md#option-b--vertex-ai-reserved-capacity-no-503s) for the full setup walkthrough.

---

### Azure OpenAI

```yaml
api_keys:
  openai:
    api_key: your-azure-resource-key   # NOT an OpenAI key

models:
  openai:
    provider: azure
    model: gpt-5.6-terra               # informational label only
    endpoint: https://my-resource.openai.azure.com
    deployment: my-gpt5-deployment
    api_version: "2024-02-01"          # optional; defaults to 2024-02-01
```

Azure provisioned throughput deployments do not 503; the fallback chain is bypassed.

---

### Azure AI (Mistral serverless)

```yaml
api_keys:
  mistral:
    api_key: your-azure-endpoint-key

models:
  mistral:
    provider: azure
    model: mistral-large-latest        # informational label only
    endpoint: https://Mistral-Large-abc.eastus2.inference.ai.azure.com
```

---

### Pipeline behavior

```yaml
pipeline:
  grammar_pass: true            # false = skip LanguageTool entirely
  parallel_review_calls: true
  retry_on_failure: true
  retry_delay_seconds: 10
  recovery_passes: 1            # additional passes after the main batch, retrying only calls still marked failed
  recovery_delay_seconds: 30    # pause before each recovery pass — deliberately coarser than retry_delay_seconds
  abort_if_all_provider_calls_fail: false
  task_timeout_seconds: 1100    # absolute ceiling for the sliding-scale timeout model; formula clamps to this − 15
  cost_preset: wide             # economy | wide | balanced | thorough | maximum

  link_validation: true         # check HTTP status of every URL in the draft
  wayback_link_check: true      # also query the Wayback Machine for each URL
  wayback_snapshot_stale_days: 180  # snapshots older than this are flagged [STALE]

  drafting_model: claude        # excluded from voice_style — see below
  prompt_cache_layout: false    # see "Prompt cache layout"
```

**`recovery_passes`/`recovery_delay_seconds`** re-attempt only the (model, domain) calls still marked failed after the main ensemble batch — a run that comes back 28/30 calls otherwise costs the same as a clean one, and the only way to fill the gap was a full re-run. A call whose error text looks permanent (bad key, archived account, quota exhausted) is skipped rather than retried every pass: it will fail the same way each time and only spends money to learn that again. Set `recovery_passes: 0` to disable. Not run for `--replay`, which makes no model calls at all.

**`--retry-failed RESULTS_JSON`** is the manual counterpart to `recovery_passes`, for when the gap surfaces after the fact — the automatic passes exhausted their budget, or a run predates them. It loads a prior run's `run_N_results.json`, makes model calls only for the entries marked failed in it, and merges the new attempts onto everything that already succeeded, then continues through consolidation/citations/report as normal — a new, distinct `run_N` is written, nothing is overwritten in place. Its cost is what it bought: the results it carried over are marked `replayed` in the report's `api_call_log`, so `cost_summary.incurred_usd` and the printed `Estimated cost:` cover only the calls this run made, while `replayed_usd` is what the capture's own run already paid for the rest. The report names the capture in `retry_failed_from`. Requires the same draft-loading flags (`--draft`/`--url`/`--raw-draft`) as the original run, so the same runners and prompts can be rebuilt; mutually exclusive with `--replay`, which makes no model calls at all.

```powershell
uv run ci-review --draft handoff.md --publication mypub --retry-failed pipeline_history/<article>/run_16_20260815_140635_results.json
```

**`wayback_snapshot_stale_days`** controls when a Wayback Machine snapshot is considered stale. At 180 days (default), a snapshot from more than six months ago triggers a `[STALE]` flag and a manual re-archive recommendation. Lower this for publications with high source-freshness standards (e.g., 90 days for breaking-news adjacent pieces). It applies to both draft link validation and resolved citation URLs.

Archiving is not check-only: resolved citation URLs that aren't archived yet are submitted to archive.org's Save Page Now, and a fetch the origin refused (401/403/429), that never reached it (timeout, DNS failure), or that stopped at a certificate which failed verification falls back to reading an archived snapshot. Both are covered in [CITATIONS.md](CITATIONS.md#wayback-machine-behavior).

---

### Drafting model

If you draft with a model, name it — that model is then excluded from `voice_style`:

```yaml
pipeline:
  drafting_model: claude
```

`voice_style` runs `ai_speak.txt`, which asks the reviewer to flag hedging, throat-clearing, vague significance gesturing and the problem→cause→solution skeleton. Those are AI defaults. A model asked to find them in its own output is being asked to notice its own habits, and it under-reports them. Every other model still reviews voice normally.

For a single article, declare it in the handoff instead — this wins over the config, because the drafting tool can change between pieces while the config does not:

```
Drafted with: claude
```

Accepted names are the model keys: `claude`, `openai`, `gemini`, `mistral`, `grok`, `perplexity`. An unrecognised name logs a warning and excludes nothing, so a typo costs a dropped review pass rather than the run.

Only `voice_style` is affected. A model re-reading its own reasoning in `argument_integrity` has a similar conflict but a much weaker one — that prompt asks whether the logic holds, not whether the prose carries the model's fingerprints — and widening the exclusion costs real review coverage.

Watch for one edge case: at `standard` thoroughness `voice_style` is a single model, so declaring that model as the drafter leaves the domain with no reviewer. Drafting with `openai` at `standard` is the only preset/drafter combination that does this — at `thorough` or `maximum` there are always other models covering it.

Three things happen when it does, because an empty voice section otherwise reads exactly like a clean one:

1. **A warning at assignment time**, before any call is made, naming the domain and the drafter — the only signal that arrives while the run can still be stopped.
2. **A substitute provider runs the domain**, via the same pass that covers a domain whose models all failed (see *Substituting a provider for an empty domain* below). This is the normal outcome: any other configured, credentialed model can take `voice_style`.
3. **The report says the domain was not reviewed** — a *Domains not reviewed* block in the header and a note above the section itself — for when substitution cannot help: `substitute_failed_domains: false`, a replay (which makes no calls), or no other model available to take the domain.

---

### Prompt cache layout

```yaml
pipeline:
  prompt_cache_layout: true
```

Providers cache on an exact *leading* prefix. The per-domain instruction normally sits ahead of the article, and it differs for every domain, so a provider's five calls in one run share a long article and not one cacheable byte — measured 0 cached tokens on every call. This setting moves the domain instruction to the end of the user message, leaving a constant stub plus the article as a shared prefix, so calls 2+ hit the cache. Measured 1792/2368 tokens cached (76%), worth roughly $0.56/run at a 50% cached-token discount and ~$1.01 at 90%.

**Off by default — verified, not merely unproven.** Four full live runs of the same unedited article, two with the layout off and two on, found no detectable effect on review quality: voice findings were 31, 34 (off) vs. 23, 39 (on) — the two conditions differ by 1.5 findings on average, while a single condition varies by 16 on its own. Every other section showed the same pattern (citations ranged 147–217 regardless of the setting). Two earlier concerns raised during that test — a "26% drop" in voice findings and weak consensus overlap between arms — were both retracted once a second same-condition run showed identical spread with nothing changed.

**Why it stays off anyway, for this project:** the same four runs surfaced something bigger than the caching question — only 18 of 259 distinct findings reproduced across 3 or more of the 4 runs, meaning a single run here is roughly 75% non-reproducible regardless of this setting. Against that, a $0.55 saving on a ~$8 run (≈7%) optimizes the cost of a measurement this pipeline's own output doesn't yet make trustworthy in one copy — and the setting is a second prompt-assembly path to keep working. That's a judgment call about this pipeline's priorities, not a defect in the feature: **if your use case runs the pipeline at high volume, or already aggregates multiple runs into one result (where the saving scales with run count instead of being swamped by per-run noise), this is a legitimate setting to turn on.** The code, tests, and this section exist so that decision is cheap to make.

**Re-examine this now that aggregation has landed (2026-09-05).** The clause above names a condition — "already aggregates multiple runs into one result" — that the pipeline now meets part of the way by itself: every run is scored against earlier runs of the same draft (see [Reproducibility context](#reproducibility-context) below). That does not flip the decision on its own, and the default stays `false`. What changed is the incentive underneath it. Re-running an unchanged draft used to buy nothing but a different random sample; it now measurably improves the report, because each repeat run enlarges the denominator every finding is scored against. **If you start deliberately running a draft two or three times before revising it, you are the use case this setting was left open for, and the per-run saving multiplies by the number of runs you make.** If you keep running each draft once, nothing here has changed and it should stay off.

**The golden report cannot verify this.** `test_pipeline_end_to_end.py` stubs `_run_domain`, and that is exactly where this setting is applied, so flipping the flag produces an empty golden diff by construction. An empty diff there is evidence the code never ran, not evidence the findings held. Verifying it means a live run compared against a prior live run of the same article — and, per the finding above, at least two runs per condition, since a single run's findings are not a stable baseline to diff against.

---

### Substituting a provider for an empty domain

```yaml
pipeline:
  substitute_failed_domains: true   # default; false disables the pass
```

When every model assigned to a domain fails, one different provider is tried for
that domain. It runs after the recovery pass, adds nothing to a clean run, and
tries exactly one substitute — a provider having an outage should not buy call
after call.

**Why recovery is not enough.** `recovery_passes` retries the model that failed,
which is right for a flake and useless for an outage. Measured 2026-09-05 on
`dc-environment-v26` at `--cost-preset standard`: `gemini:fact_check` returned
"stream stalled before the first chunk: nothing received for 160.0s", recovery
retried the same model and it stalled identically, and the run exited 0 having
spent $0.64.

**Why `fact_check` in particular.** At `standard` thoroughness it is a single
model *and* the only source of claims, so losing it empties Section 2, leaves
nothing for citation resolution, and empties Section 9 as well. Losing one of
two models in another domain costs coverage; losing this one costs both sections
that justify the run.

Substitutes are drawn from the `maximum` preset's list for the domain, minus
whatever was already tried, and honour the same credential checks, `prompts:`
overrides and drafting-model exclusion as a normal assignment. For `fact_check`
the search-grounded models are preferred first — grounding is the reason gemini
is in that ensemble at all — falling back to an ungrounded model only because an
ungrounded fact-check pass is still worth more than an empty section. The
original failure is kept in the results either way, so the report still says
which model failed.

**A domain that was never assigned a model is repaired too.** The set of domains
checked for emptiness comes from the thoroughness preset, not from the results.
Results only exist for domains that were *attempted*, so a domain whose every
candidate was excluded — no credentials, `enabled: false`, a `prompts:` override,
or drafting with the one model assigned to it — produces no result to be found
empty, and used to be the one case this pass could not see. That is the total
loss, not a partial one.

---

### Ensemble width, and backfilling a narrowed domain

```yaml
pipeline:
  backfill_narrowed_domains: true   # default; false keeps the preset's literal lists
```

A cost preset buys fewer models, and that is the point of it. Two things make
the trade worse than it needs to be, and both are addressed here.

**The report now states the width.** Every report carries an *Ensemble Width*
section in its header giving, for the preset actually used: how many domains ran
on a single model, how many distinct models ran at all, a per-domain table of
which models ran where, and what that does to Section 1. Previously the only
record was the API call table, which reads as width only if you already know
`_THOROUGHNESS_PRESETS` by heart and subtract the models the preset disabled.

Consensus is the part whose meaning changes. Section 1 needs
`consensus_min_models` (default 2) *distinct* sources on a passage, so:

- Where every domain runs one model, no passage can reach the minimum from
  inside a single domain — it takes two different domains flagging the same
  passage. The section says so rather than leaving a thin Section 1 to read as a
  clean draft.
- Where the whole voter pool is below the minimum, Section 1 **cannot flag
  anything at all**, and the report says that in those words. LanguageTool
  counts toward the pool, since it is an independent source.

`consensus_min_models` is deliberately *not* lowered automatically at thin
presets. Lowering it would trade the one guarantee Section 1 makes — that
something more than a single model agreed — for a fuller-looking section, and
the backfill below recovers the width more honestly. Set it yourself if you want
the weaker bar; the report prints the value in force.

**Backfill.** A domain left narrower than its own preset entry asked for is
topped back up from the models still available. Measured 2026-09-05 at
`economy`, which disables grok and claude: the `standard` map pairs mistral with
claude in `argument_integrity` and with grok in `red_team`, so disabling those
two costs *two* domains their second model — and perplexity, which `economy`
configures as a cheap grounded model, that map never assigns at all. The result
was five domains on three distinct models, with the cheapest available second
opinion sitting idle. With backfill it is seven calls across four distinct
models, and three single-model domains.

The rules are deliberately tight:

- A domain is topped up **only to two models**, and never past what its own
  preset entry asked for. Two is what corroboration costs — one model flagging
  a passage is a finding, two is agreement, and `consensus_min_models` will not
  promote anything to Section 1 below it. Going further buys a third voter for
  a passage that already had a second: measured at +30% cost on `thorough` with
  one key missing, for no change in how many domains were left uncorroborated.
- A run with every configured model available is **untouched** — nothing is
  short, so nothing is added.
- Candidates are ordered by which model is carrying the fewest domains already,
  so the width bought is distinct-model coverage rather than a third and fourth
  domain piled onto whichever model sorts first.
- Credential checks, `enabled: false`, `prompts:` overrides and the
  drafting-model exclusion all apply exactly as in a normal assignment, and
  `fact_check` still prefers a search-grounded model.

Every backfilled assignment is logged (`Backfilled: ...`) and listed in the
report's *Ensemble Width* section, naming the preset entry it stands in for.
Set `backfill_narrowed_domains: false` to keep the preset's literal lists.

**What it costs, per tier.** Measured 2026-09-05 against the real presets and
`pricing.yaml`, for a ~1,400-word draft. With every provider credentialled only
`economy` changes at all — the tier that was losing the most, and the only one
whose preset disables models:

| Preset | Calls | Distinct models | Single-model domains | Review-call cost |
| --- | --- | --- | --- | --- |
| `economy` | 5 → 7 | 3 → 4 | 5 → 3 | $0.019 → $0.034 (+81%) |
| `standard` (since retired) | 7 → 7 | 5 → 5 | 3 → 3 | unchanged |
| `balanced` / `thorough` / `maximum` | unchanged | unchanged | unchanged | unchanged |

The `standard` row is that preset as it was when this was measured, on the day it
was retired; it now runs as `wide`, which this table does not cover.

`economy`'s +81% is the largest relative increase and the smallest absolute one:
about 1.5 cents. A live run measured $0.0645 all-in, on a real draft whose size
was not recorded.

When a key is missing rather than a preset disabling a model, the two-model cap
is what keeps the thick tiers cheap: `thorough` without a claude key is 9 → 10
calls (+9%), where topping up to full preset width would have been 12 (+30%) for
the same number of uncorroborated domains.

Turn it off with `backfill_narrowed_domains: false` if the preset's literal call
count is the budget you are holding to. It is also off automatically under
`--only-model` / `--only-domain`, which exist to price one cell.

**Why not rebalance the preset lists instead?** Splitting the mistral pairing
would fix `economy` specifically and nothing else. Backfill fixes whichever
models a given run is actually missing, including combinations nobody
anticipated — a key that expired, a provider having an outage, a `prompts:`
override — so the fixed lists stay a statement of intent rather than a table
that has to encode every degraded configuration.

---

### Citation re-ask

```yaml
pipeline:
  citation_reask: true          # default; false disables the pass entirely
  citation_reask_limit: 12      # default; refutations re-asked per run
```

When a citation is fetched, read, and found not to support the claim it was
cited for, the claim is handed back to the model that asserted it. The model is
shown its own claim, the URL, the verdict, and the sentence the relevance check
relied on, and answers with one of four actions: correct the claim, propose a
different source, withdraw it, or stand by it. The answer is rendered under the
refutation in Section 9.

**Why it is worth a call.** In the run this was built against, 2 of 49
refutations came back `contradicts`, and both were repairable rather than wrong:
a claim of "17 billion gallons" against a page reading "66 billion liters" —
which is ≈17.4 billion gallons — and a compound claim whose page supported one
half. In both the correct figure was already on the page the pipeline had
fetched, and the report said only that the citation failed.

**What it cannot do.** The model being asked is the one whose assertion just
failed, and the question invites it to defend itself. So a re-ask never changes
`verification`: a refuted citation stays refuted, and the answer is advisory
text beside it. A proposed alternative URL is not reported as a source either —
it goes back through the same fetch, checksum, relevance check and
grounded-quote requirement as any other citation, and what the report shows is
what that check found. A model answering with a plausible-looking URL gets it
checked, not printed.

`stand` is a first-class answer for the same reason: a model with no way to
disagree picks the nearest available action instead, and a fabricated
`different_source` costs a fetch to disprove.

**Cost.** One call per refutation, bounded by `citation_reask_limit`; live web
search is disabled for these calls. Refutations past the limit are logged rather
than dropped silently. The pass does not run with `--offline`. A claim traced to
the draft's own citation block was asserted by the author rather than by a
model, so there is nobody to hand it back to and it is skipped.

---

### Reproducibility context

Every run compares itself against earlier runs of the same draft and reports what
it finds. There is no setting: it is always on, it costs nothing, and it makes no
model calls.

**The problem it addresses.** A review report used to present one run's findings
as a definitive list — counts, weights, and a ranked Section 1 that feeds the
revision prompt. It is not one. As measured above, only 18 of 259 distinct
findings reproduced across 3 or more of 4 runs of the same unedited article. A
reader — including the person who wrote the draft — reasonably takes "4 consensus
flags" as a property of their article. It is substantially a property of that
run. Reports now say so, at the top, in a **Reading this report** block that
frames the sections rather than footnoting them.

**Where the comparison comes from.** `pipeline_history/` already holds a full
report for every run ever made, including the draft text each one reviewed. So
the Nth run of a draft can be scored against the N-1 that preceded it using work
already paid for. Nothing is re-run, and no run costs more than it did before.
This directly answers the objection that reproducibility can only be bought by
paying N times per draft: for the second and later runs of a draft, it is free.

**What counts as comparable.** Two runs are compared only when they reviewed the
same draft *and* ran the same ensemble configuration. Both halves matter, and
the second is easy to get wrong. This project's own
`pipeline_history/dc-environment` holds 47 runs over 8 distinct drafts, and its
largest same-draft cluster — 23 runs — mixes single-pass probes with full 25-pass
`maximum` runs. Grouping on the draft alone would report a finding as "absent
from 11 runs" that never ran the domain capable of producing it. Runs that do not
match are listed in the report as skipped, with the reason, so a small
denominator never looks like a thin history.

The key covers more than the pass list, because `model:domain` names a
*provider*: `openai:fact_check` is the same string whether the run used
`gpt-5.6-luna` or `gpt-5.6-sol`. So the distinct model identities are folded in from
the run's call log, together with `consensus_threshold` and
`consensus_min_models` — the two settings that decide what reaches Section 1 at
all. Change a preset or retune either gate and later runs stop being scored
against the old ones, which is correct: they are measuring a different pipeline.

Every part of that key was chosen by measuring what it costs against the 47-run
`dc-environment` history, counting runs that still find a comparable
predecessor: pass list alone 25, plus the two consensus gates 25 (free), plus
model identities 20. Two further candidates were rejected for what they cost —
splitting on the `[grounded]` suffix (14) and on per-call reasoning effort (11).
The grounding suffix in particular marks a *call*, not a configuration: the same
model appears with and without it inside a single run.

Whitespace-only differences do not count as a different draft. `prompt_cache_layout`
is deliberately *not* part of the comparability key: the four-run test above found
its two conditions indistinguishable within run-to-run noise, and it is the pairing
of those two conditions that produced the 18-of-259 figure in the first place.

**Partial failures.** A run whose `voice_style` passes all failed had no
opportunity to raise a voice finding, and scoring it as "did not reproduce" would
understate every voice finding in the report. Denominators are therefore per
section: a prior run counts toward a section only if at least one pass feeding
that section succeeded. This matters more than it sounds — of the 12 comparable
25-pass runs of one draft in this repo, exactly **one** finished with no failed
passes, so discarding degraded runs would have discarded the history. The
correction is partial by construction (a run that lost one of five voice passes
still counts as a whole unit), so reproduction counts are a floor rather than a
point estimate, and the report says that whenever any comparable run was degraded.

**What the report shows.**

- A **Reading this report** block at the top: how many comparable runs were
  found, how many of this run's findings recurred in at least half of them, and
  a per-section table. With no comparable history it says so plainly and quotes
  the 18-of-259 calibration figure instead of implying a measurement it did not
  take.
- **Section 1 in bands, not ranked.** The old strict ordering by `weight_sum`
  was misleading: in one measured run of this repo's own history, the
  top-ranked finding (weight 2.5) appeared in *none* of the 4 comparable prior
  runs, while the finding ranked third appeared in all 4. Findings are now
  grouped into `Reproduced in earlier runs` / `Partly reproduced` / `New in
  this run`, and order within a band carries no meaning. They say
  "reproduced", not "corroborated": this report uses corroboration for a
  different axis — several models agreeing *within one run*, in Section 8's
  ranking and in the Ensemble Width block — and a finding can be one without
  the other. Without comparable
  history the bands fall back to how many passes flagged each finding, which is
  a count of evidence rather than a weighted score.
- **Weights to one decimal, labelled "run weight".** Two decimals implied a
  precision the process does not have: the same finding on the same draft scored
  anywhere from 2.0 to 7.4 across repeat runs.
- **Per-finding notes** in Sections 1 through 5, reading `[also in 3 of 4
  comparable prior runs]` or `[new — in none of 4 ...]`.

A finding raised only once is not thereby wrong. Most findings are single-run,
and single-run findings include real ones. It means the evidence for it is one
sample, which is what the report now says.

**What this run may have missed.** Everything above annotates findings that are
*present*. The reverse — what earlier runs caught and this one did not — is
invisible by construction, and on measured history it is the larger set: a run
of the dc-environment draft reproduced 3 of 26 completeness findings, so each
run also misses many its predecessors found. The report now lists them, under
that heading, above the worklist.

Ordinarily "present last time, absent now" would be ambiguous — it might have
been fixed. Here it cannot be. Comparability already pins the draft to the same
text, so nothing in that list was resolved between runs; each entry is either
noise in the earlier runs or a miss in this one, and the report says it cannot
tell you which.

Two thresholds keep it readable, both set by counting what they would actually
print on the same cluster with each run in turn treated as the current one:

| prior runs (N) | 1 | 2 | 3 | 4 |
|---|---:|---:|---:|---:|
| raised by >=1 prior run | 130 | 239 | 329 | 424 |
| raised by >=half | 130 | 239 | 35 | 54 |
| raised by **all** | 130 | **15** | **4** | **4** |

So a finding is listed only when **every** comparable prior run raised it, and
only when there are **at least two** of them — at N=1 every rule degenerates to
"everything the other run found". The list caps at ten with the remainder
counted, and the full set is in the report JSON. A `fact-check / confirmed`
entry means a claim earlier runs verified went unchecked this time: a gap in
coverage rather than a problem missed.

**In the report JSON**, findings carry `reproduced_in` / `reproduced_of`, and a
`reproducibility` block records the fingerprint, the comparable runs, the skipped
ones with reasons, per-section totals, and `dropped` (the block above). When
nothing comparable was found the per-finding fields are **absent rather than
zero** — a measurement that was never taken must not read as a measurement that
came back empty.

**To get the measurement**, run the same draft twice before revising it. The
second run reports against the first at no extra cost. Editing the draft resets
it, which is correct: findings about the old text are not evidence about the new.

---

### Cost presets

The `cost_preset` setting is the easiest way to control quality vs cost. It sets model variants, reasoning flags, and thoroughness level as a bundle. You set one value instead of configuring six providers separately.

Preset model assignments live in [`configs/presets.yaml`](../packages/ci-article-review/src/ci_article_review/configs/presets.yaml). When providers release new models, edit that file to update the model names — no code change needed.

**When `cost_preset` is set:**
- It overrides model names and reasoning flags for all configured providers
- Provider infrastructure settings (vertex_ai, azure, credentials_file) are preserved
- Providers you haven't configured (no API key) are skipped
- Setting `enabled: false` on a provider still takes precedence
- Set `thoroughness:` separately to override just that part of the preset

**The presets**, cheapest first. `wide` is the default in `user.example.yaml`.

| Preset | Thoroughness | Models used | Reasoning |
|---|---|---|---|
| `economy` | standard | gpt-5.6-luna, gemini-2.5-flash, mistral-small-latest, sonar | not set (each provider's default applies) |
| `wide` | thorough | gpt-5.6-luna, gemini-2.5-flash, mistral-small-latest, sonar, grok-4.3, claude-haiku-4-5 | not set (each provider's default applies) |
| `balanced` | thorough | gpt-5.6-terra (low), gemini-2.5-flash, mistral-medium-3-5, sonar-reasoning-pro, grok-4.6 (low), claude-sonnet-5 (effort medium) | light |
| `thorough` | thorough | gpt-5.6-terra (high), gemini-2.5-pro, mistral-medium-3-5 (high), sonar-reasoning-pro, grok-4.6 (high), claude-sonnet-5 (effort high) | deep |
| `maximum` | maximum | gpt-5.6-sol (xhigh), gemini-2.5-pro (thinking budget 16,000), mistral-medium-3-5 (high), sonar-reasoning-pro, grok-4.6 (high), claude-opus-5 (effort high, searches on fact_check) | max |

`standard` was a sixth preset until 2026-09-05. `wide` beat it on every axis measured over three isolated runs each — 33% more strongly corroborated consensus flags, 74% more fact-check claims, and findings that survived a rerun 67% of the time against 50% — so it was retired rather than kept as a tier that costs more for less. An existing `cost_preset: standard` still works: it runs as `wide` and warns on every run, because the change is real (six models over twelve calls where `standard` ran five over seven), not a rename.

**Two of the models these presets run are on retirement schedules.** Perplexity has announced that its Sonar chat-completions API is supported until 2026-09-27; `sonar` and `sonar-reasoning-pro` are the Perplexity models every preset runs. Google Cloud lists `gemini-2.5-flash` and `gemini-2.5-pro` for retirement on Vertex AI on 2026-10-20. `presets.yaml` has not been changed for either yet. Dates and sources are under [Perplexity](PROVIDERS.md#perplexity-ai-optional--recommended) and [Gemini](PROVIDERS.md#google-gemini-required) in PROVIDERS.md.

**What a run costs.** Cost depends on the draft, so each figure below is tied to the draft it was measured on. Read each as a floor rather than a quote: a retried attempt that the provider billed but reported no usage for is priced at $0.00 (the report's `cost_summary` counts these as `uncosted_calls`), and the figures are token costs only, so per-search and per-request fees come on top.

| Preset | ~1,400 words (9,456 chars) | ~2,900 words (18,167 chars) | ~19,500 words (135,514 chars) |
|---|---|---|---|
| `economy` | — | — | — |
| `wide` | $0.10–$0.15 (4 runs) | — | $0.38 (mean of 3) |
| `balanced` | — | — | $1.28 (mean of 3) |
| `thorough` | — | $1.33 (1 run) | $1.74 (mean of 3) |
| `maximum` | — | $6.15 (1 run) | ≈$9.6 (1 run) |

A dash means no run at that size is on record. `economy` has none at any recorded size: it runs seven calls on four of `wide`'s six models where `wide` runs twelve, so on the same draft it costs less than `wide`. The three-run means are from the 2026-09-08 study behind the `thorough` change. `maximum`'s three-run mean in that study was $10.27, priced at a `gpt-5.6-sol` rate that has since been corrected; a later single run on the same draft, re-priced at current rates, gives about $9.6. Cost grows far more slowly than the draft does: at `maximum`, 7.5× the characters cost about 1.6× the money.

**Guidance:**
- `wide` is the default and the place to start: twelve calls over six models for roughly ten to fifteen cents on a ~1,400-word draft.
- `economy` is for volume workflows where speed and cost matter more than depth.
- `balanced` is no longer the recommended default (it was, until 2026-09-08). On the ~19,500-word draft its cross-run reproducibility, 6.4%, was indistinguishable from `wide`'s 6.8% at 3.4× the cost ($1.28 against $0.38). Configs that name it are unchanged.
- `thorough` is the sensible step up. Moving its Claude model from `claude-opus-5` to `claude-sonnet-5` captured 84% of `maximum`'s overall reproducibility and 75% of its fact-check reproducibility for under a fifth of the cost; the note above `maximum:` in `presets.yaml` has the study.
- `maximum` is for high-stakes pieces where you want every model running every domain at maximum reasoning. On the long draft it costs about 25× `wide`; what that buys is the last stretch of fact-check reliability, 29.6% cross-run reproducibility against `thorough`'s 22.1%.

---

### Preset detail — exact model and reasoning flags per platform

The tables below show exactly what settings each preset applies to each provider, as `presets.yaml` sets them. Every preset names a model for every provider it runs, so none of them uses the model from your own `models:` entry. **Not set** means the preset sends no such parameter, so the provider's own default applies — which is not the same as off.

#### economy preset — standard thoroughness

| Provider | Model | Reasoning | Notes |
|---|---|---|---|
| openai | `gpt-5.6-luna` | not set (OpenAI's default: `medium`) | Cheapest gpt-5.6 tier |
| gemini | `gemini-2.5-flash` | not set (dynamic thinking) | |
| mistral | `mistral-small-latest` | none | Small variant; does not support `reasoning_effort` |
| perplexity | `sonar` | — | Lightweight search-grounded; no CoT |
| grok | **disabled** | — | Excluded at this cost tier |
| claude | **disabled** | — | Excluded at this cost tier |

#### wide preset — thorough thoroughness *(default)*

| Provider | Model | Reasoning | Notes |
|---|---|---|---|
| openai | `gpt-5.6-luna` | not set (OpenAI's default: `medium`) | Same model as `economy` |
| gemini | `gemini-2.5-flash` | not set (dynamic thinking) | |
| mistral | `mistral-small-latest` | none | |
| perplexity | `sonar` | — | Lightweight search-grounded; no CoT |
| grok | `grok-4.3` | not set | Left unset on purpose: grok-4.3 predates `reasoning_effort` |
| claude | `claude-haiku-4-5-20251001` | none | Does not think unless `thinking_budget` is set |

#### standard preset — retired 2026-09-05

Runs as `wide`, with a warning. See [Cost presets](#cost-presets) above.

#### balanced preset — thorough thoroughness

Measured no more reproducible than `wide` at 3.4× the cost, so it is no longer the default; see the guidance above.

| Provider | Model | Reasoning | Param | Notes |
|---|---|---|---|---|
| openai | `gpt-5.6-terra` | `reasoning_effort` | `"low"` | Light CoT, modest latency increase |
| gemini | `gemini-2.5-flash` | — | not set (dynamic) | Dynamic thinking (model default) |
| mistral | `mistral-medium-3-5` | — | — | Reasoning model; `low`/`medium` not accepted — preset omits effort flag |
| perplexity | `sonar-reasoning-pro` | — | — | CoT+search grounding |
| grok | `grok-4.6` | `reasoning_effort` | `"low"` | Light CoT. Unset would mean `high` |
| claude | `claude-sonnet-5` | `effort` | `"medium"` | Adaptive thinking, on by default; `medium` spends less than the `high` default |

#### thorough preset — thorough thoroughness

| Provider | Model | Reasoning | Param | Notes |
|---|---|---|---|---|
| openai | `gpt-5.6-terra` | `reasoning_effort` | `"high"` | Deep CoT. No `stream_read_timeout` override: the Responses API streams reasoning summaries through the silent phase |
| gemini | `gemini-2.5-pro` | — | not set (dynamic) | Pro; thinking cannot be turned off on this model |
| mistral | `mistral-medium-3-5` | `reasoning_effort` | `"high"` | Deep CoT; only `"high"` or `"none"` accepted on this model. `stream_read_timeout: 200` |
| perplexity | `sonar-reasoning-pro` | — | — | CoT+search grounding. `stream_read_timeout: 500`: its search phase is one long silence before the first byte |
| grok | `grok-4.6` | `reasoning_effort` | `"high"` | What unset already resolves to, stated so the tier says what it does. `xhigh` exists, unmeasured |
| claude | `claude-sonnet-5` | `effort` | `"high"` | Adaptive thinking; `high` is also the API default. Was `claude-opus-5` until 2026-09-08 — see the note above `maximum:` in `presets.yaml` |

#### maximum preset — maximum thoroughness

| Provider | Model | Reasoning | Param | Notes |
|---|---|---|---|---|
| openai | `gpt-5.6-sol` | `reasoning_effort` | `"xhigh"` | Highest reasoning depth. Slow: a median of about 370s per call over 23 saved calls |
| gemini | `gemini-2.5-pro` | `thinking_budget` | `16000` | Pro with a 16K thinking budget (2.5 Pro accepts 128–32,768). `stream_read_timeout: 260`: search grounding and thinking each add a silent phase |
| mistral | `mistral-medium-3-5` | `reasoning_effort` | `"high"` | Deep CoT; only `"high"` or `"none"` accepted. `stream_read_timeout: 200` |
| perplexity | `sonar-reasoning-pro` | — | — | CoT+search grounding. `stream_read_timeout: 500` |
| grok | `grok-4.6` | `reasoning_effort` | `"high"` | Full CoT depth. `xhigh` exists, unmeasured |
| claude | `claude-opus-5` | `effort` | `"high"` | Adaptive thinking; `high` is also the API default. Also sets `web_search: [fact_check]`, so it searches on that domain only |

**Notes on the preset tables:**
- "Not set" means the preset leaves the parameter out and the provider's own default applies. For the gpt-5.6 models that default is `medium`, and `gemini-2.5-flash` thinks dynamically. Neither is off.
- Gemini's `thinking_budget` is only set at `maximum`, on `gemini-2.5-pro`; everywhere else the model uses its dynamic default. `thinking_budget` under `models:` is not one of the keys a preset preserves, so to change it under a preset use `preset_overrides` (below). `0` turns thinking off on the Flash models only: `thorough` and `maximum` run `gemini-2.5-pro`, where thinking cannot be turned off.
- `mistral-medium-3-5` is the reasoning-capable Mistral model (replaces the deprecated `magistral-medium-latest`). It only accepts `reasoning_effort: "high"` or `"none"` — not `"low"` or `"medium"`. The `-latest` suffix variant (`mistral-medium-3-5-latest`) does not exist and returns a 400 error.
- Claude Opus 5 and Sonnet 5 think by default, at effort `high`; the `effort:` parameter sets the depth, and both reject `thinking_budget:`. Other Claude models differ — see [Claude — adaptive vs extended thinking](#claude--adaptive-vs-extended-thinking).
- Provider infrastructure settings (Vertex AI, Azure, credentials) are always preserved regardless of preset.
- If you haven't configured a provider (no API key), the preset silently skips it.

**Per-model cost reference**: list-price arithmetic for one call of 4,000 input and 2,000 output tokens (6,000 in all). Reasoning tokens bill as output, so a call that reasons costs more; the last column links to measured per-call costs where saved runs have them.

| Provider / model | $/call (list price) | Measured, and what moves it |
|---|---:|---|
| gemini-2.5-flash | $0.006 | [Measured](PROVIDERS.md#google-gemini-required); dynamic thinking adds to it |
| gemini-2.5-pro | $0.025 | [Measured](PROVIDERS.md#google-gemini-required); thinking is always on |
| gemini-3.5-flash | $0.024 | Not run by any preset |
| gpt-5.6-luna | $0.003 | [Measured](PROVIDERS.md#openai-required); reasons at `medium` unless an effort is set |
| gpt-5.6-terra | $0.032 | [Measured](PROVIDERS.md#openai-required) |
| gpt-5.6-sol | $0.056 | [Measured](PROVIDERS.md#openai-required); $4/$20 is a promotional rate |
| grok-4.3 | $0.010 | [Measured](PROVIDERS.md#grok--xai-optional) |
| grok-4.6 | $0.020 | [Measured](PROVIDERS.md#grok--xai-optional); reasons at `high` unless an effort is set, and the reasoning tokens dominate the bill |
| mistral-small-latest | $0.002 | [Measured](PROVIDERS.md#mistral-ai-required) |
| mistral-medium-3-5 | $0.021 | [Measured](PROVIDERS.md#mistral-ai-required); reasoning at `high` multiplies it |
| mistral-large-latest | $0.005 | Not run by any preset |
| claude-haiku-4-5 | $0.014 | +$0.02+ (extended thinking) |
| claude-sonnet-5 | $0.028 | Thinks by default, at `high` — [measured per-call cost](PROVIDERS.md#anthropic-claude-optional) |
| claude-opus-5 | $0.070 | Thinks by default, at `high` — [measured per-call cost](PROVIDERS.md#anthropic-claude-optional) |
| perplexity sonar | $0.006 | Plus a $5–$12 per 1,000 requests fee, by search-context size; [measured](PROVIDERS.md#perplexity-ai-optional--recommended) |
| perplexity sonar-pro | $0.042 | Plus $6–$14 per 1,000 requests; not run by any preset |
| perplexity sonar-reasoning-pro | $0.024 | Plus $6–$14 per 1,000 requests; the reasoning trace makes tokens vary widely — [measured](PROVIDERS.md#perplexity-ai-optional--recommended) |
---

### Thoroughness

Controls how many models run each review domain per pipeline run.

| Level | Fact-check | Voice/style | Completeness | Argument | Red team | Approx. calls |
|---|---|---|---|---|---|---|
| `standard` | Gemini | OpenAI | OpenAI | Mistral + Claude* | Mistral + Grok* | 5–7 |
| `thorough` | Gemini + Perplexity* | OpenAI + Claude* | OpenAI + Mistral | Mistral + Claude* + OpenAI | Mistral + Grok* + Claude* | 10–15 |
| `maximum` | All configured | All configured | All configured | All configured | All configured | up to 30 |

\* = only included when that model's key is configured

**`standard`** (default) — current baseline behavior. One primary model per domain. Lowest cost, fastest.

**`thorough`** — two to three well-suited models per domain. Search-grounded models (Gemini + Perplexity) cover fact-check; argument integrity gets three independent perspectives. Recommended when you have Perplexity and Claude configured.

**`maximum`** — every configured model runs every domain. Domain weights sort signal from noise — a general-purpose model running red team at weight 1.0 contributes less than Grok at 1.2, so its findings are ranked lower but still present. Best coverage; ~3–5× the cost of standard.

Per-model `prompts:` overrides take precedence over the thoroughness preset for that model.

---

### Ensemble weighting

The consolidation pass uses a weighted scoring system to identify consensus findings (Section 1) and sort findings within each section (Sections 2–6).

**How it works:**

For each passage flagged by one or more models, the pipeline sums the weights of all models that flagged it. When the sum meets the `consensus_threshold`, the passage is promoted to Section 1 (Consensus). Findings within each section are sorted by source weight descending — higher-weight model findings appear first.

LanguageTool adds a partial vote (`lt_weight`) when it independently flagged the same passage.

**Built-in default weights:**

| Model | Default | fact_check | voice_style | completeness | argument_integrity | red_team |
|---|---|---|---|---|---|---|
| gemini | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| perplexity | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| openai | 1.0 | 1.0 | **1.2** | **1.2** | 1.0 | 1.0 |
| mistral | 1.0 | 1.0 | 1.0 | 1.0 | **1.2** | **1.1** |
| grok | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | **1.2** |
| claude | 1.0 | 1.0 | **1.1** | **1.1** | **1.3** | 1.0 |

Claude receives a 1.3× bonus for argument_integrity based on observed reasoning depth.

Gemini and Perplexity used to carry a flat **1.5×** for fact_check here. That was a guess about which models ground, standing in for whether they actually did — and on 2026-09-03 it was wrong in both directions at once: gemini took the bonus while reporting `grounding_available: False`, and openai ran a real search on the flat 1.0. The bonus is now applied by reading the result rather than the model name:

```yaml
ensemble:
  grounding_bonus: 1.5       # multiplier when a fact_check call actually consulted live sources
```

**Measured 2026-09-05 — what these weights actually change.** Re-scoring 12 captured ensembles with the default table against a flat 1.0 for every model produced **identical Section 1 membership in all 12**, while changing the *order* in 10 of them. The same held for `grounding_bonus` at 1.5 versus 1.0: no membership change.

The reason is that `consensus_threshold: 2.0` sits below the point where weights discriminate. Two models at 1.0 already reach exactly 2.0, so any passage with two distinct voters clears the bar whatever the weights say, and a bonus can only move something already over the line further over it. Sweeping the threshold, output was identical at 1.0, 1.5 and 2.0, and only began to cut at 2.5.

So today the weights table is a **ranking** control, not a gate, and what actually decides Section 1 membership is `consensus_min_models`. If you want the weights to affect what surfaces rather than the order it surfaces in, raise `consensus_threshold` to 2.5 or above so a bare two-model agreement no longer clears it on its own.

**What reaches Section 1 (defaults: threshold 2.0, min_models 2):**

- One model, however heavily weighted → **not** consensus. `consensus_min_models` blocks it whatever the sum reaches, which is the point: a single model emitting two red_team sub-findings on one passage once totalled 2.2 and published itself as consensus.
- Two distinct models at 1.0 each → 2.0, meets the threshold → consensus.
- Anything weighted above 1.0 → also consensus, but it was already over the line at 1.0.

That third case is why the weights do not currently gate anything. LanguageTool counts as one of the distinct sources when it independently flagged the same passage.

**Configuring weights:**

```yaml
ensemble:
  consensus_min_models: 2    # distinct sources required — the actual gate
  consensus_threshold: 2.0   # weighted sum required alongside that count
  lt_weight: 0.5             # LanguageTool partial vote
  grounding_bonus: 1.5       # applied when a fact_check call really searched

  weights:
    # Override only what you want to change.
    # Omitted keys use built-in defaults.
    gemini:
      fact_check: 2.0        # trust Gemini fact-check findings more strongly
    openai:
      default: 0.8           # lower all OpenAI domain weights
      voice_style: 1.5       # but keep voice_style at 1.5
```

---

### Customizing a preset (preset_overrides)

`preset_overrides` lets you adjust individual fields of a cost preset without specifying the entire config from scratch. Think of it as: "use `balanced`, but change these specific things."

```yaml
pipeline:
  cost_preset: balanced
  preset_overrides:
    openai:
      reasoning_effort: high     # balanced uses "low"; bump to "high"
    claude:
      model: claude-opus-5       # balanced uses sonnet-5; use opus-5 instead
      effort: high               # balanced sets medium
    grok:
      enabled: false             # skip Grok for this run
```

**How it works:**
1. The preset is applied first (sets model + reasoning + thoroughness for all providers)
2. `preset_overrides` is then applied on top — only the keys you list are changed
3. Keys you don't mention keep the preset's value
4. Setting a key to `null` neutralizes it (adapters guard with `if value:`, so `null` disables it)
5. Only providers already configured in `models:` are affected — overrides for unconfigured providers are silently ignored

**What you can override per provider:**

| Key | Applies to | Values |
|---|---|---|
| `model` | all | any model ID string |
| `reasoning_effort` | openai | `none \| low \| medium \| high \| xhigh` |
| `reasoning_effort` | grok (`grok-4.6`, `grok-4.5`) | `low \| medium \| high \| xhigh` |
| `reasoning_effort` | mistral (`mistral-medium-3-5` only) | `"high"` or `"none"` only — `"low"`/`"medium"` return 400 |
| `thinking_budget` | gemini, claude-haiku | integer (tokens), or `null` to drop the key. On gemini, `0` turns thinking off for the Flash models only |
| `effort` | claude (opus, sonnet, fable) | `low \| medium \| high` |
| `enabled` | all | `true \| false` |
| `timeout_seconds` | all | integer (seconds) |
| `prompts` | all | list of domain names |

**Common recipes:**

```yaml
# Use maximum, but run OpenAI on terra instead of sol:
pipeline:
  cost_preset: maximum
  preset_overrides:
    openai:
      model: gpt-5.6-terra      # maximum uses gpt-5.6-sol
      reasoning_effort: high    # and xhigh

# Use maximum preset but save money by keeping gemini on 2.5-flash:
pipeline:
  cost_preset: maximum
  preset_overrides:
    gemini:
      model: gemini-2.5-flash   # maximum uses gemini-2.5-pro
      thinking_budget: 8192     # half of maximum's 16000; 2.5 Flash accepts 1–24,576

# Use balanced but turn Mistral's reasoning on:
pipeline:
  cost_preset: balanced
  preset_overrides:
    mistral:
      reasoning_effort: high    # balanced sends no effort; "high" and "none" are the only values accepted

# Use wide but add Perplexity reasoning (normally wide uses sonar):
pipeline:
  cost_preset: wide
  preset_overrides:
    perplexity:
      model: sonar-reasoning-pro
```

---

### Live model discovery

Run model discovery any time you want to check whether newer models are available from any provider — without reading every provider's changelog yourself. It calls each provider's live models API using your existing API keys.

```powershell
uv run ci-discover
uv run ci-discover --provider openai
uv run ci-discover --provider gemini --provider claude
```

**Example output** — an excerpt from a real run on 2026-09-19 against a `maximum`-preset config. A full report lists every model each provider returns (85 for OpenAI that day), so rows are elided with `...`, and the GCP project ID is replaced by a placeholder:

```
Model Discovery Report — 2026-09-19
Built-in registry last updated: 2026-08-18 (32 days ago)
======================================================================

OpenAI  (configured: gpt-5.6-sol)
        gpt-5.6-terra  2026-06-23  (2mo ago)
    ✓  gpt-5.6-sol  2026-06-23  (2mo ago)  ← configured
    ⚠  gpt-5.5  2026-04-22  (5mo ago)  ⚠ superseded → gpt-5.6-sol
    ⚠  gpt-5.4  2026-03-05  (6mo ago)  ⚠ superseded → gpt-5.6-terra
        ...

Gemini (AI Studio)  (configured: gemini-2.5-pro)
  SKIP  Gemini is configured via Vertex AI (project=your-gcp-project-id location=us-central1).
       Model listing against Vertex AI requires the gcloud SDK and is not supported here.
       Check https://ai.google.dev/models for available Gemini models.
       Configured model: 'gemini-2.5-pro'

Mistral  (configured: mistral-medium-3-5)
    ⚠  magistral-medium-latest  2026-09-19  (today)  ⚠ superseded → mistral-medium-3-5
    ✓  mistral-medium-3-5  2026-09-19  (today)  ← configured
        ...

Anthropic / Claude  (configured: claude-opus-5)
   NEW  claude-fable-5-1  2026-08-28  (22d ago)  ← newer than configured
    ✓  claude-opus-5  2026-07-24  (1mo ago)  ← configured
        claude-sonnet-5  2026-06-29  (2mo ago)
        ...
    ⚠  claude-opus-4-8  2026-05-28  (3mo ago)  ⚠ superseded → claude-opus-5
        ...

Grok / xAI  (configured: grok-4.6)
    ✓  grok-4.6  2026-08-06  (1mo ago)  ← configured
        grok-4.5  2026-06-29  (2mo ago)
        ...
    ⚠  grok-4.20-0309-reasoning  2026-03-09  (6mo ago)  ⚠ superseded → grok-4.6
        ...

Perplexity  (configured: sonar-reasoning-pro)
  (No models endpoint — showing documented set from June 2026)
        sonar-deep-research  (no date)
    ✓  sonar-reasoning-pro  (no date)  ← configured
        sonar-pro  (no date)
        sonar  (no date)
```

**What each marker means:**
- `NEW` — model exists at the provider and has a creation date newer than your configured model
- `✓` — your currently configured model
- `⚠` — model ID appears in the built-in superseded registry
- (none) — available, not configured, not flagged

**Notes on Vertex AI:** Gemini via Vertex AI cannot be queried for model lists without the gcloud SDK. The script notes this and skips. Check [Google AI for Developers](https://ai.google.dev/models) for what exists, and Google Cloud's [Model versions and lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions) page for what is being retired: the two disagree. On 2026-09-19 the Vertex AI page listed `gemini-2.5-pro`, `gemini-2.5-flash` and `gemini-2.5-flash-lite` for retirement on 2026-10-20, while the Gemini API's [deprecations](https://ai.google.dev/gemini-api/docs/deprecations) page listed no shutdown date for them.

**Notes on Perplexity:** Perplexity does not publish a models list API. The script shows the documented set as a static fallback. That set is from June 2026 and does not reflect Perplexity's announced end of Sonar chat-completions support on 2026-09-27 (see [PROVIDERS.md](PROVIDERS.md#perplexity-ai-optional--recommended)).

**After discovery, to update your configured model:**
1. Edit `models:` in `configs/user.yaml` (or update the `cost_preset` which sets models automatically)
2. Run `uv run ci-check --publication your_pub` to verify the new model responds
3. Optionally add the old model to `superseded:` in [`model_registry.yaml`](../packages/ci-core/src/ci_core/configs/model_registry.yaml)

---

### Model currency detection

Every pipeline run checks your configured model IDs against a built-in registry of known-current and superseded models. Results appear in the terminal summary and the saved report JSON.

**Three signal levels:**

| Signal | Condition | What it means |
|---|---|---|
| Superseded warning | Configured model ID is in the deprecated list | Model has a newer replacement; update user.yaml |
| Upgrade notice | Configured model has a newer variant available | Current model is fine; newer one exists if you want it |
| Registry staleness | Registry data is 60+ days old | Re-check provider docs for new releases |

**Example terminal output** — for a config that still names `gpt-4o`, as it printed on 2026-09-19:
```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
MODEL CURRENCY: Outdated model(s) detected — update user.yaml
  openai: 'gpt-4o' → replace with 'gpt-5.6-terra' (GPT-5.6 family available (2026-07-09))
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

Model registry: current (last updated 2026-08-18, 32 days ago)
```

At 60 or more days the last line becomes a note, `Note: model registry last updated <date> (<N> days ago). Consider re-checking for newer models.`, and at 120 or more it becomes a boxed `MODEL REGISTRY is <N> days old` warning.

**Keeping the registry current:**

The registry lives in [`ci-core`'s `model_registry.yaml`](../packages/ci-core/src/ci_core/configs/model_registry.yaml). After any provider model audit:
1. Add entries to `superseded:` for models that have been replaced
2. Update `newer_available:` for current models that have newer variants
3. Bump `registry_date:` to today's date

The registry date drives the staleness notice — bumping it resets the 60-day clock even if you haven't changed any model entries. No code change is needed; the pipeline reloads the YAML at each run.

---

## Publication config

Open `configs/your_publication_name.yaml`. Key fields:

| Field | What to put there |
|---|---|
| `publication_description` | One paragraph: what you cover, who reads it, what makes a piece unpublishable |
| `audience.primary` | Who reads it, what they know, what makes them stop reading |
| `author_name` | Optional — who "I" refers to in your drafts, by name. Citation verification needs it to check first-person claims against a source page. The handoff's `Author:` line overrides it per article; this is the only source for `--raw-draft` and `--url`. |
| `style_profile` | Your characteristic style — see PLAYBOOK.md for how to develop this (the legacy key `voice_profile` is still accepted) |
| `style_rules.banned_words` | Words you never want in your published writing |
| `style_rules.banned_phrases` | Phrases you never want |
| `seo_rules.title_max_chars` | SEO title length ceiling (default 60) |
| `seo_rules.title_min_chars` | SEO title length floor (default 20) |
| `seo_rules.min_article_words` | Minimum word count before thin-content warning (default 300) |
| `seo_rules.meta_description_max_chars` | Meta description ceiling (default 155) |
| `seo_rules.meta_description_min_chars` | Meta description floor (default 70) |
| `seo_rules.suggestions` | Whether to run the SEO suggestion pass (default `true`) |
| `seo_rules.content_review` | Whether to run the SEO structure review (default `true`) |
| `wordpress.site_url` | `https://yoursite.com` |
| `wordpress.username` | Your WordPress login username |
| `wordpress.application_password` | The application password from your WordPress profile |
| `api_keys` | Optional — overrides specific providers' credentials from `user.yaml` for this publication only. See [API key precedence](#api-key-precedence). |

Any top-level key not in this table is an error. That is deliberate: a misspelled key used to read as an absent setting, so `authorname` silently cost the run its author and nothing anywhere said so. The error names the key, suggests the intended spelling when it is close, and lists what is valid.

If you need a key that is deliberately not this pipeline's -- a note to a colleague, or a value another tool reads out of the same file -- prefix it with `x_`. Anything under that prefix is passed over without validation, following the same idea as OpenAPI's `x-` specification extensions.

**`seo_rules`** is optional — omit it to use the defaults. Add it when your publication has different SEO standards from the defaults:

```yaml
seo_rules:
  title_max_chars: 55       # tighter ceiling for a publication with long-title history
  title_min_chars: 20
  min_article_words: 500    # longer minimum for long-form-only publication
```

---

### SEO suggestions

The pre-analysis SEO pass reports what's missing. The suggestion pass proposes
values for it: the publication handoff's SEO METADATA fields that
`adapters/cms/wordpress.py` pushes to Rank Math (all but SEO title, which falls
back to the OG title), plus a schema type, which is advice only:

| Field | What you get | Pushed as |
|---|---|---|
| Focus keyword | **3–5 candidates**, strongest first, each with a one-line rationale — including whether the article actually uses the phrase | `rank_math_focus_keyword` |
| Meta description | A draft under `seo_rules.meta_description_max_chars` | `rank_math_description` |
| OG title | A shorter title, but only when the article title exceeds `seo_rules.title_max_chars` — otherwise the field reports that the article title is used as-is | `rank_math_facebook_title` and `rank_math_twitter_title` when `rank_math.auto_set_og_tags` is on; also `rank_math_title` when the handoff gives no SEO title |
| OG description | Social-card text, but only when a distinctly social framing beats reusing the meta description — otherwise the field reports that the meta description is used. Held to the same character limit | `rank_math_facebook_description` and `rank_math_twitter_description` when `rank_math.auto_set_og_tags` is on |
| Schema type | `Article`, `NewsArticle`, or `BlogPosting` with a one-line rationale, flagged when it differs from `rank_math.default_schema_type` | Not pushed, and the handoff has no field for it. Rank Math applies its default for the post type; set a different one in the post's Rank Math Schema tab. `AboutPage` and `ContactPage` are site-wide, under Rank Math SEO → Titles & Meta → Local SEO |

Every field reports an outcome. The two with defaults in the push (OG title, OG
description) name the default that would take effect rather than going silent,
so you can see the field was considered. A value over its character limit is
reported with its count and flagged — never truncated into a dangling clause,
and never dropped.

It runs during a `--draft` review, not at publish time, so the output feeds the
[revision round-trip](../packages/ci-article-review/src/ci_article_review/handoff_templates/revise_after_review_prompt.md)
and can be regenerated for free on every pass. Seeing the intended keyword this
early is also the point at which you can notice that the article never actually
uses the phrase it should rank for. It appears in three places: the console
summary under `SEO issues`, an `## SEO Suggestions` section at the end of
`run_N_<timestamp>_review.md`, and `pre_analysis.seo.suggestions` in the report
JSON.

At publish time it runs only as a backstop: if a publication handoff reaches
`--publish` with SEO METADATA still missing a focus keyword or meta description,
suggestions print before the WordPress confirmation prompt so you can cancel,
fill the handoff in, and re-run. A field counts as missing when it is blank,
still on the template's bracketed placeholder
(`[keyword or phrase | or: derive from primary claim]`), or one of the
template's alternatives written out bare (`derive from primary claim`). The
parser drops all three rather than send them to Rank Math. A value written
entirely inside brackets reads as a placeholder too, and is logged as dropped,
so write a real value without them. One that only begins with a bracketed tag,
such as `[Case Study] How We Cut Costs`, is kept.

**Nothing here is applied automatically** — not to a config, not to a handoff,
not to WordPress. Keyword choice is a strategic decision about what you want to
rank for, so the candidates are yours to pick from. (Older configs carried
`rank_math.derive_focus_keyword_if_missing` and
`derive_meta_description_if_missing`; no code ever read them, and they have
been removed rather than wired up to write values on your behalf.)

**Cost and failure behavior.** One call to a small fast model
(`mistral-small-latest`, the same model the citation relevance verifier uses),
roughly $0.0002 per run, tracked in the report's `cost_summary` under the
`seo_suggestions` pass. It needs a Mistral API key; without one it is skipped
with that reason stated. A failed call is logged and the run continues — a
suggestion never fails a review.

**Turning it off:**

```yaml
seo_rules:
  suggestions: false        # no suggestion call on any run
```

Or for a single run, without editing the config:

```bash
uv run ci-review --draft handoff.md --publication your_publication_name --no-seo-suggestions
```

`--no-seo-suggestions` turns off **both** SEO model calls (suggestions and the
structure review below) — it's about not paying for the SEO extras, not about
one of the two. The config keys are independent if you want only one.

With the pass off, the `no_meta_description` finding still appears — it just
states where a meta description becomes due (the publication handoff's SEO
METADATA block) rather than offering a draft of one.

---

### On-page checks

These are deterministic — no model call, no cost, and a finding is either true
or it isn't. They run as part of the pre-analysis SEO pass and appear in the
same `SEO issues` list:

| Check | Fires when |
|---|---|
| `missing_image_alt` | An image (markdown or raw `<img>`) has no alt text — the only description a screen reader or image-search crawler gets. One finding for the batch, not one per image |
| `weak_anchor_text` | Link text is `click here`, `read more`, a bare URL, or similar — text that promises nothing about the destination |
| `no_internal_links` | The article links out but never to your own site. Requires `wordpress.site_url`; skipped entirely without it rather than guessed at |
| `title_h1_mismatch` | The handoff title and the article's H1 differ. Deliberate is fine — but it is just as often an edit applied to one and not the other |
| `meta_description_too_long` / `_too_short` | A **supplied** description falls outside the configured range. Only presence was checked before |

**Keyword usage** is reported alongside each suggested keyword candidate: does
the phrase appear in your title, your headings, your opening, and how many
times in the body. A candidate the article never uses is called out plainly —
that is the finding worth acting on, and it costs nothing to compute.

Matching is literal (casefolded, whitespace-collapsed) with no stemming, so
"interconnection queues" does not match a candidate of "interconnection queue".
That slightly overstates the problem, which beats a fuzzy match reporting a
phrase as present when a reader would not find it.

Deliberately **not** checked: keyword density. It has not been a ranking signal
for well over a decade, and writing to a density target makes prose worse.
Paragraph and sentence length are also absent here — `readability.py` already
reports both, including `longest_paragraph_words`.

---

### SEO structure review

A second cheap model call, judging the article the way someone who just
arrived from a search result sees it — and has not yet decided to stay. It
answers three questions only:

- **Headings** — does each one tell a scanning reader what is in the section
  below it, or could it sit above any section of any article?
- **Opening** — does it deliver what the title and keyword promise, or warm up
  first and bury the answer?
- **Title promise** — does the article deliver what the title claims?

Findings carry a concrete suggestion (an actual replacement heading, not "make
it more descriptive") and quote the passage they are about. **Zero findings is
the expected result on a sound article** and renders as such, rather than as
an empty section or a manufactured nit.

The prompt explicitly tells the model to leave missing information, weak
arguments, factual doubts, and tone alone — the `completeness`, `argument_integrity`,
`fact_check`, and `voice_style` ensemble domains already cover those, and
repeating them here would bury the structural findings. Findings that come
back outside the three categories are dropped rather than passed through.

Cost is tracked separately from the suggestion pass, under the
`seo_content_review` entry in `cost_summary`. To turn off only this one:

```yaml
seo_rules:
  content_review: false
```

---

You can use an environment variable for the application password:

```yaml
wordpress:
  application_password: ${WP_APPLICATION_PASSWORD}
```

See `configs/examples/` for complete worked examples.

---

## Delta assessment

When a prior run's report exists, the pipeline compares the new draft against it and recommends whether to re-run. Controls:

```yaml
delta:
  word_change_threshold_pct: 15        # re-run if >15% of words changed vs prior run
  claim_change_triggers_rerun: true    # re-run if the handoff PRIMARY CLAIM changed
  structure_change_triggers_rerun: true # re-run if the heading outline changed
```

A re-run is recommended when **any** of these is true: word change exceeds the threshold; a new consensus flag appeared; the `PRIMARY CLAIM` differs from the prior run (when `claim_change_triggers_rerun`); or the markdown heading outline was added to, removed from, renamed, or reordered (when `structure_change_triggers_rerun`).

- **Claim comparison** is whitespace- and case-insensitive, and only fires when both runs supplied a claim — reports from before claim tracking won't trigger a spurious re-run.
- **Structure comparison** looks only at headings (`#`–`######`), so body-only edits don't count as a structural change.
- **Which run is "prior"** is decided by execution time, not by the handoff's `Pipeline run:` number. That number is author-declared, so running the same handoff twice writes two reports at the same run number; the delta always compares against the report from the execution that immediately preceded this one, whatever number it declared. The report it picked is recorded in the delta as `compared_against` and printed in the console summary and the markdown review as `Compared against: run_2_20260810_005452_report.json`.
- **Which directory it looks in** is the article's history key — see below. A revised title with no history key means the delta looks in a brand-new directory and finds nothing to compare against.

---

## History key

Every run is saved under `pipeline_history/<slug>/`, and that slug is what ties an article's runs together. By default it is slugged from the title, which makes the title the article's primary key — and titles get revised.

When that happens the history forks. One article titled "…They Have Eight of Them.", then "…Ten of Them.", then "…Twelve of Them." produced three directories. Nothing errors; the runs simply stop finding each other:

- **Delta comparison** looks for the prior run in the new directory, finds none, and reports a revised article as a first run.
- **`ci-voice-patterns`** counts distinct articles by directory to decide whether a phrasing habit recurs across your body of work. Its threshold is three articles, so one article split three ways can clear that bar on its own and promote a pattern that only ever appeared in one piece.

Pin it in the handoff:

```
Article: Data Centers Don't Have an Environmental Record. They Have Twelve of Them.
History key: dc-environment
```

Set it once, when you start the piece, and leave it alone however much the headline moves. Omit it and the title is used exactly as before, so existing handoffs keep working.

---

## URL input mode

Instead of a local handoff document, you can point the pipeline at an already
published web page:

```powershell
uv run ci-review --url https://example.com/some-post --publication your_publication_name
```

`--url` is mutually exclusive with `--draft` and `--publish`, and still requires
`--publication` (the publication config supplies the voice profile, audience,
and style rules the review prompts need).

### How a handoff is synthesized

A normal draft run reads a handoff document and parses many fields
(`PRIMARY CLAIM`, `PRE-DRAFT ANALYSIS SUMMARY`, `TARGET AUDIENCE`, etc.). The
pipeline body only *strictly* needs the article **title** and **draft body** —
everything else is optional. URL mode therefore builds an in-memory handoff with
just those two fields plus `run_number: 1`, then feeds it into the exact same
review path:

```python
{"title": "<page title>", "draft": "<extracted article text>", "run_number": 1}
```

### What it can and can't infer

| Field | URL mode |
|---|---|
| `title` | Page `<title>`, or the first `<h1>` if there's no title tag |
| `draft` | Extracted main-article markdown (headings preserved) |
| `primary_claim` | **Not inferred** — empty; the delta "claim changed" check won't fire |
| `pre_draft_analysis` | **Not inferred** — argument/completeness models get less context |
| `target_audience` / `additional_context` | **Not inferred** — comes only from the publication config |
| `seo` | **Not inferred** — there's no author-supplied SEO block |

If you want the richer author-intent context, use a `--draft` handoff instead.

### Fetching and extraction

- **SSRF guard.** Only public hosts are fetched. The same check used for link
  validation (`analysis/links.py`) rejects loopback, private, link-local, and
  cloud-metadata (`169.254.169.254`) addresses *before* any request is made.
- **Extraction.** [`trafilatura`](https://trafilatura.readthedocs.io/) is used
  for main-content extraction. It is a required dependency (`ci_core.extract`),
  shared with citation verification, which depends on extraction quality for
  correctness. If it is somehow unavailable, a built-in heuristic strips
  `<script>`/`<style>`/`<nav>`/`<header>`/`<footer>`/`<aside>`, prefers the
  `<article>` or `<main>` element, and keeps `<h1>`–`<h3>` as markdown headings
  (the SEO and structure checks key off heading markup). The fallback is
  markedly weaker on pages with no `<article>`/`<main>` region, where it can
  return navigation chrome instead of body text.
- **Thin-extraction warning.** If fewer than ~200 words are extracted, the run
  warns loudly — usually a paywall, a JavaScript-rendered page, or a bot-block —
  and proceeds on whatever content was recovered.
