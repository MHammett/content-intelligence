"""Is the quote a fact-check model gave actually on the page it cites? (#344)

``prompts/fact_check.txt`` asks every ``confirmed``, ``outdated`` and
``contradicted`` verdict for a ``source_url`` and a ``supporting_quote`` "copied
verbatim from that page". Consolidation (``_demote_unevidenced_verdict``) holds
the verdict to having both and never opens the page, so a real URL beside an
invented sentence kept the verdict. This module opens the page.

It runs once, before consolidation, over the raw ensemble results, and returns
``{(url, quote): {"found": True | False | None, ...}}``. Consolidation reads that
and demotes on ``found is False``; it does no fetching itself, which keeps it
pure and keeps ``--replay`` over a saved capture able to run it offline (an
empty dict changes nothing).

**Only a page that was read, and did not contain the quote, is a miss.** A 404,
a refusal, a timeout, a bot wall, a near-empty body (a script-rendered page), a
PDF longer than the extraction cap: each says nothing about the quote, so each
is ``found: None`` and the verdict stays where it was. The cost of the opposite
mistake is a correct verdict demoted on the strength of our own extraction
failing, which is the same false statement about a source that
``resolver._extract_fetched`` and ``_MIN_VERIFIABLE_CHARS`` exist to prevent.

**Matching is on letters and digits only** (``_compact``): NFKC, casefolded,
everything else dropped. The resolver's own ``_quote_is_grounded`` is a strict
whitespace-and-case substring test, and it is right to be: it guards a verifier
verdict a hostile page wants to steer, so loosening it "would reopen the hole".
This is a different question with the opposite cost. A model copying a sentence
"verbatim" off a page routinely loses curly quotes, a non-breaking space, a
dash, markdown the extractor added, the ``|`` between table cells, or a
hyphenated line break from a PDF, and none of those is an invented quote.
Matching on the words and numbers alone cannot be fooled into accepting
anything that is not on the page in the same order; the only thing it accepts
that a strict test would not is punctuation and spacing the page did not have.
(It also ignores word boundaries, so a very short quote such as "9 units" will
match "19 units". Real quotes are sentences, and that looseness can only keep a
verdict, never demote one.)

The fetch reuses the resolver's: SSRF-guarded ``safe_get``, the same text
extraction, and the same TLS-fingerprint retry on a 403. It does not use the
archive.org fallback (a quote found in a year-old snapshot is not a quote on the
page the draft cites, and the lookups would multiply with the number of URLs).
Section 9 reads the same pages for its own purposes. The pipeline hands both one
``page_cache.PageCache`` so each page is fetched once per run (#356).
"""

import html
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ci_core import extract
from ci_core.concurrency import run_all_bounded
from ci_core.http import UnsafeURLError, safe_get

from ci_article_review import consolidation

from . import resolver, wayback

log = logging.getLogger(__name__)

#: Bound on concurrent page fetches, the same bound the resolver uses.
_MAX_PARALLEL = resolver._MAX_PARALLEL

#: Per-request timeout handed to ``safe_get``.
_FETCH_TIMEOUT = 15

#: Safety net for one URL, above ``_FETCH_TIMEOUT`` for the same reason
#: ``resolver._RESOLVE_TIMEOUT_SECONDS`` is: it exists for what the request
#: timeout does not cover (a stalled DNS lookup, a slow PDF parse). The parse
#: has a budget of its own, ``extract.PDF_MAX_SECONDS``, which this sits above.
_JOB_TIMEOUT_SECONDS = 60 + extract.PDF_MAX_SECONDS

#: Most distinct URLs fetched in one run. A ``maximum`` run across six models
#: cites a few dozen; the cap is a bound on a pathological response, and a URL
#: past it is reported ``unchecked`` rather than skipped silently.
_MAX_URLS = 200

