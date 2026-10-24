"""A claim not found in the part of a document that was read is not refuted.

On the 2026-10-24 run of a 267-claim draft, 111 Section 9 entries read "does NOT
support the claim". 65 were PDFs and 40 were council packets the verifier had
stopped reading at page 40 (issue #395): the claims were cited to pages it never
opened. These tests pin the distinction the report now draws between "the
document does not say this" and "the part of the document we read does not say
this", end to end — resolver, ledger, worklist and readable report.
"""

from unittest.mock import patch

import pytest

from ci_article_review import claim_status, report_markdown, worklist
from ci_article_review.adapters.citation import quote_check, reask, resolver
from ci_article_review.adapters.citation.disposition import DISPOSITIONS, label_for
from ci_core import extract

CLAIM = "The council capped Project Cardinal at 42,000 gallons per day"
URL = "https://www.example.gov/ArchiveCenter/ViewFile/Item/6395"

_SOURCES = []


def _response(content_type):
    class _Resp:
        status_code = 200
        content = b"%PDF-1.6 body"
        headers = {"Content-Type": content_type}
        encoding = "utf-8"
        url = URL

        def raise_for_status(self):
            return None

    return _Resp()


def _no_wayback(*_a, **_k):
    return {"archived": False}


def _resolve(text, kind, coverage, verdict, quote=""):
    """Resolve CLAIM against ``text`` with a verifier that answers ``verdict``."""
    seen = {}

    def fake_call(provider, system, user, api_key, model=None, **_kw):
        seen["prompt"] = user
        return {
            "failed": False,
            "data": {
                "verdict": verdict,
                "reason": "the page talks about other things",
                "quote": quote,
            },
            "model": "mistral-small-latest",
            "tokens": {},
            "elapsed_seconds": 0.1,
        }

    with (
        patch(
            "ci_article_review.adapters.citation.resolver.safe_get",
            return_value=_response("application/pdf" if kind == "pdf" else "text/html"),
        ),
        patch(
            "ci_article_review.adapters.citation.resolver.extract"
            ".extract_response_text_with_coverage",
            return_value=(text, kind, coverage),
        ),
        patch(
            "ci_article_review.adapters.citation.resolver.wayback.check",
            side_effect=_no_wayback,
        ),
        patch(
            "ci_article_review.adapters.citation.resolver.llm.call_provider",
            side_effect=fake_call,
        ),
    ):
        results = resolver.resolve_citations(
            [{"claim": CLAIM, "known_url": URL}],
            _SOURCES,
            api_keys={"mistral": {"api_key": "k"}},
        )
    return results[0], seen.get("prompt", "")


SHORT = (
    "Council minutes. " * 20 + "The cap on Project Cardinal is 42,000 gallons per day. "
)
LONG = (
    "Unrelated committee business about parking permits and sidewalks. " * 400
    + "The cap on Project Cardinal is 42,000 gallons per day. "
    + "More unrelated business about the fall festival. " * 400
)
WHOLE_PDF = extract.read_coverage("pdf", pages_total=12, pages_read=12)
CAPPED_PDF = extract.read_coverage("pdf", pages_total=612, pages_read=400)
HTML = extract.read_coverage("html")


