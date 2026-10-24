"""One net status per claim, across Section 2, Section 1, Section 9 and the worklist.

Before this, a reader had to look in four places to learn what became of one
claim, and the four could disagree. In a real saved run (pipeline-and-
verification-update, run 6, 2026-10-06) a count of the author's own published
posts was ``confirmed`` by gemini, ``primary_source_needed`` for perplexity,
classified out of scope by a model whose call was not honoured, and
``content_mismatch`` in Section 9 because the page read did not mention it. The
worklist then put a "contradicted or outdated" banner on it, although Section 2
had zero entries in either bucket: the banner was reading Section 9's mismatch
through a different name. Issue #338.

Divergence was the bug, so the vocabulary and the rule live here and every
reader uses them, in the pattern of ``adapters/citation/disposition.py``.

The precedence, strongest first, is the order of :data:`STATUSES`:

* an exclusion beats everything, because ``fact_check_scope`` has already
  withdrawn the verdicts behind it (they survive in ``withdrawn_verdicts`` and
  the ``why`` line names them);
* a contradiction beats a page that merely does not support the claim: any one
  ``contradicted`` or ``outdated`` verdict, a read source whose verdict is
  ``contradicts``, or the asserting model conceding at re-ask (``correct_claim``
  or ``withdraw``, which is the model retracting its own assertion). An
  ``unverifiable`` verdict is not a contradiction; it is a disagreement about
  evidence, and ``consolidation.find_contradictions`` already reports it;
* a page that was read outranks a model's assertion, in either direction;
* Section 1 is a reference only. Its items are paragraph-sized passages, and
  ``passage_match`` deliberately never merges a sentence into the paragraph
  that contains it, so a Section 1 flag adds a link to the entry, never a
  status.

Claims are joined with :func:`passage_match.group_passages`, not by exact
string, because the same claim comes back with different quote marks and
clause boundaries across models and runs, and ``find_contradictions`` already
groups that way. The grouping is conservative on purpose: short claims never
merge, so "I have a day job." and "I have a day job. I have a side job." stay
two entries. The ledger shows that rather than hiding it.
"""

from urllib.parse import urlparse

from .adapters.citation.disposition import disposition
from .fact_check_scope import AUTHOR_ORIGINS
from .passage_match import group_passages, normalise

__all__ = [
    "STATUSES",
    "annotate_fact_check",
    "author_hosts",
    "build_claim_ledger",
    "label_for",
    "status_by_claim",
    "summary_line",
]

#: Every status a claim can end in, strongest evidence first. The order is the
#: precedence rule: the first status whose condition holds is the claim's.
STATUSES = (
    ("excluded_by_author", "Excluded by the author"),
    ("excluded_by_model", "Excluded by a fact-check model"),
    ("contradicted", "Contradicted"),
    ("source_does_not_support", "Source read, does not support the claim"),
    ("confirmed_source_read", "Confirmed, source read"),
    ("confirmed_source_not_read", "Confirmed by a model, source not read"),
    ("unverified", "Unverified"),
)

_LABELS = dict(STATUSES)

#: Section 2 buckets that hold verdicts on a claim, in the order they are read.
_VERDICT_BUCKETS = (
    "confirmed",
    "outdated",
    "contradicted",
    "unverifiable",
    "primary_source_needed",
)

#: Re-ask answers in which the asserting model retracts its own assertion.
#: ``report_markdown._REASK_CONCEDES`` is the same set; it is repeated here
#: rather than imported because this module must not depend on a renderer.
_REASK_CONCEDES = frozenset({"correct_claim", "withdraw"})

#: Section 9 dispositions in which a page was fetched and its text read.
_READ = ("content_mismatch", "checksum")


def label_for(key):
    """Human-readable phrasing for a status key."""
    return _LABELS.get(key, key)


def author_hosts(pub_config):
    """Hosts that count as the author's own site, from the publication config.

    A supporting page on one of these is the author confirming the author.
    Read off ``wordpress.site_url``, the one place a publication names its site;
    a leading ``www.`` is dropped so both spellings match.
    """
    wp = (pub_config or {}).get("wordpress") or {}
    host = _host(wp.get("site_url") if isinstance(wp, dict) else None)
    return (host,) if host else ()


