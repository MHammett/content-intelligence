# Provider Setup

API keys and account setup for every service the pipeline can use.

**Required:** OpenAI, Gemini, Mistral  
**Optional:** Perplexity, Grok, Claude, LanguageTool, WordPress

Skip optional sections for services you don't plan to use. The pipeline detects missing keys and skips those passes automatically — nothing will break if a section is absent.

---

## OpenAI (required)

**Where:** https://platform.openai.com

**Steps:**
1. Create an account at https://platform.openai.com/signup
2. Go to API keys: https://platform.openai.com/api-keys
3. Click **Create new secret key**, give it a name, and copy it immediately — shown once only
4. Add a payment method: https://platform.openai.com/account/billing
5. Load a minimum of $5 in credits to start

**What you need:**
- API key: the key you just created

**Current models (September 2026):**

| Model | Input | Output | Context | Presets | Notes |
|---|---|---|---|---|---|
| `gpt-5.6-sol` | $4.00/MTok | $20/MTok | 1.05M tokens | `maximum` (`reasoning_effort: xhigh`) | Highest capability. The rate is promotional: OpenAI's pricing page says it is available "at least through November 21, 2026" |
| `gpt-5.6-terra` | $2.00/MTok | $12/MTok | 1.05M tokens | `balanced` (`low`), `thorough` (`high`) | Best value; **recommended default** |
| `gpt-5.6-luna` | $0.20/MTok | $1.20/MTok | 1.05M tokens | `economy`, `wide` (no effort set) | Economy option |

