"""Turn what a run could not settle into a worklist the author can work through.

The report is honest about its gaps and stops there. Section 9 says 5 citations
were fetched and could not be read; Section 2 says 8 claims need a primary
source. Both are true, both are useful, and neither tells the author what to
*do* — so the gaps get re-read every run and cleared in none of them.

This module is the missing half. It reads the same report dict everything else
renders from and emits a ranked list of actions, each one naming what is needed,
why the pipeline could not get it, and the most specific next step available.

Three things it does that a straight re-listing of the gaps would not:

**It collapses by target.** In the 2026-09-05 run that motivated this, 8 claims
carried a URL that returned 403 — and 6 of them were the *same* URL. Two pages
opened in a browser clear all 8. Reporting "8 items" there would be dishonest
about the effort in exactly the direction that gets a worklist abandoned.

**It recovers the URL a human can actually open.** The ``url`` field on a
citation is whatever the fact-check model handed over, which for a grounded
Gemini answer is a ``vertexaisearch.cloud.google.com`` redirect that expires
about 30 days after the run. Two better addresses exist and neither is that
field: for a fetch that landed, the resolver records where it landed in
``final_url``; for one that was refused, the address it was refused at is in
the ``note``, because that is where ``requests`` puts it. Both are preferred
over the stored URL, so the author gets ``bianchihonda.com/...`` rather than an
opaque blob with a clock on it.

**It separates the permanent from the temporary.** An item is blocked either by
something no tool will ever do (judgement, a paywall, a document that is not
online, the author's own observation) or by something a tool could do that this
pipeline cannot (render JavaScript, read a scanned PDF, browse with a real
session). The first is the author's, forever. The second is a roadmap, and the
bottom of the rendered list totals it up as one.

Nothing here restates a finding. Every item names the section it came from and
clips the claim text, because the full record is already three screens up.

The only import is ``adapters.citation.disposition``, a leaf that pulls nothing
in behind it — so ``report_markdown`` can import *this* and stay a
dependency-free renderer over a plain dict.
"""

import re

# The one package import, and it costs this module nothing: ``disposition``
# is a leaf — a tuple, a lookup and a pure function, importing nothing
# itself. It exists precisely because two callers classifying citations
# independently drifted apart; a third doing so would be the same bug.
from .adapters.citation.disposition import disposition as _disposition

#: Ranked items rendered before the list is cut off. A list nobody finishes is
#: worse than a short one that gets cleared — past ~a dozen actions the author
#: is reading a backlog, not a worklist. What is cut is named and counted rather
#: than dropped silently, and it is always the lowest-ranked tail.
DEFAULT_LIMIT = 12

#: An item is blocked by one of two things, and which one decides whether it is
#: ever worth building something for.
#:
#: ``HUMAN`` — nothing will do this but a person: judgement, a subscription or
#: paywall, a document that was never put online, a claim resting on the
#: author's own observation. These never leave the list.
#:
#: ``TOOLING`` — a tool could do this and this pipeline has no such tool:
#: rendering JavaScript, reading a scanned PDF, fetching with a real browser
#: session. These are a roadmap, and they are totalled separately.
HUMAN = "human"
TOOLING = "tooling"

#: Groups, in the order the author meets them — cheapest-per-claim first, so a
#: pass down the list spends its early minutes on the pages that clear the most.
#: Keyed by the item's ``action``.
_GROUPS = (
    (
        "open_page",
        "Open a page the run fetched but could not read",
        "The fetch succeeded and the extractor got nothing usable out of it — a "
        "JavaScript-rendered page, a PDF that did not extract, or a bot wall "
        "serving a challenge instead of the article. A person with a browser "
        "reads these in seconds. Nothing here is evidence against the source.",
    ),
    (
        "find_copy",
        "Find a readable copy — the fetch was refused, or the page is gone",
        "A specific URL was named and did not yield the document. A 403 is a "
        "statement about automated access, not about the page, and the same "
        "address usually opens fine in a browser. Where archive.org was asked "
        "and had nothing, that is said below rather than left blank.",
    ),
    (
        "find_document",
        "Track down a document the run named but never linked",
        "The fact-check pass named a specific document — often down to a "
        "bulletin number — and produced no URL for it. These are precise enough "
        "to go and find, which is why they are worth listing at all; a vague "
        "'needs a source' would not be.",
    ),
    (
        "stand_behind",
        "Confirm what only you can confirm",
        "No source settles these, so no amount of fetching would have. They are "
        "your own observation, your own arithmetic, or a projection you are "
        "making. Read them once and satisfy yourself each is right — you are "
        "the source, and the draft should say so where that is not obvious.",
    ),
)

