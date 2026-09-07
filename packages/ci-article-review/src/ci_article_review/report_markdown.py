"""
Render a consolidated review report (the dict produced by
``consolidation.build_report``) into a human-readable markdown document.

The saved JSON report holds everything the ensemble found, but nothing
renders it as prose a human can paste into a chat model or read directly —
only aggregate counts are printed to the console. This module fills that
gap, following the SECTION 1-8 structure documented in
``handoff_templates/review_report.md`` (section 9, citations, was added to
the pipeline after that template was written).

The no-dependency rule below is about the *provider adapters* — importing
``analysis.seo_suggest`` or ``adapters.citation.wayback`` would pull ``requests``
and the model clients in behind them, so their constants are duplicated here and
kept in step by a test. ``adapters.citation.disposition`` is exempt because it is
a leaf: a tuple, a lookup and a pure function, importing nothing itself.
"""

from .adapters.citation.disposition import DISPOSITIONS, disposition

# ``worklist`` is a second exempt import, for the same reason: it reads the same
# plain report dict, imports only the stdlib and the leaf module above, and so
# pulls nothing in behind it.
from .worklist import build_worklist, render_worklist

#: Order the SEO METADATA fields render in, matching publication.md's block.
#: Duplicated from ``analysis.seo_suggest.FIELD_ORDER`` rather than imported so
#: this module stays a dependency-free renderer over a plain dict — importing
#: the suggestion module would pull the provider adapters in behind it. A test
#: asserts the two stay in step.
_SEO_FIELD_ORDER = ("meta_description", "og_title", "og_description", "schema_type")

#: Archive outcome vocabulary, duplicated from ``adapters.citation.wayback``'s
#: ``ARCHIVE_*`` constants rather than imported, for the same reason as
#: ``_SEO_FIELD_ORDER`` above: this module is a dependency-free renderer over a
#: plain dict, and importing the adapter would pull ``requests`` in behind it. A
#: test asserts the two sets stay in step.
_ARCHIVE_SUBMITTED = "submitted"
_ARCHIVE_ARCHIVED = "archived"
_ARCHIVE_PENDING = "pending"
_ARCHIVE_CAPTURE_FAILED = "capture_failed"
_ARCHIVE_SUBMIT_FAILED = "submit_failed"
_ARCHIVE_NOT_ATTEMPTED = "not_attempted"


def _wayback_summary(wb):
    """One line describing a wayback result, for a reader rather than a debugger.

    A wayback result is a dict, and ``_kv_lines`` dumped it raw — putting a
    reader in front of ``{'archived': None, 'error': '...'}`` and asking them to
    work out what it meant.

    The ``archived is None`` case is the one that has to be right: it means the
    lookup never completed, which is NOT "there is no snapshot". Since the rate
    limiter's circuit breaker skips every remaining lookup once it trips, a null
    is the common case in a throttled run rather than a rare one, and rendering
    it as anything resembling "not archived" would assert something the run
    never established.

    Deliberately duplicated from ``adapters.citation.wayback.format_summary``
    rather than imported, for the same reason ``_SEO_FIELD_ORDER`` above is:
    this module is a dependency-free renderer over a plain dict, and importing
    the wayback adapter would pull ``requests`` in behind it. A test asserts the
    two stay in step across all four states.
    """
    if wb.get("archived") is None:
        return (
            f"NOT CHECKED — the archive.org lookup did not complete "
            f"({wb.get('error', 'unknown error')}). This says nothing about "
            f"whether the page is archived."
        )
    if not wb.get("archived"):
        return "Not archived in Wayback Machine"
    age = wb.get("snapshot_age_days")
    age_str = f"{age}d ago" if age is not None else "age unknown"
    flag = " [STALE]" if wb.get("snapshot_stale") else ""
    return f"Archived — latest snapshot {age_str}{flag}: {wb.get('snapshot_url', '')}"


def _render_alternates(urls, indent="  "):
    """The other URLs tried for the same claim: a count first, then the list.

    ``_kv_lines`` dumped this as a raw Python list repr, which for a
    grounded-model citation is two or three 271-character opaque redirect URLs
    run together on a single line — 546 of the 2,682 characters in the Honda
    run's one content-mismatch entry, sitting above the line the author acts on.

    The URLs stay, because a reader auditing a tier has to be able to see which
    sources were tried. What changes is that the count leads, so the fact ("two
    other sources were checked") is legible without reading 546 characters of
    tracking URL, and each URL is its own subordinate line rather than part of a
    wrapped blob with quotes and brackets in it.

    Saying none of *them* supported the claim is safe in both places the
    resolver sets this field: the checksum branch records the attempts that
    failed *before* the one that succeeded, and the mismatch branch records
    every attempt other than the best one. Neither list ever holds a supporting
    source.

    The wording has to stop there, though. The resolver's own note says "none
    supported it either", which is true where it is written — under a refuted
    claim — and false under the five real checksum entries that also carry this
    field, where the primary source did support the claim. This renderer is
    shared by every tier, so it says only what is true of the alternates.
    """
    urls = [u for u in (urls or []) if u]
    if not urls:
        return []
    lines = [
        f"{indent}- Alternates checked: {len(urls)} other source(s) cited for "
        f"this claim were also fetched; none of them supported it."
    ]
    lines.extend(f"{indent}  - {u}" for u in urls)
    return lines


def _kv_lines(d, exclude=()):
    """Render remaining key/value pairs of a flag dict as indented bullets."""
    lines = []
    for key, value in d.items():
        if key in exclude or value in (None, "", [], {}):
            continue
        if key == "wayback" and isinstance(value, dict):
            lines.append(f"  - Wayback: {_wayback_summary(value)}")
            continue
        if key == "alternates_checked" and isinstance(value, (list, tuple)):
            lines.extend(_render_alternates(value))
            continue
        label = key.replace("_", " ").capitalize()
        lines.append(f"  - {label}: {value}")
    return lines


def _dicts(entries):
    """The dict entries of ``entries``, and how many were dropped.

    A findings list is meant to hold records, but a salvaged model response can
    leave a bare string among them — a response truncated mid-object and
    recovered as text. Rendering used to raise ``AttributeError`` on the first
    one. Skipping is the smaller loss, so the count comes back alongside the
    survivors instead of being swallowed — see ``_skipped_note``.
    """
    entries = entries or []
    kept = [e for e in entries if isinstance(e, dict)]
    return kept, len(entries) - len(kept)


def _mapping(value):
    """``value`` if it is a dict, else an empty one.

    The companion to ``_dicts`` for the case where a whole section, rather than
    one entry in it, arrived as something other than a record. Section 2 handles
    that case itself with a message saying so; this is for the call sites that
    only need to reach through it without raising.
    """
    return value if isinstance(value, dict) else {}


def _skipped_note(dropped_by_field):
    """One line accounting for entries dropped by ``_dicts``, or [].

    Dropping them silently would repeat the mistake the rest of this module is
    built to avoid: a section short by a finding reads exactly like a section
    that had one fewer finding to report. So it is counted and named. The field
    name is what makes it actionable: the entry cannot be rendered, but it was
    already written to the run's ``_report.json`` before this point, and the
    field says where to find it.
    """
    dropped_by_field = {f: n for f, n in dropped_by_field.items() if n}
    total = sum(dropped_by_field.values())
    if not total:
        return []
    where = ", ".join(f"`{f}` ({n})" for f, n in sorted(dropped_by_field.items()))
    one = total == 1
    field_ref = "that field" if len(dropped_by_field) == 1 else "those fields"
    return [
        f"> **{total} malformed {'entry' if one else 'entries'} skipped** — "
        f"{where}. {'That finding' if one else 'Those findings'} arrived as "
        f"something other than a record, which usually means a model response "
        f"was truncated and salvaged as text. This section is short by "
        f"{total}; the raw {'entry is' if one else 'entries are'} in this run's "
        f"`_report.json` under {field_ref}.",
        "",
    ]


def _render_section_1(consensus_flags):
    lines = ["## SECTION 1: Consensus Flags", ""]
    consensus_flags, dropped = _dicts(consensus_flags)
    lines.extend(_skipped_note({"section_1_consensus": dropped}))
    if not consensus_flags:
        lines.append(
            "_No consensus flag entry in this run could be read._"
            if dropped
            else "_No consensus flags._"
        )
        return lines

    for i, entry in enumerate(consensus_flags, 1):
        passage = entry.get("passage", "")
        models = ", ".join(entry.get("models", []))
        weight = entry.get("weight_sum")
        lt = (
            " (LanguageTool also flagged)"
            if entry.get("languagetool_also_flagged")
            else ""
        )
        lines.append(f'### {i}. "{passage}"')
        lines.append(f"- Flagged by: {models} — weight {weight}{lt}")
        flags, flags_dropped = _dicts(entry.get("flags", []))
        lines.extend(_skipped_note({"section_1_consensus[].flags": flags_dropped}))
        for flag in flags:
            source = flag.get("source_model", "?")
            domain = flag.get("domain", "?")
            lines.append(f"  - **{source}:{domain}**")
            for kv in _kv_lines(
                flag,
                exclude=(
                    "domain",
                    "source_model",
                    "type",
                    "passage",
                    "passage_reference",
                    # Fact-check findings reach Section 1 now, and their quoted
                    # text lives in "claim" rather than "passage" — which is
                    # already the heading directly above, so printing it again
                    # under every source repeated the passage five or six times
                    # per entry.
                    "claim",
                ),
            ):
                lines.append(f"  {kv}")
        lines.append("")
    return lines


#: Buckets that state a verdict about a claim, and so are expected to carry
#: evidence for it. ``unverifiable`` and ``primary_source_needed`` are excluded
#: deliberately: both are the model declining to reach a verdict, which is the
#: honest answer when it has nothing to quote, and counting them as missing
#: evidence would penalise exactly the behaviour the prompt asks for.
_VERDICT_BUCKETS = ("confirmed", "outdated", "contradicted")


def _render_evidence_coverage(fact_check):
    """A line saying how many verdicts arrived with checkable evidence.

    Section 9 exists because "a model asserts this" and "a document was read"
    are different things. The same gap opens one section earlier: a `confirmed`
    verdict is a model's judgment, and until the prompt asked for a verbatim
    quote and a direct URL there was nothing in the output to tell a reader
    whether it rested on anything they could open. In the 2026-08-12 run 85
    claims came back confirmed and 50 carried no URL at all.

    Counted rather than enforced. A model that ignores the field still produces
    a usable report — it just produces a visibly weaker one, which is the
    information a reader needs.
    """
    items = [i for b in _VERDICT_BUCKETS for i in (fact_check.get(b) or [])]
    if not items:
        return []
    quoted = sum(1 for i in items if (i.get("supporting_quote") or "").strip())
    linked = sum(1 for i in items if (i.get("source_url") or "").strip())
    lines = [
        f"**{quoted} of {len(items)} verdict(s) arrived with a verbatim "
        f"supporting quote; {linked} with a direct source URL.**",
        "",
    ]
    if quoted < len(items) or linked < len(items):
        lines += [
            "A verdict missing either one is the model's assertion rather than "
            "something you can open and check. It is not necessarily wrong — but "
            "it has not been shown to be right, and Section 9 will not be able "
            "to confirm it against a document either.",
            "",
        ]
    return lines


