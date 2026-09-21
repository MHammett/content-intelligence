#!/usr/bin/env python3
"""
Connectivity check — verifies every configured API key works before
you submit a real article. Makes one minimal call to each service
and reports pass/fail with actionable error messages.

Respects your configured provider (AI Studio, Vertex AI, Azure OpenAI,
Azure Mistral) so the check hits the same endpoint the pipeline will use.

Usage:
  python check.py --publication your_publication_name
"""

import sys

# Provider and WordPress failures are reported verbatim, and their text is not
# ours to keep inside cp1252.
from ci_core.console import force_utf8_stdio

force_utf8_stdio()

if sys.version_info < (3, 10):
    print(f"ERROR: Python 3.10+ required. You are running {sys.version}.")
    sys.exit(1)

import importlib.util

_REQUIRED = {"requests": "requests", "yaml": "pyyaml", "dotenv": "python-dotenv"}
_missing = [pkg for mod, pkg in _REQUIRED.items() if not importlib.util.find_spec(mod)]
if _missing:
    print(f"ERROR: Missing packages. Run: pip install {' '.join(_missing)}")
    sys.exit(1)

import argparse
import json
import requests
import base64

from .config_loader import (
    apply_api_key_overrides,
    describe_api_key_sources,
    load_user_config,
    load_publication_config,
    merge_configs,
    parse_api_key_overrides,
    validate_publication_name,
)
from ci_core.redact import redact_url_keys

# Labels for describe_api_key_sources()'s `source` field. The
# env_overrides_os_env case gets a visibly different label on purpose -- it's
# the disagreement that used to cost real money by going unnoticed for days
# (see ci_core.env_provenance). .env wins either way now; this is a heads-up,
# not an alarm.
_KEY_SOURCE_LABELS = {
    "env": "[.env]",
    "env_overrides_os_env": "[.env — differs from an OS env var, .env wins]",
    "os_env": "[OS ENV]",
    "literal": "[user.yaml, literal]",
    "unset": "[NOT SET]",
}


def print_key_sources(config_dir):
    """Print a masked, provenance-annotated view of every configured API key.

    Reads user.yaml directly and never makes a network call -- this is meant
    to answer "which key is this run actually about to use, and where did it
    come from" in under a second. Only reflects the user.yaml/.env/OS-env
    tiers -- a publication config's own api_keys section or a --api-key CLI
    override (both higher-precedence) can still change what an actual run
    uses; this is the fallback those layers sit on top of.
    """
    entries = describe_api_key_sources(config_dir)
    print("\nAPI key sources (masked):\n")
    for entry in entries:
        label = _KEY_SOURCE_LABELS[entry["source"]]
        var = f"  (${{{entry['var_name']}}})" if entry["var_name"] else ""
        print(
            f"  {entry['provider']}.{entry['field']:<10} {label:34s} {entry['masked_value']}{var}"
        )
        if entry["source"] == "env_overrides_os_env":
            print(
                "      └─ an OS environment variable also sets this, with a "
                "different value -- .env wins under this project's "
                "precedence (CLI > publication config > .env > OS env)"
            )
    print()


PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"
SKIP = "\033[33m SKIP\033[0m"


def check(label, fn):
    try:
        msg = fn()
        print(f"  {PASS}  {label}{(' — ' + msg) if msg else ''}")
        return True
    except requests.HTTPError as e:
        # Include the response body so model/auth errors are immediately actionable.
        body = redact_url_keys(e.response.text[:300]) if e.response is not None else ""
        detail = f"{redact_url_keys(e)}"
        if body:
            detail += f"\n         {body}"
        print(f"  {FAIL}  {label} — {detail}")
        return False
    except Exception as e:
        # Gemini AI Studio carries the key as a URL query param; network errors
        # embed the full URL in the exception string.  Redact before printing.
        print(f"  {FAIL}  {label} — {redact_url_keys(e)}")
        return False


# ---------------------------------------------------------------------------
# Per-provider check functions
# ---------------------------------------------------------------------------


def check_openai(api_key, model):
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        # GPT-5.x requires max_completion_tokens; max_tokens was deprecated for these models.
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_completion_tokens": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    return f"model={model}, replied: {reply!r}"


