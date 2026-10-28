"""Turn a fetched HTTP response body into readable text.

Two consumers need this and they must not import each other:
``ci_article_review.analysis.webpage`` (URL input mode) and
``ci_article_review.adapters.citation.resolver`` (citation verification).
``analysis.links`` already imports ``adapters.citation.wayback``, so a direct
``adapters -> analysis`` import would close a cycle. Per the post-#43 layout,
the shared primitive lives in ci-core and both sides import it from here.

HTML extraction prefers `trafilatura` (best-in-class main-content extraction)
when installed — an OPTIONAL dependency. Without it we fall back to a built-in
heuristic HTML parser that drops obvious boilerplate, prefers the
<article>/<main> region, and keeps h1-h3 headings (the SEO and structure checks
key off heading markup).

PDF extraction uses `pypdf`. Every extractor here returns empty rather than
raising: callers must be able to tell "read it, and here is the text" from
"could not read it", and an exception in the middle of a citation resolution
would collapse that distinction into a generic failure.
"""

import bisect
import codecs
import logging
import math
import re
import time
from html.parser import HTMLParser

log = logging.getLogger(__name__)

# Elements whose text is never article content.
_SKIP_TAGS = {
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "aside",
    "noscript",
    "form",
    "svg",
    "template",
}
# Block-level elements that mark a text-fragment boundary.
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "tr",
    "br",
    "ul",
    "ol",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
}
# Headings we preserve, mapped to their markdown prefix.
_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###"}