#: PDF pages read. The same limits as the citation verifier's — one number for
#: the whole pipeline, kept in ``extract.PDF_MAX_PAGES`` and
#: ``extract.PDF_MAX_SECONDS``. The two used to differ (40 pages there, 400
#: here), and the lower one produced a false negative: a claim cited to page 120
#: of a council packet was reported as unsupported by a document the verifier
#: had not read past page 40. A miss past either limit is still reported as our
#: limit and not the model's invention — see ``PageRead.partial``.
_PDF_MAX_PAGES = extract.PDF_MAX_PAGES

#: A piece of a quote shorter than this (after ``_compact``) is not required to
#: match, provided a longer piece is. It is what is left around an ellipsis or an
#: attribution, and a handful of characters says nothing in either direction.
_MIN_PIECE_CHARS = 8

#: Where a quote stops being one continuous passage: "...", ". . .", an ellipsis
#: character, the bracketed forms quotations use, and a line break. The last is
#: the model marking a paragraph or block boundary, and the page has things
#: there (a heading, a caption, a run of sentences the quote skipped). Measured
#: on a saved run, 2026-10-06: a model quoted two non-adjacent paragraphs of an
#: about page on separate lines with the text between them left out, and every
#: line was on the page word for word. Treating that as one passage called a
#: real quote invented.
_HELLIP = chr(0x2026)
_ELLIPSIS = re.compile(
    r"\[\s*(?:\.\s*){2,}\]|\[\s*"
    + _HELLIP
    + r"\s*\]|(?:\.\s*){3,}|"
    + _HELLIP
    + r"|\s*[\r\n]+\s*"
)


@dataclass
class PageRead:
    """What fetching one page yielded.

    ``status`` is ``read`` (``texts`` hold the page, and a quote missing from
    them is a miss) or anything else (``failed``, ``too_short``,
    ``access_wall``, ``unsafe``): nothing was established. ``partial`` marks a
    document that was only read in part, so a miss in it is not a miss.
    """

    status: str
    texts: list = field(default_factory=list)
    partial: bool = False
    detail: str = ""
    #: Where the fetch ended up after following redirects, when it got that far
    #: ("" otherwise). A grounded model cites through a forwarder, so this is the
    #: only place the page a verdict names is learned (#359).
    final_url: str = ""


def _compact(text):
    """Letters and digits of ``text``, NFKC-folded and casefolded, nothing else."""
    folded = unicodedata.normalize("NFKC", html.unescape(str(text or ""))).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def quote_found(quote, texts):
    """Whether ``quote`` is in any of ``texts``: True, False, or None for "no idea".

    An ellipsis or a line break splits the quote into the passages it joins, and
    every passage of a useful length has to be on the page, in the same view of
    it, though not necessarily next to each other. ``None``
    when nothing is left to compare (a quote that was only an ellipsis), which
    the caller treats like any other check that could not be made.
    """
    pieces = [_compact(p) for p in _ELLIPSIS.split(str(quote or ""))]
    pieces = [p for p in pieces if p]
    # Short fragments around an ellipsis are noise, but a quote that is short
    # throughout is still the whole claim to be checked.
    pieces = [p for p in pieces if len(p) >= _MIN_PIECE_CHARS] or pieces
    if not pieces:
        return None
    for text in texts:
        haystack = _compact(text)
        if all(piece in haystack for piece in pieces):
            return True
    return False


class _AllText(HTMLParser):
    """Every visible text node on a page, boilerplate and tables included.

    The resolver's article extractor drops ``nav``, ``header``, ``aside`` and
    ``footer`` and, through trafilatura, anything it takes for a sidebar: right
    for deciding what a page *argues*, wrong for deciding what it *says*. A
    figure in a sidebar table is on the page. Only what a reader never sees
    (script, style, templates) is dropped.
    """

    _HIDDEN = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._HIDDEN and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth and data.strip():
            self.parts.append(data)


def all_page_text(markup):
    """Visible text of an HTML document, or "" if it will not parse."""
    parser = _AllText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page is "no second view"
        return ""
    return " ".join(parser.parts)


