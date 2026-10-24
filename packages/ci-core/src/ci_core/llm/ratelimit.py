"""A per-provider (or per-model) meter of tokens and requests a minute, so a call
waits for room instead of a 429.

Why this exists. On 2026-10-05 a review of a 135,514-character draft lost 5
citation relevance checks to HTTP 429s from Mistral. The Mistral key allows
100,000 tokens a minute on ``mistral-small``; the ensemble's three
``mistral-medium-3-5`` calls had just finished having used an estimated 140,000,
so the window was still full when the relevance checks began, and the one 10s
retry landed in the same window (#361).

Why requests as well as tokens. Mistral's limits are per model, and
``mistral-small`` is also held to 100 requests a minute on this account (the
console, 2026-10-24; the docs publish no figure). The relevance check runs
three of those calls at once at ~1.1s each, ~160 a minute, so it draws 429s
with the token window nearly empty (#389). A cap on how many run at once does
not fix a per-minute count; a meter of starts does.

Why per model. A limit named ``mistral`` applies to every Mistral model; one
named ``mistral/mistral-small-latest`` applies to that model alone and wins
over the provider's. Mistral and OpenAI enforce per model, so a single
provider-wide figure either throttles the large model to the small one's
budget or lets the small one through.

Why not read the provider's own count. Mistral sends
``x-ratelimit-remaining-tokens-minute`` and ``-req-minute`` on a non-streamed
call (measured 2026-10-24), but litellm drops the response headers from a
streamed Mistral call, which is how every call here is made. So the meter
counts for itself, from the usage each call reports.

Off unless configured: with no limit set for a provider nothing waits and
nothing is recorded, so a provider whose limit is not known behaves as before.
What is always recorded is a refusal: the client reports every 429 here, and
the pipeline warns at the end of a run about a provider that refused calls with
no limit configured, naming the key to set. The limit is the account's, which
this code cannot see; set it under ``rate_limits:`` in the user config.

What it counts, and what it assumes. A call reserves an estimate of its prompt
(characters over three, deliberately high) when it starts, and swaps in the real
prompt and completion counts when it ends. That treats a call's tokens as
spent over the minute they were requested in. Mistral does not document when it
charges a streamed call, so a measured live check is the test of that, and the
headroom (90% of the limit) exists to absorb the difference. A request counts
from its start, refused or not: the provider counted the attempt.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

#: The window every provider quotes "per minute" over, in seconds.
_WINDOW_SECONDS = 60.0

#: Fraction of the limit a call may fill. The rest absorbs what the estimate
#: and the provider's own counting disagree about.
_HEADROOM = 0.9

#: No call waits longer than this for room, whatever the meter says. A call this
#: far behind has an old reservation in front of it that never settled; running
#: it and taking the provider's answer is better than a stalled run.
_MAX_WAIT_SECONDS = 70.0

#: Characters per token used to estimate a prompt before it is sent. Real
#: English text runs nearer four, so this over-reserves a little.
_CHARS_PER_TOKEN = 3.0

_lock = threading.Lock()
#: limit key ("provider" or "provider/model") -> {"tokens": n | None, "requests": n | None}
_limits: dict[str, dict] = {}
#: limit key -> list of [timestamp, tokens] spent inside the window
_entries: dict[str, list[list[float]]] = {}
#: limit key -> timestamps of calls started inside the window
_starts: dict[str, list[float]] = {}
#: limit key -> the time until which the provider's own refusal says the window is full
_refused_until: dict[str, float] = {}
#: provider name -> HTTP 429s seen this run, limit configured or not
_refusals: dict[str, int] = {}


def _positive_int(value):
    try:
        n = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def configure(rate_limits):
    """Replace every limit from the ``rate_limits:`` config block.

    ``{"mistral": {"tokens_per_minute": 100000, "requests_per_minute": 100}}``,
    keyed by provider or by ``provider/model``. Called with None, an empty
    mapping or an unreadable one it clears them all, so a run that sets none
    does not inherit an earlier run's. A bad entry is skipped with a warning
    and never raises: a typo here must not stop a review.
    """
    limits = {}
    if isinstance(rate_limits, dict):
        for key, spec in rate_limits.items():
            spec = spec if isinstance(spec, dict) else {}
            tokens = _positive_int(spec.get("tokens_per_minute"))
            requests = _positive_int(spec.get("requests_per_minute"))
            if tokens is None and "tokens_per_minute" in spec:
                log.warning(
                    f"rate_limits.{key}: tokens_per_minute must be a positive "
                    f"integer, got {spec.get('tokens_per_minute')!r}. Ignored."
                )
            if requests is None and "requests_per_minute" in spec:
                log.warning(
                    f"rate_limits.{key}: requests_per_minute must be a positive "
                    f"integer, got {spec.get('requests_per_minute')!r}. Ignored."
                )
            if tokens is None and requests is None:
                log.warning(
                    f"rate_limits.{key}: neither tokens_per_minute nor "
                    f"requests_per_minute is set. No limit set for {key}."
                )
                continue
            limits[str(key)] = {"tokens": tokens, "requests": requests}
    with _lock:
        _limits.clear()
        _limits.update(limits)
        _entries.clear()
        _starts.clear()
        _refused_until.clear()
        _refusals.clear()


def _key_for(provider, model):
    """The limit that governs ``provider``'s ``model``: the model's own, else the provider's."""
    if model and f"{provider}/{model}" in _limits:
        return f"{provider}/{model}"
    if provider in _limits:
        return provider
    return None


def has_limit(provider, model=None):
    """True when a call to ``provider`` (``model``, if given) is metered."""
    with _lock:
        return _key_for(provider, model) is not None


