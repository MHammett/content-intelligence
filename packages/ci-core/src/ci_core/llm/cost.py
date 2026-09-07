"""Cost estimation from API token counts.

Prices are per-million tokens (input, output).
Perplexity pricing is per-request-based in practice but approximated here
using token counts so the table stays consistent.

Pricing is loaded from configs/pricing.yaml at import time so model additions
and price changes require only a YAML edit, not a code change.  The hardcoded
The YAML is the single source of truth; there is no duplicate table in Python.
"""

import logging
from pathlib import Path

import yaml as _yaml  # noqa: F401

from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml

log = logging.getLogger(__name__)

# Hardcoded fallback — used only when configs/pricing.yaml is missing or unreadable.


def _load_pricing():
    """Load pricing from the packaged configs/pricing.yaml.

    Raises PackagedConfigError if the file is missing, unparseable, or does not
    contain a usable pricing table. There is deliberately no hardcoded fallback:
    the duplicate table this replaced had to be edited in lockstep with the YAML
    on the config surface that changes most often, and in the only state it
    could ever have fired — a broken install — silently serving stale prices is
    worse than saying so.
    """
    yaml_path = Path(__file__).parent.parent / "configs" / "pricing.yaml"
    data = load_packaged_yaml(yaml_path)

    pricing = {}
    for model_id, pair in (data.get("models") or {}).items():
        # [input, output] or [input, output, cached_input]. The third element is
        # optional: a model without it bills cached tokens at the full input
        # rate, which over-reports rather than inventing a discount.
        if not (isinstance(pair, (list, tuple)) and len(pair) in (2, 3)):
            raise PackagedConfigError(
                f"{yaml_path}: models.{model_id} must be [prompt, completion] or "
                f"[prompt, completion, cached], got {pair!r}"
            )
        pricing[str(model_id)] = tuple(float(x) for x in pair)
    if not pricing:
        raise PackagedConfigError(f"{yaml_path}: 'models' is empty or missing")

    unknown_raw = data.get("unknown_price")
    if not (isinstance(unknown_raw, (list, tuple)) and len(unknown_raw) == 2):
        raise PackagedConfigError(
            f"{yaml_path}: 'unknown_price' must be a [prompt, completion] pair"
        )
    return pricing, (float(unknown_raw[0]), float(unknown_raw[1]))


_PRICING, _UNKNOWN_PRICE = _load_pricing()


def known_price(model_id):
    """Return this model's pricing row, or ``None`` if the table has no entry.

    ``_price_for_model`` cannot answer this: it substitutes ``unknown_price``
    for anything unrecognized, so a caller gets numbers either way and has no
    way to tell a real rate from the conservative placeholder. That distinction
    matters as soon as something reports on a model the pipeline has not run —
    a newly released model is exactly the case pricing.yaml is most likely not
    to know yet, and quoting it the fallback rate would present a guess as a
    price.

    Public because it crosses a package boundary (see docs/NAMING.md).
    """
    if not model_id:
        return None
    if model_id in _PRICING:
        return _PRICING[model_id]
    for key in sorted(_PRICING, key=len, reverse=True):
        if model_id.startswith(key):
            return _PRICING[key]
    return None


def _price_for_model(model_id):
    price = known_price(model_id)
    return _UNKNOWN_PRICE if price is None else price


def call_log_entry(pass_name, result, default_model=""):
    """Build one ``api_call_log`` entry from a provider result.

    Three callers — citation verification and the two SEO passes — each wrote
    this dict out by hand, byte-identical apart from the pass name. When
    ``discarded_attempts`` was added so retried attempts could be billed, all
    three silently kept dropping it, because there was nowhere for the new field
    to be added once. This is that place.

    Deliberately tolerant of a partial result: a provider adapter that failed
    early may carry nothing but ``failed`` and an error string.
    """
    entry = {
        "pass": pass_name,
        "model": result.get("model", default_model),
        "failed": bool(result.get("failed")),
        "tokens": result.get("tokens", {}),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "error": result.get("error") if result.get("failed") else None,
    }
    # Attempts the provider billed for and this call then threw away. Absent on
    # the overwhelming majority of calls, so only present when it happened.
    if result.get("discarded_attempts"):
        entry["discarded_attempts"] = result["discarded_attempts"]
    return entry