def _render_model_failures(report):
    """The failed passes, why they failed, and which section is short a model.

    "WARNING — failed model passes: openai:fact_check" was the whole of it.
    That says a pass died, but not that it died with "Response ended
    prematurely" after 413 seconds, and not that Section 2 was consequently
    built from four models instead of five. The second is the part that changes
    how the rest of the report should be read: consensus counts are votes, and
    a missing voter moves the threshold without moving the number printed
    beside it.
    """
    failures = report.get("model_failures") or []
    if not failures:
        return []

    details = report.get("model_failure_details") or []
    lines = [f"## ⚠ Failed model passes ({len(failures)})", ""]
    if not details:
        # A report written before the details existed. Say what is known.
        lines.append(f"Failed: {', '.join(failures)}")
        lines.append("")
        return lines

    for detail in details:
        elapsed = detail.get("elapsed_seconds")
        after = f" after {elapsed:.0f}s" if isinstance(elapsed, (int, float)) else ""
        lines.append(
            f"- **{detail.get('pass')}** ({detail.get('model')}) failed{after}: "
            f"{detail.get('error')}"
        )
        section = detail.get("section")
        if section:
            lines.append(
                f"  - {section} was built without this model. Its consensus "
                f"counts are out of a smaller pool than the run intended."
            )
    lines.append("")
    return lines


_SEVERITY_HEADING = {
    "critical": "Changed what the models were asked",
    "degrading": "Context the models would have used",
    "advisory": "Run bookkeeping",
}


def _render_handoff_gaps(report):
    """Which handoff fields were missing, what each cost, and the line to paste.

    Sits in the header rather than among the numbered sections, for the same
    reason the failed-passes block does: an incomplete handoff is not a finding
    about the article, it is context for reading every finding below. A reader
    who reaches SECTION 5 without knowing that `PRIMARY CLAIM` was empty will
    read a generic completeness flag as a fact about the draft rather than as
    an artefact of what the pass was told.

    The proposed values are **proposals**. The run was not conducted against
    them and never will be — ``handoff_gaps.assess`` runs after the last model
    call precisely so that it cannot be. Pasting one into the handoff and
    re-running is what makes it real.
    """
    gaps = report.get("handoff_gaps") or []
    if not gaps:
        return []

    lines = [f"## ⚠ Handoff metadata gaps ({len(gaps)})", ""]
    lines.append(
        "These fields were absent from the handoff. Each entry says what the "
        "absence cost *this* run, and what to write instead. Nothing proposed "
        "here was used in the review — a value you have not accepted is not a "
        "value the pipeline may act on. Paste the fix into the handoff and "
        "re-run to get the review these sections could not give you."
    )
    lines.append("")

    for severity in ("critical", "degrading", "advisory"):
        in_tier = [g for g in gaps if g.get("severity") == severity]
        if not in_tier:
            continue
        lines.append(f"### {_SEVERITY_HEADING[severity]}")
        lines.append("")
        for gap in in_tier:
            flag = (
                " — left as its template placeholder" if gap.get("placeholder") else ""
            )
            lines.append(f"**`{gap.get('label', gap.get('field', ''))}`**{flag}")
            lines.append("")
            lines.append(gap.get("impact", ""))
            lines.append("")
            suggestion = gap.get("suggestion")
            if suggestion:
                basis = gap.get("suggestion_basis")
                lines.append(
                    f"*Proposed, not used in this run. From {basis}. Paste it "
                    f"into the handoff if you agree with it:*"
                    if basis
                    else "*Proposed, not used in this run. Paste it into the "
                    "handoff if you agree with it:*"
                )
                lines.append("")
                lines.append("```")
                lines.extend(suggestion.splitlines())
                lines.append("```")
                lines.append("")
            elif gap.get("guidance"):
                lines.append(f"*What to add: {gap['guidance']}*")
                lines.append("")
    return lines


def _metadata_gap_note(report, domain):
    """One line naming the handoff fields this section was built without, or []."""
    missing = [
        g.get("label", g.get("field", ""))
        for g in (report.get("handoff_gaps") or [])
        if domain in (g.get("domains") or [])
    ]
    if not missing:
        return []
    fields = ", ".join(f"`{m}`" for m in missing)
    return [
        f"> **Built without {fields}** — those handoff fields were missing or "
        f"unfilled this run (see *Handoff metadata gaps* above). Findings here "
        f"reflect what the pass was told, not only what is in the draft.",
        "",
    ]


def _render_degradations(report):
    """Knock-on effects of a failed pass — what it cost a *different* section.

    ``report["degradations"]`` has had a producer and a console reader since it
    was added, and no reader for the report itself: the entry that says
    "Sections 2 and 9 are working from an incomplete claim list" scrolled past
    in the terminal and never reached ``run_N_*_review.md`` — the file the
    author keeps, re-reads, and pastes into a chat model. Of the two places it
    could be said, it was only ever said in the one that disappears.

    Sits with the failure blocks for the same reason the console prints it
    there: "perplexity:fact_check failed" and "9 claims verified" are
    separately unremarkable, and it is the link between them that tells a
    reader which numbers below to distrust.
    """
    degradations = report.get("degradations") or []
    if not degradations:
        return []

    lines = [f"## ⚠ Knock-on effects of failed passes ({len(degradations)})", ""]
    for entry in degradations:
        # ``section`` is a comma-joined title string; older reports predate the
        # key entirely. Neither is worth failing a render over.
        where = entry.get("section")
        caused_by = entry.get("caused_by") or []
        heading = where or "Affected sections"
        lines.append(f"**{heading}**")
        if caused_by:
            lines.append(f"- Caused by: {', '.join(caused_by)}")
        lines.append("")
        lines.append(entry.get("detail", ""))
        lines.append("")
    return lines


def _render_domains_not_run(report):
    """Header block naming domains no model reviewed, or [].

    Sits beside the failed-passes block rather than inside it because the two
    say different things about how to read the report. A failed pass narrows
    the pool a section was built from. This removes the section's basis
    entirely, and it is the one a reader is least equipped to notice on their
    own — there is no error, no warning count, and no missing model name
    anywhere in the report to prompt the question.
    """
    details = report.get("domains_not_run") or []
    if not details:
        return []

    lines = [f"## ⚠ Domains not reviewed ({len(details)})", ""]
    for detail in details:
        section = detail.get("section") or detail.get("domain")
        lines.append(
            f"- **{detail.get('domain')}** — {detail.get('reason')}. "
            f"{section} is empty because nothing ran, not because the draft "
            f"is clean."
        )
    lines.append("")
    return lines


def _missing_models_note(report, domain):
    """One line naming the models that failed on ``domain``, or []."""
    missing = [
        d.get("model") or d.get("pass")
        for d in (report.get("model_failure_details") or [])
        if d.get("domain") == domain
    ]
    if not missing:
        return []
    return [
        f"> **Built without {', '.join(missing)}** — that pass failed this run "
        f"(see *Failed model passes* above). Anything only that model would have "
        f"caught is missing here, not absent from the draft.",
        "",
    ]


def _not_run_note(report, domain):
    """One line saying ``domain`` had no reviewer at all, or [].

    The empty section is the whole problem. "_No flags._" under a heading reads
    as a clean result, and for a domain nothing ran it is the opposite — an
    absence of evidence presented in the same words the report uses for
    evidence of absence. Measured 2026-09-05: rendering the never-ran case and
    the clean case produced byte-identical markdown.
    """
    for detail in report.get("domains_not_run") or []:
        if detail.get("domain") != domain:
            continue
        return [
            f"> **Not reviewed this run** — {detail.get('reason')}. This "
            f"section is empty because nothing ran, not because the draft is "
            f"clean.",
            "",
        ]
    return []


def _domain_notes(report, domain):
    """Every caveat that belongs above ``domain``'s section.

    One call site per section so a new caveat reaches all of them at once, and
    so the two cases cannot both be claimed: a domain with a failed pass has a
    result entry, which is exactly what keeps it out of ``domains_not_run``.
    """
    return (
        _missing_models_note(report, domain)
        + _not_run_note(report, domain)
        + _metadata_gap_note(report, domain)
    )


#: How an out-of-scope claim's category reads to someone who did not write the
#: enum. Kept here rather than in ``fact_check_scope`` for the same reason
#: ``_SEO_FIELD_ORDER`` is duplicated: this module stays a renderer over a plain
#: dict. A test asserts every category has a phrase.
_CLAIM_TYPE_PHRASES = {
    "first_person": "a first-person statement only the author can confirm",
    "author_hypothesis": "the author's own hypothesis, framed as such in the draft",
    "internal_arithmetic": "arithmetic derived from figures already in the draft",
    "subjective_judgment": "an evaluation or editorial stance, with no truth value",
    "future_prediction": "a claim about the future, which no current source settles",
    "other": "out of scope for a reason outside the standard categories",
}


def _render_out_of_scope(items):
    """The claims that were never candidates for external verification.

    Rendered by hand rather than through ``_kv_lines`` because the two fields
    that matter most here are lists — which models made the call, and what
    verdict an author marking overrode. ``_kv_lines`` would print those as raw
    Python reprs, and the withdrawn verdict is the whole reason the entry is
    worth reading: "the author marked this out of scope, and openai had it as
    confirmed against <url>" is a disagreement the author should see, not a
    detail to bury.
    """
    if not items:
        return []
    excluded = [i for i in items if i.get("excluded")]
    reported = [i for i in items if not i.get("excluded")]

    lines = [f"### Out of scope for verification ({len(items)})", ""]
    lines.append(
        f"_{len(excluded)} claim(s) were held out of the fact-check verdicts and "
        f"out of Section 9 entirely — no source can settle them, so resolving one "
        f"could only ever return 'the page does not say this'. Nothing here is a "
        f"finding against the claim. Nothing here has been checked either._"
    )
    if reported:
        lines.append("")
        was_were = "was" if len(reported) == 1 else "were"
        lines.append(
            f"_{len(reported)} more {was_were} classified out of scope by a model "
            f"but NOT excluded — this publication does not act on that category "
            f"unprompted, so the claim stayed in verification. It is listed so "
            f"you can see the judgment that was made and overruled._"
        )
    lines.append("")

    for item in items:
        lines.append(f'- "{item.get("claim", "")}"')
        state = "excluded" if item.get("excluded") else "reported only, still checked"
        lines.append(f"  - Status: {state}")
        lines.append(f"  - Decided by: {item.get('excluded_by', 'unknown')}")
        claim_type = item.get("claim_type") or ""
        if claim_type:
            phrase = _CLAIM_TYPE_PHRASES.get(claim_type, claim_type)
            lines.append(f"  - Category: {claim_type} — {phrase}")
        models = item.get("classified_by") or []
        if models:
            lines.append(f"  - Classified out of scope by: {', '.join(models)}")
        if item.get("reason"):
            lines.append(f"  - Reason: {item['reason']}")
        for withdrawn in item.get("withdrawn_verdicts") or []:
            source = (
                withdrawn.get("source_url")
                or withdrawn.get("source")
                or "no source given"
            )
            lines.append(
                f"  - Overrode a verdict: {withdrawn.get('model') or 'a model'} "
                f"had this as `{withdrawn.get('bucket')}` citing {source}. "
                f"The marking wins; the verdict is recorded here rather than used."
            )
    lines.append("")
    return lines


