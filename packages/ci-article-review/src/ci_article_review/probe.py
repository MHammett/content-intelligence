#!/usr/bin/env python3
"""
Probe — checks that the models a cost preset names will actually answer, before
a full article run is paid for.

Usage:
  ci-probe                          # every preset, every provider
  ci-probe --preset maximum         # one preset
  ci-probe --preset standard openai claude
  ci-probe --list-models            # also ask each provider what it offers

What it sends and why it matters
--------------------------------
Every call goes through ``ci_core.llm.client.call`` — the same entry point the
pipeline uses — against the config ``merge_configs`` resolves for that preset.
So a probe exercises the real model ID, the real reasoning effort, the real
temperature decision, the real schema support and the real streaming path.

That is the whole point, and it was not true before. This module used to hand-
roll its own ``requests.post`` per provider with a payload of its own design.
On 2026-09-04 it reported ``gpt-5.6-terra OK`` while a live standard run failed
every openai call with HTTP 400 — the pipeline sent ``temperature`` and the
model refused it, but the probe had never sent one. A pre-flight check that
does not send what the flight sends can only tell you the model exists.

It also read credentials straight from ``os.getenv``, bypassing the project's
precedence (CLI override > publication config > .env > OS environment). On a
machine where a provider key is set in both places with different values — and
this repo already prints a ``WP_USER`` notice for exactly that — the probe would
test one credential and the run would use another. Keys now come from
``load_user_config``, which resolves ``${VAR}`` the documented way.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

# Before anything prints: this report carries ANSI colour and model names a
# default Windows console (cp1252) cannot encode. Required of every console
# script, and enforced by test_every_cli_entry_point_forces_utf8_stdio.
from ci_core.console import force_utf8_stdio

force_utf8_stdio()

from ci_core.llm import client  # noqa: E402

from .config_loader import (  # noqa: E402
    load_publication_config,
    load_user_config,
    merge_configs,
)

OK = "\033[32m OK  \033[0m"
FAIL = "\033[31m FAIL\033[0m"
WARN = "\033[33m WARN\033[0m"
SKIP = "\033[33m SKIP\033[0m"

#: A probe has to ask for JSON, because the client parses every response as JSON
#: and raises on anything else. Asking for a schema too is deliberate: schema
#: support is the one capability that genuinely differs per provider (see
#: ci_core.llm.schema), so a probe that skipped it would leave the most
#: provider-specific behaviour unchecked.
#: Ordinary phrasing, deliberately. A terser instruction —
#: 'Return exactly this object and nothing more: {"ok": true}' — tripped xAI's
#: content filter outright (``permission-denied``, ``SAFETY_CHECK_TYPE_BIO``)
#: and reported grok as unreachable when it was fine. A probe that fails for
#: reasons of its own is the failure mode this whole module was rewritten to
#: remove.
PROBE_SYSTEM = "You are a helpful assistant. Reply with JSON and no other text."
PROBE_PROMPT = (
    "Please reply with a JSON object that has a single field named ok, set to true."
)
#: The client takes ``{"name": ..., "schema": ...}``, matching
#: ``schemas.for_domain``, not a bare JSON Schema.
PROBE_SCHEMA = {
    "name": "probe",
    "schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
}


def _result(ok, label, detail=""):
    tag = OK if ok else FAIL
    line = f"  {tag}  {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _warn(label, detail=""):
    line = f"  {WARN}  {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def preset_names():
    """Every preset defined in the packaged presets.yaml."""
    path = Path(__file__).parent / "configs" / "presets.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [name for name, cfg in data.items() if isinstance(cfg, dict)]


def resolve_preset(preset, publication, config_dir):
    """``(models, api_keys)`` exactly as a run with this preset would resolve them.

    Goes through the same loader the pipeline does, so ``${VAR}`` follows the
    project's precedence rather than a second, private reading of the
    environment.
    """
    user = load_user_config(config_dir)
    user.setdefault("pipeline", {})["cost_preset"] = preset
    pub = load_publication_config(publication, config_dir)
    config = merge_configs(user, pub)
    return config.get("models") or {}, config.get("api_keys") or {}


def _api_key_for(provider, api_keys):
    """The key the pipeline would use, or "" when the provider needs none.

    Gemini via Vertex authenticates with a service-account file named in the
    model config, so an absent key is expected rather than a failure.
    """
    return ((api_keys.get(provider) or {}).get("api_key")) or ""


def probe_provider(provider, model_cfg, api_key):
    """One real call, through the client the pipeline uses."""
    model_cfg = dict(model_cfg or {})

    # Live search off, always. The pipeline resolves ``web_search`` to a bool
    # per domain before calling — only fact_check has any use for it, and it
    # bills per search. Passing the raw config through left it as the list
    # ``["fact_check"]``, which is truthy, so every openai probe ran a real
    # search: 4,487 prompt tokens for a one-line question, on a tool whose
    # whole selling point is being cheap enough to run before every job.
    model_cfg["web_search"] = False

    model = model_cfg.get("model") or provider
    effort = model_cfg.get("reasoning_effort")
    detail_bits = [f"effort={effort}" if effort else "effort=none"]

    label = f"{provider}: {model}"
    try:
        result = client.call(
            provider,
            PROBE_SYSTEM,
            PROBE_PROMPT,
            api_key,
            # A probe wants the first answer, not a masked one: retrying here
            # would hide exactly the transient a run is about to hit.
            retry=False,
            model=model,
            provider_config=model_cfg,
            response_schema=PROBE_SCHEMA,
        )
    except Exception as e:  # pragma: no cover - network shapes vary
        return _result(False, label, f"{type(e).__name__}: {e}")

    elapsed = result.get("elapsed_seconds")
    if result.get("failed"):
        body = (result.get("error_body") or "").strip().replace("\n", " ")[:200]
        detail = f"{result.get('error')}"
        if body:
            detail += f" | {body}"
        return _result(False, label, detail)

    data = result.get("data")
    reported = result.get("model", "?")
    tokens = result.get("tokens") or {}
    detail = (
        f"answered={json.dumps(data)} model_in_response={reported!r} "
        f"{', '.join(detail_bits)} "
        f"{tokens.get('prompt', 0)}+{tokens.get('completion', 0)} tok {elapsed}s"
    )
    ok = _result(True, label, detail)
    if data != {"ok": True}:
        _warn(
            label,
            "the schema was accepted but the content is not what was asked for",
        )
    return ok


def probe_preset(preset, providers, publication, config_dir):
    """Probe every requested provider as ``preset`` resolves them."""
    print(f"\n== {preset} " + "=" * (62 - len(preset)))
    try:
        models, api_keys = resolve_preset(preset, publication, config_dir)
    except (FileNotFoundError, ValueError) as e:
        print(f"  {FAIL}  could not resolve this preset: {e}")
        return False

    all_ok = True
    for provider in providers:
        cfg = models.get(provider)
        if not cfg or cfg.get("enabled") is False:
            print(f"  {SKIP}  {provider}: not enabled in this preset")
            continue
        key = _api_key_for(provider, api_keys)
        if not key and provider != "gemini":
            print(f"  {SKIP}  {provider}: no API key resolved — skipping")
            continue
        all_ok = probe_provider(provider, cfg, key) and all_ok
    return all_ok


#: Providers a preset can name. Read from the packaged presets rather than
#: hardcoded, so a provider added there is probed without editing this file.
def known_providers():
    path = Path(__file__).parent / "configs" / "presets.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    found = []
    for cfg in data.values():
        if not isinstance(cfg, dict):
            continue
        for provider in cfg.get("models") or {}:
            if provider not in found:
                found.append(provider)
    return found


def main():
    providers = known_providers()
    presets = preset_names()

    parser = argparse.ArgumentParser(
        description=(
            "Check that the models a cost preset names will answer, using the "
            "same client and config resolution a real run uses."
        )
    )
    # No `choices=` on the positional: with nargs="*" argparse validates a list
    # default as a single value, so `ci-probe` with no arguments died on
    # `invalid choice: "['all']"` and the documented default invocation had
    # never worked. Validated below instead.
    parser.add_argument(
        "providers",
        nargs="*",
        help=f"Providers to probe: {', '.join(providers)}, or all (default: all)",
    )
    parser.add_argument(
        "--preset",
        action="append",
        help=f"Preset to probe (repeatable). One of: {', '.join(presets)}. "
        f"Default: every preset.",
    )
    parser.add_argument(
        "--publication",
        default="mikehammett",
        help="Publication config to resolve against (default: mikehammett)",
    )
    parser.add_argument(
        "--config-dir", default="configs", help="Directory containing config files"
    )
    args = parser.parse_args()

    requested = args.providers or ["all"]
    unknown = [p for p in requested if p != "all" and p not in providers]
    if unknown:
        parser.error(
            f"unknown provider(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(providers)}, all"
        )
    chosen_providers = providers if "all" in requested else requested

    chosen_presets = args.preset or presets
    unknown_presets = [p for p in chosen_presets if p not in presets]
    if unknown_presets:
        parser.error(
            f"unknown preset(s): {', '.join(unknown_presets)}. "
            f"Choose from: {', '.join(presets)}"
        )

    print("Probing preset models through the pipeline's own client.")
    print("Each call sends one tiny schema-constrained prompt.")

    all_ok = True
    for preset in chosen_presets:
        all_ok = (
            probe_preset(preset, chosen_providers, args.publication, args.config_dir)
            and all_ok
        )

    print()
    if all_ok:
        print("All probed models answered.")
    else:
        print("Some models did not answer — see FAIL rows above.")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
