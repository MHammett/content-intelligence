"""
Recurring AI-speak pattern detection across pipeline_history/.

Every review run's section_3_voice (and the voice-tagged entries in
section_8_additional) flags AI-speak/voice problems for that one article, but
nothing looks across runs to notice when the *same* pattern keeps getting
flagged article after article. When it does, that is a strong signal the
pattern belongs in the publication's style_rules.banned_words /
banned_phrases config instead of being caught fresh by the review pipeline
every single time.

This module scans a publication's history (reusing history_analytics'
report-loading so file layout knowledge lives in one place), extracts every
voice-flagged passage/problem pair, and clusters near-duplicates with a
simple normalized-text similarity check — no NLP/ML clustering, since the
goal is surfacing obvious repeats, not perfect semantic dedup.

Output is a REPORT for human review only. This module never writes to
pipeline_history/ or to any publication/style config — it only reads them
(the config read is optional and used solely to filter out patterns already
banned, so the report doesn't re-suggest what's already handled).
"""

import argparse
import difflib
import json
import logging
from pathlib import Path

import yaml

from ci_core.console import force_utf8_stdio

from .history_analytics import (
    HISTORY_ROOT,
    HISTORY_ROOT_ENV,
    load_reports,
    resolve_history_root,
)

# Every flagged passage this prints is article prose, quoted as it was written.
force_utf8_stdio()

log = logging.getLogger(__name__)

# A pattern must show up in at least this many DISTINCT articles before it's
# reported as a candidate — recurrence within a single article (a model
# flagging the same phrase twice in one draft) isn't cross-article evidence.
MIN_ARTICLES = 3

# Two normalized texts are treated as "the same pattern" when their
# difflib.SequenceMatcher ratio meets this threshold. High enough to avoid
# lumping unrelated findings together, tolerant enough to catch the same
# critique/phrase reworded slightly by different models or across articles.
SIMILARITY_THRESHOLD = 0.82

# Passage/problem text is truncated to this many characters before
# normalization and comparison — mirrors consolidation._passage_key's 250
# char window, long enough to retain a distinguishing snippet without letting
# one long outlier dominate comparisons.
TEXT_WINDOW = 250


def _normalize(text):
    return " ".join((text or "").lower().split())[:TEXT_WINDOW]


def _similar(a, b, threshold=SIMILARITY_THRESHOLD):
    # real_quick_ratio()/quick_ratio() are cheap upper bounds on ratio() (same
    # trick difflib.get_close_matches uses) — at hundreds of findings, most
    # pairs are obviously dissimilar, and skipping the full O(n*m) ratio()
    # computation for those is the difference between this running in
    # milliseconds and minutes.
    sm = difflib.SequenceMatcher(None, a, b)
    if sm.real_quick_ratio() < threshold:
        return False
    if sm.quick_ratio() < threshold:
        return False
    return sm.ratio() >= threshold


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_voice_findings(entries, publication=None):
    """Pull every voice-flagged finding out of a set of loaded reports.

    Covers section_3_voice (flags with `passage` + `problem`) and the
    section_8_additional entries tagged `category: "voice"` (flags with
    `passage` + `observation` instead — different key, same intent).

    Returns a flat list of dicts: slug, article_title, passage, problem,
    suggested_rewrite, source_model, section.
    """
    findings = []
    for e in entries:
        report = e["report"]
        if publication is not None and report.get("publication") != publication:
            continue
        slug = e["slug"]
        article_title = report.get("article_title", slug)

        for flag in report.get("section_3_voice") or []:
            passage = flag.get("passage", "")
            problem = flag.get("problem", "")
            if not passage and not problem:
                continue
            findings.append(
                {
                    "slug": slug,
                    "article_title": article_title,
                    "passage": passage,
                    "problem": problem,
                    "suggested_rewrite": flag.get("suggested_rewrite", ""),
                    "source_model": flag.get("source_model", ""),
                    "section": "section_3_voice",
                }
            )

        for obs in report.get("section_8_additional") or []:
            if not isinstance(obs, dict) or obs.get("category") != "voice":
                continue
            passage = obs.get("passage", "")
            problem = obs.get("observation", "")
            if not passage and not problem:
                continue
            findings.append(
                {
                    "slug": slug,
                    "article_title": article_title,
                    "passage": passage,
                    "problem": problem,
                    "suggested_rewrite": "",
                    "source_model": obs.get("source_model", ""),
                    "section": "section_8_additional",
                }
            )

    return findings


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _cluster(findings, text_key, similarity_threshold=SIMILARITY_THRESHOLD):
    """Greedily group findings whose normalized `text_key` text is near-duplicate.

    O(n * clusters) — fine at the dozens-to-low-hundreds-of-findings scale
    this operates at. Each cluster keeps a representative (its first member's
    normalized text) that later findings are compared against.
    """
    clusters = []  # list of {"representative": str, "members": [finding, ...]}
    for finding in findings:
        text = _normalize(finding[text_key])
        if not text:
            continue
        for cluster in clusters:
            if _similar(text, cluster["representative"], similarity_threshold):
                cluster["members"].append(finding)
                break
        else:
            clusters.append({"representative": text, "members": [finding]})
    return clusters


def _cluster_to_candidate(cluster, text_key):
    members = cluster["members"]
    distinct_articles = sorted({m["slug"] for m in members})
    return {
        "pattern": cluster["representative"],
        "signal": text_key,
        "occurrence_count": len(members),
        "distinct_article_count": len(distinct_articles),
        "articles": distinct_articles,
        "examples": [
            {
                "article_title": m["article_title"],
                "slug": m["slug"],
                "passage": m["passage"],
                "problem": m["problem"],
                "source_model": m["source_model"],
            }
            for m in members[:5]
        ],
    }


