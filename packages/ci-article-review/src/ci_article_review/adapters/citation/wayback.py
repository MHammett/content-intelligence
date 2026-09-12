"""Citation-review-specific wayback policy, on top of the shared ``spn_client`` engine.

The archive.org submit/check engine itself (rate pacing, circuit breaker,
dual-mode SPN2 submission, the archive-outcome vocabulary) moved to the
standalone ``spn-client`` package — extracted from this module so it could be
shared with other projects that were each independently re-implementing the
same thing. This module now holds only what's specific to citation review:
which HTTP failures on a citation's live link justify falling back to an
archive copy, and how to summarize a wayback result for a human reader.
"""

import requests

from spn_client import (
    ARCHIVE_ARCHIVED,
    ARCHIVE_CAPTURE_FAILED,
    ARCHIVE_NOT_ATTEMPTED,
    ARCHIVE_OUTCOME_LABELS,
    ARCHIVE_PENDING,
    ARCHIVE_SUBMIT_FAILED,
    ARCHIVE_SUBMITTED,
    capture_capacity,
    check,
    check_job_status,
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
    "FALLBACK_REASON_LABELS",
    "capture_capacity",
    "check",
    "check_job_status",
    "fallback_reason_for_exception",
    "fallback_reason_for_status",
    "format_summary",
    "reset_rate_limit_state",
    "service_health_note",
    "snapshot_raw_url",
    "submit",
    "system_status",
]

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