def _render_section_2(fact_check, report=None):
    lines = ["## SECTION 2: Factual Verification", ""]
    lines.extend(_domain_notes(report or {}, "fact_check"))
    if not fact_check:
        lines.append("_No fact-check results._")
        return lines
    if not isinstance(fact_check, dict):
        # The whole pass salvaged to something that is not a mapping. Rare, but
        # it reaches here the same way a bad entry does, and "_No fact-check
        # results._" would describe a pass that never ran.
        lines.append(
            "_The fact-check pass returned something this report cannot read. "
            "Its raw output is in this run's `_report.json` under "
            "`section_2_fact_check`._"
        )
        return lines

    labels = {
        "confirmed": "Confirmed",
        "outdated": "Outdated",
        "contradicted": "Contradicted",
        "unverifiable": "Unverifiable",
        "primary_source_needed": "Primary source resolution required",
    }

    # Filter every findings list before anything reads it, rather than at each
    # of the loops below: ``_render_evidence_coverage`` takes the whole mapping
    # and reaches into the verdict buckets itself, and ``_render_out_of_scope``
    # reads ``excluded`` off every entry it is handed — so a bare string in
    # either would raise before the first heading is written.
    dropped = {}
    cleaned = dict(fact_check)
    for key in tuple(labels) + ("additional_observations", "out_of_scope"):
        if key not in fact_check:
            continue
        cleaned[key], n = _dicts(fact_check.get(key))
        dropped[key] = n
    fact_check = cleaned

    # Above the counts it qualifies, so a reader meets the shortfall before the
    # numbers it applies to rather than after.
    lines.extend(_skipped_note(dropped))
    lines.extend(_render_evidence_coverage(fact_check))
    for key, label in labels.items():
        items = fact_check.get(key, [])
        if not items:
            continue
        lines.append(f"### {label}")
        for item in items:
            claim = item.get("claim", "")
            lines.append(f'- "{claim}"')
            for kv in _kv_lines(item, exclude=("claim",)):
                lines.append(kv)
        lines.append("")

    lines.extend(_render_out_of_scope(fact_check.get("out_of_scope") or []))

    observations = fact_check.get("additional_observations", [])
    if observations:
        lines.append("### Additional observations (fact-check pass)")
        for obs in observations:
            passage = obs.get("passage") or obs.get("observation", "")
            lines.append(f'- "{passage}"')
            for kv in _kv_lines(obs, exclude=("passage",)):
                lines.append(kv)
        lines.append("")

    return lines


def _render_flags_section(title, flags, passage_key="passage", note=(), field=None):
    lines = [f"## {title}", ""]
    lines.extend(note)
    flags, dropped = _dicts(flags)
    lines.extend(_skipped_note({field or title: dropped}))
    if not flags:
        lines.append(
            "_No flag entry in this run could be read._" if dropped else "_No flags._"
        )
        return lines
    for flag in flags:
        passage = flag.get(passage_key, "")
        lines.append(f'- "{passage}"')
        for kv in _kv_lines(flag, exclude=(passage_key,)):
            lines.append(kv)
    lines.append("")
    return lines


def _render_red_team_entry(label, item):
    lines = [f'- **{label}**: "{item.get("passage", "")}"']
    for kv in _kv_lines(item, exclude=("passage",)):
        lines.append(kv)
    return lines


def _render_section_6(red_team, note=()):
    header = ["## SECTION 6: Red Team Findings", ""]
    header.extend(note)
    if not red_team:
        header.append("_No red team results._")
        return header
    if not isinstance(red_team, dict):
        # The whole pass salvaged to something that is not a mapping — same
        # failure mode as a bare string among a findings list, one level up.
        header.append(
            "_The red-team pass returned something this report cannot read. "
            "Its raw output is in this run's `_report.json` under "
            "`section_6_red_team`._"
        )
        return header

    rt_keys = (
        ("most_vulnerable_claim", "Most vulnerable claim"),
        ("highest_audience_risk", "Highest audience risk"),
        ("highest_credibility_risk", "Highest credibility risk"),
    )

    # Not list-shaped, so ``_dicts`` doesn't apply directly — each entry is
    # reached by key rather than by iterating a findings list — but a salvaged
    # non-dict value in any of these spots raises the same
    # ``AttributeError: 'str' object has no attribute 'get'`` a bare list entry
    # does, so it gets the same tolerance and the same count.
    body = []
    dropped = 0
    if "most_vulnerable_claim" in red_team or "highest_audience_risk" in red_team:
        # Single-source: flat dict with the three well-known keys.
        for key, label in rt_keys:
            item = red_team.get(key)
            if item is None:
                continue
            if not isinstance(item, dict):
                dropped += 1
                continue
            body.extend(_render_red_team_entry(label, item))
        body.append("")
    else:
        # Multi-source: keyed by model name.
        for model_name, data in red_team.items():
            if not isinstance(data, dict):
                dropped += 1
                continue
            weight = data.get("_weight")
            weight_str = f" (weight {weight})" if weight is not None else ""
            body.append(f"### {model_name}{weight_str}")
            for key, label in rt_keys:
                item = data.get(key)
                if item is None:
                    continue
                if not isinstance(item, dict):
                    dropped += 1
                    continue
                body.extend(_render_red_team_entry(label, item))
            body.append("")

    return header + _skipped_note({"section_6_red_team": dropped}) + body


def _render_section_7(low_confidence):
    lines = [
        "## SECTION 7: Low-Confidence Flags",
        "_For awareness only — dismiss unless something catches your attention._",
        "",
    ]
    low_confidence, dropped = _dicts(low_confidence)
    lines.extend(_skipped_note({"section_7_low_confidence": dropped}))
    if not low_confidence:
        lines.append(
            "_No low-confidence entry in this run could be read._"
            if dropped
            else "_None._"
        )
        return lines
    for item in low_confidence:
        passage = item.get("passage") or item.get("passage_reference", "")
        source = item.get("source_model", "?")
        domain = item.get("domain", "?")
        lines.append(f'> "{passage}" — {source}:{domain}')
        for kv in _kv_lines(
            item, exclude=("passage", "passage_reference", "source_model", "domain")
        ):
            lines.append(f"> {kv.strip('- ')}")
    lines.append("")
    return lines


def _render_section_8(additional):
    lines = ["## SECTION 8: Additional Findings", ""]
    additional, dropped = _dicts(additional)
    lines.extend(_skipped_note({"section_8_additional": dropped}))
    if not additional:
        lines.append(
            "_No additional-findings entry in this run could be read._"
            if dropped
            else "_None._"
        )
        return lines
    lines.append(
        "_Most-corroborated first, then by the flagging model's stated "
        "confidence. Confidence is self-reported and not comparable between "
        "providers, so it only breaks ties between observations no other model "
        "raised._"
    )
    lines.append("")
    for obs in additional:
        passage = obs.get("passage", "")
        category = obs.get("category", "?")
        models = obs.get("models") or [obs.get("source_model", "?")]
        domain = obs.get("source_domain", "?")
        if len(models) > 1:
            who = f"{', '.join(models)} — {len(models)} models agree"
        else:
            who = f"{models[0]}:{domain}"
        lines.append(f'- [{category}] "{passage}" — flagged by {who}')
        for kv in _kv_lines(
            obs,
            exclude=(
                "passage",
                "category",
                "source_model",
                "source_domain",
                # Rendered in the bullet above; repeating them reads as data the
                # model supplied rather than as bookkeeping this pass added.
                "models",
                "model_count",
            ),
        ):
            lines.append(kv)
    lines.append("")
    return lines


#: Retrieval fields ``_render_archive_pair`` renders itself, beside the URLs
#: they are about. Appended to each caller's own exclude list so nothing is said
#: twice, and so the access story stays next to the links it governs. (``url``
#: and ``final_url`` are excluded by the callers directly — the pair block has
#: rendered those since the redirector fix.)
#:
#: ``verified_via`` is here because it is an enum — ``tls_impersonation`` told a
#: reader nothing, and the resolver already writes the sentence that does: one
#: per retrieval route, ``reader_access`` for an escalated fetch and
#: ``archive_provenance`` for one read from the archive. ``archive_provenance``
#: stays in the dump below, where it has always rendered, so each route states
#: itself exactly once.
#:
#: A plain direct fetch says nothing at all, which is the point: it is the
#: unremarkable case, and a line on every entry reporting that nothing happened
#: would bury the entries where something did.
_PAIR_RENDERED_FIELDS = ("verified_via", "reader_access")


def _citation_pair(citation):
    """Return the live URL and its archive URL, for a citation the author can paste.

    A citation that names only a live URL is durable until the page moves,
    changes, or 403s — which link-check runs show happening constantly. Pairing
    every live link with its Wayback snapshot is what makes the citation survive
    the source. The pipeline has always *collected* the snapshot URL; it was
    never rendered anywhere a human would see it, so the pairing existed in the
    data and nowhere in the output.

    Returns ``(live_url, archive_url_or_None)``.
    """
    wayback = citation.get("wayback") or {}
    # The resolved URL when the citation came in through a redirector. A
    # grounded model cites as `vertexaisearch.cloud.google.com/
    # grounding-api-redirect/AUZIY...` — 271 opaque characters that name no
    # publication, and that the reader is being asked to go and check. The page
    # behind it was fetched and read by this pass, so its address is known.
    live = citation.get("final_url") or citation.get("url", "")
    return live, wayback.get("snapshot_url")


#: Verdicts from ``resolver._verify_archive_matches``. Duplicated rather than
#: imported for the same reason as ``_ARCHIVE_*`` — see the note there.
_MATCH_IDENTICAL = "identical"
_MATCH_DIFFERS = "differs"
_MATCH_UNCHECKED = "unchecked"


def _archive_match_lines(citation, indent):
    """Whether the archived copy was confirmed to say what the live page said.

    The report tells the author to *cite both* the live URL and the archive
    copy. That is a recommendation they act on, and nothing used to establish
    that the two say the same thing — a snapshot of a paywall, a cookie wall, a
    bot block, or a much older version of the page renders exactly like a good
    one. The pairing now carries the result of actually checking.

    Silent when nothing was checked and nothing is wrong to report, so an
    ordinary verified citation does not grow a line saying so twice.
    """
    verdict = citation.get("archive_match")
    if not verdict:
        return []
    detail = citation.get("archive_match_detail") or ""
    if verdict == _MATCH_IDENTICAL:
        return [
            f"{indent}- Archive verified: the snapshot's text is identical to "
            f"the live page this run checked, so the pairing below is safe to "
            f"publish."
        ]
    if verdict == _MATCH_DIFFERS:
        return [f"{indent}- **Archive does NOT match the live page.** {detail}"]
    return [
        f"{indent}- Archive not verified against the live page — {detail} The "
        f"snapshot may or may not contain the document."
    ]


def _capture_note(citation):
    """Suffix for the Archive line saying what this run's submission produced.

    Only ever describes the snapshot's own timestamp, because that is the only
    thing actually known. Save Page Now does not always make a new capture: on
    2026-09-05 a live save of an IANA page redirected to a snapshot dated
    2026-08-31, five days old. The first version of this line said "captured
    this run" for any submission that came back with a URL, which would have
    reported that five-day-old copy as fresh — the same species of overstatement
    as calling a submission "archived".

    An existing snapshot returned instead of a new capture is worth saying out
    loud rather than papering over: it tells the author archive.org declined to
    re-capture, which is why a page they just asked to archive still carries an
    older date.
    """
    wb = citation.get("wayback") or {}
    if wb.get("archive_outcome") != _ARCHIVE_ARCHIVED:
        return ""
    age = wb.get("snapshot_age_days")
    if age == 0:
        return " — snapshot dated today"
    if age:
        plural = "day" if age == 1 else "days"
        return (
            f" — archive.org returned an existing snapshot {age} {plural} old "
            f"rather than making a new capture"
        )
    return ""


