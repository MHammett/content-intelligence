"""Cost estimation from API token counts and search counts.

Token prices are per-million tokens (input, output). Search and grounding fees
are per 1,000 billed units and come on top of them: Anthropic and OpenAI charge
per search, Gemini per query or per grounded prompt, and Perplexity a fee on
every request. Each call's ``searches`` count is read off the provider's
response by ci_core.llm.client.

Pricing is loaded from configs/pricing.yaml at import time so model additions
and price changes require only a YAML edit, not a code change. The YAML is the
single source of truth; there is no duplicate table in Python.
"""

import logging
from pathlib import Path

import yaml as _yaml  # noqa: F401

from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml

log = logging.getLogger(__name__)

# Hardcoded fallback — used only when configs/pricing.yaml is missing or unreadable.


def _load_pricing():
    """Load token prices and search fees from the packaged configs/pricing.yaml.

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

    search_fees = {}
    for key, row in (data.get("search_fees") or {}).items():
        if not (
            isinstance(row, (list, tuple)) and len(row) == 2 and row[1] in _SEARCH_UNITS
        ):
            raise PackagedConfigError(
                f"{yaml_path}: search_fees.{key} must be [usd_per_1000, unit] "
                f"with unit one of {sorted(_SEARCH_UNITS)}, got {row!r}"
            )
        search_fees[str(key)] = (float(row[0]), str(row[1]))
    return pricing, (float(unknown_raw[0]), float(unknown_raw[1])), search_fees


# What one billed unit of a search fee is. Every unit but "prompt" bills the
# client's `searches` count as it stands; "prompt" bills a call once if it
# searched at all (Gemini 2.5 charges one grounded prompt however many queries
# it ran). See the search_fees block in pricing.yaml for each provider's source.
_SEARCH_UNITS = frozenset({"search", "call", "query", "prompt", "request"})

_PRICING, _UNKNOWN_PRICE, _SEARCH_FEES = _load_pricing()


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
    return _lookup(_PRICING, model_id)


def known_search_fee(model_id):
    """``(usd_per_1000, unit)`` for this model's search fee, or ``None``.

    ``None`` means the table has no rate, not that searching is free: a call
    that reports searches against a model with no row is counted in
    ``unpriced_searches`` rather than billed at $0.00.
    """
    return _lookup(_SEARCH_FEES, model_id)


def _lookup(table, model_id):
    """``table``'s row for ``model_id``: an exact key, else the longest prefix."""
    if not model_id:
        return None
    if model_id in table:
        return table[model_id]
    for key in sorted(table, key=len, reverse=True):
        if model_id.startswith(key):
            return table[key]
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
    # How many billable searches the call reported, next to its tokens. The key
    # is present only on a call that could search, and None there means the
    # provider did not say, which calculate() counts rather than prices.
    # Copied by presence, not truthiness, so neither 0 nor None is lost.
    if "searches" in result:
        entry["searches"] = result["searches"]
    # What the provider itself billed, where it says (Perplexity's Agent API).
    # Recorded, not used: calculate() prices from the table, and the two agreeing
    # is how the table is checked.
    if result.get("provider_cost_usd") is not None:
        entry["provider_cost_usd"] = result["provider_cost_usd"]
    # Attempts the provider billed for and this call then threw away. Absent on
    # the overwhelming majority of calls, so only present when it happened.
    if result.get("discarded_attempts"):
        entry["discarded_attempts"] = result["discarded_attempts"]
    # Follow-up calls made for a response cut off at its output ceiling, each
    # billed on its own (see calculate()). Only present when one ran.
    if result.get("continuations"):
        entry["continuations"] = result["continuations"]
    # One record per stream the call opened: how long it went silent before
    # and after real output. See ci_core.llm.client._StreamTiming.
    if result.get("stream_timing"):
        entry["stream_timing"] = result["stream_timing"]
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
    #
    # Known gap, deliberately not modelled: cache *writes* bill at 1.25x input
    # (2x on a 1h TTL, which this pipeline does not use). tokens.py folds
    # ``cache_creation_input_tokens`` into ``prompt`` and only breaks out
    # reads, so written tokens land in ``uncached_tok`` and are billed at 1.00x
    # — an under-report of 0.25x on the written span only. Measured on the
    # 2026-09-10 verification run that span was worth ~$0.01 against ~$0.39
    # saved on the same call, so a fourth price slot is not worth the config
    # surface. Revisit if a run ever writes far more than it reads.
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