class _ArticleParser(HTMLParser):
    """Heuristic main-content extractor used when trafilatura is unavailable.

    Collects text fragments tagged as paragraphs or headings, recording whether
    each fragment sits inside an <article>/<main> region so the caller can prefer
    that region. Boilerplate tags in ``_SKIP_TAGS`` are dropped wholesale.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title = ""
        self.main_depth = 0
        self.cur_heading = None
        self._buf = []
        self.nodes = []  # [{"kind": "#"|"##"|"###"|"p", "text": str, "in_main": bool}]

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if not text:
            return
        self.nodes.append(
            {
                "kind": self.cur_heading or "p",
                "text": text,
                "in_main": self.main_depth > 0,
            }
        )

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
            return
        if tag in ("article", "main"):
            self.main_depth += 1
        if tag in _HEADING_TAGS:
            self._flush()
            self.cur_heading = _HEADING_TAGS[tag]
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self.cur_heading = None
        elif tag in _BLOCK_TAGS:
            self._flush()
        if tag in ("article", "main") and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title += data
            return
        if data.strip():
            self._buf.append(data)


def _fallback_body(nodes):
    """Render extracted nodes to markdown, preferring the <article>/<main> region."""
    chosen = [n for n in nodes if n["in_main"]] or nodes
    lines = []
    for n in chosen:
        if n["kind"] in ("#", "##", "###"):
            lines.append(f"{n['kind']} {n['text']}")
        else:
            lines.append(n["text"])
    return "\n\n".join(lines).strip()


def _extract_with_trafilatura(html):
    """Return markdown body via trafilatura, or None if unavailable/failed."""
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        return trafilatura.extract(html, output_format="markdown", include_links=False)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"trafilatura extraction failed ({exc}); using built-in fallback.")
        return None


def extract_article(html):
    """Extract (title, body_markdown) from raw HTML.

    Title is the page <title> or, failing that, the first <h1>. Body comes from
    trafilatura when installed, otherwise the built-in heuristic parser.
    """
    parser = _ArticleParser()
    parser.feed(html)

    title = (parser.title or "").strip()
    if not title:
        for n in parser.nodes:
            if n["kind"] == "#":
                title = n["text"]
                break

    body = _best_body(html, parser.nodes)

    return (title or "Untitled"), (body or "").strip()


#: How much longer the heuristic body must be before it displaces trafilatura's.
#:
#: Sized from the failure that prompted it rather than picked. On a six-person
#: team page (36,717 bytes) trafilatura returned 439 characters covering one
#: person, while the heuristic parser returned 3,381 covering all six — a factor
#: of 7.7. A normal article does not produce that gap: trafilatura's job there is
#: to strip navigation, a modest trim rather than a 90% cut. 2.5 sits well above
#: the trim and well below the cliff.
_HEURISTIC_WINS_RATIO = 2.5


def _best_body(html, nodes):
    """Body text, preferring trafilatura but not when it has lost most of the page.

    trafilatura is tuned for news articles: it finds one main content block and
    discards the rest as boilerplate. That is the right instinct for an article
    and the wrong one for a page whose content *is* repeated blocks — a team
    page, a staff directory, a list of filings. The heuristic parser was only
    consulted when trafilatura returned nothing at all, so a page it mangled
    down to one seventh passed through unnoticed.

    Measured 2026-09-04 on https://fd-ix.com/about/team/, which a fact-check
    model had cited for two claims about the article's author. trafilatura kept
    one colleague's bio; the author's own — the text that settles both claims —
    was not in it. Citation verification then reported the source as failing to
    support a claim the page states outright, and no trafilatura option
    (favor_recall, deduplicate, include_comments) recovered it.

    Erring toward recall is right for this caller specifically. Missing text
    produces a confident "the source does not support this", asserting something
    false about a real document; surplus navigation produces noise, and
    ``select_excerpt`` already centres its window on the claim's own terms. The
    wrong refutation is the more expensive error.
    """
    trafilatura_body = (_extract_with_trafilatura(html) or "").strip()
    heuristic_body = (_fallback_body(nodes) or "").strip()

    if not trafilatura_body:
        return heuristic_body
    if len(heuristic_body) > len(trafilatura_body) * _HEURISTIC_WINS_RATIO:
        log.debug(
            "Heuristic body (%d chars) displaces trafilatura's (%d): a gap that "
            "size means the page is not shaped like an article.",
            len(heuristic_body),
            len(trafilatura_body),
        )
        return heuristic_body
    return trafilatura_body


def looks_like_pdf(content_type=None, url=None, raw=None):
    """Best-effort PDF detection from the response's own evidence.

    Checks all three signals because each one alone is routinely wrong: servers
    mislabel PDFs as ``application/octet-stream``, plenty of PDF URLs carry no
    ``.pdf`` suffix (content-negotiated or query-string downloads), and a
    truncated body may lack the magic bytes.
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/pdf" or ctype == "application/x-pdf":
        return True
    if url and url.split("?")[0].split("#")[0].strip().lower().endswith(".pdf"):
        return True
    if raw and raw[:5] == b"%PDF-":
        return True
    return False


#: Wall-clock seconds one PDF may spend in text extraction before the reader
#: stops and says so. One value for every reader in the pipeline, because two
#: readers with different limits had already produced a false negative: the
#: citation verifier stopped at 40 pages while the quote check read 400, so a
#: claim cited to page 120 of a council packet was reported as "not supported"
#: by a document the pipeline had not read past page 40.
#:
#: A budget in seconds and not a page count, because pages are not the cost.
#: Measured 2026-10-25 on a 981-page, 172 MB council packet (pypdf opens it in
#: 0.3 s): 100 pages 2.0 s, 400 pages 14.8 s, 700 pages 73.5 s, all 981 pages
#: 134.3 s. The cost is back-loaded, so a 400-page cap left the heaviest third of
#: the document unread to save two minutes, and 156 light pages of a state
#: report took 5.5 s. 180 s is that packet with margin; it is measured from the
#: start of extraction (not the fetch) and checked between pages, so one
#: pathological page can overrun it. At least one page is always read. A read
#: that stops here says so: ``stopped_by`` in :func:`read_coverage`.
PDF_MAX_SECONDS = 180

#: A sanity ceiling on pages, far above any real document (the packet above has
#: 981) and well short of the page count a malformed or hostile file can
#: declare. The time budget is the working limit; this one only has to stop a
#: file that is fast per page but endless.
PDF_MAX_PAGES = 5000

