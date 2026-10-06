"""The citation resolver reads past page one of a paginated source (#339).

``_resolve_known_url`` fetched one URL and judged the claim against that single
response. A claim that is true but sits on page 2 of an archive, a listing or a
multi-page article was reported "read, but does not support", and a count claim
checked against a paginated index could not be settled at all.

The order, as the issue asks: the WordPress REST totals first (exact and free,
they ride on the response already in hand), then ``rel=next`` (``Link`` header,
``<link>`` or ``<a>``), then a ``/page/N/`` or ``?page=N`` URL the page itself
links to, with a hard cap on pages fetched. A pattern is never guessed at: a
single article would answer a made-up ``/page/2/`` with a 404 for every
citation in the report.

Every test here runs on fake responses; the suite blocks sockets.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from ci_article_review.adapters.citation import pagination, resolver

_BASE = "https://example.org/archive/"


class _Resp:
    """The slice of ``requests.Response`` the resolver and pagination read."""

    def __init__(self, body, url=_BASE, headers=None, content_type="text/html"):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.url = url
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status_code = 200

    def raise_for_status(self):
        pass


def _page(text, head=""):
    """A page long enough to count as readable, carrying ``text``."""
    filler = "Regional power market commentary and background. " * 8
    return (
        f"<html><head>{head}</head><body><article><p>{filler}</p>"
        f"<p>{text}</p></article></body></html>"
    )


class TestNextUrl:
    def test_a_link_header_rel_next(self):
        resp = _Resp(
            _page("x"),
            headers={"Link": '<https://example.org/archive/?page=2>; rel="next"'},
        )
        assert pagination.next_url(resp, _BASE) == "https://example.org/archive/?page=2"

    def test_a_relative_link_header_is_resolved(self):
        resp = _Resp(_page("x"), headers={"Link": '</archive/?page=2>; rel="next"'})
        assert pagination.next_url(resp, _BASE) == "https://example.org/archive/?page=2"

    def test_a_link_element_rel_next(self):
        resp = _Resp(_page("x", head='<link rel="next" href="/archive/2.html">'))
        assert pagination.next_url(resp, _BASE) == "https://example.org/archive/2.html"

    def test_an_anchor_rel_next(self):
        resp = _Resp(_page('<a rel="next" href="/archive/2.html">Next</a>'))
        assert pagination.next_url(resp, _BASE) == "https://example.org/archive/2.html"

    def test_the_header_wins_over_markup(self):
        resp = _Resp(
            _page("x", head='<link rel="next" href="/from-markup">'),
            headers={"Link": '</from-header>; rel="next"'},
        )
        assert pagination.next_url(resp, _BASE) == "https://example.org/from-header"

    @pytest.mark.parametrize(
        "current, linked, expected",
        [
            (_BASE, "/archive/page/2/", "https://example.org/archive/page/2/"),
            (
                "https://example.org/archive/page/2/",
                "/archive/page/3/",
                "https://example.org/archive/page/3/",
            ),
            (_BASE, "/archive/?page=2", "https://example.org/archive/?page=2"),
            (
                "https://example.org/archive/?page=2",
                "/archive/?page=3",
                "https://example.org/archive/?page=3",
            ),
            (_BASE, "/archive/?paged=2", "https://example.org/archive/?paged=2"),
        ],
    )
    def test_a_numbered_url_the_page_links_to(self, current, linked, expected):
        resp = _Resp(
            _page(f'<a class="page-numbers" href="{linked}">next</a>'), url=current
        )
        assert pagination.next_url(resp, current) == expected

    def test_a_numbered_url_is_never_guessed(self):
        """No link to it, so there is nothing to follow: a single article would
        404 on a made-up /page/2/."""
        assert pagination.next_url(_Resp(_page("a plain article")), _BASE) is None

    def test_a_link_to_the_wrong_page_number_is_not_the_next_page(self):
        resp = _Resp(_page('<a href="/archive/page/5/">five</a>'))
        assert pagination.next_url(resp, _BASE) is None

    def test_another_host_is_not_followed(self):
        resp = _Resp(_page('<a rel="next" href="https://elsewhere.example/2">n</a>'))
        assert pagination.next_url(resp, _BASE) is None

    def test_www_and_bare_host_are_the_same_site(self):
        resp = _Resp(
            _page('<a rel="next" href="https://www.example.org/archive/2">n</a>')
        )
        assert pagination.next_url(resp, _BASE) == "https://www.example.org/archive/2"

    def test_a_non_http_scheme_is_not_followed(self):
        resp = _Resp(_page('<a rel="next" href="javascript:void(0)">n</a>'))
        assert pagination.next_url(resp, _BASE) is None

    def test_a_response_that_is_not_text_has_no_next_page(self):
        assert (
            pagination.next_url(
                _Resp(b"%PDF-1.4", content_type="application/pdf"), _BASE
            )
            is None
        )

    def test_a_mock_response_is_not_a_crash(self):
        """The resolver's own tests hand it MagicMocks all over."""
        assert pagination.next_url(MagicMock(), _BASE) is None
        assert pagination.collection_totals(MagicMock()) is None