def _search_cost(model_id, searches):
    """Return ``(usd, billed_units, unpriced)`` for one call's search count.

    ``searches`` is what the client read off the response, never None here:
    the caller counts an unreported count before it gets this far. A model
    with no row in pricing.yaml's ``search_fees`` bills nothing and returns the
    count as ``unpriced``, so the summary can say the total is short rather than
    quietly pricing the searches at zero.
    """
    searches = int(searches or 0)
    if searches <= 0:
        return 0.0, 0, 0
    fee = known_search_fee(model_id)
    if fee is None:
        return 0.0, 0, searches
    usd_per_1000, unit = fee
    units = 1 if unit == "prompt" else searches
    return units * usd_per_1000 / 1000, units, 0


# The exception classes a discarded attempt's ``reasons`` can name when the
# provider refused the request before generating anything: litellm's 429 and
# 5xx. Read only for records written before an attempt carried its own
# ``unbilled`` flag, which is what decides it now (ci_core.llm.client,
# ``_refused_before_generation``). A mid-stream failure is a
# MidStreamFallbackError, not one of these, and a stall has its own class.
_LEGACY_REFUSAL_REASONS = frozenset(
    {
        "RateLimitError",
        "ServiceUnavailableError",
        "BadGatewayError",
        "InternalServerError",
    }
)


def _unbilled_attempts(discarded, stream_timing=None):
    """How many of a record's attempts were refused before generating anything.

    A record says so itself (``unbilled``). One written before that, such as the
    two 2026-10-05 captures behind #331, is read from what it kept: all its
    reasons being refusal classes, or else the call's per-attempt stream records,
    which name the class each stream was cut short by even where the record's
    own reason is "unknown". Never more than the attempts with no usage.
    """
    unpriced = max(discarded.get("count", 0) - discarded.get("costed", 0), 0)
    if "unbilled" in discarded:
        return min(discarded["unbilled"] or 0, unpriced)
    reasons = discarded.get("reasons") or []
    if reasons and all(r in _LEGACY_REFUSAL_REASONS for r in reasons):
        return unpriced
    refused = sum(
        1
        for s in stream_timing or ()
        if isinstance(s, dict) and s.get("cut_short_by") in _LEGACY_REFUSAL_REASONS
    )
    return min(refused, unpriced)


