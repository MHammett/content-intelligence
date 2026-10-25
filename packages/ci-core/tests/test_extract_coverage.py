"""What the extractor says about how much of a document it read.

Two readers with different page caps had produced a false negative: the citation
verifier stopped at page 40 while the quote check read 400, so a claim cited to
page 120 of a council packet was reported "not supported" by a document the
verifier had never read past page 40. One cap now, and every read says whether
it stopped short.
"""

import io

import pypdf

from ci_core import extract


def _pdf_with_pages(n):
    writer = pypdf.PdfWriter()
    for _ in range(n):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class TestOnePageCap:
    def test_the_cap_is_the_quote_checks_cap(self):
        assert extract.PDF_MAX_PAGES == 400

    def test_default_reads_up_to_the_cap(self):
        raw = _pdf_with_pages(5)
        _text, coverage = extract.extract_pdf_text_with_coverage(raw)
        assert coverage == {
            "kind": "pdf",
            "pages_total": 5,
            "pages_read": 5,
            "partial": False,
        }


class TestCoverageSaysWhenTheReadStoppedShort:
    def test_a_cap_below_the_page_count_is_partial(self):
        raw = _pdf_with_pages(5)
        _text, coverage = extract.extract_pdf_text_with_coverage(raw, max_pages=2)
        assert coverage["pages_total"] == 5
        assert coverage["pages_read"] == 2
        assert coverage["partial"] is True

    def test_a_cap_at_or_above_the_page_count_is_whole(self):
        raw = _pdf_with_pages(3)
        _text, coverage = extract.extract_pdf_text_with_coverage(raw, max_pages=3)
        assert coverage["partial"] is False
        assert coverage["pages_read"] == 3

    def test_an_unreadable_pdf_reports_no_counts_and_is_not_partial(self):
        text, coverage = extract.extract_pdf_text_with_coverage(b"%PDF-1.4 not a pdf")
        assert text == ""
        assert coverage["pages_total"] is None
        assert coverage["pages_read"] is None
        # Nothing was read, which is a different fact from "read in part".
        assert coverage["partial"] is False

    def test_response_text_carries_coverage_for_pdfs_only(self):
        raw = _pdf_with_pages(4)
        _text, kind, coverage = extract.extract_response_text_with_coverage(
            raw, content_type="application/pdf"
        )
        assert kind == "pdf"
        assert coverage["pages_total"] == 4

        _text, kind, coverage = extract.extract_response_text_with_coverage(
            b"<html><body><article><p>"
            + b"Plain page. " * 50
            + b"</p></article></body></html>",
            content_type="text/html",
        )
        assert kind == "html"
        assert coverage == {
            "kind": "html",
            "pages_total": None,
            "pages_read": None,
            "partial": False,
        }

    def test_two_value_form_still_returns_text_and_kind(self):
        raw = _pdf_with_pages(2)
        text, kind = extract.extract_response_text(raw, content_type="application/pdf")
        assert kind == "pdf"
        assert isinstance(text, str)


class TestExcerptWindowIsReported:
    CLAIM = "the reference level is 200 microtesla at 50 Hz"

    def test_a_short_document_is_sent_whole(self):
        text = "The reference level is 200 microtesla at 50 Hz. " * 10
        excerpt, window = extract.select_excerpt_with_window(
            text, self.CLAIM, head=4000, tail=1000
        )
        assert excerpt == text
        assert window == {
            "chars_total": len(text),
            "chars_read": len(text),
            "windowed": False,
            "how": "whole",
        }

    def test_a_long_document_is_windowed_on_the_claims_terms(self):
        filler = "Unrelated committee business about parking permits. " * 200
        passage = "The reference level for the public is 200 microtesla at 50 Hz. "
        text = filler + passage + filler
        excerpt, window = extract.select_excerpt_with_window(
            text, self.CLAIM, head=4000, tail=1000
        )
        assert "200 microtesla" in excerpt
        assert window["windowed"] is True
        assert window["how"] == "best_window"
        assert window["chars_total"] == len(text)
        assert window["chars_read"] <= 5000

    def test_a_long_document_with_no_usable_terms_falls_back_to_head_and_tail(self):
        text = "x " * 6000
        _excerpt, window = extract.select_excerpt_with_window(
            text, "it is so", head=4000, tail=1000
        )
        assert window["windowed"] is True
        assert window["how"] == "head_tail"

    def test_select_excerpt_is_the_text_half(self):
        text = "The reference level is 200 microtesla at 50 Hz. " * 10
        assert extract.select_excerpt(text, self.CLAIM) == text