#: What an Azure check takes from the model config: where to go, not what a
#: preset asks for there.
_AZURE_ROUTE_KEYS = ("model", "endpoint", "deployment", "api_version")


def _check_azure(provider, api_key, cfg):
    """One call down the route the pipeline uses to Azure.

    These checks used to build requests of their own: Chat Completions at
    api-version 2024-02-01 for openai. They passed while every run sent the
    Azure key to api.openai.com and api.mistral.ai instead, and the pipeline
    now sends openai to the Responses API, which 2024-02-01 does not have.
    Going through ci_core.llm.client, as ci-probe does, makes this module's
    promise true by construction: the same URL, key header, api_version and
    streaming as a run.

    Only the route comes from the config. The preset's effort and search stay
    out: this checks the endpoint, the deployment and the key, and ci-probe
    checks what a preset asks of them.
    """
    from ci_core.llm import client

    from .probe import PROBE_PROMPT, PROBE_SCHEMA, PROBE_SYSTEM

    azure_cfg = {key: cfg[key] for key in _AZURE_ROUTE_KEYS if cfg.get(key)}
    azure_cfg["provider"] = "azure"
    result = client.call(
        provider,
        PROBE_SYSTEM,
        PROBE_PROMPT,
        api_key,
        retry=False,
        provider_config=azure_cfg,
        response_schema=PROBE_SCHEMA,
    )
    if result.get("failed"):
        body = result.get("error_body")
        raise Exception(result["error"] + (f"\n         {body}" if body else ""))
    deployment = azure_cfg.get("deployment")
    deployment = f" deployment={deployment}" if deployment else ""
    return (
        f"provider=azure{deployment} model={result['model']}, "
        f"replied: {result['data']!r}"
    )


def check_openai_azure(api_key, cfg):
    return _check_azure("openai", api_key, cfg)


def _extract_gemini_text(candidate):
    """Pull the first non-thinking text part out of a Gemini candidate dict.

    gemini-2.5-flash is a thinking model — it may return one or more parts
    with ``thought: true`` before the actual output part.  Accessing parts[0]
    directly therefore returns a thinking trace, not the reply, and with a very
    small maxOutputTokens the output part may not exist at all (KeyError: 'parts').

    This helper:
    - uses .get() throughout so it never raises KeyError
    - skips parts marked as thought
    - raises a descriptive exception when no text part is found so the caller
      can report what the API actually returned
    """
    parts = candidate.get("content", {}).get("parts", [])
    text_parts = [p for p in parts if not p.get("thought") and "text" in p]
    if text_parts:
        return text_parts[0]["text"].strip()
    # No usable text — surface the finish reason and raw candidate for diagnosis
    finish_reason = candidate.get("finishReason", "unknown")
    raise Exception(
        f"No text in response (finishReason={finish_reason!r}). "
        f"Raw candidate: {json.dumps(candidate)[:300]}"
    )


def check_gemini(api_key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": "Reply with the single word: ok"}]}],
            "generationConfig": {"maxOutputTokens": 64},
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise Exception("No candidates returned")
    reply = _extract_gemini_text(candidates[0])
    return f"model={model}, replied: {reply!r}"


def check_gemini_vertex(cfg):
    """Check Vertex AI Gemini connectivity using google-auth credentials."""
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError:
        raise Exception(
            "google-auth is not installed — required for provider: vertex_ai. "
            "Run: pip install 'google-auth>=2.22.0,<3.0'"
        )

    project = cfg.get("project", "")
    location = cfg.get("location", "us-central1")
    model = cfg.get("model", "gemini-2.5-flash")

    if not project:
        raise Exception(
            "Missing 'project' in gemini model config (required for provider: vertex_ai)"
        )

    credentials_file = cfg.get("credentials_file")
    if credentials_file:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    else:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{model}:generateContent"
    )
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        },
        json={
            "contents": [
                {"role": "user", "parts": [{"text": "Reply with the single word: ok"}]}
            ],
            "generationConfig": {"maxOutputTokens": 64},
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise Exception("No candidates returned")
    reply = _extract_gemini_text(candidates[0])
    return f"provider=vertex_ai project={project} location={location} model={model}, replied: {reply!r}"


def check_mistral(api_key, model):
    resp = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    return f"model={model}, replied: {reply!r}"


def check_mistral_azure(api_key, cfg):
    return _check_azure("mistral", api_key, cfg)


def check_claude(api_key, model):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["content"][0]["text"].strip()
    return f"model={model}, replied: {reply!r}"


def check_perplexity(api_key, model):
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 20,
        },
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    return f"model={model}, replied: {reply!r}"


