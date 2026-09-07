"""
How much of a single run's finding list is a property of the draft, and how
much is a property of the run.

The pipeline is not deterministic. Four full live runs of the same unedited
article produced 259 distinct findings, of which only 18 appeared in three or
more of the four — a single run is roughly 75% non-reproducible. That number is
recorded in ``docs/CONFIGURATION.md`` and was reconfirmed here against the
project's own ``pipeline_history/``: on the four comparable runs of one draft,
20 of 262 distinct findings appeared in 3 or more (7.6%).

Nothing in the report said so. It presented one run's findings as a definitive
list, with counts, weights, and a ranked Section 1 feeding the revision prompt.
A reader — including the author — reasonably reads "4 consensus flags" as a
property of the draft. It is substantially a property of that run.

This module supplies the correction, using runs that have already been paid
for. Every run writes a full report JSON to ``pipeline_history/``, including the
draft it reviewed, so the Nth run of a draft can be measured against the N-1
that preceded it at no additional cost and no additional model calls. A finding
this run raised that also appeared in four of the five previous runs of the same
draft is a different kind of claim from one appearing for the first time, and
the report can now say which it is.

Comparability
-------------
Two runs may be compared only when they reviewed **the same draft** with **the
same ensemble configuration**. Both halves are load-bearing, and the second one
is not obvious:

``pipeline_history/dc-environment`` holds 47 runs over 8 distinct drafts. The
largest same-draft cluster is 23 runs — but those 23 span single-pass probes
(one model, one domain) and full 25-pass ``maximum`` runs. A finding "seen in 12
of 23 runs" where eleven of those runs never ran the domain that produces it is
not a reproducibility measurement, it is an artifact of counting. Grouping by
draft alone would have manufactured exactly that number. So runs are grouped by
draft fingerprint *and* by an ensemble signature; anything else is reported as
skipped, with the reason.

The signature covers the passes attempted, the models that actually served
them, and the two settings that decide what reaches Section 1 at all. Each
component was chosen by measuring what it costs against the real history rather
than by argument — ``ensemble_signature`` carries that table, including the two
candidates rejected for halving the comparable set.

Failed passes and per-section denominators
------------------------------------------
Partial failure is the normal case here, not the exception: of the 12
comparable 25-pass runs of one draft, exactly one completed with no failed
passes. Excluding degraded runs would therefore discard almost all the
available history, and counting them whole would deflate every rate — a run
whose five ``voice_style`` passes all failed had no opportunity to reproduce a
voice finding, and scoring that as "did not reproduce" is simply wrong.

Denominators are therefore per section: a prior run counts toward a section's
denominator only if at least one pass feeding that section succeeded in it.
Section 1 draws on every domain, so it counts a run with any successful pass.

This is an improvement on whole-run counting, not a complete correction, and
the report says so: a run where four of five ``voice_style`` passes succeeded
had a real but reduced chance of surfacing any given voice finding, and it
still counts as a full unit in that section's denominator. Reproduction rates
are consequently a slight under-estimate whenever comparable runs were
degraded, which is why ``degraded_runs`` is carried in the summary and rendered
as a caveat rather than dropped.

Cost
----
Free. Reads local JSON, makes no model calls. Parsing the whole 47-run,
16.7 MB ``dc-environment`` history takes 0.13s, against a pipeline run that
takes minutes and costs dollars, so this runs by default rather than behind a
flag.
"""

import hashlib
import json
import logging

from .consolidation import _passage_key
from .history import _REPORT_NAME_RE, _existing_run_dir, _report_timestamp

log = logging.getLogger(__name__)


#: The project-wide measurement quoted when this article has no comparable
#: history of its own to measure against. Four full live runs of the same
#: unedited article, recorded in docs/CONFIGURATION.md under "Prompt cache
#: layout"; a test asserts these numbers still match the ones documented there.
#:
#: It is a calibration figure from one article, not a constant of nature. It is
#: presented as such wherever it is rendered, precisely because the alternative
#: — saying nothing — is what this module exists to fix.
CALIBRATION = {
    "runs": 4,
    "distinct_findings": 259,
    "reproduced_in_3_or_more": 18,
    "source": "docs/CONFIGURATION.md, 'Prompt cache layout'",
}

#: Sections holding a flat list of findings, and the field naming the passage
#: each one is about. These are the sections a reader acts on. Section 6
#: (red team) is keyed by model rather than by passage and Sections 7-9 are
#: observations, citations and low-confidence notes rather than flags, so
#: neither shape fits a passage-keyed count.
_LIST_SECTIONS = {
    "section_1_consensus": "passage",
    "section_3_voice": "passage",
    "section_4_argument": "passage",
    "section_5_completeness": "passage_reference",
}

