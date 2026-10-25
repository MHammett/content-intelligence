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

import logging
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


def select_excerpt(text, claim, head=4000, tail=1000):
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
    return select_excerpt_with_window(text, claim, head=head, tail=tail)[0]


def select_excerpt_with_window(text, claim, head=4000, tail=1000):
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

    numbers, words = claim_terms(claim)
    if not numbers and not words:
        return _head_tail()

    lowered = text.lower()
    step = max(budget // 8, 1)
    best_score, best_start = 0, None
    for start in range(0, max(total - budget, 0) + step, step):
        end = start + budget
        if start >= len(lowered):
            break
        # Numbers weigh more than words: a matching figure is far stronger
        # evidence of the right passage than a matching common-ish term.
        score = sum(
            3 * min(lowered.count(n.lower(), start, end), _TERM_COUNT_CAP)
            for n in numbers
        )
        score += sum(min(lowered.count(w, start, end), _TERM_COUNT_CAP) for w in words)
        if score > best_score:
            best_score, best_start = score, start

    if best_start is None:
        return _head_tail()

    excerpt = text[best_start : best_start + budget]
    if best_start > 0:
        excerpt = f"...[{best_start} chars omitted]...\n{excerpt}"
    remaining = total - (best_start + budget)
    if remaining > 0:
        excerpt = f"{excerpt}\n...[{remaining} chars omitted]..."
    return excerpt, {
        "chars_total": total,
        "chars_read": min(budget, total - best_start),
        "windowed": True,
        "how": "best_window",
    }