def _host(url):
    host = (urlparse(url or "").netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _records(entries):
    return (
        [e for e in (entries or []) if isinstance(e, dict)]
        if isinstance(entries, list)
        else []
    )


def _rows(report):
    """Every record that names a claim, tagged with where it came from."""
    fact_check = report.get("section_2_fact_check")
    if not isinstance(fact_check, dict):
        return []
    rows = []
    for bucket in _VERDICT_BUCKETS:
        for entry in _records(fact_check.get(bucket)):
            if entry.get("claim"):
                rows.append((entry["claim"], "verdict", bucket, entry))
    for entry in _records(fact_check.get("out_of_scope")):
        if entry.get("claim"):
            rows.append((entry["claim"], "scope", None, entry))
    for entry in _records(report.get("section_9_citations")):
        if entry.get("claim"):
            rows.append((entry["claim"], "citation", None, entry))
    return rows


def _strongest_citation(citations):
    """The citation that decides, when a claim has several.

    A read page beats an unread one, and within the read pages a refutation
    beats a confirmation: that is the same ordering the statuses carry.
    """
    rank = {"content_mismatch": 0, "checksum": 1}
    return min(
        citations,
        key=lambda c: (
            0 if (c.get("relevance_verdict") == "contradicts") else 1,
            0 if (c.get("reask") or {}).get("action") in _REASK_CONCEDES else 1,
            rank.get(disposition(c), 2),
        ),
        default=None,
    )


def _decide(buckets, exclusion, citation, hosts):
    """Apply the precedence to one claim's evidence. Returns (status, why, extra)."""
    extra = {}
    if exclusion is not None:
        by = exclusion.get("excluded_by") or "an unknown rule"
        who = "author" if by in AUTHOR_ORIGINS else "model"
        why = f"excluded by the {who} ({by})"
        withdrawn = [
            f"{w.get('model') or 'a model'} had marked it {w.get('bucket')}"
            for w in exclusion.get("withdrawn_verdicts") or []
            if isinstance(w, dict)
        ]
        if withdrawn:
            why += "; " + ", ".join(withdrawn)
        return f"excluded_by_{who}", why, extra

    disp = disposition(citation) if citation else None
    verdict = (citation or {}).get("relevance_verdict")
    reask = (citation or {}).get("reask") or {}

    challengers = [
        f"{m} marked it {b}"
        for b in ("contradicted", "outdated")
        for m in buckets.get(b, ())
    ]
    if reask.get("action") in _REASK_CONCEDES:
        why = (
            f"{reask.get('asked_model') or 'the asserting model'} conceded at "
            f"re-ask ({reask['action']}) after the source was read"
        )
        if reask.get("corrected_claim"):
            extra["corrected_claim"] = reask["corrected_claim"]
        return "contradicted", why, extra
    if disp == "content_mismatch" and verdict == "contradicts":
        return (
            "contradicted",
            f"the source read contradicts it ({_where(citation)})",
            extra,
        )
    if challengers:
        return "contradicted", "; ".join(challengers), extra

    confirmers = ", ".join(buckets.get("confirmed", ())) or None
    if disp == "content_mismatch":
        why = f"the page read does not support it ({verdict or 'no verdict'}; {_where(citation)})"
        if confirmers:
            why = f"{confirmers} confirmed it, but " + why
        return "source_does_not_support", why, extra
    if disp == "checksum":
        own = _host(citation.get("final_url") or citation.get("url")) in hosts
        extra["source_is_authors"] = own
        where = _where(citation)
        why = (
            f"the author's own page supports it ({where})"
            if own
            else f"the source was read and supports it ({where})"
        )
        return "confirmed_source_read", why, extra
    if confirmers:
        why = f"{confirmers} confirmed it; no source was read"
        if citation is not None:
            why += f" ({disp})"
        return "confirmed_source_not_read", why, extra

    seen = ", ".join(f"{b} by {', '.join(ms)}" for b, ms in buckets.items() if ms)
    return "unverified", seen or "no verdict reached it", extra


def _where(citation):
    return _host(citation.get("final_url") or citation.get("url")) or "no URL"


def build_claim_ledger(report, author_hosts=()):
    """The ledger: one entry per claim, with its status, why, and the evidence.

    Pure: reads the report, writes nothing. The pipeline calls it once, after
    Section 9 is final, and stores the result as ``report["claim_ledger"]``.
    A report without Section 9 (an ``--offline`` replay, or a run whose citation
    pass never ran) gets a ledger that says so in ``citations_resolved`` and
    can rise no higher than ``confirmed_source_not_read``.
    """
    hosts = frozenset(h.lower() for h in author_hosts if h)
    rows = _rows(report)
    section_1 = [
        e["passage"]
        for e in _records(report.get("section_1_consensus"))
        if isinstance(e.get("passage"), str) and e["passage"]
    ]
    section_1_norm = [(p, normalise(p)) for p in section_1]

    claims = []
    for rep, group in group_passages(rows, lambda r: r[0]):
        buckets = {}
        exclusion = None
        citations = []
        variants = []
        for text, kind, bucket, entry in group:
            if text != rep and text not in variants:
                variants.append(text)
            if kind == "verdict":
                buckets.setdefault(bucket, []).append(entry.get("source_model") or "?")
            elif kind == "scope":
                if entry.get("excluded"):
                    # An author marking beats a model's, if both reached one claim.
                    if exclusion is None or (
                        entry.get("excluded_by") in AUTHOR_ORIGINS
                        and exclusion.get("excluded_by") not in AUTHOR_ORIGINS
                    ):
                        exclusion = entry
            else:
                citations.append(entry)

        citation = _strongest_citation(citations)
        status, why, extra = _decide(buckets, exclusion, citation, hosts)
        rep_norm = normalise(rep)
        entry = {
            "claim": rep,
            "status": status,
            "why": why,
            **extra,
            "evidence": {
                "buckets": {b: ms for b, ms in buckets.items()},
                "exclusion": (
                    {
                        "excluded_by": exclusion.get("excluded_by"),
                        "reason": exclusion.get("reason"),
                        "withdrawn_verdicts": exclusion.get("withdrawn_verdicts") or [],
                    }
                    if exclusion
                    else None
                ),
                "citation": (
                    {
                        "url": citation.get("final_url") or citation.get("url"),
                        "disposition": disposition(citation),
                        "relevance_verdict": citation.get("relevance_verdict"),
                        "reask": (citation.get("reask") or {}).get("action"),
                    }
                    if citation
                    else None
                ),
                "citations_total": len(citations),
                "section_1_passages": [
                    p for p, n in section_1_norm if rep_norm and rep_norm in n
                ],
                "variants": variants,
            },
        }
        claims.append(entry)

    counts = {key: 0 for key, _ in STATUSES}
    for entry in claims:
        counts[entry["status"]] += 1
    return {
        "claims": claims,
        "counts": counts,
        "citations_resolved": bool(_records(report.get("section_9_citations"))),
    }


def status_by_claim(ledger):
    """``{claim text: status}`` for every claim text the ledger saw.

    Each entry's representative text and every variant quotation grouped under
    it map to the same status, so a reader holding the text a citation or a
    verdict carries can look its claim up without grouping again.
    """
    by_text = {}
    for entry in (ledger or {}).get("claims") or []:
        if not isinstance(entry, dict) or not entry.get("claim"):
            continue
        by_text[entry["claim"]] = entry["status"]
        for variant in (entry.get("evidence") or {}).get("variants") or []:
            by_text[variant] = entry["status"]
    return by_text


def annotate_fact_check(fact_check, ledger):
    """Stamp each Section 2 entry with its claim's ``net_status``.

    Joined the same way the ledger was built, so an entry whose claim was
    grouped under a longer quotation gets that group's status. Entries that are
    not records are left alone, as every renderer leaves them.
    """
    if not isinstance(fact_check, dict):
        return
    by_text = status_by_claim(ledger)
    for bucket in _VERDICT_BUCKETS + ("out_of_scope",):
        for entry in _records(fact_check.get(bucket)):
            status = by_text.get(entry.get("claim"))
            if status:
                entry["net_status"] = status


def summary_line(ledger):
    """The one console line: a total and every non-zero status, in precedence order."""
    claims = ledger.get("claims") or []
    line = f"Claim ledger: {len(claims)} claim(s)"
    parts = [
        f"{n} {label_for(key).lower()}"
        for key, _ in STATUSES
        if (n := (ledger.get("counts") or {}).get(key, 0))
    ]
    if parts:
        line += ": " + ", ".join(parts)
    if claims and not ledger.get("citations_resolved"):
        line += " (Section 9 not resolved this run, so nothing can rank above a model's confirmation)"
    return line
