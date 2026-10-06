"""Reading past page one of a paginated citation source (#339).

``resolver._resolve_known_url`` judges a claim against the page it fetched. A
claim that is true but sits on page 2 of an archive, a listing or a multi-page
article was then reported "read, but does not support the claim", and a count
claim checked against a paginated index could not be settled.

This module finds the next page of what was fetched and reads on, in the order
that costs least and trusts most:

1. **WordPress REST totals.** ``X-WP-Total`` and ``X-WP-TotalPages`` ride on the
   response already in hand and give an exact count of a collection. Stated to
   the verifier as a line of text; nothing is fetched for them.
2. **``rel=next``.** The ``Link`` response header (which WordPress REST sends as
   well, so a REST collection walks without any special case), then ``<link>`` or
   ``<a>`` carrying ``rel="next"``.
3. **A numbered page the page links to.** ``/page/N/``, ``?page=N`` or
   ``?paged=N`` where the current page's own markup contains an anchor to exactly
   the next number. Never guessed: a single article would answer a made-up
   ``/page/2/`` with a 404 for every citation in a report.

A hard cap (``MAX_PAGES``, the first page included) bounds the fetches, and a
link to another site, another scheme or an already-seen page is not followed. A
page that fails stops the walk and keeps what was read. Everything is
best-effort: nothing here may cost a citation the resolution it would have had.

The fetch and the text extraction are passed in, so this module does no network
I/O of its own and the resolver's SSRF-guarded ``safe_get`` stays the only way
out.
"""

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

#: Most pages read for one citation, the first included. A page of a listing is a
#: fetch and, in the verifier's window, a share of 20,000 characters; five covers
#: a paginated article or the first fifty posts of an archive, and a source that
#: needs more than that is a source to open by hand (the result says so).
MAX_PAGES = 5

_NUMBERED_PATH = re.compile(r"^(?P<head>.*/page/)(?P<n>\d+)(?P<tail>/?)$")
_PAGE_PARAMS = ("page", "paged")


@dataclass
class Followed:
    """What reading past the first page produced.

    ``text`` is only the *additional* material (a totals line, then the text of
    each following page), to be appended to what was already read; empty when
    there was nothing more. ``pages_read`` counts the first page. ``stopped``
    says why the walk ended early (``"page cap"``, ``"fetch failed (...)"``,
    ``"unreadable page"``) and is None when it ran out of next pages.
    """

    text: str = ""
    pages_read: int = 1
    stopped: str | None = None
    totals: dict | None = None
    urls: list = field(default_factory=list)

    @property
    def noteworthy(self):
        """Whether the result should say anything about pagination at all."""
        return bool(self.pages_read > 1 or self.stopped or self.totals)


def collection_totals(resp):
    """``{"total", "total_pages"}`` from WordPress REST headers, or None."""
    headers = getattr(resp, "headers", None)
    try:
        total = headers.get("X-WP-Total")
        pages = headers.get("X-WP-TotalPages")
    except Exception:  # noqa: BLE001 - not a mapping, so not a REST response
        return None
    if not (isinstance(total, str) and isinstance(pages, str)):
        return None
    if not (total.strip().isdigit() and pages.strip().isdigit()):
        return None
    return {"total": int(total), "total_pages": int(pages)}


def totals_line(totals):
    """The totals as a sentence a verifier model will read as page content."""
    return (
        "[Collection totals reported by the server for this listing "
        f"(X-WP-Total: {totals['total']}, X-WP-TotalPages: {totals['total_pages']}): "
        f"{totals['total']} items in all, across {totals['total_pages']} page(s).]"
    )