def calculate(api_call_log):
    """Return a cost summary dict from the api_call_log list.

    Keys in the returned dict:
      total_usd         float  — grand total across all calls: input + output
                                 + search
      total_input_usd   float
      total_output_usd  float
      total_search_usd  float  — search and grounding fees, billed per search
                                 on top of tokens (pricing.yaml ``search_fees``)
      by_pass           list   — [{pass, model, input_usd, output_usd, total_usd}],
                                 each also ``replayed: True`` if its entry is,
                                 ``search_usd`` / ``search_units`` if the call
                                 could search, and ``unmeasured_search_calls``
                                 if any of its attempts' counts is unknown
      pricing_known     bool   — False when any model fell back to unknown pricing
      discarded_calls   int    — retried attempts whose output was thrown away
      uncosted_calls    int    — of those, how many may have been billed and
                                 carried no usage to price. Attempts the
                                 provider refused before generating (a 429)
                                 are not among them: nothing was billed
      refused_calls     int    — of the discarded ones, how many the provider
                                 refused before generating: not spend, so not
                                 uncosted either
      search_units      int    — billed search units priced into total_search_usd
      unmeasured_search_calls int — attempts that could search whose response
                                 did not say how many times, retried ones
                                 included: their fee is missing
      unpriced_searches int    — searches reported against a model with no
                                 search_fees row: also missing
      replayed_usd      float  — of total_usd, what ``replayed`` entries cost:
                                 a capture's spend, re-reported, not re-spent
      incurred_usd      float  — the rest: what this run actually bought
      continuation_calls int   — follow-up calls made for passes cut off at
                                 their output ceiling (an entry's
                                 ``continuations``)
      continuation_usd  float  — of total_usd, what those cost
      uncosted_continuation_calls int — of uncosted_calls, the continuation
                                 calls that carried no usage

    The attempt and search counts describe total_usd, so a replayed entry's are
    counted too — they are what makes that history a floor.

    A continuation is a call of its own, beside the entry it continued: its
    tokens are priced at the model that answered it (the entry's, unless a
    fallback did), its searches billed per call, and the attempts its own retry
    discarded counted with everyone else's. One that carried no usage is in
    ``uncosted_calls``, as a discarded attempt would be, unless the provider
    refused it before generating. It is history when its entry is ``replayed``,
    unless it says ``replayed: False`` itself: ``--retry-failed`` continues a
    pass from its capture, so the first call is the capture's spend and the
    follow-up this run's.

    Search fees follow the same rule as discarded attempts: price what the
    response reported, and count what it did not. A call carries ``searches``
    only if it could search at all; ``None`` there means the provider did not
    say, and that call lands in ``unmeasured_search_calls`` so the caller can
    call the total a floor. Nothing is estimated.

    Discarded attempts are real spend. A retry replaces the failed attempt's
    result with the next one's, and the provider still billed for what it had
    already generated. Where the attempt carried usage — a malformed-JSON
    response is complete, merely unparseable — it is priced here against the same
    model. Where it did not, a stalled stream having no usage to read, it is
    counted in ``uncosted_calls`` so the caller can say the total is a floor
    rather than an exact figure. An attempt refused at the door, an HTTP 429 or
    5xx before any output, also reports no usage, but generated nothing to bill
    and is left out of that count (issue #331). Seven attempts were discarded
    unrecorded on 2026-09-03 under a summary line reading "(exact)".
    """
    by_pass = []
    total_in = 0.0
    total_out = 0.0
    total_search = 0.0
    pricing_known = True
    discarded_calls = 0
    uncosted_calls = 0
    refused_calls = 0
    search_units = 0
    unmeasured_search_calls = 0
    unpriced_searches = 0
    replayed_usd = 0.0
    incurred_usd = 0.0
    continuation_calls = 0
    continuation_usd = 0.0
    uncosted_continuation_calls = 0

    for entry in api_call_log or []:
        in_usd, out_usd = _entry_cost(entry)
        model_id = (entry.get("model") or "").split(" ")[0]
        if not _is_priced(model_id):
            pricing_known = False

        # One count per attempt that reached the provider and could search:
        # the call's own, then any its retries threw away. Priced one by one,
        # because a per-prompt fee bills each attempt once, not their sum once.
        counts = [entry["searches"]] if "searches" in entry else []
        discarded = entry.get("discarded_attempts") or {}
        if discarded:
            d = _discarded_cost(
                entry.get("model", ""), discarded, entry.get("stream_timing")
            )
            discarded_calls += d["count"]
            refused_calls += d["refused"]
            uncosted_calls += d["uncosted"]
            in_usd += d["input_usd"]
            out_usd += d["output_usd"]
            counts += d["searches"]

        search_usd, units, unmeasured, unpriced = _searches_cost(model_id, counts)
        unpriced_searches += unpriced
        own_usd = in_usd + out_usd + search_usd

        # Follow-up calls for a response cut off at its ceiling: each a call of
        # its own, priced at the model that answered it.
        cont_spent = 0.0
        cont_history = 0.0
        cont_searched = False
        records = [c for c in entry.get("continuations") or () if isinstance(c, dict)]
        for record in records:
            continuation_calls += 1
            c_model = record.get("model") or entry.get("model", "")
            c_model_id = c_model.split(" ")[0]
            if not _is_priced(c_model_id):
                pricing_known = False
            tokens = record.get("tokens") or {}
            c_in, c_out = _entry_cost({"model": c_model, "tokens": tokens})
            if (
                not (tokens.get("prompt") or tokens.get("completion"))
                and record.get("unbilled") is not True
            ):
                # Nothing to price, and nothing says it was not billed: the
                # same floor a stalled, discarded attempt marks.
                uncosted_calls += 1
                uncosted_continuation_calls += 1
            c_counts = [record["searches"]] if "searches" in record else []
            c_discarded = record.get("discarded_attempts") or {}
            if c_discarded:
                d = _discarded_cost(c_model, c_discarded)
                discarded_calls += d["count"]
                refused_calls += d["refused"]
                uncosted_calls += d["uncosted"]
                c_in += d["input_usd"]
                c_out += d["output_usd"]
                c_counts += d["searches"]
            c_search, c_units, c_unmeasured, c_unpriced = _searches_cost(
                c_model_id, c_counts
            )
            cont_searched = cont_searched or bool(c_counts)
            units += c_units
            unmeasured += c_unmeasured
            unpriced_searches += c_unpriced
            in_usd += c_in
            out_usd += c_out
            search_usd += c_search
            c_total = c_in + c_out + c_search
            continuation_usd += c_total
            if record.get("replayed", entry.get("replayed")):
                cont_history += c_total
            else:
                cont_spent += c_total
        unmeasured_search_calls += unmeasured

        row = {
            "pass": entry.get("pass", ""),
            "model": entry.get("model", ""),
            "input_usd": round(in_usd, 6),
            "output_usd": round(out_usd, 6),
            "total_usd": round(in_usd + out_usd + search_usd, 6),
        }
        if counts or cont_searched:
            # Only on a call that could search, so a zero here means it could
            # and did not, not that nobody looked.
            row["search_usd"] = round(search_usd, 6)
            row["search_units"] = units
        if unmeasured:
            # Without this, a pass whose count never came back reads the same
            # as one that searched nothing: search_usd 0.0 either way.
            row["unmeasured_search_calls"] = unmeasured
        if records:
            row["continuation_usd"] = round(cont_spent + cont_history, 6)
        by_pass.append(row)
        total_in += in_usd
        total_out += out_usd
        total_search += search_usd
        search_units += units
        if entry.get("replayed"):
            replayed_usd += own_usd
            if cont_spent:
                # History with this run's follow-up on top (--retry-failed).
                row["incurred_usd"] = round(cont_spent, 6)
            else:
                # So a reader of by_pass can tell history from spend pass by
                # pass, not only in total. Present only when true, like the
                # entry's own.
                row["replayed"] = True
        else:
            incurred_usd += own_usd
        replayed_usd += cont_history
        incurred_usd += cont_spent

    return {
        "total_usd": round(total_in + total_out + total_search, 4),
        "total_input_usd": round(total_in, 4),
        "total_output_usd": round(total_out, 4),
        "total_search_usd": round(total_search, 4),
        "by_pass": by_pass,
        "pricing_known": pricing_known,
        "discarded_calls": discarded_calls,
        "uncosted_calls": uncosted_calls,
        "refused_calls": refused_calls,
        "search_units": search_units,
        "unmeasured_search_calls": unmeasured_search_calls,
        "unpriced_searches": unpriced_searches,
        # A replay re-reports the captured run's token counts. Splitting them
        # from what this process actually bought is what lets the summary say
        # "$0.0000" only when that is true.
        "replayed_usd": round(replayed_usd, 4),
        "incurred_usd": round(incurred_usd, 4),
        "continuation_calls": continuation_calls,
        "continuation_usd": round(continuation_usd, 4),
        "uncosted_continuation_calls": uncosted_continuation_calls,
    }