def _render_archive_pair(citation, indent="  "):
    """Lines pairing a citation's live URL with its archive copy.

    Says which state applies rather than silently omitting the archive line,
    because the states need different follow-up from the author.

    The distinction this function exists to hold, and which it previously did
    not: **"submitted" is not "archived".** The old wording — "submitted to the
    Wayback Machine this run; the snapshot URL appears on the next run once
    archive.org has captured it" — asserted a future that nothing checked. A
    capture archive.org accepted and then dropped rendered identically to one it
    completed, and the author had no way to tell, this run or any later one.
    Only ``archived`` says a snapshot exists, and it is only ever set alongside
    the URL of that snapshot.

    The negative states are the ones worth being careful about, and there are
    five, not two:

    * ``archived: None`` — a lookup was made and did not complete (the circuit
      breaker tripped, or the request failed). NOT the same as "there is no
      snapshot"; reporting it as "none" would assert something this run never
      established, and the breaker makes it the common case rather than a rare
      one. Re-running can still answer it.
    * no ``wayback`` key at all — archive.org was never asked, because the fetch
      failed in a way the archive fallback deliberately does not cover (404,
      5xx) or must not be used for (a non-public address). Rendering that as
      "NOT CHECKED" would imply a lookup that could succeed next time; none was
      attempted and none will be.
    * ``archived: False`` — archive.org answered and has no snapshot.
    * submission failed — archive.org refused the request outright.
    * capture failed — archive.org took the job and then could not capture it.
      This is the one that used to be invisible, and the one that repeats
      silently run after run if nobody names it.

    The missing-key branch has to come before the null one: ``{}.get("archived")
    is None`` is True, so an absent dict would otherwise fall into "NOT CHECKED".

    Reading "never asked" out of an absent key is only sound because the
    resolver now always records the answer when a lookup ran, and because
    ``history.save_run`` renders a report as it is built rather than re-rendering
    old JSON — citations written before that change dropped the answer on the
    failure path, and would land here claiming nobody looked.
    """
    live, archive = _citation_pair(citation)
    if not live:
        return []
    out = [f"{indent}- Live: {live}"]
    # Immediately under the URL, not down in the key/value dump: this is what
    # the author needs while deciding what to paste into the article.
    friction = citation.get("reader_access")
    if friction:
        out.append(f"{indent}- Reader access: {friction}")
    wb = citation.get("wayback") or {}
    outcome = wb.get("archive_outcome")
    detail = wb.get("archive_outcome_detail")

    if archive:
        stale = (
            " (STALE — re-archive before relying on it)"
            if wb.get("snapshot_stale")
            else ""
        )
        out.append(f"{indent}- Archive: {archive}{stale}{_capture_note(citation)}")
        if wb.get("snapshot_is_error_capture"):
            # A snapshot exists, and it is a capture of an error page. "Archived"
            # would be true and useless: what is preserved is the refusal, not
            # the document. Our own captures cannot produce this (capture_all=0),
            # but a pre-existing snapshot is outside our control.
            out.append(
                f"{indent}- **This snapshot is a capture of an HTTP "
                f"{wb.get('snapshot_status')} response, not of the document.** "
                f"Something is archived at that URL; the source is not. Archive "
                f"it by hand or re-source the claim."
            )
        out.extend(_archive_match_lines(citation, indent))
        out.append(f"{indent}- Cite both: {live} (archived: {archive})")
    elif outcome == _ARCHIVE_SUBMIT_FAILED:
        out.append(
            f"{indent}- Archive: SUBMISSION FAILED — archive.org did not accept "
            f"the request to capture this URL ({detail or 'no reason given'}). "
            f"It is NOT archived. Archive it by hand, or re-run."
        )
    elif outcome == _ARCHIVE_NOT_ATTEMPTED:
        # "re-run once archiving succeeds" would be a promise nothing is going
        # to keep for an internal address, and a misleading one for a host that
        # would not resolve — same reasoning as the unresolved-citation branch
        # at the bottom of this function.
        out.append(
            f"{indent}- Archive: NOT SUBMITTED — {detail or 'no reason recorded'}. "
            f"This URL is not archived and nothing in this run tried to archive "
            f"it."
        )
    elif outcome == _ARCHIVE_CAPTURE_FAILED:
        out.append(
            f"{indent}- Archive: CAPTURE FAILED — archive.org accepted the "
            f"request and then could not capture the page "
            f"({detail or 'no reason given'}). It is NOT archived, and "
            f"re-running will most likely fail the same way. Archive it by hand."
        )
    elif outcome == _ARCHIVE_PENDING:
        out.append(
            f"{indent}- Archive: SUBMITTED, OUTCOME UNKNOWN — archive.org was "
            f"still capturing this URL when the report was written"
            f"{f' ({detail})' if detail else ''}. Nothing here establishes that "
            f"the capture succeeded; the next run reads the job's outcome and "
            f"says which way it went."
        )
    elif outcome == _ARCHIVE_SUBMITTED or wb.get("submitted"):
        # Deliberately does not say archive.org "accepted" anything: this branch
        # also covers a request that timed out or was abandoned, where even the
        # acceptance is unestablished. Everything asserted here is something the
        # run actually observed.
        out.append(
            f"{indent}- Archive: SUBMITTED, OUTCOME UNKNOWN — a capture request "
            f"went out for this URL and no snapshot came back"
            f"{f' ({detail})' if detail else ''}. That it was asked for is all "
            f"this establishes — treat the URL as unarchived until a snapshot "
            f"appears."
        )
    elif not wb:
        out.append(
            f"{indent}- Archive: NOT LOOKED UP — archive.org was never asked "
            f"about this URL, because the fetch failed in a way an archived copy "
            f"does not stand in for (a 404 or 5xx), or the address was not one we "
            f"would hand to a third party. Re-running will not ask either. This "
            f"says nothing about whether the page is archived — check by hand."
        )
    elif wb.get("archived") is None:
        out.append(
            f"{indent}- Archive: NOT CHECKED — the archive.org lookup did not "
            f"complete this run. This says nothing about whether the page is "
            f"archived; re-run to find out."
        )
    elif citation.get("resolved"):
        # A citation that only came back because the fetch escalated is the one
        # case where "as durable as the live URL" understates the problem: the
        # live URL already refused a client once. Saying the absence out loud is
        # the point — an escalated citation with no archive beside it is the
        # weakest thing this section can produce, and it should not read like
        # the ordinary not-yet-archived case.
        if citation.get("verified_via") == "tls_impersonation":
            out.append(
                f"{indent}- Archive: NONE — and this is the citation that most "
                f"needed one. The source refused an ordinary automated request, "
                f"so the live URL is both the only copy and the fragile kind. "
                f"Archive it by hand before publishing, or re-source the claim."
            )
        else:
            out.append(
                f"{indent}- Archive: none. This citation is only as durable as "
                f"the live URL — re-run once archiving succeeds, or archive it "
                f"by hand."
            )
    else:
        # Unresolved: the live URL did not yield readable content this run, and
        # archive.org confirmed it has no snapshot either. Neither half of the
        # resolved wording holds — there is no fetched copy for the live URL to
        # be "as durable as", and archiving is never submitted for an unresolved
        # citation (see resolver._submit_missing_archives, which requires
        # `resolved`), so "re-run once archiving succeeds" would be a promise
        # nothing in the pipeline is going to keep.
        out.append(
            f"{indent}- Archive: none — archive.org answered and has no snapshot "
            f"of this URL, and the live fetch did not succeed either, so no "
            f"readable copy was obtained from anywhere. Unresolved citations are "
            f"not submitted for archiving; archive it by hand if you keep it."
        )

    # The escalation warning above lives in the `resolved` branch, which only
    # citations that were never submitted ever reach — and an escalated citation
    # is resolved, so `_submit_missing_archives` always submits it. That made the
    # warning nearly unreachable in a real run: it fired only for the rare
    # escalated-and-non-public combination, and the common case (submitted,
    # capture pending or failed) quietly got the milder wording meant for
    # ordinary citations. An escalated source with no snapshot is in the same
    # weak position however the archiving ended, so say it there too — once,
    # only when the branch above did not already say it.
    if (
        citation.get("verified_via") == "tls_impersonation"
        and not archive
        and outcome
        and outcome != _ARCHIVE_ARCHIVED
    ):
        out.append(
            f"{indent}- This is the citation that most needed an archive: the "
            f"source refused an ordinary automated request, so the live URL is "
            f"both the only copy and the fragile kind. Archive it by hand before "
            f"publishing, or re-source the claim."
        )

    # A capture that failed on an earlier run and is being retried. Worth a line
    # of its own: a URL that never archives across several runs looks like bad
    # luck one report at a time, and like a page archive.org cannot capture once
    # you can see the reason repeating.
    prior = wb.get("prior_capture_failure")
    if prior and outcome != _ARCHIVE_ARCHIVED:
        run = prior.get("run_number")
        where = f" (run {run})" if run else ""
        out.append(
            f"{indent}- Archive history: a previous capture of this URL "
            f"failed{where} — {prior.get('reason') or 'no reason given'}."
        )
    return out


#: Disposition buckets, strongest retrieval first. The order is the order the
#: reader meets them: "we fetched and read the document" before "we never looked
#: it up", so the section opens on its best evidence and degrades honestly.
#:
#: Keyed by ``verification`` tier, with the untiered entries split in two. That
#: split matters: an entry with no tier but a URL was a real fetch that was
#: refused (403, 404, DNS), which is a different fact about the claim than never
#: having had a URL at all — and it is usually actionable, because a publisher
#: that refuses an automated fetch will often serve the same page to a person.
#: Re-exported from the citation adapters so this renderer and the console run
#: summary classify identically. They used to do it separately and disagreed.
_DISPOSITIONS = DISPOSITIONS

#: The two dispositions where a document was genuinely retrieved *and* its text
#: read by the relevance check. "Checked" means these and only these — the
#: verdict then splits them. Conflating "checked" with "supports" is the same
#: mistake this section exists to stop a reader making, one level up.


_disposition = disposition


#: How each re-ask answer reads to someone deciding what to do about the claim.
_REASK_LEAD = {
    "correct_claim": "says the source is right and the claim was wrong",
    "different_source": "stands by the claim and proposes a different source",
    "withdraw": "withdraws the claim",
    "stand": "maintains the claim and disputes the refutation",
}

#: Re-ask answers in which the asserting model faulted the *claim* rather than
#: the citation. Both mean the draft is what has to change: ``correct_claim``
#: supplies replacement wording, ``withdraw`` says the claim should go.
#:
#: The other two deliberately do not belong here. ``different_source`` stands by
#: the claim and blames the URL, which is precisely the citation problem the
#: default guidance describes; ``stand`` disputes the refutation outright and
#: concedes nothing. Reading either as "the claim was wrong" would put words in
#: the model's mouth in the one block where the section is trying to stop
#: exactly that kind of substitution.
_REASK_CONCEDES = frozenset({"correct_claim", "withdraw"})


