"""
Merge ensemble model responses into a structured review report.

The pipeline runs each configured model against each assigned prompt domain,
producing a dict of results keyed by (model_name, domain).  This module merges
those results into a single report with eight sections.

Weighting
---------
Each (model, domain) pair has a configurable weight that reflects how well-
suited the model is for that domain.  Weights are used in two places:

  1. Consensus detection (section 1): a passage is flagged as consensus when
     the sum of weights of all models that flagged it meets the threshold
     (default 2.0).  LanguageTool adds a partial vote (default 0.5).

  2. Section ordering: within sections 2-6, findings from higher-weight models
     are sorted first so the most reliable signal appears at the top.

Built-in default weights reflect observed capability fit:

  Model       fact_check  voice_style  completeness  argument  red_team
  ----------  ----------  -----------  ------------  --------  --------
  gemini      1.5 *       1.0          1.0           1.0       1.0
  perplexity  1.5 *       1.0          1.0           1.0       1.0
  openai      1.0         1.2          1.2           1.0       1.0
  mistral     1.0         1.0          1.0           1.2       1.1
  grok        1.0         1.0          1.0           1.0       1.2
  claude      1.0         1.1          1.1           1.3       1.0

  * Grounding bonus: search-grounded models receive a 1.5x weight for
    fact_check because their claims are verifiable against live sources.

All weights are configurable in configs/user.yaml under the ``ensemble`` key.

Thoroughness levels (set under ``pipeline.thoroughness`` in user.yaml)
-----------------------------------------------------------------------
  standard  — one primary model per domain (current default behavior)
  thorough  — two to three models per domain; search-grounded models for
              fact_check, specialised models for other domains
  maximum   — every configured model runs every domain

Per-model overrides (``models.<name>.prompts``) take precedence over the
thoroughness preset for that model.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

#: Built-in domain weights.  Configurable under ``ensemble.weights`` in user.yaml.
#: The ``default`` key applies to all domains not explicitly listed.
_DEFAULT_WEIGHTS = {
    "gemini": {"default": 1.0, "fact_check": 1.5},
    "perplexity": {"default": 1.0, "fact_check": 1.5},
    "openai": {"default": 1.0, "voice_style": 1.2, "completeness": 1.2},
    "mistral": {"default": 1.0, "argument_integrity": 1.2, "red_team": 1.1},
    "grok": {"default": 1.0, "red_team": 1.2},
    "claude": {"default": 1.0, "argument_integrity": 1.3, "voice_style": 1.1},
}

#: Weighted sum required to call a passage consensus.
_DEFAULT_CONSENSUS_THRESHOLD = 2.0

#: Partial vote weight added when LanguageTool also flagged a passage.
_DEFAULT_LT_WEIGHT = 0.5

#: Which report section each domain feeds, so a failed pass can name the section
#: it left short-handed rather than only the pass that died. Section 1 draws on
#: every domain and is reported separately.
#:
#: These strings are the readable report's own headings, verbatim — a reader
#: told "SECTION 6 was built without this model" has to be able to find SECTION
#: 6. A test in test_report_markdown asserts they stay in step with the
#: renderer.
_DOMAIN_SECTIONS = {
    "fact_check": "SECTION 2: Factual Verification",
    "voice_style": "SECTION 3: Voice and AI-Speak",
    "completeness": "SECTION 5: Completeness and Framing",
    "argument_integrity": "SECTION 4: Argument Integrity",
    "red_team": "SECTION 6: Red Team Findings",
}


def _get_weight(model_name, domain, ensemble_cfg):
    """Return effective weight for a (model, domain) pair.

    Checks user-configured weights first, then falls back to built-in defaults.
    """
    user_weights = ensemble_cfg.get("weights", {})
    model_weights = user_weights.get(model_name, {})

    # User-configured domain-specific weight
    if domain in model_weights:
        return float(model_weights[domain])
    # User-configured model default
    if "default" in model_weights:
        return float(model_weights["default"])

    # Built-in default
    defaults = _DEFAULT_WEIGHTS.get(model_name, {"default": 1.0})
    return float(defaults.get(domain, defaults.get("default", 1.0)))


# ---------------------------------------------------------------------------
# Passage normalisation
# ---------------------------------------------------------------------------


def _passage_key(passage):
    """Normalise a passage for fuzzy cross-model matching.

    250 chars retains most of a typical factual claim while still tolerating
    minor wording differences between models.  Consensus detection benefits from
    the same precision — a false positive there is worse than a missed match.
    """
    return " ".join(passage.lower().split())[:250]


# ---------------------------------------------------------------------------
# Passage extraction per domain schema
# ---------------------------------------------------------------------------


def _extract_passages(model_name, domain, result):
    """Return list of (passage_text, flag_data_dict) pairs for consensus detection.

    Each domain has a different JSON schema:
      fact_check         — outdated[].claim, contradicted[].claim
      voice_style        — flags[].passage
      argument_integrity — flags[].passage
      completeness       — flags[].passage_reference
      red_team           — most_vulnerable_claim.passage etc.
    """
    if result.get("failed") or not result.get("data"):
        return []

    data = result["data"]
    out = []

    if domain == "fact_check":
        for item in data.get("outdated", []):
            claim = item.get("claim", "")
            if claim:
                out.append(
                    (
                        claim,
                        {
                            **item,
                            "domain": domain,
                            "type": "outdated",
                            "source_model": model_name,
                        },
                    )
                )
        for item in data.get("contradicted", []):
            claim = item.get("claim", "")
            if claim:
                out.append(
                    (
                        claim,
                        {
                            **item,
                            "domain": domain,
                            "type": "contradicted",
                            "source_model": model_name,
                        },
                    )
                )

    elif domain in ("voice_style", "argument_integrity"):
        for flag in data.get("flags", []):
            passage = flag.get("passage", "")
            if passage:
                out.append(
                    (passage, {**flag, "domain": domain, "source_model": model_name})
                )

    elif domain == "completeness":
        for flag in data.get("flags", []):
            passage = flag.get("passage_reference", "")
            if passage:
                out.append(
                    (passage, {**flag, "domain": domain, "source_model": model_name})
                )

    elif domain == "red_team":
        for rt_key in (
            "most_vulnerable_claim",
            "highest_audience_risk",
            "highest_credibility_risk",
        ):
            item = data.get(rt_key, {})
            passage = item.get("passage", "")
            if passage:
                out.append(
                    (
                        passage,
                        {
                            **item,
                            "domain": domain,
                            "type": rt_key,
                            "source_model": model_name,
                        },
                    )
                )

    return out


# ---------------------------------------------------------------------------
# Consensus detection
# ---------------------------------------------------------------------------


#: Default confidence multipliers: all 1.0, i.e. exactly the previous behaviour.
#:
#: The models are asked for a `confidence` on every fact-check item and every
#: additional observation, and nothing read it. Two models flagging a passage at
#: "low" produced a Section 1 consensus flag identical to two at "high" — the
#: hedging was discarded precisely on the path into the section that implies the
#: most certainty, and Section 1 is what the revision prompt feeds back to the
#: drafting model.
#:
#: Off by default on purpose. Self-reported confidence is not calibrated and is
#: not comparable across providers, and turning it on shifts what lands in
#: Section 1 — so it is a deliberate choice with a golden-report diff attached,
#: not a silent change to everyone's thresholds. Set ensemble.confidence_weights
#: to enable; a sane starting point is high 1.0 / medium 0.75 / low 0.5.
_DEFAULT_CONFIDENCE_MULTIPLIERS = {"high": 1.0, "medium": 1.0, "low": 1.0}


def _confidence_multipliers(ensemble_cfg):
    """Resolve the configured confidence multipliers, falling back to no-op."""
    configured = (ensemble_cfg or {}).get("confidence_weights") or {}
    merged = dict(_DEFAULT_CONFIDENCE_MULTIPLIERS)
    for level, value in configured.items():
        try:
            merged[str(level).strip().lower()] = float(value)
        except (TypeError, ValueError):
            log.warning(
                "ensemble.confidence_weights.%s is not a number (%r) — ignoring it.",
                level,
                value,
            )
    return merged


def _confidence_multiplier(flag_data, multipliers):
    """Multiplier for one finding's self-reported confidence.

    Damps a finding's contribution; never vetoes it. A "low" flag several models
    agree on should still be able to reach consensus, because agreement is
    evidence even when each model hedged. An unrecognised or absent value scores
    1.0 — most domains do not emit a confidence at all, and their weighting must
    not change.
    """
    level = str(flag_data.get("confidence", "")).strip().lower()
    return multipliers.get(level, 1.0)


def _find_consensus(results, lt_flagged_passages, ensemble_cfg):
    """Weighted consensus detection across all model/domain results.

    Returns (consensus_flags, single_source_flags).  consensus_flags are
    sorted by weight_sum descending so the strongest findings appear first.
    """
    threshold = float(
        ensemble_cfg.get("consensus_threshold", _DEFAULT_CONSENSUS_THRESHOLD)
    )
    lt_weight = float(ensemble_cfg.get("lt_weight", _DEFAULT_LT_WEIGHT))

    # passage_key → accumulated data
    passage_map: dict[str, dict] = {}

    confidence_multipliers = _confidence_multipliers(ensemble_cfg)

    for (model_name, domain), result in results.items():
        weight = _get_weight(model_name, domain, ensemble_cfg)
        for passage, flag_data in _extract_passages(model_name, domain, result):
            key = _passage_key(passage)
            if not key:
                continue
            if key not in passage_map:
                passage_map[key] = {
                    "weight_sum": 0.0,
                    "models": [],
                    "flag_data": [],
                    "passage": passage,
                }
            multiplier = _confidence_multiplier(flag_data, confidence_multipliers)
            passage_map[key]["weight_sum"] += weight * multiplier
            passage_map[key]["models"].append(f"{model_name}:{domain}")
            passage_map[key]["flag_data"].append(flag_data)

    lt_keys = {_passage_key(p) for p in lt_flagged_passages}

    consensus = []
    single_source = []

    for key, entry in passage_map.items():
        has_lt = key in lt_keys
        effective_weight = entry["weight_sum"] + (lt_weight if has_lt else 0.0)

        if effective_weight >= threshold:
            consensus.append(
                {
                    "passage": entry["passage"],
                    "models": sorted(set(entry["models"])),
                    "weight_sum": round(effective_weight, 2),
                    "languagetool_also_flagged": has_lt,
                    "flags": entry["flag_data"],
                }
            )
        else:
            single_source.extend(entry["flag_data"])

    consensus.sort(key=lambda x: x["weight_sum"], reverse=True)
    return consensus, single_source


# ---------------------------------------------------------------------------
# Domain section builders
# ---------------------------------------------------------------------------


def _build_fact_check(results, ensemble_cfg, scope=None):
    """Merge fact_check results from all models that ran the domain.

    ``scope`` is the run's :class:`~ci_article_review.fact_check_scope.ScopeRules`.
    It is applied here, on the merged section, rather than per model: an author
    marking has to sweep every model's verdict on the same claim, and a claim one
    model puts out of scope has to be reconciled with another model's verdict on
    it. Both are cross-model questions, and this is the only place the whole set
    exists at once.
    """
    domain_results = [
        (model, r)
        for (model, d), r in results.items()
        if d == "fact_check" and not r.get("failed") and r.get("data")
    ]
    if not domain_results:
        return {}
    if len(domain_results) == 1:
        model_name, r = domain_results[0]
        # Single source — return as-is but tag with source metadata. Only
        # out_of_scope is tagged per item: the report names the model that made
        # each scope call, because "grok judged this out of scope" is a claim
        # about a model's judgment rather than about the draft.
        merged = {
            **r["data"],
            "out_of_scope": _tag_scope_items(r["data"].get("out_of_scope"), model_name),
            "_sources": {
                model_name: {
                    "weight": round(
                        _get_weight(model_name, "fact_check", ensemble_cfg), 2
                    ),
                    "grounding": r.get("grounding_available", False),
                }
            },
        }
        return _apply_scope(merged, scope)

    # Multiple sources — merge all lists, tag each item with source model and weight
    merged: dict = {
        "confirmed": [],
        "outdated": [],
        "contradicted": [],
        "unverifiable": [],
        "primary_source_needed": [],
        "out_of_scope": [],
        "additional_observations": [],
        "_sources": {},
    }
    for model_name, r in domain_results:
        weight = _get_weight(model_name, "fact_check", ensemble_cfg)
        grounding = r.get("grounding_available", False)
        tag = {
            "source_model": model_name,
            "source_weight": round(weight, 2),
            "grounding": grounding,
        }
        merged["_sources"][model_name] = {
            "weight": round(weight, 2),
            "grounding": grounding,
        }
        data = r["data"]
        for key in (
            "confirmed",
            "outdated",
            "contradicted",
            "unverifiable",
            "primary_source_needed",
            "out_of_scope",
        ):
            for item in data.get(key, []) or []:
                merged[key].append({**item, **tag})
        for obs in data.get("additional_observations", []):
            merged["additional_observations"].append(
                {**obs, "source_model": model_name}
            )

    # Sort problem arrays: higher-weight model findings first
    for key in ("outdated", "contradicted"):
        merged[key].sort(key=lambda x: x.get("source_weight", 1.0), reverse=True)

    return _apply_scope(merged, scope)


def _tag_scope_items(items, model_name):
    """Stamp each out_of_scope item with the model that classified it."""
    return [{**item, "source_model": model_name} for item in (items or [])]


def _apply_scope(merged, scope):
    """Hand the merged section to the run's scope rules, if there are any.

    A run with no rules object — an older captured report replayed, a caller
    that predates this argument — is left exactly as it was, minus an empty
    ``out_of_scope`` key so the report renderer and the citation collector do
    not each need their own ``or []``.
    """
    if scope is None:
        merged.setdefault("out_of_scope", [])
        return merged
    return scope.apply(merged)


def _build_flags_section(domain, results, ensemble_cfg):
    """Build a flags list from all models that ran a given domain.

    Used for voice_style and argument_integrity, which share the same schema.
    Returns a flat list tagged with source_model (and source_weight when
    multiple models contributed).
    """
    domain_results = [
        (model, r)
        for (model, d), r in results.items()
        if d == domain and not r.get("failed") and r.get("data")
    ]
    if not domain_results:
        return []

    merged = []
    multi = len(domain_results) > 1

    for model_name, r in domain_results:
        weight = _get_weight(model_name, domain, ensemble_cfg)
        for flag in r["data"].get("flags", []):
            entry = {**flag, "source_model": model_name}
            if multi:
                entry["source_weight"] = round(weight, 2)
            merged.append(entry)

    if multi:
        merged.sort(key=lambda x: x.get("source_weight", 1.0), reverse=True)

    return merged


def _build_completeness(results, ensemble_cfg):
    """Build completeness flags from all models that ran the domain.

    Same as _build_flags_section but completeness flags use passage_reference
    instead of passage, so they normalise slightly differently.
    """
    domain_results = [
        (model, r)
        for (model, d), r in results.items()
        if d == "completeness" and not r.get("failed") and r.get("data")
    ]
    if not domain_results:
        return []

    merged = []
    multi = len(domain_results) > 1

    for model_name, r in domain_results:
        weight = _get_weight(model_name, "completeness", ensemble_cfg)
        for flag in r["data"].get("flags", []):
            entry = {**flag, "source_model": model_name}
            if multi:
                entry["source_weight"] = round(weight, 2)
            merged.append(entry)

    if multi:
        merged.sort(key=lambda x: x.get("source_weight", 1.0), reverse=True)

    return merged


def _build_red_team(results, ensemble_cfg):
    """Build red team section from all models that ran the domain.

    Single source → flat dict (same structure as before, report readers work).
    Multiple sources → dict keyed by model_name (same as the old Mistral+Grok
    structure, so existing pipeline_history/ readers still work).
    """
    domain_results = [
        (model, r)
        for (model, d), r in results.items()
        if d == "red_team" and not r.get("failed") and r.get("data")
    ]
    if not domain_results:
        return {}
    if len(domain_results) == 1:
        _, r = domain_results[0]
        return r["data"]

    merged = {}
    for model_name, r in domain_results:
        weight = _get_weight(model_name, "red_team", ensemble_cfg)
        merged[model_name] = {**r["data"], "_weight": round(weight, 2)}
    return merged


# ---------------------------------------------------------------------------
# Cross-model contradiction detection
# ---------------------------------------------------------------------------


def find_contradictions(results):
    """Surface claims confirmed by one fact-check model but challenged by another.

    Returns a list of contradiction dicts.  Each entry has:
      claim           str   — the disputed claim text
      confirmed_by    list  — model names that marked it confirmed
      challenged_by   list  — model names that marked it outdated or contradicted
      challenge_type  str   — "outdated" | "contradicted" | "mixed"
    """
    confirmed: dict[str, list] = {}  # passage_key → [{model, claim, ...}]
    challenged: dict[str, list] = {}  # passage_key → [{model, claim, type, ...}]

    for (model_name, domain), result in results.items():
        if domain != "fact_check" or result.get("failed") or not result.get("data"):
            continue
        data = result["data"]
        for item in data.get("confirmed", []):
            claim = item.get("claim", "")
            if claim:
                key = _passage_key(claim)
                confirmed.setdefault(key, []).append(
                    {"model": model_name, "claim": claim, **item}
                )
        for item in data.get("contradicted", []):
            claim = item.get("claim", "")
            if claim:
                key = _passage_key(claim)
                challenged.setdefault(key, []).append(
                    {
                        "model": model_name,
                        "claim": claim,
                        "type": "contradicted",
                        **item,
                    }
                )
        for item in data.get("outdated", []):
            claim = item.get("claim", "")
            if claim:
                key = _passage_key(claim)
                challenged.setdefault(key, []).append(
                    {"model": model_name, "claim": claim, "type": "outdated", **item}
                )

    contradictions = []
    for key in set(confirmed) & set(challenged):
        c_types = {e["type"] for e in challenged[key]}
        challenge_type = c_types.pop() if len(c_types) == 1 else "mixed"
        contradictions.append(
            {
                "claim": challenged[key][0]["claim"],
                "confirmed_by": sorted({e["model"] for e in confirmed[key]}),
                "challenged_by": sorted({e["model"] for e in challenged[key]}),
                "challenge_type": challenge_type,
            }
        )

    return contradictions


# ---------------------------------------------------------------------------
# Low-confidence and additional observations collectors
# ---------------------------------------------------------------------------


def _collect_low_confidence(results):
    out = []
    for (model_name, domain), r in results.items():
        if r.get("failed") or not r.get("data"):
            continue
        for lc in r["data"].get("low_confidence", []):
            # Looser models sometimes emit bare strings instead of the
            # {passage, observation} schema — coerce so consolidation doesn't crash.
            if isinstance(lc, str):
                lc = {"passage": lc}
            elif not isinstance(lc, dict):
                continue
            out.append({**lc, "source_model": model_name, "domain": domain})
    return out


def _collect_additional_observations(results):
    """Gather cross-domain observations from all models.

    Each model's primary domain determines whether an observation is in-domain
    (rare) or out-of-domain (the typical case for additional_observations).
    """
    _domain_label = {
        "fact_check": "fact_check",
        "voice_style": "voice",
        "argument_integrity": "argument",
        "completeness": "completeness",
        "red_team": "red_team",
    }
    out = []
    for (model_name, domain), r in results.items():
        if r.get("failed") or not r.get("data"):
            continue
        primary_category = _domain_label.get(domain, domain)
        for obs in r["data"].get("additional_observations", []):
            # Tolerate bare strings / non-dict entries from looser models.
            if isinstance(obs, str):
                obs = {"observation": obs}
            elif not isinstance(obs, dict):
                continue
            obs_category = obs.get("category", "")
            out.append(
                {
                    **obs,
                    "source_model": model_name,
                    "source_domain": domain,
                    "in_domain": obs_category == primary_category,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_report(
    article_title,
    publication_name,
    run_number,
    corrected_draft,
    lt_result,
    results,  # {(model_name, domain): result_dict}
    ensemble_cfg,  # from config["ensemble"] — weights, threshold, etc.
    api_call_log,
    prior_report=None,
    primary_claim="",
    prior_report_path=None,
    fact_check_scope=None,
):
    """Merge ensemble results into a structured report.

    Parameters
    ----------
    results:
        Dict mapping (model_name, domain) tuples to adapter result dicts.
        Each result dict has at minimum: failed, data, model, tokens, elapsed_seconds.
    ensemble_cfg:
        The ``ensemble`` section from user.yaml (may be empty dict for defaults).
    fact_check_scope:
        The run's ``ScopeRules``, or None to leave the fact-check section
        untouched. See :mod:`ci_article_review.fact_check_scope`.
    """
    now = datetime.now(timezone.utc).isoformat()

    # LanguageTool flagged passages used for consensus boosting
    lt_flagged_passages = []
    if lt_result and not lt_result.get("failed"):
        for match in lt_result.get("flagged_matches", []):
            ctx = match.get("context", "")
            if ctx:
                lt_flagged_passages.append(ctx)

    # Section 1 — Weighted consensus across all domains
    consensus_flags, _single = _find_consensus(
        results, lt_flagged_passages, ensemble_cfg
    )

    # Sections 2-6 — Domain sections
    section_2_fact_check = _build_fact_check(results, ensemble_cfg, fact_check_scope)
    section_3_voice = _build_flags_section("voice_style", results, ensemble_cfg)
    section_4_argument = _build_flags_section(
        "argument_integrity", results, ensemble_cfg
    )
    section_5_completeness = _build_completeness(results, ensemble_cfg)
    section_6_red_team = _build_red_team(results, ensemble_cfg)

    # Section 7 — Low-confidence observations
    section_7_low_confidence = _collect_low_confidence(results)

    # Section 8 — Cross-domain additional observations
    section_8_additional = _collect_additional_observations(results)

    # Cross-model contradictions — claims confirmed by one model, challenged by another
    contradictions = find_contradictions(results)

    # Model failures (skipped entries are not failures)
    model_failures = [
        f"{model}:{domain}"
        for (model, domain), r in results.items()
        if r.get("failed") and not r.get("skipped")
    ]
    # …and what actually happened, plus which section is short a model as a
    # result. The bare "openai:fact_check" in the header said a pass failed but
    # not why, and nothing downstream said that Section 2 was consequently built
    # from four models instead of five — which is what changes how its consensus
    # counts should be read.
    model_failure_details = [
        {
            "pass": f"{model}:{domain}",
            "model": r.get("model") or model,
            "domain": domain,
            "section": _DOMAIN_SECTIONS.get(domain),
            "error": r.get("error") or "no error recorded",
            "elapsed_seconds": r.get("elapsed_seconds"),
        }
        for (model, domain), r in results.items()
        if r.get("failed") and not r.get("skipped")
    ]

    # Calls that succeeded but had to be salvaged from a truncated response —
    # some findings were recovered, but some were genuinely lost. Not a failure
    # (its findings are already merged into the sections above), but distinct
    # enough from a clean call that it needs to stay visible in the report.
    truncated_results = [
        f"{model}:{domain}"
        for (model, domain), r in results.items()
        if r.get("truncated")
    ]

    # Delta from prior run
    delta = (
        _compute_delta(
            corrected_draft,
            prior_report,
            consensus_flags,
            primary_claim,
            prior_report_path=prior_report_path,
        )
        if prior_report
        else None
    )

    # Ensemble metadata for the report header
    assignments = sorted(f"{m}:{d}" for (m, d) in results)

    return {
        "generated": now,
        "run_number": run_number,
        "article_title": article_title,
        "publication": publication_name,
        "lt_corrections_applied": lt_result.get("change_log", []) if lt_result else [],
        "lt_failed": lt_result.get("failed", False) if lt_result else True,
        "lt_skipped": lt_result.get("skipped", False) if lt_result else False,
        # "disabled" (grammar_pass: false) or "no_credentials" — the summary
        # named the wrong one for either, sending operators to configure
        # credentials they already had.
        "lt_skipped_reason": lt_result.get("skipped_reason") if lt_result else None,
        "corrected_draft": corrected_draft,
        "primary_claim": primary_claim,
        "api_call_log": api_call_log,
        "delta": delta,
        "section_1_consensus": consensus_flags,
        "section_2_fact_check": section_2_fact_check,
        "section_3_voice": section_3_voice,
        "section_4_argument": section_4_argument,
        "section_5_completeness": section_5_completeness,
        "section_6_red_team": section_6_red_team,
        "section_7_low_confidence": section_7_low_confidence,
        "section_8_additional": section_8_additional,
        "contradictions": contradictions,
        "model_failures": model_failures,
        "model_failure_details": model_failure_details,
        "truncated_results": truncated_results,
        "ensemble": {
            "thoroughness": ensemble_cfg.get("thoroughness", "standard"),
            "consensus_threshold": float(
                ensemble_cfg.get("consensus_threshold", _DEFAULT_CONSENSUS_THRESHOLD)
            ),
            "assignments": assignments,
        },
    }


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------


def _heading_structure(text):
    """Ordered list of (level, normalized_text) for every markdown heading.

    Used to detect structural edits between runs — added/removed/renamed/reordered
    headings — independent of body wording.
    """
    headings = []
    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.MULTILINE):
        headings.append((len(m.group(1)), " ".join(m.group(2).split()).lower()))
    return headings


def _normalize_claim(claim):
    return " ".join((claim or "").split()).strip().lower()


def _compute_delta(
    current_draft,
    prior_report,
    current_consensus,
    current_claim="",
    prior_report_path=None,
):
    import difflib

    if not prior_report:
        return None

    prior_draft = prior_report.get("corrected_draft", "")
    prior_words = prior_draft.split()
    current_words = current_draft.split()

    if prior_words:
        diff = difflib.SequenceMatcher(None, prior_words, current_words)
        changed_words = sum(
            max(b2 - b1, d2 - d1)
            for tag, b1, b2, d1, d2 in diff.get_opcodes()
            if tag != "equal"
        )
        word_change_pct = round(changed_words / max(len(prior_words), 1) * 100, 1)
    else:
        word_change_pct = 100.0

    prior_passages = {
        _passage_key(f.get("passage", ""))
        for f in prior_report.get("section_1_consensus", [])
    }
    current_passages = {_passage_key(f.get("passage", "")) for f in current_consensus}

    # Claim change: only flag when both runs supplied a claim to compare. Older
    # reports predating claim storage have no primary_claim — treat as unchanged
    # rather than triggering a spurious rerun.
    prior_claim = _normalize_claim(prior_report.get("primary_claim", ""))
    curr_claim = _normalize_claim(current_claim)
    claim_changed = bool(prior_claim and curr_claim and prior_claim != curr_claim)

    # Structure change: heading outline differs (added/removed/renamed/reordered).
    structure_changed = _heading_structure(prior_draft) != _heading_structure(
        current_draft
    )

    # Which execution this delta was measured against. Run numbers are
    # author-declared and repeat across re-runs of the same handoff, so the
    # numbers alone don't identify what was compared — record the file.
    compared_against = {
        "report": Path(prior_report_path).name if prior_report_path else None,
        "run_number": prior_report.get("run_number"),
        "generated": prior_report.get("generated"),
    }

    return {
        "word_change_pct": word_change_pct,
        "compared_against": compared_against,
        "prior_consensus_count": len(prior_passages),
        "current_consensus_count": len(current_passages),
        "resolved_consensus_count": len(prior_passages - current_passages),
        "new_consensus_count": len(current_passages - prior_passages),
        "claim_changed": claim_changed,
        "structure_changed": structure_changed,
    }


def rerun_recommended(delta, delta_config):
    if not delta:
        return False
    threshold = delta_config.get("word_change_threshold_pct", 15)
    if delta["word_change_pct"] > threshold:
        return True
    if delta["new_consensus_count"] > 0:
        return True
    # Honor the configurable triggers (default True). delta.get(...) guards older
    # delta dicts that predate these keys.
    if delta_config.get("claim_change_triggers_rerun", True) and delta.get(
        "claim_changed"
    ):
        return True
    if delta_config.get("structure_change_triggers_rerun", True) and delta.get(
        "structure_changed"
    ):
        return True
    return False
