"""Tests for ci_core.extract — response-body text extraction and excerpt selection.

The bug these guard against: the citation resolver fed raw HTTP response bodies
to the relevance verifier, so the model saw doctype/meta/script/nav markup (or
raw PDF bytes) and correctly reported that it did not support the claim. Every
test here is about the input being readable before anything judges it.
"""

import sys
import types
from unittest.mock import patch


from ci_core import extract


# --- Minimal PDF builder --------------------------------------------------
# Written by hand rather than shipped as a binary fixture: these tests need a
# PDF whose text layer they control exactly, and a byte-level builder keeps
# that visible in the test instead of opaque in a blob.


def _escape(text):
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(lines):
    """Return bytes of a one-page PDF whose text layer contains ``lines``."""
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        ops.append(f"({_escape(line)}) Tj")
        ops.append("T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


# Boilerplate deliberately long enough to fill a head-truncation window on its
# own, and containing none of the article's substance.
_BOILERPLATE = (
    '<!DOCTYPE html><html lang="en"><head>'
    '<meta charset="utf-8">'
    + "".join(
        f'<link rel="stylesheet" href="/assets/style-{i}.css">'
        f'<script src="/assets/bundle-{i}.js"></script>'
        for i in range(40)
    )
    + "<title>Emissions & Generation Resource Integrated Database</title></head><body>"
    "<nav>Home Newsroom Regulations About EPA Contact Us Subscribe</nav>"
    "<header>United States Environmental Protection Agency</header>"
)
_FOOTER = "<footer>Privacy notice. Accessibility. FOIA requests.</footer></body></html>"


class TestExtractResponseText:
    def test_article_body_survives_and_boilerplate_does_not(self):
        html = (
            _BOILERPLATE
            + "<article><h1>eGRID</h1><p>eGRID is a comprehensive source of data "
            "on the environmental characteristics of electric power generated in "
            "the United States.</p></article>" + _FOOTER
        )
        text, kind = extract.extract_response_text(
            html.encode("utf-8"), content_type="text/html; charset=utf-8"
        )

        assert kind == "html"
        assert "eGRID is a comprehensive source of data" in text
        assert "stylesheet" not in text
        assert "bundle-3.js" not in text
        assert "FOIA requests" not in text

    def test_pdf_bytes_become_text_not_binary(self):
        raw = make_pdf(
            [
                "Guidelines for limiting exposure to time-varying fields.",
                "The reference level is 200 microtesla at 50 Hz.",
            ]
        )
        text, kind = extract.extract_response_text(
            raw, content_type="application/pdf", url="https://example.org/gdl.pdf"
        )

        assert kind == "pdf"
        assert "200 microtesla" in text
        assert "%PDF" not in text

    def test_pdf_detected_when_server_mislabels_content_type(self):
        """Servers routinely serve PDFs as octet-stream; magic bytes decide."""
        raw = make_pdf(["Reference level table follows."])
        text, kind = extract.extract_response_text(
            raw, content_type="application/octet-stream", url="https://x/download"
        )

        assert kind == "pdf"
        assert "Reference level table" in text

    def test_unreadable_pdf_returns_empty_not_garbage(self):
        text, kind = extract.extract_response_text(
            b"%PDF-1.4\nthis is not actually a parseable pdf",
            content_type="application/pdf",
        )

        assert kind == "pdf"
        assert text == ""

    def test_missing_pypdf_degrades_to_empty(self):
        raw = make_pdf(["Reference level table follows."])
        with patch.dict(sys.modules, {"pypdf": None}):
            text, kind = extract.extract_response_text(
                raw, content_type="application/pdf"
            )

        assert kind == "pdf"
        assert text == ""

    def test_js_only_page_yields_no_text(self):
        """A shell page with no article body must report nothing readable,
        rather than handing tag soup on to a verifier."""
        html = (
            "<!DOCTYPE html><html><head><title>App</title></head><body>"
            '<div id="root"></div><script>renderApp();</script></body></html>'
        )
        text, _kind = extract.extract_response_text(
            html.encode("utf-8"), content_type="text/html"
        )

        assert text == ""

    def test_plain_text_passes_through(self):
        text, kind = extract.extract_response_text(
            b"total generation: 4178 TWh", content_type="text/plain"
        )

        assert kind == "text"
        assert text == "total generation: 4178 TWh"

    def test_undecodable_bytes_do_not_raise(self):
        text, _kind = extract.extract_response_text(
            b"\xff\xfe\x00 plain content", content_type="text/plain"
        )

        assert isinstance(text, str)


class TestLooksLikePdf:
    def test_url_suffix_with_query_string(self):
        assert extract.looks_like_pdf(url="https://x/report.pdf?download=1")

    def test_html_page_is_not_pdf(self):
        assert not extract.looks_like_pdf(
            content_type="text/html", url="https://x/page", raw=b"<!DOCTYPE html>"
        )


class TestLooksLikeAccessWall:
    """Bot walls are served as HTTP 200 and extract into clean prose, so they
    reach the verifier looking exactly like a real page that fails the claim.
    """

    def test_captcha_interstitial_detected(self):
        text = (
            "Request Access. Due to aggressive automated scraping of eCFR.gov, "
            "programmatic access is limited. Your request has been flagged as "
            "potentially automated. If you are a human user receiving this "
            "message, please complete the CAPTCHA (bot test) below."
        )

        assert extract.looks_like_access_wall(text)

    def test_paywall_detected(self):
        assert extract.looks_like_access_wall(
            "This content is available to subscribers. Please subscribe to "
            "continue reading this article."
        )

    def test_real_article_is_not_a_wall(self):
        assert not extract.looks_like_access_wall(
            "The eGRID subregions are EPA's emissions accounting zones covering "
            "the contiguous United States. " * 5
        )

    def test_long_article_discussing_captchas_is_not_a_wall(self):
        """A genuine article about bot detection must not be discarded."""
        text = (
            "Researchers studied how sites verify you are a human. " * 5
            + "Filler analysis of the findings and their implications. " * 60
        )

        assert len(text) > 2500
        assert not extract.looks_like_access_wall(text)

    def test_empty_text_is_not_a_wall(self):
        assert not extract.looks_like_access_wall("")
        assert not extract.looks_like_access_wall(None)


class TestSelectExcerpt:
    def test_short_text_returned_whole(self):
        assert extract.select_excerpt("short body", "a claim") == "short body"

    def test_centers_on_the_passage_containing_the_claim_terms(self):
        """The bug in miniature: the supporting sentence sits well past the
        first 4000 characters, so head+tail truncation would drop it."""
        filler = "Unrelated background discussion of policy history. " * 400
        needle = "Loudoun County hosts approximately 4,700 diesel generators."
        text = filler + needle + filler

        excerpt = extract.select_excerpt(
            text,
            "Virginia DEQ reports approximately 4,700 generators in Loudoun County",
        )

        assert needle in excerpt
        assert len(excerpt) < len(text)

    def test_falls_back_to_head_tail_when_no_terms_match(self):
        text = "A" * 3000 + "B" * 4000
        excerpt = extract.select_excerpt(text, "zzzzz qqqqq wwwww")

        assert excerpt.startswith("A")
        assert "chars omitted" in excerpt

    def test_claimless_input_does_not_raise(self):
        text = "x" * 6000
        assert extract.select_excerpt(text, "")
        assert extract.select_excerpt(text, None)

    def test_is_deterministic(self):
        text = ("filler text about other things. " * 300) + "the value is 4,700 units."
        claim = "the value is 4,700 units"
        assert extract.select_excerpt(text, claim) == extract.select_excerpt(
            text, claim
        )


class TestClaimTerms:
    def test_numbers_and_acronyms_are_kept(self):
        numbers, words = extract.claim_terms(
            "EPA eGRID reports 4,700 generators across 9 subregions"
        )

        assert "4,700" in numbers
        assert "epa" in words
        assert "generators" in words

    def test_short_and_stopword_terms_are_dropped(self):
        _numbers, words = extract.claim_terms("the data is about those which are there")

        assert "the" not in words
        assert "those" not in words
        assert "there" not in words


class TestExtractArticleStillWorks:
    """extract_article moved here from analysis.webpage; its contract is unchanged."""

    def test_title_and_body(self):
        title, body = extract.extract_article(
            "<html><head><title>T</title></head><body><article><h2>H</h2>"
            "<p>Body text.</p></article></body></html>"
        )

        assert title == "T"
        assert "Body text." in body

    def test_fallback_parser_keeps_heading_markup(self):
        """The SEO and structure checks key off heading markup, so the built-in
        fallback must emit it when trafilatura is unavailable."""
        with patch.dict(sys.modules, {"trafilatura": None}):
            _title, body = extract.extract_article(
                "<html><head><title>T</title></head><body><article><h2>H</h2>"
                "<p>Body text.</p></article></body></html>"
            )

        assert "## H" in body
        assert "Body text." in body

    def test_trafilatura_used_when_available(self):
        fake = types.ModuleType("trafilatura")
        fake.extract = lambda html, **kw: "trafilatura body"
        with patch.dict(sys.modules, {"trafilatura": fake}):
            _title, body = extract.extract_article("<html><body><p>x</p></body></html>")

        assert body == "trafilatura body"


#: Built rather than written so no tool in the chain can normalise it away.
_FFFD = chr(0xFFFD)


class TestBinaryMislabelledAsText:
    """The gap left after PDFs were routed by magic bytes.

    A body with no angle brackets cannot be caught by the HTML path, and
    without PDF magic nothing else looks at it -- so a server that labels a
    binary file ``text/plain`` used to hand the verifier a page of mojibake.
    That is the raw-PDF failure again, minus the signature that made it
    obvious.
    """

    #: Binary that deliberately contains no "<" or ">".
    RAW = bytes(b for b in range(256) if b not in (0x3C, 0x3E)) * 4

    def test_binary_labelled_text_plain_yields_nothing(self):
        text, kind = extract.extract_response_text(
            self.RAW, content_type="text/plain", url="https://x.example/d"
        )
        assert kind == "text"
        assert text == ""

    def test_binary_with_no_content_type_yields_nothing(self):
        text, _kind = extract.extract_response_text(self.RAW, url="https://x.example/d")
        assert text == ""

    def test_real_prose_with_one_bad_byte_is_kept(self):
        """A single mojibake in a long page is not grounds to discard it."""
        prose = ("The commission published its findings in April. " * 20) + _FFFD
        assert not extract.looks_like_binary(prose)
        text, kind = extract.extract_response_text(
            prose.encode("utf-8"), content_type="text/plain"
        )
        assert kind == "text"
        assert text.startswith("The commission published")

    def test_clean_prose_is_not_binary(self):
        assert not extract.looks_like_binary("Perfectly ordinary text.")
        assert not extract.looks_like_binary("")

    def test_mostly_replacement_characters_is_binary(self):
        assert extract.looks_like_binary("ab" + _FFFD * 30)


class TestBodySelection:
    """trafilatura is preferred, but not when it has lost most of the page.

    It is tuned for news articles: find one main content block, discard the rest
    as boilerplate. Right for an article, wrong for a page whose content *is*
    repeated blocks — a team page, a staff directory, a filings list. The
    heuristic parser was only consulted when trafilatura returned nothing, so a
    page it cut to one seventh went through unnoticed.

    Measured 2026-09-04 on a six-person team page: trafilatura kept 439 of
    36,717 bytes, covering one colleague. The page had been cited for two claims
    about the article's *author*, whose bio was among the discarded 98%, so
    citation verification reported the source as failing to support a claim the
    page states outright.
    """

    def test_trafilatura_is_used_when_the_two_agree_closely(self):
        """The normal case. Measured gaps on four real article pages were 1.09x
        to 1.45x — trafilatura trimming navigation, which is its job."""
        keep = "x" * 1000
        assert self._choose(trafilatura=keep, heuristic="y" * 1200) == keep

    def _choose(self, trafilatura, heuristic):
        import unittest.mock as m

        with (
            m.patch.object(
                extract, "_extract_with_trafilatura", return_value=trafilatura
            ),
            m.patch.object(extract, "_fallback_body", return_value=heuristic),
        ):
            return extract._best_body("<html></html>", [])

    def test_the_heuristic_wins_a_landslide(self):
        """439 vs 3,381 — the shape that says "this is not an article"."""
        heuristic = "y" * 3381
        assert self._choose(trafilatura="x" * 439, heuristic=heuristic) == heuristic

    def test_an_empty_trafilatura_result_falls_back_as_before(self):
        assert self._choose(trafilatura="", heuristic="body") == "body"

    def test_both_empty_is_empty(self):
        assert self._choose(trafilatura="", heuristic="") == ""

    def test_a_shorter_heuristic_never_wins(self):
        keep = "x" * 5000
        assert self._choose(trafilatura=keep, heuristic="y" * 100) == keep

    def test_the_threshold_is_not_met_at_exactly_the_ratio(self):
        """Strictly greater than, so the boundary keeps trafilatura."""
        tr = "x" * 1000
        at_ratio = "y" * int(1000 * extract._HEURISTIC_WINS_RATIO)
        assert self._choose(trafilatura=tr, heuristic=at_ratio) == tr