def _render_reask(reask):
    """What the asserting model said when handed its own refutation.

    Rendered as the model's answer, never as a resolution. The verdict above it
    is unchanged and stays unchanged: this is the author being told what the
    model would do about it, and a ``stand`` is reported as plainly as a
    ``withdraw`` so that "the model disagrees" is visible rather than absorbed.
    """
    if not reask:
        return []
    action = reask.get("action", "")
    who = reask.get("asked_model", "the asserting model")
    lead = _REASK_LEAD.get(action, "responded")
    lines = [f"  - **Asked {who} again** — {lead}."]
    if reask.get("reason"):
        lines.append(f"    - Its reason: {reask['reason']}")
    if action == "correct_claim" and reask.get("corrected_claim"):
        lines.append(f'    - Proposed wording: "{reask["corrected_claim"]}"')
    if action == "different_source" and reask.get("source_url"):
        lines.append(f"    - Proposed source: {reask['source_url']}")

    check = reask.get("source_check")
    if check:
        # The proposal was put through the same resolution the original URL
        # failed. Reporting the proposal without this would hand the author a
        # URL on the strength of the model having named it, which is the tier
        # confusion the whole section exists to prevent.
        verification = check.get("verification")
        if verification == "checksum":
            outcome = "fetched, read, and it does support the claim"
        elif verification == "content_mismatch":
            verdict = check.get("relevance_verdict") or "does not support"
            outcome = f"fetched and read, and it does NOT support the claim ({verdict})"
        elif verification == "unverifiable":
            outcome = "fetched, but its content could not be read or assessed"
        else:
            outcome = "could not be resolved"
        lines.append(f"    - That proposed source was checked: {outcome}.")
        # The run already archived this URL if it needed archiving, so the
        # author gets the durable address here rather than being sent to find
        # it. Only offered when the pairing is safe — the same three states the
        # reference list withholds.
        snapshot = check.get("snapshot_url")
        if snapshot and not check.get("snapshot_is_error_capture"):
            if check.get("archive_match") == _MATCH_DIFFERS:
                lines.append(
                    f"    - Archive of that source: {snapshot} — **does not "
                    f"match the live page**; check before citing it."
                )
            elif check.get("archive_match") == _MATCH_IDENTICAL:
                lines.append(
                    f"    - Archive of that source: {snapshot} (verified "
                    f"identical to the live page)."
                )
            else:
                lines.append(f"    - Archive of that source: {snapshot}")
    elif action == "different_source":
        lines.append("    - That proposed source was NOT checked. Treat it as a lead.")
    return lines


def _render_mismatch_entry(citation):
    """One content-mismatch entry, with the actionable content at the top.

    Fields used to render in whatever order the resolver happened to write them,
    which put a 546-character ``content_summary``, a 271-character opaque
    redirect URL and two more of them under ``alternates_checked`` above the one
    line an author acts on. On the Honda run the re-ask's proposed correction —
    the draft says "January 1, 2003" where the source says "1998 or 2002" — was
    the last line of a 2,682-character entry.

    The order is now: what the check found, what to do about it, then the
    evidence it rests on. The verdict still leads, so ``_render_reask``'s framing
    survives the move: the reader meets the finding before the asserting model's
    answer to it, and the answer is never presented as having settled anything.

    Nothing is dropped, because the point of the section is that a reader can
    audit a tier instead of trusting it — the checksum, the relevance quote, the
    archive pairing and the alternate URLs all still render. ``note`` is the sole
    suppression, and only in the specific case where it restates the relevance
    reason printed two lines above it: the resolver builds it as verdict +
    reason + alternates count, and all three of those now have their own lines.
    A ``note`` that says anything else is kept, so the test is on the string
    rather than on the disposition.
    """
    c = citation
    lines = [f'- "{c.get("claim", "")}"']

    verdict = c.get("relevance_verdict")
    if verdict:
        lines.append(f"  - Relevance verdict: {verdict}")
    reason = c.get("relevance_reason")
    if reason:
        lines.append(f"  - Relevance reason: {reason}")

    lines.extend(_render_reask(c.get("reask")))
    lines.extend(_render_archive_pair(c))

    exclude = [
        "claim",
        "resolved",
        "reask",
        "url",
        "final_url",
        "relevance_verdict",
        "relevance_reason",
        *_PAIR_RENDERED_FIELDS,
    ]
    if reason and reason in (c.get("note") or ""):
        exclude.append("note")
    lines.extend(_kv_lines(c, exclude=tuple(exclude)))
    return lines