class _Links(HTMLParser):
    """``href`` and ``rel`` of every ``<link>`` and ``<a>`` in a document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("a", "link"):
            return
        attrs = dict(attrs)
        href = attrs.get("href")
        if href:
            self.links.append((href, (attrs.get("rel") or "").lower().split()))


def _site(url):
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_site(a, b):
    return bool(_site(a)) and _site(a) == _site(b)


def _key(url):
    """A URL reduced to what identifies a page, for loop detection."""
    parts = urlsplit(url)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return (_site(url), path, parts.query)


def _numbered_candidates(url):
    """The URLs the page after ``url`` would have under the common numbering."""
    parts = urlsplit(url)
    candidates = []

    match = _NUMBERED_PATH.match(parts.path)
    if match:
        path = f"{match['head']}{int(match['n']) + 1}{match['tail']}"
    else:
        base = parts.path if parts.path.endswith("/") else parts.path + "/"
        path = f"{base}page/2/"
    candidates.append(urlunsplit(parts._replace(path=path)))

    query = parse_qsl(parts.query, keep_blank_values=True)
    for name in _PAGE_PARAMS:
        current = next((v for k, v in query if k == name and v.isdigit()), None)
        rest = [(k, v) for k, v in query if k != name]
        nxt = int(current) + 1 if current is not None else 2
        candidates.append(
            urlunsplit(parts._replace(query=urlencode(rest + [(name, str(nxt))])))
        )
    return candidates


def _header_next(resp, current_url):
    link = getattr(resp, "headers", None)
    try:
        value = link.get("Link")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(value, str):
        return None
    for entry in requests.utils.parse_header_links(value):
        rels = str(entry.get("rel", "")).lower().split()
        if "next" in rels and entry.get("url"):
            return urljoin(current_url, entry["url"])
    return None


def _markup_links(resp):
    headers = getattr(resp, "headers", None)
    try:
        ctype = headers.get("Content-Type")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(ctype, str) or "html" not in ctype.lower():
        return []
    raw = getattr(resp, "content", None)
    if not isinstance(raw, (bytes, bytearray)):
        return []
    encoding = getattr(resp, "encoding", None)
    try:
        text = bytes(raw).decode(
            encoding if isinstance(encoding, str) else "utf-8", "replace"
        )
        parser = _Links()
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup is "no links"
        return []
    return parser.links


def next_url(resp, current_url):
    """The URL of the page after ``resp``'s, or None.

    ``Link: rel=next`` first, then ``rel="next"`` markup, then an anchor to
    exactly the next ``/page/N/`` or ``?page=N``. Only an http(s) address on the
    same site as ``current_url`` is returned.
    """
    found = _header_next(resp, current_url)
    links = _markup_links(resp) if found is None else []
    if found is None:
        for href, rels in links:
            if "next" in rels:
                found = urljoin(current_url, href)
                break
    if found is None and links:
        wanted = {_key(c) for c in _numbered_candidates(current_url)}
        for href, _rels in links:
            absolute = urljoin(current_url, href)
            if _key(absolute) in wanted:
                found = absolute
                break
    if not found:
        return None
    if urlsplit(found).scheme not in ("http", "https"):
        return None
    if not _same_site(found, current_url):
        return None
    return found.split("#", 1)[0]


def follow(first_resp, first_url, first_text, fetch, extract, timeout):
    """Read on from ``first_resp`` and return what was found. Never raises.

    ``fetch(url, timeout=...)`` returns a response and raises on failure;
    ``extract(resp, url)`` returns ``(text, kind)``. ``first_text`` is not used
    beyond signature symmetry with the caller, which keeps the first page itself.
    """
    out = Followed()
    parts = []
    out.totals = collection_totals(first_resp)
    if out.totals:
        parts.append(totals_line(out.totals))

    seen = {_key(first_url)}
    resp, url = first_resp, first_url
    while True:
        try:
            following = next_url(resp, url)
        except Exception as exc:  # noqa: BLE001 - never cost the citation its read
            log.debug("Pagination: could not look for a next page of %s: %s", url, exc)
            break
        if not following or _key(following) in seen:
            break
        if out.pages_read >= MAX_PAGES:
            out.stopped = "page cap"
            break
        seen.add(_key(following))
        try:
            resp = fetch(following, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            out.stopped = f"fetch failed ({type(exc).__name__})"
            break
        try:
            text, _kind = extract(resp, following)
        except Exception:  # noqa: BLE001
            text = ""
        if not (text or "").strip():
            out.stopped = "unreadable page"
            break
        parts.append(f"[Next page of this source: {following}]\n{text}")
        out.pages_read += 1
        out.urls.append(following)
        url = following

    out.text = "\n\n".join(parts)
    return out