def estimate_tokens(*texts):
    """A high estimate of the tokens in ``texts``, for reserving room before a call."""
    chars = sum(len(t or "") for t in texts)
    return int(chars / _CHARS_PER_TOKEN) + 1


def _prune(entries, now):
    cutoff = now - _WINDOW_SECONDS
    entries[:] = [e for e in entries if e[0] > cutoff]


def _prune_starts(starts, now):
    cutoff = now - _WINDOW_SECONDS
    starts[:] = [s for s in starts if s > cutoff]


def _seconds_until_room(entries, need, budget, now):
    """Seconds until enough of the window has expired to fit ``need`` more tokens."""
    used = sum(e[1] for e in entries)
    stamp = now
    for stamp, tokens, *_ in sorted(entries):
        used -= tokens
        if used + need <= budget:
            break
    # A call bigger than the whole budget never fits, so it falls out of the loop
    # having waited for everything to expire, which is when it goes.
    return max(stamp + _WINDOW_SECONDS - now, 0.0)


def _seconds_until_start_room(starts, budget, now):
    """Seconds until enough starts have aged out for one more under ``budget``."""
    ordered = sorted(starts)
    excess = len(ordered) - budget + 1
    if excess <= 0:
        return 0.0
    return max(ordered[excess - 1] + _WINDOW_SECONDS - now, 0.0)


def reserve(provider, estimate, model=None):
    """Wait until a call of ``estimate`` tokens fits under the limit, then claim it.

    Returns a ticket for :func:`settle` or :func:`release`, or None when no limit
    governs the call (nothing is waited for or recorded). Never waits for room
    that cannot come: a call larger than the whole token budget goes once the
    window is empty, and no call waits past ``_MAX_WAIT_SECONDS``.
    """
    started = time.monotonic()
    while True:
        with _lock:
            key = _key_for(provider, model)
            if key is None:
                return None
            limit = _limits[key]
            now = time.monotonic()
            entries = _entries.setdefault(key, [])
            starts = _starts.setdefault(key, [])
            _prune(entries, now)
            _prune_starts(starts, now)
            waited = now - started
            pauses = []
            used = sum(e[1] for e in entries)
            if limit["tokens"]:
                budget = limit["tokens"] * _HEADROOM
                if used and used + estimate > budget:
                    pauses.append(_seconds_until_room(entries, estimate, budget, now))
            if limit["requests"]:
                budget_starts = max(1, int(limit["requests"] * _HEADROOM))
                if len(starts) >= budget_starts:
                    pauses.append(_seconds_until_start_room(starts, budget_starts, now))
            refused_for = _refused_until.get(key, 0.0) - now
            if refused_for > 0:
                pauses.append(refused_for)
            if not pauses or waited >= _MAX_WAIT_SECONDS:
                ticket = [now, estimate]
                entries.append(ticket)
                starts.append(now)
                return ticket
            pause = max(pauses)
            shown = _describe(key, limit, used, len(starts), estimate)
        pause = min(max(pause, 0.05), _MAX_WAIT_SECONDS - waited)
        log.info(f"{shown}, waiting {pause:.1f}s for room")
        time.sleep(pause)


def _describe(key, limit, used, started, estimate):
    parts = []
    if limit["tokens"]:
        parts.append(
            f"{used:,} of {limit['tokens']:,} tokens/minute in use "
            f"(about {estimate:,} more wanted)"
        )
    if limit["requests"]:
        parts.append(f"{started} of {limit['requests']} requests/minute started")
    return f"{key}: " + ", ".join(parts)


def settle(ticket, prompt, completion):
    """Replace a reservation with what the call really used.

    The prompt keeps the time the call started; the completion is stamped now,
    because it arrived over the length of the call and not at its start.
    """
    if ticket is None:
        return
    with _lock:
        ticket[1] = int(prompt or 0)
        provider_entries = next(
            (e for e in _entries.values() if any(x is ticket for x in e)), None
        )
        if provider_entries is not None and completion:
            provider_entries.append([time.monotonic(), int(completion)])


def release(ticket):
    """Give a token reservation back, for a call the provider refused and so did not count.

    The request itself stays counted: the provider saw the attempt.
    """
    if ticket is None:
        return
    with _lock:
        for entries in _entries.values():
            entries[:] = [e for e in entries if e is not ticket]


def note_refused(provider, model=None):
    """Record that ``provider`` refused a call as over its limit (a 429).

    Counted for every provider, metered or not, so the end of the run can say
    which providers were refused with no limit configured. Where a limit is set,
    the refusal also stands in for a full window: the meter's own count can be
    wrong (the prompt estimate is a guess, and the key may be shared with
    another review running elsewhere), and a refusal is the provider's own
    reading, so the next call waits a window instead of walking into the same
    refusal. A burst of refusals extends that window once, not once each.
    """
    with _lock:
        _refusals[provider] = _refusals.get(provider, 0) + 1
        key = _key_for(provider, model)
        if key is None:
            return
        _refused_until[key] = max(
            _refused_until.get(key, 0.0), time.monotonic() + _WINDOW_SECONDS
        )


def refusals():
    """``{provider: count}`` of HTTP 429s seen since :func:`configure`."""
    with _lock:
        return dict(_refusals)


def unmetered_refusals():
    """``{provider: count}`` for providers refused this run with no limit set.

    A provider counts as metered when any limit names it, provider-wide or for
    one of its models.
    """
    with _lock:
        metered = {k.split("/", 1)[0] for k in _limits}
        return {p: n for p, n in _refusals.items() if p not in metered}