class TestCollectionTotals:
    def test_the_wordpress_rest_headers(self):
        resp = _Resp(
            "[]",
            headers={"X-WP-Total": "52", "X-WP-TotalPages": "6"},
            content_type="application/json",
        )
        assert pagination.collection_totals(resp) == {"total": 52, "total_pages": 6}

    def test_a_page_without_them_has_no_totals(self):
        assert pagination.collection_totals(_Resp(_page("x"))) is None

    def test_a_non_numeric_header_is_ignored(self):
        resp = _Resp("[]", headers={"X-WP-Total": "many", "X-WP-TotalPages": "6"})
        assert pagination.collection_totals(resp) is None

    def test_the_text_states_the_totals_where_a_verifier_will_read_it(self):
        line = pagination.totals_line({"total": 52, "total_pages": 6})
        assert "52" in line and "6" in line and "X-WP-Total" in line


def _site(pages):
    """A fetcher over ``{url: response}`` that records what it was asked."""
    calls = []

    def fetch(url, timeout=15):
        calls.append(url)
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value

    fetch.calls = calls
    return fetch


def _extract(resp, url):
    return resolver._extract_fetched(resp, url)


class TestFollow:
    def _chain(self, n, body=lambda i: f"Items on page {i}."):
        """``n`` pages linked by rel=next, page 1 being ``_BASE``."""
        urls = [_BASE] + [f"{_BASE}?page={i}" for i in range(2, n + 1)]
        pages = {}
        for i, url in enumerate(urls, start=1):
            nxt = f'<link rel="next" href="{urls[i]}">' if i < n else ""
            pages[url] = _Resp(_page(body(i), head=nxt), url=url)
        return urls, pages

    def test_the_text_of_the_following_pages_is_appended(self):
        urls, pages = self._chain(3)
        fetch = _site(pages)
        first_text, _kind = _extract(pages[_BASE], _BASE)
        out = pagination.follow(
            pages[_BASE], _BASE, first_text, fetch, _extract, timeout=5
        )
        assert fetch.calls == urls[1:]
        assert out.pages_read == 3
        assert "Items on page 2." in out.text and "Items on page 3." in out.text
        assert out.stopped is None

    def test_the_cap_bounds_the_pages_fetched(self):
        urls, pages = self._chain(20)
        fetch = _site(pages)
        first_text, _ = _extract(pages[_BASE], _BASE)
        out = pagination.follow(
            pages[_BASE], _BASE, first_text, fetch, _extract, timeout=5
        )
        assert out.pages_read == pagination.MAX_PAGES
        assert len(fetch.calls) == pagination.MAX_PAGES - 1
        assert out.stopped == "page cap"

    def test_a_loop_is_not_followed_forever(self):
        looped = _Resp(_page("a", head=f'<link rel="next" href="{_BASE}">'), url=_BASE)
        fetch = _site({_BASE: looped})
        first_text, _ = _extract(looped, _BASE)
        out = pagination.follow(looped, _BASE, first_text, fetch, _extract, timeout=5)
        assert fetch.calls == []
        assert out.pages_read == 1

    def test_a_page_that_fails_stops_the_walk_and_keeps_what_was_read(self):
        urls, pages = self._chain(4)
        pages[urls[2]] = requests.exceptions.Timeout("slow")
        fetch = _site(pages)
        first_text, _ = _extract(pages[_BASE], _BASE)
        out = pagination.follow(
            pages[_BASE], _BASE, first_text, fetch, _extract, timeout=5
        )
        assert out.pages_read == 2
        assert "Items on page 2." in out.text
        assert out.stopped.startswith("fetch failed")

    def test_an_unreadable_following_page_adds_nothing(self):
        urls, pages = self._chain(2)
        pages[urls[1]] = _Resp(
            "<html><body><div id='root'></div></body></html>", url=urls[1]
        )
        fetch = _site(pages)
        first_text, _ = _extract(pages[_BASE], _BASE)
        out = pagination.follow(
            pages[_BASE], _BASE, first_text, fetch, _extract, timeout=5
        )
        assert out.text == ""
        assert out.pages_read == 1

    def test_a_single_page_costs_nothing_extra(self):
        only = _Resp(_page("just one page"))
        fetch = _site({})
        first_text, _ = _extract(only, _BASE)
        out = pagination.follow(only, _BASE, first_text, fetch, _extract, timeout=5)
        assert fetch.calls == [] and out.pages_read == 1 and out.text == ""

    def test_wordpress_totals_are_stated_even_with_no_next_page(self):
        resp = _Resp(
            "[]" + " " * 300,
            headers={"X-WP-Total": "52", "X-WP-TotalPages": "6"},
            content_type="application/json",
        )
        out = pagination.follow(resp, _BASE, "x", _site({}), _extract, timeout=5)
        assert out.totals == {"total": 52, "total_pages": 6}
        assert "X-WP-Total: 52" in out.text

    def test_the_totals_line_comes_before_any_page_text(self):
        urls, pages = self._chain(2)
        pages[_BASE].headers.update({"X-WP-Total": "9", "X-WP-TotalPages": "2"})
        first_text, _ = _extract(pages[_BASE], _BASE)
        out = pagination.follow(
            pages[_BASE], _BASE, first_text, _site(pages), _extract, timeout=5
        )
        assert out.text.index("X-WP-Total") < out.text.index("Items on page 2.")


