"""The page store under ``safe_get`` (issue #451): a page is reused only after
its origin answers a conditional request with 304, and never otherwise.

A local HTTP server plays the origin. ``classify_host`` is stubbed so the SSRF
guard treats 127.0.0.1 as public, as the guard's own tests do; every hop still
passes through ``_guard``, which ``test_the_guard_still_runs_on_a_stored_page``
checks directly.
"""

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from ci_core import http


class _Origin(BaseHTTPRequestHandler):
    """Routes by path. Records (path, If-None-Match, If-Modified-Since)."""

    seen: list = []
    etag_version = '"v1"'

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        inm = self.headers.get("If-None-Match")
        ims = self.headers.get("If-Modified-Since")
        type(self).seen.append((self.path, inm, ims))
        tag = type(self).etag_version
        if self.path == "/etag":
            if inm == tag:
                return self._send(304, headers=[("ETag", tag)])
            return self._send(200, b"etag body " + tag.encode(), [("ETag", tag)])
        if self.path == "/last-modified":
            if ims:
                return self._send(304)
            lm = "Wed, 21 Oct 2025 07:28:00 GMT"
            return self._send(200, b"lm body", [("Last-Modified", lm)])
        if self.path == "/max-age":
            if inm == '"m"':
                return self._send(304, headers=[("ETag", '"m"')])
            headers = [("ETag", '"m"'), ("Cache-Control", "max-age=3600")]
            return self._send(200, b"max-age body", headers)
        if self.path == "/no-validator":
            return self._send(200, b"plain body")
        if self.path == "/gone":
            return self._send(404, b"gone", [("ETag", '"g"')])
        if self.path == "/big":
            return self._send(200, b"x" * 4096, [("ETag", '"big"')])
        if self.path == "/redirect":
            return self._send(302, headers=[("Location", "/etag")])
        return self._send(500)


@pytest.fixture
def origin(monkeypatch):
    monkeypatch.setattr(http, "classify_host", lambda url: http.HOST_PUBLIC)
    _Origin.seen = []
    _Origin.etag_version = '"v1"'
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    http.close_page_store()


@pytest.fixture
def store(tmp_path):
    assert http.open_page_store(tmp_path / "pages.sqlite")
    yield tmp_path / "pages.sqlite"
    http.close_page_store()


def _conditionals(path):
    return [(inm, ims) for p, inm, ims in _Origin.seen if p == path]