def _pdf_text(raw):
    """``(text, partial)`` for PDF bytes; ``partial`` when a read limit cut it short."""
    text, coverage = extract.extract_pdf_text_with_coverage(
        raw, max_pages=_PDF_MAX_PAGES
    )
    if not coverage.get("partial") and extract.looks_like_scanned_pdf(
        text, coverage.get("pages_read")
    ):
        # A scan's stamp is not its text; "" makes it "too short", not a miss.
        return "", False
    return text, bool(coverage.get("partial"))


def _read_response(resp, url):
    """Turn a fetched response into a ``PageRead``."""
    content_type = resp.headers.get("Content-Type")
    raw = resp.content
    if extract.looks_like_pdf(content_type, url, raw):
        text, partial = _pdf_text(raw)
        texts, kind = [text], "pdf"
    else:
        text, kind = resolver._extract_fetched(resp, url)
        texts, partial = [text], False
        # The same charset choice as ``_extract_fetched`` (issue #439), so the
        # two views of one page agree on what its dashes and quotes are.
        encoding = extract.response_encoding(resp)
        if kind == "html":
            decoded = raw.decode(encoding, errors="replace")
            texts.append(all_page_text(decoded))
    return _classify(texts, partial)


def _classify(texts, partial):
    texts = [t for t in texts if t]
    longest = max(texts, key=len, default="")
    if extract.looks_like_access_wall(longest):
        return PageRead(status="access_wall", detail="a bot or paywall interstitial")
    if len(longest.strip()) < resolver._MIN_VERIFIABLE_CHARS:
        return PageRead(
            status="too_short", detail="the page held almost no readable text"
        )
    return PageRead(status="read", texts=texts, partial=partial)


def _get_checked(url, timeout):
    resp = safe_get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_page(url, timeout=_FETCH_TIMEOUT, cache=None):
    """Fetch ``url`` and return what a quote can be looked for in. Never raises.

    ``cache`` is the run's ``page_cache.PageCache``: the response is kept in it
    for Section 9, and a page another caller already fetched is not fetched again.
    """
    landed = ""
    try:
        if cache is None:
            resp = _get_checked(url, timeout)
        else:
            resp = cache.get_page(url, lambda: _get_checked(url, timeout), timeout)
        landed = getattr(resp, "url", "") or ""
        page = _read_response(resp, url)
        page.final_url = landed
        return page
    except UnsafeURLError:
        return PageRead(status="unsafe", detail="a non-public address")
    except Exception as exc:  # noqa: BLE001 - any failure is "could not read"
        # A refusal still carries the response, and with it the page a
        # forwarder pointed at: that is all the scope vote needs.
        landed = landed or getattr(getattr(exc, "response", None), "url", "") or ""
        if wayback.fallback_reason_for_exception(exc) == "blocked":
            escalated = resolver._escalate(url, timeout, cache)
            if escalated is not None:
                final, content, _kind, _coverage = escalated
                page = _classify([content], False)
                page.final_url = final or landed
                return page
        return PageRead(status="failed", detail=_describe(exc), final_url=landed)


def _describe(exc):
    cert = wayback.certificate_failure_summary(exc)
    return cert or f"{type(exc).__name__}: {exc}"[:160]


#: Search-provider forwarders that name no page. Gemini's grounding metadata
#: returns every citation as one, and the token differs per citation whatever
#: the page behind it (the documented behaviour of Grounding with Google Search;
#: see the note above ``pipeline._URL_REDIRECTOR_MARKERS``). A fetch that ended
#: on another forwarder named nothing.
_FORWARDER_MARKER = "grounding-api-redirect"


def _landing_page(url, page):
    """Where ``url`` led, when that is a different page than the one cited.

    Empty when the fetch recorded nothing, landed where it started (a
    trailing slash or a scheme does not make a new page) or ended on another
    forwarder, which an expired wrapper does.
    """
    landed = getattr(page, "final_url", "") or ""
    if not landed or _FORWARDER_MARKER in landed.lower():
        return ""
    if consolidation.url_key(landed) == consolidation.url_key(url):
        return ""
    return landed