#: Below this many extracted characters per page on average, a PDF is a scan
#: whose text layer is only a stamp or a header. A page of real text carries
#: well over a thousand.
SCANNED_PDF_CHARS_PER_PAGE = 80


def looks_like_scanned_pdf(text, pages_read):
    """True when a PDF's text is too thin per page to be its content.

    A court order filed as page images, downloaded on brockovich-fact-check run
    2 (issue #447), gave 251 characters over 11 pages: "Copy from re:SearchTX",
    the e-filing system's stamp, once a page. That is above the 200-character
    floor callers use for "could not read", so the verifier was asked about it
    and reported that the document does not support the claim, a false
    statement about a source nobody read.
    """
    if not pages_read:
        return False
    return len((text or "").strip()) / pages_read < SCANNED_PDF_CHARS_PER_PAGE


def read_coverage(kind, pages_total=None, pages_read=None, stopped_by=None):
    """What was read of a document, as the record every reader hands on.

    ``partial`` is True only when the pipeline's own limit left part of the
    document unread. It is the one field a verdict is allowed to consult before
    calling a negative answer evidence: a claim not found in a document read
    in part is a claim not found *yet*, and the report must say which.

    ``stopped_by`` names the limit that ended a PDF read, ``"page_cap"`` or
    ``"time_budget"``, and is None when nothing did.
    """
    partial = bool(
        pages_total is not None and pages_read is not None and pages_read < pages_total
    )
    return {
        "kind": kind,
        "pages_total": pages_total,
        "pages_read": pages_read,
        "partial": partial,
        "stopped_by": stopped_by if partial else None,
    }


def extract_pdf_text_with_coverage(
    raw, max_pages=PDF_MAX_PAGES, max_seconds=PDF_MAX_SECONDS
):
    """``(text, coverage)`` for PDF bytes. ``text`` is "" when unreadable.

    ``coverage`` is :func:`read_coverage` for kind ``"pdf"``: the page count the
    document declares, how many were read, and which limit, ``max_pages`` or
    the ``max_seconds`` wall-clock budget, cut the read short. ``max_seconds``
    of None means no time limit. A document that could not be opened reports
    neither count.

    Never raises — pypdf absent, bytes that are not a parseable PDF, a password
    the empty string does not open, or a pure scan with no text layer all come
    back as ``""``, which the caller reports as unreadable rather than as a
    mismatch.
    """
    try:
        import pypdf
    except ImportError:
        log.warning(
            "pypdf is not installed — PDF citation sources cannot be verified. "
            "Install it to enable PDF content verification."
        )
        return "", read_coverage("pdf")

    import io

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if getattr(reader, "is_encrypted", False):
            # Some PDFs are "encrypted" with an empty owner password and decrypt
            # fine; a real password-protected one raises and falls through.
            try:
                reader.decrypt("")
            except Exception:
                log.warning("PDF is password-protected; cannot extract text.")
                return "", read_coverage("pdf")
        total = len(reader.pages)
        started = time.monotonic()
        chunks = []
        read = 0
        stopped_by = None
        for index in range(total):
            if index >= max_pages:
                stopped_by = "page_cap"
                break
            if (
                index > 0
                and max_seconds is not None
                and time.monotonic() - started >= max_seconds
            ):
                stopped_by = "time_budget"
                break
            try:
                chunks.append(reader.pages[index].extract_text() or "")
            except Exception:  # pragma: no cover - defensive, per-page corruption
                pass
            read += 1
    except Exception as exc:
        log.warning(f"PDF text extraction failed: {exc}")
        return "", read_coverage("pdf")

    text = "\n\n".join(c.strip() for c in chunks if c.strip())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, read_coverage(
        "pdf", pages_total=total, pages_read=read, stopped_by=stopped_by
    )


def extract_pdf_text(raw, max_pages=PDF_MAX_PAGES, max_seconds=PDF_MAX_SECONDS):
    """Extract text from PDF bytes via pypdf. Returns "" when unreadable.

    The text half of :func:`extract_pdf_text_with_coverage`, for callers that
    do not report on what was read. Anything that turns the text into a verdict
    about the document should use the coverage-carrying form instead.
    """
    return extract_pdf_text_with_coverage(
        raw, max_pages=max_pages, max_seconds=max_seconds
    )[0]