def check_grok(api_key, model):
    resp = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    return f"model={model}, replied: {reply!r}"


def check_languagetool(username, api_key):
    resp = requests.post(
        "https://api.languagetool.org/v2/check",
        data={
            "text": "This are a test.",
            "language": "en-US",
            "username": username,
            "apiKey": api_key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    matches = resp.json().get("matches", [])
    return f"API responded, {len(matches)} match(es) on test sentence"


def check_wordpress(site_url, username, app_password):
    site_url = site_url.rstrip("/")
    api_base = f"{site_url}/wp-json/wp/v2"

    # First check: REST API is reachable at all
    resp = requests.get(f"{site_url}/wp-json/wp/v2", timeout=15)
    if resp.status_code != 200:
        raise Exception(
            f"REST API returned HTTP {resp.status_code}. Check Settings → Permalinks in your WP admin."
        )

    # Second check: credentials are valid
    token = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    resp = requests.get(
        f"{api_base}/users/me",
        headers={"Authorization": f"Basic {token}"},
        timeout=15,
    )
    if resp.status_code == 401:
        raise Exception(
            "Authentication failed. Check your username and application password."
        )
    resp.raise_for_status()
    data = resp.json()
    wp_user = data.get("name") or data.get("slug") or "unknown"
    return f"authenticated as '{wp_user}' at {site_url}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Article Review Pipeline — connectivity check"
    )
    parser.add_argument(
        "--publication", required=True, help="Publication config name (without .yaml)"
    )
    parser.add_argument("--config-dir", default="configs")
    parser.add_argument(
        "--show-keys",
        action="store_true",
        help="Print a masked view of each provider's resolved API key and "
        "where it actually came from (.env file vs. OS environment variable "
        "fallback), then exit without making any network calls. Reflects the "
        "user.yaml/.env/OS-env tiers only — a publication config's api_keys "
        "section or --api-key (both higher-precedence) aren't shown here.",
    )
    parser.add_argument(
        "--api-key",
        metavar="PROVIDER=VALUE",
        action="append",
        help="Override one provider's api_key for this check only — same "
        "precedence and same single-field providers as ci-review's "
        "--api-key. Repeatable.",
    )
    args = parser.parse_args()

    if args.show_keys:
        try:
            print_key_sources(args.config_dir)
        except (FileNotFoundError, ValueError) as e:
            print(f"Config error: {e}")
            sys.exit(1)
        sys.exit(0)

    try:
        api_key_overrides = parse_api_key_overrides(args.api_key)
    except ValueError as e:
        parser.error(str(e))

    try:
        validate_publication_name(args.publication)
        user_config = load_user_config(args.config_dir)
        pub_config_raw = load_publication_config(args.publication, args.config_dir)
        config = merge_configs(user_config, pub_config_raw)
        if api_key_overrides:
            config = apply_api_key_overrides(config, api_key_overrides)
    except (FileNotFoundError, ValueError) as e:
        print(f"Config error: {e}")
        sys.exit(1)

    api_keys = config["api_keys"]
    models = config.get("models", {})  # always dicts after _normalize_model_configs()
    pub = config["publication"]
    wp = pub.get("wordpress", {})
    pipeline_cfg = config.get("pipeline", {})

    print(f"\nConnectivity check — publication: {args.publication}\n")

    results = []

    # --- AI review models (required) ---
    print("Review models (required):")

    # OpenAI — dispatch by provider
    openai_cfg = models.get("openai", {})
    openai_model = openai_cfg.get("model", "gpt-4o")
    openai_provider = openai_cfg.get("provider", "openai")

    if openai_provider == "azure":
        _key = openai_cfg  # capture for lambda closure
        results.append(
            check(
                "OpenAI (Azure)",
                lambda: check_openai_azure(api_keys["openai"]["api_key"], _key),
            )
        )
    else:
        _m = openai_model
        results.append(
            check("OpenAI", lambda: check_openai(api_keys["openai"]["api_key"], _m))
        )

    # Gemini — dispatch by provider
    gemini_cfg = models.get("gemini", {})
    gemini_model = gemini_cfg.get("model", "gemini-2.5-flash")
    gemini_provider = gemini_cfg.get("provider", "ai_studio")

    if gemini_provider == "vertex_ai":
        _cfg = gemini_cfg
        results.append(check("Gemini (Vertex AI)", lambda: check_gemini_vertex(_cfg)))
    else:
        _m = gemini_model
        results.append(
            check("Gemini", lambda: check_gemini(api_keys["gemini"]["api_key"], _m))
        )

    # Mistral — dispatch by provider
    mistral_cfg = models.get("mistral", {})
    mistral_model = mistral_cfg.get("model", "mistral-large-latest")
    mistral_provider = mistral_cfg.get("provider", "mistral")

    if mistral_provider == "azure":
        _key = mistral_cfg
        results.append(
            check(
                "Mistral (Azure)",
                lambda: check_mistral_azure(api_keys["mistral"]["api_key"], _key),
            )
        )
    else:
        _m = mistral_model
        results.append(
            check("Mistral", lambda: check_mistral(api_keys["mistral"]["api_key"], _m))
        )

    # --- Optional models ---
    print("\nReview models (optional):")

    perplexity_creds = api_keys.get("perplexity", {})
    if perplexity_creds.get("api_key"):
        perplexity_cfg = models.get("perplexity", {})
        perplexity_model = perplexity_cfg.get("model", "sonar-pro")
        if perplexity_cfg.get("enabled", True):
            _m = perplexity_model
            results.append(
                check(
                    "Perplexity",
                    lambda: check_perplexity(perplexity_creds["api_key"], _m),
                )
            )
        else:
            print(f"  {SKIP}  Perplexity — disabled (enabled: false in model config)")
    else:
        print(f"  {SKIP}  Perplexity — no api_key configured (optional)")
    grok_creds = api_keys.get("grok", {})
    if grok_creds.get("api_key"):
        grok_cfg = models.get("grok", {})
        grok_model = grok_cfg.get("model", "grok-3-latest")
        if grok_cfg.get("enabled", True):
            _m = grok_model
            results.append(check("Grok", lambda: check_grok(grok_creds["api_key"], _m)))
        else:
            print(f"  {SKIP}  Grok — disabled (enabled: false in model config)")
    else:
        print(f"  {SKIP}  Grok — no api_key configured (optional)")

    claude_creds = api_keys.get("claude", {})
    if claude_creds.get("api_key"):
        claude_cfg = models.get("claude", {})
        claude_model = claude_cfg.get("model", "claude-opus-4-5")
        if claude_cfg.get("enabled", True):
            _m = claude_model
            results.append(
                check("Claude", lambda: check_claude(claude_creds["api_key"], _m))
            )
        else:
            print(f"  {SKIP}  Claude — disabled (enabled: false in model config)")
    else:
        print(f"  {SKIP}  Claude — no api_key configured (optional)")

    # --- LanguageTool (optional) ---
    lt_creds = api_keys.get("languagetool", {})
    grammar_enabled = pipeline_cfg.get("grammar_pass", True)
    if not grammar_enabled:
        print(f"  {SKIP}  LanguageTool — grammar_pass: false")
    elif lt_creds.get("username") and lt_creds.get("api_key"):
        results.append(
            check(
                "LanguageTool",
                lambda: check_languagetool(lt_creds["username"], lt_creds["api_key"]),
            )
        )
    else:
        print(f"  {SKIP}  LanguageTool — no credentials configured (optional)")

    # --- WordPress ---
    print("\nWordPress:")
    if wp.get("site_url") and wp.get("username") and wp.get("application_password"):
        results.append(
            check(
                "WordPress REST API + credentials",
                lambda: check_wordpress(
                    wp["site_url"], wp["username"], wp["application_password"]
                ),
            )
        )
    else:
        print(
            f"  {SKIP}  WordPress — missing site_url, username, or application_password in publication config"
        )

    # --- Summary ---
    failures = results.count(False)
    print(
        f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) failed — fix the errors above before running the pipeline.'}\n"
    )
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