#: Fact-check buckets worth tracking. ``outdated`` and ``contradicted`` are the
#: two that generate revision work — they assert the draft is wrong about
#: something. ``confirmed`` is tracked as well because a claim confirmed in one
#: run and silently absent in the next is exactly the instability this module
#: exists to expose. The declining-to-verdict buckets (``unverifiable``,
#: ``primary_source_needed``) are skipped: they say more about what the model
#: could reach than about the draft.
_FACT_CHECK_BUCKETS = ("outdated", "contradicted", "confirmed")

#: Domains feeding each tracked section, for per-section denominators. A key
#: mapping to None draws on every domain.
_SECTION_DOMAINS = {
    "section_1_consensus": None,
    "section_3_voice": ("voice_style",),
    "section_4_argument": ("argument_integrity",),
    "section_5_completeness": ("completeness",),
    "section_2_fact_check": ("fact_check",),
}

#: Field written onto each finding: how many comparable prior runs also raised
#: it. Named on the finding rather than held in a side table so it survives
#: every existing consumer of the report dict unchanged.
REPRODUCED_FIELD = "reproduced_in"

#: Companion field: the per-section denominator that count is out of. Both are
#: needed on the finding itself — the denominators differ per section, so a
#: bare count is unreadable without it.
DENOMINATOR_FIELD = "reproduced_of"

#: Appended to a model id in ``api_call_log`` when that call used search
#: grounding. Stripped for comparability: the same model appears with and
#: without it inside a single run, so it identifies a call rather than a
#: configuration — see ``ensemble_signature`` for the measurement that settled
#: this.
_GROUNDED_SUFFIX = " [grounded]"

#: Most recent comparable runs to carry in the report's run list. The counting
#: itself is unbounded; this only bounds how much provenance the report JSON
#: repeats back, since each entry is a filename and a timestamp.
_MAX_LISTED_RUNS = 25


def draft_fingerprint(text):
    """Stable fingerprint of a draft, whitespace-normalised.

    Whitespace is normalised because a draft that round-tripped through an
    editor differing only in line wrapping is the same draft for review
    purposes, and treating it as a different one would silently empty the
    comparable set.
    """
    normalised = " ".join((text or "").split())
    if not normalised:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def ensemble_signature(report):
    """Comparability key for a run: what it attempted, not what succeeded.

    Failures are handled by the per-section denominators, so a run that
    attempted the same passes and lost some of them stays comparable — which
    matters, because runs with no failed passes are rare enough that requiring
    them would discard most of the available history.

    Returns None for a report predating the ``ensemble`` block, which cannot be
    placed in any comparison group.

    What is in the key, and why it stops there
    ------------------------------------------
    ``assignments`` names providers, not models — ``openai:fact_check`` is the
    same string whether the run used ``gpt-5.4-mini`` or ``gpt-5.5``. So the
    distinct model identities are folded in from ``api_call_log``, along with
    the two settings that mechanically decide what reaches Section 1 at all
    (``consensus_threshold`` and ``consensus_min_models``).

    Only calls belonging to a *review* pass are read. ``api_call_log`` also
    carries citation-verification calls, which run a different model on a
    different job: one run of the dc-environment draft logged three
    ``citation_verification:known_url`` calls on ``mistral-small-latest``, and
    counting those made it incomparable with four runs whose five review models
    were identical to its own. Filtering to the assignment set is what keeps
    "which models reviewed this article" from being polluted by "what else the
    run happened to call".

    Every candidate was measured against this repo's 47-run ``dc-environment``
    history, counting how many runs would still find a comparable predecessor:

      thoroughness + assignments (alone)          25/47
      + threshold and min_models                  25/47   <- free, and adopted
      + review-pass model ids                     21/47   <- adopted
      + models from every logged call             20/47   <- rejected, see above
      + the "[grounded]" suffix on those ids      14/47   <- rejected
      + per-call reasoning effort                 11/47   <- rejected

    The gates cost nothing on this history and guard a real failure mode if
    either is ever retuned. Model identity costs four runs and buys the thing
    ``assignments`` cannot express. The last two were rejected on cost: the
    grounding suffix marks a call rather than a configuration (the same model
    appears with and without it inside one run), and reasoning effort halves
    the comparable set for a difference smaller than the run-to-run noise this
    is trying to measure. Both are defensible to add back — but only against a
    history where the comparable set survives it.
    """
    ensemble = report.get("ensemble") or {}
    assignments = ensemble.get("assignments")
    if not assignments:
        return None
    width = ensemble.get("width") or {}
    review_passes = {str(a) for a in assignments}
    models = {
        str(call.get("model") or "").replace(_GROUNDED_SUFFIX, "")
        for call in (report.get("api_call_log") or [])
        if call.get("model") and str(call.get("pass") or "") in review_passes
    }
    return (
        str(ensemble.get("thoroughness") or ""),
        tuple(sorted(str(a) for a in assignments)),
        ensemble.get("consensus_threshold"),
        width.get("consensus_min_models"),
        tuple(sorted(models)),
    )


