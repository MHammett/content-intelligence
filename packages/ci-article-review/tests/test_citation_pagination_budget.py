"""A WordPress REST collection cannot cost the resolver unbounded bytes (#357).

``pagination.follow`` walks ``rel=next`` for up to ``MAX_PAGES`` pages. A REST
collection (``/wp-json/wp/v2/posts?per_page=3``) is JSON carrying full post
bodies, which ``extract_response_text`` hands on unstripped, so a four page walk
was about 240,000 characters fetched and held, against a verifier that windows
the lot to 20,000. HTML is reduced to its article text before it is held, so it
is not what this budget is for and must read exactly as it did.

Every test here runs on fake responses; the suite blocks sockets.
"""

import json
from unittest.mock import patch

import pytest

from ci_article_review.adapters.citation import pagination, resolver

_BASE = "https://example.org/wp-json/wp/v2/posts?per_page=3"
_HTML_BASE = "https://example.org/archive/"


class _Resp:
    """The slice of ``requests.Response`` the resolver and pagination read."""

    def __init__(self, body, url, headers=None, content_type="application/json"):
        self.content = body.encode("utf-8")
        self.url = url
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status_code = 200

    def raise_for_status(self):
        pass


def _json_page(marker, chars):
    """A REST-shaped body of about ``chars`` characters naming ``marker``."""
    post = {
        "id": 1,
        "title": {"rendered": marker},
        "content": {"rendered": "x " * (chars // 2)},
    }
    return json.dumps([post])


def _rest_chain(n, chars, total=None):
    """``n`` REST pages linked by the ``Link`` header, page 1 being ``_BASE``."""
    urls = [_BASE] + [f"{_BASE}&page={i}" for i in range(2, n + 1)]
    pages = {}
    for i, url in enumerate(urls, start=1):
        headers = {"X-WP-Total": str(total or n * 3), "X-WP-TotalPages": str(n)}
        if i < n:
            headers["Link"] = f'<{urls[i]}>; rel="next"'
        pages[url] = _Resp(_json_page(f"Post on page {i}", chars), url, headers)
    return urls, pages


def _fetcher(pages):
    calls = []

    def fetch(url, timeout=15):
        calls.append(url)
        return pages[url]

    fetch.calls = calls
    return fetch


def _extract(resp, url):
    return resolver._extract_fetched(resp, url)


def _follow(pages, first=_BASE):
    fetch = _fetcher(pages)
    first_text, _kind = _extract(pages[first], first)
    out = pagination.follow(pages[first], first, first_text, fetch, _extract, timeout=5)
    return out, fetch


# Read from the module so the tests follow the constant.
_BUDGET = pagination.MAX_FOLLOWED_CHARS


class TestTheBudgetBoundsAWalk:
    def test_a_collection_of_fat_pages_stops_when_the_budget_is_spent(self):
        _urls, pages = _rest_chain(5, chars=_BUDGET // 2 + 1000)
        out, fetch = _follow(pages)
        # Two followed pages spend the budget (the second is cut), so the third,
        # fourth and fifth are never fetched.
        assert len(fetch.calls) == 2
        assert out.stopped == "byte budget"

    def test_what_is_held_is_bounded_by_the_budget_not_the_page_count(self):
        _urls, pages = _rest_chain(5, chars=_BUDGET // 2 + 1000)
        out, _fetch = _follow(pages)
        followed_text = out.text.split("\n\n", 1)[1]  # drop the totals line
        # Budget plus what labels the pages and marks the cut, nothing proportional
        # to the pages that were skipped.
        assert len(followed_text) < _BUDGET + 1000

    def test_the_page_that_crosses_the_budget_is_cut_and_says_so(self):
        _urls, pages = _rest_chain(3, chars=_BUDGET + 5000)
        out, fetch = _follow(pages)
        assert len(fetch.calls) == 1
        assert out.pages_read == 2
        assert "Post on page 2" in out.text
        assert "truncated" in out.text.lower()
        assert out.stopped == "byte budget"

    def test_the_totals_line_is_not_charged_to_the_budget(self):
        # A followed page one character under the budget fits only if the totals
        # line, which is stated in any case, costs it nothing.
        overhead = len(_json_page("Post on page 2", 0))
        _urls, pages = _rest_chain(2, chars=_BUDGET - 1 - overhead)
        assert len(_extract(pages[_BASE + "&page=2"], _BASE)[0]) == _BUDGET - 1
        out, _fetch = _follow(pages)
        assert out.text.startswith("[Collection totals")
        assert "truncated" not in out.text.lower()
        assert out.stopped is None

    def test_a_page_that_spends_the_budget_exactly_ends_the_walk(self):
        _urls, pages = _rest_chain(4, chars=10)
        pages[_BASE + "&page=2"] = _Resp(
            "x" * _BUDGET,
            _BASE + "&page=2",
            {"Link": f'<{_BASE}&page=3>; rel="next"'},
            content_type="text/plain",
        )
        out, fetch = _follow(pages)
        assert fetch.calls == [_BASE + "&page=2"]
        assert out.stopped == "byte budget"
        assert "truncated" not in out.text.lower()

    def test_a_cut_on_the_last_page_the_cap_allows_is_still_a_budget_stop(self):
        urls, pages = _rest_chain(pagination.MAX_PAGES + 1, chars=10)
        last = urls[pagination.MAX_PAGES - 1]
        pages[last] = _Resp(
            _json_page("Post on the last page", _BUDGET + 5000),
            last,
            pages[last].headers,
        )
        out, _fetch = _follow(pages)
        assert out.pages_read == pagination.MAX_PAGES
        assert out.stopped == "byte budget"

    def test_pages_that_fit_are_read_in_full(self):
        _urls, pages = _rest_chain(4, chars=_BUDGET // 10)
        out, fetch = _follow(pages)
        assert out.pages_read == 4 and len(fetch.calls) == 3
        assert out.stopped is None
        for i in (2, 3, 4):
            assert f"Post on page {i}" in out.text

    def test_a_last_page_inside_the_budget_is_not_a_stop(self):
        """Nothing was left unread, so there is nothing to warn about."""
        _urls, pages = _rest_chain(3, chars=_BUDGET // 2 - 200)
        pages[_BASE + "&page=2"].headers.pop("Link")
        out, fetch = _follow(pages)
        assert len(fetch.calls) == 1 and out.stopped is None


class TestHtmlIsNotChargedToTheBudget:
    """HTML is held as extracted article text. Reading a long paginated article
    must come out exactly as before the budget existed."""

    def _html_chain(self, n, words):
        urls = [_HTML_BASE] + [f"{_HTML_BASE}?page={i}" for i in range(2, n + 1)]
        pages = {}
        for i, url in enumerate(urls, start=1):
            nxt = f'<link rel="next" href="{urls[i]}">' if i < n else ""
            body = " ".join(f"Sentence {i}-{j} about the grid." for j in range(words))
            pages[url] = _Resp(
                f"<html><head>{nxt}</head><body><article><p>{body}</p></article>"
                "</body></html>",
                url,
                content_type="text/html",
            )
        return urls, pages

    def test_pages_of_html_each_longer_than_the_budget_are_all_read(self):
        urls, pages = self._html_chain(5, words=3000)
        out, fetch = _follow(pages, first=_HTML_BASE)
        page_two, _ = _extract(pages[urls[1]], urls[1])
        assert len(page_two) > _BUDGET, "the fixture must exceed the budget"
        assert out.pages_read == pagination.MAX_PAGES
        assert out.stopped == "page cap" or out.stopped is None
        assert fetch.calls == urls[1:]
        for i in (2, 3, 4, 5):
            assert f"Sentence {i}-2999" in out.text, "no HTML page may be cut"
        assert "truncated" not in out.text.lower()


_CLAIM = "The site has 12 posts."


class TestTheResolverUsesIt:
    def _resolve(self, pages, verdict="supports"):
        seen = {}

        def fake_verify(claim, content, api_keys, author=None):
            seen["content"] = content
            return {
                "checked": True,
                "verdict": verdict,
                "reason": "r",
                "quote": "",
            }, None

        with (
            patch.object(
                resolver, "safe_get", side_effect=lambda url, **_k: pages[url]
            ),
            patch.object(resolver, "_verify_relevance", side_effect=fake_verify),
            patch.object(resolver.wayback, "check", return_value={"archived": False}),
        ):
            result = resolver._resolve_known_url(
                _CLAIM, _BASE, {"mistral": {"api_key": "k"}}
            )
        return result, seen.get("content", "")

    def test_the_verifier_candidate_is_bounded(self):
        _urls, pages = _rest_chain(5, chars=_BUDGET // 2 + 1000)
        first, _ = _extract(pages[_BASE], _BASE)
        _result, content = self._resolve(pages)
        assert len(content) < len(first) + _BUDGET + 2000

    def test_the_checksum_still_covers_page_one_only(self):
        """Drift compares this against every earlier run's checksum of the URL,
        and a budget that moved it would flag every collection as changed."""
        _urls, pages = _rest_chain(5, chars=_BUDGET // 2 + 1000)
        first, _ = _extract(pages[_BASE], _BASE)
        result, _content = self._resolve(pages)
        assert result["checksum"] == resolver.sha256_checksum(first)

    def test_the_stop_is_recorded(self):
        _urls, pages = _rest_chain(5, chars=_BUDGET // 2 + 1000)
        result, _content = self._resolve(pages)
        assert result["pagination"]["stopped"] == "byte budget"
        assert result["pagination"]["totals"]["total"] == 15

    def test_a_negative_verdict_from_a_cut_off_collection_is_a_partial_read(self):
        """It used to stay a ``content_mismatch`` with a caveat appended to the
        note. A reader skimming the mismatch block does not see caveats, and a
        reading limit was being counted as a refutation (issue #395). Now the
        cut moves the entry to its own tier, and the note leads with it."""
        _urls, pages = _rest_chain(5, chars=_BUDGET // 2 + 1000)
        result, _content = self._resolve(pages, verdict="not_addressed")
        assert result["verification"] == "partial_read"
        assert result["note"].startswith("Read in part only.")
        assert "size budget stopped it" in result["note"]
        assert "NOT evidence against the claim" in result["note"]
        assert "byte_budget" in result["read_coverage"]["limits"]

    def test_a_refutation_from_a_whole_collection_does_not_say_that(self):
        _urls, pages = _rest_chain(3, chars=_BUDGET // 10)
        result, _content = self._resolve(pages, verdict="not_addressed")
        assert result["verification"] == "content_mismatch"
        assert "may sit further on" not in result["note"]


@pytest.mark.parametrize("n", [1])
def test_a_single_rest_page_costs_nothing_extra(n):
    _urls, pages = _rest_chain(n, chars=_BUDGET // 10)
    out, fetch = _follow(pages)
    assert fetch.calls == [] and out.pages_read == 1 and out.stopped is None
