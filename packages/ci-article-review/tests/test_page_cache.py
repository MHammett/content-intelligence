"""One fetch per cited page per run (issue #356).

``quote_check`` read every page a fact-check verdict cites, and Section 9
(``resolver._resolve_known_url``) then read them again, once per claim citing
them. Measured on a saved ``maximum`` run: 110 pages for the quote check, then
325 fetches in Section 9 over 117 distinct URLs. A ``PageCache``, made once per
run, shares the successful fetches between the two.

What the tests hold the design to:

* the saving is real (fetch counts through the real quote check and resolver, with
  only the socket layer replaced);
* nothing the resolver's ladder decides changes: a 403 still escalates and then
  falls to the archive, a failed fetch is never kept, ``verified_via`` is what it
  was;
* parallel claims share a slow fetch, rather than racing past a plain dict;
* the cache is per run and per instance, never module state.
"""

import threading
import time
from collections import Counter

import pytest
import requests

from ci_article_review.adapters.citation import page_cache, quote_check, resolver
from ci_article_review.adapters.citation.page_cache import PageCache

_PAGE_A = "https://example.org/report-a"
_PAGE_B = "https://example.org/report-b"
_QUOTE = "The plant operates nine generating units across two sites."
_BODY = (
    "<html><head><title>Report</title></head><body><article><h1>Report</h1><p>"
    + (_QUOTE + " ") * 12
    + "</p></article></body></html>"
)


class _Resp:
    """What the resolver and the quote check read from a ``requests`` response."""

    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    encoding = "utf-8"
    url = ""

    def __init__(self, body=_BODY):
        self.content = body.encode("utf-8")

    def raise_for_status(self):
        return None


def _refusal(status):
    response = type("R", (), {"status_code": status, "headers": {}})()
    return requests.exceptions.HTTPError(f"{status} error", response=response)


class TestPageCache:
    def test_a_second_request_for_a_page_is_served_without_fetching(self):
        cache, calls = PageCache(), []

        def produce():
            calls.append(1)
            return _Resp()

        first = cache.get_page(_PAGE_A, produce, 1)
        second = cache.get_page(_PAGE_A, produce, 1)
        assert first is second
        assert len(calls) == 1
        assert cache.stats() == {"fetched": 1, "reused": 1}

    def test_pages_are_kept_per_url(self):
        cache = PageCache()
        a = cache.get_page(_PAGE_A, _Resp, 1)
        b = cache.get_page(_PAGE_B, _Resp, 1)
        assert a is not b

    def test_the_direct_fetch_and_the_escalated_one_are_separate_entries(self):
        """The same URL has two answers: what an honest request got, and what the
        browser-fingerprint retry got. One must never stand in for the other."""
        cache = PageCache()
        direct = cache.get_page(_PAGE_A, _Resp, 1)
        escalated = cache.get_escalated(_PAGE_A, lambda: (_PAGE_A, "text", "html"), 1)
        assert escalated == (_PAGE_A, "text", "html")
        assert cache.get_page(_PAGE_A, lambda: pytest.fail("refetched"), 1) is direct

    def test_a_failure_is_raised_and_not_kept(self):
        """The resolver's ladder runs from the failure, so the next caller has to
        see the page fail for itself, and may find it recovered."""
        cache, calls = PageCache(), []

        def produce():
            calls.append(1)
            if len(calls) == 1:
                raise _refusal(429)
            return _Resp()

        with pytest.raises(requests.exceptions.HTTPError):
            cache.get_page(_PAGE_A, produce, 1)
        assert cache.get_page(_PAGE_A, produce, 1) is not None
        assert len(calls) == 2

    def test_nothing_is_not_kept(self):
        """A block that held (``None``) is not an answer to remember."""
        cache, calls = PageCache(), []

        def produce():
            calls.append(1)
            return None

        assert cache.get_escalated(_PAGE_A, produce, 1) is None
        assert cache.get_escalated(_PAGE_A, produce, 1) is None
        assert len(calls) == 2

    def test_two_caches_share_nothing(self):
        """Per run means per instance: no class or module state to leak a page
        from one run, or one replay, into the next."""
        first, second = PageCache(), PageCache()
        first.get_page(_PAGE_A, _Resp, 1)
        calls = []
        second.get_page(_PAGE_A, lambda: calls.append(1) or _Resp(), 1)
        assert calls == [1]
        assert not any(isinstance(v, PageCache) for v in vars(page_cache).values()), (
            "a module-level PageCache would outlive a run"
        )

    def test_a_page_over_the_byte_budget_is_not_kept(self):
        cache, calls = PageCache(max_bytes=10), []

        def produce():
            calls.append(1)
            return _Resp("x" * 50)

        cache.get_page(_PAGE_A, produce, 1)
        cache.get_page(_PAGE_A, produce, 1)
        assert len(calls) == 2

    def test_pages_within_the_budget_are_kept(self):
        cache, calls = PageCache(max_bytes=10_000), []

        def produce():
            calls.append(1)
            return _Resp("x" * 50)

        cache.get_page(_PAGE_A, produce, 1)
        cache.get_page(_PAGE_A, produce, 1)
        assert len(calls) == 1


