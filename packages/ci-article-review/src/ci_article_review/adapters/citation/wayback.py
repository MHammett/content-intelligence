"""Citation-review-specific wayback policy, on top of the shared ``spn_client`` engine.

The archive.org submit/check engine itself (rate pacing, circuit breaker,
dual-mode SPN2 submission, the archive-outcome vocabulary) moved to the
standalone ``spn-client`` package — extracted from this module so it could be
shared with other projects that were each independently re-implementing the
same thing. This module now holds only what's specific to citation review:
which HTTP failures on a citation's live link justify falling back to an
archive copy, and how to summarize a wayback result for a human reader.
"""

import logging

import requests

from ci_core.http import UnsafeURLError

from spn_client import (
    ARCHIVE_ARCHIVED,
    ARCHIVE_CAPTURE_FAILED,
    ARCHIVE_NOT_ATTEMPTED,
    ARCHIVE_OUTCOME_LABELS,
    ARCHIVE_PENDING,
    ARCHIVE_SUBMIT_FAILED,
    ARCHIVE_SUBMITTED,
    DEFAULT_STALE_DAYS,
    CaptureCapacityResult,
    CheckResult,
    JobStatusResult,
    SubmitResult,
    SystemStatusResult,
    capture_capacity,
    categorize_job_error,
    check,
    check_job_status,
    rate_limited_out,
    reset_rate_limit_state,
    service_health_note,
    snapshot_raw_url,
    submit,
    system_status,
)

__all__ = [
    "ARCHIVE_ARCHIVED",
    "ARCHIVE_CAPTURE_FAILED",
    "ARCHIVE_NOT_ATTEMPTED",
    "ARCHIVE_OUTCOME_LABELS",
    "ARCHIVE_PENDING",
    "ARCHIVE_SUBMIT_FAILED",
    "ARCHIVE_SUBMITTED",
    "DEFAULT_STALE_DAYS",
    "FALLBACK_REASON_LABELS",
    "CaptureCapacityResult",
    "CheckResult",
    "JobStatusResult",
    "SubmitResult",
    "SystemStatusResult",
    "CAPTURE_SECRET_OPTIONS",
    "CAPTURE_SETTING_OPTIONS",
    "capture_capacity",
    "capture_options",
    "categorize_job_error",
    "check",
    "check_job_status",
    "fallback_reason_for_exception",
    "fallback_reason_for_status",
    "format_summary",
    "rate_limited_out",
    "reset_rate_limit_state",
    "service_health_note",
    "snapshot_raw_url",
    "submit",
    "system_status",
    "transport_failure_summary",
]

log = logging.getLogger(__name__)


#: The SPN2 capture options spn-client exposes on ``submit()`` that carry no
#: secret, with the type each must be. Safe to read from ``user.yaml``.
#:
#: ``js_behavior_timeout`` is bounded 0-30 by archive.org, not by us; a value
#: outside it is rejected by archive.org rather than clamped, which would cost
#: a capture request to discover.
CAPTURE_SETTING_OPTIONS = {
    "js_behavior_timeout": int,
    "skip_first_archive": bool,
    "capture_screenshot": bool,
    "outlinks_availability": bool,
    "delay_wb_availability": bool,
    "use_user_agent": str,
}

#: The three that carry a secret: a login for the page being captured, and a
#: cookie sent to it. These are read from the archive.org *credentials* entry,
#: never from ``pipeline.wayback_capture`` — a password belongs in the channel
#: the rest of this repo's secrets already use, not in a settings file that gets
#: copied between checkouts and pasted into issues.
#:
#: Worth knowing before enabling any of them: a source that needs a login to
#: read is one a reader cannot verify. Archiving it produces a snapshot of a
#: page nobody else can reach, which is a weaker citation than an honest "this
#: is paywalled". They exist here because archive.org supports them and the
#: choice is the author's, not because this pipeline recommends them.
CAPTURE_SECRET_OPTIONS = ("target_username", "target_password", "capture_cookie")


def capture_options(settings=None, creds=None):
    """Assemble ``submit()`` capture kwargs from config and credentials.

    Returns only keys the caller actually set. Every option archive.org offers
    defaults to off, and an omitted key is how spn-client is told "use the
    default" — passing an explicit ``False`` for each would send archive.org a
    capture request full of fields nobody asked about.

    An unknown or mistyped key is dropped with a warning rather than raising. A
    typo in an optional capture setting should not fail a review that would
    otherwise complete; but it must not pass silently either, because the
    failure mode is a run that quietly ignores what the author configured.
    """
    out = {}
    for key, value in (settings or {}).items():
        expected = CAPTURE_SETTING_OPTIONS.get(key)
        if expected is None:
            log.warning(
                "Ignoring unknown wayback_capture option %r (known: %s)",
                key,
                ", ".join(sorted(CAPTURE_SETTING_OPTIONS)),
            )
            continue
        # bool is a subclass of int, so an explicit bool check has to come first
        # or `capture_screenshot: true` would satisfy an int-typed option.
        if isinstance(value, bool) != (expected is bool) or not isinstance(
            value, expected
        ):
            log.warning(
                "Ignoring wayback_capture.%s: expected %s, got %r",
                key,
                expected.__name__,
                value,
            )
            continue
        if key == "js_behavior_timeout" and not 0 <= value <= 30:
            log.warning(
                "Ignoring wayback_capture.js_behavior_timeout=%r: archive.org "
                "accepts 0-30 seconds and refuses anything else",
                value,
            )
            continue
        if value in (False, ""):
            # Off is the default; saying so explicitly buys nothing.
            continue
        out[key] = value

    for key in CAPTURE_SECRET_OPTIONS:
        value = (creds or {}).get(key)
        if value:
            out[key] = value
    return out


