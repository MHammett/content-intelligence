"""A resolution that timed out or raised is not "No source identified" (#340).

``resolve_citations`` runs each claim as a job with a 90 second safety net. A job
that outlived it, or raised, used to be written as
``{"claim", "resolved": False, "note": "Resolution error: ..."}`` with no URL and
no ``verification``, and ``disposition()`` files a citation with neither under
``no_source``. Section 9 then said, of a claim whose source had been named and
whose fetch had simply not finished, "No URL was found for these claims, so
nothing was fetched". Both halves of that are false for a timeout.

It now has its own disposition, keeps the URL it was trying, says whether it
timed out or raised, and the report words the section by what happened.
"""

import time


from ci_article_review import report_markdown
from ci_article_review.adapters.citation import resolver
from ci_article_review.adapters.citation.disposition import (
    DISPOSITIONS,
    disposition,
    label_for,
)

_URL = "https://example.org/slow-page"


def _entry(urls=(_URL,), claim="The plant has nine units."):
    return {"claim": claim, "known_urls": list(urls), "fact_check_bucket": "confirmed"}


def _resolve(monkeypatch, one, entries, timeout=None):
    monkeypatch.setattr(resolver, "_resolve_one", one)
    if timeout is not None:
        monkeypatch.setattr(resolver, "_RESOLVE_TIMEOUT_SECONDS", timeout)
    # Nothing past the resolution jobs is under test, and none of it may reach
    # the network.
    monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_verify_archive_matches", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_note_weak_span_sources", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_note_breaker_state", lambda *a, **k: None)
    return resolver.resolve_citations(entries, [], api_keys={})


class TestTheResolverRecordsWhatHappened:
    def test_a_job_past_its_time_limit_is_a_timeout_not_a_missing_source(
        self, monkeypatch
    ):
        def slow(*args, **kwargs):
            time.sleep(0.5)

        (result,) = _resolve(monkeypatch, slow, [_entry()], timeout=0.05)
        assert result["resolved"] is False
        assert result["verification"] == "resolution_error"
        assert result["error_kind"] == "timeout"
        assert result["url"] == _URL, "the page it was trying is still known"
        assert "did not finish" in result["note"]
        assert "re-run" in result["note"]
        assert disposition(result) == "resolution_error"

    def test_a_job_that_raised_says_what_it_raised(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("socket on fire")

        (result,) = _resolve(monkeypatch, boom, [_entry()])
        assert result["verification"] == "resolution_error"
        assert result["error_kind"] == "error"
        assert "RuntimeError" in result["note"] and "socket on fire" in result["note"]
        assert result["url"] == _URL

    def test_a_claim_with_no_url_still_has_a_resolution_error_disposition(
        self, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("x")

        (result,) = _resolve(monkeypatch, boom, [_entry(urls=())])
        assert "url" not in result
        assert disposition(result) == "resolution_error"

    def test_several_known_urls_are_not_pretended_to_be_one(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("x")

        (result,) = _resolve(
            monkeypatch, boom, [_entry(urls=(_URL, "https://example.org/b"))]
        )
        assert result["url"] == _URL
        assert "2 source URLs" in result["note"]

    def test_the_other_claims_still_resolve(self, monkeypatch):
        def one(claim, *args, **kwargs):
            if claim == "bad":
                raise RuntimeError("x")
            return {"claim": claim, "resolved": True, "verification": "checksum"}

        results = _resolve(
            monkeypatch,
            one,
            [_entry(claim="good"), _entry(claim="bad"), _entry(claim="fine")],
        )
        assert [r["verification"] for r in results] == [
            "checksum",
            "resolution_error",
            "checksum",
        ]
        assert [r["claim"] for r in results] == ["good", "bad", "fine"]


class TestDispositionVocabulary:
    def test_resolution_error_is_a_bucket_with_its_own_label(self):
        assert "resolution_error" in dict(DISPOSITIONS)
        assert "did not complete" in label_for("resolution_error")

    def test_a_url_does_not_make_it_a_refused_fetch(self):
        """`fetch_failed` means the origin refused or was unreachable. A job that
        never finished tells us nothing about the origin."""
        assert disposition({"url": _URL, "verification": "resolution_error"}) == (
            "resolution_error"
        )

    def test_a_citation_that_merely_has_no_url_is_still_no_source(self):
        assert disposition({"claim": "c", "resolved": False}) == "no_source"


def _report(citations):
    from .test_report_markdown import _base_report

    return _base_report(section_9_citations=citations)


_TIMEOUT = {
    "claim": "The plant has nine units.",
    "url": _URL,
    "resolved": False,
    "verification": "resolution_error",
    "error_kind": "timeout",
    "note": "Resolution did not finish within 90s, so nothing was checked.",
}


class TestTheReportWordsItByWhatHappened:
    def _md(self, citations):
        return report_markdown.render_report_markdown(_report(citations))

    def test_it_is_not_listed_under_no_source_identified(self):
        md = self._md([_TIMEOUT])
        assert "No source identified (" not in md
        assert "No URL was found" not in md

    def test_it_gets_its_own_section_with_rerun_advice(self):
        md = self._md([_TIMEOUT])
        section = md.split("### Resolution did not complete")[1]
        assert "(1)" in section.split("\n")[0]
        assert "re-run" in section.lower()
        assert _URL in section
        assert "The plant has nine units." in section

    def test_the_summary_table_counts_it_in_its_own_row(self):
        md = self._md([_TIMEOUT, {"claim": "x", "resolved": False}])
        assert "| Resolution did not complete (error or timeout) | 1 |" in md
        assert "| No source identified | 1 |" in md

    def test_a_real_missing_source_keeps_its_wording(self):
        md = self._md([{"claim": "x", "resolved": False}])
        assert "No URL was found for these claims" in md

    def test_the_rows_still_sum_to_the_claims(self):
        import re

        citations = [_TIMEOUT, {"claim": "x", "resolved": False}]
        md = self._md(citations)
        counts = [int(n) for n in re.findall(r"^\| .+ \| (\d+) \|$", md, re.M)]
        assert sum(counts) == len(citations)


class TestTheConsoleSummary:
    def test_it_counts_as_unresolved_and_is_named(self, tmp_path, capsys):
        from unittest.mock import patch

        from .test_pipeline_end_to_end import _stubbed_run

        def resolutions(claims, *args, **kwargs):
            return [{**_TIMEOUT, "claim": c["claim"]} for c in claims]

        stub = patch(
            "ci_article_review.adapters.citation.resolver.resolve_citations",
            side_effect=resolutions,
        )
        with _stubbed_run(tmp_path, extra_patches=[stub]) as report:
            n = len(report["section_9_citations"])
        out = capsys.readouterr().out

        assert n > 0
        assert f"{n} unresolved" in out
        assert f"{n} citation(s) did not finish resolving" in out
        assert "re-run" in out