class TestParallelCallers:
    """Section 9 resolves claims in parallel, so several ask for one page before
    the first answer is back."""

    def _race(self, cache, produce, callers=6, wait=5):
        results = [None] * callers
        errors = []
        arrived = threading.Semaphore(0)

        def call(i):
            arrived.release()
            try:
                results[i] = cache.get_page(_PAGE_A, produce, wait)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=call, args=(i,)) for i in range(callers)]
        for t in threads:
            t.start()
        return threads, results, errors, arrived

    def test_callers_arriving_during_a_fetch_wait_for_it(self):
        cache, calls = PageCache(), []
        callers = 6
        page = _Resp()
        arrived_all = threading.Event()

        def produce():
            calls.append(1)
            # Hold the fetch open until every caller has started, then a moment
            # more for them to reach the cache, so a cache that does not make
            # them wait would let each one fetch.
            arrived_all.wait(5)
            time.sleep(0.3)
            return page

        threads, results, errors, arrived = self._race(cache, produce, callers)
        for _ in range(callers):
            arrived.acquire(timeout=5)
        arrived_all.set()
        for t in threads:
            t.join(10)
        assert not errors
        assert len(calls) == 1
        assert all(r is page for r in results)

    def test_when_the_first_fetch_fails_each_waiter_fetches_for_itself(self):
        """What every caller did before the cache: nothing is lost by waiting."""
        cache, calls = PageCache(), []
        callers = 4
        lock = threading.Lock()
        arrived_all = threading.Event()

        def produce():
            with lock:
                calls.append(1)
                n = len(calls)
            if n == 1:
                arrived_all.wait(5)
                time.sleep(0.3)
                raise _refusal(429)
            return _Resp()

        threads, results, errors, arrived = self._race(cache, produce, callers)
        for _ in range(callers):
            arrived.acquire(timeout=5)
        arrived_all.set()
        for t in threads:
            t.join(10)
        assert len(errors) == 1 and isinstance(errors[0], requests.HTTPError)
        assert sum(r is not None for r in results) == callers - 1
        assert len(calls) == callers

    def test_a_waiter_does_not_wait_past_its_own_timeout(self):
        cache = PageCache()
        release = threading.Event()
        entered = threading.Event()

        def slow():
            entered.set()
            release.wait(10)
            return _Resp()

        leader = threading.Thread(target=lambda: cache.get_page(_PAGE_A, slow, 10))
        leader.start()
        assert entered.wait(5)
        started = time.monotonic()
        mine = cache.get_page(_PAGE_A, _Resp, 0.1)
        waited = time.monotonic() - started
        release.set()
        leader.join(10)
        assert mine is not None
        assert waited < 2