#: Phrases that identify an interstitial blocking us from the real document.
#: Deliberately narrow and literal — a false positive here silently discards a
#: page we could have verified against.
_ACCESS_WALL_MARKERS = (
    "request has been flagged as potentially automated",
    "complete the captcha",
    "captcha (bot test)",
    "verify you are a human",
    "are you a robot",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "unusual traffic from your computer network",
    "your access to this site has been limited",
    "access denied",
    "403 forbidden",
    "please subscribe to continue reading",
    "this content is available to subscribers",
)

#: An access wall is a stub page. Real documents that happen to *discuss*
#: CAPTCHAs or paywalls are long, so requiring brevity as well as a marker
#: keeps the check from swallowing genuine sources.
_ACCESS_WALL_MAX_CHARS = 2500


def looks_like_access_wall(text):
    """True if ``text`` is a bot-check, paywall, or access-denied interstitial.

    These are served with HTTP 200 and extract into perfectly clean prose, so
    nothing upstream catches them. Without this check the verifier reads "Your
    request has been flagged as potentially automated", correctly concludes it
    does not support the claim, and the report asserts that a source we were
    blocked from reading fails to back the claim — the same false statement
    this module exists to prevent, in a different disguise.
    """
    if not text or len(text) > _ACCESS_WALL_MAX_CHARS:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _ACCESS_WALL_MARKERS)


#: Above this share of undecodable characters, a "text" body is a binary file
#: that was decoded rather than a document that was read. Real prose sits at
#: zero; a stray replacement character from one bad byte in a long page stays
#: far below it, so this does not discard a page over a single mojibake.
_MAX_UNDECODABLE_RATIO = 0.05


def looks_like_binary(text):
    """True if ``text`` is decoded bytes rather than readable content.

    PDFs are caught before this by magic bytes, and markup by the HTML path.
    What reaches here is the remaining case: a body with no angle brackets that
    some server labelled ``text/plain`` (or did not label at all), decoded with
    ``errors="replace"`` into a run of replacement characters. Handing that to
    the verifier asks a model whether mojibake supports a claim, which is the
    same failure as handing it raw PDF -- just without the magic bytes that
    make it obvious.
    """
    if not text:
        return False
    bad = sum(1 for c in text if c == "\ufffd" or (ord(c) < 0x20 and c not in "\t\n\r"))
    return bad / len(text) > _MAX_UNDECODABLE_RATIO


#: ``<meta charset="...">`` or ``<meta http-equiv="Content-Type" content="...;
#: charset=...">`` in the head of an HTML document, and the ``encoding`` of an
#: XML declaration. Matched on bytes: the declaration is ASCII whatever the
#: document's own encoding is, which is the point of it.
_DECLARED_CHARSET = re.compile(
    rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)|<\?xml[^>]*encoding=["']([A-Za-z0-9_.:-]+)""",
    re.IGNORECASE,
)
#: How far into the body a charset declaration is looked for. The HTML standard
#: tells browsers to scan the first 1024 bytes; real pages put it behind a long
#: run of ``<link>`` and ``<script>`` tags often enough that this reads further.
_CHARSET_SCAN_BYTES = 8192


