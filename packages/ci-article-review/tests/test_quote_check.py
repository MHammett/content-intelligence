"""A fact-check verdict's quote has to be on the page it cites (issue #344).

PR #335 made a verdict carry an openable URL and a non-placeholder
``supporting_quote``, and never opened the page: a model could attach a real URL
and an invented sentence and keep its verdict. ``adapters/citation/quote_check``
fetches each cited page once and says whether the quote is on it;
``consolidation`` demotes a verdict whose quote is not, the way it demotes one
with no quote at all.

The asymmetry that shapes every test below: **only a page that was read and did
not contain the quote demotes.** A fetch that failed, a bot wall, a near-empty
body, a PDF cut off by the page cap all leave the verdict where it was, because
nothing was established about the model's quote either way.
"""

import pytest

from ci_article_review import consolidation
from ci_article_review.adapters.citation import quote_check as qc

_URL = "https://www.example.org/report"
_WRAPPER = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
_QUOTE = "The plant operates nine generating units across two sites."
#: What ``_impersonation_fallback_content`` reports for a page read whole.
_WHOLE = {"kind": "html", "pages_total": None, "pages_read": None, "partial": False}
_PAGE = (
    "Annual report. The plant operates nine generating units across two sites, "
    "and a tenth is under construction. " * 4
)


def _page(*texts, partial=False):
    return qc.PageRead(status="read", texts=list(texts), partial=partial)