def _is_priced(model_id):
    """False for a model id the token table has no row for, exact or prefix."""
    if not model_id or model_id in _PRICING:
        return True
    return any(model_id.startswith(k) for k in _PRICING)


def _discarded_cost(model, discarded, stream_timing=None):
    """What one ``discarded_attempts`` summary cost, priced at ``model``.

    ``count`` attempts, of which ``refused`` the provider turned away before
    generating and ``uncosted`` may have been billed with no usage to price.
    ``searches`` is one count per attempt that reported one.
    """
    refused = _unbilled_attempts(discarded, stream_timing)
    d_in, d_out = _entry_cost({"model": model, "tokens": discarded.get("tokens", {})})
    return {
        "count": discarded.get("count", 0),
        "refused": refused,
        "uncosted": discarded.get("count", 0) - discarded.get("costed", 0) - refused,
        "input_usd": d_in,
        "output_usd": d_out,
        "searches": list(discarded.get("searches") or []),
    }


def _searches_cost(model_id, counts):
    """``(usd, billed_units, unmeasured, unpriced)`` for per-attempt counts.

    A None count is an attempt that could search and did not say how often:
    counted as ``unmeasured``, never priced.
    """
    usd = 0.0
    units = 0
    unmeasured = 0
    unpriced = 0
    for count in counts:
        if count is None:
            unmeasured += 1
            continue
        s_usd, s_units, s_unpriced = _search_cost(model_id, count)
        usd += s_usd
        units += s_units
        unpriced += s_unpriced
    return usd, units, unmeasured, unpriced