def response_encoding(resp):
    """The charset to decode ``resp.content`` with.

    ``requests`` sets ``resp.encoding`` to ``ISO-8859-1`` for any ``text/*``
    body whose ``Content-Type`` header names no charset (an RFC 2616 default
    that HTML pages have not followed in twenty years), and the readers here
    used to pass that straight through. A UTF-8 page served with a bare
    ``text/html`` header, which is what static hosting commonly sends, was then
    decoded as Latin-1: every en dash, curly quote and emoji became mojibake,
    the relevance verifier was shown the mojibake, and the verbatim quote it
    gave back could never be found in it (issue #439; five of five claims
    against one such site were reported unverifiable, and all five verified
    once decoded correctly).

    The order is the one a browser uses. A charset in the header wins. Failing
    that, the document's own declaration (``<meta charset>``, the http-equiv
    form, or an XML declaration), provided Python knows the codec. Failing
    that, the body is tried as UTF-8 and kept if it decodes without error,
    since UTF-8 is self-validating and legacy-encoded text with any non-ASCII
    bytes almost never passes. Only then the library's guess
    (``apparent_encoding``, where the response type offers one), and last of
    all whatever the response said, or UTF-8.

    A PDF never gets here in a way that matters -- its bytes go to the PDF
    extractor -- but the function is cheap on one anyway, because the
    declaration scan reads only the first few kilobytes and the UTF-8 probe
    is skipped when the body does not look like text.
    """
    headers = getattr(resp, "headers", None) or {}
    content_type = str(headers.get("Content-Type") or "")
    declared_in_header = "charset=" in content_type.lower()
    own = getattr(resp, "encoding", None)
    if declared_in_header and own:
        return own

    raw = getattr(resp, "content", b"") or b""
    if isinstance(raw, str):
        return own or "utf-8"
    if looks_like_pdf(content_type, None, raw):
        return own or "utf-8"

    match = _DECLARED_CHARSET.search(raw[:_CHARSET_SCAN_BYTES])
    if match:
        declared = (match.group(1) or match.group(2) or b"").decode("ascii", "ignore")
        try:
            return codecs.lookup(declared).name
        except LookupError:
            pass

    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    apparent = getattr(resp, "apparent_encoding", None)
    if apparent:
        return apparent
    return own or "utf-8"


def extract_response_text(raw, content_type=None, url=None, encoding="utf-8"):
    """Turn a fetched response body into readable text.

    ``raw`` is bytes (``resp.content``). Dispatches on PDF vs HTML/text and
    returns ``(text, kind)`` where ``kind`` is "pdf", "html", or "text".

    An empty ``text`` means "could not read this", which callers must report
    distinctly from "read it and it does not support the claim".

    The two-value form, for callers that only need the text. A caller that will
    judge a claim against the text wants
    :func:`extract_response_text_with_coverage`, which also says how much of the
    document the text covers.
    """
    text, kind, _coverage = extract_response_text_with_coverage(
        raw, content_type=content_type, url=url, encoding=encoding
    )
    return text, kind


def extract_response_text_with_coverage(
    raw, content_type=None, url=None, encoding="utf-8"
):
    """``(text, kind, coverage)`` for a fetched response body.

    ``coverage`` is :func:`read_coverage`. For HTML and plain text the whole
    body is read, so it never reports ``partial``; for a PDF it carries the page
    counts, and ``partial`` is True when :data:`PDF_MAX_SECONDS` or :data:`PDF_MAX_PAGES`
    stopped the read.
    """
    if isinstance(raw, str):
        decoded, raw = raw, raw.encode(encoding, errors="replace")
    else:
        decoded = None

    if looks_like_pdf(content_type, url, raw):
        text, coverage = extract_pdf_text_with_coverage(raw)
        return text, "pdf", coverage

    text, kind = _extract_markup_or_text(raw, decoded, content_type, encoding)
    return text, kind, read_coverage(kind)


def _extract_markup_or_text(raw, decoded, content_type, encoding):
    if decoded is None:
        decoded = raw.decode(encoding or "utf-8", errors="replace")

    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith("text/") and "html" not in ctype:
        # Plain text, JSON, CSV, XML: no markup to strip, use it as-is.
        if ctype in ("application/json", "text/plain") or "xml" in ctype:
            text = decoded.strip()
            return ("" if looks_like_binary(text) else text), "text"

    if "<" in decoded and ">" in decoded:
        _title, body = extract_article(decoded)
        if body:
            return body, "html"
        # Markup that yielded no article body (JS-only render, bot wall). Fall
        # through rather than handing raw tags to a text model.
        return "", "html"

    text = decoded.strip()
    return ("" if looks_like_binary(text) else text), "text"