_GROUP_ORDER = {action: i for i, (action, _, _) in enumerate(_GROUPS)}

#: ``requests`` renders a failed status as "403 Client Error: Forbidden for url:
#: https://...", and the resolver folds that whole string into ``note``. Both
#: halves are worth recovering: the URL is the only place the publisher's real
#: address survives, and the status decides what the author should try next.
_URL_IN_NOTE = re.compile(r"for url:\s*(\S+)")
_STATUS_IN_NOTE = re.compile(r"\b(\d{3})\s+(?:Client|Server)\s+Error\b")

#: Gemini hands back every grounded source as a redirect through this host. The
#: wrapper expires roughly 30 days after the run (see ``ci_core.llm.client``'s
#: ``_grounding_chunks``), so an author who files one of these away for later is
#: filing away a dead link — worth saying on the item rather than in a doc.
_GROUNDING_MARKER = "/grounding-api-redirect/"

#: Tails the fact-check model appends to a document name to mean "or something
#: like it". They are what stops three references to *the same* Furuno document
#: from grouping together, because each one hedges differently. Cut for the
#: purposes of grouping only — the longest original string is what gets shown,
#: so "(e.g., techinfo.honda.com ...)" survives into the next step, where it is
#: the most specific pointer the run produced.
_HEDGE_TAILS = (
    " or equivalent",
    " or similar",
    " or other",
    " or another",
    " (e.g.",
    ", e.g.",
)


def _dicts(entries):
    """The dict entries of a report list, skipping anything else.

    Load-bearing rather than defensive habit: ``render_report_markdown`` now
    calls into this module, so a stray non-dict in ``section_9_citations`` — a
    bare string where a model's output was salvaged, say — would take down a
    review that rendered fine before the worklist existed. Every other renderer
    here degrades on bad input rather than raising, and this keeps that true.
    """
    return [e for e in (entries or []) if isinstance(e, dict)]


def _mapping(value):
    """``value`` if it is a dict, else an empty one. Same reasoning as ``_dicts``."""
    return value if isinstance(value, dict) else {}


