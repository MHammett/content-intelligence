# style-profile-bootstrap — Development Plan

Analyzes a writing corpus across multiple platforms and synthesizes a structured style profile for use in `publication.yaml`. Lives at `style-profile-bootstrap/` inside the content-intelligence repo; shares no runtime code with the pipeline but follows the same conventions (YAML config, secrets in gitignored files, `tests/` with mocked network).

---

## Repo layout

```
content-intelligence/
  style-profile-bootstrap/
    bootstrap.py              ← CLI entry point
    normalize.py              ← corpus cleaning, deduplication, per-doc metrics
    detect.py                 ← style cluster detection + document classification
    callers.py                ← thin multi-model caller; routes synthesis prompts to each provider
    style_consolidation.py    ← merge per-model synthesis outputs (weighted lists + prose collection)
    synthesize.py             ← orchestrates detect → call → consolidate → reconcile
    output.py                 ← formatter base class + PublicationYamlFormatter
    logging_config.py         ← configurable modular logging setup
    collectors/
      __init__.py             ← collector registry (auto-discovered)
      base.py                 ← abstract Collector + Document dataclass + CollectorError
      wordpress.py
      twitter.py
      gmail.py
      outlook365.py           ← Microsoft Graph API (Outlook / Exchange)
      textfiles.py
      custom/                 ← drop-in directory for user-defined collectors
        convertkit.py         ← Phase 8 (planned); lives here until built into core
        .gitkeep
    prompts/
      detect_styles.txt            ← detection pass: find N distinct styles in the corpus
      synthesize_canonical.txt     ← unified single-style synthesis prompt
      synthesize_per_style.txt     ← per-detected-style synthesis prompt
      synthesize_reconcile.txt     ← reconciliation pass: canonical from per-style profiles
      synthesize_per_source.txt    ← per-source-label synthesis (fallback mode)
      consolidate_detection.txt    ← reconcile multi-model detection outputs into unified clusters
    profiles/                 ← timestamped profile snapshots (gitignored)
      .gitkeep
    staging/                  ← NDJSON corpus cache (gitignored)
      .gitkeep
    tests/
      __init__.py
      fixtures/               ← canned API response JSON for mocking
      test_collectors.py
      test_normalize.py
      test_detect.py          ← style detection, classification, cluster validation
      test_callers.py         ← per-provider routing, streaming accumulation, error handling
      test_style_consolidation.py ← weighted list intersection, prose collection, model weighting
      test_synthesize.py
      test_output.py
      test_logging.py         ← log level routing, JSON format, file handler
      test_bootstrap.py       ← CLI integration tests (argparse + dry-run flow)
    configs/
      presets.yaml            ← run-intensity presets (economy → maximum); mirrors pipeline's configs/presets.yaml
    sources.yaml              ← credentials + per-source config (gitignored)
    sources.example.yaml      ← committed template; uses ${ENV_VAR} for all credentials
    requirements.txt
    README.md
```

---

## Internal data model

### `Document` (dataclass)

```python
@dataclass
class Document:
    text: str           # cleaned plain text (no HTML, no Markdown syntax)
    source: str         # "wordpress" | "twitter" | "gmail" | "outlook365" | "textfiles" | custom SOURCE_NAME
    register: str       # source hint, not a clustering label: "long_form" | "casual" | "correspondence" | "manual:<label>"
    date: str           # ISO 8601 date string
    url_or_id: str      # post URL, tweet ID, Gmail message ID, or file path
    word_count: int     # computed after cleaning
    content_hash: str   # SHA-256 of cleaned text — used for cross-source deduplication
    metadata: dict      # source-specific fields: WP categories, tweet metrics, Gmail recipient shape, etc.
    metrics: dict       # populated by normalize.compute_metrics(); used for classification
```

`register` is a *source context hint* — it describes where the document came from — not a style cluster label. In `detect` mode the model uses `register` as one signal among many but is not constrained to cluster by it. A blog might contain two distinct styles; email and a blog post might share the same detected style. `metrics` is populated in-place by `normalize.py` and used by `detect.py` for fast metric-based classification without additional API calls.

### `StyleCluster` (dataclass)

```python
@dataclass
class StyleCluster:
    label: str                      # model-generated name, e.g. "technical analysis"
    description: str                # what distinguishes this style from others
    features: dict                  # metric thresholds for classification, e.g. {"avg_sentence_words": (">", 18)}
    source_distribution: dict       # {source_name: pct} — where this style appears most
    sample_ids: list[str]           # url_or_id of representative documents identified by detection pass
    assigned_docs: list[Document]   # populated by classify_documents()
    word_count: int                 # total words in assigned docs; computed after classification
    confidence: str                 # "low" | "medium" | "high" — detection pass self-assessment
```

`StyleCluster` objects are ephemeral — they are not staged to disk and are rebuilt on each run (detection is cheap relative to collection). Only the final synthesized profiles are persisted.

### Staging format

Each staging file: `staging/<source>.ndjson`

Line 1 is always a metadata header:
```json
{"schema_version": 1, "source": "wordpress", "generated": "2026-06-23T14:00:00Z", "watermark": "2026-06-20"}
```
Lines 2..N are serialized `Document` objects (one per line). Staged documents are serialized **without** the `metrics` field — metrics are recomputed fresh by `normalize.py` on every run from the staged `text`. `schema_version` reflects the collector output schema only (not the normalize schema). If `schema_version` differs from the current code's version, the tool refuses to use the cache and forces `--refresh`.

### Watermark tracking

`staging/.watermarks.json` stores `{source_name: last_id_or_date}` per source. On non-refresh runs, each collector uses the watermark as the API-level start point (not a post-filter), avoiding fetching data that would be discarded anyway.

---

## Collector interface

### `collectors/base.py`

```python
class Collector(ABC):
    SOURCE_NAME: str   # must be set on each subclass

    def __init__(self, config: dict): ...

    @classmethod
    def validate_config(cls, config: dict) -> None:
        """Raise ConfigError on missing required keys. Called at startup before fetching."""

    def estimate_count(self) -> int | None:
        """Return approximate document count without fetching full corpus, or None."""
        return None

    @abstractmethod
    def fetch(self, since: str | None = None) -> Iterator[Document]:
        """Yield Documents. Raises CollectorError on auth failure or quota exhaustion."""
```

### Collector registry

`collectors/__init__.py` builds the registry by:
1. Importing the four built-in collectors
2. Scanning `collectors/custom/` for `.py` files with a class that subclasses `Collector`
3. Exposing `REGISTRY: dict[str, type[Collector]]`

`bootstrap.py` references only `REGISTRY` — it never imports individual collectors directly. Adding a new collector requires only dropping a file in `collectors/custom/`.

### Output formatter interface

```python
class Formatter(ABC):
    @abstractmethod
    def format(self, profile: dict) -> str: ...

class PublicationYamlFormatter(Formatter): ...  # publication.yaml style sections
class MarkdownReportFormatter(Formatter): ...   # human-readable Markdown summary
class JsonFormatter(Formatter): ...             # raw JSON blob for programmatic use
```

`--format yaml|markdown|json` selects the formatter. Default is `yaml`.

---

## Phase 1 — Scaffold and collector framework

**Goal:** CLI boots, each collector validates its config, fetches, and writes staging files.

### 1a. `collectors/base.py`
- `Document` dataclass (all fields including `content_hash`, `metadata`)
- Abstract `Collector` class with `validate_config`, `estimate_count`, `fetch`
- `CollectorError(source_name, message)` exception
- `ConfigError(source_name, missing_keys)` exception

**Per-collector `metadata` field contents** (documented here as the authoritative reference for test fixtures and prompt engineering):

| Source | Metadata keys |
|--------|---------------|
| `wordpress` | `post_id`, `categories` (list of term slugs), `tags` (list of term slugs) |
| `twitter` | `tweet_id`, `public_metrics` (`like_count`, `retweet_count`, `reply_count`), `conversation_id` |
| `gmail` | `message_id`, `thread_id`, `recipient_count`, `labels` (list of label names) |
| `outlook365` | `message_id`, `folder`, `recipient_count`, `importance` (`low`/`normal`/`high`) |
| `textfiles` | `file_path` (absolute), `file_size` (bytes) |
| custom | whatever the custom collector provides |

### 1b. `collectors/wordpress.py`
- Config keys: `site_url`, `username`, `application_password`
- Validate `site_url` passes `_is_public_host()` at `validate_config` time (not at fetch time)
- `GET /wp-json/wp/v2/posts?per_page=100&after=<watermark>&status=publish`
  - `after` param uses watermark date if available; falls back to `--since` if no watermark
