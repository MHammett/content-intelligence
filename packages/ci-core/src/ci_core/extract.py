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


def extract_pdf_text(raw, max_pages=40):
    """Extract text from PDF bytes via pypdf. Returns "" when unreadable.

    ``max_pages`` bounds the work on very long documents (some primary sources
    here run to hundreds of pages); the leading pages are what a citation
    excerpt search needs, and parsing all of them is pure latency.

    Returns "" — never raises — when pypdf is absent, the bytes are not a
    parseable PDF, or the PDF is a pure scan with no text layer. The caller
    distinguishes that from a genuine content mismatch.
    """
    try:
        import pypdf
    except ImportError:
        log.warning(
            "pypdf is not installed — PDF citation sources cannot be verified. "
            "Install it to enable PDF content verification."
        )
        return ""

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
                return ""
        pages = reader.pages[:max_pages]
        chunks = []
        for page in pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # pragma: no cover - defensive, per-page corruption
                continue
    except Exception as exc:
        log.warning(f"PDF text extraction failed: {exc}")
        return ""

    text = "\n\n".join(c.strip() for c in chunks if c.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
    """
    if isinstance(raw, str):
        decoded, raw = raw, raw.encode(encoding, errors="replace")
    else:
        decoded = None

    if looks_like_pdf(content_type, url, raw):
        return extract_pdf_text(raw), "pdf"

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


def select_excerpt(text, claim, head=4000, tail=1000):
    """Return the region of ``text`` most likely to contain support for ``claim``.

    Blind head+tail truncation reliably misses the supporting passage in long
    documents — a 60-page guidelines PDF puts the relevant limit in the middle.
    This scores fixed-size windows by how many of the claim's distinctive terms
    they contain and returns the best one, falling back to head+tail truncation
    when the claim has no usable terms or none of them appear.

    Deliberately simple and deterministic: sliding-window term counting, no
    embeddings, no index. It only has to beat "always the first 4000 chars".
    """
    from ci_core import redact

    text = text or ""
    budget = head + tail
    if len(text) <= budget:
        return text

    numbers, words = claim_terms(claim)
    if not numbers and not words:
        return redact.truncate_excerpt(text, head=head, tail=tail)

    lowered = text.lower()
    step = max(budget // 4, 1)
    best_score, best_start = 0, None
    for start in range(0, max(len(text) - budget, 0) + step, step):
        window = lowered[start : start + budget]
        if not window:
            break
        # Numbers weigh more than words: a matching figure is far stronger
        # evidence of the right passage than a matching common-ish term.
        score = sum(3 for n in numbers if n.lower() in window)
        score += sum(1 for w in words if w in window)
        if score > best_score:
            best_score, best_start = score, start

    if best_start is None:
        return redact.truncate_excerpt(text, head=head, tail=tail)

    excerpt = text[best_start : best_start + budget]
    if best_start > 0:
        excerpt = f"...[{best_start} chars omitted]...\n{excerpt}"
    remaining = len(text) - (best_start + budget)
    if remaining > 0:
        excerpt = f"{excerpt}\n...[{remaining} chars omitted]..."
    return excerpt
