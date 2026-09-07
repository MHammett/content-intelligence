"""What actually happened to one citation, as a single closed vocabulary.

Two places report on citation outcomes — the run summary printed to the console
and SECTION 9 of the readable review — and they classified independently. They
disagreed. The console derived "unresolved" by subtraction, so the five claims
whose source was fetched, read, and found *not* to support the claim were filed
with the ones no adapter had even attempted, under a line that simultaneously
read "0 could not be verified". SECTION 9, working from its own table, reported
those same five correctly.

Divergence was the bug, so the vocabulary lives in one place and both callers
read it from here.
"""

__all__ = ["DISPOSITIONS", "disposition", "label_for"]

#: Every outcome a citation can end in, ordered from strongest evidence to
#: weakest, with the phrasing used in the readable report.
DISPOSITIONS = (
    ("checksum", "Read, and supports the claim"),
    ("content_mismatch", "Read, and does NOT support the claim"),
    ("unverifiable", "Fetched, but could not be read"),
    ("fetch_failed", "Source URL identified, but the fetch was refused"),
    ("pointer", "Pointer only — nothing retrieved"),
    ("no_source", "No source identified"),
)

_KEYS = frozenset(key for key, _ in DISPOSITIONS)
_LABELS = dict(DISPOSITIONS)


def disposition(citation):
    """Which :data:`DISPOSITIONS` bucket ``citation`` belongs in.

    Anything that never reached a verification tier is one of the two
    "nothing was read" buckets, regardless of ``resolved``: ``fetch_failed``
    when a URL was identified and the fetch did not succeed, ``no_source``
    when there was no URL to try.

    Total by construction — every citation lands in exactly one bucket, so
    counts built from this always sum to the number of citations.
    """
    tier = citation.get("verification")
    if tier in _KEYS:
        return tier
    return "fetch_failed" if citation.get("url") else "no_source"


def label_for(key):
    """Human-readable phrasing for a disposition key."""
    return _LABELS.get(key, key)