def _html_escape(value):
    """Escape a URL for use inside an HTML attribute and as link text.

    Four replacements rather than ``html.escape`` so this module keeps the
    zero-import property the comments above rely on. Ampersand first, or it
    would double-escape the entities the other replacements introduce.

    Query strings routinely carry ``&`` (``?a=1&b=2``), and pasting that raw
    into an ``href`` produces a link that silently drops everything after the
    first parameter — a broken citation that looks fine in the editor.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _publishable_archive(citation, archive):
    """The archive URL only when the pairing is safe to publish unreviewed.

    Withholds exactly the three states the per-entry list flags: a snapshot that
    captured an error page rather than the document, one whose text does not
    match the live page, and one that could not be checked this run. A
    copy-paste block is acted on without re-reading the reasoning above it, so
    anything questionable must not be in it — the markdown list above still
    names them, with why.
    """
    if not archive:
        return None
    wb = citation.get("wayback") or {}
    if wb.get("snapshot_is_error_capture"):
        return None
    if citation.get("archive_match") in (_MATCH_DIFFERS, _MATCH_UNCHECKED):
        return None
    return archive


def _html_reference_block(entries):
    """The same reference list as pasteable HTML.

    The markdown above is paste-ready for a markdown-authored article; a
    WordPress block editor wants anchors. Same sources, same order, and every
    source appears — one that has no publishable archive copy is still a
    reference the article cites, so it goes in as a plain link rather than
    being dropped from the list the author pastes.
    """
    if not entries:
        return []
    lines = ["```html", "<ol>"]
    withheld = 0
    for live, archive, c in entries:
        safe = _publishable_archive(c, archive)
        if archive and not safe:
            withheld += 1
        href = _html_escape(live)
        if safe:
            lines.append(
                f'  <li><a href="{href}">{href}</a> '
                f'(<a href="{_html_escape(safe)}">archived</a>)</li>'
            )
        else:
            lines.append(f'  <li><a href="{href}">{href}</a></li>')
    lines.append("</ol>")
    lines.append("```")
    lines.append("")
    carrying = sum(1 for live, a, c in entries if _publishable_archive(c, a))
    note = f"{carrying} of {len(entries)} carry an archive link."
    if withheld:
        note += (
            f" {withheld} archived source(s) are linked live-only here because "
            f"the snapshot was not confirmed to match the page — see the list "
            f"above for which and why."
        )
    lines.append(note)
    lines.append("")
    return lines


def _render_reference_list(citations):
    """Every cited address once, with its archive copy, ready to publish.

    The pairing already existed per citation, but only inside the diagnostic
    entries — spread over five disposition buckets and interleaved with claim
    text, verification tiers and relevance notes. That is the right shape for
    deciding whether to trust a citation and the wrong shape for the job the
    author actually has next, which is putting both addresses into the article.
    Getting a reference list out of it meant reading the whole section and
    transcribing by hand, and a source backing three claims appeared three
    times.

    So: one entry per address, in the order first cited, deduplicated, split by
    whether an archive copy exists. Sources with no snapshot are listed rather
    than dropped — the author needs to know which references the article cannot
    carry an archive link for, and silence would read as "all of them are fine".

    Where the archive was checked against the live page (see
    ``resolver._verify_archive_matches``) an unconfirmed pairing is marked. It
    is the only part of this list that is a judgement rather than a fact, and it
    is the one the author would otherwise publish blind.
    """
    paired, live_only, seen = [], [], set()
    for c in citations:
        live, archive = _citation_pair(c)
        if not live or live in seen:
            continue
        seen.add(live)
        (paired if archive else live_only).append((live, archive, c))

    if not paired and not live_only:
        return []

    lines = [
        f"### Reference list — live and archived addresses "
        f"({len(paired) + len(live_only)} source(s))",
        "",
        "Each source once, in the order first cited. Paste-ready: the archive "
        "address is what keeps the citation readable after the live page moves, "
        "changes, or starts refusing readers.",
        "",
    ]

    if paired:
        for i, (live, archive, c) in enumerate(paired, 1):
            lines.append(f"{i}. {live}")
            wb = c.get("wayback") or {}
            note = ""
            if wb.get("snapshot_is_error_capture"):
                note = (
                    f"  — **do not publish this pairing**: the snapshot captured "
                    f"an HTTP {wb.get('snapshot_status')} response, not the document"
                )
            elif c.get("archive_match") == _MATCH_DIFFERS:
                note = "  — **archive does not match the live page**; check before publishing"
            elif c.get("archive_match") == _MATCH_UNCHECKED:
                note = "  — archive not verified against the live page this run"
            elif wb.get("snapshot_stale"):
                note = "  — snapshot is stale; re-archive before relying on it"
            lines.append(f"   archived: {archive}{note}")
        lines.append("")

    if live_only:
        lines.append(
            f"**No archive copy ({len(live_only)})** — publishable only as a "
            f"live link, and only as durable as that link:"
        )
        for live, _archive, c in live_only:
            wb = c.get("wayback")
            if not isinstance(wb, dict):
                why = "archive.org was never asked about this URL"
            elif wb.get("archived") is None:
                why = "the archive.org lookup did not complete this run"
            elif wb.get("archive_outcome") == _ARCHIVE_CAPTURE_FAILED:
                why = "archive.org tried to capture it and could not"
            elif wb.get("archive_outcome") in (_ARCHIVE_PENDING, _ARCHIVE_SUBMITTED):
                why = "submitted for archiving this run; no snapshot yet"
            elif wb.get("archive_outcome") == _ARCHIVE_NOT_ATTEMPTED:
                why = "not submitted for archiving"
            else:
                why = "archive.org has no snapshot of this URL"
            lines.append(f"- {live} — {why}")
        lines.append("")

    lines.append("For pasting into an HTML editor:")
    lines.append("")
    lines.extend(_html_reference_block(paired + live_only))
    return lines


def _render_section_9(citations, out_of_scope=()):
    # The heading carries the framing deliberately. "Citations" alone reads as a
    # list of sources backing the article, which invites more confidence than
    # the tiers below have earned — in a real run 18 of 144 claims had a document
    # fetched and read, and the rest rested on a model asserting a source
    # exists. "SECTION 9" and "Citations" both stay greppable: the paste-based
    # revision loop in handoff_templates/revise_after_review_prompt.md refers to
    # this section by name.
    lines = ["## SECTION 9: Citations — what was actually checked", ""]

    # Once, at the top: every count, percentage and tier below is derived from
    # this list, and ``_render_reference_list`` re-reads it, so filtering here
    # is what keeps all of them consistent with each other. ``out_of_scope`` is
    # filtered on the same line because it is read the same way, one statement
    # later.
    citations, dropped = _dicts(citations)
    scoped, scoped_dropped = _dicts(out_of_scope)
    lines.extend(
        _skipped_note({"section_9_citations": dropped, "out_of_scope": scoped_dropped})
    )
    held = [i for i in scoped if i.get("excluded")]
    if held:
        # Before the "N of M were checked" line, not after: M is a smaller
        # number than the fact-check pass raised, and a reader who works that
        # out for themselves will read it as claims having gone missing.
        lines.append(
            f"**{len(held)} claim(s) never entered resolution** — they were "
            f"marked out of scope for verification, so no source was fetched for "
            f"them and they are not counted below. They are listed in Section 2 "
            f"under *Out of scope for verification*, with the reason for each."
        )
        lines.append("")
    if not citations:
        # Distinguished because "not attempted" is a claim about the run, and
        # with entries dropped it is false — resolution ran and its output was
        # unreadable. This also keeps ``total`` non-zero for the percentage.
        lines.append(
            "_No citation entry in this run could be read._"
            if dropped
            else "_No citation resolution attempted._"
        )
        return lines

    grouped = {key: [] for key, _ in _DISPOSITIONS}
    for c in citations:
        grouped[_disposition(c)].append(c)

    verified = grouped["checksum"]
    mismatch = grouped["content_mismatch"]
    unverifiable = grouped["unverifiable"]
    fetch_failed = grouped["fetch_failed"]
    pointer = grouped["pointer"]
    no_source = grouped["no_source"]
    total = len(citations)
    checked = len(verified) + len(mismatch)

    # Lead with the fraction, not a tier breakdown. The breakdown was accurate
    # but made the reader do arithmetic across four numbers to learn the one
    # thing that governs how much of this section to trust.
    #
    # "Checked" counts both read dispositions, not just the supporting one. A
    # claim whose source was fetched, read, and found not to back it was checked
    # — the check returned "no". Reporting only the confirmations as "checked"
    # would repeat, in the summary line, exactly the conflation between "a model
    # says so" and "a document was read" that the tiers below exist to separate.
    lines.append(
        f"**{checked} of {total} claim(s) ({round(100 * checked / total)}%) were "
        f"checked against a document the pipeline fetched and read** — "
        f"{len(verified)} where the document supported the claim, "
        f"{len(mismatch)} where it did not."
    )
    lines.append("")
    lines.append(
        f"For the other {total - checked}, no document was read. What stands "
        "behind those claims is a model asserting that a source exists and "
        "supports it — recalled from training data or read off a search result. "
        "That is a research lead, not a verification. The table says which is "
        "which; every claim is in exactly one row."
    )
    lines.append("")
    lines.append("| What happened | Claims |")
    lines.append("| --- | ---: |")
    for key, label in _DISPOSITIONS:
        lines.append(f"| {label} | {len(grouped[key])} |")
    lines.append("")

    # The deliverable, before the diagnostics. Everything below this point helps
    # the author decide what to trust; this is what they act on once they have.
    lines.extend(_render_reference_list(citations))

    # Relate the fact-check pass's own verdict to whether anything was retrieved.
    # These are independent — one is model judgment, the other is retrieval — and
    # a reader who sees "Bucket: confirmed" beside a claim with no URL reasonably
    # reads it as corroboration. In the run that motivated this, 85 claims came
    # back "confirmed"; 9 were read and supported, 9 were read and were not, and
    # for the remaining 67 no document was read at all.
    confirmed = [c for c in citations if c.get("fact_check_bucket") == "confirmed"]
    if confirmed:
        supported = sum(1 for c in confirmed if _disposition(c) == "checksum")
        refuted = sum(1 for c in confirmed if _disposition(c) == "content_mismatch")
        unread = len(confirmed) - supported - refuted
        lines.append(
            f"> **The fact-check pass called {len(confirmed)} of these claims "
            f'"confirmed." Of those, {supported} had a document fetched and read '
            f"here that supports the claim, {refuted} had one that does not, and "
            f"for {unread} no document was read at all.** A `confirmed` bucket is "
            "that pass's judgment about the claim, not a retrieval result. Where "
            "the two disagree, this section is the one that opened the document."
        )
        lines.append("")

    # A wholesale archive-lookup failure is invisible one entry at a time: every
    # citation reads "Archive: NOT CHECKED", and a reader skimming for archive
    # coverage concludes nothing is archived. Say it once, with a count.
    #
    # The circuit breaker makes this the common case rather than a rare one —
    # once archive.org has 429'd enough times the run stops asking, so every
    # remaining citation carries a null from that point on. That is a statement
    # about the run, not about the pages.
    unchecked = [
        c
        for c in citations
        if isinstance(c.get("wayback"), dict)
        and c["wayback"].get("archived") is None
        and c.get("url")
    ]
    if unchecked:
        rate_limited = sum(1 for c in unchecked if c["wayback"].get("rate_limited"))
        reason = (
            " archive.org rate-limited this run (HTTP 429)"
            if rate_limited == len(unchecked)
            else f" {rate_limited} of them to archive.org rate limiting"
            if rate_limited
            else ""
        )
        lines.append(
            f"> **Archive status is unknown for {len(unchecked)} of these "
            f"citations.**{reason}. `archived: null` means the lookup did not "
            f"complete, **not** that the page is unarchived — and nothing was "
            f"submitted for archiving on that basis. Re-run to find out."
        )
        lines.append("")

    changed = [c for c in verified if c.get("content_changed_since")]
    if changed:
        lines.append(f"### ⚠ Content changed since prior checksum ({len(changed)})")
        for c in changed:
            drift = c["content_changed_since"]
            when = f" on {drift['prior_date']}" if drift.get("prior_date") else ""
            lines.append(f'- "{c.get("claim", "")}"')
            # Where the fetch landed, not the address the model cited. A
            # grounded-model citation arrives as a `grounding-api-redirect`
            # wrapper that names no publication and expires within days; this
            # block is telling the author to go and re-check a source, so it
            # has to name one they can open. Every other URL in the section
            # goes through `_citation_pair` for this reason -- the drift block
            # predates it and kept reading `url` directly.
            lines.append(f"  - URL: {_citation_pair(c)[0]}")
            lines.append(
                f"  - Last matched in run {drift.get('prior_run')} of "
                f"'{drift.get('prior_article')}'{when} — content has since changed. "
                "Previously-verified claim may need re-checking."
            )
        lines.append("")

    if verified:
        # One-line gloss per tier. CITATIONS.md calibrates these carefully, but
        # the reader meets them here first, and "Verified" alone invites more
        # trust than the tier earns (audit finding 18).
        lines.append(
            f"### Read, and supports the claim ({len(verified)}) — checksummed "
            "against fetched content"
        )
        lines.append(
            "_Source fetched, checksummed, and a model confirmed the extracted "
            "text supports the claim — with a quote checked against the page. "
            "The strongest tier; still one cheap model call, not a human._"
        )
        lines.append("")
        for c in verified:
            lines.append(f'- "{c.get("claim", "")}"')
            lines.extend(_render_archive_pair(c))
            for kv in _kv_lines(
                c,
                exclude=(
                    "claim",
                    "resolved",
                    "url",
                    "final_url",
                    "content_changed_since",
                )
                + _PAIR_RENDERED_FIELDS,
            ):
                lines.append(kv)
        lines.append("")

    if mismatch:
        # These used to render inside "Unresolved", indistinguishable from
        # claims nothing was ever fetched for. They are the opposite: the only
        # rows in the section where a document was retrieved, read, and found
        # not to back the claim it was cited for. README.md calls them among the
        # most actionable findings a run produces, so they get their own block,
        # directly under the confirmed ones.
        lines.append(
            f"### Read, and does NOT support the claim ({len(mismatch)}) — check these"
        )
        lines.append(
            "_The source fetched and read cleanly; the relevance check came back "
            "saying it does not back this specific claim. Read the verdict before "
            "reacting to it, because the two kinds mean different things._"
        )
        lines.append("")
        # "contradicts" means the document says otherwise and the draft may be
        # wrong. "not_addressed"/"inconclusive" much more often means the URL was
        # wrong or the extraction missed the relevant part — a citation problem,
        # not a factual one. Collapsing them would overstate the first and bury
        # the second.
        #
        # "Much more often", though, is not "always", and this guidance used to
        # be written as though it were: with nothing marked `contradicts` it told
        # the reader, unconditionally, to go and check the URL. On the Honda run
        # the single `not_addressed` entry carried a re-ask in which the
        # asserting model read its own refutation and concluded the *claim* was
        # wrong — the draft said the clock advanced to "January 1, 2003" where
        # the source says "1998 or 2002" — and attached the corrected wording.
        # The header pointed the reader away from the one actionable finding in
        # the block.
        #
        # So the guidance is conditional on what the entries actually contain,
        # and each sentence is scoped to the entries it is true of. The
        # all-`not_addressed` wording is kept verbatim for the case it was
        # written for: on the dc-environment run that was 47 of 49 refutations,
        # and the advice is right there.
        contradicted = [
            c for c in mismatch if c.get("relevance_verdict") == "contradicts"
        ]
        # Identity, not equality: two citations can compare equal as dicts, and
        # `in` on a list of dicts would fold them together and undercount.
        contradicted_ids = {id(c) for c in contradicted}
        conceded = [
            c
            for c in mismatch
            if id(c) not in contradicted_ids
            and (c.get("reask") or {}).get("action") in _REASK_CONCEDES
        ]
        remaining = len(mismatch) - len(contradicted) - len(conceded)
        if contradicted:
            lines.append(
                f"⚠ {len(contradicted)} of these came back `contradicts` — the "
                "source says something that conflicts with the claim. Treat those "
                "as a possible factual error, not a citation error."
            )
        if conceded:
            n = len(conceded)
            lines.append(
                f"⚠ The model that asserted the claim was handed its own "
                f"refutation for {n} of these and concluded the claim — not the "
                f"citation — was wrong. What it proposes is near the top of "
                f"{'that entry' if n == 1 else 'those entries'}. Do not read "
                f"{'it' if n == 1 else 'them'} as "
                f"{'a citation problem' if n == 1 else 'citation problems'}."
            )
        if remaining and (contradicted or conceded):
            lead = (
                "The one remaining entry is"
                if remaining == 1
                else f"The remaining {remaining} entries are"
            )
            lines.append(
                f"{lead} `not_addressed` or `inconclusive` with no re-ask that "
                "faulted the claim: the page did not cover it. That usually means "
                "the wrong URL was checked, or the relevant part of the page did "
                "not extract — verify the source is the one intended before "
                "treating it as a problem with the claim."
            )
        elif remaining:
            lines.append(
                "None came back `contradicts`. Every entry here is "
                "`not_addressed` or `inconclusive`: the page did not cover the "
                "claim. That usually means the wrong URL was checked, or the "
                "relevant part of the page did not extract — verify the source is "
                "the one intended before treating it as a problem with the claim."
            )
        lines.append("")
        for c in mismatch:
            lines.extend(_render_mismatch_entry(c))
        lines.append("")

    if unverifiable:
        lines.append(
            f"### Fetched, but could not be read ({len(unverifiable)}) — its "
            "content could NOT be read or assessed (this is NOT a finding "
            "against the source)"
        )
        lines.append(
            "_A scanned PDF, a JavaScript-rendered page, a bot wall, or a "
            "relevance check that could not run. Treat exactly like pointer-only. "
            "The one thing it never means is that the source failed to back you._"
        )
        lines.append("")
        for c in unverifiable:
            lines.append(f'- "{c.get("claim", "")}"')
            lines.extend(_render_archive_pair(c))
            for kv in _kv_lines(
                c,
                exclude=("claim", "resolved", "url", "final_url")
                + _PAIR_RENDERED_FIELDS,
            ):
                lines.append(kv)
        lines.append("")

    if fetch_failed:
        # Split out of the old "Unresolved" pile because it is the one bucket in
        # the section a reader can usually clear by hand: the exact document is
        # named, and a publisher that refuses an automated fetch (403) will often
        # serve the same page to a person in a browser. Lumping it in with claims
        # that never had a URL hid that.
        lines.append(
            f"### Source URL identified, but the fetch was refused "
            f"({len(fetch_failed)}) — worth opening by hand"
        )
        lines.append(
            "_A specific URL was named for these claims and the fetch did not "
            "succeed: refused (403), missing (404), or unreachable. A 403 here "
            "is the hard kind — a 403 this run could get past was retried as a "
            "browser and read, so what is left refused that too, which in "
            "practice means a subscription gate or a JS/CAPTCHA challenge rather "
            "than a bot policy. Some are still readable to a logged-in person. "
            "Where an archive copy exists it is listed below, and for these URLs "
            "that copy may be the only readable version. Nothing here is "
            "evidence either way._"
        )
        lines.append("")
        for c in fetch_failed:
            lines.append(f'- "{c.get("claim", "")}"')
            lines.extend(_render_archive_pair(c))
            for kv in _kv_lines(
                c,
                exclude=("claim", "resolved", "url", "final_url")
                + _PAIR_RENDERED_FIELDS,
            ):
                lines.append(kv)
        lines.append("")

    if pointer:
        lines.append(
            f"### Pointer only ({len(pointer)}) — topic-relevant source "
            "identified, NOT independently verified (confirm manually before citing)"
        )
        lines.append(
            "_A keyword match pointed at a portal that is probably about the right "
            "topic. Nothing was retrieved or confirmed. Treat as a research lead._"
        )
        lines.append("")
        for c in pointer:
            lines.append(f'- "{c.get("claim", "")}"')
            lines.extend(_render_archive_pair(c))
            for kv in _kv_lines(
                c,
                exclude=("claim", "resolved", "url", "final_url")
                + _PAIR_RENDERED_FIELDS,
            ):
                lines.append(kv)
        lines.append("")

    if no_source:
        # Previously "Unresolved", which quietly also held every content mismatch
        # and every refused fetch. Naming it for what happened keeps the bucket
        # honest: no URL was ever found, so nothing here is evidence either way.
        lines.append(f"### No source identified ({len(no_source)})")
        lines.append(
            "_No URL was found for these claims, so nothing was fetched and "
            "nothing was checked. This is not evidence against the claims — it is "
            "the absence of evidence about them. Usually the largest group, and "
            "the one most worth reading as "
            '"still to do" rather than as a result._'
        )
        lines.append("")
        for c in no_source:
            lines.append(f'- "{c.get("claim", "")}"')
            for kv in _kv_lines(c, exclude=("claim", "resolved", "url", "final_url")):
                lines.append(kv)
        lines.append("")

    return lines


#: Domains whose loss costs more than their own section, and what it costs.
#: fact_check is the only source of claims: nothing reaches citation resolution
#: without it, so Sections 2 and 9 both go with it.
_DOMAIN_STAKES = {
    "fact_check": (
        "it is the only source of claims, so Section 2 and Section 9 both depend on it"
    ),
}


def _render_ensemble_width(report):
    """How wide the ensemble was, in the report the author reads.

    Reduced cost is meant to mean reduced functionality. The defect was that it
    meant it silently: the only record of how narrow a run had become was the
    API call table, and reading width off that means knowing
    `_THOROUGHNESS_PRESETS` by heart and subtracting the models the preset
    disabled. At `economy` that is five domains on three distinct models, every
    one of them single-model — a report that reads exactly like a six-model run
    that happened to agree less.

    Consensus is the part that actually changes meaning. Section 1 needs
    ``consensus_min_models`` distinct sources, so on a run where every domain
    has one model, a consensus flag can only come from two domains agreeing —
    and where the whole voter pool is smaller than the minimum, Section 1 cannot
    flag anything at all, however much the models agree.

    Returns [] for reports written before the width block existed, so they
    render exactly as they did before.
    """
    ensemble = report.get("ensemble") or {}
    width = ensemble.get("width")
    if not width:
        return []

    models_by_domain = width.get("models_by_domain") or {}
    if not models_by_domain:
        return []

    distinct = width.get("distinct_models") or []
    single = width.get("domains_single_model") or []
    min_models = width.get("consensus_min_models")
    total_domains = len(width.get("domains_expected") or models_by_domain)

    preset = ensemble.get("cost_preset")
    thoroughness = ensemble.get("thoroughness", "standard")
    preset_line = (
        f"`{preset}` (thoroughness `{thoroughness}`)"
        if preset
        else f"thoroughness `{thoroughness}`"
    )

    lines = ["## Ensemble Width", ""]
    lines.append(
        "_What this run actually bought. A cost preset trades models for money; "
        "these are the numbers that trade moves, and they change how everything "
        "below should be read._"
    )
    lines.append("")
    lines.append(f"- **Preset:** {preset_line}")
    lines.append(
        f"- **Distinct models that ran:** {len(distinct)}"
        + (f" — {', '.join(distinct)}" if distinct else "")
    )
    lines.append(
        f"- **Domains run by a single model:** {len(single)} of {total_domains}"
        + (f" — {', '.join(single)}" if single else "")
    )
    lines.append("")

    lines.append("| Domain | Models that ran |")
    lines.append("| --- | --- |")
    for domain, models in models_by_domain.items():
        cell = ", ".join(models) if models else "**none — did not run**"
        lines.append(f"| {domain} | {cell} |")
    lines.append("")

    # A single-model domain has no internal corroboration: whatever that one
    # model missed is missing from the section, and nothing in the section says
    # so. Worth naming the domains where that costs more than their own section.
    for domain in single:
        stake = _DOMAIN_STAKES.get(domain)
        if stake:
            lines.append(
                f"**{domain} ran on one model.** Nothing corroborates it, and "
                f"{stake}. A single provider failing there empties those "
                f"sections without failing the run."
            )
            lines.append("")

    # A domain nothing reviewed is deliberately NOT narrated here. The
    # "Domains not reviewed" header block and the per-section note above it
    # already say so, and say it better: they carry the reason and they sit
    # directly above the empty section, where the reader meets the problem.
    # Repeating it inside a block about width would give one fact two voices
    # that could drift apart.

    lines.extend(_consensus_meaning(width, min_models))

    backfilled = width.get("backfilled") or []
    if backfilled:
        lines.append(
            "**Backfilled for corroboration.** These domains lost a model the "
            "preset names and were given a second one from what was available, "
            "so no finding in them rests on a single pass:"
        )
        lines.append("")
        for entry in backfilled:
            lines.append(f"- {entry}")
        lines.append("")

    return lines


def _consensus_meaning(width, min_models):
    """State what this ensemble's width does to Section 1, in the report."""
    if not min_models:
        return []

    pool = width.get("voter_pool") or 0
    lt = " (including LanguageTool)" if width.get("languagetool_voted") else ""

    if not width.get("consensus_reachable"):
        return [
            f"**SECTION 1 CANNOT FLAG ANYTHING.** Consensus requires "
            f"{min_models} distinct sources agreeing on a passage, and this run "
            f"had {pool}{lt}. An empty Section 1 here means the run could not "
            f"reach consensus, not that the models found nothing to agree on.",
            "",
        ]

    if width.get("consensus_needs_cross_domain"):
        return [
            f"**Consensus is reachable only across domains.** Section 1 needs "
            f"{min_models} distinct sources on the same passage, and every "
            f"domain here ran a single model — so no passage can reach it from "
            f"within one domain. It takes two different domains flagging the "
            f"same passage, out of a pool of {pool}{lt}. A thin Section 1 is "
            f"the expected shape at this width, not a verdict on the draft.",
            "",
        ]

    return [
        f"Section 1 needs {min_models} distinct sources on a passage; this run "
        f"had a pool of {pool}{lt}.",
        "",
    ]