def _targets(results):
    """``{(url, quote): None}`` for every verdict a page can be held to.

    The same verdicts, by the same reading of URL and quote, that
    ``consolidation._demote_unevidenced_verdict`` keeps after its own rules; a
    verdict that fails those never gets this far, so fetching for it would be
    wasted. Ordered by first appearance so ``_MAX_URLS`` cuts predictably.
    """
    targets = {}
    for (_model, domain), result in (results or {}).items():
        if domain != "fact_check" or not isinstance(result, dict):
            continue
        data = result.get("data")
        if result.get("failed") or not isinstance(data, dict):
            continue
        for bucket in consolidation._VERDICT_BUCKETS:
            items = data.get(bucket)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = consolidation._evidence_url(item)
                quote = consolidation._evidence_quote(item)
                if url and quote:
                    targets.setdefault((url, quote), None)
    return list(targets)


def check_quotes(results, fetch=None, max_parallel=_MAX_PARALLEL, cache=None):
    """Look for each fact-check verdict's quote on the page it cites.

    ``results`` is the ensemble's ``{(model, domain): result}`` dict, untouched.
    ``fetch`` is for tests: ``fetch(url, timeout) -> PageRead``. ``cache`` is the
    run's ``page_cache.PageCache``, handed on to ``fetch_page`` so Section 9 can
    reuse these fetches; it is not used when ``fetch`` is given.

    Returns ``(checks, summary)``. ``checks`` maps ``(url, quote)`` to
    ``{"found": True | False | None, "reason": str, "chars": int}``; ``found`` is
    ``None`` whenever nothing was established. ``summary`` counts the outcomes
    for the report.
    """
    if fetch is None:
        fetch = lambda url, timeout: fetch_page(url, timeout, cache=cache)  # noqa: E731
    pairs = _targets(results)
    urls = list(dict.fromkeys(url for url, _quote in pairs))
    fetched_urls, over_limit = urls[:_MAX_URLS], set(urls[_MAX_URLS:])
    if over_limit:
        log.warning(
            "Quote check: %d distinct URL(s) cited, checking the first %d; the "
            "rest are left unchecked.",
            len(urls),
            _MAX_URLS,
        )

    jobs = [
        (url, lambda url=url: fetch(url, _FETCH_TIMEOUT), _JOB_TIMEOUT_SECONDS)
        for url in fetched_urls
    ]
    outcomes = run_all_bounded(jobs, max_parallel=max_parallel) if jobs else {}

    pages = {}
    for url, (value, error) in outcomes.items():
        if error is not None:
            pages[url] = PageRead(status="failed", detail=_describe(error))
        else:
            pages[url] = value

    checks = {}
    for url, quote in pairs:
        page = pages.get(url)
        if page is None:
            checks[(url, quote)] = {
                "found": None,
                "reason": "not fetched: over the per-run URL limit",
            }
            continue
        if page.status != "read":
            checks[(url, quote)] = {
                "found": None,
                "reason": f"page not read ({page.status}: {page.detail})",
            }
        else:
            found = quote_found(quote, page.texts)
            if found is False and page.partial:
                checks[(url, quote)] = {
                    "found": None,
                    "reason": "document longer than the pages read",
                }
            else:
                checks[(url, quote)] = {
                    "found": found,
                    "reason": "" if found is not None else "nothing to compare",
                    "chars": max((len(t) for t in page.texts), default=0),
                }
        landed = _landing_page(url, page)
        if landed:
            checks[(url, quote)]["final_url"] = landed

    outcome_counts = Counter(
        {True: "found", False: "not_found", None: "unchecked"}[c["found"]]
        for c in checks.values()
    )
    summary = {
        "verdicts": len(checks),
        "urls": len(urls),
        "found": outcome_counts["found"],
        "not_found": outcome_counts["not_found"],
        "unchecked": outcome_counts["unchecked"],
    }
    log.info(
        "Quote check: %d verdict quote(s) across %d page(s): %d on the page, "
        "%d not on the page, %d could not be checked",
        summary["verdicts"],
        summary["urls"],
        summary["found"],
        summary["not_found"],
        summary["unchecked"],
    )
    return checks, summary