class TestResolverTellsNotFoundFromNotThere:
    def test_pdf_cut_at_the_page_cap_is_a_partial_read_not_a_mismatch(self):
        result, _ = _resolve(SHORT, "pdf", CAPPED_PDF, "not_addressed")

        assert result["verification"] == "partial_read"
        assert result["resolved"] is False
        assert result["read_coverage"]["limits"] == ["pdf_page_cap"]
        assert result["read_coverage"]["partial"] is True
        assert "Read in part only" in result["note"]
        assert "400 of 612 PDF pages were read" in result["note"]
        assert "NOT evidence against the claim" in result["note"]
        # The verdict is kept — it describes the part that was read.
        assert result["relevance_verdict"] == "not_addressed"

    def test_a_long_page_shown_through_the_window_is_a_partial_read(self):
        assert len(LONG) > resolver._EXCERPT_HEAD + resolver._EXCERPT_TAIL
        result, prompt = _resolve(LONG, "html", HTML, "not_addressed")

        assert result["verification"] == "partial_read"
        assert result["read_coverage"]["limits"] == ["excerpt_window"]
        assert result["read_coverage"]["chars_total"] == len(LONG)
        assert result["read_coverage"]["chars_shown"] < len(LONG)
        assert "most likely to address the claim" in result["note"]
        # The window did its job: the passage reached the verifier anyway.
        assert "42,000 gallons" in prompt

    def test_a_document_read_in_full_that_does_not_support_is_still_a_mismatch(self):
        result, _ = _resolve(SHORT, "html", HTML, "not_addressed")

        assert result["verification"] == "content_mismatch"
        assert result["read_coverage"]["partial"] is False
        assert result["read_coverage"]["limits"] == []
        assert "read in full" in result["note"]

    def test_a_whole_pdf_that_does_not_support_is_still_a_mismatch(self):
        result, _ = _resolve(SHORT, "pdf", WHOLE_PDF, "not_addressed")

        assert result["verification"] == "content_mismatch"
        assert result["read_coverage"]["pages_read"] == 12

    def test_a_grounded_contradiction_counts_even_on_a_partial_read(self):
        """Text that was found is evidence whatever was left unread."""
        result, _ = _resolve(
            LONG,
            "html",
            HTML,
            "contradicts",
            quote="The cap on Project Cardinal is 42,000 gallons per day.",
        )

        assert result["verification"] == "content_mismatch"
        assert result["relevance_verdict"] == "contradicts"
        assert result["read_coverage"]["partial"] is True

    def test_an_ungrounded_contradiction_on_a_partial_read_stays_open(self):
        result, _ = _resolve(LONG, "html", HTML, "contradicts", quote="not on the page")

        assert result["verification"] == "partial_read"

    def test_a_supporting_verdict_carries_coverage_too(self):
        result, prompt = _resolve(
            SHORT,
            "pdf",
            WHOLE_PDF,
            "supports",
            quote="The cap on Project Cardinal is 42,000 gallons per day.",
        )

        assert result["verification"] == "checksum"
        assert result["read_coverage"]["partial"] is False
        assert result["read_coverage"]["pages_total"] == 12

    def test_a_partial_read_is_not_handed_back_for_a_reask(self):
        result, _ = _resolve(SHORT, "pdf", CAPPED_PDF, "not_addressed")
        assert reask.is_refuted(result) is False

    def test_a_partial_read_ranks_with_unverifiable_among_candidates(self):
        partial = {"verification": "partial_read", "relevance_verdict": "not_addressed"}
        mismatch = {
            "verification": "content_mismatch",
            "relevance_verdict": "not_addressed",
        }
        assert resolver._informativeness(partial) < resolver._informativeness(mismatch)
        assert resolver._informativeness(partial) == resolver._informativeness(
            {"verification": "unverifiable"}
        )


class TestOneCapForEveryReader:
    def test_the_quote_check_reads_to_the_same_page_as_the_verifier(self):
        assert quote_check._PDF_MAX_PAGES == extract.PDF_MAX_PAGES


def _partial_citation(claim=CLAIM, url=URL, **extra):
    return {
        "claim": claim,
        "url": url,
        "resolved": False,
        "verification": "partial_read",
        "relevance_verdict": "not_addressed",
        "relevance_reason": "the page talks about other things",
        "read_coverage": {
            "kind": "pdf",
            "pages_total": 612,
            "pages_read": 400,
            "chars_total": 1_480_000,
            "chars_shown": 20_000,
            "window": "best_window",
            "limits": ["pdf_page_cap", "excerpt_window"],
            "partial": True,
        },
        **extra,
    }


def _report(citations, confirmed=()):
    return {
        "section_1_consensus": [],
        "section_2_fact_check": {
            "confirmed": [{"claim": c, "source_model": "gemini"} for c in confirmed],
            "outdated": [],
            "contradicted": [],
            "unverifiable": [],
            "primary_source_needed": [],
            "out_of_scope": [],
        },
        "section_9_citations": list(citations),
    }