class TestReusedOnlyAfterA304:
    def test_an_unchanged_page_costs_a_304_not_a_body(self, origin, store):
        first = http.safe_get(origin + "/etag")
        second = http.safe_get(origin + "/etag")
        assert first.content == second.content == b'etag body "v1"'
        assert _conditionals("/etag") == [(None, None), ('"v1"', None)]
        assert getattr(second, "from_cache", False) is True
        stats = http.page_store_stats()
        assert stats["stored_hits"] == 1
        assert stats["bytes_not_downloaded"] == len(second.content)
        assert stats["live"] == 1
        assert stats["persistent"] is True

    def test_last_modified_is_a_validator_too(self, origin, store):
        http.safe_get(origin + "/last-modified")
        second = http.safe_get(origin + "/last-modified")
        assert second.content == b"lm body"
        assert _conditionals("/last-modified")[1][1] is not None

    def test_a_changed_page_is_downloaded_again(self, origin, store):
        http.safe_get(origin + "/etag")
        _Origin.etag_version = '"v2"'
        changed = http.safe_get(origin + "/etag")
        assert changed.content == b'etag body "v2"'
        assert getattr(changed, "from_cache", False) is False

    def test_max_age_does_not_skip_the_revalidation(self, origin, store):
        # The content-drift check needs the origin's word on every run; an
        # origin's own max-age must not let a stored body stand in for it.
        http.safe_get(origin + "/max-age")
        http.safe_get(origin + "/max-age")
        assert _conditionals("/max-age") == [(None, None), ('"m"', None)]

    def test_a_page_with_no_validator_is_fetched_in_full_every_time(
        self, origin, store
    ):
        http.safe_get(origin + "/no-validator")
        again = http.safe_get(origin + "/no-validator")
        assert again.content == b"plain body"
        assert getattr(again, "from_cache", False) is False
        assert _conditionals("/no-validator") == [(None, None), (None, None)]

    def test_an_error_page_is_never_kept(self, origin, store):
        http.safe_get(origin + "/gone")
        again = http.safe_get(origin + "/gone")
        assert again.status_code == 404
        assert _conditionals("/gone") == [(None, None), (None, None)]

    def test_a_body_over_the_cap_is_returned_but_not_kept(self, origin, tmp_path):
        assert http.open_page_store(tmp_path / "small.sqlite", max_body_bytes=1024)
        first = http.safe_get(origin + "/big")
        second = http.safe_get(origin + "/big")
        assert len(first.content) == len(second.content) == 4096
        assert _conditionals("/big") == [(None, None), (None, None)]

    def test_kept_across_runs(self, origin, tmp_path):
        path = tmp_path / "pages.sqlite"
        assert http.open_page_store(path)
        http.safe_get(origin + "/etag")
        http.close_page_store()
        assert http.open_page_store(path)
        second = http.safe_get(origin + "/etag")
        assert getattr(second, "from_cache", False) is True
        assert http.page_store_stats()["stored_hits"] == 1

    def test_redirect_hops_are_followed_live_and_the_target_reused(self, origin, store):
        http.safe_get(origin + "/redirect")
        landed = http.safe_get(origin + "/redirect")
        assert landed.content == b'etag body "v1"'
        assert [p for p, *_ in _Origin.seen] == [
            "/redirect",
            "/etag",
            "/redirect",
            "/etag",
        ]
        assert _conditionals("/etag")[1] == ('"v1"', None)


class TestNeverAReasonForWorseFetches:
    def test_off_unless_opened(self, origin):
        http.close_page_store()
        http.safe_get(origin + "/etag")
        again = http.safe_get(origin + "/etag")
        assert getattr(again, "from_cache", False) is False
        assert http.page_store_stats()["persistent"] is False

    def test_a_streamed_fetch_bypasses_the_store(self, origin, store):
        http.safe_get(origin + "/etag", stream=True).close()
        http.safe_get(origin + "/etag", stream=True).close()
        assert _conditionals("/etag") == [(None, None), (None, None)]

    def test_a_store_that_cannot_open_leaves_fetches_live(
        self, origin, tmp_path, caplog
    ):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert http.open_page_store(blocker / "pages.sqlite") is False
        assert "fetched live" in caplog.text
        assert http.safe_get(origin + "/etag").status_code == 200

    def test_a_store_that_breaks_mid_run_leaves_fetches_live(
        self, origin, store, monkeypatch
    ):
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(http, "_cached_session", broken)
        assert http.safe_get(origin + "/etag").content == b'etag body "v1"'

    def test_an_origin_failure_still_raises_as_before(self, origin, store):
        with pytest.raises(requests.exceptions.ConnectionError):
            http.safe_get("http://127.0.0.1:1/unreachable", timeout=2)

    def test_the_guard_still_runs_on_a_stored_page(self, origin, store, monkeypatch):
        http.safe_get(origin + "/etag")
        monkeypatch.setattr(http, "classify_host", lambda url: http.HOST_NON_PUBLIC)
        with pytest.raises(http.UnsafeURLError):
            http.safe_get(origin + "/etag")

    def test_pages_from_concurrent_threads_share_the_store(self, origin, store):
        http.safe_get(origin + "/etag")
        errors, results = [], []

        def fetch():
            try:
                results.append(http.safe_get(origin + "/etag").content)
            except Exception as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)

        threads = [threading.Thread(target=fetch) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert results == [b'etag body "v1"'] * 8
        assert http.page_store_stats()["stored_hits"] == 8