def _render_model_currency(report):
    """What is known about the age of the models this run used.

    Two sources, deliberately labelled apart. ``ci_core.llm.model_registry``
    reads a hand-maintained table, so it can only name a replacement somebody
    already wrote down — it will never mention a model released after the last
    audit. The live check asks the providers, so it can, but only for the
    providers it actually reached.

    Everything here is advisory, and the section says so. Newer is not better
    and often is not cheaper, and a model too new for ``pricing.yaml`` would be
    costed at the unknown-model fallback rather than its real rate — so the
    listing flags what the price table does not know instead of implying a
    recommendation the data cannot support.

    Returns [] when the report carries no currency block, so reports written
    before this section render exactly as they did before.
    """
    currency = report.get("model_currency")
    if not currency:
        return []

    live = currency.get("live") or {}
    warnings = currency.get("warnings") or []
    notices = currency.get("notices") or []
    newer = live.get("newer") or []
    current = live.get("current") or []
    unchecked = live.get("unchecked") or []

    if not (warnings or notices or newer or current or unchecked):
        return []

    lines = ["## Model Currency", ""]
    lines.append(
        "_Advisory. Nothing here has been changed for you, and a newer model is "
        "not automatically a better or a cheaper one._"
    )
    lines.append("")

    # "configured", not "ran": the registry half is checked against the config
    # before the run, so on a run where a pass fell back to another model the
    # two halves of this section legitimately name different models. Saying
    # "ran" in both places would make that look like a contradiction.
    if warnings:
        lines.append("### Superseded models configured")
        lines.append("")
        for w in warnings:
            note = f" — {w['note']}" if w.get("note") else ""
            lines.append(
                f"- **{w['provider']}** is configured for `{w['model']}`, which "
                f"the registry lists as superseded by `{w['replacement']}`{note}"
            )
        lines.append("")

    if notices:
        lines.append("### Soft upgrades noted in the registry")
        lines.append("")
        for n in notices:
            note = f" — {n['note']}" if n.get("note") else ""
            lines.append(
                f"- **{n['provider']}** is configured for `{n['model']}`; "
                f"`{n['newer']}` exists{note}"
            )
        lines.append("")

    if newer:
        lines.append("### Newer models the providers are offering")
        lines.append("")
        lines.append(
            "_Read from the provider's own model list — what exists, not what "
            "you should switch to._"
        )
        lines.append("")
        for finding in newer:
            lines.append(
                f"- **{finding['provider']}** ran `{finding['model']}`. "
                f"{finding['provider']} also lists:"
            )
            for m in finding["newer"]:
                released = f" — released {m['released']}" if m.get("released") else ""
                priced = (
                    ""
                    if m.get("price_known")
                    else " (no entry in `pricing.yaml`; a run on it would be "
                    "costed at the unknown-model fallback rate)"
                )
                lines.append(f"  - `{m['model']}`{released}{priced}")
            if finding.get("undated_models"):
                lines.append(
                    f"  - _{finding['undated_models']} further model(s) carry no "
                    "release date and could not be compared._"
                )
        lines.append("")

    if current:
        names = ", ".join(f"**{c['provider']}** (`{c['model']}`)" for c in current)
        lines.append(f"Checked and nothing newer offered: {names}.")
        lines.append("")

    if unchecked and not (newer or current):
        # No provider was reached at all — the default, since the live check is
        # opt-in. One line rather than a roll-call: with nothing to contrast it
        # against, naming each provider separately adds length, not meaning.
        lines.append(
            "_No provider was asked for its live model list this run, so the "
            "registry below is the only source here — and it can only name "
            "models a human already recorded. `uv run ci-discover` asks the "
            "providers directly; `live_model_check: true` in the pipeline "
            "config does it as part of the run._"
        )
        lines.append("")
    elif unchecked:
        # The distinction this section exists to protect: an empty "newer" list
        # is evidence only for the providers that were actually asked.
        lines.append("Not checked against the provider's live model list:")
        lines.append("")
        for u in unchecked:
            lines.append(f"- **{u['provider']}** (`{u['model']}`) — {u['reason']}")
        lines.append("")
        lines.append(
            "_No conclusion either way for these — run `uv run ci-discover` to "
            "ask the providers directly._"
        )
        lines.append("")

    reg_date = currency.get("registry_date")
    if reg_date:
        age = currency.get("registry_age_days", 0)
        staleness = ""
        if currency.get("registry_warning"):
            staleness = " — overdue for review"
        elif currency.get("registry_stale"):
            staleness = " — worth re-checking"
        lines.append(
            f"_Built-in model registry last updated {reg_date} ({age} days ago)"
            f"{staleness}. It can only name replacements a human recorded, which "
            "is why the live check above exists._"
        )
        lines.append("")

    return lines