def _clip(text, limit=110):
    """One line of a claim, for recognising it — not for re-reading it."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _is_grounding_redirect(url):
    return _GROUNDING_MARKER in (url or "")


#: How the address on an item was arrived at, when it was not simply the URL
#: the citation stored. Both cases need saying, because both mean the author is
#: being handed something different from what the report's ``url`` field shows.
_FROM_REDIRECT = "redirect"
_FROM_NOTE = "note"


def _real_url(citation):
    """The address a human should open, and how it was arrived at.

    Returns ``(url, provenance)`` where provenance is ``None``, ``_FROM_REDIRECT``
    or ``_FROM_NOTE``. Three sources, in descending order of how much the run
    actually knows:

    ``final_url`` — where the fetch landed after following redirects. The
    resolver records it only when it differs from the requested URL, so its mere
    presence means the stored ``url`` was a redirector. This is the best answer
    available and it is the one the run *proved*, by fetching it.

    The failure message — for a fetch that never landed there is no
    ``final_url``, but ``requests`` puts the address it was refused at into the
    exception text, and the resolver folds that into ``note``.

    ``url`` — what the citation stored, which for a grounded answer is a
    ``vertexaisearch`` redirect that expires roughly 30 days after the run.

    Preferring ``final_url`` is the point of this ordering. A page that fetched
    and could not be read is the one case where the run knows the publisher's
    own address and the citation still shows a 271-character redirect; handing
    the author the redirect there was giving them a link with a clock on it when
    a durable one was sitting in the same dict.
    """
    stored = citation.get("url") or ""
    landed = citation.get("final_url") or ""
    if landed and landed != stored:
        return landed, _FROM_REDIRECT
    from_note = _URL_IN_NOTE.search(citation.get("note") or "")
    if from_note:
        recovered = from_note.group(1).rstrip(".,;)")
        if recovered:
            return recovered, _FROM_NOTE if recovered != stored else None
    return stored, None


def _status(citation):
    """HTTP status behind a failed fetch, or None when the note names none."""
    match = _STATUS_IN_NOTE.search(citation.get("note") or "")
    return int(match.group(1)) if match else None


def _archive_state(citation):
    """What this run established about an archived copy, in the author's terms.

    The three negative states are not interchangeable and the difference decides
    the next step, so each gets its own sentence rather than a shared "no
    archive". ``report_markdown._render_archive_pair`` draws the same three
    distinctions for the same reason; this is the one-line version.
    """
    wayback = citation.get("wayback")
    if not isinstance(wayback, dict):
        return (
            "archive.org was never asked about this one, and re-running will not ask."
        )
    if wayback.get("snapshot_url"):
        return f"There is an archived copy: {wayback['snapshot_url']}"
    if wayback.get("archived") is None:
        return (
            "The archive.org lookup did not complete this run, so whether a "
            "snapshot exists is unknown — check before assuming there is none."
        )
    return "archive.org answered and has no snapshot of it."


#: The two dispositions where a document was actually retrieved and read. A
#: claim in either is settled as far as retrieval goes, whatever the verdict
#: was, so neither belongs on a list of things still to do.
_READ_DISPOSITIONS = ("checksum", "content_mismatch")


def _normalise_document(name):
    """Group key for a named document: the name with the model's hedge cut off.

    Three claims in the motivating run cited "Furuno, GPS/GNSS Receiver GPS Week
    Number Rollover, document SE18-100-034-02" and each hedged differently after
    it — "or equivalent official Furuno documentation", "...containing the full
    table", "...containing this specific table". Grouping on the raw string
    makes that one document into three errands.

    Only the recognised tails in ``_HEDGE_TAILS`` are cut, never a bare " or ":
    "Official Honda service manual or bulletin for 2011+ Odyssey navigation
    diagnostic menu access" is one document description, and truncating it at
    the first "or" would throw away the half that says which vehicle.
    """
    flat = " ".join(str(name or "").split())
    lowered = flat.lower()
    cut = len(flat)
    for tail in _HEDGE_TAILS:
        found = lowered.find(tail)
        if found != -1:
            cut = min(cut, found)
    return flat[:cut].strip(" .,;:").lower()


# ---------------------------------------------------------------------------
# Item construction, one group at a time
# ---------------------------------------------------------------------------


def _item(
    action,
    blocked_by,
    headline,
    target,
    why,
    next_step,
    section,
    claims,
    gap=None,
    target_label="Open",
    target_url=None,
):
    return {
        "action": action,
        "blocked_by": blocked_by,
        "headline": headline,
        "target": target,
        "target_label": target_label,
        # The clickable address, when the target itself is a document name
        # rather than a URL. Read by _has_address, which ranks a click above
        # the start of a search.
        "target_url": target_url,
        "why": why,
        "next_step": next_step,
        "section": section,
        "claims": list(claims),
        "gap": gap,
        "possibly_wrong": False,
    }


#: Why a fetched page could not be read, and what that costs the author.
#:
#: Read off the resolver's own record rather than guessed at: it sets
#: ``content_kind`` to ``access_wall`` when it positively recognises an
#: interstitial, leaves it ``pdf``/``html`` when extraction simply produced
#: nothing, and sets ``relevance_check`` when the page *was* read and only the
#: check on it failed. An earlier version of this inferred the reason from
#: whether ``content_summary`` was empty, and got the ``example.com`` citation
#: in the 2026-09-05 run backwards: 158 characters of the domain's own
#: boilerplate is a non-empty summary and no article text at all.
#:
#: Ordered by precedence, most specific first, so a URL whose citations disagree
#: still resolves to one deterministic reason.
#:
#: Each entry is ``(blocked_by, why, next_step_prefix, gap)``.
_UNREADABLE = {
    "access_wall": (
        HUMAN,
        "The site served a bot-check, CAPTCHA, or paywall interstitial instead "
        "of the document, so the real content was never seen. The run "
        "recognised the interstitial for what it was rather than reading the "
        "blocking notice and reporting that the source fails the claim.",
        "Open it yourself and see which it is.",
        None,
    ),
    "pdf": (
        TOOLING,
        "It is a PDF and no text came out of it — a scan with no text layer, a "
        "password on the file, or pypdf missing from the environment.",
        "Open it and read the relevant page.",
        "Read scanned PDFs (OCR) instead of reporting them as unreadable",
    ),
    "html": (
        TOOLING,
        "The fetch succeeded and no article text could be extracted: a "
        "JavaScript-rendered page, a paywall, or a bot-block. The run cannot "
        "tell which of the three, and none of them is a finding against the "
        "source. If it turns out to be a subscription, this one is permanently "
        "yours — no fetcher gets past one.",
        "Open it and read enough to settle the claims below.",
        "Render JavaScript before extracting, instead of reporting an empty page",
    ),
    "relevance_failed": (
        TOOLING,
        "The page was fetched and read — only the relevance check on it failed "
        "to return a usable verdict, so nothing was concluded either way. This "
        "is the one entry here where the document itself was never the problem.",
        "Re-running may clear this one without you. If it recurs, open it and "
        "judge the claims yourself.",
        "Retry a relevance check that comes back without a usable verdict",
    ),
    "default": (
        TOOLING,
        "The fetch succeeded and produced no readable text, for a reason the "
        "run did not classify.",
        "Open it and read enough to settle the claims below.",
        "Render JavaScript before extracting, instead of reporting an empty page",
    ),
}

_UNREADABLE_ORDER = ("access_wall", "pdf", "html", "relevance_failed", "default")


def _unreadable_reason(citation):
    """Which ``_UNREADABLE`` key a fetched-but-unread citation belongs to."""
    kind = citation.get("content_kind")
    if kind == "access_wall":
        return "access_wall"
    if citation.get("relevance_check"):
        return "relevance_failed"
    return kind if kind in _UNREADABLE else "default"


def _open_page_items(citations):
    """Pages that were fetched and yielded nothing readable, grouped by URL."""
    by_url = {}
    for c in citations:
        if _disposition(c) != "unverifiable":
            continue
        url, provenance = _real_url(c)
        if not url:
            continue
        slot = by_url.setdefault(
            url, {"citations": [], "reasons": set(), "provenance": provenance}
        )
        slot["citations"].append(c)
        slot["reasons"].add(_unreadable_reason(c))

    items = []
    for url, slot in by_url.items():
        reason = min(slot["reasons"], key=_UNREADABLE_ORDER.index)
        blocked_by, why, next_step, gap = _UNREADABLE[reason]
        if slot["provenance"] == _FROM_REDIRECT:
            # The run followed the redirector and this is where it landed, so
            # the author gets the durable address rather than the wrapper the
            # citation stored. Worth saying: it will not match the URL beside
            # this claim in SECTION 9, and an unexplained mismatch reads like
            # a bug.
            next_step += (
                " This is where the fetch actually landed, not the redirector "
                "the citation stores — SECTION 9 will show the other one."
            )
        elif _is_grounding_redirect(url):
            next_step += (
                " That address is a Google grounding redirect rather than the "
                "publisher's own, and those expire roughly 30 days after the "
                "run — open it now, and record the real URL while you are there."
            )
        items.append(
            _item(
                "open_page",
                blocked_by,
                "Open and read",
                url,
                why,
                next_step,
                'SECTION 9, "Fetched, but could not be read"',
                [c.get("claim", "") for c in slot["citations"]],
                gap=gap,
            )
        )
    return items


def _find_copy_items(citations):
    """URLs that were named and refused, grouped by the address a human can open."""
    by_url = {}
    for c in citations:
        if _disposition(c) != "fetch_failed":
            continue
        url, provenance = _real_url(c)
        if not url:
            continue
        slot = by_url.setdefault(url, {"citations": [], "provenance": provenance})
        slot["citations"].append(c)

    items = []
    for url, slot in by_url.items():
        here = slot["citations"]
        status = next((s for s in map(_status, here) if s), None)

        if status == 403:
            blocked_by = TOOLING
            why = (
                "The publisher refused an automated fetch (HTTP 403). That is a "
                "statement about automated access, not about the document, and "
                "the same address usually opens fine in a browser. If it turns "
                "out to be a subscription wall, this one is permanently yours — "
                "no fetcher gets past one."
            )
            gap = "Fetch with a real browser session so a 403 bot wall is not the end of it"
        elif status == 404:
            blocked_by = HUMAN
            why = (
                "The page is gone (HTTP 404). Nothing in the pipeline will bring "
                "it back; the question is whether a copy survives elsewhere."
            )
            gap = None
        else:
            blocked_by = TOOLING
            note = " ".join(str(here[0].get("note") or "").split())
            why = (
                f"The fetch failed with HTTP {status}."
                if status is not None
                else (note or "The fetch did not succeed, and no reason was recorded.")
            )
            gap = "Tell a transient fetch failure from a permanent one and retry it"

        next_step = f"Open it in a browser. {_archive_state(here[0])}"
        if slot["provenance"] == _FROM_NOTE:
            next_step += (
                " (That address was recovered from the failure message — the URL "
                "stored on the citation is an expiring redirect, not the "
                "publisher's own.)"
            )
        items.append(
            _item(
                "find_copy",
                blocked_by,
                "Open by hand, or find another copy",
                url,
                why,
                next_step,
                'SECTION 9, "Source URL identified, but the fetch was refused"',
                [c.get("claim", "") for c in here],
                gap=gap,
            )
        )
    return items


def _find_document_items(fact_check):
    """Named-but-unlinked documents from ``primary_source_needed``, grouped."""
    by_document = {}
    for entry in _dicts(fact_check.get("primary_source_needed")):
        name = entry.get("best_candidate_source") or ""
        key = _normalise_document(name)
        if not key:
            continue
        slot = by_document.setdefault(key, {"names": [], "claims": [], "url": None})
        slot["names"].append(" ".join(name.split()))
        slot["claims"].append(entry.get("claim", ""))
        if not slot["url"] and entry.get("best_candidate_url"):
            slot["url"] = entry["best_candidate_url"]

    items = []
    for slot in by_document.values():
        # Longest original wins: the hedge cut for grouping is often the only
        # place the model said *where* to look, and that pointer is the most
        # specific thing on the item.
        display = max(slot["names"], key=len).rstrip(" .")
        url = slot["url"]
        if url and not _is_grounding_redirect(url):
            next_step = (
                f"Open {url} — the run named it as the candidate but never fetched it."
            )
        elif url:
            next_step = (
                f"Open {url} — the only address the run has for this is a Google "
                "grounding redirect, which expires roughly 30 days after the "
                "run. Follow it now and record the publisher's own URL."
            )
        else:
            next_step = (
                "No URL was produced, so this starts as a search rather than a "
                "click. Search on the document name above — the identifiers in "
                "it are what make it findable."
            )
        items.append(
            _item(
                "find_document",
                HUMAN,
                "Find this document",
                display,
                "The fact-check pass could name the document but produced no "
                "usable link to it, and no configured source adapter covers this "
                "publisher. A named document with no URL is a research lead, not "
                "a citation.",
                next_step,
                "SECTION 2, primary_source_needed",
                slot["claims"],
                target_label="Find",
                target_url=url,
            )
        )
    return items


def _stand_behind_items(fact_check, citations):
    """The two kinds of claim no fetch was ever going to settle.

    Collapsed into at most two items rather than one per claim, because they are
    a single sitting for the author — read the list, satisfy yourself about each
    — where every other group is one errand per entry. Splitting them into
    fifteen line items would push real errands off a capped list to make room
    for work that is not errands.
    """
    items = []

    # The fact-check pass looked at these and concluded no public source
    # applies. `sources_checked` being empty is what separates that judgement
    # from "searched and found nothing", which is a research problem rather than
    # the author's. The pass's own reason is quoted, not paraphrased.
    author_only = [
        entry
        for entry in _dicts(fact_check.get("unverifiable"))
        if not (entry.get("sources_checked") or [])
    ]
    if author_only:
        reasons = sorted(
            {
                " ".join(str(e.get("reason") or "").split())
                for e in author_only
                if e.get("reason")
            }
        )
        why = "The fact-check pass judged that no public source settles these."
        if reasons:
            why += " Its reasons: " + " ".join(f'"{r}"' for r in reasons)
        items.append(
            _item(
                "stand_behind",
                HUMAN,
                "Satisfy yourself these are right — no source will",
                None,
                why,
                "Check each against whatever you actually relied on, and make "
                "sure the draft is explicit that it is your observation or your "
                "reasoning rather than a reported fact.",
                "SECTION 2, unverifiable",
                [e.get("claim", "") for e in author_only],
            )
        )

    # Called confirmed, with nothing behind it. The bucket is the fact-check
    # pass's judgement about the claim; it is not a retrieval result, and where
    # no URL existed and no adapter matched, no document was read by anybody.
    # Section 9 files these under "No source identified", which is accurate and
    # reads as a shortfall in coverage rather than as something to act on.
    unbacked = [
        c
        for c in citations
        if _disposition(c) == "no_source" and c.get("fact_check_bucket") == "confirmed"
    ]
    if unbacked:
        items.append(
            _item(
                "stand_behind",
                HUMAN,
                'Check the ones called "confirmed" with nothing behind them',
                None,
                "The fact-check pass called each of these confirmed, but no URL "
                "was ever produced and no source adapter matched, so no document "
                "was fetched or read. That verdict is a model's recollection. "
                "Where the claim is your own arithmetic or your own observation "
                "this is fine and expected — you are the source — but nothing "
                "external is standing behind it.",
                "Re-derive each one. Date and unit arithmetic is worth redoing on "
                "a calculator: this pipeline has no arithmetic checker and never "
                "attempted these.",
                'SECTION 9, "No source identified"',
                [c.get("claim", "") for c in unbacked],
            )
        )
    return items


# ---------------------------------------------------------------------------
# Ranking and assembly
# ---------------------------------------------------------------------------


def _claim_owner_order(item):
    """Precedence for which action owns a claim two actions would both clear.

    The group order is the precedence, and it reads as one rule: an action with
    an address on it beats an action that starts with a search. In the
    motivating run the Electronics360 teardown arrived twice — once as a refused
    URL (the publisher's own address, recovered from the 403) and once as a
    ``primary_source_needed`` document naming the same teardown with only an
    expiring redirect. Both are the same errand, and listing it in two groups
    sends the author to do it twice.
    """
    return (
        _GROUP_ORDER.get(item["action"], len(_GROUPS)),
        -len(item["claims"]),
        str(item["target"] or ""),
    )


def _dedupe_across_items(items):
    """Give each claim to exactly one action, and drop actions left with none.

    Runs before ranking, deliberately: ranking is by how many claims an action
    clears, and an action whose claims all belong to a stronger one clears none.
    Letting it keep them would rank a duplicate errand above a real one.
    """
    seen = set()
    for item in sorted(items, key=_claim_owner_order):
        item["claims"] = [c for c in item["claims"] if c not in seen]
        seen.update(item["claims"])
    return [i for i in items if i["claims"]]


def _flag_possibly_wrong(items, report):
    """Mark items whose claims the run itself suspected, so they sort first.

    An unread source behind a claim nothing disputes is a coverage gap. The same
    unread source behind a claim another model contradicted is a possible error
    in the published article, and it should not rank below a page that happens
    to cover more claims.
    """
    suspect = set()
    fact_check = _mapping(report.get("section_2_fact_check"))
    for bucket in ("contradicted", "outdated"):
        for entry in _dicts(fact_check.get(bucket)):
            if entry.get("claim"):
                suspect.add(entry["claim"])
    for c in _dicts(report.get("section_9_citations")):
        if c.get("verification") == "content_mismatch" and c.get("claim"):
            suspect.add(c["claim"])
    for item in items:
        item["possibly_wrong"] = any(claim in suspect for claim in item["claims"])
    return items


def _has_address(item):
    """True when the next step is a click rather than the start of a search.

    The deciding tie-break once reach runs out, and it earns that place on a
    real report: the 161-citation data-centre run produces 45 ``find_document``
    actions that each clear exactly one claim. Tied on reach, they fell through
    to sorting by target — so *which* eight of the forty-five got printed was
    alphabetical accident, under a header promising a ranking by value.

    "Can I click it" is the one thing that genuinely separates them. A named
    document with a candidate URL is a minute's work; the same document with
    only a name is an open-ended search that may end in nothing.
    """
    return bool(item.get("target_url") or item["target_label"] == "Open")


def _rank(item):
    """Sort key: suspect claims first, then reach, then how findable it is.

    Reach — how many claims one action clears — is the honest measure of what a
    minute spent here buys, and it is the whole reason items are collapsed by
    target before they are ranked. ``_has_address`` breaks the ties reach
    leaves, which on a large report is most of the list.
    """
    return (
        0 if item["possibly_wrong"] else 1,
        -len(item["claims"]),
        _GROUP_ORDER.get(item["action"], len(_GROUPS)),
        0 if _has_address(item) else 1,
        item["headline"],
        str(item["target"] or ""),
    )


def _tool_gaps(items):
    """The ``TOOLING`` items rolled up by gap — the roadmap half of the list."""
    rolled = {}
    for item in items:
        if item["blocked_by"] != TOOLING or not item.get("gap"):
            continue
        slot = rolled.setdefault(
            item["gap"], {"gap": item["gap"], "actions": 0, "claims": 0}
        )
        slot["actions"] += 1
        slot["claims"] += len(item["claims"])
    return sorted(rolled.values(), key=lambda g: (-g["claims"], g["gap"]))


#: Fact-check buckets that mean the pass did not settle the claim. Read only to
#: count what the worklist leaves alone; ``primary_source_needed`` and
#: ``unverifiable`` are also read for real, above.
_UNSETTLED_BUCKETS = (
    "contradicted",
    "outdated",
    "unverifiable",
    "primary_source_needed",
)


def _unactioned(citations, fact_check, items):
    """How many unsettled claims the worklist deliberately attaches no action to.

    There are always some, and they are not oversights:

    * a ``pointer`` citation is a portal that is probably about the right topic
      with nothing retrieved from it — a research lead, and a poor errand;
    * a claim the pass marked ``unverifiable`` *after* searching is a research
      problem, not something only the author can do;
    * a ``contradicted`` claim with no URL either way is a finding to read, not
      a document to go and get.

    All three belong in the sections, and all three would be invisible here
    without this count — which is the exact failure of a list that opens by
    claiming to cover everything the run could not settle. Counting them costs
    one line and keeps that opening sentence true.
    """
    covered = {claim for item in items for claim in item["claims"]}
    seen = {
        c["claim"]
        for c in citations
        if c.get("claim") and _disposition(c) not in _READ_DISPOSITIONS
    }
    for bucket in _UNSETTLED_BUCKETS:
        seen.update(
            e["claim"] for e in _dicts(fact_check.get(bucket)) if e.get("claim")
        )
    return len(seen - covered)


def build_worklist(report, limit=DEFAULT_LIMIT):
    """Build the ranked worklist for a consolidated review report.

    ``report`` is the dict ``consolidation.build_report`` produces and
    ``history.save_run`` writes to ``run_N_*_report.json``.

    Returns a dict with ``items`` (ranked, capped at ``limit``), ``held_back``
    (the ranked tail that did not fit, kept so it can be counted rather than
    silently dropped), ``tool_gaps``, and the claim totals the header states.
    """
    citations = _dicts(report.get("section_9_citations"))
    fact_check = _mapping(report.get("section_2_fact_check"))

    items = (
        _open_page_items(citations)
        + _find_copy_items(citations)
        + _find_document_items(fact_check)
        + _stand_behind_items(fact_check, citations)
    )

    # Two passes, because a claim can repeat inside one action (two models named
    # the same URL for it) and across two (a refused URL and the document that
    # URL *is*). Either way the author should meet it once.
    for item in items:
        item["claims"] = list(dict.fromkeys(c for c in item["claims"] if c))
    items = _dedupe_across_items([i for i in items if i["claims"]])

    _flag_possibly_wrong(items, report)
    items.sort(key=_rank)

    shown, held_back = items[:limit], items[limit:]
    # Whether the cap fell inside a run of equally-ranked actions. Everything
    # after ``possibly_wrong`` in the sort key is a genuine value judgement
    # except the last two entries, which are pure tie-breaks for stability — so
    # comparing the key up to that point says whether the split was earned.
    cut_inside_a_tie = (
        bool(held_back) and _rank(shown[-1])[:4] == _rank(held_back[0])[:4]
    )
    return {
        "items": shown,
        "held_back": held_back,
        "cut_inside_a_tie": cut_inside_a_tie,
        "tool_gaps": _tool_gaps(shown),
        "claims_covered": len({c for i in shown for c in i["claims"]}),
        "claims_total": len({c for i in items for c in i["claims"]}),
        "no_action": _unactioned(citations, fact_check, items),
        "grounding_redirects": sum(
            1 for c in citations if _is_grounding_redirect(c.get("url"))
        ),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: Not a "## SECTION N" heading, and deliberately so. The revise loop in
#: handoff_templates/revise_after_review_prompt.md tells the author to paste
#: SECTION 1 through SECTION N into a chat model and ask it to revise the draft.
#: A list of documents nobody has read yet is the last thing to hand a model in
#: that context — it is an invitation to invent them. This is the author's list,
#: and it stays out of that range on purpose. (test_templates_current derives
#: the pasted range from the "## SECTION" headings in report_markdown, so this
#: wording also keeps that test honest rather than merely quiet.)
HEADING = (
    "## WORKLIST — what only you, or a tool this pipeline does not have, can settle"
)

_BADGE = {HUMAN: "**[you]**", TOOLING: "**[tool gap]**"}


def _render_item(number, item):
    # Four spaces, not three: a two-digit number makes the list marker four
    # characters wide, and three-space continuations stop nesting under it at
    # item 10 — which is exactly where a worklist starts being long enough to
    # matter. Four is still within CommonMark's tolerance for a one-digit marker.
    indent = "    - "
    lines = [
        f"{number}. {_BADGE[item['blocked_by']]} **{item['headline']}** — "
        f"clears {len(item['claims'])} claim(s)"
    ]
    if item["possibly_wrong"]:
        lines.append(
            f"{indent}⚠ At least one claim here was contradicted or came back "
            "outdated. Treat this as a possible error in the article, not a "
            "gap in its sourcing."
        )
    if item["target"]:
        lines.append(f"{indent}{item['target_label']}: {item['target']}")
    lines.append(f"{indent}Why the run could not: {item['why']}")
    lines.append(f"{indent}Next: {item['next_step']}")
    shown = item["claims"][:3]
    claims = " · ".join(f'"{_clip(c)}"' for c in shown)
    rest = len(item["claims"]) - len(shown)
    if rest:
        claims += f" · and {rest} more"
    lines.append(f"{indent}Clears: {claims} — {item['section']}")
    return lines


def render_worklist(worklist):
    """Render ``build_worklist``'s output as markdown lines."""
    lines = [HEADING, ""]
    items = worklist["items"]
    if not items:
        lines.append(
            "_Nothing outstanding: every claim this run could not settle on its "
            "own was either read from a document or has no action a person could "
            "take on it._"
        )
        lines.append("")
        return lines

    lines.append(
        f"**{len(items)} action(s) below clear {worklist['claims_covered']} "
        "claim(s) this run could not settle on its own.** Ranked by what each "
        "one buys: claims the run flagged as possibly wrong first, then by how "
        "many claims a single action clears. Actions are collapsed by target — "
        "one page can settle six claims, and listing it six times would "
        "misrepresent the work."
    )
    lines.append("")
    # Said here rather than buried at the end. The sentence above is a claim
    # about coverage, and it is only true alongside this one: some unsettled
    # claims have no errand attached — a pointer to a portal, a claim the pass
    # searched for and could not confirm, a contradiction with no document
    # either way. They are findings to read, not work to do, and leaving them
    # uncounted would make this list look complete when it is not.
    if worklist["no_action"]:
        lines.append(
            f"_{worklist['no_action']} further unsettled claim(s) have no action "
            "attached to them. They are findings to read rather than errands to "
            "run — a topic-relevant portal with nothing retrieved from it, a "
            "claim the fact-check pass searched for and could not confirm, or a "
            "contradiction with no document on either side. SECTION 2 and "
            "SECTION 9 have them._"
        )
        lines.append("")
    lines.append(
        "Every item is blocked by one of two things. **[you]** — no tool will "
        "ever do this: judgement, a paywall, a document nobody put online, or a "
        "claim resting on your own observation. **[tool gap]** — a tool could do "
        "this and this pipeline has none; those are totalled at the bottom as a "
        "roadmap."
    )
    lines.append("")
    lines.append(
        "_Claims are clipped to one line. The full record stays in the sections "
        "named on each item; nothing is repeated here._"
    )
    lines.append("")

    number = 0
    for action, title, blurb in _GROUPS:
        group = [i for i in items if i["action"] == action]
        if not group:
            continue
        claims = len({c for i in group for c in i["claims"]})
        lines.append(f"### {title} ({len(group)} action(s), {claims} claim(s))")
        lines.append("")
        lines.append(f"_{blurb}_")
        lines.append("")
        for item in group:
            number += 1
            lines.extend(_render_item(number, item))
            lines.append("")

    held = worklist["held_back"]
    if held:
        held_claims = len({c for i in held for c in i["claims"]})
        # "Lower-ranked" is only true when the cut fell between two genuinely
        # different ranks. On a big report it usually does not: the
        # 161-citation data-centre run ends in a run of 45 actions that each
        # clear one claim, so the boundary sits inside a tie and the split is
        # arbitrary. Claiming a ranking there would be the same overclaim the
        # header sentence was fixed for.
        tied = worklist["cut_inside_a_tie"]
        opening = (
            f"_{len(held)} further action(s) covering {held_claims} further "
            "claim(s) are not listed, to keep this a worklist rather than a "
            "backlog. "
        )
        lines.append(
            opening
            + (
                "The list does not end on a clean break: the actions above and "
                "below the cut clear the same number of claims each and are "
                "equally findable, so which of them got printed is arbitrary. "
                "Treat the omitted ones as equal in value to the last few here, "
                "not as lesser. "
                if tied
                else "They rank below every action above. "
            )
            + "Clear the list above and re-run, or read them in the sections "
            "named on each group heading._"
        )
        lines.append("")

    gaps = worklist["tool_gaps"]
    if gaps or worklist["grounding_redirects"]:
        lines.append("### What a better pipeline would have cleared without you")
        lines.append("")
        lines.append(
            "_Only the **[tool gap]** items above. Every one is something a tool "
            "could do, so each line is a candidate for the roadmap rather than "
            "something the author is stuck with._"
        )
        lines.append("")
        for gap in gaps:
            lines.append(
                f"- {gap['gap']} — would have cleared {gap['actions']} action(s), "
                f"{gap['claims']} claim(s) this run."
            )
        if worklist["grounding_redirects"]:
            lines.append(
                "- Resolve grounded-search redirects to the publisher's own URL "
                f"before storing them — {worklist['grounding_redirects']} "
                "citation(s) in this run point at a "
                "`vertexaisearch.cloud.google.com` redirect that expires roughly "
                "30 days from the run date. After that the report names a source "
                "nobody can open."
            )
        lines.append("")

    return lines