def _already_banned(pattern, banned_words, banned_phrases):
    normalized_pattern = _normalize(pattern)
    for phrase in banned_words | banned_phrases:
        if _normalize(phrase) and _normalize(phrase) in normalized_pattern:
            return True
    return False


def load_banned_terms(config_path):
    """Read style_rules.banned_words/banned_phrases from a publication config.

    Read-only — used only to filter candidates already covered, never to
    modify the config.
    """
    if not config_path:
        return set(), set()
    path = Path(config_path)
    if not path.is_file():
        return set(), set()
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    style = config.get("style_rules", {}) or {}
    return (
        set(style.get("banned_words", []) or []),
        set(style.get("banned_phrases", []) or []),
    )


def candidate_patterns(
    findings,
    text_key,
    min_articles=MIN_ARTICLES,
    similarity_threshold=SIMILARITY_THRESHOLD,
    banned_words=frozenset(),
    banned_phrases=frozenset(),
):
    clusters = _cluster(findings, text_key, similarity_threshold)
    candidates = []
    for cluster in clusters:
        if len({m["slug"] for m in cluster["members"]}) < min_articles:
            continue
        candidate = _cluster_to_candidate(cluster, text_key)
        if _already_banned(candidate["pattern"], banned_words, banned_phrases):
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda c: c["distinct_article_count"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Top-level aggregation + console output
# ---------------------------------------------------------------------------


def build_voice_pattern_report(
    history_root=None,
    publication=None,
    min_articles=MIN_ARTICLES,
    similarity_threshold=SIMILARITY_THRESHOLD,
    config_path=None,
):
    if history_root is None:
        history_root = resolve_history_root()
    entries = load_reports(history_root)
    findings = extract_voice_findings(entries, publication=publication)
    banned_words, banned_phrases = load_banned_terms(config_path)

    return {
        "history_root": str(history_root),
        "publication": publication,
        "total_reports": len(entries),
        "total_voice_findings": len(findings),
        "min_articles": min_articles,
        "passage_candidates": candidate_patterns(
            findings,
            "passage",
            min_articles=min_articles,
            similarity_threshold=similarity_threshold,
            banned_words=banned_words,
            banned_phrases=banned_phrases,
        ),
        "problem_pattern_candidates": candidate_patterns(
            findings,
            "problem",
            min_articles=min_articles,
            similarity_threshold=similarity_threshold,
            banned_words=banned_words,
            banned_phrases=banned_phrases,
        ),
    }


def _print_candidates(candidates, heading):
    print(f"\n{heading}:")
    if not candidates:
        print("  (none found)")
        return
    for c in candidates:
        print(
            f"\n  Flagged in {c['distinct_article_count']} article(s) "
            f"({c['occurrence_count']} occurrence(s)):"
        )
        print(f"    Pattern: {c['pattern']!r}")
        print(f"    Articles: {', '.join(c['articles'])}")
        for ex in c["examples"][:2]:
            snippet = ex["passage"] or ex["problem"]
            print(f"    e.g. [{ex['article_title']!r}] {snippet[:120]!r}")


def print_voice_pattern_report(result):
    print("\n" + "=" * 60)
    print("RECURRING VOICE PATTERN SUGGESTIONS")
    scope = result["publication"] or "all publications"
    print(
        f"Scope: {scope}  ({result['total_reports']} run report(s), "
        f"{result['total_voice_findings']} voice finding(s), "
        f"under {result['history_root']})"
    )
    print(f"Minimum distinct articles to qualify: {result['min_articles']}")
    print("=" * 60)

    if result["total_voice_findings"] == 0:
        print("\nNo voice findings found.")
        return

    _print_candidates(
        result["passage_candidates"],
        "Recurring flagged passages (candidate banned_phrases entries)",
    )
    _print_candidates(
        result["problem_pattern_candidates"],
        "Recurring voice problems (candidate patterns worth reviewing)",
    )

    print(
        "\nThis is a suggestion report only — nothing here was written to any "
        "config. A human should review each candidate before adding it to "
        "style_rules.banned_words/banned_phrases.\n"
    )


def build_parser():
    """Construct the CLI parser.

    Split out of main() so tests can introspect the flags without running the
    report — see tests/test_docs_current.py.
    """
    parser = argparse.ArgumentParser(
        description="Article Review Pipeline — recurring voice pattern suggestions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ci-voice-patterns\n"
            "  ci-voice-patterns --publication mikehammett --config configs/mikehammett.yaml\n"
            "  ci-voice-patterns --min-articles 2 --json\n"
        ),
    )
    parser.add_argument(
        "--history-root",
        default=resolve_history_root(),
        help=(
            "Directory containing per-article run history (default: "
            f"${HISTORY_ROOT_ENV} if set, else {HISTORY_ROOT})"
        ),
    )
    parser.add_argument(
        "--publication",
        help="Scope to reports whose `publication` field matches this value",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Publication config YAML to read existing style_rules.banned_words/"
        "banned_phrases from, so already-banned patterns are excluded "
        "(read-only, never modified)",
    )
    parser.add_argument(
        "--min-articles",
        type=int,
        default=MIN_ARTICLES,
        help=f"Minimum distinct articles a pattern must appear in to be reported "
        f"(default: {MIN_ARTICLES})",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=SIMILARITY_THRESHOLD,
        help=f"Normalized-text similarity ratio (0-1) for two findings to count "
        f"as the same pattern (default: {SIMILARITY_THRESHOLD})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw result as JSON instead of the console summary",
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

    result = build_voice_pattern_report(
        args.history_root,
        publication=args.publication,
        min_articles=args.min_articles,
        similarity_threshold=args.similarity_threshold,
        config_path=args.config,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_voice_pattern_report(result)


if __name__ == "__main__":
    main()