def _fetch_counter(monkeypatch, behave=None, delay=0):
    """Replace the socket layer under both consumers; return the list of URLs hit.

    ``behave(url, n)`` (``n`` counts this URL's calls, from 1) may raise or return
    a response; the default serves ``_BODY``.
    """
    calls = []
    lock = threading.Lock()

    def fake_get(url, timeout=15, **_kw):
        with lock:
            calls.append(url)
            n = Counter(calls)[url]
        if delay:
            time.sleep(delay)
        return behave(url, n) if behave else _Resp()

    monkeypatch.setattr(quote_check, "safe_get", fake_get)
    monkeypatch.setattr(resolver, "safe_get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def _offline_resolution(monkeypatch):
    """Everything Section 9 does besides fetch the page, stubbed."""
    monkeypatch.setattr(
        resolver,
        "_verify_relevance",
        lambda claim, content, keys, author=None: (
            {"checked": True, "verdict": "supports", "reason": "ok", "quote": _QUOTE},
            None,
        ),
    )
    monkeypatch.setattr(resolver.wayback, "check", lambda url, **kw: {"archived": True})
    monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_verify_archive_matches", lambda *a, **k: None)


def _verdicts(url, n):
    return [
        {"claim": f"claim {i}", "source_url": url, "supporting_quote": _QUOTE}
        for i in range(n)
    ]


def _results(*urls):
    return {
        ("gemini", "fact_check"): {
            "failed": False,
            "data": {"confirmed": [v for url in urls for v in _verdicts(url, 1)]},
        }
    }


def _claims(url, n):
    return [{"claim": f"claim {i} of {url}", "known_urls": [url]} for i in range(n)]


def _section_9(claims, cache):
    return resolver.resolve_citations(claims, [], {}, page_cache=cache)


def _outcome(result):
    return (
        result.get("verification"),
        result.get("checksum"),
        result.get("verified_via"),
    )


class TestQuoteCheckAndSectionNineShareFetches:
    def test_each_page_is_fetched_once_for_the_quote_check_and_every_claim(
        self, monkeypatch
    ):
        calls = _fetch_counter(monkeypatch)
        cache = PageCache()
        checks, summary = quote_check.check_quotes(
            _results(_PAGE_A, _PAGE_B), cache=cache
        )
        resolved = _section_9(_claims(_PAGE_A, 3) + _claims(_PAGE_B, 3), cache)

        assert summary["found"] == 2
        assert Counter(calls) == {_PAGE_A: 1, _PAGE_B: 1}
        assert [r["verification"] for r in resolved] == ["checksum"] * 6

    def test_sharing_changes_what_is_fetched_and_nothing_the_resolver_reports(
        self, monkeypatch
    ):
        calls = _fetch_counter(monkeypatch)
        claims = _claims(_PAGE_A, 3) + _claims(_PAGE_B, 2)
        uncached = [_outcome(r) for r in _section_9(claims, None)]
        uncached_fetches = len(calls)
        del calls[:]
        cache = PageCache()
        quote_check.check_quotes(_results(_PAGE_A, _PAGE_B), cache=cache)
        shared = [_outcome(r) for r in _section_9(claims, cache)]

        assert shared == uncached
        assert uncached_fetches == 5
        assert len(calls) == 2

    def test_without_a_cache_every_claim_fetches_its_own_copy(self, monkeypatch):
        """The behaviour #356 measured, kept as the control for the tests above:
        2 pages, 6 claims, and 8 fetches."""
        calls = _fetch_counter(monkeypatch)
        quote_check.check_quotes(_results(_PAGE_A, _PAGE_B))
        _section_9(_claims(_PAGE_A, 3) + _claims(_PAGE_B, 3), None)
        assert Counter(calls) == {_PAGE_A: 4, _PAGE_B: 4}

    def test_claims_resolved_in_parallel_share_one_slow_fetch(self, monkeypatch):
        calls = _fetch_counter(monkeypatch, delay=0.3)
        resolved = _section_9(_claims(_PAGE_A, 6), PageCache())
        assert len(calls) == 1
        assert [r["verification"] for r in resolved] == ["checksum"] * 6

    def test_the_pages_after_the_first_are_shared_too(self, monkeypatch):
        """Section 9 reads on through a paginated source (#339), once per claim."""
        page_two = _PAGE_A + "?page=2"

        def behave(url, n):
            resp = _Resp()
            if url == _PAGE_A:
                resp.headers = {**resp.headers, "Link": f'<{page_two}>; rel="next"'}
            return resp

        calls = _fetch_counter(monkeypatch, behave=behave)
        _section_9(_claims(_PAGE_A, 3), None)
        assert Counter(calls) == {_PAGE_A: 3, page_two: 3}

        del calls[:]
        _section_9(_claims(_PAGE_A, 3), PageCache())
        assert Counter(calls) == {_PAGE_A: 1, page_two: 1}

    def test_a_separate_run_starts_empty(self, monkeypatch):
        calls = _fetch_counter(monkeypatch)
        for _run in range(2):
            quote_check.check_quotes(_results(_PAGE_A), cache=PageCache())
        assert len(calls) == 2

    def test_a_quote_check_without_a_cache_leaves_nothing_behind(self, monkeypatch):
        calls = _fetch_counter(monkeypatch)
        quote_check.check_quotes(_results(_PAGE_A))
        quote_check.check_quotes(_results(_PAGE_A))
        assert len(calls) == 2


class TestTheResolversLadderStillDecides:
    """The cache holds successes. A refusal reaches the resolver as a refusal."""

    @pytest.fixture
    def escalations(self, monkeypatch):
        calls = []

        def escalate(url, timeout):
            calls.append(url)
            return (url, quote_check.resolver._extract_fetched(_Resp(), url)[0], "html")

        monkeypatch.setattr(resolver, "_impersonation_fallback_content", escalate)
        return calls

    def test_a_403_is_escalated_once_for_the_quote_check_and_every_claim(
        self, monkeypatch, escalations
    ):
        def blocked(url, n):
            raise _refusal(403)

        calls = _fetch_counter(monkeypatch, behave=blocked)
        cache = PageCache()
        checks, _summary = quote_check.check_quotes(_results(_PAGE_A), cache=cache)
        resolved = _section_9(_claims(_PAGE_A, 3), cache)

        assert checks[(_PAGE_A, _QUOTE)]["found"] is True
        assert [r["verified_via"] for r in resolved] == ["tls_impersonation"] * 3
        assert [r["origin_failure"] for r in resolved] == ["blocked"] * 3
        assert len(escalations) == 1
        # The refused honest request is not kept: it is made again, and fails
        # again, so the ladder starts where it always started.
        assert len(calls) == 4

    def test_a_failed_escalation_still_falls_to_the_archive(self, monkeypatch):
        monkeypatch.setattr(
            resolver, "_impersonation_fallback_content", lambda url, timeout: None
        )
        snapshot = (
            "https://web.archive.org/web/2026/" + _PAGE_A,
            resolver._extract_fetched(_Resp(), _PAGE_A)[0],
            "html",
        )
        monkeypatch.setattr(
            resolver,
            "_wayback_fallback_content",
            lambda url, timeout: (
                snapshot,
                {"archived": True, "snapshot_url": snapshot[0]},
            ),
        )

        def blocked(url, n):
            raise _refusal(403)

        _fetch_counter(monkeypatch, behave=blocked)
        cache = PageCache()
        quote_check.check_quotes(_results(_PAGE_A), cache=cache)
        resolved = _section_9(_claims(_PAGE_A, 2), cache)

        assert [r["verified_via"] for r in resolved] == ["wayback_fallback"] * 2

    def test_a_page_the_quote_check_could_not_read_is_asked_for_again(
        self, monkeypatch
    ):
        """A rate limit that cooled by the time Section 9 asked is not held
        against the page."""
        monkeypatch.setattr(
            resolver, "_impersonation_fallback_content", lambda url, timeout: None
        )

        def rate_limited_once(url, n):
            if n == 1:
                raise _refusal(429)
            return _Resp()

        calls = _fetch_counter(monkeypatch, behave=rate_limited_once)
        cache = PageCache()
        checks, _summary = quote_check.check_quotes(_results(_PAGE_A), cache=cache)
        resolved = _section_9(_claims(_PAGE_A, 2), cache)

        assert checks[(_PAGE_A, _QUOTE)]["found"] is None
        assert [_outcome(r)[0::2] for r in resolved] == [("checksum", "direct")] * 2
        assert len(calls) == 2