# --- Claim-centered excerpt selection -------------------------------------

_STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "their",
    "there",
    "these",
    "those",
    "which",
    "while",
    "would",
    "could",
    "should",
    "being",
    "between",
    "during",
    "through",
    "under",
    "where",
    "when",
    "other",
    "another",
    "because",
    "before",
    "below",
    "https",
    "http",
    "www",
    "com",
    "html",
}

_NUM_RE = re.compile(r"\d[\d,.]*%?")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']+")


def claim_terms(claim):
    """Distinctive terms from a claim, used to locate the supporting passage.

    Numbers and acronyms are the highest-signal tokens in the factual claims
    this pipeline checks ("9,000 generators", "eGRID", "300 GHz"), so they are
    kept separately from ordinary long words and scored higher.
    """
    claim = claim or ""
    numbers = {
        n.strip(".,") for n in _NUM_RE.findall(claim) if any(c.isdigit() for c in n)
    }
    words = set()
    for w in _WORD_RE.findall(claim):
        if len(w) >= 2 and w.isupper():  # acronyms: EPA, ICNIRP, WHO
            words.add(w.lower())
        elif len(w) >= 5 and w.lower() not in _STOPWORDS:
            words.add(w.lower())
    return numbers, words


#: Per-term ceiling on how much repetition can add to a window's score. Without
#: it, one word repeated down a long table outweighs a window matching several
#: distinct claim terms; with it, breadth and density both count.
_TERM_COUNT_CAP = 5


def select_excerpt(text, claim, head=4000, tail=1000, hints=()):
    """Return the region of ``text`` most likely to contain support for ``claim``.

    Blind head+tail truncation reliably misses the supporting passage in long
    documents — a 60-page guidelines PDF puts the relevant limit in the middle.
    This scores fixed-size windows by how many of the claim's distinctive terms
    they contain and returns the best one, falling back to head+tail truncation
    when the claim has no usable terms or none of them appear.

    Deliberately simple and deterministic: sliding-window term counting, no
    embeddings, no index. It only has to beat "always the first 4000 chars".

    Scoring counts *occurrences*, not mere presence. Presence-scoring was the
    original design and it silently reintroduced the head bias this function
    exists to remove: in a document about one topic, the topic's vocabulary
    appears in every window, so windows tie — and a strictly-greater comparison
    hands a tie to the earliest, i.e. to the head.

    Measured on Furuno's 9-page GPS rollover PDF (2026-09-19). For the claim
    "lists the chips whose rollover date can be changed by command", six of the
    eight candidate windows tied at score 5 on {"1", "command", "rollover"}, so
    the head-most won at offset 2500 — while "command" occurs 17 times in
    offsets 9793-11889, the Chapter 6 list that actually answers the claim.
    Counting occurrences separates 17 from 1 and picks the right passage.

    Each term's count is capped so one repeated word cannot swamp a window that
    matches several distinct terms; the score still rewards both breadth and
    density.
    """
    return select_excerpt_with_window(text, claim, head=head, tail=tail, hints=hints)[0]


