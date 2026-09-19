"""
Cross-run analytics over pipeline_history/.

Every pipeline run writes a full report JSON via history.save_run(), but until
this module the only code that ever read that history back was
history.load_prior_report() — and that only fetches the single immediately-
preceding run for one article, for delta-tracking. Nothing aggregated across
runs, across articles, or over time.

This module scans all run_*_report.json files under a history root and
reports on trends that only become visible across many runs:

  - Provider reliability: per-model success rate, recent vs historical
    baseline, flagged when a provider degrades sharply (the kind of thing
    that would have caught a 401-across-every-domain API key outage
    automatically instead of after several human-noticed failed runs).
  - Cost: per-run spend, totals, and trend direction.
  - Readability/SEO/link quality: trending better or worse, both globally
    and per article across its revision history.

Report JSON schema has evolved over the life of this project (fields like
raw_excerpt, fallback_warnings, and model_currency were added at different
times), so every field is read with .get() and missing data is treated as
"not enough history" rather than an error.

A --retry-failed run is saved in the article's own history, beside the run
whose capture it loaded, and its report lists every call it carried over from
that capture as well as the calls it made. Since 2026-09-18 (PR #211) the
carried calls are marked ``replayed`` in ``api_call_log`` and
``cost_summary.by_pass``, and the report names its capture in
``retry_failed_from``. Every figure here counts a call once, in the run that
made it: what it cost, whether it failed, and what it contributed. A carried
call is left to the report of the run that made it — in this history too,
unless the capture came from another checkout, in which case no run here paid
for it. The cost trend also adds what a retry itself spent to the run it
re-ran, so that a gap-fill is not read as a run (see cost_trend). A
--retry-failed run from before that date (the flag dates from 2026-08-27) has
no marker, and nothing else in its report tells it apart from a full run, so
it is counted as one. This module does not guess.

Reads pipeline_history/ fresh on every call — no database, no persistent
index. That's fine at the dozens-to-low-hundreds-of-files scale this
operates at.
"""

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ci_core.console import force_utf8_stdio

# Article titles out of the history files reach stdout verbatim.
force_utf8_stdio()

log = logging.getLogger(__name__)

HISTORY_ROOT = "pipeline_history"

# How many of the most recent data points (calls, for provider reliability;
# runs, for cost/quality) count as "recent" vs everything before them being
# "baseline". Simple fixed-window comparison — no attempt at anything fancier.
RECENT_WINDOW = 5

# A provider needs at least this many baseline calls before we trust the
# baseline enough to compare against — otherwise a single early failure
# looks like a "100% -> 0%" collapse.
MIN_BASELINE_CALLS = 3

# Flag a provider as degraded when its recent failure rate is at least this
# many percentage points worse than its baseline failure rate.
DEGRADED_THRESHOLD = 0.4

# A cost/quality metric trend is only called "increasing"/"decreasing" when
# the relative change between recent and baseline averages exceeds this.
TREND_RELATIVE_THRESHOLD = 0.15


