"""ci_core.llm — the shared LLM call layer.

This is the single path by which every CI tool talks to a model provider. It is
synchronous, streaming, and built on litellm:

  * :mod:`ci_core.llm.client` — the litellm shim. Five providers through
    ``litellm.completion()``, OpenAI through ``litellm.responses()``, all of
    them streaming under an inter-chunk read-gap timeout.
  * :mod:`ci_core.llm.json_utils` — JSON extraction from model output,
    including salvage of arrays truncated at the output-token ceiling. litellm
    has no equivalent; a truncated response is our problem either way.
  * :mod:`ci_core.llm.cost` — token/cost calculation from ``configs/pricing.yaml``.
    Deliberately not litellm's cost table — see that module for the measured
    disagreements.
  * :mod:`ci_core.llm.timeout_model` — the sliding-scale wall-clock backstop
    (size x model x effort) from ``configs/timeouts.yaml``.
  * :mod:`ci_core.llm.model_registry` — model deprecation tracking from
    ``configs/model_registry.yaml``. litellm knows what a model costs, not
    whether it has been superseded.

Callers that just want the assembled text back — rather than the JSON-parsing
verdict — should use :func:`call_text`.
"""

import logging

from . import (
    cache,
    client,
    cost,
    json_utils,
    model_registry,
    schema,
    timeout_model,
    watermarking,
)
from .client import PROVIDERS

log = logging.getLogger(__name__)

__all__ = [
    "PROVIDERS",
    "call_provider",
    "call_text",
    "cache",
    "client",
    "cost",
    "json_utils",
    "model_registry",
    "watermarking",
    "schema",
    "timeout_model",
]


def call_provider(
    name,
    system_prompt,
    user_prompt,
    api_key,
    retry=True,
    retry_delay=10,
    model=None,
    provider_config=None,
    response_schema=None,
    cache_prefix=None,
):
    """Call provider ``name`` and return its result dict unchanged."""
    return client.call(
        name,
        system_prompt,
        user_prompt,
        api_key,
        retry=retry,
        retry_delay=retry_delay,
        model=model,
        provider_config=provider_config,
        response_schema=response_schema,
        cache_prefix=cache_prefix,
    )


def call_text(
    name,
    system_prompt,
    user_prompt,
    api_key,
    retry=True,
    retry_delay=10,
    model=None,
    provider_config=None,
    response_schema=None,
    cache_prefix=None,
):
    """Call provider ``name`` and return the assembled text, not parsed JSON.

    The shim parses its response as JSON and reports a non-JSON body as a
    failure, because the review pipeline requires structured findings. Callers
    that do their own parsing downstream (ci-style-profile runs
    :func:`ci_core.llm.json_utils.extract_json` over several passes, and feeds
    some model output back into a later prompt verbatim) need the text itself,
    and a prose response is usable to them rather than fatal.

    So a ``"Malformed JSON response"`` failure carrying ``raw`` text is
    reported here as a success with that text. Every other failure — HTTP
    error, timeout, empty response — stays a failure.

    Returns ``{"content": str, "failed": bool, "tokens": dict, "elapsed":
    float, "model": str}``, plus ``error`` when failed.
    """
    result = call_provider(
        name,
        system_prompt,
        user_prompt,
        api_key,
        retry=retry,
        retry_delay=retry_delay,
        model=model,
        provider_config=provider_config,
        response_schema=response_schema,
        cache_prefix=cache_prefix,
    )

    failed = bool(result.get("failed"))
    raw = result.get("raw") or ""

    if (
        failed
        and raw
        and str(result.get("error", "")).startswith("Malformed JSON response")
    ):
        # Not a transport failure — the model answered, just not in JSON.
        # The caller parses this itself.
        log.debug(
            "%s returned non-JSON content; passing %d chars through as text",
            name,
            len(raw),
        )
        failed = False

    out = {
        "content": raw,
        "failed": failed,
        "tokens": result.get("tokens") or {},
        "elapsed": result.get("elapsed_seconds", 0.0),
        # `or` rather than a dict default: a result carrying model=None (a
        # failure before a model was resolved) must still name something the
        # cost log can key on, or the call bills at unknown_price.
        "model": result.get("model") or model or name,
    }
    if failed:
        out["error"] = result.get("error", "")
        if result.get("error_body"):
            out["error_body"] = result["error_body"]
    return out