def select_excerpt_with_window(text, claim, head=4000, tail=1000, hints=()):
    """:func:`select_excerpt`, plus a record of how much of ``text`` it kept.

    Returns ``(excerpt, window)`` where ``window`` is::

        {"chars_total": len(text), "chars_read": <characters handed on>,
         "windowed": <True when part of the text was left out>,
         "how": "whole" | "best_window" | "head_tail"}

    ``windowed`` is the fact a verdict has to carry: a negative answer about a
    windowed document is an answer about the window, not the document. The
    window is chosen by the claim's own terms, which is a real search and not a
    blind cut, but it is still a 20,000-character sample of what may be a
    600,000-character council packet.

    ``hints`` are other texts that say where the evidence probably is: the
    draft's own citation entry for the URL (an author who wrote "p. 110 (Council
    Bluffs, IA: 1,410.3 million gallons withdrawn)" has named the row) and the
    fact-check model's quote from the page. Their terms help choose the window
    at a lower weight than the claim's own; they never reach the verifier.
    The window may come back as several stretches of the document
    (``how: "best_windows"``) when the claim's terms sit far apart.
    """
    from ci_core import redact

    text = text or ""
    budget = head + tail
    total = len(text)
    if total <= budget:
        return text, {
            "chars_total": total,
            "chars_read": total,
            "windowed": False,
            "how": "whole",
        }

    def _head_tail():
        return redact.truncate_excerpt(text, head=head, tail=tail), {
            "chars_total": total,
            "chars_read": min(total, budget),
            "windowed": True,
            "how": "head_tail",
        }

    lowered = text.lower()
    numbers, words = claim_terms(claim)
    hint_numbers, hint_words = set(), set()
    for hint in hints or ():
        n, w = claim_terms(hint)
        hint_numbers |= n
        hint_words |= w
    claim_weighted = _weighted_terms(numbers, words, lowered, total, budget)
    hint_weighted = _weighted_terms(
        hint_numbers - numbers, hint_words - words, lowered, total, budget
    )
    if not claim_weighted and not hint_weighted:
        return _head_tail()

    # The claim chooses first and the hints only add to what it found. Mixed
    # into one pool, a long citation entry ("[9] ... p. 110 (Council Bluffs ...)
    # and endnote 66, p. 117 (golf ...)") outvoted the claim it was cited for
    # and pulled the window off the passage the claim's own terms had found.
    reserve = _HINT_PASSAGES if hint_weighted else 0
    chosen = _pick_passages(
        claim_weighted, [], total, budget, _PASSAGES_PER_BUDGET - reserve
    )
    chosen = _pick_passages(
        hint_weighted, chosen, total, budget, _PASSAGES_PER_BUDGET - len(chosen)
    )
    if not chosen:
        return _head_tail()
    spans = _grow_spans(sorted(chosen), total, budget)

    parts = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            parts.append(f"...[{start - cursor} chars omitted]...")
        parts.append(text[start:end])
        cursor = end
    if cursor < total:
        parts.append(f"...[{total - cursor} chars omitted]...")
    return "\n".join(parts), {
        "chars_total": total,
        "chars_read": sum(end - start for start, end in spans),
        "windowed": True,
        # One stretch keeps the name it always had, so a report or test that
        # reads it is unchanged; several stretches say so.
        "how": "best_window" if len(spans) == 1 else "best_windows",
    }


#: Passages held back for the hints when there are any. A hint says where the
#: evidence probably is (the draft's citation entry for this URL, a fact-check
#: model's quote from the page); the claim is what is being checked, so it gets
#: the rest, and the hints steer the window, never the verdict.
_HINT_PASSAGES = 2

#: A later passage is kept only if what it adds is at least this share of the
#: first passage's score; below it, the budget is better spent as context
#: around what was already found than on a distant, weak match.
_LATER_PASSAGE_SHARE = 0.35

#: Base weight of a figure against a word. A matching figure is far stronger
#: evidence of the right passage than a matching common-ish term.
_NUMBER_WEIGHT = 3.0

#: The passages a long document is cut into while choosing what to show, as a
#: fraction of the budget. Four 5,000-character passages fit the verifier's
#: 20,000; a claim whose figures sit in two places gets both.
_PASSAGES_PER_BUDGET = 4

#: How much a term still counts in a later passage once an earlier chosen one
#: already holds it. Low, so the second pick goes to whatever the first did not
#: cover rather than to another copy of the same match.
_COVERED_DISCOUNT = 0.25