#: HTTP statuses where the origin is reachable but refuses to serve *us* the
#: document. The resource itself is not claimed to be gone, so reading
#: archive.org's copy answers the question the origin declined to.
#:
#: Deliberately excluded:
#:   404 / 410 — the resource is genuinely gone. Surfacing that is the whole
#:     point of link validation; an archive copy would mask a real problem the
#:     author has to fix by re-sourcing the claim.
#:   5xx — the origin's own failure, not a refusal aimed at us. A transient 5xx
#:     will be fine by the time a reader clicks, and a persistent one means the
#:     source needs replacing; standing in an archived copy hides both. (A 5xx
#:     is also the shape a misconfigured origin returns for a page it no longer
#:     has, so treating it as "reachable content" is not safe.)
_FALLBACK_STATUSES = {
    401: "auth_required",
    403: "blocked",
    # Rate limiting is transient in a way the others aren't, but it is still
    # "the origin won't serve us right now" rather than "this is gone", and a
    # run shouldn't report a good link as broken because we asked too fast.
    # The distinct reason label keeps that visible in the report.
    429: "rate_limited",
}

#: Human-readable phrasing for each reason, for report output.
FALLBACK_REASON_LABELS = {
    "auth_required": "401 auth required",
    "blocked": "403 blocked",
    "rate_limited": "429 rate limited",
    "timeout": "origin timed out",
    "unreachable": "origin unreachable",
}


def fallback_reason_for_status(status):
    """Reason label if an HTTP ``status`` warrants an archive fallback, else None.

    See ``_FALLBACK_STATUSES`` for what is in scope and, more importantly, what
    is deliberately not.
    """
    return _FALLBACK_STATUSES.get(status)


def fallback_reason_for_exception(exc):
    """Reason label if a fetch exception warrants an archive fallback, else None.

    Covers the "we never reached the origin" failures — connect/read timeouts
    and connection errors (which is where ``requests`` puts DNS resolution
    failures). These say nothing about whether the resource exists, only that
    we couldn't ask, so an archived copy is a legitimate substitute in exactly
    the way it is for a 403.

    An ``HTTPError`` is dispatched to ``fallback_reason_for_status`` so callers
    with one bare ``except`` don't have to special-case it.
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return fallback_reason_for_status(status)
    # Timeout first: ConnectTimeout subclasses both Timeout and ConnectionError,
    # and "timed out" is the more specific description of it.
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "unreachable"
    return None


def transport_failure_summary(exc, what):
    """One reader-facing sentence for an archive.org fetch that did not complete.

    Lives here rather than in ``spn_client`` for two reasons, and the second is
    the operative one:

    1. spn-client's README draws the responsibility line at "anything whose
       correct answer depends on archive.org's own API behavior" — how a report
       phrases a failure is explicitly on our side of it.
    2. The call site this exists for does not go through spn-client at all.
       ``_verify_archive_match`` reads a snapshot's raw bytes with ``safe_get``,
       our own SSRF-guarded fetch, so the exception it catches can be
       ``UnsafeURLError`` — raised when a hop resolves to a non-public address.
       spn-client has no reason to know that type exists, and its own summary
       would fold it into the generic "the request failed", which is exactly
       backwards: a guard rejection is a statement about the URL, not about
       archive.org being unreachable.

    The raw exception belongs in the log. Left unfiltered it reaches whoever
    reads the report as ``HTTPSConnectionPool(host='web.archive.org', port=443):
    Max retries exceeded with url: /save/status/... (Caused by
    NewConnectionError(... [WinError 10061] ...))`` — observed verbatim in a real
    run 2026-09-06. That invites the reader to debug our networking instead of
    telling them what it means for their citation.
    """
    if isinstance(exc, UnsafeURLError):
        return f"the address {what} resolved to a non-public host and was not fetched"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"archive.org did not answer {what} within the timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return (
            f"could not reach archive.org {what} — the connection was refused, "
            f"dropped, or the host did not resolve"
        )
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"archive.org answered HTTP {status} {what}"
    return f"the request to archive.org {what} failed"


def format_summary(wb):
    """One-line human-readable summary of a wayback result.

    The ``archived is None`` branch is the one that matters. It means the lookup
    never completed — the circuit breaker tripped, or the request failed — which
    is NOT the same as "there is no snapshot". Since the breaker makes a null the
    common case rather than a rare one, this says outright that it implies
    nothing about the page.
    """
    if wb.get("archived") is None:
        return (
            f"NOT CHECKED — the archive.org lookup did not complete "
            f"({wb.get('error', 'unknown error')}). This says nothing about "
            f"whether the page is archived."
        )
    if not wb.get("archived"):
        return "Not archived in Wayback Machine"
    age = wb.get("snapshot_age_days")
    stale = wb.get("snapshot_stale")
    age_str = f"{age}d ago" if age is not None else "age unknown"
    flag = " [STALE]" if stale else ""
    return f"Archived — latest snapshot {age_str}{flag}: {wb.get('snapshot_url', '')}"
