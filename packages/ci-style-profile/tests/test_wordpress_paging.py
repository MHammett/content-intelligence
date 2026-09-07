"""The WordPress collector's paged fan-out.

This path had no tests at all, which is part of why it kept a
``ThreadPoolExecutor`` after :mod:`ci_core.concurrency` documented why not.

The hazard here was two-layered. The shared one is the atexit join every pool
carries. The one specific to this module is that ``fetch`` is a *generator* and
the pool lived in a ``with`` block that the code ``yield``ed from — so a caller
that stopped consuming (a break, an exception, or just dropping the iterator)
left the generator suspended inside that block, pool open and workers unjoined,
for as long as it went un-finalized. Fetching a wave at a time and yielding only
after the wave is collected means no thread of ours is alive while suspended,
which is what ``test_no_threads_are_alive_while_the_generator_is_suspended``
pins.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
import requests

from ci_style_profile.collectors.wordpress import WordPressCollector

_CONFIG = {
    "site_url": "https://example.com",
    "username": "u",
    "application_password": "p",
}


def _post(n):
    return {
        "content": {"rendered": f"<p>Body of post {n}, long enough to keep.</p>"},
        "date": "2026-01-01",
        "link": f"https://example.com/{n}",
        "title": {"rendered": f"Post {n}"},
        "id": n,
    }


class _Resp:
    """Minimal stand-in for a requests Response."""

    def __init__(self, posts, total_pages=1, total=None):
        self._posts = posts
        self.headers = {
            "X-WP-Total": str(total if total is not None else len(posts)),
            "X-WP-TotalPages": str(total_pages),
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self._posts


def _collector():
    return WordPressCollector(dict(_CONFIG))


class TestPagedFetch:
    def test_every_page_is_fetched_and_yielded(self):
        """Four pages, one post each: page 1 inline, 2-4 via the fan-out."""
        pages_seen = []

        def _fake_get(url, params=None, **kw):
            page = (params or {}).get("page", 1)
            pages_seen.append(page)
            return _Resp([_post(page)], total_pages=4, total=4)

        with patch.object(requests, "get", side_effect=_fake_get):
            docs = list(_collector().fetch())

        assert sorted(pages_seen) == [1, 2, 3, 4]
        assert len(docs) == 4

    def test_documents_come_back_in_page_order(self):
        """Deterministic order, where completion order used to decide it.

        The old ``as_completed`` loop yielded whichever page the network
        finished first, so a run's document order was a race. Page 2 is slowed
        here so that a regression to completion order would put it last, where
        page order requires it to stay in the middle.
        """

        def _fake_get(url, params=None, **kw):
            page = (params or {}).get("page", 1)
            if page == 2:
                time.sleep(0.25)
            return _Resp([_post(page)], total_pages=3, total=3)

        with patch.object(requests, "get", side_effect=_fake_get):
            docs = list(_collector().fetch())

        assert [d.url_or_id for d in docs] == [
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
        ]

    def test_one_failing_page_is_skipped_not_fatal(self):
        """A single bad page must not cost the whole corpus."""

        def _fake_get(url, params=None, **kw):
            page = (params or {}).get("page", 1)
            if page == 2:
                raise requests.ConnectionError("page 2 is down")
            return _Resp([_post(page)], total_pages=3, total=3)

        with patch.object(requests, "get", side_effect=_fake_get):
            docs = list(_collector().fetch())

        assert [d.url_or_id for d in docs] == [
            "https://example.com/1",
            "https://example.com/3",
        ]

    def test_a_single_page_site_makes_no_extra_requests(self):
        calls = []

        def _fake_get(url, params=None, **kw):
            calls.append((params or {}).get("page", 1))
            return _Resp([_post(1)], total_pages=1, total=1)

        with patch.object(requests, "get", side_effect=_fake_get):
            docs = list(_collector().fetch())

        assert calls == [1]
        assert len(docs) == 1

    def test_a_wedged_page_is_abandoned_rather_than_waited_out(self):
        """The wall-clock backstop, which the socket timeouts cannot provide.

        ``timeout=(10, 60)`` bounds the connect and the gap between reads. A
        server that never returns at all — or dribbles a byte inside every gap —
        satisfies both forever.
        """
        never = threading.Event()  # deliberately never set

        def _fake_get(url, params=None, **kw):
            page = (params or {}).get("page", 1)
            if page == 2:
                never.wait()
            return _Resp([_post(page)], total_pages=2, total=2)

        with (
            patch.object(requests, "get", side_effect=_fake_get),
            patch("ci_style_profile.collectors.wordpress._PAGE_TIMEOUT_SECONDS", 0.3),
        ):
            started = time.monotonic()
            docs = list(_collector().fetch())
            elapsed = time.monotonic() - started

        assert [d.url_or_id for d in docs] == ["https://example.com/1"]
        assert elapsed < 10, f"waited {elapsed:.1f}s on a page that never returns"


class TestGeneratorSuspension:
    """The hazard specific to fanning out inside a generator.

    Consuming *two* documents matters. Page 1 is fetched inline and yielded
    before any fan-out starts, so a test that stops after one document suspends
    the generator before the pool ever existed and would pass against the broken
    version too. The second document can only come from the fan-out, which in
    the old code meant the generator was parked inside the
    ``with ThreadPoolExecutor(...)`` block with its workers live.
    """

    @staticmethod
    def _fan_out_threads(before):
        """Threads that appeared since ``before`` and belong to a fan-out.

        Matches both mechanisms deliberately: ``ThreadPoolExecutor-N_M`` for the
        pool this replaced, and ``ci-group-call-*`` for the daemon threads that
        replaced it. Naming only one would let the other slip through.
        """
        return [
            t
            for t in threading.enumerate()
            if t.name not in before
            and (
                t.name.startswith("ThreadPoolExecutor")
                or t.name.startswith("ci-group-call-")
            )
        ]

    def _slow_pages(self, total_pages=12, hold=0.4):
        def _fake_get(url, params=None, **kw):
            page = (params or {}).get("page", 1)
            if page > 1:
                time.sleep(hold)
            return _Resp([_post(page)], total_pages=total_pages, total=total_pages)

        return _fake_get

    def test_no_threads_are_alive_while_the_generator_is_suspended(self):
        """A half-consumed fetch must not be holding workers.

        The old code yielded from inside ``with ThreadPoolExecutor(...)``, so a
        caller that took a couple of documents and stopped left the generator
        parked in that block: the pool's ``__exit__`` had not run, and its
        workers stayed alive for as long as the generator went un-finalized.
        Fetching a wave at a time and yielding only after the wave is collected
        means a suspended generator owns no threads at all.
        """
        before = {t.name for t in threading.enumerate()}

        with patch.object(requests, "get", side_effect=self._slow_pages()):
            gen = _collector().fetch()
            next(gen)  # page 1, yielded before any fan-out
            next(gen)  # first fan-out document: we are now past the pool

            leaked = self._fan_out_threads(before)
            try:
                assert not leaked, (
                    f"generator suspended with {len(leaked)} fan-out thread(s) "
                    f"still alive: {sorted(t.name for t in leaked)}"
                )
            finally:
                gen.close()

    def test_an_abandoned_generator_leaves_nothing_running(self):
        """Dropping the iterator entirely.

        Weaker than its sibling, and deliberately kept anyway: CPython
        refcounting finalizes the generator the moment the last reference goes,
        which runs the old ``with`` block's ``__exit__`` and shuts its pool down
        — so this passes against the broken version too. It guards the case
        refcounting does not cover (a reference caught in a cycle, or a
        non-refcounted runtime), where finalization is deferred to whenever the
        collector next runs.
        """
        before = {t.name for t in threading.enumerate()}

        with patch.object(requests, "get", side_effect=self._slow_pages()):
            gen = _collector().fetch()
            next(gen)
            next(gen)
            del gen

            leaked = self._fan_out_threads(before)
            assert not leaked, (
                f"threads outlived the generator: {sorted(t.name for t in leaked)}"
            )


class TestConcurrencyBound:
    def test_no_more_than_max_page_parallel_requests_are_in_flight(self):
        """This is someone's live blog; the REST API shares its PHP workers."""
        from ci_style_profile.collectors import wordpress as wp

        live = 0
        peak = 0
        lock = threading.Lock()

        def _fake_get(url, params=None, **kw):
            nonlocal live, peak
            page = (params or {}).get("page", 1)
            if page == 1:
                return _Resp([_post(1)], total_pages=13, total=13)
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1
            return _Resp([_post(page)], total_pages=13, total=13)

        with patch.object(requests, "get", side_effect=_fake_get):
            docs = list(_collector().fetch())

        assert len(docs) == 13
        assert peak <= wp._MAX_PAGE_PARALLEL, (
            f"{peak} concurrent page fetches against a bound of {wp._MAX_PAGE_PARALLEL}"
        )


@pytest.mark.parametrize("total_pages", [1, 2, 5, 6, 11])
def test_page_count_is_respected_exactly(total_pages):
    """Off-by-one in the wave loop would silently drop or refetch a page."""
    seen = []

    def _fake_get(url, params=None, **kw):
        page = (params or {}).get("page", 1)
        seen.append(page)
        return _Resp([_post(page)], total_pages=total_pages, total=total_pages)

    with patch.object(requests, "get", side_effect=_fake_get):
        docs = list(WordPressCollector(dict(_CONFIG)).fetch())

    assert sorted(seen) == list(range(1, total_pages + 1))
    assert len(docs) == total_pages
