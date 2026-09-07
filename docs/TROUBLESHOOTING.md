# Troubleshooting

Run the check command first:

```powershell
uv run ci-check --publication your_publication_name
```

It makes one minimal call to each configured service and tells you exactly what's wrong before you waste a full pipeline run diagnosing it.

---

## Configuration errors

**`User config not found`**  
You haven't created `configs/user.yaml` yet. Copy `configs/user.example.yaml` to `configs/user.yaml` and fill in your API keys.

**`Environment variable X is not set`**  
You used `${VAR_NAME}` syntax in a config but the variable isn't in your `.env` file or shell environment. Copy `.env.example` to `.env` and add the missing variable.

**`Invalid publication name`**  
Publication names can only contain letters, numbers, hyphens, and underscores. No slashes, spaces, or dots.

**`No DRAFT section found`**  
Your handoff document doesn't have a line that reads exactly `DRAFT` followed by the article text. Use the template in `handoff_templates/draft_submission.template.md`.

**`No model assignments could be built`**  
No models passed credential and enabled checks. Make sure at least one model has a valid API key and is not set to `enabled: false`.

---

## Gemini / Google

**Gemini returns 503 (capacity)**  
AI Studio draws from a shared capacity pool that fills up at peak hours. The pipeline retries once automatically. If 503s are a consistent pattern, switch to Vertex AI. See [PROVIDERS.md](PROVIDERS.md#option-b--vertex-ai-reserved-capacity-no-503s) for setup. Your fact-check pass routes automatically once you update `user.yaml` — the check command will confirm.

**Vertex AI: `No such file or directory` on credentials_file**  
The path in `credentials_file` doesn't point to the downloaded service account JSON. Move the file and update the path, or remove the key and use Application Default Credentials (`gcloud auth application-default login`) instead.

**Vertex AI: `'parts'` or `KeyError` in response**  
`gemini-2.5-flash` is a thinking model that returns internal reasoning traces before the actual output. The pipeline filters these out automatically. If you see this in the pipeline adapter (not just the check command), update to the latest version.

**Vertex AI: `403 Permission denied`**  
The service account doesn't have the **Agent Platform User** role on the project, or the Agent Platform API isn't enabled. Check IAM in the GCP console and verify the API is enabled at https://console.cloud.google.com/apis/library/aiplatform.googleapis.com.

**Vertex AI: `project` not found**  
Verify you are using the project **ID** (like `my-project-123`), not the display name. The ID appears in smaller text below the project name in the GCP console's project selector, and in the browser URL.

---

## OpenAI

**OpenAI returns 429**  
Rate limit or insufficient credits. Add credits at https://platform.openai.com/account/billing. The pipeline retries once after a delay.

**OpenAI web search not working**  
The Responses API with `web_search_preview` falls back silently to standard chat completions if it errors. Check `pipeline_<YYYYMMDD>.log` in `pipeline_history/` for the specific error. If the Responses API isn't available on your account tier, remove `web_search: true` from the model config.

---

## Perplexity

**Perplexity (or Gemini) returns non-JSON / Malformed JSON**  
Reasoning and grounded models (sonar-reasoning-pro, gemini-2.5-pro) sometimes wrap responses in markdown fences, prepend a chain-of-thought / `<think>` block, or surround the JSON with prose. All review adapters share a robust extractor (`ci_core.llm.json_utils`) that tries a direct parse, then a fenced block, then the outermost `{…}` span. If none parse, the run continues with that pass marked failed and the other models' results stand.

**sonar-reasoning-pro timing out even with a generous `stream_read_timeout` / wall-clock budget**  
Investigated 2026-08-07 after PRs #19/#20/#23/#26 each raised Perplexity's timeouts and each got outrun within days (calibration 59-141s → 233.93s → 240s ceiling hit → 162s vs 160s read-gap → 282s vs 280s → 352s/354s vs 350s, wall-clock hit directly at 375s for the first time). The pattern — failures landing within a few seconds of whatever the current ceiling happened to be — raised the question of whether something in this codebase was leaking the configured timeout back into the request (e.g. inflating `max_tokens` or reasoning depth), rather than the calls genuinely getting slower.

Findings:
- The outgoing payload (built in `ci_core/llm/client.py:_provider_params`, then `adapters/perplexity.py` before the litellm migration) is `model` / `messages` / `temperature: 0.2` / `stream` / `stream_options` only, plus `reasoning_effort` *if the provider config sets one* — and no `economy`/`standard`/`balanced`/`thorough`/`maximum` preset in `configs/presets.yaml` ever sets `reasoning_effort` for perplexity. `stream_read_timeout` (the read-gap knob) is read only by `_stream_timeout()` to build the local socket timeout; it is never written into the request. There is no `search_context_size` parameter sent either — the `'search_context_size': 'low'` seen in a past raw usage excerpt is Perplexity's own server-side default, not something this code sets or varies. So the payload is byte-for-byte identical regardless of which timeout tier is configured — ruled out a request-leak bug.
- `git log` on the then-current `perplexity.py`, `streaming.py`, and `prompts/*.txt` since the 2026-06-22 calibration date shows only unrelated fixes (UTF-8 decoding, malformed-JSON hardening, a 400-diagnostics addition) — no change to prompt content, payload construction, or reasoning parameters that could explain a genuine latency shift.
- Re-ran the exact failing calls live (`--only-model perplexity --only-domain fact_check|completeness --cost-preset maximum --no-timeout`, same 73786-char `dc-environment-v19-handoff.md` doc used throughout this timeline): three calls came back at 95.37s, 86.42s, and 91.55s — back in (or below) the original 59-141s calibration range, with normal token counts (~19-20k prompt / ~3-4k completion) and no retries.
- Conclusion: the "near-exact match to the current ceiling" pattern across PRs #19-#26 was **not** evidence of a request-side leak or a sustained regression — it was several weeks of a genuinely worsening (and now recovered) tail on Perplexity's side, most likely `sonar-reasoning-pro` capacity/latency variance on their infrastructure, landing right at whatever ceiling we'd most recently raised because each bump was sized to the last failure rather than to a stable distribution. No code change was needed. `stream_read_timeout: 500` / `sonar` `model_multiplier: 7.0` (from PR #26) are being left as-is: they're a legitimate safety margin now that the read-gap correctly no longer masks whether a stall is a real hang vs. one long silent reasoning+search stretch (see the `stream_read_timeout` note in `presets.yaml`), and reverting them would just reopen the near-miss risk if Perplexity's latency drifts back up.
- If sonar-reasoning-pro timeouts recur, re-run the diagnostic command above first — if it comes back fast again, it's provider-side variance, not a regression to chase in this codebase.

---

## Mistral

**`Invalid model` 400 error with `mistral-medium-3-5-latest`**  
The model ID `mistral-medium-3-5-latest` does not exist. The correct ID is `mistral-medium-3-5` — no `-latest` suffix. Update `user.yaml` and any `preset_overrides` that reference the bad ID.

**`reasoning_effort: low` or `reasoning_effort: medium` returns 400**  
`mistral-medium-3-5` only accepts `"high"` or `"none"` for `reasoning_effort`. The `"low"` and `"medium"` values are not supported. Standard Mistral models (`mistral-large-latest`, `mistral-small-latest`) don't support `reasoning_effort` at all. The adapter detects the error, retries without reasoning, and logs a `[MISCONFIGURATION]` warning.

**Mistral returns Malformed JSON on long or complex articles**  
`mistral-medium-3-5` with `reasoning_effort: high` occasionally wraps JSON output in markdown code fences (`` ```json `` … ` ``` ``) or adds a prose preamble before the JSON. The adapter strips these automatically using a regex fallback. If the raw content is logged as a warning, the adapter still returns `failed: true` — but this is rare and should only happen on unusually long or complex prompts.

**Mistral returns 402**  
Your account has no credits or your payment method needs updating. Go to https://console.mistral.ai under Billing.

---

## WordPress

**WordPress returns 404 on REST API**  
Visit `https://yoursite.com/wp-json/wp/v2` in a browser. If that 404s, go to **Settings → Permalinks** in your WordPress admin and click **Save Changes**. This rebuilds the rewrite rules that enable the REST API.

**WordPress returns 401**  
Authentication failed. Your application password is wrong or the username doesn't match the account that generated it. Re-generate the application password under **Users → Profile** and update your publication config.

**WordPress category or tag not found**  
The pipeline resolves string slugs to integer IDs via the WordPress REST API. If a slug doesn't match any existing category or tag, that term is silently dropped and a warning is logged. Create the category or tag in WordPress first, or use the integer ID directly in the handoff document.

---

## Claude

**Claude `fact_check` always fails at ~15s with Malformed JSON**  
The `fact_check` prompt explicitly requires live web search ("verify against live sources using your search capability"). Claude has no live web search capability and returns a refusal or explanation instead of JSON. Fix: add a `prompts:` filter to the Claude config that excludes `fact_check`:

```yaml
models:
  claude:
    model: claude-opus-4-8
    prompts: [voice_style, completeness, argument_integrity, red_team]
    # fact_check excluded: Claude has no live web search
```

The `prompts:` key survives `cost_preset` overrides — you only need to set this once.

**`thinking_budget` on Sonnet 4.6 or Opus 4.8 causes 400 errors**  
These models use adaptive thinking, not extended thinking. Use `effort: low/medium/high` instead of `thinking_budget`. See [CONFIGURATION.md](CONFIGURATION.md#claude--adaptive-vs-extended-thinking).

**Claude times out on long articles**  
Timeouts are sized automatically (see [the sliding-scale model](CONFIGURATION.md#timeouts-are-automatic-sliding-scale)) — Claude gets a budget from draft size × model × effort like every other provider, so you don't normally set one. If Opus 4.8 at high effort still times out on a very long article, raise `variance_margin` in ci-core's `timeouts.yaml` or add a `claude` entry to `model_multipliers` there. As a last resort, set `timeout_seconds` explicitly on the Claude model in `user.yaml` to override the formula.

---

## LanguageTool

**LanguageTool returns 401**  
Your API key or username is wrong. Log in to languagetool.org and check your account settings. Alternatively, set `grammar_pass: false` in `configs/user.yaml` to skip the grammar pass entirely.

---

## Pipeline behavior

**A model pass timed out**  
Timeouts are sized automatically by the sliding-scale model in ci-core's `timeouts.yaml` — you don't hand-set them per model. Each call's budget is `base × size_mult × model_mult × effort_mult × variance_margin`, clamped to `pipeline.task_timeout_seconds − 15`. See [CONFIGURATION.md](CONFIGURATION.md#timeouts-are-automatic-sliding-scale).

If a pass still times out, in order of preference:

1. **Raise `variance_margin`** in ci-core's `timeouts.yaml` (default `1.25`). This lifts *every* model's budget proportionally — the right move when timeouts are generally tight. It trades a longer worst-case wait for fewer truncations.
2. **Bump that model's effort multiplier** in `timeouts.yaml` if only one model/effort cell is affected (e.g. `xhigh`).
3. **Raise `pipeline.task_timeout_seconds`** if the computed value is being clamped (the log line `Timeouts (N chars): …` shows each model's budget; if it equals `task_timeout_seconds − 15`, it's clamped).
4. **Override one model** by setting `timeout_seconds` explicitly on it in `user.yaml` — that value wins and skips the formula entirely.

To find the true (untruncated) time a model needs, run with `--no-timeout --only-model PROVIDER --only-domain DOMAIN` and read the `[CALIBRATION]` log line. Then size the multiplier from the measured value.

`timeout_seconds` (when set as an override) and `prompts:` are **infrastructure keys** preserved when `cost_preset` overrides model IDs.

**All model passes failed**  
By default the pipeline produces a partial report rather than aborting. To make it abort when all calls fail, set `abort_if_all_provider_calls_fail: true` in the pipeline config.

**Report shows no consensus flags**  
At `standard` thoroughness, consensus requires the same passage to be flagged by multiple models in different domains (e.g., Mistral's argument flags + OpenAI's voice flags). This happens when a passage has both a logical and a style problem. To get within-domain consensus, switch to `thorough` or `maximum` thoroughness so multiple models cover each domain. You can also lower `ensemble.consensus_threshold` in `user.yaml` — the default is 2.0.

**Fallback model warning in summary**  
A model returned 503 capacity errors and the pipeline fell back to a less capable variant (e.g., `gemini-2.5-flash-lite` instead of `gemini-2.5-flash`). The findings are valid but may be less thorough. Re-run when the preferred model is available to confirm.

**MODEL CURRENCY warning in summary**  
One of your configured model IDs has been superseded by a newer model. The old model still works; this is informational. Update `user.yaml` to the replacement model shown. The registry tracking this lives in [`ci-core`'s `model_registry.yaml`](../packages/ci-core/src/ci_core/configs/model_registry.yaml) — if you're confident the current model is still the best choice, you can remove its entry from `superseded:`.

**Model registry staleness notice**  
The built-in model registry hasn't been updated in 60+ days. Provider APIs change frequently. Re-check [PROVIDERS.md](PROVIDERS.md) against current provider documentation, update `superseded:` / `newer_available:` in [`ci-core`'s `model_registry.yaml`](../packages/ci-core/src/ci_core/configs/model_registry.yaml), and bump `registry_date:` to today. This resets the staleness clock. No code change is needed — the pipeline reloads the YAML each run.

**Model discovery shows no models for a provider**  
Check that the provider's API key is valid (run the check command first). For Gemini configured via Vertex AI, model listing is not supported — check https://ai.google.dev/models manually. For Perplexity, model listing isn't available from their API; the script shows a static documented list.

**Model discovery shows NEW models but the check command fails with 404 on the new model**  
The new model exists in the provider's catalog but may require a different API access tier, may be in preview, or the model ID in the discovery list may not match exactly what the API accepts for inference. Check the provider's documentation for the exact model ID and any access requirements.

**Link validation takes a long time**  
By default the pipeline checks every URL in the article over HTTP and queries the Wayback Machine for each. On an article with many links, this can add 30–60 seconds. Set `link_validation: false` in `user.yaml` to disable entirely, or `wayback_link_check: false` to skip Wayback while keeping the HTTP status check.

**Link shows `skipped: non-public host (SSRF guard)`**  
The URL resolves to a private, loopback, or link-local address (e.g. `localhost`, `127.0.0.1`, `192.168.x.x`, or the cloud metadata IP `169.254.169.254`). The pipeline refuses to fetch internal hosts to avoid server-side request forgery when reviewing drafts that may contain untrusted links. If the link is legitimately internal and you trust the draft, verify it manually — the guard is intentional and not configurable.

**Link shows `BROKEN` but the page loads in a browser**  
Some servers reject automated HEAD requests (returning 403 or 405), and some are simply unreachable from where the pipeline runs (timeout, DNS failure). The pipeline falls back to a GET request for 405s, and to an archive.org snapshot for 401/403/429/timeout/DNS failures — so `BROKEN` here means there was no usable snapshot either. The summary calls these out separately from confirmed 404s. The link is likely still fine; verify manually before removing it.

**Link shows `OK (via archive: …)`**  
The live URL could not be read — the parenthetical says why (`403 blocked`, `origin timed out`, `origin unreachable`, `401 auth required`, `429 rate limited`) — but archive.org had a snapshot the pipeline could read. The link is not confirmed working right now, only confirmed to have existed. If the snapshot is also flagged `[STALE]`, the content you're citing may have moved on. Worth a manual look before publishing, especially for a primary source.

**Wayback Machine shows `Not archived` for a valid URL**  
The Wayback Machine hasn't crawled that URL yet, or the URL is behind a login. Consider requesting archival at https://web.archive.org/save/ before publishing — it's a single form submission. The pipeline flags this in the summary so you can act on it.

**Wayback snapshot shows `[STALE]`**  
The most recent Wayback snapshot is more than `wayback_snapshot_stale_days` old (default 180). The page may have changed since the snapshot. If the URL is a primary source (stat, study, official document), verify the current content matches what you cited, then manually request a fresh snapshot at https://web.archive.org/save/.

**Link shows `archive link (Nd)`**  
The cited URL is itself a `web.archive.org` snapshot. The pipeline recognizes this from the URL's embedded timestamp (it does not look for an archive *of* an archive), reports the snapshot's own age, and flags it `archive link STALE (Nd)` if older than the stale threshold. Whether the archive link actually resolves is the normal HTTP status check — `OK` means it works, `BROKEN` means the snapshot URL itself is dead.

**Section 9 — Citations shows 0 resolved**  
Either: (a) no `citation_sources` are configured in your publication config, (b) no claims from the fact-check results matched the source adapters' keyword rules, or (c) the API keys for those sources aren't set (e.g., `FRED_API_KEY` env var). Check `pipeline_<YYYYMMDD>.log` for "Citation adapter … failed" messages. Note that `topic_match` deliberately suppresses keyword hits occurring in credential phrases ("credentials in air quality analysis"), so some claims a human would match land unresolved on purpose — see [CITATIONS.md](CITATIONS.md#pointer-only--verification-pointer).

**Section 9 — everything comes back "pointer-only"**  
Pointer-only is what the `epa`, `ferc`, `fhwa`, `icc`, `ilga`, and `pjm` adapters return by design — they name a portal, they don't retrieve or check the data. Only `census`, `crossref`, `eia`, `fred`, and fact-check-supplied `known_url` citations can reach the verified tier. If you expected verified results, check that a data-fetching adapter is listed in `citation_sources` and that its API key is set.

**A "verified" citation carries a `relevance_check` note**  
The content-relevance model call that normally gates the verified tier didn't run — usually no `mistral` API key configured, otherwise a call failure or an unparseable verdict. The entry was fetched and checksummed but *not* relevance-confirmed, and the note says which. This is a deliberate graceful degradation, not an error, but treat those entries as fetch-only until a key is configured.

**Section 9 — `content_mismatch`**  
The source URL fetched and checksummed fine, but the relevance check found the page does not support the claim. The entry records a `contradicts`, `not_addressed`, or `inconclusive` verdict plus a one-sentence reason, and appears under *Unresolved* in the readable report. Worth more attention than an ordinary unresolved entry — `contradicts` in particular is a signal about the claim itself, not just about the citation.

**Section 9 — a citation reads `SUBMITTED, OUTCOME UNKNOWN`**  
The submission was accepted and no snapshot was established, so the report declines to call it archived. Without credentials this means archive.org took the request but did not redirect to a snapshot; with credentials it means the capture was still queued when the bounded same-run wait expired. Either way the next run follows it up — an authenticated one by reading the job status, an unauthenticated one by re-checking availability. Treat the URL as unarchived until a snapshot URL appears beside it.

**Section 9 — a citation reads `CAPTURE FAILED`**  
archive.org accepted the capture and then could not complete it, and the reason it gave is printed with the entry. This is the state that used to be invisible: it rendered identically to a successful capture, so the same URL could fail every run without anyone noticing. Re-running usually fails the same way — common causes are a robots/ownership block, a page too slow for the capture time limit, and a URL that does not resolve from archive.org's side. Archive it by hand or re-source the claim.

**Section 9 — a citation reads `NOT SUBMITTED`**  
Nothing tried to archive it, and the entry says which of the two reasons applied. A non-public address is a deliberate, permanent refusal — an internal hostname is never handed to archive.org — so re-running will not change it. A host that did not resolve is transient; the next run will try again.

**Wayback submissions don't show up as archived**  
Check the `archive_outcome` on the entry before assuming it is a timing issue. Without credentials a successful capture reports its snapshot URL in the same run, so a citation that is *not* archived usually means a real failure rather than a pending one — see the three entries above. If submissions are failing outright, you're likely hitting unauthenticated rate limits — configure `api_keys.archive_org` ([CONFIGURATION.md](CONFIGURATION.md#api-keys)).

**Custom domain prompt_file not found**  
The `prompt_file` path in `custom_domains` must be relative to your project root or an absolute path. Run the check command to verify paths resolve before a full pipeline run. If the file doesn't exist, the custom domain is silently skipped with a warning in `pipeline_<YYYYMMDD>.log`.

**Fact-check CONTRADICTIONS banner in summary**  
Two or more models reviewed the same claim and disagreed — one marked it confirmed, another marked it outdated or contradicted. This is expected when models have different training data cutoffs or search grounding. Manually verify the claim against a primary source before publishing. The contradiction is saved in the report under the `contradictions` key.

**`Unknown cost_preset` error**  
The `cost_preset` value in `pipeline:` isn't one of the supported values. Valid values: `economy`, `wide`, `balanced`, `thorough`, `maximum`. Check for typos.

`standard` was retired on 2026-09-05 and is not an error: it still runs, as `wide`, and warns once per run until you change it. That is a real behaviour change rather than a rename — `wide` runs six models over twelve calls where `standard` ran five over seven, at roughly half the cost. See [CONFIGURATION.md](CONFIGURATION.md#cost-presets) for the measurements behind the retirement.

**cost_preset isn't applying expected models**  
The preset overrides model names for configured providers only. If a provider has no entry in your `models:` section (you removed it or never added it), the preset has nothing to override. Add the provider's model entry back — the preset will then apply its model selection on top.

**cost_preset overriding a model you explicitly set**  
By design, `cost_preset` overrides model names. If you want to use a preset for reasoning flags and thoroughness but keep a specific model, remove `cost_preset:` and set `thoroughness:` plus per-model reasoning flags manually. See [CONFIGURATION.md](CONFIGURATION.md#reasoning-controls) for the individual flags.
