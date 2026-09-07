"""
Render a consolidated review report (the dict produced by
``consolidation.build_report``) into a human-readable markdown document.

The saved JSON report holds everything the ensemble found, but nothing
renders it as prose a human can paste into a chat model or read directly —
only aggregate counts are printed to the console. This module fills that
gap, following the SECTION 1-8 structure documented in
``handoff_templates/review_report.md`` (section 9, citations, was added to
the pipeline after that template was written).
"""

#: Order the SEO METADATA fields render in, matching publication.md's block.
#: Duplicated from ``analysis.seo_suggest.FIELD_ORDER`` rather than imported so
#: this module stays a dependency-free renderer over a plain dict — importing
#: the suggestion module would pull the provider adapters in behind it. A test
#: asserts the two stay in step.
_SEO_FIELD_ORDER = ("meta_description", "og_title", "og_description", "schema_type")


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


def _kv_lines(d, exclude=()):
    """Render remaining key/value pairs of a flag dict as indented bullets."""
    lines = []
    for key, value in d.items():
        if key in exclude or value in (None, "", [], {}):
            continue
        if key == "wayback" and isinstance(value, dict):
            lines.append(f"  - Wayback: {_wayback_summary(value)}")
            continue
        label = key.replace("_", " ").capitalize()
        lines.append(f"  - {label}: {value}")
    return lines


def _render_section_1(consensus_flags):
    lines = ["## SECTION 1: Consensus Flags", ""]
    if not consensus_flags:
        lines.append("_No consensus flags._")
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
        for flag in entry.get("flags", []):
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
    lines.extend(_missing_models_note(report or {}, "fact_check"))
    if not fact_check:
        lines.append("_No fact-check results._")
        return lines

    lines.extend(_render_evidence_coverage(fact_check))

    labels = {
        "confirmed": "Confirmed",
        "outdated": "Outdated",
        "contradicted": "Contradicted",
        "unverifiable": "Unverifiable",
        "primary_source_needed": "Primary source resolution required",
    }
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


def _render_flags_section(title, flags, passage_key="passage", note=()):
    lines = [f"## {title}", ""]
    lines.extend(note)
    if not flags:
        lines.append("_No flags._")
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
    lines = ["## SECTION 6: Red Team Findings", ""]
    lines.extend(note)
    if not red_team:
        lines.append("_No red team results._")
        return lines

    rt_keys = (
        ("most_vulnerable_claim", "Most vulnerable claim"),
        ("highest_audience_risk", "Highest audience risk"),
        ("highest_credibility_risk", "Highest credibility risk"),
    )

    if "most_vulnerable_claim" in red_team or "highest_audience_risk" in red_team:
        # Single-source: flat dict with the three well-known keys.
        for key, label in rt_keys:
            item = red_team.get(key)
            if item:
                lines.extend(_render_red_team_entry(label, item))
        lines.append("")
    else:
        # Multi-source: keyed by model name.
        for model_name, data in red_team.items():
            weight = data.get("_weight")
            weight_str = f" (weight {weight})" if weight is not None else ""
            lines.append(f"### {model_name}{weight_str}")
            for key, label in rt_keys:
                item = data.get(key)
                if item:
                    lines.extend(_render_red_team_entry(label, item))
            lines.append("")

    return lines


def _render_section_7(low_confidence):
    lines = [
        "## SECTION 7: Low-Confidence Flags",
        "_For awareness only — dismiss unless something catches your attention._",
        "",
    ]
    if not low_confidence:
        lines.append("_None._")
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
    if not additional:
        lines.append("_None._")
        return lines
    for obs in additional:
        passage = obs.get("passage", "")
        category = obs.get("category", "?")
        source = obs.get("source_model", "?")
        domain = obs.get("source_domain", "?")
        lines.append(f'- [{category}] "{passage}" — flagged by {source}:{domain}')
        for kv in _kv_lines(
            obs, exclude=("passage", "category", "source_model", "source_domain")
        ):
            lines.append(kv)
    lines.append("")
    return lines


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
    return citation.get("url", ""), wayback.get("snapshot_url")


