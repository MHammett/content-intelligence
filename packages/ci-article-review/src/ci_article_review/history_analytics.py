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

Reads pipeline_history/ fresh on every call — no database, no persistent
index. That's fine at the dozens-to-low-hundreds-of-files scale this
operates at.
"""

import argparse
import json
import logging
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


# ---------------------------------------------------------------------------
# Provider reliability
# ---------------------------------------------------------------------------


def _provider_calls(entries):
    """provider -> chronological list of (timestamp, failed) from api_call_log."""
    calls = {}
    for e in entries:
        for call in e["report"].get("api_call_log") or []:
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


def cost_trend(entries, recent_window=RECENT_WINDOW):
    points = [
        (e["timestamp"], e["report"]["cost_summary"]["total_usd"])
        for e in entries
        if isinstance(e["report"].get("cost_summary"), dict)
        and e["report"]["cost_summary"].get("total_usd") is not None
    ]
    if not points:
        return {
            "runs": 0,
            "total_usd": 0.0,
            "average_usd": 0.0,
            "recent_average_usd": None,
            "baseline_average_usd": None,
            "trend": "no_data",
        }

    total = sum(v for _, v in points)
    recent = points[-recent_window:]
    baseline = points[:-recent_window]
    recent_avg = sum(v for _, v in recent) / len(recent)
    baseline_avg = (sum(v for _, v in baseline) / len(baseline)) if baseline else None

    return {
        "runs": len(points),
        "total_usd": round(total, 4),
        "average_usd": round(total / len(points), 4),
        "recent_average_usd": round(recent_avg, 4),
        "baseline_average_usd": round(baseline_avg, 4)
        if baseline_avg is not None
        else None,
        "trend": _trend_direction(baseline_avg, recent_avg),
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


def per_article_quality_trend(entries):
    """First run -> latest run direction for each metric, per article."""
    by_slug = {}
    for e in entries:
        by_slug.setdefault(e["slug"], []).append(e)

    results = {}
    for slug, runs in by_slug.items():
        runs = sorted(runs, key=lambda e: e["timestamp"])
        first_metrics = _quality_metrics(runs[0]["report"])
        last_metrics = _quality_metrics(runs[-1]["report"])
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
    """Recent-runs vs baseline-runs average for each metric, across all articles."""
    all_metrics = [_quality_metrics(e["report"]) for e in entries]
    recent_metrics = all_metrics[-recent_window:]
    baseline_metrics = all_metrics[:-recent_window]

    def _avg(metrics, key):
        vals = [m[key] for m in metrics if m[key] is not None]
        return sum(vals) / len(vals) if vals else None

    out = {}
    for key in _QUALITY_LABELS:
        avg_all = _avg(all_metrics, key)
        avg_recent = _avg(recent_metrics, key)
        avg_baseline = _avg(baseline_metrics, key)
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

        for call in report.get("api_call_log") or []:
            name = call.get("pass")
            if not name:
                continue
            slot = _slot(name)
            slot["calls"] += 1
            if call.get("failed"):
                slot["failures"] += 1

        for cost_entry in (report.get("cost_summary") or {}).get("by_pass") or []:
            name = cost_entry.get("pass")
            if name:
                _slot(name)["total_usd"] += float(cost_entry.get("total_usd") or 0.0)

        # Expansion's unit of output is a proposal, counted per model that
        # offered it — a merged candidate credits everyone in proposed_by.
        expansion = report.get("section_10_expansion") or {}
        for bucket in ("sources", "topics", "angles", "data_points"):
            for item in expansion.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                for model in item.get("proposed_by") or []:
                    _slot(f"{model}:expansion")["proposals"] += 1

        for flag in report.get("section_1_consensus") or []:
            models = [m for m in (flag.get("models") or []) if m]
            for name in set(models):
                slot = _slot(name)
                slot["consensus_hits"] += 1
                if len(set(models)) == 1:
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