def _succeeded_domains(report):
    """Domains with at least one pass that ran and did not fail."""
    ensemble = report.get("ensemble") or {}
    attempted = {str(a) for a in (ensemble.get("assignments") or [])}
    failed = {str(f) for f in (report.get("model_failures") or [])}
    return {a.split(":", 1)[1] for a in (attempted - failed) if ":" in a}


def _covers_section(report, section):
    """True when ``report`` had a real opportunity to produce ``section``."""
    domains = _succeeded_domains(report)
    if not domains:
        return False
    wanted = _SECTION_DOMAINS.get(section)
    if wanted is None:
        return True
    return any(d in domains for d in wanted)


def _is_degraded(report):
    return bool(report.get("model_failures"))


def finding_keys(report):
    """Map of section name -> set of normalised passage keys in that section.

    Uses ``consolidation._passage_key`` rather than its own normalisation so
    that "the same finding" means the same thing here as it does in consensus
    detection. Two modules disagreeing about that would produce a
    reproducibility rate that quietly measured its own key function.
    """
    out = {}
    for section, field in _LIST_SECTIONS.items():
        keys = set()
        for finding in report.get(section) or []:
            if not isinstance(finding, dict):
                continue
            key = _passage_key(finding.get(field, ""))
            if key:
                keys.add(key)
        out[section] = keys

    fact_check = report.get("section_2_fact_check") or {}
    fc_keys = set()
    if isinstance(fact_check, dict):
        for bucket in _FACT_CHECK_BUCKETS:
            for finding in fact_check.get(bucket) or []:
                if not isinstance(finding, dict):
                    continue
                key = _passage_key(finding.get("claim", ""))
                if key:
                    fc_keys.add((bucket, key))
    out["section_2_fact_check"] = fc_keys
    return out


def _load_prior_reports(history_root, history_key, before_ts=None):
    """Every readable saved report for this article preceding ``before_ts``.

    A history directory that cannot be read, or a report that cannot be parsed,
    yields nothing rather than raising: reproducibility context is an
    enhancement to a report, and must never be the reason a paid-for run fails
    to produce one.
    """
    try:
        run_dir = _existing_run_dir(history_root, history_key)
    except OSError as exc:
        log.warning("Cannot read history directory for reproducibility: %s", exc)
        return []
    if run_dir is None:
        return []

    loaded = []
    for path in run_dir.glob("run_*_report.json"):
        if not _REPORT_NAME_RE.match(path.name):
            continue
        try:
            timestamp = _report_timestamp(path)
            if before_ts is not None and timestamp >= before_ts:
                continue
            with open(path, encoding="utf-8") as handle:
                loaded.append((timestamp, path, json.load(handle)))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("Skipping unreadable history report %s: %s", path, exc)
    loaded.sort(key=lambda item: (item[0], item[1].name))
    return loaded


def comparable_runs(report, history_root, history_key, before_ts=None):
    """Split this article's history into runs comparable to ``report``, and not.

    Returns ``(comparable, skipped)``. ``comparable`` is a list of
    ``(timestamp, path, prior_report)`` oldest first; ``skipped`` is a list of
    ``{"report", "reason"}`` dicts, kept so the report can say what was set
    aside rather than presenting a filtered denominator as the whole history.
    """
    fingerprint = draft_fingerprint(report.get("corrected_draft"))
    signature = ensemble_signature(report)
    comparable, skipped = [], []

    if fingerprint is None or signature is None:
        return comparable, skipped

    for timestamp, path, prior in _load_prior_reports(
        history_root, history_key, before_ts
    ):
        if draft_fingerprint(prior.get("corrected_draft")) != fingerprint:
            skipped.append({"report": path.name, "reason": "different draft"})
            continue
        prior_signature = ensemble_signature(prior)
        if prior_signature is None:
            skipped.append({"report": path.name, "reason": "no ensemble metadata"})
            continue
        if prior_signature != signature:
            attempted = len(prior_signature[1])
            skipped.append(
                {
                    "report": path.name,
                    "reason": (
                        f"different ensemble ({prior_signature[0] or 'unknown'}, "
                        f"{attempted} pass(es) vs {len(signature[1])})"
                    ),
                }
            )
            continue
        comparable.append((timestamp, path, prior))

    return comparable, skipped