def _parse_timestamp(report, path):
    generated = report.get("generated")
    if generated:
        try:
            return datetime.fromisoformat(generated)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def iter_reports(history_root, article_slug=None):
    """Yield (article_slug, path, report_dict) for every readable report JSON."""
    root = Path(history_root)
    if not root.is_dir():
        return
    if article_slug:
        dirs = [root / article_slug]
    else:
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("run_*_report.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    report = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                log.warning("Skipping unreadable report %s: %s", path, e)
                continue
            yield d.name, path, report


def load_reports(history_root, article_slug=None):
    """Load all reports as {slug, path, report, timestamp} dicts, oldest first."""
    entries = [
        {
            "slug": slug,
            "path": path,
            "report": report,
            "timestamp": _parse_timestamp(report, path),
        }
        for slug, path, report in iter_reports(history_root, article_slug)
    ]
    entries.sort(key=lambda e: e["timestamp"])
    return entries


def _carried_over(report):
    """The passes a run carried over from a capture rather than ran itself.

    Read from ``api_call_log``, where the pipeline marks them; ``by_pass``
    carries the same mark only since PR #211, while the call log has carried
    it for a --replay since 2026-09-04. A pass name appears once per call log,
    so it identifies the entry.
    """
    return {
        call["pass"]
        for call in report.get("api_call_log") or []
        if call.get("replayed") and call.get("pass")
    }


# ---------------------------------------------------------------------------
# Provider reliability
# ---------------------------------------------------------------------------


def _provider_calls(entries):
    """provider -> chronological list of (timestamp, failed) from api_call_log."""
    calls = {}
    for e in entries:
        for call in e["report"].get("api_call_log") or []:
            # A call a --retry-failed run carried over from its capture was
            # made, and succeeded or failed, in the run that wrote the capture,
            # and is counted there. Counted again here it would be one call
            # observed twice, the second time at the retry's timestamp, where
            # it can move the recent window on its own.
            if call.get("replayed"):
                continue
            pass_key = call.get("pass") or ""
            # Expect "model:domain" (e.g. "openai:fact_check"). Some very old
            # reports recorded just the domain with no colon — that can't be
            # attributed to a provider, so skip it rather than misreading the
            # domain as a provider name.
            if ":" not in pass_key:
                continue
            provider = pass_key.split(":", 1)[0]
            if not provider:
                continue
            calls.setdefault(provider, []).append(
                (e["timestamp"], bool(call.get("failed")))
            )
    for provider_calls in calls.values():
        provider_calls.sort(key=lambda c: c[0])
    return calls


def provider_reliability(
    entries,
    recent_window=RECENT_WINDOW,
    min_baseline=MIN_BASELINE_CALLS,
    degraded_threshold=DEGRADED_THRESHOLD,
):
    """Per-provider recent vs baseline success rate, with a degradation flag."""
    results = {}
    for provider, calls in sorted(_provider_calls(entries).items()):
        recent = calls[-recent_window:]
        baseline = calls[:-recent_window]

        recent_rate = sum(1 for _, failed in recent if failed) / len(recent)
        recent_success_rate = 1 - recent_rate

        if len(baseline) >= min_baseline:
            baseline_rate = sum(1 for _, failed in baseline if failed) / len(baseline)
            baseline_success_rate = 1 - baseline_rate
        else:
            baseline_rate = None
            baseline_success_rate = None

        degraded = (
            baseline_rate is not None
            and (recent_rate - baseline_rate) >= degraded_threshold
        )

        results[provider] = {
            "total_calls": len(calls),
            "recent_calls": len(recent),
            "recent_success_rate": recent_success_rate,
            "baseline_calls": len(baseline),
            "baseline_success_rate": baseline_success_rate,
            "degraded": degraded,
        }
    return results


# ---------------------------------------------------------------------------
# Cost trend
# ---------------------------------------------------------------------------


def _trend_direction(baseline_avg, recent_avg, threshold=TREND_RELATIVE_THRESHOLD):
    """Raw numeric direction (increasing/decreasing/flat) — no good/bad judgment.

    Cost has no "better" direction to editorialize about; per-article and
    global quality trends use _direction() separately for improved/worsened.
    """
    if baseline_avg is None or recent_avg is None:
        return "insufficient_history"
    if baseline_avg == 0:
        return "flat" if recent_avg == 0 else "increasing"
    change = (recent_avg - baseline_avg) / abs(baseline_avg)
    if abs(change) < threshold:
        return "flat"
    return "increasing" if change > 0 else "decreasing"


def _spent_usd(cost_summary):
    """What one run spent, or None when its report has no cost figure.

    ``incurred_usd`` leaves out the calls a run carried over from a capture;
    for a run that carried none it is ``total_usd``. Reports from before it
    existed (2026-09-04) have only ``total_usd``.
    """
    if not isinstance(cost_summary, dict):
        return None
    spent = cost_summary.get("incurred_usd")
    # Not ``or``: a --retry-failed run whose capture had nothing to retry
    # re-ran nothing, and 0.0 is its cost, not a gap to fill with the total.
    return spent if spent is not None else cost_summary.get("total_usd")


def _retried_report_name(report):
    """The report file name of the run a --retry-failed run re-ran, or None.

    ``retry_failed_from`` is the capture's path as it was typed, relative or
    not, so only its name is used. A capture is saved beside its run's report,
    under the same stem (``ensemble_capture.capture_path_for``).
    """
    source = report.get("retry_failed_from")
    name = re.split(r"[\\/]", str(source))[-1] if source else ""
    if not name.endswith("_results.json"):
        return None
    return name[: -len("_results.json")] + "_report.json"


def cost_trend(entries, recent_window=RECENT_WINDOW):
    """Spend per run: total, average, and recent vs. baseline direction.

    Each run counts at what it spent itself (``_spent_usd``). A --retry-failed
    run spends only what it takes to fill an earlier run's gaps, so its spend
    is added to that run rather than counted as a run of its own. Counted as
    one, it is a run costing cents: it drags the per-run average down and, in
    the recent window, can turn the trend by itself (2026-09-18: one $0.03
    retry added to a 59-run history turned "flat" into "decreasing"). A retry
    whose earlier run is not in this history, because its capture came from
    another checkout, counts as a run of its own.
    """
    # Spend per run, oldest first. Entries are oldest first too, so the run a
    # retry re-ran is already here when the retry arrives.
    points = {}
    # Retry -> the run it was added to, so a retry of a retry's capture lands
    # on the run that first failed as well.
    folded = {}
    retry_failed_runs = 0
    retry_failed_usd = 0.0
    carried_over_usd = 0.0
    unmatched = 0
    for e in entries:
        summary = e["report"].get("cost_summary")
        spent = _spent_usd(summary)
        if spent is None:
            continue
        key = (e["slug"], Path(e["path"]).name) if e.get("path") else id(e)
        if e["report"].get("retry_failed_from"):
            retry_failed_runs += 1
            retry_failed_usd += spent
            carried_over_usd += float(summary.get("replayed_usd") or 0.0)
            retried = _retried_report_name(e["report"])
            target = folded.get((e["slug"], retried), (e["slug"], retried))
            if retried and target in points:
                points[target] += spent
                folded[key] = target
                continue
            unmatched += 1
        points[key] = spent

    retry_failed = {
        "retry_failed_runs": retry_failed_runs,
        "retry_failed_usd": round(retry_failed_usd, 4),
        "carried_over_usd": round(carried_over_usd, 4),
        "retry_failed_unmatched": unmatched,
    }
    if not points:
        return {
            "runs": 0,
            "total_usd": 0.0,
            "average_usd": 0.0,
            "recent_average_usd": None,
            "baseline_average_usd": None,
            "trend": "no_data",
            **retry_failed,
        }

    values = list(points.values())
    total = sum(values)
    recent = values[-recent_window:]
    baseline = values[:-recent_window]
    recent_avg = sum(recent) / len(recent)
    baseline_avg = (sum(baseline) / len(baseline)) if baseline else None

    return {
        "runs": len(values),
        "total_usd": round(total, 4),
        "average_usd": round(total / len(values), 4),
        "recent_average_usd": round(recent_avg, 4),
        "baseline_average_usd": round(baseline_avg, 4)
        if baseline_avg is not None
        else None,
        "trend": _trend_direction(baseline_avg, recent_avg),
        **retry_failed,
    }


# ---------------------------------------------------------------------------
# Readability / SEO / link quality trend
# ---------------------------------------------------------------------------


def _quality_metrics(report):
    pre = report.get("pre_analysis") or {}

    readability = pre.get("readability") or {}
    fk_grade = readability.get("flesch_kincaid_grade")

    seo = pre.get("seo") or {}
    seo_issues = seo.get("issues")
    seo_issue_count = len(seo_issues) if isinstance(seo_issues, list) else None

    # A run that did not check its links (--offline, or link_validation: false)
    # writes None, with links_skipped_reason saying which, and has no count:
    # zero would say every link worked. Before this change (2026-09-18) such a
    # run wrote [], the same as a checked draft with no links, and nothing else
    # in those reports records whether the check ran, so in them an empty list
    # is ambiguous. It is read as none broken, as it always was; this does not
    # guess which of the two it was.
    links = pre.get("links")
    broken_link_count = (
        sum(1 for lk in links if not lk.get("ok")) if isinstance(links, list) else None
    )

    return {
        "fk_grade": fk_grade,
        "seo_issue_count": seo_issue_count,
        "broken_link_count": broken_link_count,
    }


def _direction(first, last):
    """improved/worsened/unchanged for a "lower is better" metric."""
    if first is None or last is None:
        return "unknown"
    if first == last:
        return "unchanged"
    return "improved" if last < first else "worsened"


_QUALITY_LABELS = {
    "fk_grade": "Readability (FK grade)",
    "seo_issue_count": "SEO issues",
    "broken_link_count": "Broken links",
}


def _measured(metrics, key):
    """One metric's values, in run order, from the runs that measured it."""
    return [m[key] for m in metrics if m[key] is not None]


def per_article_quality_trend(entries):
    """First -> latest measurement of each metric, per article.

    Each metric compares the first and the latest run that measured it. A run
    with no value for a metric (links never checked, or a report older than the
    field) is not a data point for it, so it can neither turn the trend nor
    hide it. When the first and latest runs were compared instead, one
    --offline run after an article's four checked runs turned its links from
    "worsened" to "improved" while it counted as zero, and to "unknown" once it
    counted as nothing (2026-09-18). A metric only one run measured is compared
    with itself, "unchanged", as every metric of a single-run article always
    has been.
    """
    by_slug = {}
    for e in entries:
        by_slug.setdefault(e["slug"], []).append(e)

    results = {}
    for slug, runs in by_slug.items():
        runs = sorted(runs, key=lambda e: e["timestamp"])
        metrics = [_quality_metrics(e["report"]) for e in runs]
        first_metrics = {}
        last_metrics = {}
        for key in _QUALITY_LABELS:
            values = _measured(metrics, key)
            first_metrics[key] = values[0] if values else None
            last_metrics[key] = values[-1] if values else None
        results[slug] = {
            "runs": len(runs),
            "article_title": runs[-1]["report"].get("article_title", slug),
            "first": first_metrics,
            "last": last_metrics,
            "fk_grade_trend": _direction(
                first_metrics["fk_grade"], last_metrics["fk_grade"]
            ),
            "seo_issues_trend": _direction(
                first_metrics["seo_issue_count"], last_metrics["seo_issue_count"]
            ),
            "broken_links_trend": _direction(
                first_metrics["broken_link_count"], last_metrics["broken_link_count"]
            ),
        }
    return results


def global_quality_trend(entries, recent_window=RECENT_WINDOW):
    """Recent vs baseline average for each metric, across all articles.

    Windowed per metric over the runs that measured it: the latest
    ``recent_window`` measurements against every one before them. Windowed
    over runs, a run with no value still took a slot in the recent window, and
    pushed a measured run into the baseline, moving both averages while adding
    nothing to either.
    """
    all_metrics = [_quality_metrics(e["report"]) for e in entries]

    def _avg(vals):
        return sum(vals) / len(vals) if vals else None

    out = {}
    for key in _QUALITY_LABELS:
        values = _measured(all_metrics, key)
        avg_all = _avg(values)
        avg_recent = _avg(values[-recent_window:])
        avg_baseline = _avg(values[:-recent_window])
        if avg_recent is None or avg_baseline is None:
            trend = "insufficient_history"
        else:
            trend = _direction(avg_baseline, avg_recent)
        out[key] = {
            "average": round(avg_all, 2) if avg_all is not None else None,
            "recent_average": round(avg_recent, 2) if avg_recent is not None else None,
            "baseline_average": round(avg_baseline, 2)
            if avg_baseline is not None
            else None,
            "trend": trend,
        }
    return out


# ---------------------------------------------------------------------------
# Top-level aggregation + console output
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-pass contribution — is each ensemble call earning its keep?
# ---------------------------------------------------------------------------


def pass_contribution(entries):
    """Per (model, domain) pass: what it cost, and what it actually contributed.

    Audit findings 10 and 16 both ask a question the pipeline had no way to
    answer: at `maximum` thoroughness it makes ~25-30 calls, and nothing said
    which of them earn their cost. The audit deliberately declined to retune the
    presets without data. This produces the data, entirely from reports already
    on disk — no new calls, no new instrumentation.

    For each pass, across all runs:

      calls            how many times it ran
      failures         how many of those failed
      total_usd        what it cost in total
      consensus_hits   findings it raised that reached Section 1
      sole_source      consensus flags ONLY it raised (nobody corroborated)
      corroborated     consensus flags it shared with at least one other pass
      usd_per_consensus_hit   cost efficiency, the number to sort by
      proposals        Section 10 candidates it offered (expansion passes only)
      usd_per_proposal expansion's equivalent of usd_per_consensus_hit

    **The expansion domain cannot be scored on consensus and is not.** It never
    reaches Section 1 — by design, since it proposes material rather than
    flagging passages — so every expansion pass would show zero hits, no
    cost-per-hit, and land in the "never contributed, consider trimming" note
    below. That note would be recommending the deletion of a pass that was
    working exactly as intended. Expansion passes are scored on proposals
    offered and reported separately.

    The interesting signal is a pass with real cost and few consensus hits, or
    one whose hits are always corroborated by a cheaper pass — that is a
    candidate for removal from the preset. A pass with many *sole_source* hits
    is the opposite: it is seeing things nothing else sees, and dropping it
    would lose findings.

    A caveat worth keeping in view: consensus participation is a proxy for
    value, not value itself. A red-team pass that raises one genuinely alarming
    finding nobody else spots scores badly here and is worth every cent. Read
    this to form hypotheses, then confirm with --only-model / --only-domain.

    Each call counts once, in the run that made it: a pass a --retry-failed run
    carried over from its capture adds nothing from the retry's report — not a
    call, not its cost, not its findings. See the module docstring.
    """
    stats: dict[str, dict] = {}

    def _slot(name):
        return stats.setdefault(
            name,
            {
                "pass": name,
                "calls": 0,
                "failures": 0,
                "total_usd": 0.0,
                "consensus_hits": 0,
                "sole_source": 0,
                "corroborated": 0,
                "proposals": 0,
            },
        )

    for entry in entries:
        report = entry["report"]
        carried = _carried_over(report)

        for call in report.get("api_call_log") or []:
            name = call.get("pass")
            if not name or call.get("replayed"):
                continue
            slot = _slot(name)
            slot["calls"] += 1
            if call.get("failed"):
                slot["failures"] += 1

        for cost_entry in (report.get("cost_summary") or {}).get("by_pass") or []:
            name = cost_entry.get("pass")
            if name and name not in carried:
                _slot(name)["total_usd"] += float(cost_entry.get("total_usd") or 0.0)

        # Expansion's unit of output is a proposal, counted per model that
        # offered it — a merged candidate credits everyone in proposed_by.
        # A carried expansion pass is skipped for the reason given for
        # Section 1 below.
        expansion = report.get("section_10_expansion") or {}
        for bucket in ("sources", "topics", "angles", "data_points"):
            for item in expansion.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                for model in item.get("proposed_by") or []:
                    if f"{model}:expansion" not in carried:
                        _slot(f"{model}:expansion")["proposals"] += 1

        # A --retry-failed run's Section 1 is a real consensus over everything
        # it carried and everything it re-ran, but a carried pass's findings are
        # the ones it raised in the run that made it, whose own Section 1
        # already credited them. Crediting them again would set one call's cost
        # against its findings counted twice, so only the passes this run made
        # are credited. That leaves out a carried pass's share of a flag that
        # cleared the bar only with the re-run pass's vote; the re-run pass is
        # credited with it. Whether a hit was corroborated still counts every
        # pass that raised it, carried or not: a carried result did raise it.
        for flag in report.get("section_1_consensus") or []:
            models = {m for m in (flag.get("models") or []) if m}
            for name in models - carried:
                slot = _slot(name)
                slot["consensus_hits"] += 1
                if len(models) == 1:
                    slot["sole_source"] += 1
                else:
                    slot["corroborated"] += 1

    for slot in stats.values():
        hits = slot["consensus_hits"]
        slot["usd_per_consensus_hit"] = (
            round(slot["total_usd"] / hits, 4) if hits else None
        )
        proposals = slot["proposals"]
        slot["usd_per_proposal"] = (
            round(slot["total_usd"] / proposals, 4) if proposals else None
        )
        slot["total_usd"] = round(slot["total_usd"], 4)

    # Most expensive per unit of signal first; passes that contributed nothing
    # at all sort last but are kept, because "cost with zero hits" is the
    # loudest result this can produce.
    return sorted(
        stats.values(),
        key=lambda s: (s["usd_per_consensus_hit"] is None, -(s["total_usd"])),
    )


def build_history_report(
    history_root=HISTORY_ROOT, article_slug=None, recent_window=RECENT_WINDOW
):
    entries = load_reports(history_root, article_slug)
    return {
        "history_root": str(history_root),
        "article_slug": article_slug,
        "total_reports": len(entries),
        "provider_reliability": provider_reliability(
            entries, recent_window=recent_window
        ),
        "cost_trend": cost_trend(entries, recent_window=recent_window),
        "global_quality_trend": global_quality_trend(
            entries, recent_window=recent_window
        ),
        "per_article_quality_trend": per_article_quality_trend(entries),
        "pass_contribution": pass_contribution(entries),
    }


def print_history_report(result):
    print("\n" + "=" * 60)
    print("PIPELINE HISTORY ANALYTICS")
    scope = result["article_slug"] or "all articles"
    print(
        f"Scope: {scope}  ({result['total_reports']} run report(s) under {result['history_root']})"
    )
    print("=" * 60)

    if result["total_reports"] == 0:
        print("\nNo report files found.")
        return

    reliability = result["provider_reliability"]
    print("\nProvider reliability:")
    if not reliability:
        print("  (no api_call_log data found in any report)")
    degraded_providers = []
    for provider, r in sorted(reliability.items()):
        recent_pct = f"{r['recent_success_rate'] * 100:.0f}%"
        baseline_pct = (
            f"{r['baseline_success_rate'] * 100:.0f}%"
            if r["baseline_success_rate"] is not None
            else "n/a"
        )
        flag = "  <-- DEGRADED" if r["degraded"] else ""
        if r["degraded"]:
            degraded_providers.append(provider)
        print(
            f"  {provider:12s} recent: {recent_pct:>5s} ({r['recent_calls']} calls)   "
            f"baseline: {baseline_pct:>5s} ({r['baseline_calls']} calls){flag}"
        )

    if degraded_providers:
        print(f"\n{'!' * 60}")
        print(f"WARNING: degraded provider(s): {', '.join(sorted(degraded_providers))}")
        print("Recent success rate has dropped sharply vs. historical baseline.")
        print("Check API keys, quotas, and provider status before trusting new runs.")
        print("!" * 60)

    cost = result["cost_trend"]
    print("\nCost trend:")
    if cost["trend"] == "no_data":
        print("  (no cost_summary data found in any report)")
    else:
        print(
            f"  Total spend: ${cost['total_usd']:.4f} across {cost['runs']} run(s), "
            f"avg ${cost['average_usd']:.4f}/run"
        )
        if cost["retry_failed_runs"]:
            print(
                f"  ({cost['retry_failed_runs']} --retry-failed run(s) counted "
                "with the run whose failed calls each re-ran: "
                f"${cost['retry_failed_usd']:.4f} spent; the "
                f"${cost['carried_over_usd']:.4f} of calls they carried over is "
                "not counted again)"
            )
            if cost["retry_failed_unmatched"]:
                print(
                    f"  ({cost['retry_failed_unmatched']} of those counted as "
                    "run(s) of their own: the run each re-ran is not in this "
                    "history)"
                )
        if cost["baseline_average_usd"] is not None:
            print(
                f"  Recent avg ${cost['recent_average_usd']:.4f}/run vs. "
                f"baseline avg ${cost['baseline_average_usd']:.4f}/run -> {cost['trend']}"
            )
        else:
            print("  Trend: insufficient history for a baseline comparison")

    gq = result["global_quality_trend"]
    print("\nQuality trend (across all runs, chronological):")
    for key, label in _QUALITY_LABELS.items():
        m = gq[key]
        if m["trend"] == "insufficient_history":
            print(f"  {label}: insufficient history")
        else:
            print(
                f"  {label}: recent avg {m['recent_average']} vs. baseline avg {m['baseline_average']} -> {m['trend']}"
            )

    per_article = result["per_article_quality_trend"]
    multi_run = {slug: v for slug, v in per_article.items() if v["runs"] > 1}
    if multi_run:
        print(
            f"\nPer-article revision trend ({len(multi_run)} article(s) with multiple runs):"
        )
        for slug, v in sorted(multi_run.items()):
            print(
                f"  {v['article_title']!r} ({v['runs']} runs): "
                f"FK grade {v['fk_grade_trend']}, SEO {v['seo_issues_trend']}, links {v['broken_links_trend']}"
            )

    contribution = result.get("pass_contribution") or []
    # Split before rendering: the consensus table's whole question is "did this
    # call reach Section 1", which expansion is structurally unable to do.
    # Listing it there with a zero is not a neutral omission — the table is
    # read as a trim list.
    expansion_rows = [s for s in contribution if s["pass"].endswith(":expansion")]
    contribution = [s for s in contribution if not s["pass"].endswith(":expansion")]
    if contribution:
        print("\nPer-pass contribution (is each ensemble call earning its cost?):")
        print(
            f"  {'pass':30s} {'calls':>6s} {'fail':>5s} {'$total':>9s} "
            f"{'hits':>5s} {'sole':>5s} {'$/hit':>9s}"
        )
        for s in contribution:
            per_hit = (
                f"${s['usd_per_consensus_hit']:.4f}"
                if s["usd_per_consensus_hit"] is not None
                else "—"
            )
            print(
                f"  {s['pass']:30s} {s['calls']:6d} {s['failures']:5d} "
                f"${s['total_usd']:8.4f} {s['consensus_hits']:5d} "
                f"{s['sole_source']:5d} {per_hit:>9s}"
            )
        never = [s for s in contribution if s["consensus_hits"] == 0 and s["calls"]]
        if never:
            print(
                f"  Note: {len(never)} pass(es) have never contributed to a "
                "consensus flag. That is a candidate for trimming the preset — "
                "but check their sole-source findings first, since a pass that "
                "sees what nothing else sees scores badly here and is still "
                "worth paying for."
            )

    if expansion_rows:
        print("\nExpansion passes (scored on proposals, not consensus):")
        print(
            f"  {'pass':30s} {'calls':>6s} {'fail':>5s} {'$total':>9s} "
            f"{'props':>6s} {'$/prop':>9s}"
        )
        for s in expansion_rows:
            per_proposal = (
                f"${s['usd_per_proposal']:.4f}"
                if s["usd_per_proposal"] is not None
                else "—"
            )
            print(
                f"  {s['pass']:30s} {s['calls']:6d} {s['failures']:5d} "
                f"${s['total_usd']:8.4f} {s['proposals']:6d} {per_proposal:>9s}"
            )
        print(
            "  These never appear in the table above because Section 10 never "
            "reaches Section 1. Judge them on whether the proposals were "
            "adopted and whether their URLs resolved — not on consensus."
        )

    print()


def build_parser():
    """Construct the CLI parser.

    Split out of main() so tests can introspect the flags without running the
    report — see tests/test_docs_current.py.
    """
    parser = argparse.ArgumentParser(
        description="Article Review Pipeline — cross-run history analytics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ci-history-report\n"
            "  ci-history-report --article my-article-slug\n"
            "  ci-history-report --history-root pipeline_history --recent-window 10\n"
        ),
    )
    parser.add_argument(
        "--history-root",
        default=HISTORY_ROOT,
        help=f"Directory containing per-article run history (default: {HISTORY_ROOT})",
    )
    parser.add_argument(
        "--article",
        metavar="SLUG",
        help="Scope analytics to one article's history directory (the slug used as its "
        "pipeline_history subdirectory name, not the article title)",
    )
    parser.add_argument(
        "--recent-window",
        type=int,
        default=RECENT_WINDOW,
        help=f"Number of most recent calls/runs treated as 'recent' vs. baseline (default: {RECENT_WINDOW})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw analytics result as JSON instead of the console summary",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable DEBUG logging"
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    result = build_history_report(
        args.history_root, article_slug=args.article, recent_window=args.recent_window
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_history_report(result)


if __name__ == "__main__":
    main()