def _render_seo_suggestions(pre_analysis):
    """Render the SEO suggestion block, if the pass produced one.

    This section exists to reach the chat revision round-trip (see
    ``handoff_templates/revise_after_review_prompt.md``), so it is written for
    a model as much as for a person — hence the explicit instruction not to
    treat any of it as decided. Keyword choice is the author's call, and a
    revision pass that quietly picks one would be making it for them.

    Returns [] when no suggestion pass ran, so reports predating this section
    (and runs with the pass disabled) render exactly as they did before.
    """
    suggestions = (pre_analysis or {}).get("seo", {}).get("suggestions")
    if not suggestions:
        return []

    lines = ["## SEO Suggestions", ""]
    if suggestions.get("status") != "ok":
        lines.append(
            f"_Not available this run: {suggestions.get('reason', 'unknown reason')}._"
        )
        lines.append("")
        return lines

    lines.append(
        "_Proposed, not decided. Nothing here has been written to any config, "
        "handoff, or WordPress metadata. Do not select a focus keyword on the "
        "author's behalf — that is a strategic choice about what to rank for._"
    )
    lines.append("")

    candidates = suggestions.get("keyword_candidates") or []
    if candidates:
        lines.append("### Focus keyword candidates")
        for c in candidates:
            rationale = f" — {c['rationale']}" if c.get("rationale") else ""
            lines.append(f"- **{c['keyword']}**{rationale}")
            usage = _keyword_usage_line(c.get("usage"))
            if usage:
                lines.append(f"  - {usage}")
        lines.append("")

    fields = suggestions.get("fields") or {}
    if fields:
        lines.append("### SEO METADATA fields")
        lines.append("")
        for name in _SEO_FIELD_ORDER:
            field = fields.get(name)
            if field:
                lines.extend(_render_seo_field(field))

    return lines


def _keyword_usage_line(usage):
    """Where a candidate phrase actually appears in the article.

    A mechanical scan, not a judgement — and the reason keyword candidates
    surface at draft stage at all. A phrase the article never uses is the
    finding a revision pass most needs to see.
    """
    if not usage:
        return ""
    if not usage.get("body_count"):
        return (
            "**The article never uses this phrase.** Either work it in where it "
            "fits naturally, or pick a candidate the piece already speaks to."
        )

    where = []
    if usage.get("in_title"):
        where.append("the title")
    if usage.get("in_opening"):
        where.append("the opening")
    headings = usage.get("in_headings") or []
    if headings:
        where.append(f"{len(headings)} heading(s)")
    placement = ", ".join(where) if where else "the body only"
    return f"Appears {usage['body_count']}x — in {placement}."


def _render_seo_content_review(pre_analysis):
    """Render the structural findings from the search-reader review pass."""
    content_review = (pre_analysis or {}).get("seo", {}).get("content_review")
    if not content_review:
        return []

    lines = ["## SEO Structure Review", ""]
    if content_review.get("status") != "ok":
        lines.append(
            f"_Not available this run: "
            f"{content_review.get('reason', 'unknown reason')}._"
        )
        lines.append("")
        return lines

    findings = content_review.get("findings") or []
    if not findings:
        lines.append(
            "_Nothing flagged — headings, opening, and title all read as "
            "delivering what a search reader arrived for._"
        )
        lines.append("")
        return lines

    lines.append(
        "_How the article reads to someone who just arrived from a search "
        "result. Structure only — the review sections above cover argument, "
        "completeness, and voice._"
    )
    lines.append("")
    for finding in findings:
        target = f': "{finding["target"]}"' if finding.get("target") else ""
        lines.append(f"- **{finding['type']}**{target}")
        lines.append(f"  - {finding['problem']}")
        if finding.get("suggestion"):
            lines.append(f"  - Suggested: {finding['suggestion']}")
    lines.append("")
    return lines


def _render_seo_field(field):
    """One row of the SEO METADATA table of outcomes.

    Fields with no proposed value still render. Two of them (OG title, OG
    description) have defaults the WordPress push applies on its own, and
    naming the default that would take effect is more use to the author than
    omitting the field and leaving them to wonder whether it was considered.
    """
    label = field.get("label", "")
    if not field.get("value"):
        return [f"**{label}:** {field.get('default_note', '_not proposed_')}", ""]

    measured = (
        f" ({field['chars']}/{field['limit']} chars)"
        if field.get("limit") is not None
        else ""
    )
    over = " — **over the limit, trim before use**" if field.get("over_limit") else ""
    lines = [f"**{label}**{measured}{over}", ""]

    if field.get("recognized") is False:
        lines.append(
            "_Not one of the types this publication's template lists — confirm "
            "Rank Math accepts it before using._"
        )
        lines.append("")
    elif field.get("differs_from_default"):
        lines.append(
            f"_Differs from the configured default "
            f"(`{field['configured_default']}`), which is what the push would "
            f"set if this field is left blank._"
        )
        lines.append("")

    lines.append(f"> {field['value']}")
    if field.get("rationale"):
        lines.append("")
        lines.append(f"_{field['rationale']}_")
    lines.append("")
    return lines


def _render_provenance(report):
    """Which provider drafted the article, and whether it marks its text.

    Declared, never measured, and the block says so on every render. A
    statistical watermark is recovered with the provider's secret key; without
    it, marked and unmarked text are statistically indistinguishable by design.
    So this reports what the handoff said, and an author who reads it as a
    detection result has been misled by us rather than by the tool they used.

    Silent for a provider that does not mark, and silent for a report written
    before this block existed -- there is nothing to say, and a line saying
    nothing is a line that trains people to skip the section.
    """
    prov = report.get("provenance")
    if not prov or not prov.get("marked"):
        return []

    lines = ["## Authorship Provenance", ""]
    declared = prov.get("drafted_with")
    lines.append(f"- Drafted with: **{declared}**")

    status = prov.get("status")
    if status == "partial":
        lines.append(
            "- This provider marks text on some surfaces; the API path was "
            "unconfirmed when the registry was last checked. Treat the mark as "
            "present rather than absent."
        )
    else:
        since = prov.get("since")
        lines.append(
            "- This provider embeds a statistical watermark in generated text"
            + (f", since {since}." if since else ".")
        )
    if prov.get("scope"):
        lines.append(f"- Scope: {prov['scope']}")

    lines.append(
        f"- Basis: {prov.get('basis')}. Nothing in this pipeline can detect a "
        "statistical watermark — that needs the provider's key."
    )

    if prov.get("registry_staleness") in ("notice", "warning"):
        age = prov.get("registry_age_days")
        lines.append(
            f"- NOTE: the watermarking registry was last verified "
            f"{prov.get('registry_date')} ({age} days ago). These facts move "
            "quickly; re-check configs/watermarking.yaml before relying on this."
        )
    if prov.get("note"):
        lines.append("")
        lines.append(f"_{prov['note']}_")
    if prov.get("source"):
        lines.append("")
        lines.append(f"Source: {prov['source']}")
    lines.append("")
    return lines


def render_report_markdown(report):
    """Render a review report dict into a readable markdown document.

    ``report`` has the same shape saved to ``run_N_<timestamp>_report.json``
    by ``consolidation.build_report``.
    """
    lines = [
        "# CONSOLIDATED REVIEW REPORT",
        f"Generated: {report.get('generated', '')}",
        f"Pipeline run: {report.get('run_number', '')}",
        f"Article: {report.get('article_title', '')}",
        f"Publication: {report.get('publication', '')}",
        "",
    ]

    corrections = report.get("lt_corrections_applied", [])
    if report.get("lt_skipped"):
        # Name the actual reason. Both skip paths land here, and asserting "no
        # credentials configured" at a reader who had turned the pass off in
        # config sends them to fix something that is not broken — the same
        # wrong-message bug the console summary was already corrected for.
        why = (
            "grammar_pass is set to false in the pipeline config"
            if report.get("lt_skipped_reason") == "disabled"
            else "no LanguageTool credentials configured"
        )
        lines.append(f"LanguageTool: skipped ({why})")
    elif report.get("lt_failed"):
        lines.append("LanguageTool: FAILED — draft not grammar-corrected")
    else:
        lines.append(f"LanguageTool corrections applied: {len(corrections)}")
    lines.append("")

    lines.extend(_render_model_failures(report))
    lines.extend(_render_domains_not_run(report))
    lines.extend(_render_degradations(report))

    if report.get("truncated_results"):
        lines.append(
            "WARNING — truncated model responses (output-token ceiling hit; "
            "some findings were recovered, some were lost): "
            f"{', '.join(report['truncated_results'])}"
        )
        lines.append("")

    if report.get("empty_results"):
        lines.append(
            "WARNING — model passes that returned a well-formed but completely "
            "empty result. These did not fail and were not truncated, so the "
            "sections they feed are simply short a model: "
            f"{', '.join(report['empty_results'])}"
        )
        lines.append("")

    lines.extend(_render_handoff_gaps(report))

    delta = report.get("delta")
    if delta:
        lines.append("## Delta From Prior Run")
        compared = (delta.get("compared_against") or {}).get("report")
        if compared:
            lines.append(f"- Compared against: `{compared}`")
        lines.append(f"- Word change: {delta.get('word_change_pct')}%")
        lines.append(
            f"- Resolved consensus flags: {delta.get('resolved_consensus_count')}/{delta.get('prior_consensus_count')}"
        )
        lines.append(f"- New consensus flags: {delta.get('new_consensus_count')}")
        if delta.get("claim_changed"):
            lines.append("- Primary claim: CHANGED since prior run")
        if delta.get("structure_changed"):
            lines.append("- Heading structure: CHANGED since prior run")
        lines.append("")

    # Run metadata, like the failed-passes block above it — how old the models
    # behind this report are is context for reading it, not a finding about the
    # article, so it sits in the header rather than among the sections.
    lines.extend(_render_ensemble_width(report))

    lines.extend(_render_model_currency(report))
    lines.extend(_render_provenance(report))

    lines.append("---")
    lines.append("")

    # The worklist comes before the findings because it is the only part of this
    # file the author acts on directly; everything below it is the evidence it
    # points back at. It is deliberately not a "## SECTION N" heading, so the
    # paste-into-a-chat-model revision loop does not sweep it up — see
    # worklist.HEADING for why that matters.
    lines.extend(render_worklist(build_worklist(report)))
    lines.append("---")
    lines.append("")

    lines.extend(_render_section_1(report.get("section_1_consensus", [])))
    lines.extend(_render_section_2(report.get("section_2_fact_check", {}), report))
    lines.extend(
        _render_flags_section(
            "SECTION 3: Voice and AI-Speak",
            report.get("section_3_voice", []),
            note=_domain_notes(report, "voice_style"),
            field="section_3_voice",
        )
    )
    lines.extend(
        _render_flags_section(
            "SECTION 4: Argument Integrity",
            report.get("section_4_argument", []),
            note=_domain_notes(report, "argument_integrity"),
            field="section_4_argument",
        )
    )
    lines.extend(
        _render_flags_section(
            "SECTION 5: Completeness and Framing",
            report.get("section_5_completeness", []),
            passage_key="passage_reference",
            note=_domain_notes(report, "completeness"),
            field="section_5_completeness",
        )
    )
    lines.extend(
        _render_section_6(
            report.get("section_6_red_team", {}),
            note=_domain_notes(report, "red_team"),
        )
    )
    lines.extend(_render_section_7(report.get("section_7_low_confidence", [])))
    lines.extend(_render_section_8(report.get("section_8_additional", [])))
    lines.extend(
        _render_section_9(
            report.get("section_9_citations", []),
            _mapping(report.get("section_2_fact_check")).get("out_of_scope") or [],
        )
    )
    lines.extend(_render_seo_suggestions(report.get("pre_analysis", {})))
    lines.extend(_render_seo_content_review(report.get("pre_analysis", {})))

    return "\n".join(lines).rstrip() + "\n"