def _annotate_findings(report, prior_keys, denominators):
    """Write per-finding reproduction counts onto ``report`` in place.

    Returns per-section totals: findings seen, how many recurred in at least
    one comparable run, and how many in at least half of them.

    Says "reproduced", not "corroborated", for the same reason the rendered
    bands do: this report spends "corroboration" on several models agreeing
    *within* one run, and the two are independent.
    """
    stats = {}

    def _record(section, key, target):
        denominator = denominators.get(section, 0)
        seen = sum(1 for keys in prior_keys if key in keys.get(section, ()))
        # No comparable run means no measurement, and a measurement that was
        # never taken must not be written as a zero. ``reproduced_in: 0`` beside
        # ``reproduced_of: 0`` reads to a later consumer as "checked, found
        # nothing" — the same confusion the Wayback renderer avoids for a lookup
        # that never completed. Absent fields say "not checked" unambiguously.
        if denominator:
            target[REPRODUCED_FIELD] = seen
            target[DENOMINATOR_FIELD] = denominator
        else:
            # Clear rather than merely skip, so annotating a report that already
            # carries counts — a saved report re-read and re-scored against a
            # history that no longer has anything comparable — cannot leave the
            # old numbers standing as if they described this comparison.
            target.pop(REPRODUCED_FIELD, None)
            target.pop(DENOMINATOR_FIELD, None)
        bucket = stats.setdefault(
            section, {"findings": 0, "reproduced": 0, "at_least_half": 0}
        )
        bucket["findings"] += 1
        if seen:
            bucket["reproduced"] += 1
        if denominator and seen * 2 >= denominator:
            bucket["at_least_half"] += 1

    for section, field in _LIST_SECTIONS.items():
        for finding in report.get(section) or []:
            if not isinstance(finding, dict):
                continue
            key = _passage_key(finding.get(field, ""))
            if key:
                _record(section, key, finding)

    fact_check = report.get("section_2_fact_check") or {}
    if isinstance(fact_check, dict):
        for bucket_name in _FACT_CHECK_BUCKETS:
            for finding in fact_check.get(bucket_name) or []:
                if not isinstance(finding, dict):
                    continue
                key = _passage_key(finding.get("claim", ""))
                if key:
                    _record("section_2_fact_check", (bucket_name, key), finding)

    for section, bucket in stats.items():
        bucket["denominator"] = denominators.get(section, 0)
    return stats


def annotate(report, history_root, history_key, before_ts=None):
    """Attach cross-run reproducibility context to ``report``, in place.

    Adds a ``reproducibility`` block describing what could be compared, and
    writes a reproduction count onto every finding in the tracked sections.
    Returns the same report for convenience.

    Always adds the block, including when there is no comparable history — a
    report whose findings have never been checked against a second run needs to
    say so at least as loudly as one whose findings have.
    """
    comparable, skipped = comparable_runs(report, history_root, history_key, before_ts)

    denominators = {}
    for section in list(_LIST_SECTIONS) + ["section_2_fact_check"]:
        denominators[section] = sum(
            1 for _, _, prior in comparable if _covers_section(prior, section)
        )

    prior_keys = []
    for _, _, prior in comparable:
        keys = finding_keys(prior)
        # A section the prior run had no opportunity to produce must not count
        # as a run that failed to reproduce the finding; drop it from that
        # section's evidence entirely, matching the denominator above.
        prior_keys.append(
            {
                section: (keys.get(section) or set())
                for section in keys
                if _covers_section(prior, section)
            }
        )

    stats = _annotate_findings(report, prior_keys, denominators)
    degraded = sum(1 for _, _, prior in comparable if _is_degraded(prior))

    listed = [
        {
            "report": path.name,
            "generated": prior.get("generated"),
            "degraded": _is_degraded(prior),
        }
        for _, path, prior in comparable[-_MAX_LISTED_RUNS:]
    ]

    report["reproducibility"] = {
        "draft_fingerprint": draft_fingerprint(report.get("corrected_draft")),
        "comparable_run_count": len(comparable),
        "degraded_runs": degraded,
        "runs": listed,
        "runs_truncated": max(0, len(comparable) - len(listed)),
        "skipped": skipped[-_MAX_LISTED_RUNS:],
        "skipped_count": len(skipped),
        "section_denominators": denominators,
        "sections": stats,
        "calibration": CALIBRATION,
    }
    return report