class TestLedgerGivesItItsOwnStatus:
    def test_partial_read_is_source_read_in_part(self):
        ledger = claim_status.build_claim_ledger(_report([_partial_citation()]))
        (entry,) = ledger["claims"]
        assert entry["status"] == "source_read_in_part"
        assert "not evidence against the claim" in entry["why"]
        assert ledger["counts"]["source_read_in_part"] == 1
        assert ledger["counts"]["source_does_not_support"] == 0

    def test_a_models_confirmation_is_named_but_does_not_outrank_it(self):
        ledger = claim_status.build_claim_ledger(
            _report([_partial_citation()], confirmed=[CLAIM])
        )
        (entry,) = ledger["claims"]
        assert entry["status"] == "source_read_in_part"
        assert entry["why"].startswith("gemini confirmed it;")

    def test_it_is_never_a_contradiction(self):
        ledger = claim_status.build_claim_ledger(
            _report([_partial_citation(relevance_verdict="contradicts")])
        )
        assert ledger["claims"][0]["status"] == "source_read_in_part"


class TestWorklistSendsTheAuthorToTheCitedPage:
    def test_one_item_per_document_under_its_own_heading(self):
        report = _report(
            [_partial_citation(), _partial_citation(claim="A second claim on page 410")]
        )
        rendered = "\n".join(worklist.render_worklist(worklist.build_worklist(report)))

        assert (
            "### Open a document the run read only in part (1 action(s), 2 claim(s))"
            in rendered
        )
        assert "400 of 612 PDF pages were read" in rendered
        assert "the PDF page cap" in rendered
        assert "Open at the cited page and look" in rendered
        assert "[you]" in rendered

    def test_it_is_not_treated_as_settled(self):
        assert "partial_read" not in worklist._READ_DISPOSITIONS


class TestReadableReportSeparatesTheTwoReadings:
    def _md(self, citations):
        return report_markdown.render_report_markdown(
            {
                "generated": "2026-10-24T00:00:00+00:00",
                "run_number": 1,
                "article_title": "t",
                "publication": "p",
                "api_call_log": [],
                "section_1_consensus": [],
                "section_2_fact_check": {},
                "section_3_voice": [],
                "section_4_argument": [],
                "section_5_completeness": [],
                "section_6_red_team": [],
                "section_7_low_confidence": [],
                "section_8_additional": [],
                "section_9_citations": citations,
                "contradictions": [],
                "model_failures": [],
                "cost_summary": {},
                "pre_analysis": {},
            }
        )

    def test_partial_reads_get_their_own_block_and_summary_line(self):
        mismatch = {
            "claim": "A claim the whole page did not support",
            "url": "https://www.example.gov/page",
            "resolved": False,
            "verification": "content_mismatch",
            "relevance_verdict": "not_addressed",
            "relevance_reason": "r",
            "read_coverage": {
                "kind": "html",
                "chars_total": 900,
                "chars_shown": 900,
                "window": "whole",
                "limits": [],
                "partial": False,
            },
        }
        md = self._md([_partial_citation(), mismatch])

        assert "### Read in part only — not found in the part read (1)" in md
        assert "NOT evidence against the claim" in md
        assert "**A further 1 had a document fetched and read IN PART" in md
        assert "1 where it did not." in md
        # The coverage line is rendered on both, and reads differently.
        assert "Read: IN PART — 400 of 612 PDF pages were read" in md
        assert (
            "Read: in full — the verifier was shown the whole text (900 characters)"
            in md
        )
        assert "| Read in part only — not found in the part read | 1 |" in md

    @pytest.mark.parametrize("key", [k for k, _ in DISPOSITIONS])
    def test_every_disposition_has_a_place_in_the_report(self, key):
        """A new disposition must reach the table, the ledger and the worklist.

        This is the guard against the next silent cap: a citation outcome the
        vocabulary knows about but some reader does not would be filed under
        the wrong heading, which is how a reading limit came to be reported as
        a refutation in the first place.
        """
        citation = {"claim": "c", "url": "https://x.example/d", "verification": key}
        md = self._md([citation])
        assert f"| {label_for(key)} |" in md

        report = _report([citation])
        ledger = claim_status.build_claim_ledger(report)
        assert ledger["claims"][0]["status"] in {k for k, _ in claim_status.STATUSES}
        worklist.render_worklist(worklist.build_worklist(report))