def _fetch_from(pages):
    """A fetcher that never touches the network and counts what it was asked."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value

    fetch.calls = calls
    return fetch


def _verdict(bucket="confirmed", url=_URL, quote=_QUOTE, **extra):
    return {
        bucket: [
            {
                "claim": "The plant has nine units.",
                "source": "Annual report",
                "source_url": url,
                "supporting_quote": quote,
                **extra,
            }
        ]
    }


def _results(data, model="gemini"):
    return {
        (model, "fact_check"): {
            "failed": False,
            "data": data,
            "model": model,
            "tokens": {},
        }
    }


class TestQuoteFound:
    """What counts as "the quote is on the page"."""

    def test_verbatim(self):
        assert qc.quote_found(_QUOTE, [_PAGE]) is True

    def test_an_invented_sentence_is_not_found(self):
        invented = "The plant operates eleven generating units across two sites."
        assert qc.quote_found(invented, [_PAGE]) is False

    @pytest.mark.parametrize(
        "page",
        [
            "THE PLANT  OPERATES\nNINE GENERATING UNITS\tACROSS TWO SITES.",
            "The plant operates \u201cnine\u201d generating units across two sites",
            "The plant\u00a0operates nine generating units\u2014across two sites.",
            "The **plant** operates nine | generating units | across two sites.",
        ],
    )
    def test_case_whitespace_quote_marks_and_markup_do_not_matter(self, page):
        """Extraction noise, not a different sentence. A false "not found" is
        the costly error here, because it demotes a verdict that was right."""
        assert qc.quote_found(_QUOTE, [page]) is True

    def test_a_quote_mark_in_the_quote_but_not_the_page(self):
        assert qc.quote_found(f"\u201c{_QUOTE}\u201d", [_PAGE]) is True

    def test_a_pdf_hyphenation_break_does_not_matter(self):
        assert (
            qc.quote_found(
                "The plant operates nine generat-\ning units across two sites.",
                [_PAGE],
            )
            is True
        )

    def test_an_ellipsis_joins_two_real_passages(self):
        quote = "The plant operates nine generating units ... and a tenth is under"
        assert qc.quote_found(quote, [_PAGE]) is True

    @pytest.mark.parametrize("joiner", ["...", "\u2026", "[...]", "[\u2026]", ". . ."])
    def test_every_ellipsis_spelling(self, joiner):
        quote = f"The plant operates nine generating units {joiner} a tenth is under construction."
        assert qc.quote_found(quote, [_PAGE]) is True

    def test_a_line_break_marks_a_gap_the_page_may_fill(self):
        """Measured 2026-10-06: two real paragraphs of an about page quoted on
        separate lines, the text between them left out. Not an invented quote."""
        quote = (
            "The plant operates nine generating units across two sites,\n\n"
            "and a tenth is under construction."
        )
        page = (
            "The plant operates nine generating units across two sites, which "
            "were built in stages. Records of that are kept elsewhere. "
            "Separately, and a tenth is under construction."
        )
        assert qc.quote_found(quote, [page]) is True

    def test_every_line_must_still_be_on_the_page(self):
        quote = f"{_QUOTE}\nAnd a twelfth is planned for next year."
        assert qc.quote_found(quote, [_PAGE]) is False

    def test_each_piece_around_an_ellipsis_must_be_on_the_page(self):
        quote = "The plant operates nine generating units ... and a twelfth is planned"
        assert qc.quote_found(quote, [_PAGE]) is False

    def test_any_one_view_of_the_page_is_enough(self):
        """The extracted article text drops tables and sidebars; the raw view
        keeps them."""
        extracted = "Unrelated article body. " * 20
        assert qc.quote_found(_QUOTE, [extracted, _PAGE]) is True

    def test_nothing_to_compare_is_not_a_miss(self):
        assert qc.quote_found("...", [_PAGE]) is None
        assert qc.quote_found("", [_PAGE]) is None


class _Resp:
    """The slice of ``requests.Response`` the fetch reads."""

    def __init__(self, body, content_type="text/html; charset=utf-8", status=200):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.status_code = status
        self.url = _URL

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}", response=self)


_NAV_TABLE_PAGE = (
    "<html><head><title>t</title><script>var x = 'The plant is closed.';</script>"
    "</head><body><nav>Home</nav><article><p>"
    + "Some unrelated article prose about regional power markets. "
    * 8
    + "</p></article><aside><table><tr><td>Nine generating units</td>"
    "<td>2 sites</td></tr></table></aside></body></html>"
)


class TestFetchPage:
    """The fetch itself, with ``safe_get`` stubbed (the suite blocks sockets)."""

    def _patch(self, monkeypatch, resp=None, exc=None):
        def fake(url, timeout=15, **_kw):
            if exc is not None:
                raise exc
            return resp

        monkeypatch.setattr(qc, "safe_get", fake)

    def test_text_outside_the_article_is_on_the_page(self, monkeypatch):
        """trafilatura keeps the article and drops the sidebar table; the quote
        is on the page all the same."""
        self._patch(monkeypatch, _Resp(_NAV_TABLE_PAGE))
        page = qc.fetch_page(_URL)
        assert page.status == "read"
        assert qc.quote_found("Nine generating units 2 sites", page.texts) is True

    def test_script_text_is_not_on_the_page(self, monkeypatch):
        self._patch(monkeypatch, _Resp(_NAV_TABLE_PAGE))
        page = qc.fetch_page(_URL)
        assert qc.quote_found("The plant is closed.", page.texts) is False

    def test_an_http_error_is_a_failed_read(self, monkeypatch):
        self._patch(monkeypatch, _Resp("gone", status=404))
        assert qc.fetch_page(_URL).status == "failed"

    def test_a_near_empty_page_is_too_short_to_hold_against_a_quote(self, monkeypatch):
        self._patch(
            monkeypatch,
            _Resp(
                "<html><body><div id='root'></div><script>app()</script></body></html>"
            ),
        )
        assert qc.fetch_page(_URL).status == "too_short"

    def test_a_bot_wall_served_as_200_is_not_the_page(self, monkeypatch):
        self._patch(
            monkeypatch,
            _Resp(
                "<html><body><p>Please verify you are a human to continue.</p></body></html>"
            ),
        )
        assert qc.fetch_page(_URL).status == "access_wall"

    def test_a_non_public_address_is_never_fetched(self, monkeypatch):
        from ci_core.http import UnsafeURLError

        self._patch(monkeypatch, exc=UnsafeURLError("private"))
        assert qc.fetch_page("http://10.0.0.1/x").status == "unsafe"

    def test_a_refusal_is_retried_with_a_browser_fingerprint(self, monkeypatch):
        import requests

        self._patch(
            monkeypatch,
            exc=requests.HTTPError("403", response=_Resp("no", status=403)),
        )
        monkeypatch.setattr(
            qc.resolver,
            "_impersonation_fallback_content",
            lambda url, timeout: (url, _PAGE, "html", _WHOLE),
        )
        page = qc.fetch_page(_URL)
        assert page.status == "read"
        assert qc.quote_found(_QUOTE, page.texts) is True

    def test_where_a_forwarder_landed_is_recorded(self, monkeypatch):
        """``safe_get`` follows a grounding wrapper; the landing page is the source."""
        resp = _Resp(_PAGE)
        resp.url = _URL
        self._patch(monkeypatch, resp)
        page = qc.fetch_page(_WRAPPER + "AUZ1")
        assert page.status == "read"
        assert page.final_url == _URL

    def test_where_a_refused_forwarder_landed_is_still_recorded(self, monkeypatch):
        """A publisher that refuses us still told us which page the wrapper named."""
        import requests

        refused = _Resp("no", status=403)
        refused.url = _URL
        self._patch(monkeypatch, exc=requests.HTTPError("403", response=refused))
        monkeypatch.setattr(
            qc.resolver, "_impersonation_fallback_content", lambda url, timeout: None
        )
        page = qc.fetch_page(_WRAPPER + "AUZ1")
        assert page.status == "failed"
        assert page.final_url == _URL

    def test_a_fingerprinted_retry_records_where_it_landed(self, monkeypatch):
        import requests

        self._patch(
            monkeypatch,
            exc=requests.HTTPError("403", response=_Resp("no", status=403)),
        )
        monkeypatch.setattr(
            qc.resolver,
            "_impersonation_fallback_content",
            lambda url, timeout: (_URL + "/landed", _PAGE, "html", _WHOLE),
        )
        assert qc.fetch_page(_WRAPPER + "AUZ1").final_url == _URL + "/landed"

    def test_an_exception_with_no_response_has_no_landing_page(self, monkeypatch):
        self._patch(monkeypatch, exc=TimeoutError("slow"))
        assert qc.fetch_page(_WRAPPER + "AUZ1").final_url == ""


class TestCheckQuotes:
    def test_a_quote_on_the_page_is_found(self):
        checks, summary = qc.check_quotes(
            _results(_verdict()), fetch=_fetch_from({_URL: _page(_PAGE)})
        )
        assert checks[(_URL, _QUOTE)]["found"] is True
        assert summary["found"] == 1 and summary["not_found"] == 0

    def test_an_invented_quote_on_a_real_page_is_not_found(self):
        invented = "The plant operates eleven generating units across two sites."
        checks, summary = qc.check_quotes(
            _results(_verdict(quote=invented)),
            fetch=_fetch_from({_URL: _page(_PAGE)}),
        )
        assert checks[(_URL, invented)]["found"] is False
        assert summary["not_found"] == 1

    def test_all_three_verdict_buckets_are_checked(self):
        data = {}
        for bucket in ("confirmed", "outdated", "contradicted"):
            data.update(_verdict(bucket, quote=f"{_QUOTE} {bucket}"))
        checks, _ = qc.check_quotes(
            _results(data), fetch=_fetch_from({_URL: _page(_PAGE)})
        )
        assert len(checks) == 3

    def test_buckets_that_assert_no_verdict_are_not_fetched(self):
        fetch = _fetch_from({})
        checks, _ = qc.check_quotes(
            _results({"unverifiable": [{"claim": "c", "source_url": _URL}]}),
            fetch=fetch,
        )
        assert checks == {} and fetch.calls == []

    def test_a_verdict_without_a_url_or_quote_is_left_to_the_demotion_rules(self):
        fetch = _fetch_from({})
        data = {
            "confirmed": [
                {
                    "claim": "a",
                    "source": "x",
                    "source_url": "N/A",
                    "supporting_quote": _QUOTE,
                },
                {
                    "claim": "b",
                    "source": "x",
                    "source_url": _URL,
                    "supporting_quote": "N/A",
                },
            ]
        }
        checks, _ = qc.check_quotes(_results(data), fetch=fetch)
        assert checks == {} and fetch.calls == []

    def test_one_page_is_fetched_once_however_many_models_cite_it(self):
        results = {}
        for model in ("gemini", "openai", "grok"):
            results.update(_results(_verdict(), model=model))
        fetch = _fetch_from({_URL: _page(_PAGE)})
        checks, summary = qc.check_quotes(results, fetch=fetch)
        assert fetch.calls == [_URL]
        assert len(checks) == 1 and summary["urls"] == 1

    def test_a_url_in_the_source_text_is_checked_like_source_url(self):
        data = {
            "confirmed": [
                {
                    "claim": "c",
                    "source": f"Annual report, {_URL}",
                    "supporting_quote": _QUOTE,
                }
            ]
        }
        checks, _ = qc.check_quotes(
            _results(data), fetch=_fetch_from({_URL: _page(_PAGE)})
        )
        assert checks[(_URL, _QUOTE)]["found"] is True

    @pytest.mark.parametrize(
        "read",
        [
            qc.PageRead(status="failed", detail="HTTP 404"),
            qc.PageRead(status="failed", detail="HTTP 403"),
            qc.PageRead(status="too_short", texts=["Enable cookies."]),
            qc.PageRead(status="access_wall", texts=["Verify you are a human."]),
            qc.PageRead(
                status="read",
                texts=["A page about something else. " * 40],
                partial=True,
            ),
        ],
        ids=["404", "403", "near-empty", "bot-wall", "pdf-past-the-page-cap"],
    )
    def test_a_page_that_was_not_fully_read_never_counts_against_the_quote(self, read):
        """The positive control for the whole feature: every one of these is a
        page on which the quote is NOT found, and none may demote."""
        checks, summary = qc.check_quotes(
            _results(_verdict()), fetch=_fetch_from({_URL: read})
        )
        assert checks[(_URL, _QUOTE)]["found"] is None
        assert summary["unchecked"] == 1 and summary["not_found"] == 0

    def test_a_fetch_that_raises_is_unchecked_not_a_miss(self):
        checks, summary = qc.check_quotes(
            _results(_verdict()),
            fetch=_fetch_from({_URL: RuntimeError("boom")}),
        )
        assert checks[(_URL, _QUOTE)]["found"] is None
        assert summary["unchecked"] == 1

    def test_a_partial_read_that_does_contain_the_quote_is_still_found(self):
        checks, _ = qc.check_quotes(
            _results(_verdict()), fetch=_fetch_from({_URL: _page(_PAGE, partial=True)})
        )
        assert checks[(_URL, _QUOTE)]["found"] is True

    def test_malformed_buckets_are_skipped_not_raised(self):
        results = _results({"confirmed": "not a list", "outdated": ["not a dict"]})
        results[("openai", "fact_check")] = {"failed": True, "data": None}
        checks, _ = qc.check_quotes(results, fetch=_fetch_from({}))
        assert checks == {}

    def test_the_url_limit_leaves_the_rest_unchecked(self, monkeypatch):
        monkeypatch.setattr(qc, "_MAX_URLS", 1)
        data = {
            "confirmed": [
                {
                    "claim": "a",
                    "source_url": f"https://e.org/{n}",
                    "supporting_quote": _QUOTE,
                }
                for n in range(3)
            ]
        }
        pages = {f"https://e.org/{n}": _page(_PAGE) for n in range(3)}
        checks, summary = qc.check_quotes(_results(data), fetch=_fetch_from(pages))
        assert sorted(
            c["found"] for c in checks.values() if c["found"] is not None
        ) == [True]
        assert summary["unchecked"] == 2

    def test_a_forwarder_records_the_page_it_landed_on(self):
        wrapper = _WRAPPER + "AUZ1"
        checks, _ = qc.check_quotes(
            _results(_verdict(url=wrapper)),
            fetch=_fetch_from(
                {wrapper: qc.PageRead(status="read", texts=[_PAGE], final_url=_URL)}
            ),
        )
        assert checks[(wrapper, _QUOTE)]["final_url"] == _URL

    def test_a_page_that_landed_on_itself_records_nothing(self):
        checks, _ = qc.check_quotes(
            _results(_verdict()),
            fetch=_fetch_from(
                {_URL: qc.PageRead(status="read", texts=[_PAGE], final_url=_URL)}
            ),
        )
        assert "final_url" not in checks[(_URL, _QUOTE)]

    def test_an_expired_forwarder_that_landed_on_another_forwarder_records_nothing(
        self,
    ):
        wrapper = _WRAPPER + "AUZ1"
        checks, _ = qc.check_quotes(
            _results(_verdict(url=wrapper)),
            fetch=_fetch_from(
                {wrapper: qc.PageRead(status="failed", final_url=_WRAPPER + "expired")}
            ),
        )
        assert "final_url" not in checks[(wrapper, _QUOTE)]

    def test_the_landing_page_is_kept_when_the_page_could_not_be_read(self):
        wrapper = _WRAPPER + "AUZ1"
        checks, _ = qc.check_quotes(
            _results(_verdict(url=wrapper)),
            fetch=_fetch_from(
                {wrapper: qc.PageRead(status="failed", detail="403", final_url=_URL)}
            ),
        )
        assert checks[(wrapper, _QUOTE)]["found"] is None
        assert checks[(wrapper, _QUOTE)]["final_url"] == _URL


class TestDemotion:
    """consolidation reads the checks; it never fetches."""

    def _section(self, data, checks):
        results, _ = consolidation._normalise_fact_check_results(
            _results(data), None, checks
        )
        return consolidation._build_fact_check(results, {})

    def test_a_quote_not_on_the_page_demotes_the_verdict(self):
        fc = self._section(
            _verdict(), {(_URL, _QUOTE): {"found": False, "chars": 4000}}
        )
        assert fc["confirmed"] == []
        (moved,) = fc["unverifiable"]
        assert moved["demoted_from"] == "confirmed"
        assert moved["quote_check"] == "not_found"
        assert moved["sources_checked"] == [_URL], (
            "Section 9 should still go and read the page"
        )
        assert _URL in moved["reason"]

    @pytest.mark.parametrize("bucket", ["outdated", "contradicted"])
    def test_the_other_verdict_buckets_demote_the_same_way(self, bucket):
        fc = self._section(
            _verdict(bucket, current_value="ten units", contradiction="ten units"),
            {(_URL, _QUOTE): {"found": False}},
        )
        assert fc[bucket] == []
        (moved,) = fc["unverifiable"]
        assert moved["demoted_from"] == bucket
        assert "ten units" in moved["reason"]

    @pytest.mark.parametrize("found", [True, None])
    def test_a_found_or_unchecked_quote_keeps_the_verdict(self, found):
        fc = self._section(_verdict(), {(_URL, _QUOTE): {"found": found}})
        assert len(fc["confirmed"]) == 1
        assert fc.get("unverifiable", []) == []

    def test_no_checks_at_all_changes_nothing(self):
        """--offline, a disabled check, or a check that crashed."""
        assert len(self._section(_verdict(), None)["confirmed"]) == 1
        assert len(self._section(_verdict(), {})["confirmed"]) == 1

    def test_only_the_pair_that_missed_is_demoted(self):
        data = {
            "confirmed": [
                {
                    "claim": "a",
                    "source": "s",
                    "source_url": _URL,
                    "supporting_quote": _QUOTE,
                },
                {
                    "claim": "b",
                    "source": "s",
                    "source_url": _URL,
                    "supporting_quote": "An invented line.",
                },
            ]
        }
        fc = self._section(data, {(_URL, "An invented line."): {"found": False}})
        assert [c["claim"] for c in fc["confirmed"]] == ["a"]
        assert [u["claim"] for u in fc["unverifiable"]] == ["b"]

    def test_every_reader_sees_the_demotion(self):
        """Section 1 and the contradiction list read the same buckets."""
        results = _results(_verdict())
        results[("openai", "fact_check")] = {
            "failed": False,
            "data": {
                "contradicted": [
                    {
                        "claim": "The plant has nine units.",
                        "source": "s",
                        "source_url": "https://other.org/x",
                        "supporting_quote": "It has ten units.",
                    }
                ]
            },
            "model": "openai",
            "tokens": {},
        }
        checks = {(_URL, _QUOTE): {"found": False}}
        normalised, _ = consolidation._normalise_fact_check_results(
            results, None, checks
        )
        assert consolidation.find_contradictions(normalised) == []

    def test_build_report_threads_the_checks_through(self):
        report = consolidation.build_report(
            article_title="t",
            publication_name="p",
            run_number=1,
            corrected_draft="The plant has nine units.",
            lt_result=None,
            results=_results(_verdict()),
            ensemble_cfg={},
            api_call_log=[],
            quote_checks={(_URL, _QUOTE): {"found": False}},
        )
        assert report["section_2_fact_check"]["confirmed"] == []
        assert len(report["section_2_fact_check"]["unverifiable"]) == 1

    def test_the_callers_results_are_not_mutated(self):
        results = _results(_verdict())
        before = repr(results)
        consolidation._normalise_fact_check_results(
            results, None, {(_URL, _QUOTE): {"found": False}}
        )
        assert repr(results) == before


class TestPageCharsetFollowsTheDocument:
    """Both views of a page are decoded by its declared charset (issue #439).

    The second view, ``all_page_text``, decoded the raw bytes with
    ``resp.encoding`` on its own, so it could disagree with the extracted view
    about what a dash was; now both go through ``extract.response_encoding``.
    """

    _BODY = (
        '<html><head><meta charset="UTF-8"></head><body><nav>Home</nav><article>'
        "<p>Once built, a large data center may employ only 20–50 full-time workers. "
        "Protecting local watersheds is part of the pledge, especially where water stress "
        "is growing, and the design consumes zero water for cooling.</p>"
        "</article></body></html>"
    )

    def test_a_quote_with_a_dash_is_found_on_a_bare_text_html_page(self):
        resp = _Resp(self._BODY, content_type="text/html")
        resp.encoding = "ISO-8859-1"  # what requests reports for that header
        page = qc._read_response(resp, _URL)
        assert page.status == "read"
        assert all("20–50" in t for t in page.texts)
        assert qc.quote_found("employ only 20–50 full-time workers", page.texts) is True


# --- issue #443: table rows, who offered a quote, and sibling-aware demotion --

_ROW_PAGE = (
    "Water use by data center location. "
    "Berkeley County, SC Million gallons 1,200.0 300.0 900.0 5.1 "
    "Council Bluffs, IA Million gallons 1,410.3 400.1 1,010.2 6.7 "
    "The Dalles, OR Million gallons 461.1 120.0 341.1 2.3"
)
_ROW_VERBATIM = "Council Bluffs, IA Million gallons 1,410.3 400.1 1,010.2 6.7"
_ROW_RETYPED = (
    "Council Bluffs, IA ... Water withdrawal: 1,410.3 million gallons; "
    "Water consumption: 1,010.2 million gallons"
)


class TestFiguresMatch:
    """A table row re-typed with its labels counts when its figures are on the page."""

    def test_a_retyped_row_is_found_on_its_figures(self):
        assert qc.quote_match(_ROW_RETYPED, [_ROW_PAGE]) == (True, "figures")
        assert qc.quote_found(_ROW_RETYPED, [_ROW_PAGE]) is True

    def test_a_verbatim_row_is_still_reported_as_verbatim(self):
        assert qc.quote_match(_ROW_VERBATIM, [_ROW_PAGE]) == (True, "verbatim")

    def test_prose_without_figures_has_to_match_as_written(self):
        paraphrase = (
            "A large area of southwest Kansas was in moderate to severe drought"
        )
        page = "D1-D2 drought still exists across a large area of southwest Kansas."
        assert qc.quote_match(paraphrase, [page]) == (False, None)

    def test_figures_out_of_order_do_not_match(self):
        swapped = "Council Bluffs, IA: consumption 1,010.2, withdrawal 1,410.3"
        assert qc.quote_match(swapped, [_ROW_PAGE]) == (False, None)

    def test_figures_too_far_apart_do_not_match(self):
        page = "Council Bluffs 1,410.3 " + "filler text " * 60 + "1,010.2 later"
        quote = "Council Bluffs: 1,410.3 withdrawn and 1,010.2 consumed"
        assert qc.quote_match(quote, [page]) == (False, None)

    def test_one_long_figure_is_enough_but_one_short_one_is_not(self):
        page = "The site withdrew 1,410.3 million gallons; 20 staff work there."
        assert qc.quote_match(
            "Water withdrawal of 1,410.3 million gallons at the site", [page]
        ) == (True, "figures")
        assert qc.quote_match("about 20 full-time employees at the site", [page]) == (
            False,
            None,
        )

    def test_a_figures_match_can_only_turn_a_miss_into_a_hit(self):
        """A quote on the page as written is never downgraded by the figures path."""
        assert qc.quote_match(_QUOTE, [_PAGE]) == (True, "verbatim")

    def test_the_first_figure_is_tried_from_every_occurrence(self):
        page = "Row A: 1,410.3 9,999 " + "x " * 200 + "Row B: 1,410.3 1,010.2"
        assert qc.quote_match("1,410.3 withdrawn, 1,010.2 consumed", [page]) == (
            True,
            "figures",
        )


class TestCheckQuotesRecordsWhoOfferedEachQuote:
    def test_claims_models_and_how_matched_are_on_the_check(self):
        results = _results(_verdict(), model="gemini")
        results[("openai", "fact_check")] = {
            "failed": False,
            "data": {
                "confirmed": [
                    {
                        "claim": "Nine units operate at the plant.",
                        "source": "s",
                        "source_url": _URL,
                        "supporting_quote": _QUOTE,
                    }
                ]
            },
            "model": "openai",
            "tokens": {},
        }
        checks, _ = qc.check_quotes(results, fetch=_fetch_from({_URL: _page(_PAGE)}))
        check = checks[(_URL, _QUOTE)]
        assert check["matched"] == "verbatim"
        assert check["models"] == ["gemini", "openai"]
        assert check["claims"] == [
            "The plant has nine units.",
            "Nine units operate at the plant.",
        ]

    def test_an_unread_page_still_says_who_asked(self):
        checks, _ = qc.check_quotes(
            _results(_verdict()),
            fetch=_fetch_from({_URL: qc.PageRead(status="failed", detail="boom")}),
        )
        assert checks[(_URL, _QUOTE)]["found"] is None
        assert checks[(_URL, _QUOTE)]["models"] == ["gemini"]


class TestSiblingQuoteKeepsTheVerdict:
    """One model paraphrases, another copies: the verdict rests on the copy (#443)."""

    _CLAIM = "Google's thirstiest site, Council Bluffs, withdrew 1.41 billion gallons in 2024"
    _OTHER = (
        "Council Bluffs withdrew 1.41 billion gallons in 2024, Google's thirstiest site"
    )

    def _run(self, checks, model="gemini"):
        data = {
            "confirmed": [
                {
                    "claim": self._CLAIM,
                    "source": "Google report",
                    "source_url": _URL,
                    "supporting_quote": _ROW_RETYPED,
                }
            ]
        }
        results = _results(data, model=model)
        normalised, _ = consolidation._normalise_fact_check_results(
            results, None, checks
        )
        return normalised[(model, "fact_check")]["data"]

    def _checks(self, other_model="openai", other_claim=None, other_url=_URL):
        return {
            (_URL, _ROW_RETYPED): {
                "found": False,
                "models": ["gemini"],
                "claims": [self._CLAIM],
            },
            (other_url, _ROW_VERBATIM): {
                "found": True,
                "matched": "verbatim",
                "models": [other_model],
                "claims": [other_claim or self._OTHER],
            },
        }

    def test_another_models_found_quote_for_the_same_point_keeps_it_confirmed(self):
        data = self._run(self._checks())
        assert data.get("unverifiable", []) == []
        (kept,) = data["confirmed"]
        assert kept["quote_check"] == "sibling_found"
        assert kept["quote_found_by"] == "openai"
        assert "openai" in kept["quote_check_note"]
        assert kept["supporting_quote"] == _ROW_RETYPED, "the model's own item is kept"

    def test_the_same_models_other_quote_is_not_a_second_opinion(self):
        data = self._run(self._checks(other_model="gemini"))
        assert data["confirmed"] == []
        (moved,) = data["unverifiable"]
        assert moved["quote_check"] == "not_found"

    def test_a_found_quote_for_a_different_point_does_not_count(self):
        data = self._run(
            self._checks(other_claim="Golf courses use 150 million gallons a year each")
        )
        assert data["confirmed"] == []
        assert len(data["unverifiable"]) == 1

    def test_a_wrapper_and_the_page_it_forwards_to_are_the_same_page(self):
        data = self._run(self._checks(other_url=_URL + "?utm_source=x"))
        (kept,) = data["confirmed"]
        assert kept["quote_check"] == "sibling_found"

    def test_a_demotion_says_the_page_was_read_in_full(self):
        data = self._run(
            {
                (_URL, _ROW_RETYPED): {
                    "found": False,
                    "models": ["gemini"],
                    "claims": [self._CLAIM],
                }
            }
        )
        (moved,) = data["unverifiable"]
        assert "read in full" in moved["reason"]
        assert "not on the page" in moved["reason"]
        assert "did not see" not in moved["reason"]

    def test_the_callers_results_are_not_mutated_by_the_annotation(self):
        results = _results(
            {
                "confirmed": [
                    {
                        "claim": self._CLAIM,
                        "source": "s",
                        "source_url": _URL,
                        "supporting_quote": _ROW_RETYPED,
                    }
                ]
            }
        )
        before = repr(results)
        consolidation._normalise_fact_check_results(results, None, self._checks())
        assert repr(results) == before

    def test_section_1_shows_the_sibling_line(self):
        from ci_article_review import report_markdown

        data = self._run(self._checks())
        (kept,) = data["confirmed"]
        lines = "\n".join(report_markdown._kv_lines(kept))
        assert "Quote check: sibling_found" in lines
        assert "Quote found by: openai" in lines