def _render_archive_pair(citation, indent="  "):
    """Lines pairing a citation's live URL with its archive copy.

    Says which state applies rather than silently omitting the archive line,
    because "no snapshot yet" and "never submitted" need different follow-up
    from the author.

    The two negative states are the ones worth being careful about, and they are
    three, not two:

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
    wayback = citation.get("wayback") or {}
    if archive:
        stale = (
            " (STALE — re-archive before relying on it)"
            if wayback.get("snapshot_stale")
            else ""
        )
        out.append(f"{indent}- Archive: {archive}{stale}")
        out.append(f"{indent}- Cite both: {live} (archived: {archive})")
    elif wayback.get("submitted"):
        out.append(
            f"{indent}- Archive: submitted to the Wayback Machine this run; the "
            f"snapshot URL appears on the next run once archive.org has captured it."
        )
    elif not wayback:
        out.append(
            f"{indent}- Archive: NOT LOOKED UP — archive.org was never asked "
            f"about this URL, because the fetch failed in a way an archived copy "
            f"does not stand in for (a 404 or 5xx), or the address was not one we "
            f"would hand to a third party. Re-running will not ask either. This "
            f"says nothing about whether the page is archived — check by hand."
        )
    elif wayback.get("archived") is None:
        out.append(
            f"{indent}- Archive: NOT CHECKED — the archive.org lookup did not "
            f"complete this run. This says nothing about whether the page is "
            f"archived; re-run to find out."
        )
    elif citation.get("resolved"):
        out.append(
            f"{indent}- Archive: none. This citation is only as durable as the "
            f"live URL — re-run once archiving succeeds, or archive it by hand."
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
_DISPOSITIONS = (
    ("checksum", "Read, and supports the claim"),
    ("content_mismatch", "Read, and does NOT support the claim"),
    ("unverifiable", "Fetched, but could not be read"),
    ("fetch_failed", "Source URL identified, but the fetch was refused"),
    ("pointer", "Pointer only — nothing retrieved"),
    ("no_source", "No source identified"),
)

#: The two dispositions where a document was genuinely retrieved *and* its text
#: read by the relevance check. "Checked" means these and only these — the
#: verdict then splits them. Conflating "checked" with "supports" is the same
#: mistake this section exists to stop a reader making, one level up.
_READ_DISPOSITIONS = ("checksum", "content_mismatch")


def _disposition(citation):
    """Which ``_DISPOSITIONS`` bucket a citation belongs in.

    Anything that never reached a verification tier is one of the two "nothing
    was read" buckets, regardless of ``resolved``: ``fetch_failed`` when a URL
    was identified and the fetch did not succeed, ``no_source`` when there was
    no URL to try.
    """
    tier = citation.get("verification")
    if tier in {k for k, _ in _DISPOSITIONS}:
        return tier
    return "fetch_failed" if citation.get("url") else "no_source"


def _render_section_9(citations, out_of_scope=()):
    # The heading carries the framing deliberately. "Citations" alone reads as a
    # list of sources backing the article, which invites more confidence than
    # the tiers below have earned — in a real run 18 of 144 claims had a document
    # fetched and read, and the rest rested on a model asserting a source
    # exists. "SECTION 9" and "Citations" both stay greppable: the paste-based
    # revision loop in handoff_templates/revise_after_review_prompt.md refers to
    # this section by name.
    lines = ["## SECTION 9: Citations — what was actually checked", ""]
    held = [i for i in out_of_scope or () if i.get("excluded")]
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
        lines.append("_No citation resolution attempted._")
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
            lines.append(f"  - URL: {c.get('url', '')}")
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
                c, exclude=("claim", "resolved", "content_changed_since")
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
        contradicted = [
            c for c in mismatch if c.get("relevance_verdict") == "contradicts"
        ]
        if contradicted:
            lines.append(
                f"⚠ {len(contradicted)} of these came back `contradicts` — the "
                "source says something that conflicts with the claim. Treat those "
                "as a possible factual error, not a citation error."
            )
        else:
            lines.append(
                "None came back `contradicts`. Every entry here is "
                "`not_addressed` or `inconclusive`: the page did not cover the "
                "claim. That usually means the wrong URL was checked, or the "
                "relevant part of the page did not extract — verify the source is "
                "the one intended before treating it as a problem with the claim."
            )
        lines.append("")
        for c in mismatch:
            lines.append(f'- "{c.get("claim", "")}"')
            lines.extend(_render_archive_pair(c))
            for kv in _kv_lines(c, exclude=("claim", "resolved")):
                lines.append(kv)
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
            for kv in _kv_lines(c, exclude=("claim", "resolved")):
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
            "succeed: refused (403), missing (404), or unreachable. A 403 is a "
            "statement about automated access, not about the document — these are "
            "often readable in a browser, and academic publishers in particular "
            "refuse every automated tier. Where an archive copy exists it is "
            "listed below, and for a refused URL that copy may be the only "
            "readable version. Nothing here is evidence either way._"
        )
        lines.append("")
        for c in fetch_failed:
            lines.append(f'- "{c.get("claim", "")}"')
            lines.extend(_render_archive_pair(c))
            for kv in _kv_lines(c, exclude=("claim", "resolved")):
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
            for kv in _kv_lines(c, exclude=("claim", "resolved")):
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
            for kv in _kv_lines(c, exclude=("claim", "resolved")):
                lines.append(kv)
        lines.append("")

    return lines


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
        lines.append("LanguageTool: skipped (no credentials configured)")
    elif report.get("lt_failed"):
        lines.append("LanguageTool: FAILED — draft not grammar-corrected")
    else:
        lines.append(f"LanguageTool corrections applied: {len(corrections)}")
    lines.append("")

    lines.extend(_render_model_failures(report))

    if report.get("truncated_results"):
        lines.append(
            "WARNING — truncated model responses (output-token ceiling hit; "
            "some findings were recovered, some were lost): "
            f"{', '.join(report['truncated_results'])}"
        )
        lines.append("")

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
    lines.extend(_render_model_currency(report))

    lines.append("---")
    lines.append("")

    lines.extend(_render_section_1(report.get("section_1_consensus", [])))
    lines.extend(_render_section_2(report.get("section_2_fact_check", {}), report))
    lines.extend(
        _render_flags_section(
            "SECTION 3: Voice and AI-Speak",
            report.get("section_3_voice", []),
            note=_missing_models_note(report, "voice_style"),
        )
    )
    lines.extend(
        _render_flags_section(
            "SECTION 4: Argument Integrity",
            report.get("section_4_argument", []),
            note=_missing_models_note(report, "argument_integrity"),
        )
    )
    lines.extend(
        _render_flags_section(
            "SECTION 5: Completeness and Framing",
            report.get("section_5_completeness", []),
            passage_key="passage_reference",
            note=_missing_models_note(report, "completeness"),
        )
    )
    lines.extend(
        _render_section_6(
            report.get("section_6_red_team", {}),
            note=_missing_models_note(report, "red_team"),
        )
    )
    lines.extend(_render_section_7(report.get("section_7_low_confidence", [])))
    lines.extend(_render_section_8(report.get("section_8_additional", [])))
    lines.extend(
        _render_section_9(
            report.get("section_9_citations", []),
            (report.get("section_2_fact_check") or {}).get("out_of_scope") or [],
        )
    )
    lines.extend(_render_seo_suggestions(report.get("pre_analysis", {})))
    lines.extend(_render_seo_content_review(report.get("pre_analysis", {})))

    return "\n".join(lines).rstrip() + "\n"