- Pagination: page 1 fetched first to get `X-WP-Total` and `X-WP-TotalPages`; pages 2..N fetched concurrently (up to 5 threads)
- HTTP calls: `allow_redirects=False` on all requests (REST API endpoints don't redirect; unexpected redirects are rejected)
- HTML stripping: stdlib `html.parser` (replicate approach from `analysis/webpage.py`, do not import it)
- `estimate_count()`: HEAD or GET page 1 only, return `X-WP-Total` header value
- Register: `long_form`

### 1c. `collectors/twitter.py`
- Config keys: `bearer_token`, `user_id` (or `username` — resolve via `/2/users/by/username/:username`), `exclude_retweets` (default true), `exclude_replies` (default true), `max_results_per_page` (default 100, max 100)
- `GET /2/users/:id/tweets?max_results=100&start_time=<watermark_RFC3339>`
  - `start_time` uses watermark; falls back to `--since` converted to RFC3339
- 429 handling: read `Retry-After` header; fall back to exponential backoff (2s, 4s, 8s) if header absent
- 5xx handling: retry up to 3 times with exponential backoff before raising `CollectorError`
- Register: `casual`
- Note in README: requires Twitter API v2 Basic tier ($100/month) for general access; Free tier only allows reading your own recent tweets (limited to last 7 days). Document both paths.

### 1d. `collectors/gmail.py`
- Config keys: `credentials_file`, `query`, `max_messages` (default 500)
- Startup: verify `credentials_file` permissions are mode `600` (owner-only read); warn and continue on Windows (ACL check is complex; note in README)
- OAuth2 flow: `InstalledAppFlow` if token file missing; write token back
- Fetch: list messages matching `query` up to `max_messages`; if more exist, log a warning and take the most recent N
- Per-message body: prefer `text/plain` parts; strip quoted/forwarded blocks (lines starting `>`, patterns matching `On .* wrote:`, `From:` dividers)
- 5xx handling: retry up to 3 times
- Register: `correspondence`
- `--no-stage` flag: discard staging files after synthesis; never persist email text

### 1e. `collectors/outlook365.py`
- Config keys: `tenant_id`, `client_id`, `auth_method` (`device_code` | `client_credentials`), `client_secret` (only for `client_credentials`), `folder` (default `SentItems`), `query` (optional OData/KQL filter), `max_messages` (default 500)
- Auth via `msal`: `device_code` flow for personal/interactive use (user opens a URL, enters a code, token saved to `credentials_file`); `client_credentials` for org/unattended use
- Personal Microsoft accounts (MSA): `tenant_id: common`; organizational accounts: actual tenant UUID
- `GET https://graph.microsoft.com/v1.0/me/mailFolders/<folder>/messages?$select=body,subject,sentDateTime,toRecipients&$top=50&$filter=<query>`
  - `$filter` supports OData: `sentDateTime ge <watermark_ISO8601>`
  - Body preference: `application/json` with `Prefer: outlook.body-content-type="text"` header to receive plain text directly (avoids stripping HTML client-side)
- Pagination via `@odata.nextLink` token
- Rate limiting: Microsoft throttles per-app at ~10,000 requests/10min; respects `Retry-After` header; exponential backoff for 429 and 503
- Per-message: strip quoted/forwarded blocks by the same heuristics as `gmail.py` (lines starting `>`, `On ... wrote:`, `From:` dividers)
- Token file: same permission check as Gmail (mode 600 warning); stored at `credentials_file` config path
- Token refresh: MSAL handles silent refresh automatically. If the refresh token has expired (personal MSA: ~90 days; org accounts: configurable by tenant), MSAL raises `InteractionRequiredAuthError`. Catch this explicitly and re-run the device-code flow rather than surfacing a cryptic error. Log a clear message: `"Outlook365 session expired — re-authentication required. Run again to get a new device code."`
- Register: `correspondence` — same as Gmail; both are personal email
- Privacy: same staging/`--no-stage` considerations as Gmail; noted in README under a combined "Email sources" section
- `estimate_count()`: `GET .../messages?$count=true&$top=1` returns `@odata.count`

### 1f. `collectors/textfiles.py`
- Config keys: `path`, `register` (user-specified), `glob` (default `**/*.{txt,md}`), optional `max_files`
- `.md`: strip fenced code blocks, `[text](url)` → `text`, ATX heading markers
- `.docx`: use `python-docx` if installed; skip with logged warning if not (optional dep)
- Register: value of `register` config key

### 1g. `collectors/custom/` hook
- `collectors/__init__.py` scans this directory for `.py` files, imports any class that subclasses `Collector`, adds to `REGISTRY`
- Failure to import a custom collector: log warning, skip, continue — don't abort
- Duplicate `SOURCE_NAME` across any two collectors (built-in or custom): raise `ConfigError` at registry build time — do not silently overwrite; message names both conflicting classes

### 1h. `sources.example.yaml`
All credential values use `${ENV_VAR}` pattern so the copy-paste path never puts credentials in the file:

```yaml
wordpress:
  site_url: https://www.example.com
  username: ${WP_USER}
  application_password: ${WP_APPLICATION_PASSWORD}

twitter:
  bearer_token: ${TWITTER_BEARER_TOKEN}
  user_id: "123456789"
  exclude_retweets: true
  exclude_replies: true

gmail:
  credentials_file: ~/.config/style-bootstrap/gmail_token.json
  query: "from:me after:2023/01/01 -category:promotions -category:social"
  max_messages: 500

outlook365:
  tenant_id: common                   # "common" for personal MSA; tenant UUID for org accounts
  client_id: ${AZURE_CLIENT_ID}
  auth_method: device_code            # device_code (interactive) | client_credentials (unattended)
  # client_secret: ${AZURE_CLIENT_SECRET}   # uncomment for client_credentials auth
  credentials_file: ~/.config/style-bootstrap/outlook_token.json
  folder: SentItems                   # SentItems | AllMail | or a folder display name
  max_messages: 500

textfiles:
  path: ~/writing-archive
  register: long_form
  glob: "**/*.md"

synthesis:
  # Model selection comes from user.yaml (same as the review pipeline) — no model config here.
  # API keys are also from user.yaml. The style profiler uses whatever models are configured there.
  style_mode: detect                 # canonical | detect | per-source
  max_styles: 5                      # ceiling for auto-detection (detect mode only); 0 = no limit
  max_input_chars: 120000            # corpus budget before sampling
  prompt_overhead_chars: 12000       # reserved for system prompt + schema + metrics header
  per_style_min_words: 2000          # minimum words per detected style for per-style synthesis
  ambiguity_threshold: 0.2           # classification: docs within this score margin are "ambiguous"
  consensus_threshold: 2.0           # weighted-vote threshold for including a banned word/phrase
  detection_models: []               # models to use for detection pass; empty = style-weighted models from user.yaml
  max_parallel_models: 0             # max simultaneous model calls in call_all(); 0 = no limit (all at once)
  per_source_group_by: source        # per-source mode grouping: "source" (collector name) or "register" (source hint)
                                     # "source" keeps gmail and outlook365 separate; "register" merges them as "correspondence"

logging:
  level: INFO                        # DEBUG | INFO | WARNING | ERROR
  format: human                      # human | json
  file: null                         # path to log file; null = stdout only
  also_stdout: true                  # echo to stdout even when file is set
  modules: {}                        # per-module overrides, e.g.:
    # style_profile_bootstrap.collectors.gmail: DEBUG
    # style_profile_bootstrap.synthesize: WARNING
```

### 1i. `bootstrap.py` (CLI)

```
python style-profile-bootstrap/bootstrap.py \
  --publication mikehammett \
  --sources wordpress,gmail,outlook365 \
  --since 2023-01-01 \
  --style canonical \
  --format yaml \
  [--preset balanced] \
  [--refresh] \
  [--dry-run] \
  [--no-stage] \
  [--continue-on-error] \
  [--overwrite] \
  [--log-level DEBUG] \
  [--check-draft path/to/draft.md]

# Mutually exclusive with --publication:
  --output-yaml path/to/output.yaml
```

- `--publication <name>`: resolves to `configs/<name>.yaml`; updates style sections only; mirrors pipeline's `--publication` flag
- `--output-yaml <path>`: escape hatch for explicit output path; **mutually exclusive with `--publication`** (enforced via `argparse.add_mutually_exclusive_group`)
- `--preset economy|standard|balanced|thorough|maximum`: run-intensity preset (default `balanced`); sets corpus budget, model variants, reasoning effort, style-mode, and model subset as a bundle. Individual `sources.yaml` keys override specific preset values. CLI `--style` and `--max-styles` override preset style settings.
- `--sources`: comma-separated; defaults to all sources in `sources.yaml`
- `--since`: ISO date; applied at API level where supported; post-filter otherwise
- `--style canonical|detect|per-source`: synthesis mode; fallback resolution: CLI `--style` → preset `style_mode` → `sources.yaml synthesis.style_mode` → default `detect`
- `--max-styles N`: ceiling on auto-detected style count (detect mode only; 0 = no limit)
- `--dry-run`: collect + normalize + deduplicate + print corpus stats + bias warnings; **behavior differs by mode**:
  - `canonical` / `per-source`: zero API calls — collection and metrics only
  - `detect`: runs one detection pass (costs money — two model calls) and prints the cluster summary; then exits before synthesis. Call this out in `--help` text so users know `--dry-run --style detect` is not free.
- `--refresh`: ignore staging cache, re-fetch all sources; reset watermarks
- `--no-stage`: do not write staging files; process in memory only
- `--continue-on-error`: skip failed collectors, warn in output YAML header
- `--format yaml|markdown|json`: output formatter; default `yaml`
- `--overwrite`: skip confirmation prompt when merging into existing file
- `--log-level DEBUG|INFO|WARNING|ERROR`: runtime override of `logging.level` config key
- `--check-draft <path>`: style consistency check on a draft (see Phase 7)

**Startup sequence:**
1. Load `sources.yaml`, apply `_resolve_env_recursive`
2. Apply logging config via `logging_config.configure_logging()` — before any module logs; `--log-level` CLI flag overrides config
3. Load `configs/presets.yaml` (required: raises `PackagedConfigError` if missing, no hardcoded fallback); apply selected preset over base config; apply `sources.yaml` key overrides on top; apply CLI flags last
4. Build collector registry; raise `ConfigError` on duplicate `SOURCE_NAME`
4. For each requested source: call `Collector.validate_config()` — abort on `ConfigError` (or skip if `--continue-on-error`)
5. For each requested source: call `Collector.validate_config()` — abort on `ConfigError` (or skip if `--continue-on-error`)
6. Check staging schema versions match current code
7. Pre-synthesis cost estimate: log expected API call count by mode at INFO level (e.g., `"detect mode: ~2 detection + 5 per-style × 4 models + 1 reconciliation = 22 calls"`)
8. Collect (parallel `ThreadPoolExecutor`), with progress logging
9. Deduplicate by `content_hash`
10. Normalize (populates `Document.metrics` in-place from staged `text`; metrics are never read from staging)
11. Print corpus stats + bias warnings
12. If `detect` mode: run detection pass → print cluster summary; if `--dry-run`, exit here
13. If `canonical` or `per-source` mode and `--dry-run`: exit here
14. Synthesize (mode from `--style`)
15. Validate synthesis output
16. Write output (atomic via temp file + `os.replace()`)
17. Save timestamped snapshot: if `--publication`, to `profiles/<publication>/<ISO8601>.yaml`; if `--output-yaml`, to `profiles/_output/<output-stem>/<ISO8601>.yaml`
18. Update watermarks

---

## Logging configuration

### `logging_config.py`

Called once at startup before any other module logs. Reads the `logging:` block from `sources.yaml` and calls `logging.config.dictConfig`.

**Supported configuration:**

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `level` | str | `INFO` | Global log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `format` | str | `human` | `human` = timestamped readable; `json` = one JSON object per record |
| `file` | str\|null | null | Path to log file; null = stdout only |
| `also_stdout` | bool | true | When `file` is set, also echo to stdout |
| `modules` | dict | `{}` | Per-module level overrides |

**`human` format** (default):
```
2026-06-23 14:00:05 INFO  collectors.wordpress  Fetched page 3/12 (274 posts so far)
2026-06-23 14:00:06 DEBUG collectors.gmail      Stripped 12 quoted blocks from message id=abc123
```

**`json` format** (for log aggregators, piping to `jq`, or programmatic analysis of collection runs):
```json
{"ts": "2026-06-23T14:00:05Z", "level": "INFO", "logger": "collectors.wordpress", "msg": "Fetched page 3/12", "page": 3, "total": 12, "count": 274}
```
Structured extra fields (page, count, source, etc.) are added by calling `log.info("msg", extra={...})` — the JSON formatter includes all `extra` keys at the top level.

**Module name convention** (for `modules:` overrides):
- `style_profile_bootstrap.collectors.wordpress`
- `style_profile_bootstrap.collectors.gmail`
- `style_profile_bootstrap.collectors.outlook365`
- `style_profile_bootstrap.collectors.twitter`
- `style_profile_bootstrap.normalize`
- `style_profile_bootstrap.synthesize`
- `style_profile_bootstrap.output`

**CLI override:** `--log-level DEBUG` overrides the `level` key globally at runtime without editing `sources.yaml`. Useful for diagnosing a single run without changing config.

---

## Presets

Mirrors the pipeline's `configs/presets.yaml` pattern. A preset bundles corpus budget, synthesis intensity, reasoning depth, and model selection into a named tier. The user picks a preset at the CLI; individual `sources.yaml` keys override specific preset values.

```
--preset economy     ← fast sanity check; single model; canonical only
--preset standard    ← two-model ensemble; basic detection
--preset balanced    ← default; full ensemble; standard detection
--preset thorough    ← elevated reasoning; wider cluster search
--preset maximum     ← all models; max reasoning; all phases
```

**Preset shape** (`configs/presets.yaml`):

```yaml
<name>:
  style_mode: canonical | detect    # detect for standard+; canonical for economy
  max_input_chars: <int>            # corpus budget before sampling
  max_styles: <int>                 # ceiling on detected style count
  per_style_min_words: <int>        # cluster minimum after classification
  synthesis_models: [...]           # empty = all configured models
  detection_models: [...]           # empty = style-weighted subset; "*" = all configured
  models:
    claude:
      model: <model_id>             # overrides user.yaml model choice for this run
      effort: low | medium | high   # Claude adaptive thinking depth
    openai:
      model: <model_id>
      reasoning_effort: low | medium | high
    gemini:
      model: <model_id>
      thinking_budget: <int>
```

**Five presets:**

| Preset | style_mode | max_input_chars | max_styles | synthesis_models | detection_models | reasoning | Approx cost |
|--------|-----------|-----------------|------------|------------------|------------------|-----------|-------------|
| `economy` | canonical | 40,000 | — | `[claude]` only | — | low | ~$0.01–$0.05 |
| `standard` | detect | 80,000 | 3 | `[claude, openai]` | `[claude]` | default | ~$0.10–$0.30 |
| `balanced` | detect | 120,000 | 5 | all configured | style-weighted | default | ~$0.50–$1.50 |
| `thorough` | detect | 160,000 | 7 | all configured | style-weighted | medium | ~$1.50–$3.00 |
| `maximum` | detect | 200,000 | 10 | all configured | `"*"` (all) | high | ~$3.00–$8.00 |

**`economy`** — single-model canonical synthesis on a small corpus sample. No detection pass, no ensemble voting. Useful for a quick sanity check on a new corpus before investing in a full run.

**`standard`** — two-model ensemble, single-model detection pass, 3-style ceiling. Appropriate for first real run after dry-run validation.

**`balanced`** (default when no `--preset` given and no `sources.yaml` override) — full ensemble synthesis, two-model detection, up to 5 styles. This is the recommended day-to-day preset.

**`thorough`** — elevated reasoning effort on all synthesis calls; 7-style ceiling; wider cluster search. Use when the balanced run produces style clusters that don't feel right or when the corpus is unusually heterogeneous.

**`maximum`** — all configured models run detection (not just style-weighted), maximum reasoning effort, 200k-char budget, 10-style ceiling. Appropriate for a comprehensive initial profiling of a large corpus, or when you want the most reliable possible profile and cost is secondary.

**Preset application** in `bootstrap.py`:
1. Load `configs/presets.yaml` (raise `PackagedConfigError` if missing or malformed; no hardcoded fallback, same as the pipeline)
2. Apply preset: override `max_input_chars`, `max_styles`, `per_style_min_words`, `synthesis_models`, `detection_models`
3. For each model in preset's `models:` dict: override `model`, `effort`, `reasoning_effort`, `thinking_budget` while preserving infrastructure keys (`provider`, `api_key`, `endpoint`, `timeout_seconds`, `prompts`)
4. Apply any `sources.yaml` explicit keys on top (same override priority as pipeline's `preset_overrides`)
5. CLI flags (`--style`, `--max-styles`) override all of the above

**`--preset` flag** in bootstrap.py:
```
--preset economy|standard|balanced|thorough|maximum
```
Default: `balanced` (if not specified in `sources.yaml synthesis.preset` and not on CLI).

---

## Phase 2 — Normalize

**Goal:** clean corpus, compute metrics, deduplicate.

### `normalize.py`

- `clean_text(raw: str, source: str) -> str` — source-aware:
  - WordPress: strip residual HTML entities, shortcodes `[gallery]` etc.
  - Twitter: normalize `https://t.co/...` → `[link]`, strip leading @mentions on replies
  - Gmail: strip quoted blocks, signature separators (`-- \n`, `___`), boilerplate footers
  - All: normalize Unicode quotes/dashes to ASCII equivalents before metric computation
- `sentence_split(text: str) -> list[str]` — stdlib `re`-based; handles common abbreviations (Mr., Dr., etc.) as non-sentence-ending
- `compute_metrics(doc: Document) -> dict`:
  - `avg_sentence_words`, `median_sentence_words`, `p90_sentence_words`
  - `passive_ratio` — heuristic: `(was|were|is|are|been)\s+\w+ed`
  - `first_person_ratio` — sentences containing I/me/my/we/our
  - `hedging_ratio` — sentences containing may/might/perhaps/could be argued
  - `question_ratio` — sentences ending `?`
  - `avg_paragraph_sentences`
  - `vocab_richness` — type-token ratio (unique words / total words)
- `deduplicate(docs: list[Document]) -> tuple[list[Document], int]` — compare `content_hash`; return (unique docs, n_dropped); log which sources had duplicates
- `corpus_summary(docs: list[Document]) -> dict` — aggregate metrics per source + overall; includes `total_words`, `doc_count`, `date_range`, `source_word_pct` (percentage of total words per source)
- `corpus_bias_warnings(summary: dict) -> list[str]` — returns human-readable warnings when any single register exceeds 75% of total word count; used in dry-run output and synthesis prompt header

---

## Phase 3 — Detect and Synthesize

**Goal:** either detect distinct styles from the corpus automatically, or synthesize a single canonical profile, depending on `--style` mode.

### Style modes

| Mode | What it does | API calls | When to use |
|------|-------------|-----------|-------------|
| `canonical` | All configured models synthesize; outputs consolidated; Claude reconciles | M + 1 | Small corpus or fast first pass |
| `detect` | Style-style-weighted models detect; Claude consolidates clusters; all models synthesize per style; Claude reconciles | D + 1 + (N×M) + 1 | Default; model discovers the structure |
| `per-source` | All models synthesize per source group; consolidated; Claude reconciles | (G×M) + 1 | Fast fallback when sources are clean and well-separated |

M = number of configured models; D = number of detection models (style-weighted subset); N = detected styles; G = source groups

`detect` is the primary multi-style mode. `per-source` is the fallback for when the corpus is too small for reliable detection or when the user wants to force source-based grouping.

---

### `detect.py` (new module)

#### Detection pass
Sends a stratified sample from the full corpus to `prompts/detect_styles.txt`. The sample is drawn proportionally across sources, with enough text from each to give the model representative signal. The model is instructed to:
- Identify N distinct writing styles or styles present in the corpus (N is discovered, not specified; `--max-styles` sets a ceiling, default 5)
- Name each style with a short descriptive label (e.g. "technical analysis", "editorial commentary", "professional correspondence")
- Describe what distinguishes each style
- Provide metric thresholds that characterize each style (sentence length, hedging ratio, etc.) for use in document classification
- Note which source types each style appears in (as a cross-check, not a constraint)
- Rate its confidence in each cluster

**Detection output schema:**
```json
{
  "detected_styles": [
    {
      "label": "technical analysis",
      "description": "Long-form explanatory writing with citations, sequential mechanism walkthrough, low hedging",
      "features": {
        "avg_sentence_words": [">", 16],
        "hedging_ratio": ["<", 0.05],
        "first_person_ratio": ["<", 0.25],
        "vocab_richness": [">", 0.55]
      },
      "source_distribution": {"wordpress": 0.85, "textfiles": 0.15},
      "sample_ids": ["https://mikehammett.net/post-slug-1", "https://mikehammett.net/post-slug-2"],
      "confidence": "high"
    },
    {
      "label": "direct editorial",
      "description": "Shorter, more opinion-forward. First person, shorter sentences, more questions",
      "features": { "avg_sentence_words": ["<", 14], "question_ratio": [">", 0.08] },
      "source_distribution": {"twitter": 0.6, "wordpress": 0.4},
      "confidence": "medium"
    }
  ],
  "detection_notes": "string — logged, not written to output",
  "overall_confidence": "low|medium|high"
}
```

If `overall_confidence` is `low` (corpus too small or too homogeneous to distinguish styles), log a warning and fall back to `canonical` mode automatically.

If **all** detection models fail (API errors, rate-limit exhaustion, network timeout — not just low confidence), log an error for each failed model and fall back to `canonical` with a prominent warning in the output YAML header. Do not raise an unhandled exception. This is the same spirit as `--continue-on-error` for collectors.

#### Document classification (`classify_documents`)
For each document in the full corpus, compute a match score against each cluster's `features` thresholds using the document's pre-computed `metrics` dict. No API call per document.

```python
def classify_documents(
    docs: list[Document],
    clusters: list[StyleCluster],
    ambiguity_threshold: float = 0.2,   # if top-2 cluster scores differ by < threshold, doc is "ambiguous"
) -> tuple[dict[str, list[Document]], list[Document]]:
    # Returns: (cluster_label → assigned docs, ambiguous_docs)
    # Ambiguous docs contribute to canonical only, not per-style synthesis
```

Classification is entirely metric-based — fast even for thousands of documents. Ambiguous documents (score difference between top-two clusters below threshold) are flagged in the dry-run output and excluded from per-style synthesis, contributing only to the canonical pass.

After classification, log per-cluster stats:
```
Detected 3 styles:
  technical analysis:    127 docs  (48,200 words)  — primarily wordpress, textfiles
  direct editorial:       63 docs  (11,400 words)  — primarily twitter, short wordpress
  professional email:     94 docs  (22,100 words)  — primarily gmail, outlook365
  ambiguous / canonical:  18 docs   (4,300 words)
```

Clusters with fewer than `per_style_min_words` words after classification are merged into the nearest cluster (by feature distance) with a warning.

---

### `callers.py`

Thin multi-model caller. Loads model config from `user.yaml` via `load_user_config()`. Routes synthesis prompts to each configured provider using the existing SSE accumulators from `adapters/review/streaming.py`. Returns a dict `{model_name: {"content": str, "failed": bool, "tokens": dict, "elapsed": float}}`.

```python
def call_all(
    system_prompt: str,
    user_prompt: str,
    user_config: dict,           # from load_user_config()
    models: list[str] | None,    # None = all configured; or explicit subset
) -> dict[str, dict]:
    """Call each model in parallel (ThreadPoolExecutor). Returns per-model results."""

def call_one(
    model_name: str,
    model_cfg: dict,
    api_keys: dict,
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Route to the right provider and accumulate SSE response."""
```

Provider routing:
- `anthropic` provider → `accumulate_anthropic` + Anthropic API
- `openai` / `azure` provider → `accumulate_chat_completions` or `accumulate_openai_responses` + OpenAI API
- `ai_studio` / `vertex_ai` provider → `accumulate_gemini` + Gemini API
- `mistral` provider → `accumulate_chat_completions` + Mistral API
- `grok` provider → `accumulate_chat_completions` + Grok API
- `perplexity` provider → `accumulate_chat_completions` + Perplexity API

`call_all` runs providers in parallel up to `synthesis.max_parallel_models` concurrent threads (0 = all at once; default 0). Failed providers are logged with `source_model` in the error; the caller proceeds with available results (mirrors `--continue-on-error` for collectors).

**Perplexity exclusion:** Perplexity runs a web search before every response. For corpus analysis tasks (synthesis, detection) this introduces irrelevant external content and adds latency. Perplexity is excluded from synthesis and detection calls by default. It may still be included explicitly by listing it in `synthesis.detection_models`. This is distinct from how the review pipeline uses Perplexity (where web grounding is the point).

**Import direction** (to prevent circular dependency): `style_consolidation.py` imports from `callers.py`; `callers.py` imports from `adapters/review/streaming.py` only. Neither imports back up the chain. `detect.py` imports from both `callers.py` and `style_consolidation.py`.

### `style_consolidation.py`

Merges per-model synthesis outputs into a single consolidated profile. Analogous to `consolidation.py` but operates on prose + lists rather than passage flags.

```python
DEFAULT_STYLE_WEIGHTS = {
    "openai":     1.2,   # from consolidation.py voice_style weights
    "claude":     1.1,
    "mistral":    1.0,
    "gemini":     1.0,
    "grok":       1.0,
    "perplexity": 1.0,
}
```

**`consolidate_lists(results, key, threshold, weights) -> list[str]`**
For `banned_words`, `banned_phrases`, `positive_rules`: include an item only if the weighted sum of models that produced it meets `threshold` (default 2.0, same as review pipeline). Items from higher-weight models need fewer co-signers. Items present in only one low-weight model are dropped; items present in Claude + OpenAI clear the threshold immediately.

**`collect_prose(results, key, weights) -> list[dict]`**
For `style_profile` and `audience_*` prose: collect all model outputs sorted by weight descending. These are not merged algorithmically — they are passed to the Claude reconciliation pass as input, with each model's output labeled by name and weight.

**`consolidate_detection(detection_results, weights) -> list[StyleCluster]`**
For the detection pass with multiple models: collect all detected style clusters, group semantically similar ones across models (by feature overlap and description similarity), produce a unified cluster set. This is itself a Claude API call using `prompts/consolidate_detection.txt` — the model receives all detection outputs and produces the final agreed-upon cluster list.

### `synthesize.py`

**Token budgeting (all modes):**
```python
corpus_budget = (
    config["synthesis"]["max_input_chars"]
    - config["synthesis"]["prompt_overhead_chars"]
)
```

#### `canonical` mode
1. **All configured models** called in parallel via `callers.call_all()` with `prompts/synthesize_canonical.txt` and the full sampled corpus
2. **List consolidation** via `style_consolidation.consolidate_lists()` — `banned_words`, `banned_phrases`, `positive_rules` filtered by weighted threshold (items that only one minor model flags are dropped)
3. **Prose collection** via `style_consolidation.collect_prose()` — all models' `style_profile` outputs collected, sorted by weight
4. **Reconciliation** — Claude called with `prompts/synthesize_reconcile.txt` receiving the consolidated lists and all prose outputs; produces the final unified profile

#### `detect` mode
1. **Detection** — `callers.call_all()` with `prompts/detect_styles.txt`, but only the *style-weighted* models (those with `voice_style` weight ≥ 1.0 in `DEFAULT_STYLE_WEIGHTS`; by default Claude + OpenAI). Reduces cost on the detection pass without sacrificing style-analysis quality.
2. **Detection consolidation** — `style_consolidation.consolidate_detection()` calls Claude with `prompts/consolidate_detection.txt` to reconcile the per-model cluster sets into a unified `list[StyleCluster]`
3. **Classification** — `detect.classify_documents()`, metric-based, no API calls
4. **Per-style synthesis** — for each cluster: `callers.call_all()` with `prompts/synthesize_per_style.txt` using only that cluster's documents; list consolidation and prose collection
5. **Canonical reconciliation** — Claude called with all per-style consolidated outputs; produces canonical profile + validated per-style deltas

#### `per-source` mode
Same flow as `detect` from step 4 onward, but grouping is by the `per_source_group_by` config key instead of detected cluster. Skips detection and classification entirely.

- `per_source_group_by: source` (default): groups by collector `SOURCE_NAME` — keeps `gmail` and `outlook365` as separate groups even though both have `register: "correspondence"`
- `per_source_group_by: register`: groups by `Document.register` — merges `gmail` and `outlook365` into a single `"correspondence"` group; useful for a coarser split

Each group name becomes a key in `style_profiles:` in the output.

---

**Streaming:** `callers.call_all()` runs providers in parallel; each individual provider call uses SSE streaming via `stream_timeout` + the appropriate accumulator. Progress logged per-provider as calls complete: `"openai: synthesis complete (1,842 tokens, 14.2s)"`.

**Reconciliation model:** always Claude, regardless of what other models are configured. This matches the pipeline's established pattern where Claude acts as the reconciler.

---

**Output schema (`detect` and `per-source` modes):**
```json
{
  "canonical": {
    "style_profile": "string",
    "audience_primary": "string",
    "audience_secondary": "string or null",
    "banned_words": ["..."],
    "banned_phrases": ["..."],
    "positive_rules": ["..."],
    "confidence": "low|medium|high"
  },
  "detected_styles": {
    "technical analysis": {
      "style_profile": "string — what's distinctive here",
      "additional_banned_words": ["..."],
      "additional_positive_rules": ["..."],
      "style_notes": "string",
      "source_distribution": {"wordpress": 0.85},
      "doc_count": 127,
      "confidence": "high"
    },
    "direct editorial": { "..." },
    "professional email": { "..." }
  },
  "synthesis_notes": "string — logged, not written to YAML"
}
```

Style labels in the output are model-generated strings (from the detection pass), not fixed register names.

---

**Output validation:**
```python
def validate_synthesis_output(raw: dict, mode: str) -> dict:
    if mode == "canonical":
        required = ["style_profile", "audience_primary", "banned_words", "banned_phrases", "positive_rules"]
        canonical = raw
    else:
        required = ["canonical", "detected_styles"]
        missing = [k for k in required if not raw.get(k)]
        if missing:
            raise SynthesisError(f"Missing top-level keys: {missing}")
        canonical = raw["canonical"]
    # Validate canonical section
    canonical_required = ["style_profile", "banned_words", "banned_phrases", "positive_rules"]
    missing = [k for k in canonical_required if not canonical.get(k)]
    if missing:
        raise SynthesisError(f"Canonical profile missing: {missing}")
    return raw
```

**Streaming:** SSE streaming on every API call; progress indicator names the current pass ("Detection pass...", "Synthesizing style: technical analysis...", "Reconciliation pass...").

**Model:** configurable via `synthesis.model`; default `claude-sonnet-4-6`.

---

## Phase 4 — Output

**Goal:** format the validated profile dict and write it safely.

### `output.py`

**`PublicationYamlFormatter`:**
- `format(profile: dict, mode: str) -> str`: produces YAML shaped by style mode:
  - `canonical`: `style_profile`, `audience`, `style_rules` at top level — backward-compatible with pipeline
  - `detect` / `per-source`: canonical at top level AND a `style_profiles:` block keyed by model-generated style labels:
    ```yaml
    style_profile: |   # canonical — pipeline uses this by default
      ...
    style_rules:
      banned_words: [...]
      ...
    style_profiles:    # auto-detected; not yet consumed by pipeline (Phase 9)
      technical analysis:
        style_profile: |
          ...
        additional_banned_words: [...]
        additional_positive_rules: [...]
        style_notes: |
          ...
        source_distribution: {wordpress: 0.85, textfiles: 0.15}
      direct editorial:
        ...
    ```
- `merge_into_existing(existing_path: str, profile: dict, mode: str) -> str`: load with `ruamel.yaml`; replace style sections and `style_profiles:` block if present; preserve all other keys
- `diff_style_sections(existing_path: str, new_yaml: str) -> str`: unified diff of both canonical and `style_profiles:` sections

**`MarkdownReportFormatter`:**
- Produces a human-readable `.md` summary: style profile prose, audience description, banned words/phrases as a table, positive rules as a numbered list
- Useful for sharing the synthesized profile without embedding it in a config file

**`JsonFormatter`:**
- Raw JSON dump of the profile dict; for programmatic use or piping to other tools

**Atomic write:**
```python
tmp = output_path.with_suffix(".tmp")
tmp.write_text(content, encoding="utf-8")
tmp.replace(output_path)   # atomic on POSIX and Windows (same drive)
```

**Internal → YAML key mapping:** the synthesized profile dict uses `detected_styles` as the key internally; `output.py` maps this to `style_profiles:` in the emitted YAML. This distinction matters for test assertions — mock the internal dict with `detected_styles`; assert the YAML output contains `style_profiles:`.

**Profile versioning:**
After writing the primary output, always save a timestamped copy:
- `--publication <name>` → `profiles/<name>/<ISO8601>.yaml`
- `--output-yaml path/to/file.yaml` → `profiles/_output/<stem>/<ISO8601>.yaml` (stem = filename without extension)

This gives drift detection for free: diff consecutive snapshots to see how the profile evolves.

---

## Phase 5 — Tests

Conventions mirror the pipeline: all network calls mocked, no real API keys in tests, fixtures in `tests/fixtures/`.

### `tests/test_collectors.py`
Per collector (WordPress, Twitter, Gmail, Outlook365, TextFiles):
- Happy path: mock response from fixture → assert correct `Document` field values, `content_hash` set, `metadata` populated
- Auth error (401/403): assert `CollectorError` with source name in message
- Server error (503): assert retry logic fires 3 times then raises `CollectorError`
- Rate limit (429 with `Retry-After: 2`): mock two calls; assert backoff delay
- Pagination: mock 3 pages → assert total doc count correct
- `validate_config`: assert `ConfigError` on missing required keys
- WordPress: assert `allow_redirects=False` in request kwargs
- Gmail + Outlook365: assert `max_messages` cap enforced on large result sets
- Outlook365: assert `Prefer: outlook.body-content-type="text"` header present; assert `device_code` flow invoked when token file absent

### `tests/test_normalize.py`
- `clean_text` strips HTML entities, email quote blocks, Twitter `t.co`, WP shortcodes
- `sentence_split` handles abbreviations correctly (Dr., Mr., U.S. don't split)
- `compute_metrics` on known paragraphs returns expected ratios
- `deduplicate` drops exact-hash duplicates and returns correct count
- `corpus_summary` aggregates per-source and overall correctly
- `corpus_bias_warnings` fires at >75% single-register threshold; silent below

### `tests/test_detect.py`
- `detect_styles()`: mock SSE detection response → assert `StyleCluster` list returned with correct fields
- `detect_styles()`: `overall_confidence: "low"` in response → assert `CanonicalFallbackWarning` raised; caller falls back to canonical
- `classify_documents()`: fixture documents with known metrics → assert correct cluster assignment
- `classify_documents()`: docs with scores within `ambiguity_threshold` → assert placed in ambiguous bucket
- `classify_documents()`: cluster below `per_style_min_words` after assignment → assert merged into nearest cluster
- Dry-run: `--dry-run` with `detect` mode runs detection pass, prints cluster summary, exits before synthesis

### `tests/test_callers.py`
- `call_one` Anthropic provider: mock `accumulate_anthropic` SSE response → assert `content` and `tokens` returned
- `call_one` OpenAI provider: mock `accumulate_chat_completions` → assert correct endpoint used
- `call_one` Gemini provider: mock `accumulate_gemini` → assert `usageMetadata` mapped to `tokens`
- `call_one` provider fails (5xx): assert `{"failed": True, "error": ...}` returned; does not raise
- `call_all` with 3 providers: assert all 3 called in parallel (mock `ThreadPoolExecutor`); assert all results keyed by model name
- `call_all` with `models=["claude"]`: assert only Claude called; others skipped

### `tests/test_style_consolidation.py`
- `consolidate_lists`: item present in Claude (1.1) + OpenAI (1.2) → weight_sum 2.3 ≥ 2.0 → included
- `consolidate_lists`: item present only in Mistral (1.0) → weight_sum 1.0 < 2.0 → excluded
- `consolidate_lists`: item present in all 4 models → included regardless
- `collect_prose`: returns list sorted by weight descending; Claude and OpenAI outputs first
- `consolidate_detection`: mock Claude call with `prompts/consolidate_detection.txt` → assert unified cluster list returned

### `tests/test_synthesize.py`
- `canonical` mode: mock `call_all` (3 models) + mock reconciliation (Claude) → assert 4 total calls; consolidated lists threshold applied; prose passed to reconciliation
- `detect` mode: mock detection (2 style-style models) + mock consolidation + 3 per-style × 3 models + reconciliation → assert correct call graph; `detected_styles` keyed by model-generated labels
- `per-source` mode: assert detection/classification skipped; only synthesis and reconciliation calls
- `SynthesisError` raised when reconciliation response missing required keys
- Sampling: corpus does not exceed `corpus_budget`; most-recent docs preferred within each cluster
- All-models-fail: assert `SynthesisError` with "no model produced a valid synthesis result"

### `tests/test_output.py`
- `PublicationYamlFormatter.format()` in `canonical` mode: valid YAML, required keys, no `style_profiles:` block
- `PublicationYamlFormatter.format()` in `detect` mode: canonical at top level AND `style_profiles:` block keyed by model-generated labels (not register names)
- `merge_into_existing()` preserves `wordpress`, `rank_math`, `citation_sources` sections unchanged in all modes
- `merge_into_existing()` in `detect` mode: replaces existing `style_profiles:` block; adds it if absent; does not touch other sections
- `diff_style_sections()` diffs both top-level and `style_profiles:` sections; empty diff when identical
- Atomic write: `.tmp` cleaned up on success; original preserved on write failure (simulate interrupt)
- `MarkdownReportFormatter` produces a section per detected style with label as heading; `source_distribution` shown as a small table
- Profile versioning: snapshot written to `profiles/<name>/` on every successful run regardless of mode

### `tests/test_logging.py`
- `human` format: assert output contains timestamp, level, logger name, message
- `json` format: assert each line is valid JSON; assert required keys present (`ts`, `level`, `logger`, `msg`)
- `json` format with `extra={"page": 3}`: assert `"page": 3` appears at top level of JSON record
- Per-module override: `style_profile_bootstrap.collectors.gmail` set to DEBUG → assert DEBUG messages from gmail module appear; assert DEBUG messages from wordpress module suppressed
- File handler: assert log written to file; assert stdout also receives output when `also_stdout: true`
- `--log-level DEBUG` CLI flag overrides config-level `INFO` globally

### `tests/test_bootstrap.py`
- `--dry-run` exits before synthesis; prints corpus stats; no output file written
- `--continue-on-error`: one collector raises `CollectorError`; run completes with remaining sources; warning in output
- `--refresh`: staging cache ignored, watermarks reset
- `--publication mikehammett`: resolves to `configs/mikehammett.yaml`
- Unknown `--publication`: `FileNotFoundError` with helpful message

---

## Phase 6 — README and gitignore

### `README.md`
- Prerequisites (Python version, Twitter API tier requirement)
- Setup: install deps, create `sources.yaml` from example, set env vars
- Gmail OAuth one-time flow (step by step; note credentials file permission requirement)
- Usage: dry-run first, then full run, then merge into publication.yaml
- Privacy notes: what staging stores, cloud sync risk, `--no-stage` option
- Output format: how synthesized fields map to publication.yaml sections
- Adding a custom collector (drop a file in `collectors/custom/`)
- Interpreting the confidence field and synthesis_notes log

### Gitignore additions (to repo root `.gitignore`)
```
style-profile-bootstrap/sources.yaml
style-profile-bootstrap/staging/
style-profile-bootstrap/profiles/
```

### `requirements.txt`
```
anthropic>=0.30
google-auth>=2.0
google-auth-oauthlib>=1.0
google-api-python-client>=2.0
msal>=1.20               # Microsoft Authentication Library for Outlook 365
requests>=2.31
ruamel.yaml>=0.18        # round-trip YAML preserves comments in existing publication.yaml
python-docx>=1.1         # optional, for .docx textfiles input
```

---

## Phase 7 — Style consistency check (planned, not Session 2)

`--check-draft <path>` flag on `bootstrap.py`. Uses the existing multi-model infrastructure already built for synthesis — this is where the reuse pays off clearly.

**Implementation:** inject the synthesized `style_profile` (and optionally the detected per-style profile for the intended venue) into the existing `voice_style` domain prompt from the review pipeline. Run via `callers.call_all()` against all configured models. Consolidate via `style_consolidation` list/flag merging.

**Why this works without rebuilding anything:** the `voice_style` domain prompt is already designed to evaluate style against a stated profile. The synthesized `style_profile` is exactly the kind of reference it expects. The only addition is injecting the profile text at the top of the prompt. The flag schema (`flags[].passage`, `flags[].rule`, `flags[].suggestion`) is identical to what `consolidation.py` already processes.

**Output:** `{similarity_score: 0.0–1.0, deviations: [{passage, rule_violated, suggestion, models: [...], weight_sum: float}]}`

`similarity_score` is `1.0 - (weighted_flag_count / sentence_count)` — a rough measure, not a precise metric, but consistent across runs.

Future integration: this could be wired as a pre-pipeline pass — before the 5-domain review, check style consistency and return it to the author for revision before spending the full ensemble budget on a draft that doesn't sound like them yet.

---

## Phase 8 — ConvertKit collector (planned, not Session 2)

Newsletter content is high-signal long-form writing. ConvertKit's REST API (`GET /v4/broadcasts`) is available and already connected as an MCP server in the current environment. A `collectors/custom/convertkit.py` (living in custom/ until promoted to a built-in) would follow the same `Collector` interface and yield long-form newsletter emails with register `long_form` (newsletters are closer to blog posts than email correspondence).

Config:
```yaml
convertkit:
  api_key: ${CONVERTKIT_API_KEY}
  status: public          # only published broadcasts
  max_broadcasts: 200
```

---

## Ecosystem reuse

All imports assume invocation from the repo root. Pipeline orchestration modules (`pipeline.py`, `consolidation.py`, `handoff_parser.py`) are not imported — only stable leaf utilities and transport infrastructure.

| Import | From | Why |
|--------|------|-----|
| `from adapters.cms.wordpress import _auth_header` | existing | identical Basic auth scheme for WP REST API |
| `from analysis.links import _is_public_host` | existing | SSRF guard for `site_url` validation at config-validation time |
| `from config_loader import load_user_config, _resolve_env_recursive, _load_yaml` | existing | load model config and API keys from `user.yaml`; `${ENV_VAR}` substitution in `sources.yaml`; `_load_yaml` for loading `configs/presets.yaml` with the same error handling |
| `from adapters.review.streaming import iter_sse_data, accumulate_chat_completions, accumulate_anthropic, accumulate_gemini, accumulate_openai_responses, stream_timeout` | existing | all four SSE accumulators and timeout builder — import directly, no reinvention |
| `from adapters.review.json_utils import extract_json` | existing | robust JSON extraction from model responses; handles fenced blocks, prose-wrapped JSON, reasoning preambles — critical for `callers.py` parsing synthesis/detection results |
| `from analysis.readability import analyze as readability_analyze` | existing | Flesch-Kincaid grade + reading ease + avg sentence length; pure Python, no deps; adds higher-fidelity sentence-complexity metrics to `Document.metrics` beyond our own `avg_sentence_words` heuristic |
| `from analysis.cost import calculate as cost_calculate` | existing | token-count-based cost estimation using shared `configs/pricing.yaml`; feed the api_call_log from `callers.py` to estimate actual spend after each run; displayed in run summary |
| `from model_registry import check_model_currency` | existing | warn at startup if any model configured in `user.yaml` is superseded or stale; same warning the pipeline surfaces |

**New imports rationale:**

- **`json_utils.extract_json`**: without this, `callers.py` would need its own JSON-from-prose parser. The pipeline already solved this robustly across all six providers — reuse it directly.
- **`readability.analyze`**: the Flesch-Kincaid grade level is a better discriminator for style clusters (e.g., "technical analysis" reads at grade 14; "casual editorial" reads at grade 9) than raw avg_sentence_words alone. Adding it to `Document.metrics` requires one import, no new logic.
- **`cost.calculate`**: after each synthesis run, log actual spend. The pipeline tracks this per-pass; we log it once at run end. Same pricing table, same token-counting approach.
- **`model_registry.check_model_currency`**: the style profiler runs infrequently (weekly or less). Users may not notice when their configured Claude model gets superseded by a newer one. Surfacing the same staleness warning the pipeline does costs nothing.

**Model weights:** `callers.py` applies the same `_DEFAULT_WEIGHTS` pattern from `consolidation.py` — `openai: {voice_style: 1.2}`, `claude: {voice_style: 1.1}` — but does not import from `consolidation.py` directly (the domain name used as a key differs; the weight values are documented and copied).

**HTML stripping** for `content.rendered`: replicate the stdlib HTMLParser approach from `analysis/webpage.py` — do not import it, as that module is coupled to article fetching with a different calling convention.

**Preset loading:** use `config_loader._load_yaml()` for loading `configs/presets.yaml` (consistent error handling, path resolution). Preset application logic is self-contained in `bootstrap.py` — do not import `_apply_cost_preset` from `config_loader` (it couples to pipeline's model config shape).

---

## Contributions to the pipeline

Utilities built for the style profiler that would benefit the review pipeline. File tickets / PRs when the style profiler session is complete.

| What | Style profiler module | What it adds to the pipeline |
|------|----------------------|------------------------------|
| **Configurable modular logging** | `logging_config.py` | Pipeline currently uses basic `logging.basicConfig`. `logging_config.py` adds per-module level overrides, JSON format, optional file handler, and `--log-level` CLI override. Zero-risk addition: existing `logging.getLogger()` calls throughout the pipeline work unchanged. |
| **`style_consolidation.consolidate_lists()`** | `style_consolidation.py` | The weighted list intersection + threshold logic is more general than the pipeline's `_build_flags_section()`, which is tightly coupled to flag objects with passage text. The generalized version could eventually replace the per-domain mergers in `consolidation.py`. |
| **Preset `"*"` sentinel for detection_models** | `configs/presets.yaml` | The sentinel for "all configured models" (vs. an explicit list vs. empty = style-weighted subset) is a clean pattern for any future pipeline preset that needs a "use everything" tier. |
| **`analysis/readability.py` in `Document.metrics`** | `normalize.py` | The pipeline already has `analysis/readability.py` but doesn't use it in the review workflow. The style profiler's use of Flesch-Kincaid as a style-cluster feature could motivate surfacing readability in the pipeline's article pre-analysis section (currently just SEO + grammar pre-pass). |
| **Pre-call cost estimate log line** | `bootstrap.py` startup | The pipeline logs cost *after* the run (via `analysis/cost.py`). The style profiler logs expected call count *before* synthesis so the user can abort. This pre-run estimate pattern is worth backporting: the pipeline already knows its (model, domain) assignment list before calling any model. |
| **Corpus bias warnings pattern** | `normalize.corpus_bias_warnings()` | Generalizes to any ensemble input where one source dominates — useful for detecting when the pipeline's citation sources are all from one domain (e.g., 80% EIA data on a non-energy article). |

---

## Shared core (future extraction)

Both the pipeline and the style profiler share enough infrastructure that a `shared-core/` package extraction is warranted when the two projects are both stable. Keep this in mind during implementation: avoid tight coupling to either project's domain-specific types.

**Candidate modules for extraction:**

```
shared-core/
  config_loader.py          ← YAML load + env substitution + preset apply + _load_yaml
  logging_config.py         ← modular logging (new; style profiler contributes this back)
  timeout_model.py          ← sliding-scale timeout computation
  model_registry.py         ← staleness checking
  consolidation/
    weighted.py             ← _find_consensus + consolidate_lists + collect_prose
    passage_key.py          ← 250-char normalized key (currently in consolidation.py)
  analysis/
    readability.py          ← Flesch-Kincaid (pure Python)
    cost.py                 ← token-count pricing
    links.py                ← extract_urls + _is_public_host + validate_links
  adapters/
    streaming.py            ← iter_sse_data + all four accumulators
    json_utils.py           ← extract_json
```

**What stays project-specific:**
- `pipeline.py`, `consolidation.py` (article review domain logic)
- `style-profile-bootstrap/collectors/`, `normalize.py`, `detect.py`, `synthesize.py` (corpus/style domain logic)
- `adapters/review/` provider files (claude.py, gemini.py, etc.) — stay with pipeline until style profiler's `callers.py` needs the same routing, at which point they unify
- `adapters/cms/`, `adapters/grammar/`, `adapters/citation/` — pipeline-specific; no style-profiler equivalent

**Extraction trigger:** when `callers.py` is working in Session 2 and its provider routing duplicates the review adapter routing pattern, that's the moment to propose extraction rather than copy-pasting. Don't extract prematurely — let the duplication exist through Session 2, then consolidate.

---

## Build order

Split into two implementation sessions at the `--dry-run` milestone.

### Session 1 — Collectors + normalize (deliverable: `--dry-run` works end-to-end)

| Step | Deliverable | Depends on |
|------|-------------|------------|
| 1a | `collectors/base.py` (Document, Collector, exceptions) | nothing |
| 1b | `collectors/wordpress.py` | 1a |
| 1c | `collectors/twitter.py` | 1a |
| 1d | `collectors/gmail.py` | 1a |
| 1e | `collectors/outlook365.py` | 1a |
| 1f | `collectors/textfiles.py` | 1a |
| 1g | `collectors/__init__.py` registry + custom/ hook | 1b–1f |
| 1h | `sources.example.yaml` + gitignore additions | nothing |
| P  | `configs/presets.yaml` (five presets; required, no hardcoded fallback in bootstrap.py) | nothing |
| L  | `logging_config.py` | nothing |
| 2  | `normalize.py` (clean, split, metrics via readability_analyze + our own heuristics, deduplicate, summary, bias warnings) | 1a |
| 5a | `tests/test_collectors.py` + `tests/test_normalize.py` + `tests/test_logging.py` + fixtures | 1a–1f, L, 2 |
| 1i | `bootstrap.py` through `--dry-run` (preset load, logging init, startup validation, parallel collection, deduplicate, normalize, stats, exit) | 1g, L, P, 2 |

End state: `python style-profile-bootstrap/bootstrap.py --sources wordpress --dry-run` applies logging config, fetches posts, deduplicates, prints corpus stats with bias warnings, exits cleanly.

### Session 2 — Detection + multi-model synthesis + output (deliverable: full end-to-end run, all style modes)

| Step | Deliverable | Depends on |
|------|-------------|------------|
| 3  | All prompt files (`detect_styles.txt`, `consolidate_detection.txt`, `synthesize_canonical.txt`, `synthesize_per_style.txt`, `synthesize_reconcile.txt`, `synthesize_per_source.txt`) | nothing |
| C  | `callers.py` (multi-model routing, `call_one`, `call_all`, streaming via imported accumulators, `extract_json` for response parsing, `cost_calculate` for run-end spend logging, `check_model_currency` at startup) | 3, ecosystem imports |
| VC | `style_consolidation.py` (`consolidate_lists`, `collect_prose`, `consolidate_detection`) | C |
| D  | `detect.py` (`detect_styles` using C+VC, `classify_documents`) | 2 (metrics), C, VC |
| 4  | `synthesize.py` (orchestrates C+VC+D for all three modes) | C, VC, D |
| 5  | `output.py` (all formatters, merge, diff, atomic write, profile versioning) | 4 |
| 5b | `tests/test_callers.py` + `tests/test_style_consolidation.py` + `tests/test_detect.py` + `tests/test_synthesize.py` + `tests/test_output.py` + `tests/test_bootstrap.py` | C, VC, D, 4, 5 |
| 1i | `bootstrap.py` synthesis + output + all flags integration | C, VC, D, 4, 5 |
| 6  | `README.md` | 1–5 |

---

## Open questions (resolved for v1)

| Question | Decision |
|----------|----------|
| Venue register weighting | Prompt-level instruction; `long_form` authoritative, `casual` supplemental; documented in `synthesize.txt` |
| Minimum corpus size | Warn (and note in output YAML) if `long_form` word count < 5,000; error if < 1,000 total |
| Private email content | Staging gitignored; `--no-stage` for zero-persistence; cloud sync risk noted in README |
| Temporal drift | `--since` applied at API level (WP `after=`, Twitter `start_time=`); post-filter for sources without API date params |
| Re-run / delta updates | Watermarks in `staging/.watermarks.json`; `--refresh` resets |
| Standalone vs. pipeline | Standalone CLI; three leaf-function imports from pipeline; no orchestration coupling |
| Twitter API tier | Requires Basic ($100/mo) for general access; Free tier covers own-timeline only (last 7 days); documented in README |
| `ruamel.yaml` vs `PyYAML` | `ruamel.yaml` for merge/output; preserves comments in existing publication.yaml |
| Output path convention | `--publication <name>` primary (mirrors pipeline CLI); `--output-yaml` escape hatch |
| Partial collector failure | `--continue-on-error` skips failed sources; warning written into output YAML header |
| Corpus deduplication | SHA-256 of cleaned text; deduplicate before normalization; log which sources had overlaps |
| Large Gmail mailboxes | `max_messages` config (default 500); most-recent-first sampling when limit hit |
| Synthesis prompt tuning | Externalized to `prompts/synthesize.txt`; not embedded in Python |
| Multi-pass synthesis | Per-register passes + reconciliation if corpus can't be adequately represented in one call |
| Profile versioning | Every run saves to `profiles/<publication>/<date>.yaml` automatically |
| Style consistency check | `--check-draft` flag planned as Phase 7 (not Session 2) |
| ConvertKit collector | Planned as Phase 8 (not Session 2); API is available |
| Model config source | API keys and model selection come from `user.yaml` (not `sources.yaml`); style profiler reuses the existing model infrastructure without any new key setup |
| Models for detection pass | Style-style-weighted subset only (Claude + OpenAI by default); avoids running all 6 models on detection where the quality benefit is marginal |
| Models for synthesis | All configured models, parallel via `callers.call_all()` — same pattern as the review pipeline |
| Reconciliation model | Always Claude; not configurable; matches pipeline's established reconciler role |
| Consolidation threshold | `consensus_threshold: 2.0` (default, same as review pipeline); banned words/phrases need weighted score ≥ 2.0 to be included |
| `--check-draft` infrastructure reuse | Injects synthesized profile into existing `voice_style` domain prompt; runs via `callers.call_all()`; consolidates via `style_consolidation`; no new prompt needed |
| Outlook365 auth for MSA vs. org | `tenant_id: common` for personal accounts; tenant UUID for org; `auth_method` selects device_code vs. client_credentials |
| Outlook365 token refresh expiry | Catch `InteractionRequiredAuthError` and re-run device-code flow with a clear log message; don't let MSAL surface a cryptic exception |
| Gmail + Outlook staging privacy | Same policy: gitignored staging, `--no-stage` flag, cloud sync warning in README under combined "Email sources" section |
| Multi-style output compatibility | Canonical always written at top level; `style_profiles:` block added only in detect/per-source modes; pipeline unchanged |
| Internal→YAML key mapping | Internal dict key `detected_styles` → emitted YAML key `style_profiles:`; mapping is in `output.py`, not synthesize.py |
| Style detection low-confidence fallback | If detection pass returns `overall_confidence: "low"`, automatically fall back to `canonical` with a warning |
| Style detection total-failure fallback | If all detection models fail with errors (not just low confidence), fall back to `canonical` with a warning in output YAML header; do not raise |
| Style cluster minimum corpus | Clusters below `per_style_min_words` after classification merged into nearest cluster (by feature distance), not skipped |
| Multi-style API call count | `canonical`: M + 1; `detect`: D + 1 + (N×M) + 1; `per-source`: (G×M) + 1 — surfaced in pre-run log |
| Document classification cost | Purely metric-based (no API calls); uses `Document.metrics` computed during normalize phase; metrics never read from staging files |
| Ambiguous document handling | Docs where top-two cluster scores differ by less than `ambiguity_threshold` go to canonical only; flagged in dry-run output |
| Style label stability | Model-generated labels may differ between runs; profiles dir timestamps let you correlate runs; label matching in merge is by exact string — if labels change, diff will show full replacement rather than delta |
| `--dry-run` with detect mode | Runs detection pass (one API call) and prints cluster summary; useful for validating detection quality before committing to full synthesis; `--help` text must note this costs money |
| `--dry-run` with canonical/per-source | Zero API calls — collect, normalize, stats only; completely free |
| Log format selection | `human` for interactive use; `json` for piping to aggregators or `jq`; selectable per-run via `--log-level` without changing config |
| Logging per-module override use cases | Gmail DEBUG for troubleshooting OAuth; synthesize WARNING to suppress sampling noise on routine runs |
| per-source grouping key | `per_source_group_by: source` (default) groups by collector name; `register` merges collectors with the same register tag; config in sources.yaml |
| Perplexity in synthesis | Excluded by default (web grounding adds noise for corpus analysis); can be included explicitly in `detection_models` |
| Pipeline multi-style integration | `style_profiles:` block written but not yet consumed by the review pipeline; Phase 9 (future) would add `--venue` flag to pipeline selecting which profile the `voice_style` domain uses |
| Cost estimation | Pre-synthesis, bootstrap.py logs the expected API call count by mode: `"detect mode: ~{D} detection + {N} per-style × {M} models + 1 reconciliation = {total} calls"`. No `--estimate-cost` flag needed — this info is always logged at INFO level before synthesis starts, giving the user a chance to abort. Actual spend logged at run end via `cost.calculate()` from the api_call_log. |
| `argparse` mutual exclusion | `--publication` and `--output-yaml` in `add_mutually_exclusive_group(required=False)` with `required=True` (one must be present) |
| SOURCE_NAME uniqueness | Registry build raises `ConfigError` listing both conflicting class names on duplicate SOURCE_NAME |
| Metrics in staging | Staged NDJSON omits `metrics`; normalize always recomputes from `text`; schema_version is collector-only |
| Preset system | Five tiers (economy→maximum); `configs/presets.yaml`; required, no hardcoded fallback; `--preset` CLI flag; individual `sources.yaml` keys override specific preset values; CLI flags override all |
| Preset priority order | `--preset` + `--style` + `--max-styles` CLI flags > `sources.yaml` explicit keys > preset bundle defaults |
| `detection_models: "*"` | Sentinel value meaning "all configured models" (not just style-weighted subset); used in `maximum` preset; distinct from `[]` (style-weighted) and an explicit list |
| Readability metrics in normalize | Import `analysis.readability.analyze` for Flesch-Kincaid grade + reading ease; add `flesch_kincaid_grade` and `flesch_reading_ease` to `Document.metrics`; better discriminator for style clusters than sentence-word count alone |
| JSON extraction in callers | Use `adapters.review.json_utils.extract_json()` for all model response parsing; handles fenced blocks, prose wrapping, reasoning preambles — already battle-tested across all six providers |
| Model currency at startup | Call `model_registry.check_model_currency()` after loading user.yaml; log stale/superseded warnings; non-fatal; same UX as pipeline |
| Shared core extraction timing | Don't extract during Sessions 1 or 2. After Session 2 completes and callers.py works end-to-end, evaluate: if callers.py routing substantially duplicates the review adapter pattern, propose `shared-core/` extraction at that point. |
