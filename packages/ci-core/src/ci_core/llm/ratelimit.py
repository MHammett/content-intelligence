"""A per-provider tokens-per-minute meter, so a call waits for room instead of a 429.

Why this exists. On 2026-10-05 a review of a 135,514-character draft lost 5
citation relevance checks to HTTP 429s from Mistral. The Mistral key allows
100,000 tokens a minute, shared by every model on it; the ensemble's three
``mistral-medium-3-5`` calls had just finished having used an estimated 140,000, so the
window was still full when the relevance checks began, and the one 10s retry
landed in the same window (#361). Capping how many calls run at once, or how
fast they start, does nothing about that: the budget is tokens, and it is spent
by whatever ran in the last minute.

Why not read the provider's own count. Mistral sends ``x-ratelimit-remaining-
tokens-minute`` (measured 2026-10-24), but litellm drops the response headers
from a streamed Mistral call, which is how every call here is made. So the meter
counts for itself, from the usage each call reports.

Off unless configured: with no limit set for a provider nothing waits and
nothing is recorded, so a provider whose limit is not known behaves as before.
The limit is the account's, which this code cannot see; set it under
``rate_limits:`` in the user config.

What it counts, and what it assumes. A call reserves an estimate of its prompt
(characters over three, deliberately high) when it starts, and swaps in the real
prompt and completion counts when it ends. That treats a call's tokens as
spent over the minute they were requested in. Mistral does not document when it
charges a streamed call, so a measured live check is the test of that, and the
headroom (90% of the limit) exists to absorb the difference.
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
#: provider name -> tokens per minute
_limits: dict[str, int] = {}
#: provider name -> list of [timestamp, tokens] spent inside the window
_entries: dict[str, list[list[float]]] = {}


def configure(rate_limits):
    """Replace every provider's limit from the ``rate_limits:`` config block.

    ``{"mistral": {"tokens_per_minute": 100000}}``. Called with None, an empty
    mapping or an unreadable one it clears them all, so a run that sets none
    does not inherit an earlier run's. A bad entry is skipped with a warning
    and never raises: a typo here must not stop a review.
    """
    limits = {}
    if isinstance(rate_limits, dict):
        for provider, spec in rate_limits.items():
            value = spec.get("tokens_per_minute") if isinstance(spec, dict) else None
            try:
                tokens = int(value) if value is not None else 0
            except (TypeError, ValueError):
                tokens = 0
            if tokens > 0:
                limits[str(provider)] = tokens
            else:
                log.warning(
                    f"rate_limits.{provider}: tokens_per_minute must be a positive "
                    f"integer, got {value!r}. No limit set for {provider}."
                )
    with _lock:
        _limits.clear()
        _limits.update(limits)
        _entries.clear()


def estimate_tokens(*texts):
    """A high estimate of the tokens in ``texts``, for reserving room before a call."""
    chars = sum(len(t or "") for t in texts)
    return int(chars / _CHARS_PER_TOKEN) + 1


def _prune(entries, now):
    cutoff = now - _WINDOW_SECONDS
    entries[:] = [e for e in entries if e[0] > cutoff]


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


def reserve(provider, estimate):
    """Wait until ``estimate`` tokens fit under ``provider``'s limit, then claim them.

    Returns a ticket for :func:`settle` or :func:`release`, or None when the
    provider has no limit set (nothing is waited for or recorded). Never waits
    for room that cannot come: a call larger than the whole budget goes once the
    window is empty, and no call waits past ``_MAX_WAIT_SECONDS``.
    """
    started = time.monotonic()
    while True:
        with _lock:
            limit = _limits.get(provider)
            if not limit:
                return None
            budget = limit * _HEADROOM
            entries = _entries.setdefault(provider, [])
            now = time.monotonic()
            _prune(entries, now)
            used = sum(e[1] for e in entries)
            waited = now - started
            if used == 0 or used + estimate <= budget or waited >= _MAX_WAIT_SECONDS:
                ticket = [now, estimate]
                entries.append(ticket)
                return ticket
            pause = _seconds_until_room(entries, estimate, budget, now)
        pause = min(max(pause, 0.05), _MAX_WAIT_SECONDS - waited)
        log.info(
            f"{provider}: {used:,} of {limit:,} tokens/minute in use, waiting "
            f"{pause:.1f}s for room for about {estimate:,} more"
        )
        time.sleep(pause)


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
    """Give a reservation back, for a call the provider refused and so did not count."""
    if ticket is None:
        return
    with _lock:
        for entries in _entries.values():
            entries[:] = [e for e in entries if e is not ticket]


def note_refused(provider):
    """Record that ``provider`` refused a call as over its limit (a 429).

    The meter's own count can be wrong: the prompt estimate is a guess, and the
    key may be shared with another review running elsewhere, which this process
    cannot see. A refusal is the provider's own reading of the window, so it
    stands in for a full one: the next call waits a window instead of walking
    into the same refusal. A burst of refusals leaves one such marker, stamped at
    the latest, not one per refusal.
    """
    with _lock:
        limit = _limits.get(provider)
        if not limit:
            return
        entries = _entries.setdefault(provider, [])
        entries[:] = [e for e in entries if len(e) < 3]
        entries.append([time.monotonic(), limit, True])