_CLAIM = "The archive lists a post about substations."


class TestTheResolverUsesIt:
    """End to end through ``_resolve_known_url`` with only the network faked."""

    def _resolve(self, pages, verdict="supports", quote=None):
        seen = {}

        def fake_verify(claim, content, api_keys, author=None):
            seen["content"] = content
            return {
                "checked": True,
                "verdict": verdict,
                "reason": "r",
                "quote": quote or "",
            }, None

        def fake_get(url, timeout=15, **_kw):
            return (
                pages[url]
                if not isinstance(pages[url], Exception)
                else (_ for _ in ()).throw(pages[url])
            )

        with (
            patch.object(resolver, "safe_get", side_effect=fake_get),
            patch.object(resolver, "_verify_relevance", side_effect=fake_verify),
            patch.object(resolver.wayback, "check", return_value={"archived": False}),
        ):
            result = resolver._resolve_known_url(
                _CLAIM, _BASE, {"mistral": {"api_key": "k"}}
            )
        return result, seen.get("content", "")

    def _two_pages(self):
        return {
            _BASE: _Resp(
                _page(
                    "Page one lists local news.",
                    head='<link rel="next" href="/archive/?page=2">',
                ),
            ),
            "https://example.org/archive/?page=2": _Resp(
                _page("A post about substations is listed here."),
                url="https://example.org/archive/?page=2",
            ),
        }

    def test_the_verifier_is_shown_the_second_page(self):
        result, content = self._resolve(self._two_pages())
        assert "A post about substations is listed here." in content
        assert result["pages_read"] == 2

    def test_the_checksum_still_covers_page_one_only(self):
        """Drift detection compares this against every earlier run's checksum of
        the same URL. Hashing the extra pages too would report every paginated
        source as changed."""
        pages = self._two_pages()
        result, _ = self._resolve(pages)
        first, _kind = _extract(pages[_BASE], _BASE)
        assert result["checksum"] == resolver.sha256_checksum(first)

    def test_a_single_page_source_is_unchanged(self):
        result, content = self._resolve({_BASE: _Resp(_page("Just the one page."))})
        assert "pages_read" not in result and "pagination" not in result
        assert "Just the one page." in content

    def test_what_stopped_the_walk_is_recorded(self):
        pages = self._two_pages()
        pages["https://example.org/archive/?page=2"] = requests.exceptions.Timeout(
            "slow"
        )
        result, _ = self._resolve(pages)
        assert result["pages_read"] == 1
        assert result["pagination"]["stopped"].startswith("fetch failed")

    def test_wordpress_totals_reach_the_verifier_and_the_result(self):
        pages = {
            _BASE: _Resp(
                _page("A listing."),
                headers={"X-WP-Total": "52", "X-WP-TotalPages": "6"},
            )
        }
        result, content = self._resolve(pages)
        assert "X-WP-Total: 52" in content
        assert result["pagination"]["totals"] == {"total": 52, "total_pages": 6}

    def _long_chain(self, n):
        urls = [_BASE] + [f"{_BASE}?page={i}" for i in range(2, n + 1)]
        return {
            url: _Resp(
                _page(
                    f"Items on page {i}.",
                    head=(f'<link rel="next" href="{urls[i]}">' if i < n else ""),
                ),
                url=url,
            )
            for i, url in enumerate(urls, start=1)
        }

    def test_a_refutation_from_a_cut_off_walk_says_the_claim_may_sit_further_on(self):
        result, _ = self._resolve(self._long_chain(9), verdict="not_addressed")
        assert result["verification"] == "content_mismatch"
        assert result["pagination"]["stopped"] == "page cap"
        assert f"first {pagination.MAX_PAGES} pages" in result["note"]

    def test_a_refutation_from_a_complete_walk_does_not_say_that(self):
        result, _ = self._resolve(self._two_pages(), verdict="not_addressed")
        assert result["verification"] == "content_mismatch"
        assert "may sit further on" not in result["note"]

    def test_a_bot_wall_is_not_walked(self):
        wall = _Resp(
            "<html><head><link rel='next' href='/archive/?page=2'></head><body>"
            "<p>Please verify you are a human to continue.</p></body></html>"
        )
        fetched = []

        def fake_get(url, timeout=15, **_kw):
            fetched.append(url)
            return wall

        with (
            patch.object(resolver, "safe_get", side_effect=fake_get),
            patch.object(resolver.wayback, "check", return_value={"archived": False}),
        ):
            result = resolver._resolve_known_url(_CLAIM, _BASE, {})
        assert result["verification"] == "unverifiable"
        assert fetched == [_BASE]