The gpt-5.6 family replaced gpt-5.5, gpt-5.4 and gpt-5.4-mini in the presets on 2026-08-18, at matching price tiers. Those three still work, and `pricing.yaml` still prices them ($5/$30, $2.50/$15 and $0.75/$4.50 per MTok, matching OpenAI's pricing page), for configs that name them.

Reasoning is controlled via `reasoning_effort: none | low | medium | high | xhigh` in the model config. All three gpt-5.6 models default to `medium`, and the pipeline sends nothing when the setting is omitted, so an unset effort runs at `medium`, not `none`.

**Expected cost:** measured per review call, tokens only, from saved runs and re-priced at the current rates: `gpt-5.6-luna` $0.004–$0.006, median $0.005 (12 calls, all on a 9,456-character draft); `gpt-5.6-terra` at `high` $0.064–$0.066 (3 calls, one 18,167-character draft); `gpt-5.6-sol` at `xhigh` $0.08–$1.30, median $0.33 (23 calls, drafts of 2,182–27,113 characters). Speed follows the same order: about 25s for luna, about 370s for sol at `xhigh`. Live web search, when you turn it on, is billed on top: OpenAI's pricing page lists $10 per 1,000 calls plus the search-content tokens at model rates.

**What it does:** Voice/style review (detects AI-generated phrasing, hedging language, banned words) and completeness analysis (finds gaps a technically literate critic would notice). Optional web-search upgrade — see [CONFIGURATION.md](CONFIGURATION.md#openai-web-search).

---

## Google Gemini (required)

Gemini runs the fact-check pass with live Google Search grounding. There are two access paths. Start with AI Studio; move to Vertex AI if you hit consistent 503 capacity errors.

### Option A — AI Studio (quick start, free tier available)

**Where:** https://aistudio.google.com

**Steps:**
1. Sign in with a Google account
2. Click **Get API key** in the left sidebar
3. Click **Create API key**
4. Copy the key shown

**What you need:**
- API key: the key you just generated

**Billing:** No credit card is required for the free tier. It covers the Flash models (2.5 Flash, 2.5 Flash-Lite and the 3.x Flash models) but not `gemini-2.5-pro`, which `thorough` and `maximum` run. Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing) also marks free-tier content as used to improve its products and paid-tier content as not. What this pipeline sends is unpublished drafts, so if that matters to you, use a billing-enabled key or Vertex AI.

**Limitation:** AI Studio draws from a shared capacity pool. At peak hours you may get 503 errors. If that happens consistently, use Vertex AI instead.

**Current models (September 2026):**

| Model | Input | Output | Notes |
|---|---|---|---|
| `gemini-2.5-flash` | $0.30/MTok | $2.50/MTok | Best price-performance; **recommended default**; what `economy`, `wide` and `balanced` run |
| `gemini-2.5-pro` | $1.25/MTok ($2.50 over 200K-token prompts) | $10/MTok ($15 over 200K) | What `thorough` and `maximum` run; no free tier; thinking cannot be turned off |
| `gemini-3.5-flash` | $1.50/MTok | $9.00/MTok | GA; Google's named replacement for 2.5 Pro on Vertex AI; in no preset |

Google's newer Flash models, 3.6, 3.7 and 3.8, are also GA on the Gemini API, at promotional prices that run through 2026-12-31 (see the pricing page). No preset runs them, and `pricing.yaml` has no row for them yet, so the pipeline's cost report would price them at its fallback rate.

**Retirement.** Google Cloud's [Model versions and lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions) page lists `gemini-2.5-pro`, `gemini-2.5-flash` and `gemini-2.5-flash-lite` for retirement on Vertex AI on **2026-10-20**, and names Gemini 3.5 Flash, 3.5 Flash-Lite or 3.1 Flash-Lite as replacements. Google says listed retirement dates may be extended but will not move earlier. The Gemini API's [deprecations](https://ai.google.dev/gemini-api/docs/deprecations) page listed no shutdown date for them on 2026-09-19. Every shipped preset still runs a 2.5 model, and `presets.yaml` has not been changed; Vertex AI (Option B below) is where the date applies. Vertex AI's pricing page lists Gemini 3.5 Flash under "Global", so check that your location serves a model before you switch to it.

`thinking_budget` controls reasoning token allocation on the 2.5 models, and it is the only Gemini thinking setting this pipeline sends: 2.5 Flash takes 1–24,576 tokens, 2.5 Flash-Lite 512–24,576, 2.5 Pro 128–32,768, and unset each thinks dynamically, up to 8,192. `0` turns thinking off on 2.5 Flash and Flash-Lite. **Thinking cannot be turned off on 2.5 Pro.** Gemini 3.x models use a different parameter, `thinking_level`, which the pipeline does not send. Details and Google's source are in [CONFIGURATION.md](CONFIGURATION.md#gemini--thinking_budget).

**Expected cost:** measured per review call, tokens only, from saved runs and re-priced at the current rates: `gemini-2.5-flash` $0.007–$0.033, median $0.023 (14 calls, drafts of 9,456–73,786 characters); `gemini-2.5-pro` $0.02–$0.15, median $0.05 (177 calls, 2,182–135,514 characters). Across a whole run, Gemini's share was $0.02–$0.07 at `wide` (four runs, a 9,456-character draft), $0.17 at `thorough` (one run, 18,167 characters) and $0.22–$0.49 at `maximum` (four runs, 3,999–27,113 characters). Search grounding is separate: every Gemini call here is grounded, and Google's pricing page lists a daily allowance of grounded prompts at no charge (1,500 for the 2.5 Flash models combined, 10,000 for 2.5 Pro on Vertex AI) before per-1,000 charges apply. A run makes up to five Gemini calls, one per domain, before any retries.

**Config:**
```yaml
models:
  gemini: gemini-2.5-flash
```

---

### Option B — Vertex AI (reserved capacity, no 503s)

Vertex AI uses a separate capacity pool not shared with free-tier traffic.

**Steps:**
1. Create or select a GCP project at https://console.cloud.google.com
2. Enable billing — Vertex AI calls fail without it even if the API is enabled
3. Enable the Agent Platform API (formerly Vertex AI API). Either run:
   ```
   gcloud services enable aiplatform.googleapis.com
   ```
   Or open https://console.cloud.google.com/apis/library/aiplatform.googleapis.com and click Enable

**Credentials — easiest for local use (Application Default Credentials):**

Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install

On Windows, use the Google Cloud CLI Installer (`.exe`) linked on that page — no WSL required. It adds `gcloud` to your PATH and works in Command Prompt and PowerShell. Then run:

```
gcloud auth application-default login
```

This opens a browser to complete sign-in and stores credentials in your user profile. No file to manage.

**Credentials — service account (for servers or CI):**
1. Go to **IAM & Admin → Service Accounts** in your GCP project
2. Click **Create Service Account**, give it a name, click **Create and Continue**
3. In the "Grant this service account access to project" step, search for **Agent Platform User**, select it, click **Continue**, then **Done**
4. Click the service account in the list to open it
5. Go to the **Keys** tab
6. Click **Add Key → Create new key**
7. Select **JSON** and click **Create** — the key file downloads to your Downloads folder
8. Move it somewhere permanent, e.g.:
   ```
   C:\Users\your-username\gcp-keys\my-project-vertex.json
   ```
9. Reference it in `configs/user.yaml`:
   ```yaml
   models:
     gemini:
       provider: vertex_ai
       model: gemini-2.5-flash
       project: your-gcp-project-id
       location: us-central1
       credentials_file: C:\Users\your-username\gcp-keys\my-project-vertex.json
   ```

**Install the auth library:**
```
pip install "google-auth>=2.22.0,<3.0"
```

**What you need:**
- GCP **project ID** — the unique identifier like `my-project-123`, not the display name. Find it in the project selector dropdown in the GCP console (smaller text below the project name, also visible in the browser URL).
- Region (e.g. `us-central1`)
- ADC set up, or a service account JSON file

**Config (ADC, no file needed):**
```yaml
models:
  gemini:
    provider: vertex_ai
    model: gemini-2.5-flash
    project: your-gcp-project-id
    location: us-central1
```

**Billing:** The same per-token list prices as the AI Studio paid tier for 2.5 Pro, 2.5 Flash and 3.5 Flash (Vertex AI's pricing page lists them alike). What a run costs is under **Expected cost** in the Gemini section above: a few cents at `wide`, and tens of cents at `maximum`.

**Retirement:** the 2.5 models are listed for retirement on Vertex AI on 2026-10-20; see **Retirement** above.

---

## Mistral AI (required)

**Where:** https://console.mistral.ai

**Steps:**
1. Create an account
2. Go to **API keys** in the left sidebar
3. Click **Create new key**, name it, copy the key
4. Add a payment method under **Billing**

**What you need:**
- API key: the key you created

**Current models (September 2026):**

| Model | Input | Output | Notes |
|---|---|---|---|
| `mistral-large-latest` | $0.50/MTok | $1.50/MTok | Mistral Large 3; flagship non-reasoning model; **recommended default**; in no preset |
| `mistral-medium-3-5` | $1.50/MTok | $7.50/MTok | Mistral Medium 3.5, 256K context; reasoning model, replaces deprecated `magistral-medium-latest`; what `balanced`, `thorough` and `maximum` run |
| `mistral-small-latest` | $0.15/MTok | $0.60/MTok | Mistral Small 4; economy option, reduced depth; what `economy` and `wide` run, and the model behind citation verification and both SEO passes |

Prices are from Mistral's [pricing page](https://mistral.ai/pricing/api/), checked 2026-09-19, and `pricing.yaml` carries the same rates. A `-latest` alias moves to each new GA model, and its price moves with it, so re-check the two `-latest` rows whenever Mistral ships a new Small or Large.

**Reasoning constraints (important):** `mistral-medium-3-5` is the only Mistral model that supports `reasoning_effort`. It only accepts `"high"` or `"none"` — `"low"` and `"medium"` return a 400 error. Standard models (`mistral-large-latest`, `mistral-small-latest`) reject `reasoning_effort` entirely. The `-latest` suffix variant (`mistral-medium-3-5-latest`) does not exist and returns a 400 error.

```yaml
# Standard (no reasoning)
models:
  mistral: mistral-large-latest

# Reasoning
models:
  mistral:
    model: mistral-medium-3-5
    reasoning_effort: high      # "high" or "none" only
    timeout_seconds: 240
```

**Expected cost:** measured per review call, tokens only, from saved runs and re-priced at the current rates: `mistral-small-latest` $0.001–$0.005, median $0.002 (28 calls, drafts of 3,999–73,786 characters); `mistral-medium-3-5` at `high` $0.03–$0.33, median $0.12 (132 calls, 2,182–135,514 characters), where the reasoning trace dominates the bill (a median of about 10,700 completion tokens per call).

**What it does:** Argument integrity review (logical gaps, unstated assumptions, conclusions that outrun their evidence) and red team analysis (most attackable claim, audience alienation risk, credibility risk). European company, architecture independent from Google and OpenAI — independent analytical perspective matters.

---

## Perplexity AI (optional — recommended)

Perplexity's sonar models run every response through live web search by default. Adding a Perplexity key gives you a second independent search-grounded fact-checker alongside Gemini. Both get the `grounding_bonus` (1.5×) on `fact_check` when the call actually consulted live sources; see [Ensemble weighting](CONFIGURATION.md#ensemble-weighting).

> **Sonar is being retired.** Perplexity's documentation says Sonar "will be supported until September 27, 2026", and recommends moving existing Sonar Chat Completions usage to its [Agent API](https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview). Its [forum announcement](https://community.perplexity.ai/t/sonar-is-moving-to-the-agent-api/5802) of 2026-08-13 says the Sonar endpoints retire on that date. This pipeline calls Sonar through litellm's `perplexity/` chat-completions route, and every cost preset runs a Sonar model, so plan for those calls to fail after the date unless the adapter moves to the Agent API. Setting `enabled: false` on `perplexity` (see [Disabling a model](CONFIGURATION.md#disabling-a-model)) runs the pipeline without it. `presets.yaml` has not been changed.

**Where:** https://www.perplexity.ai/settings/api

**Steps:**
1. Create an account at https://www.perplexity.ai
2. Go to **Settings → API**
3. Click **Generate** under API Keys
4. Copy the key

**What you need:**
- API key: the key you generated

**Current models (September 2026):**

| Model | Input | Output | Request fee, per 1,000 (low / medium / high search context) | Notes |
|---|---|---|---|---|
| `sonar-reasoning-pro` | $2/MTok | $8/MTok | $6 / $10 / $14 | CoT reasoning + web search; **recommended default**; what `balanced`, `thorough` and `maximum` run |
| `sonar-pro` | $3/MTok | $15/MTok | $6 / $10 / $14 | Web search, no CoT trace; good for standard tier; in no preset |
| `sonar` | $1/MTok | $1/MTok | $5 / $8 / $12 | Lightweight; economy option; what `economy` and `wide` run |
| `sonar-deep-research` | $2/MTok | $8/MTok | none | Extended research; highest cost and latency. Also bills citation tokens ($2/MTok), reasoning tokens ($3/MTok) and $5 per 1,000 searches |

Prices are from Perplexity's [pricing page](https://docs.perplexity.ai/docs/getting-started/pricing), checked 2026-09-19. The request fee is charged per request on top of the tokens. For `sonar` it is about as large as the tokens themselves: $0.005–$0.012 a request against roughly $0.009 of tokens a call in saved runs.

Perplexity's reasoning is model-selection based — use `sonar-reasoning-pro` for CoT, `sonar-pro` for standard search grounding. There is no separate reasoning parameter.

**Expected cost:** measured per review call, tokens only, from saved runs: `sonar-reasoning-pro` $0.006–$0.13, median $0.063 (160 calls, drafts of 2,182–135,514 characters; the reasoning trace makes the token count vary widely); `sonar` $0.008–$0.010 (4 calls, a 9,456-character draft), priced at Perplexity's $1/$1 rate. The request fee above comes on top of both.

**What it adds:** At `standard` thoroughness, Perplexity only runs if you explicitly add it to a model's `prompts:` list. At `thorough` thoroughness, it runs fact_check automatically alongside Gemini. Two independently grounded models flagging the same claim is very strong signal.

**Config:**
```yaml
api_keys:
  perplexity:
    api_key: your_key_here

models:
  perplexity: sonar-reasoning-pro
```

---

## Grok / xAI (optional)

**Where:** https://console.x.ai

**Steps:**
1. Create an account (sign in with X/Twitter or email)
2. Go to **API keys** and generate a key
3. Copy the key

**What you need:**
- API key: the key you generated

**Current models (September 2026):**

| Model | Input | Output | Notes |
|---|---|---|---|
| `grok-4.6` | $2.00/MTok | $6.00/MTok | 500K context; takes `reasoning_effort`; what `balanced`, `thorough` and `maximum` run |
| `grok-4.3` | $1.25/MTok | $2.50/MTok | 1M context; general purpose; what `wide` runs; **recommended default** for a plain config |
| `grok-4.20-0309-reasoning` | $1.25/MTok | $2.50/MTok | Reasoning variant; same price as standard |
| `grok-4.20-0309-non-reasoning` | $1.25/MTok | $2.50/MTok | Explicit non-reasoning variant |
| `grok-build-0.1` | $1.00/MTok | $2.00/MTok | 256K context; economy fallback |

Prices are xAI's for prompts under 200K tokens ([models page](https://docs.x.ai/docs/models), checked 2026-09-19). A request whose prompt reaches 200K tokens is billed at double these rates for all its tokens; no prompt this pipeline has sent comes near that (the largest in any saved run is 37,955 tokens, on a 135,514-character draft: 19% of it). `grok-4.5` ($2/$6) also exists and takes `reasoning_effort`; it is in no preset, and `pricing.yaml` has no row for it.

Reasoning on grok-4.6 (and grok-4.5) is set with `reasoning_effort: low | medium | high | xhigh`, default `high`, so leaving it unset buys the most expensive setting; every preset that runs grok-4.6 states an effort for that reason. On the older models reasoning is chosen by model name: the 4.20 generation has separate `-reasoning` and `-non-reasoning` variants, and the presets send grok-4.3 no `reasoning_effort`. The setting shows up in the bill: grok-4.6 at `high` wrote a median of about 15,300 tokens per call in saved runs (23 calls), against about 600 for grok-4.3 (30 calls). See [CONFIGURATION.md](CONFIGURATION.md#grok--reasoning_effort).

**Billing:** per token, against credits on your xAI account; see https://console.x.ai for current pricing. xAI's own billing pages describe no free tier (checked 2026-09-19), so do not plan around one, and if your account shows promotional credits, read their terms on how your prompts may be used before sending unpublished drafts through them.

**Expected cost:** measured per review call, tokens only, from saved runs and re-priced at the current rates: `grok-4.3` $0.009–$0.027, median $0.021 (30 calls, drafts of 9,456–73,786 characters); `grok-4.6` at `high` (or unset, which resolves to `high`) $0.01–$0.25, median $0.10 (23 calls, 2,182–27,113 characters).

**What it adds:** A second red team pass. Grok is trained on a different corpus (heavy X/Twitter data) and tends toward more direct, contrarian responses — useful for attack angles the other models miss. At `standard` thoroughness, both Mistral and Grok red team results appear in Section 6 of the report.

---

## Anthropic (Claude) (optional)

**Where:** https://platform.claude.com, the Claude Console (`console.anthropic.com` redirects there)

**Steps:**
1. Sign in, or create an account, at https://platform.claude.com
2. Go to **Settings → API keys**: https://platform.claude.com/settings/keys
3. Click **Create key**, give it a name, copy it immediately — shown once only
4. Buy credits under **Settings → Billing**: https://platform.claude.com/settings/billing

**What you need:**
- API key: the key you created

**Current models (September 2026):**

| Model | Input | Output | Context | Presets | With no `effort` set |
|---|---|---|---|---|---|
| `claude-opus-5` | $5/MTok | $25/MTok | 1M | `maximum` (`effort: high`) | Thinks, at `high` |
| `claude-sonnet-5` | $2/MTok | $10/MTok | 1M | `balanced` (`effort: medium`), `thorough` (`effort: high`) | Thinks, at `high` |
| `claude-haiku-4-5-20251001` | $1/MTok | $5/MTok | 200K | `wide` | Does not think |

`economy` runs no Claude model. The prices are Anthropic's list rates, the same ones [`pricing.yaml`](../packages/ci-core/src/ci_core/configs/pricing.yaml) bills with; Sonnet 5's $2/$10 launched as introductory pricing and is now its standard price. `claude-fable-5-1` ($10/$50, thinking cannot be turned off) is in no preset — the note on it in [`presets.yaml`](../packages/ci-article-review/src/ci_article_review/configs/presets.yaml) records why it was ruled out without a live test.

**Thinking:** Opus 5 and Sonnet 5 think by default, at effort `high`, so an unset `effort` runs exactly as `effort: high` does; set `low` or `medium` to spend less. Older models such as Opus 4.8 and Sonnet 4.6 do not think by default, and the pipeline turns thinking on for them whenever `effort` is set. Haiku 4.5 has only extended thinking, which `thinking_budget: N` turns on. Opus 4.7 and every later model — Opus 5, Sonnet 5 and Fable included — reject `thinking_budget` with a 400. The per-model table is in [CONFIGURATION.md](CONFIGURATION.md#claude--adaptive-vs-extended-thinking); Anthropic's own is on its [thinking troubleshooting](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting) page.

**Live web search:** Claude searches wherever its `web_search` setting covers the domain. It is the same key as [OpenAI web search](CONFIGURATION.md#openai-web-search), and like `prompts:` it survives `cost_preset`. The `maximum` preset sets `web_search: [fact_check]` for Claude; no other preset turns it on. To add it yourself:

```yaml
models:
  claude:
    model: claude-opus-5
    web_search: [fact_check]   # only fact_check searches
```

Anthropic bills [$10 per 1,000 searches](https://platform.claude.com/docs/en/about-claude/pricing) on top of tokens, and the pipeline lets one call make up to five. The search results count as input tokens, and since 2026-09-19 the report's cost figure also includes the per-search fee. Each call's count is the one Anthropic reports, and the fee is a line of its own (`total_search_usd` in `cost_summary`). The other grounded providers' fees are priced the same way; the rates and their sources are in [`pricing.yaml`](../packages/ci-core/src/ci_core/configs/pricing.yaml) under `search_fees`. If an admin has turned web search off for your organization in the Claude Console, every request that asks for it fails with a 400 saying web search is not enabled.

Claude runs `fact_check` only at `maximum` thoroughness, unless a `prompts:` list adds it. Without `web_search` there, it checks claims against training recall, like any model that does not search — add `web_search: [fact_check]`, or leave the domain out with [`prompts:`](CONFIGURATION.md#restricting-which-prompts-a-model-runs). An old `prompts:` list that drops `fact_check` from Claude (this page used to recommend one) keeps it off that domain under the `maximum` preset too, so the preset's search never runs.

**Choosing a model:** start from the presets. `thorough` ran Opus 5 until 2026-09-08, when a measured comparison moved it to Sonnet 5 — the note above `maximum:` in `presets.yaml` has the numbers. Opus 5 is still `maximum`'s model.

**Expected cost:** measured per call on drafts of 2,000–135,000 characters, August–September 2026: Haiku 4.5 $0.005–$0.06; Sonnet 5 at `effort: medium` $0.03–$0.06 (one draft so far); Opus 5 at `high` $0.08–$0.56 per review pass, and $0.36–$2.15 for `fact_check` with search, because the search results bill as input. Those grounded figures were measured before the report priced searches, so each is low by $0.01 a search: $0.05 at the usual five.

**What it adds:** A second argument integrity pass. Claude's training lineage is independent from the rest of the stack and it tends to catch logical gaps the other models miss. At `standard` thoroughness, both Mistral and Claude argument results are merged into Section 4. At `thorough` — the `wide`, `balanced` and `thorough` presets — it also runs voice/style and red team, and at `maximum` it runs every domain.

---

## LanguageTool (optional)

The grammar correction pass applies deterministic rule-based corrections before the AI review passes, so the models aren't distracted by surface errors. It's the only component that modifies your draft without asking.

Skip it if you already do a manual, thorough pass yourself (e.g. Grammarly Premium) — you're covering the same ground.

**To skip:** Set `grammar_pass: false` in `configs/user.yaml`, or simply omit the `languagetool` credentials block. The pipeline skips automatically and reminds you to run a manual check.

Two ways to use it, at different cost:

### Self-hosted (free)

Run the open-source LanguageTool server yourself and point the pipeline at it — no LanguageTool account, no `username`/`api_key`.

```bash
docker run -d -p 8010:8010 erikvl87/languagetool
```

```yaml
api_keys:
  languagetool:
    server_url: http://localhost:8010/v2/check
```

**Trade-off:** this runs the open-source Community rule set, not the fuller Premium one. Verified 2026-08-17 against languagetool.org's own account settings page (`/editor/settings/access-tokens`): Premium's "Access Tokens" there are scoped to *native app integrations* (Obsidian, LibreOffice, their browser add-on), not general programmatic access — so self-hosting isn't giving up something the $4.99–19.99/mo personal tier would otherwise unlock. See the note below.

### Hosted API (paid)

**This is not the same product as the $4.99–19.99/mo personal "Premium" subscription.** That tier's account settings page frames its own API tokens as being for LanguageTool's supported native integrations, not general HTTP access — despite the site being genuinely unclear about this. Programmatic access to the full rule set (what `username`/`api_key` credentials against `api.languagetool.org` require) is a separate commercial product, the [Proofreading API](https://languagetool.org/proofreading-api), starting around $40/month.

**To use:** Sign up for the Proofreading API, get a `username`/`api_key` pair, and configure:

```yaml
api_keys:
  languagetool:
    username: your_email@example.com
    api_key: your_languagetool_key
```

---

## WordPress Application Password

**Where:** Your WordPress admin dashboard

**Steps:**
1. Log in to your WordPress admin
2. Go to **Users → Profile** (or **Users → Your Profile**)
3. Scroll to the **Application Passwords** section
4. Type a name like `article-pipeline` and click **Add New Application Password**
5. Copy the password shown immediately — it will not be displayed again
6. Note your site URL and WordPress username

**Verify it works:** Visit `https://yoursite.com/wp-json/wp/v2` in a browser. A JSON response means the REST API is active. A 404 means the REST API is disabled — go to Settings → Permalinks in your WordPress admin and click Save Changes to rebuild the rewrite rules.

**What you need:**
- Site URL: `https://yoursite.com`
- Username: your WordPress login username
- Application password: the password generated above (spaces included, as shown)

Use an application password rather than your login password — it is scoped, can be revoked individually, and never exposes your main account credentials.