def _weighted_terms(numbers, words, lowered, total, budget):
    """``[(positions, weight), ...]`` for the terms that occur in ``lowered``.

    Weighted by rarity in this document, which is what the old scorer lacked.
    It counted every claim word alike, so in a 644,000-character EPA technical
    document the claim's generic vocabulary ("cooling", "energy", "plants")
    pulled the window to wherever those were densest, 58,000 characters from
    the one sentence that answers it ("The disadvantages of dry cooling include
    an increase in noise generation", issue #440). A term that occurs twice in
    a long document is a pointer to the passage; one that occurs on every page
    says nothing about where to look. The weight is an inverse document
    frequency over passage-sized pieces: ``log(1 + pieces / min(count,
    pieces))``.
    """
    piece = max(budget // _PASSAGES_PER_BUDGET, 1)
    pieces = max(total // piece, 1)
    weighted = []
    for group, base in ((numbers, _NUMBER_WEIGHT), (words, 1.0)):
        for term in sorted(group):
            needle = term.lower()
            positions = [m.start() for m in re.finditer(re.escape(needle), lowered)]
            if not positions:
                continue
            rarity = math.log(1 + pieces / min(len(positions), pieces))
            weighted.append((positions, base * rarity))
    return weighted


def _window_score(terms, start, end, covered=None):
    """Score of ``[start, end)``: each term present adds its weight, once, plus
    a little for repeats (``log``), so breadth beats one word said many times.
    ``covered`` holds the indexes of terms an already chosen passage holds."""
    score = 0.0
    for index, (positions, weight) in enumerate(terms):
        count = bisect.bisect_left(positions, end) - bisect.bisect_left(
            positions, start
        )
        if not count:
            continue
        repeats = 1 + math.log(min(count, _TERM_COUNT_CAP))
        if covered and index in covered:
            weight *= _COVERED_DISCOUNT
        score += weight * repeats
    return score


def _pick_passages(terms, chosen, total, budget, limit):
    """``chosen`` plus up to ``limit`` more passage-sized spans scored by ``terms``.

    Greedy: each pick is the best window that does not overlap one already
    chosen, scored with the terms an earlier pick of this call covered
    discounted, so the second pick goes to what the first did not hold (a 2023
    share on one page and a 2028 projection on another). A pick that adds less
    than ``_LATER_PASSAGE_SHARE`` of this call's first pick ends it. The caller
    shares the rest of the budget out around the picks (``_grow_spans``), so a
    claim answered in one place still gets one long stretch of context.
    """
    chosen = list(chosen)
    if not terms or limit <= 0:
        return chosen
    piece = max(budget // _PASSAGES_PER_BUDGET, 1)
    step = max(piece // 4, 1)
    starts = range(0, max(total - piece, 0) + 1, step)
    covered = set()
    first = None
    for _ in range(limit):
        best, best_start = 0.0, None
        for start in starts:
            end = start + piece
            if any(start < c_end and end > c_start for c_start, c_end in chosen):
                continue
            score = _window_score(terms, start, end, covered)
            if score > best:
                best, best_start = score, start
        if best_start is None:
            break
        if first is not None and best < _LATER_PASSAGE_SHARE * first:
            break
        first = best if first is None else first
        span = (best_start, min(best_start + piece, total))
        chosen.append(span)
        for index, (positions, _weight) in enumerate(terms):
            if bisect.bisect_left(positions, span[1]) > bisect.bisect_left(
                positions, span[0]
            ):
                covered.add(index)
    return chosen


def _grow_spans(spans, total, budget):
    """Widen ``spans`` evenly, merging any that meet, until ``budget`` is used."""
    spans = [list(s) for s in spans]
    while True:
        used = sum(e - s for s, e in spans)
        spare = budget - used
        if spare <= 0:
            break
        share = max(spare // (2 * len(spans)), 1)
        grew = False
        for span in spans:
            new_start = max(0, span[0] - share)
            new_end = min(total, span[1] + share)
            if (new_start, new_end) != (span[0], span[1]):
                grew = True
            span[0], span[1] = new_start, new_end
        merged = [spans[0]]
        for span in spans[1:]:
            if span[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], span[1])
            else:
                merged.append(span)
        spans = merged
        if not grew:
            break
    # Growth in whole shares can overshoot by a few characters; trim the last.
    over = sum(e - s for s, e in spans) - budget
    if over > 0:
        spans[-1][1] -= over
    return [(s, e) for s, e in spans]
