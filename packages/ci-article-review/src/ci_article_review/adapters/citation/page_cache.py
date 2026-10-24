"""One fetch per page per run, shared by the quote check and Section 9 (#356).

``quote_check`` reads every page a fact-check verdict cites, and
``resolver._resolve_known_url`` then reads them again, once **per claim** that
cites them. Measured on a saved ``maximum`` run of a long article (issue #356):
110 pages for the quote check, then 325 fetches in Section 9 over 117 distinct
URLs, 249 of them pages the quote check had already read and 208 of them a page
an earlier claim had already read. Hosts that rate-limit see the same request
many times over.

A ``PageCache`` is made once per run, in ``pipeline`` and nowhere else, and
handed to both consumers. It holds nothing between runs and is never written
into a capture or a report, so ``--replay`` starts from an empty one like any
other run.

**What is shared, and what is not.**

* The direct fetch, as the response, not as extracted text. The quote check
  reads PDFs to 400 pages and keeps a second all-visible-text view; the resolver
  reads PDFs to 40 pages and keeps article text only. Each still extracts what
  it wants from the same bytes.
* The 403 retry behind a browser TLS fingerprint, which both of them make. It
  is the dearer of the two requests and is the same request in both.
* Only a fetch that succeeded. A failure is never kept, so a page that
  rate-limited the quote check is asked for again by Section 9 as it is today,
  and the resolver's ladder (403 retry, then the archive.org fallback) still
  runs from the failure and still decides ``verified_via``. The archive.org
  fallback itself is not shared: only the resolver uses it.

**Parallel callers.** Section 9 resolves claims in parallel, so several jobs ask
for one URL before the first answer is back; a plain dict would let all of them
fetch. The first caller fetches and the others wait for it, for no longer than
their own request timeout. If it failed, or has not answered by then, each
waiter fetches for itself, which is what every caller did before this existed.
"""

import threading

#: Most response bytes kept in one run. A page past the budget is still handed to
#: the callers waiting on it and is simply not kept for later ones, so the
#: worst case is today's behaviour for that page. PDFs are the large ones; the
#: quote check reads them to 400 pages.
_MAX_BYTES = 256 * 1024 * 1024


class _Entry:
    """One URL's fetch: in flight until ``done`` is set."""

    __slots__ = ("done", "ok", "value")

    def __init__(self):
        self.done = threading.Event()
        self.ok = False
        self.value = None


def _size_of(value):
    """Bytes a cached value holds: a response body, or a tuple of text."""
    content = getattr(value, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    if isinstance(value, tuple):
        return sum(len(part) for part in value if isinstance(part, (str, bytes)))
    return 0


class PageCache:
    """A per-run cache of successful page fetches, keyed by URL."""

    def __init__(self, max_bytes=_MAX_BYTES):
        self._lock = threading.Lock()
        self._entries = {}
        self._bytes = 0
        self._max_bytes = max_bytes
        self.fetched = 0
        self.reused = 0

    def get_page(self, url, produce, wait):
        """The direct fetch of ``url``. ``produce()`` returns the response or raises."""
        return self._shared(("page", url), produce, wait)

    def get_escalated(self, url, produce, wait):
        """The browser-fingerprint retry of ``url``. ``produce()`` returns the
        ``(final_url, content, kind)`` tuple, or None when the block held."""
        return self._shared(("escalated", url), produce, wait)

    def stats(self):
        """``{"fetched": n, "reused": n}``: requests made, and requests saved."""
        with self._lock:
            return {"fetched": self.fetched, "reused": self.reused}

    def _shared(self, key, produce, wait):
        with self._lock:
            entry = self._entries.get(key)
            leader = entry is None
            if leader:
                entry = self._entries[key] = _Entry()
        if leader:
            return self._lead(key, entry, produce)
        # ``wait`` is the caller's request timeout: past it the leader has
        # outlived any single request, so this caller stops depending on it.
        if entry.done.wait(wait) and entry.ok:
            with self._lock:
                self.reused += 1
            return entry.value
        return produce()

    def _lead(self, key, entry, produce):
        value = None
        try:
            value = produce()
            return value
        finally:
            self._settle(key, entry, value)

    def _settle(self, key, entry, value):
        """Publish the leader's outcome to its waiters, and keep it if it fits.

        ``value`` is None for a failure (``produce`` raised) and for a fetch that
        produced nothing; neither is kept, so the next caller leads again.
        """
        size = _size_of(value)
        with self._lock:
            self.fetched += 1
            if value is not None:
                entry.value, entry.ok = value, True
            if value is not None and self._bytes + size <= self._max_bytes:
                self._bytes += size
            else:
                # Waiters hold the entry and still get its value; a caller
                # arriving later finds nothing and fetches again.
                self._entries.pop(key, None)
        entry.done.set()
