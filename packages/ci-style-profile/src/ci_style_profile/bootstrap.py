#!/usr/bin/env python3
"""Style Profile Bootstrap — CLI entry point.

Usage:
  python style-profile-bootstrap/bootstrap.py --publication mikehammett --sources wordpress --style detect

Startup sequence:
  1. Load sources.yaml, apply env var substitution
  2. Configure logging (--log-level overrides config)
  3. Load presets.yaml, apply selected preset, apply sources.yaml overrides, apply CLI flags
  4. Build collector registry
  5. Validate collector configs
  6. Check staging schema versions
  7. Log expected API call count
  8. Collect (parallel daemon threads, see ci_core.concurrency)
  9. Deduplicate by content_hash
  10. Normalize (compute metrics in-place)
  11. Print corpus stats + bias warnings
  12. If detect mode: run detection pass; if --dry-run exit here
  13. If canonical/per-source and --dry-run: exit here
  14. Synthesize
  15. Validate synthesis output
  16. Write output (atomic)
  17. Save versioned snapshot
  18. Update watermarks
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml as _yaml
from dotenv import find_dotenv, load_dotenv

from ci_core.concurrency import run_all_bounded
from ci_core.console import force_utf8_stdio
from ci_core.env_provenance import (
    effective_env as _effective_env,
    snapshot as _snapshot_dotenv,
)

# The corpus stats and bias warnings quote collected documents.
force_utf8_stdio()

# sources.yaml and user.yaml resolve ${ENV_VAR} placeholders via
# ci_core.config_helpers, which reads os.environ — so the .env file has to be
# loaded before either is read. This used to happen implicitly: the config
# helpers were imported from ci_article_review.config_loader, which calls
# load_dotenv() at import time. That import is gone, so do it explicitly here.
#
# Snapshot before load_dotenv() touches the OS environment, same reasoning as
# ci_article_review.config_loader: a .env-defined value must win over a
# same-named OS environment variable, not the reverse (see
# ci_core.env_provenance). Without this, ci-discover / ci-style-profile share
# configs/user.yaml and .env with ci-review but silently resolved ${VAR}
# against plain os.environ — the exact incident ci-review's config_loader was
# fixed for, just not fixed here too.
_DOTENV_PATH = find_dotenv()
_ENV_SNAPSHOT = _snapshot_dotenv(_DOTENV_PATH)
load_dotenv(_DOTENV_PATH or None)
_EFFECTIVE_ENV = _effective_env(_ENV_SNAPSHOT)

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

#: Wall-clock safety net for collecting one source, as a whole. Deliberately
#: generous: the real bound on a collection is the per-request timeout inside
#: each collector, and a large WordPress or Drupal corpus legitimately takes
#: many minutes to page through. This exists only to catch what those don't —
#: a paginator that never advances, or a host that keeps a socket alive by
#: dribbling a byte inside every read gap. Without it a single wedged source
#: hangs the whole bootstrap run indefinitely, because there is no other
#: wall-clock bound anywhere in the collection path.
_COLLECT_TIMEOUT_SECONDS = 3600
_DEFAULT_STAGING_DIR = Path(__file__).parent / "staging"
_WATERMARKS_FILE = _DEFAULT_STAGING_DIR / ".watermarks.json"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_sources_yaml() -> dict:
    """Load sources.yaml from style-profile-bootstrap directory."""
    path = Path(__file__).parent / "sources.yaml"
    if not path.exists():
        log.warning("sources.yaml not found at %s; using empty config", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
    except Exception as e:
        log.error("Failed to load sources.yaml: %s", e)
        return {}
    from ci_core.config_helpers import resolve_env_recursive

    return resolve_env_recursive(data, env=_EFFECTIVE_ENV)


def _load_presets() -> dict:
    """Load style-profile presets from configs/presets.yaml."""
    presets_path = Path(__file__).parent / "configs" / "presets.yaml"
    if presets_path.exists():
        try:
            with open(presets_path, encoding="utf-8") as f:
                return _yaml.safe_load(f) or {}
        except Exception as e:
            log.warning("Could not load presets.yaml: %s; using defaults", e)
    return _HARDCODED_PRESETS


_HARDCODED_PRESETS = {
    "economy": {
        "style_mode": "canonical",
        "max_input_chars": 40000,
        "max_styles": 0,
        "per_style_min_words": 1000,
        "synthesis_models": ["claude"],
        "detection_models": [],
    },
    "standard": {
        "style_mode": "detect",
        "max_input_chars": 80000,
        "max_styles": 3,
        "per_style_min_words": 1500,
        "synthesis_models": ["claude", "openai"],
        "detection_models": ["claude"],
    },
    "balanced": {
        "style_mode": "detect",
        "max_input_chars": 120000,
        "max_styles": 5,
        "per_style_min_words": 2000,
        "synthesis_models": [],
        "detection_models": [],
    },
    "thorough": {
        "style_mode": "detect",
        "max_input_chars": 160000,
        "max_styles": 7,
        "per_style_min_words": 2000,
        "synthesis_models": [],
        "detection_models": [],
    },
    "maximum": {
        "style_mode": "detect",
        "max_input_chars": 200000,
        "max_styles": 10,
        "per_style_min_words": 2000,
        "synthesis_models": [],
        "detection_models": "*",
    },
}


def _apply_preset(
    preset_name: str, presets: dict, sources_cfg: dict, cli_args: argparse.Namespace
) -> dict:
    """Merge preset → sources.yaml synthesis keys → CLI flags → return effective config."""
    preset = presets.get(preset_name, presets.get("balanced", {}))
    synth_cfg = sources_cfg.get("synthesis", {})

    # Start from preset
    effective = dict(preset)

    # Apply sources.yaml synthesis overrides on top
    for key in (
        "style_mode",
        "max_styles",
        "max_input_chars",
        "prompt_overhead_chars",
        "per_style_min_words",
        "ambiguity_threshold",
        "consensus_threshold",
        "detection_models",
        "max_parallel_models",
        "per_source_group_by",
        "synthesis_models",
    ):
        if key in synth_cfg:
            effective[key] = synth_cfg[key]

    # CLI flags win over everything
    if cli_args.style:
        effective["style_mode"] = cli_args.style
    if cli_args.max_styles is not None:
        effective["max_styles"] = cli_args.max_styles

    return effective


def _load_user_config_lenient() -> dict:
    """Load user.yaml without strict validation (style profiler works with any model subset)."""
    from ci_core.config_helpers import load_yaml, resolve_env_recursive

    path = Path("configs/user.yaml")
    if not path.exists():
        log.warning("configs/user.yaml not found; no API models available")
        return {}
    try:
        config = load_yaml(str(path))
        config = resolve_env_recursive(config or {}, env=_EFFECTIVE_ENV)
        return config
    except Exception as e:
        log.warning("Could not load user.yaml: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Staging helpers
# ---------------------------------------------------------------------------


def _staging_path(source: str) -> Path:
    return _DEFAULT_STAGING_DIR / f"{source}.ndjson"


def _load_staged(source: str) -> list | None:
    """Load staged docs for a source. Returns None if not staged or schema mismatch."""
    path = _staging_path(source)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return None
        header = json.loads(lines[0])
        if header.get("schema_version") != _SCHEMA_VERSION:
            log.warning(
                "Staging schema version mismatch for %s; forcing refresh", source
            )
            return None
        from .collectors.base import Document

        docs = []
        for line in lines[1:]:
            line = line.strip()
            if line:
                try:
                    d = json.loads(line)
                    docs.append(
                        Document(
                            text=d["text"],
                            source=d["source"],
                            register=d["register"],
                            date=d["date"],
                            url_or_id=d["url_or_id"],
                            word_count=d["word_count"],
                            content_hash=d["content_hash"],
                            metadata=d.get("metadata", {}),
                            metrics={},  # Never read from staging; recomputed by normalize
                        )
                    )
                except Exception:
                    pass
        return docs
    except Exception as e:
        log.warning("Could not load staging for %s: %s", source, e)
        return None


def _write_staged(source: str, docs: list, no_stage: bool = False) -> None:
    if no_stage:
        return
    from datetime import datetime, timezone

    path = _staging_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "source": source,
            "generated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "watermark": _load_watermarks().get(source),
        }
    )
    lines = [header]
    for doc in docs:
        # Never stage metrics; recompute on each run
        d = {
            "text": doc.text,
            "source": doc.source,
            "register": doc.register,
            "date": doc.date,
            "url_or_id": doc.url_or_id,
            "word_count": doc.word_count,
            "content_hash": doc.content_hash,
            "metadata": doc.metadata,
        }
        lines.append(json.dumps(d))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log.debug("Staged %d docs for %s", len(docs), source)


def _load_watermarks() -> dict:
    if _WATERMARKS_FILE.exists():
        try:
            return json.loads(_WATERMARKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_watermarks(watermarks: dict) -> None:
    _WATERMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WATERMARKS_FILE.write_text(json.dumps(watermarks, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def _collect_source(
    source_name: str,
    collector_cls,
    source_cfg: dict,
    since: str | None,
    watermarks: dict,
    refresh: bool,
    no_stage: bool,
) -> list:
    """Collect or load from staging for one source."""
    from .collectors.base import CollectorError

    watermark = watermarks.get(source_name) if not refresh else None
    effective_since = watermark or since

    # Try loading from staging unless refresh
    if not refresh:
        staged = _load_staged(source_name)
        if staged is not None:
            log.info("[%s] Using staged corpus (%d docs)", source_name, len(staged))
            return staged

    # Collect fresh
    collector = collector_cls(source_cfg)
    docs = []
    try:
        for doc in collector.fetch(since=effective_since):
            docs.append(doc)
        log.info("[%s] Collected %d docs", source_name, len(docs))
    except CollectorError:
        raise
    except Exception as e:
        raise CollectorError(source_name, f"Unexpected error: {e}") from e

    _write_staged(source_name, docs, no_stage=no_stage)
    return docs


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------


def _resolve_output_path(
    publication: str | None, output_yaml: str | None
) -> Path | None:
    """Resolve the output path from --publication or --output-yaml."""
    if publication:
        return Path("configs") / f"{publication}.yaml"
    elif output_yaml:
        return Path(output_yaml)
    return None


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Synthesize a style profile from your writing corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python style-profile-bootstrap/bootstrap.py --publication mikehammett --sources wordpress --dry-run
  python style-profile-bootstrap/bootstrap.py --publication mikehammett --sources wordpress,gmail --style detect
  python style-profile-bootstrap/bootstrap.py --output-yaml my_profile.yaml --sources textfiles --style canonical

Note: --dry-run with --style detect runs one detection API call (not free).
""",
    )

    output_group = parser.add_mutually_exclusive_group(required=False)
    output_group.add_argument(
        "--publication",
        metavar="NAME",
        help="Publication name; resolves to configs/<name>.yaml",
    )
    output_group.add_argument(
        "--output-yaml",
        metavar="PATH",
        help="Explicit output path (mutually exclusive with --publication)",
    )

    parser.add_argument(
        "--sources",
        metavar="SOURCE[,SOURCE...]",
        help="Comma-separated source names (default: all in sources.yaml)",
    )
    parser.add_argument(
        "--since", metavar="DATE", help="ISO date; applied at API level where supported"
    )
    parser.add_argument(
        "--style",
        choices=["canonical", "detect", "per-source"],
        help="Synthesis mode (overrides preset and sources.yaml)",
    )
    parser.add_argument(
        "--max-styles",
        type=int,
        metavar="N",
        help="Max detected style count (detect mode only; 0 = no limit)",
    )
    parser.add_argument(
        "--preset",
        default="balanced",
        choices=["economy", "standard", "balanced", "thorough", "maximum"],
        help="Run intensity preset (default: balanced)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore staging cache; re-fetch all sources",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect + normalize + stats only (detect mode: runs one detection pass — costs money)",
    )
    parser.add_argument(
        "--no-stage",
        action="store_true",
        help="Don't write staging files; process in memory only",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Skip failed collectors and continue with remaining sources",
    )
    parser.add_argument(
        "--format",
        choices=["yaml", "markdown", "json"],
        default="yaml",
        dest="output_format",
        help="Output format (default: yaml)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Skip confirmation prompt when merging into existing file",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override logging level from sources.yaml",
    )
    parser.add_argument(
        "--check-draft",
        metavar="PATH",
        help="[Not implemented] Style consistency check on a draft file",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --check-draft stub
    if args.check_draft:
        print(
            "--check-draft is planned for Phase 7 and not yet implemented.",
            file=sys.stderr,
        )
        raise NotImplementedError("--check-draft is not implemented")

    # 1. Load sources.yaml
    sources_cfg = _load_sources_yaml()

    # 2. Configure logging BEFORE any module logs
    from .logging_config import configure_logging

    configure_logging(sources_cfg.get("logging", {}), log_level_override=args.log_level)

    log.info("Style Profile Bootstrap starting (preset=%s)", args.preset)

    # 3. Load presets and build effective config
    presets = _load_presets()
    effective_cfg = _apply_preset(args.preset, presets, sources_cfg, args)

    style_mode = effective_cfg.get("style_mode", "detect")
    max_input_chars = int(effective_cfg.get("max_input_chars", 120000))
    prompt_overhead_chars = int(effective_cfg.get("prompt_overhead_chars", 12000))
    max_styles = int(effective_cfg.get("max_styles", 5))
    per_style_min_words = int(effective_cfg.get("per_style_min_words", 2000))
    ambiguity_threshold = float(effective_cfg.get("ambiguity_threshold", 0.2))
    detection_models = effective_cfg.get("detection_models")
    synthesis_models = effective_cfg.get("synthesis_models") or None
    max_parallel = int(effective_cfg.get("max_parallel_models", 0))
    per_source_group_by = effective_cfg.get("per_source_group_by", "source")

    # Load user config for API calls
    user_config = _load_user_config_lenient()

    # Apply preset model overrides to user_config
    preset_models = presets.get(args.preset, {}).get("models", {})
    if preset_models and user_config.get("models"):
        for model_name, preset_model_cfg in preset_models.items():
            if model_name in user_config["models"] and isinstance(
                preset_model_cfg, dict
            ):
                existing = user_config["models"][model_name]
                if isinstance(existing, str):
                    existing = {"model": existing}
                merged = {**existing}
                for k, v in preset_model_cfg.items():
                    if k not in ("provider",):  # preserve infra keys from user.yaml
                        merged[k] = v
                user_config["models"][model_name] = merged

    # Check model currency
    try:
        from ci_core.llm.model_registry import check_model_currency

        warnings = check_model_currency(user_config.get("models", {}))
        for w in (warnings or {}).values():
            if w:
                log.warning("Model currency: %s", w)
    except Exception:
        pass

    # 4. Build collector registry
    from .collectors import REGISTRY
    from .collectors.base import CollectorError, ConfigError

    # 5. Determine which sources to use
    if args.sources:
        requested_sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        # All sources configured in sources.yaml
        requested_sources = [
            name
            for name in REGISTRY
            if name in sources_cfg and name not in ("synthesis", "logging")
        ]
        if not requested_sources:
            requested_sources = list(REGISTRY.keys())

    # Validate configs
    failed_sources = []
    for source_name in list(requested_sources):
        collector_cls = REGISTRY.get(source_name)
        if not collector_cls:
            log.warning(
                "Unknown source %r; available: %s", source_name, list(REGISTRY.keys())
            )
            if not args.continue_on_error:
                return 1
            failed_sources.append(source_name)
            continue

        source_specific_cfg = sources_cfg.get(source_name, {})
        try:
            collector_cls.validate_config(source_specific_cfg)
        except ConfigError as e:
            msg = f"Config validation failed for {source_name}: {e}"
            if args.continue_on_error:
                log.warning("%s (skipping)", msg)
                failed_sources.append(source_name)
            else:
                log.error("%s", msg)
                return 1

    active_sources = [s for s in requested_sources if s not in failed_sources]
    if not active_sources:
        log.error("No valid sources available")
        return 1

    # 6. Watermarks
    watermarks = _load_watermarks()
    if args.refresh:
        log.info("--refresh: clearing watermarks")
        watermarks = {}

    # 7. Pre-synthesis cost estimate (logged after collection)
    # (Will log once we know actual doc counts)

    # 8. Collect in parallel
    all_docs = []
    collection_errors = []

    def _collect_task(source_name):
        collector_cls = REGISTRY[source_name]
        source_specific_cfg = sources_cfg.get(source_name, {})
        return _collect_source(
            source_name,
            collector_cls,
            source_specific_cfg,
            since=args.since,
            watermarks=watermarks,
            refresh=args.refresh,
            no_stage=args.no_stage,
        )

    # One daemon thread per source, all of them at once (what the previous
    # `max_workers=len(active_sources)` meant) — never a ThreadPoolExecutor.
    # See :mod:`ci_core.concurrency`: a pool worker still inside a collector's
    # network I/O at interpreter exit is joined by concurrent.futures' atexit
    # hook with a bare, untimed t.join().
    #
    # The old form also made "fail fast" a lie. `return 1` sat *inside* the
    # `with` block, so bailing on the first error still ran the pool's __exit__
    # first, which joins every remaining worker — the run waited out every other
    # source's collection before returning non-zero. Collecting the whole batch
    # up front and then deciding is both honest about that and bounded, and it
    # makes which error wins deterministic (source order) rather than a race.
    outcomes = run_all_bounded(
        [
            (name, lambda name=name: _collect_task(name), _COLLECT_TIMEOUT_SECONDS)
            for name in active_sources
        ],
        max_parallel=0,
    )

    for source_name in active_sources:
        docs, error = outcomes[source_name]
        if error is None:
            all_docs.extend(docs)
            continue
        if isinstance(error, CollectorError):
            msg = f"Collection failed for {source_name}: {error}"
            recorded = str(error)
        else:
            msg = f"Unexpected error collecting {source_name}: {error}"
            recorded = msg
        if args.continue_on_error:
            log.warning("%s (skipping)", msg)
            collection_errors.append(recorded)
        else:
            log.error("%s", msg)
            return 1

    if not all_docs:
        log.error("No documents collected. Check your sources configuration.")
        return 1

    # 9. Deduplicate
    from .normalize import (
        deduplicate,
        corpus_summary,
        corpus_bias_warnings,
        compute_metrics,
        clean_text,
    )

    all_docs, n_dropped = deduplicate(all_docs)
    log.info(
        "Corpus: %d unique documents (dropped %d duplicates)", len(all_docs), n_dropped
    )

    # 10. Normalize — clean text and compute metrics in place
    for doc in all_docs:
        doc.text = clean_text(doc.text, doc.source)
        doc.metrics = compute_metrics(doc)

    # 11. Corpus stats + bias warnings
    summary = corpus_summary(all_docs)
    log.info(
        "Corpus stats: %d docs, %d words, date range: %s",
        summary["doc_count"],
        summary["total_words"],
        summary.get("date_range"),
    )
    for src, info in summary.get("sources", {}).items():
        log.info(
            "  %s: %d docs, %d words (%.1f%%)",
            src,
            info["doc_count"],
            info["word_count"],
            summary["source_word_pct"].get(src, 0),
        )

    bias_warnings = corpus_bias_warnings(summary)
    for w in bias_warnings:
        print(w, file=sys.stderr)

    total_words = summary.get("total_words", 0)
    if total_words < 1000:
        log.error("Corpus too small (%d words); minimum 1,000 required", total_words)
        return 1

    # Print dry-run stats for canonical / per-source (zero API calls)
    if args.dry_run and style_mode in ("canonical", "per-source"):
        print("\n=== DRY RUN — CORPUS STATS ===")
        print(f"Documents: {summary['doc_count']}")
        print(f"Total words: {summary['total_words']:,}")
        if summary.get("date_range"):
            print(
                f"Date range: {summary['date_range'][0]} – {summary['date_range'][1]}"
            )
        for src, pct in summary.get("source_word_pct", {}).items():
            info = summary["sources"][src]
            print(
                f"  {src}: {info['doc_count']} docs, {info['word_count']:,} words ({pct}%)"
            )
        for w in bias_warnings:
            print(w)
        print("\nNo API calls made (--dry-run).")
        return 0

    # 12. Detection dry-run (runs detection API call)
    if args.dry_run and style_mode == "detect":
        print("\n=== DRY RUN — DETECTION PASS (this costs money) ===")
        from .detect import detect_styles, CanonicalFallbackWarning

        try:
            clusters = detect_styles(
                all_docs,
                user_config,
                max_styles=max_styles,
                detection_models=detection_models if detection_models else None,
                max_parallel=max_parallel,
                max_chars=max_input_chars - prompt_overhead_chars,
            )
            print(f"\nDetected {len(clusters)} style(s):")
            for c in clusters:
                print(f"  [{c.confidence}] {c.label}: {c.description[:100]}")
        except CanonicalFallbackWarning as w:
            print(f"\nDetection low confidence: {w}")
            print("Would fall back to canonical mode for full run.")

        from .callers import log_cost_summary

        log_cost_summary()
        return 0

    # 13. Synthesis
    if not args.publication and not args.output_yaml:
        log.error("Either --publication or --output-yaml is required for synthesis")
        parser.print_usage(sys.stderr)
        return 1

    output_path = _resolve_output_path(args.publication, args.output_yaml)

    from .synthesize import synthesize, SynthesisError

    try:
        log.info("Starting synthesis (mode=%s)", style_mode)
        profile = synthesize(
            docs=all_docs,
            user_config=user_config,
            mode=style_mode,
            synthesis_models=synthesis_models,
            detection_models=detection_models,
            max_styles=max_styles,
            max_input_chars=max_input_chars,
            prompt_overhead_chars=prompt_overhead_chars,
            ambiguity_threshold=ambiguity_threshold,
            per_style_min_words=per_style_min_words,
            max_parallel=max_parallel,
            per_source_group_by=per_source_group_by,
        )
    except SynthesisError as e:
        log.error("Synthesis failed: %s", e)
        return 1

    # 15. Validate (already done inside synthesize, but belt-and-suspenders)
    # 16. Format and write output
    from .output import (
        Formatter,
        PublicationYamlFormatter,
        MarkdownReportFormatter,
        JsonFormatter,
        write_atomic,
        save_versioned_snapshot,
    )

    formatter: Formatter
    if args.output_format == "yaml":
        formatter = PublicationYamlFormatter()
    elif args.output_format == "markdown":
        formatter = MarkdownReportFormatter()
    else:
        formatter = JsonFormatter()

    # For yaml format: merge into existing; for others: format fresh
    if args.output_format == "yaml" and isinstance(formatter, PublicationYamlFormatter):
        if output_path and output_path.exists():
            # Show diff
            new_yaml = formatter.merge_into_existing(
                str(output_path), profile, mode=style_mode
            )
            diff = formatter.diff_style_sections(str(output_path), new_yaml)
            if diff and not args.overwrite:
                print("\n=== DIFF — style section changes ===")
                print(diff)
                answer = input("\nWrite these changes? [y/N] ").strip().lower()
                if answer != "y":
                    print("Aborted. No changes written.")
                    return 0
            content = new_yaml
        else:
            content = formatter.format(profile, mode=style_mode)
    else:
        content = formatter.format(profile, mode=style_mode)

    # Add warning header if there were collection errors
    if collection_errors and args.output_format == "yaml":
        warning_lines = ["# WARNING: Some sources failed during collection:"]
        for err in collection_errors:
            warning_lines.append(f"#   {err}")
        content = "\n".join(warning_lines) + "\n\n" + content

    # Add fallback reason header if detection fell back
    if profile.get("_fallback_reason") and args.output_format == "yaml":
        content = (
            f"# NOTE: Detection fell back to canonical mode: {profile['_fallback_reason']}\n\n"
            + content
        )

    if output_path:
        write_atomic(output_path, content)
        print(f"\nProfile written to: {output_path}")
    else:
        print(content)

    # 17. Save versioned snapshot
    if args.output_format == "yaml":
        snap = save_versioned_snapshot(content, args.publication, args.output_yaml)
        log.debug("Snapshot saved: %s", snap)

    # 18. Update watermarks
    if not args.no_stage:
        new_watermarks = dict(watermarks)
        for source_name in active_sources:
            source_docs = [d for d in all_docs if d.source == source_name]
            if source_docs:
                latest_date = max(d.date for d in source_docs if d.date)
                if latest_date:
                    new_watermarks[source_name] = latest_date
        _save_watermarks(new_watermarks)

    # Log cost summary
    from .callers import log_cost_summary

    log_cost_summary()

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
