"""Does every cited address in a draft carry its archive copy beside it?

The house convention is that a source appears twice, the live address and the
Wayback snapshot of it::

    https://example.org/report (archived: https://web.archive.org/web/2026.../https://example.org/report)

Nothing used to check that at publish time. Section 9 of a review lists the
pairs, a person is meant to carry them into the draft, and a draft that skipped
that step published without a word: on 2026-11-28, seven articles from
2026-11-11 on had no archive link for any of their live sources, while the
2026-10-29 series had them all.

This module only reports. A source can legitimately have no snapshot (archive.org
refused it, or the page is a login wall), so the checklist's yes is still the
gate; this makes the gap visible at the moment that yes is asked for.
"""

import re
from urllib.parse import urlsplit

_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_WAYBACK = re.compile(
    r"^https?://(?:www\.)?web\.archive\.org/web/\d+[a-z_]*/(?P<orig>https?://.+)$",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?"


def _clean(url):
    return url.rstrip(_TRAILING)


def _key(url):
    """The address with scheme, ``www.``, fragment and trailing slash ignored."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def citation_pairs(text, own_hosts=()):
    """Split a draft's external addresses into live and archived.

    Returns ``(live, archived)``: ``live`` is the addresses in first-cited order,
    each once; ``archived`` maps the pairing key of the original address to the
    archive link. Addresses on ``own_hosts`` (the publication's own site) are not
    citations and are left out.
    """
    own = {h.lower().removeprefix("www.") for h in own_hosts if h}
    live, seen, archived = [], set(), {}
    for raw in _URL.findall(text):
        url = _clean(raw)
        wb = _WAYBACK.match(url)
        if wb:
            archived.setdefault(_key(_clean(wb.group("orig"))), url)
            continue
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        if host in own or host == "web.archive.org" or host == "archive.org":
            continue
        if _key(url) not in seen:
            seen.add(_key(url))
            live.append(url)
    return live, archived


def unpaired_citations(text, own_hosts=()):
    """The live addresses in ``text`` with no archive copy of the same address."""
    live, archived = citation_pairs(text, own_hosts)
    return [u for u in live if _key(u) not in archived]


def describe_unpaired(text, own_hosts=(), limit=15):
    """A block of text for the console, or ``""`` when every citation is paired."""
    live, archived = citation_pairs(text, own_hosts)
    missing = [u for u in live if _key(u) not in archived]
    if not missing:
        return ""
    lines = [
        "CITATIONS WITHOUT AN ARCHIVE COPY: "
        f"{len(missing)} of {len(live)} cited address(es) have no "
        "web.archive.org link beside them.",
        "  Each source should read: <live URL> (archived: <Wayback URL>). Section 9 of "
        "the review lists the pairs;",
        "  `ci-review --archive-only` fills in any it lacks. A source archive.org "
        "could not capture may stay live-only.",
    ]
    for url in missing[:limit]:
        lines.append(f"  - {url}")
    if len(missing) > limit:
        lines.append(f"  ... and {len(missing) - limit} more")
    return "\n".join(lines)