def _entry_cost(entry):
    """Return (input_cost_usd, output_cost_usd) for one api_call_log entry.

    Cost follows the tokens the provider reported, not whether the pipeline
    could use the answer. A ``failed`` short-circuit used to return zero here,
    which is right for a call that never produced anything — a transport error
    or a timeout reports ``{"prompt": 0, "completion": 0}`` and costs nothing on
    its own. It was wrong for the one failure that does produce output: a
    response whose JSON will not parse is complete, generated and billed, and
    the client returns it with real token counts and ``failed: True``. Those
    were priced at $0.00.

    Leaning on the token counts covers both cases without a flag: nothing
    generated means nothing to bill.
    """
    tokens = entry.get("tokens") or {}
    prompt_tok = tokens.get("prompt", 0) or 0
    completion_tok = tokens.get("completion", 0) or 0
    # Cached input is a subset of prompt tokens, billed at a lower rate. It is
    # reported by every provider that caches and was priced at the full input
    # rate until now, so any caching the pipeline benefits from was invisible in
    # the cost summary — which is also what made the caching work unmeasurable.
    cached_tok = min(tokens.get("cached", 0) or 0, prompt_tok)
    uncached_tok = prompt_tok - cached_tok
    model_id = entry.get("model", "")
    # Strip fallback annotations like " [FALLBACK from gpt-5.4]"
    model_id = model_id.split(" ")[0] if model_id else ""
    prices = _price_for_model(model_id)
    in_price, out_price = prices[0], prices[1]
    # No cached rate configured for this model → bill cached input as full input.
    # Conservative: over-reports rather than inventing a discount.
    cached_price = prices[2] if len(prices) > 2 else in_price
    input_usd = (uncached_tok * in_price + cached_tok * cached_price) / 1_000_000
    return (input_usd, completion_tok / 1_000_000 * out_price)


def calculate(api_call_log):
    """Return a cost summary dict from the api_call_log list.

    Keys in the returned dict:
      total_usd         float  — grand total across all calls
      total_input_usd   float
      total_output_usd  float
      by_pass           list   — [{pass, model, input_usd, output_usd, total_usd}]
      pricing_known     bool   — False when any model fell back to unknown pricing
      discarded_calls   int    — retried attempts whose output was thrown away
      uncosted_calls    int    — of those, how many carried no usage to price

    Discarded attempts are real spend. A retry replaces the failed attempt's
    result with the next one's, and the provider still billed for what it had
    already generated. Where the attempt carried usage — a malformed-JSON
    response is complete, merely unparseable — it is priced here against the same
    model. Where it did not, a stalled stream having no usage to read, it is
    counted in ``uncosted_calls`` so the caller can say the total is a floor
    rather than an exact figure. Seven attempts were discarded unrecorded on
    2026-09-03 under a summary line reading "(exact)".
    """
    by_pass = []
    total_in = 0.0
    total_out = 0.0
    pricing_known = True
    discarded_calls = 0
    uncosted_calls = 0
    replayed_usd = 0.0
    incurred_usd = 0.0

    for entry in api_call_log or []:
        in_usd, out_usd = _entry_cost(entry)
        model_id = (entry.get("model") or "").split(" ")[0]
        if model_id and model_id not in _PRICING:
            # Check prefix match
            if not any(model_id.startswith(k) for k in _PRICING):
                pricing_known = False

        discarded = entry.get("discarded_attempts") or {}
        if discarded:
            discarded_calls += discarded.get("count", 0)
            uncosted_calls += discarded.get("count", 0) - discarded.get("costed", 0)
            d_in, d_out = _entry_cost(
                {
                    "model": entry.get("model", ""),
                    "tokens": discarded.get("tokens", {}),
                }
            )
            in_usd += d_in
            out_usd += d_out

        by_pass.append(
            {
                "pass": entry.get("pass", ""),
                "model": entry.get("model", ""),
                "input_usd": round(in_usd, 6),
                "output_usd": round(out_usd, 6),
                "total_usd": round(in_usd + out_usd, 6),
            }
        )
        total_in += in_usd
        total_out += out_usd
        if entry.get("replayed"):
            replayed_usd += in_usd + out_usd
        else:
            incurred_usd += in_usd + out_usd

    return {
        "total_usd": round(total_in + total_out, 4),
        "total_input_usd": round(total_in, 4),
        "total_output_usd": round(total_out, 4),
        "by_pass": by_pass,
        "pricing_known": pricing_known,
        "discarded_calls": discarded_calls,
        "uncosted_calls": uncosted_calls,
        # A replay re-reports the captured run's token counts. Splitting them
        # from what this process actually bought is what lets the summary say
        # "$0.0000" only when that is true.
        "replayed_usd": round(replayed_usd, 4),
        "incurred_usd": round(incurred_usd, 4),
    }
