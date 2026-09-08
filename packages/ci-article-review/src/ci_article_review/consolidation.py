"""
Merge ensemble model responses into a structured review report.

The pipeline runs each configured model against each assigned prompt domain,
producing a dict of results keyed by (model_name, domain).  This module merges
those results into a single report with eight sections, plus an opt-in tenth
(``--expand``) that proposes material rather than judging it.

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
  gemini      1.0         1.0          1.0           1.0       1.0
  perplexity  1.0         1.0          1.0           1.0       1.0
  openai      1.0         1.2          1.2           1.0       1.0
  mistral     1.0         1.0          1.0           1.2       1.1
  grok        1.0         1.0          1.0           1.0       1.2
  claude      1.0         1.1          1.1           1.3       1.0

  Grounding bonus: a fact-check call that actually consulted live sources has
  its weight multiplied by 1.5 (``ensemble.grounding_bonus``), because a claim
  checked against a retrieved document is worth more than one recalled. This is
  read from the call's result, not from the model's name — the table above used
  to carry a flat 1.5 for gemini and perplexity, which was wrong in both
  directions the moment configuration changed. See ``_DEFAULT_GROUNDING_BONUS``.

The opt-in ``expansion`` domain carries no weights of its own. It was given
some — perplexity 1.3, gemini 1.2, grok 1.1, on the reasoning that a model which
cannot fetch is guessing at URLs — and they were dropped on merging with the
2026-09-03 audit, which deleted the static grounding bonus for exactly that
reasoning: it guessed which models ground instead of observing whether they did,
and was wrong in both directions. Section 10 weights would only have ordered the
list; nothing there is dropped or promoted for how many models proposed it, so
ordering falls back to encounter order. If it ever matters, the runtime
grounding multiplier is the mechanism to reuse, not a new table of guesses.

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

from .passage_match import group_passages, normalise, same_passage
from ci_core.llm import watermarking

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

#: Built-in domain weights.  Configurable under ``ensemble.weights`` in user.yaml.
#: The ``default`` key applies to all domains not explicitly listed.
_DEFAULT_WEIGHTS = {
    "gemini": {"default": 1.0},
    "perplexity": {"default": 1.0},
    "openai": {"default": 1.0, "voice_style": 1.2, "completeness": 1.2},
    "mistral": {"default": 1.0, "argument_integrity": 1.2, "red_team": 1.1},
    "grok": {"default": 1.0, "red_team": 1.2},
    "claude": {"default": 1.0, "argument_integrity": 1.3, "voice_style": 1.1},
}

#: Multiplier applied to a fact-check weight when the call actually consulted
#: live sources.
#:
#: This used to be baked into the table above as a flat 1.5 for gemini and
#: perplexity — a guess about which models ground, standing in for whether they
#: did. On 2026-09-03 the guess was wrong in both directions at once: gemini
#: took the bonus while reporting ``grounding_available: False`` (it has no
#: ``web_search`` configured at all), and openai ran a real search — 84,634
#: prompt tokens of it — on the flat 1.0. Reading the result instead of the
#: model name makes the bonus self-correcting when configuration changes, which
#: is the only way it stays true.
_DEFAULT_GROUNDING_BONUS = 1.5

#: Weighted sum required to call a passage consensus.
_DEFAULT_CONSENSUS_THRESHOLD = 2.0

#: Distinct models that must independently flag a passage before it can be
#: called consensus, regardless of what the weights add up to.
#:
#: Weight alone was never sufficient. A single model emitting two red_team
#: sub-findings on one passage (``most_vulnerable_claim`` and
#: ``highest_credibility_risk``) contributed 1.1 + 1.1 = 2.2 and cleared the
#: 2.0 threshold on its own — one model agreeing with itself, published in the
#: section whose entire meaning is that several models agreed. Observed
#: 2026-09-03 as item 14 of 15. LanguageTool counts as one of these, since it
#: is a genuinely independent source.
_DEFAULT_CONSENSUS_MIN_MODELS = 2

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
    "expansion": "SECTION 10: Expansion Candidates",
}


def _get_weight(model_name, domain, ensemble_cfg, grounded=False):
    """Return effective weight for a (model, domain) pair.

    Checks user-configured weights first, then falls back to built-in defaults.

    ``grounded`` says whether *this particular call* consulted live sources, and
    earns a fact-check multiplier when it did. It is a separate axis from the
    configured weight and multiplies whatever that resolves to, so a user who
    tunes a model's fact-check weight still gets the bonus on top rather than
    silently losing it.
    """
    user_weights = ensemble_cfg.get("weights", {})
    model_weights = user_weights.get(model_name, {})

    # User-configured domain-specific weight
    if domain in model_weights:
        weight = float(model_weights[domain])
    # User-configured model default
    elif "default" in model_weights:
        weight = float(model_weights["default"])
    else:
        # Built-in default
        defaults = _DEFAULT_WEIGHTS.get(model_name, {"default": 1.0})
        weight = float(defaults.get(domain, defaults.get("default", 1.0)))

    if grounded and domain == "fact_check":
        weight *= float(ensemble_cfg.get("grounding_bonus", _DEFAULT_GROUNDING_BONUS))
    return weight


# ---------------------------------------------------------------------------
# Passage normalisation
# ---------------------------------------------------------------------------


def _passage_key(passage):
    """Normalise a passage for exact comparison between runs.

    Cross-model matching no longer goes through this — see
    :mod:`ci_article_review.passage_match`, which handles the nesting this could
    not. What is left is the run-over-run delta, where two reports are checked
    for the same consensus flag surviving a revision, and exact normalised text
    is the conservative test to apply.

    The former ``[:250]`` truncation is gone. It merged any two passages sharing
    a 250-character prefix into one key, and three of the fifteen consensus
    passages in the 2026-09-03 run were longer than that.
    """
    return normalise(passage)


# ---------------------------------------------------------------------------
# Passage extraction per domain schema
# ---------------------------------------------------------------------------


def _extract_passages(model_name, domain, result):
    """Return list of (passage_text, flag_data_dict) pairs for consensus detection.

    Each domain has a different JSON schema:
      fact_check         — outdated[], contradicted[], unverifiable[],
                           primary_source_needed[], all by .claim
      voice_style        — flags[].passage
      argument_integrity — flags[].passage
      completeness       — flags[].passage_reference
      red_team           — most_vulnerable_claim.passage etc.

    Why fact_check reads four buckets and not two
    ---------------------------------------------
    It used to read ``outdated`` and ``contradicted`` only. Those are the two
    buckets models almost never populate: in the 2026-09-03 maximum-preset run
    both were empty across all six fact_check passes, while ``unverifiable``
    held 50 claims and ``primary_source_needed`` 14. The result was that the
    single most expensive domain in the run — $1.36 of $3.49, 39% of spend —
    contributed nothing whatsoever to Section 1, and no two models could ever
    be seen agreeing that a claim was unsourceable.

    This is structural rather than particular to that draft: ``unverifiable`` is
    the natural bucket for any claim a model cannot source, so it is the common
    case on every run.

    ``confirmed`` is deliberately still excluded. Section 1 is a list of things
    to fix; a claim that checked out is not one, and feeding it here would put
    non-problems in the section that drives the revision prompt.
    """
    if result.get("failed") or not result.get("data"):
        return []

    # expansion proposes material the draft does not contain, so it has no
    # passage to key on and no business in Section 1. The deeper reason is that
    # consensus is the wrong test for it: agreement between two models on a
    # defect is corroboration, but agreement on a suggestion is just the two of
    # them reaching for the same obvious source. The proposal only one model
    # made is frequently the one worth having, and a threshold would bury it.
    # _build_expansion unions instead. Explicit rather than relying on the
    # if/elif below falling through, which would silently absorb the next
    # domain anyone adds.
    if domain == "expansion":
        return []

    data = result["data"]
    out = []

    if domain == "fact_check":
        for bucket in (
            "outdated",
            "contradicted",
            "unverifiable",
            "primary_source_needed",
        ):
            for item in data.get(bucket, []):
                claim = item.get("claim", "")
                if claim:
                    out.append(
                        (
                            claim,
                            {
                                **item,
                                "domain": domain,
                                "type": bucket,
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

    Returns ``(consensus_flags, single_source_flags)``. ``consensus_flags`` are
    sorted by weight_sum descending so the strongest findings appear first.

    ``single_source_flags`` — everything that did not clear the bar — is
    deliberately **not** consumed by :func:`build_report`, and the phrasing here
    used to imply otherwise. Nothing is lost by dropping it: sections 2-6 are
    built from the raw results and already carry every flag whatever its weight,
    so a sub-threshold finding still appears under its own domain. It is
    returned because it makes the threshold directly testable — a test can
    assert that a given flag fell short rather than inferring it from an
    absence.
    """
    threshold = float(
        ensemble_cfg.get("consensus_threshold", _DEFAULT_CONSENSUS_THRESHOLD)
    )
    lt_weight = float(ensemble_cfg.get("lt_weight", _DEFAULT_LT_WEIGHT))
    min_models = int(
        ensemble_cfg.get("consensus_min_models", _DEFAULT_CONSENSUS_MIN_MODELS)
    )

    confidence_multipliers = _confidence_multipliers(ensemble_cfg)

    entries = []
    for (model_name, domain), result in results.items():
        weight = _get_weight(
            model_name,
            domain,
            ensemble_cfg,
            grounded=bool(result.get("grounding_available")),
        )
        for passage, flag_data in _extract_passages(model_name, domain, result):
            entries.append(
                {
                    "passage": passage,
                    "flag": flag_data,
                    "model": model_name,
                    "source": f"{model_name}:{domain}",
                    "weight": weight
                    * _confidence_multiplier(flag_data, confidence_multipliers),
                }
            )

    consensus = []
    single_source = []

    # Group by *place in the draft*, not by exact quoted string. Models quote
    # the same sentence at different lengths, and keying on the string scattered
    # one passage's votes across several buckets — which both inflated the
    # section (7 of 15 items on 2026-09-03 were nested inside another item) and
    # distorted its ranking, since it is sorted by weight_sum and the votes that
    # should have summed did not.
    for passage, group in group_passages(entries, lambda e: e["passage"]):
        weight_sum = sum(e["weight"] for e in group)
        has_lt = any(same_passage(passage, p) for p in lt_flagged_passages)
        effective_weight = weight_sum + (lt_weight if has_lt else 0.0)

        # LanguageTool counts toward the distinct-source requirement: it is an
        # independent opinion, which is the property being tested for.
        voters = {e["model"] for e in group}
        if has_lt:
            voters = voters | {"languagetool"}

        if effective_weight >= threshold and len(voters) >= min_models:
            consensus.append(
                {
                    "passage": passage,
                    "models": sorted({e["source"] for e in group}),
                    "weight_sum": round(effective_weight, 2),
                    "languagetool_also_flagged": has_lt,
                    "flags": [e["flag"] for e in group],
                }
            )
        else:
            single_source.extend(e["flag"] for e in group)

    consensus.sort(key=lambda x: x["weight_sum"], reverse=True)
    return consensus, single_source


#: The fact-check lists whose items carry per-claim source attribution.
#: `additional_observations` is deliberately absent: it is tagged separately by
#: `_collect_additional_observations`, which sets `source_domain` too.
_FACT_CHECK_ITEM_KEYS = (
    "confirmed",
    "outdated",
    "contradicted",
    "unverifiable",
    "primary_source_needed",
    # Not a verdict, but tagged like one for the same reason: which model made
    # a scope call is a fact about that model's judgment, and the report names
    # it. See :mod:`ci_article_review.fact_check_scope`.
    "out_of_scope",
)


#: Every list in a fact_check payload, and the field that carries the item's
#: text. Used by :func:`_coerce_fact_check_buckets` to know what a bare string
#: found where an object belongs should be read *as*.
#:
#: `additional_observations` is here although it is absent from
#: `_FACT_CHECK_ITEM_KEYS` above: it is tagged differently, but
#: `_build_fact_check` spreads it with `{**obs}` in the same loop and dies on a
#: malformed one exactly as the verdict buckets do. Its text field is
#: `observation`, which is what `_collect_additional_observations` has always
#: coerced a bare string into.
_FACT_CHECK_BUCKET_FIELDS = {
    **{key: "claim" for key in _FACT_CHECK_ITEM_KEYS},
    "additional_observations": "observation",
}


#: Source strings that name no document — the draft itself, or the model's own
#: reasoning. Compared against the whole source (normalised), not searched
#: within it, so "Manual Calculation" fails and "Furuno GT-8031 calculation
#: notes" does not.
#:
#: Measured 2026-09-05 on the Honda draft: 4 of 19 `confirmed` findings cited no
#: document at all. One named "Draft Article" — the pipeline confirming the
#: draft against itself — and three named "Manual Calculation", where the model
#: did the arithmetic and reported the result as a confirmed fact. All four had
#: `source_url: "N/A"`.
#:
#: The arithmetic may well be right; that is not the point. `confirmed` is the
#: strongest thing Section 2 says and the tier Section 9 counts as backed by a
#: document. A model reasoning its way to a conclusion is what the other buckets
#: already exist to represent.
_SELF_REFERENTIAL_SOURCES = frozenset(
    {
        "draft",
        "the draft",
        "draft article",
        "the draft article",
        "article",
        "the article",
        "this article",
        "manual calculation",
        "calculation",
        "own calculation",
        "computed",
        "derived",
        "inference",
        "deduction",
        "own analysis",
        "author",
        "the author",
        "internal consistency",
        "common knowledge",
        "n/a",
        "na",
        "none",
        "unknown",
    }
)

#: A URL field the model filled in to mean "there isn't one".
_EMPTY_URL_VALUES = frozenset({"", "n/a", "na", "none", "null", "-"})


def _normalise_source(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    kept = [c if (c.isalnum() or c in " /") else " " for c in str(text or "").lower()]
    return " ".join("".join(kept).split())


def _names_a_document(part):
    key = _normalise_source(part)
    return bool(key) and key not in _SELF_REFERENTIAL_SOURCES


def _has_external_source(item):
    """Whether a `confirmed` finding points at anything outside the draft.

    A URL settles it. Without one, the free-text source has to name something
    that is not the draft or the model's own reasoning — an unlinked "Honda
    ServiceNews B18010I" is a real document and stays confirmed. Models list
    several sources separated by semicolons, and one real document among them
    is enough.
    """
    url = str(item.get("source_url", "") or "").strip().lower()
    if url and url not in _EMPTY_URL_VALUES:
        return True
    return any(
        _names_a_document(part) for part in str(item.get("source", "")).split(";")
    )


def _demote_unsourced_confirmations(data):
    """Move `confirmed` findings with no external source into `unverifiable`.

    Returns a new data dict; the input is left alone. The claim is not dropped —
    it moves to the bucket that means "nothing was found to check this against",
    which is what actually happened, and keeps its original source text so a
    reader can see what the model offered instead.
    """
    confirmed = data.get("confirmed") or []
    if not confirmed:
        return data

    kept, demoted = [], []
    for item in confirmed:
        if _has_external_source(item):
            kept.append(item)
            continue
        demoted.append(
            {
                "claim": item.get("claim", ""),
                "checked": item.get("source", "") or "nothing external",
                "sources_checked": [],
                "reason": (
                    "Reported as confirmed with no external source: "
                    f"{item.get('source') or 'none given'}. A claim the model "
                    "reasoned its way to is not a claim a document backs, so it "
                    "is reported here rather than as confirmed."
                ),
            }
        )
    if not demoted:
        return data

    log.info(
        "Fact check: %d confirmed finding(s) cited no external source and were "
        "moved to unverifiable.",
        len(demoted),
    )
    return {
        **data,
        "confirmed": kept,
        "unverifiable": list(data.get("unverifiable") or []) + demoted,
    }


# ---------------------------------------------------------------------------
# Malformed fact-check buckets
# ---------------------------------------------------------------------------


def _coerce_fact_check_buckets(data, model_name):
    """Force every fact-check bucket in ``data`` to a list of dicts.

    Returns ``(data, notes)``. ``data`` is the same object, not a copy, when
    nothing needed coercing — the overwhelming case. ``notes`` describes what
    could not be read, one entry per bucket touched.

    Why this exists
    ---------------
    Consolidation reads these buckets five times over — :func:`_extract_passages`
    for Section 1, :func:`_build_fact_check` for Section 2, then
    :func:`find_contradictions`, :meth:`ScopeRules.apply
    <ci_article_review.fact_check_scope.ScopeRules.apply>` and the citation
    collector downstream of it — and every one of them assumes a list of dicts
    and calls ``item.get(...)`` or ``{**item}`` without checking. A bucket that
    arrives as a dict iterates to its *keys*; one that arrives as a string
    iterates to its *characters*. Either way the first reader raises
    ``AttributeError``/``TypeError`` and report building dies with the whole
    ensemble already paid for.

    :data:`ci_article_review.schemas.FACT_CHECK` makes that unreachable for
    every provider that enforces a schema. Gemini while grounded is the
    exception — it 400s on schema-plus-search and so runs prompt-only on
    ``fact_check`` (see the ``schemas`` module docstring) — which leaves one
    live pass per run asking a model nicely for a shape and hoping.

    What is kept, and what is not
    -----------------------------
    Dropping a malformed bucket silently would lose findings the run paid for;
    raising would lose the whole report. So: keep every item that *is* readable,
    and hand the caller a note, so the report can say which section is short and
    why rather than the count simply being lower.

    A bare string **inside a list** becomes ``{"claim": ...}``. The model put N
    elements in an array, so element k is one finding, and this is the same
    coercion :func:`_collect_low_confidence` and
    :func:`_collect_additional_observations` have always applied to their own
    buckets.

    A bare string **as the bucket** is dropped, not wrapped. Nothing asserts
    that ``"unverifiable": "none"`` is a finding, and inventing a claim out of
    it would put a fabricated row in the section whose whole job is accuracy.
    Losing a real finding is bad; printing one nobody made is worse.

    A dict **as the bucket** is one finding returned unwrapped — a common enough
    slip where nothing enforces the array — but only when it carries a
    ``claim``. Every bucket in :data:`ci_article_review.schemas.FACT_CHECK`
    requires that field and every reader keys on it, so a dict without one is
    not a finding in any useful sense and is dropped with the rest.
    """
    if not isinstance(data, dict):
        # Not a bucket problem, but the same cause and the same crash: the
        # payload has to be a mapping before any bucket can be looked up at all.
        return {}, [
            {
                "model": model_name,
                "bucket": None,
                "field": "claim",
                "found": type(data).__name__,
                "dropped": 0,
                "repaired": 0,
            }
        ]

    notes, replacements = [], {}
    for bucket, text_field in _FACT_CHECK_BUCKET_FIELDS.items():
        if bucket not in data:
            continue  # Absent: every reader defaults it to [].
        value = data[bucket]
        if isinstance(value, list) and all(isinstance(i, dict) for i in value):
            continue  # The shape the schema promises.
        if value is None:
            # A present-but-null bucket still has to be rewritten, even though
            # nothing was lost and so nothing is recorded below. Half the
            # readers spell this ``data.get(bucket) or []`` and half spell it
            # ``data.get(bucket, [])``, and the second kind never reaches its
            # default for a key that is present: ``find_contradictions`` and
            # ``_extract_passages`` both raise on ``"confirmed": null``.
            replacements[bucket] = []
            continue

        dropped = repaired = 0
        if isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict):
                    items.append(item)
                elif isinstance(item, str) and item.strip():
                    items.append({text_field: item.strip()})
                    repaired += 1
                else:
                    dropped += 1
        elif isinstance(value, dict) and str(value.get(text_field, "") or "").strip():
            items, repaired = [value], 1
        else:
            items, dropped = [], 1

        replacements[bucket] = items
        notes.append(
            {
                "model": model_name,
                "bucket": bucket,
                "field": text_field,
                "found": type(value).__name__,
                "dropped": dropped,
                "repaired": repaired,
            }
        )

    if not replacements:
        return data, notes
    return {**data, **replacements}, notes


def _describe_malformed_bucket(note):
    """One reader-facing phrase for a single :func:`_coerce_fact_check_buckets` note.

    Names the provider and the bucket in every case: "the fact-check section is
    short" is not actionable, and "gemini.unverifiable was a str rather than a
    list" says which pass to re-run and which bucket to distrust.
    """
    if note["bucket"] is None:
        return f"{note['model']} returned a {note['found']}, not an object"

    where = f"{note['model']}.{note['bucket']}"
    if note["found"] != "list":
        kept = f", {note['repaired']} finding recovered" if note["repaired"] else ""
        return f"{where} was a {note['found']} rather than a list{kept}"

    parts = []
    if note["dropped"]:
        parts.append(f"{note['dropped']} unreadable item(s) dropped")
    if note["repaired"]:
        parts.append(f"{note['repaired']} bare string(s) read as {note['field']}s")
    return f"{where}: {', '.join(parts)}"


def _normalise_fact_check_results(results):
    """Make every fact-check payload safe to consolidate, before anything reads it.

    Returns ``(results, degradations)``. ``results`` is the same object when
    nothing needed coercing; otherwise a shallow copy with only the offending
    entries replaced, so the caller's dict — and the captured ensemble a
    ``--replay`` reads off disk — are left alone.

    Called once at the top of :func:`build_report` rather than inside each
    reader, because there are five of them and the first to run is
    :func:`_extract_passages`: fixing only :func:`_build_fact_check` would move
    the crash into Section 1 rather than remove it.
    """
    notes, cleaned = [], {}
    for key, result in results.items():
        model_name, domain = key
        if domain != "fact_check" or result.get("failed") or not result.get("data"):
            continue
        data, result_notes = _coerce_fact_check_buckets(result["data"], model_name)
        notes.extend(result_notes)
        # Identity, not ``result_notes``: a present-but-null bucket is rewritten
        # and deliberately not reported, and gating the replacement on the notes
        # would drop exactly that repair on the floor.
        if data is not result["data"]:
            cleaned[key] = {**result, "data": data}

    if not cleaned:
        return results, []
    results = {**results, **cleaned}
    if not notes:
        return results, []  # Nulls rewritten; nothing lost, so nothing said.

    detail = (
        f"{_affected_sections(notes)} short findings this run paid for: "
        f"{len(notes)} fact-check bucket(s) came back in a shape consolidation "
        f"cannot read ({'; '.join(_describe_malformed_bucket(n) for n in notes)}"
        f"). Whatever was readable was kept; the rest is missing from the "
        f"report entirely and never reached citation resolution. Only a pass "
        f"running without schema enforcement can return this, so a re-run may "
        f"well come back clean — treat the counts below as a floor."
    )
    log.warning("Fact check: %s", detail)
    return results, [
        {
            "section": ", ".join(_affected_section_titles(notes)),
            "caused_by": sorted({f"{n['model']}:fact_check" for n in notes}),
            "detail": detail,
        }
    ]


#: Where a dropped item would have shown up, by the bucket it was dropped from.
#: The verdict buckets feed Section 2 and, through it, citation resolution; four
#: of the five also vote in Section 1. ``additional_observations`` feeds neither
#: and has a section of its own.
_BUCKET_SECTIONS = {
    "additional_observations": ("SECTION 8: Additional Observations",),
    None: (
        "SECTION 1: Consensus",
        "SECTION 2: Factual Verification",
        "SECTION 9: Citations",
    ),
}


def _affected_section_titles(notes):
    """The report sections these notes cost something, in section order."""
    titles = set()
    for note in notes:
        titles.update(_BUCKET_SECTIONS.get(note["bucket"], _BUCKET_SECTIONS[None]))
    return sorted(titles)


def _affected_sections(notes):
    """ "Sections 1, 2 and 9 are" / "Section 8 is" — the sentence opener."""
    numbers = [t.split(":")[0].split()[1] for t in _affected_section_titles(notes)]
    if len(numbers) == 1:
        return f"Section {numbers[0]} is"
    return f"Sections {', '.join(numbers[:-1])} and {numbers[-1]} are"


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
        (model, {**r, "data": _demote_unsourced_confirmations(r["data"])})
        for (model, d), r in results.items()
        if d == "fact_check" and not r.get("failed") and r.get("data")
    ]
    if not domain_results:
        return {}
    if len(domain_results) == 1:
        model_name, r = domain_results[0]
        grounding = bool(r.get("grounding_available"))
        weight = _get_weight(model_name, "fact_check", ensemble_cfg, grounded=grounding)
        tag = {
            "source_model": model_name,
            "source_weight": round(weight, 2),
            "grounding": grounding,
        }
        # Individual items are tagged here exactly as the merge branch below
        # tags them. This used to tag only `_sources`, so which model asserted a
        # given claim was recorded when several models ran the domain and lost
        # when one did — and `standard` thoroughness runs one. Anything keyed on
        # the asserting model was therefore dead on precisely the cheaper preset:
        # the citation re-ask had nobody to hand a refutation back to.
        merged = {
            **r["data"],
            **{
                key: [{**item, **tag} for item in (r["data"].get(key) or [])]
                for key in _FACT_CHECK_ITEM_KEYS
            },
            "_sources": {
                model_name: {"weight": round(weight, 2), "grounding": grounding}
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
        grounding = bool(r.get("grounding_available"))
        weight = _get_weight(model_name, "fact_check", ensemble_cfg, grounded=grounding)
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
        for key in _FACT_CHECK_ITEM_KEYS:
            for item in data.get(key, []) or []:
                merged[key].append({**item, **tag})
        for obs in data.get("additional_observations", []):
            merged["additional_observations"].append(
                {**obs, "source_model": model_name}
            )

    # Sort problem arrays: higher-weight model findings first.
    #
    # This covered `outdated` and `contradicted` only — the two buckets models
    # almost never fill. Both were empty across all six fact-check passes on
    # 2026-09-03, so the sort did nothing at all, while the 50-item
    # `unverifiable` list and the 14-item `primary_source_needed` list kept
    # arbitrary model-iteration order. Those are the arrays a reader actually
    # works through, and a grounded pass carries a 1.5x fact-check weight
    # precisely so its findings lead.
    #
    # `confirmed` is sorted too: it is not a problem list, but a reader deciding
    # how much to trust a verdict benefits from the same ordering.
    for key in (
        "outdated",
        "contradicted",
        "unverifiable",
        "primary_source_needed",
        "confirmed",
    ):
        merged[key].sort(key=lambda x: x.get("source_weight", 1.0), reverse=True)

    return _apply_scope(merged, scope)


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

    A thin alias for :func:`_build_flags_section`. It used to be a full copy of
    it, justified by a docstring saying completeness "flags use
    passage_reference instead of passage, so they normalise slightly
    differently" — but neither function ever touched either field. Both copy the
    flag dict through whole; the field only matters to ``_extract_passages``,
    which is a different function. The stated difference did not exist in the
    code, so the second copy was pure drift risk.
    """
    return _build_flags_section("completeness", results, ensemble_cfg)


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


#: Which field identifies a proposal, per bucket, in preference order. Two
#: models proposing the same EPA dataset under different titles are one
#: candidate if the URL matches; two proposing the same topic in different
#: words are matched on the normalised text and usually will not merge. That
#: asymmetry is deliberate — a missed merge costs a duplicate line, and a wrong
#: merge silently destroys one of the two proposals.
#:
#: **Do not "fix" this with fuzzy matching.** It was measured against two live
#: runs, using the Jaccard content-word overlap the citation pass already uses
#: (``pipeline._claim_key``). Within a single-topic article every proposal
#: shares most of its vocabulary, so the score tracks the subject rather than
#: the meaning:
#:
#:   0.42  "British Sleep Society position statement on DST"
#:         "Daylight Saving Time: An AMA Position Statement"    <- different docs
#:   0.16  "cite parallel position statements from British and European
#:          sleep societies"
#:         "broad scientific consensus across multiple medical and sleep
#:          organizations"                                       <- same idea
#:
#: The genuinely duplicated pair scores *lower* than a dozen unrelated ones. No
#: threshold separates them, and every threshold that catches the 0.16 merges
#: distinct sources wholesale. A URL is the only identity available here that
#: means anything, which is why it is the only one used.
_EXPANSION_BUCKETS = {
    "sources": ("url", "title"),
    "topics": ("topic",),
    "angles": ("angle",),
    "data_points": ("data_point",),
}


#: Query parameters that identify a referrer rather than a document. Stripping
#: these is safe in the direction that matters: two URLs differing only by a
#: campaign tag are the same page. Every *other* parameter is left alone,
#: because `?id=42` is identity, not decoration, and dropping it would merge two
#: different documents.
_TRACKING_PARAMS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
)


def url_key(url):
    """Normalise a URL enough to match the same page proposed twice.

    Public because the pipeline needs the identical notion of "same page" in
    three places — merging proposals here, deciding whether a redirect landed
    somewhere new, and spotting a data point that only re-points at a source
    already proposed. Three private copies of this drifted apart once already
    within a single change.

    **Every rule here errs toward *not* merging.** A missed merge costs one
    duplicate line in a menu the author is skimming; a wrong merge silently
    deletes somebody's proposal. So:

    - Only the **host** is lowercased. Paths are case-sensitive on essentially
      every server that is not Windows, so ``/Report.pdf`` and ``/report.pdf``
      may be two documents. Lowercasing the whole URL — which this did until it
      was measured — merges them, which is the destructive direction.
    - The **fragment is dropped**. It is never sent to the server, so it cannot
      identify a different document; ``#section-3`` is an anchor within the same
      page.
    - Only **known tracking parameters** are dropped. A general "strip the query
      string" rule would collapse ``?id=42`` and ``?id=43``.

    Not attempted: recognising that a DOI and a publisher URL are the same
    paper, or a PubMed ID and a DOI. No syntactic rule finds those, and across
    three live runs no two models ever proposed the same source at all — so the
    machinery would be for a case that has not occurred.

    **Why not w3lib.** ``w3lib.url.canonicalize_url`` was compared against this
    on nine cases before this was kept. It is not a superset: it answers a
    different question. It produces an RFC-correct canonical *URI*, so it
    correctly keeps ``http://www.example.org/doc/`` distinct from
    ``https://example.org/doc`` and keeps tracking parameters (sorted, not
    dropped). Those two equivalences are the entire reason this function exists,
    so adopting it would break the cases it is for. What it did better —
    percent-encoding case and query-argument order — is folded in below, which
    costs three lines against a new dependency in a workspace that deliberately
    keeps its install small. Dot-segment resolution (``/a/../b``) is handled by
    neither and left alone: a model proposing a relative traversal inside an
    absolute source URL has not happened and would be strange.
    """
    key = (url or "").strip()
    key = re.sub(r"^https?://", "", key, flags=re.IGNORECASE)
    key = key.split("#", 1)[0]

    # Split host from the rest before case-folding, so only the host folds.
    host, slash, rest = key.partition("/")
    host = re.sub(r"^www\.", "", host.lower(), flags=re.IGNORECASE)

    if "?" in rest:
        path, _, query = rest.partition("?")
        kept = [
            pair
            for pair in query.split("&")
            if pair and pair.split("=", 1)[0].lower() not in _TRACKING_PARAMS
        ]
        # Sorted, because argument order is not identity: ?a=1&b=2 and ?b=2&a=1
        # are one page. Borrowed from w3lib's canonicalize_url rather than
        # depending on it — see the docstring.
        rest = path + ("?" + "&".join(sorted(kept)) if kept else "")

    # Percent-encoding is case-insensitive in the escape digits, so %2f and %2F
    # are the same octet. Also from w3lib.
    key = host + slash + rest
    key = re.sub(r"%([0-9a-fA-F]{2})", lambda m: "%" + m.group(1).upper(), key)
    return key.rstrip("/")


def _expansion_key(bucket, item):
    """Identity for a proposal, or "" when it carries nothing to key on."""
    for field in _EXPANSION_BUCKETS[bucket]:
        value = item.get(field)
        if value:
            return url_key(value) if field == "url" else _passage_key(str(value))
    return ""


def _build_expansion(results, ensemble_cfg):
    """Union every model's proposals, bucket by bucket.

    This is the one section built by union rather than by scoring, and the
    difference is the point of the domain. Sections 1-6 exist to rank defects,
    where two models agreeing is corroboration worth surfacing first. Nothing
    of the sort is true of a suggestion: two models proposing the same source
    usually means it was the obvious one, and the proposal a single model made
    is regularly the one that justified the pass. So nothing is dropped for
    lack of agreement and nothing is promoted for having it — repeats merge
    into one entry that records every model that offered it, and ``convergent``
    is left for the reader to weigh rather than used as a sort key.

    Ordering is by source-model weight, the same rule sections 3-5 use, which
    orders by how well-suited the model is and not by how many models agreed.

    Returns ``{}`` when no model ran the domain, which is the common case: the
    pass is opt-in. An empty dict and a dict of empty buckets mean different
    things downstream — "nobody ran this" versus "it ran and found nothing" —
    and the renderer says which.
    """
    domain_results = [
        (model, r)
        for (model, d), r in results.items()
        if d == "expansion" and not r.get("failed") and r.get("data")
    ]
    if not domain_results:
        return {}

    # Highest-weight model first, so its ordering wins where proposals merge.
    domain_results.sort(
        key=lambda pair: _get_weight(pair[0], "expansion", ensemble_cfg),
        reverse=True,
    )

    out = {}
    for bucket in _EXPANSION_BUCKETS:
        merged = []
        by_key = {}
        for model_name, r in domain_results:
            weight = _get_weight(model_name, "expansion", ensemble_cfg)
            for item in r["data"].get(bucket, []) or []:
                if not isinstance(item, dict):
                    continue
                key = _expansion_key(bucket, item)
                existing = by_key.get(key) if key else None
                if existing is not None:
                    if model_name not in existing["proposed_by"]:
                        existing["proposed_by"].append(model_name)
                        existing["convergent"] = True
                    continue
                entry = {
                    **item,
                    "proposed_by": [model_name],
                    "convergent": False,
                    "source_weight": round(weight, 2),
                }
                merged.append(entry)
                if key:
                    by_key[key] = entry
        out[bucket] = merged

    out["models"] = [model for model, _ in domain_results]
    return out


# ---------------------------------------------------------------------------
# Malformed flags / red-team buckets
# ---------------------------------------------------------------------------


#: `flags` bucket -> the field a bare string found in it should be read as.
#: fact_check has no `flags` bucket, and red_team's three findings are single
#: objects rather than lists — see `_RED_TEAM_DICT_KEYS` below.
_FLAGS_BUCKET_FIELDS = {
    "voice_style": {"flags": "passage"},
    "completeness": {"flags": "passage_reference"},
    "argument_integrity": {"flags": "passage"},
}

#: The red_team schema's three top-level findings. Each is a single object, not
#: a list — `_extract_passages` and the report readers pull `.passage` etc.
#: straight off of it.
_RED_TEAM_DICT_KEYS = (
    "most_vulnerable_claim",
    "highest_audience_risk",
    "highest_credibility_risk",
)


def _coerce_flags_bucket(data, model_name, domain):
    """Force the `flags` bucket of a voice_style/completeness/argument_integrity
    payload to a list of dicts, and the payload itself to a dict.

    Returns ``(data, notes)``: unchanged input when nothing needed fixing, one
    note per bucket touched otherwise. `_extract_passages` and
    `_build_flags_section` (reached for completeness through
    `_build_completeness`) both read `flags` with no shape check, so a bucket
    arriving as a string, a dict, or null — or a list holding a bare string
    instead of an object — raises where the first of them runs.

    Every provider that enforces a schema makes this unreachable.
    :data:`ci_article_review.schemas.BY_DOMAIN` covers this domain like every
    other one; gemini while grounded 400s on schema-plus-search and so runs
    prompt-only here exactly as it does on fact_check (see the `schemas` module
    docstring) — one live pass per run asking a model nicely for a shape and
    hoping.

    A bare string inside the list becomes ``{text_field: ...}`` — the model put
    N elements in an array, so element k is one finding, the same coercion
    `_collect_low_confidence` and `_collect_additional_observations` already
    apply to their own buckets. A bare string *as* the whole bucket is dropped,
    not wrapped: nothing asserts that ``"flags": "none"`` names a finding.
    """
    bucket_fields = _FLAGS_BUCKET_FIELDS.get(domain)
    if not bucket_fields:
        return data, []
    if not isinstance(data, dict):
        return {}, [
            {
                "model": model_name,
                "bucket": None,
                "field": bucket_fields["flags"],
                "found": type(data).__name__,
                "dropped": 0,
                "repaired": 0,
            }
        ]

    notes, replacements = [], {}
    for bucket, text_field in bucket_fields.items():
        if bucket not in data:
            continue  # Absent: every reader defaults it to [].
        value = data[bucket]
        if isinstance(value, list) and all(isinstance(i, dict) for i in value):
            continue  # The shape the schema promises.
        if value is None:
            # Present-but-null still needs rewriting even though nothing was
            # lost: `_extract_passages` and `_build_flags_section` both spell
            # this `data.get(bucket, [])`, whose default a present key never
            # reaches.
            replacements[bucket] = []
            continue

        dropped = repaired = 0
        if isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict):
                    items.append(item)
                elif isinstance(item, str) and item.strip():
                    items.append({text_field: item.strip()})
                    repaired += 1
                else:
                    dropped += 1
        elif isinstance(value, dict) and str(value.get(text_field, "") or "").strip():
            items, repaired = [value], 1
        else:
            items, dropped = [], 1

        replacements[bucket] = items
        notes.append(
            {
                "model": model_name,
                "bucket": bucket,
                "field": text_field,
                "found": type(value).__name__,
                "dropped": dropped,
                "repaired": repaired,
            }
        )

    if not replacements:
        return data, notes
    return {**data, **replacements}, notes


def _coerce_red_team_dicts(data, model_name):
    """Force red_team's three single-finding fields, and the payload itself, to
    dicts.

    `_extract_passages` and the report readers call
    `.get("passage"/"risk"/..., ...)` on `most_vulnerable_claim`,
    `highest_audience_risk` and `highest_credibility_risk` with no shape check.
    Grounded gemini runs prompt-only on red_team like every other domain (see
    the `schemas` module docstring) and can return any of the three as a
    string, a list, or leave it null.

    A single-item list is unwrapped rather than dropped — the model put its one
    finding in an array it should not have, the same slip `_coerce_flags_bucket`
    recovers for a bare string in a list bucket. Anything else becomes ``{}``,
    which is exactly what every reader's own ``.get(key, {})`` already treats
    as "no finding" — dropping it costs nothing a well-formed empty response
    would not also have cost.
    """
    if not isinstance(data, dict):
        return {}, [
            {
                "model": model_name,
                "bucket": None,
                "field": "passage",
                "found": type(data).__name__,
                "dropped": 0,
                "repaired": 0,
            }
        ]

    notes, replacements = [], {}
    for key in _RED_TEAM_DICT_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            continue
        if value is None:
            # Nothing lost, so nothing to report — same rule as a null
            # fact-check bucket.
            replacements[key] = {}
            continue

        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            replacements[key] = value[0]
            dropped, repaired = 0, 1
        else:
            replacements[key] = {}
            dropped, repaired = 1, 0
        notes.append(
            {
                "model": model_name,
                "bucket": key,
                "field": "passage",
                "found": type(value).__name__,
                "dropped": dropped,
                "repaired": repaired,
            }
        )

    if not replacements:
        return data, notes
    return {**data, **replacements}, notes


def _describe_malformed_flags_bucket(note, shape="list"):
    """One reader-facing phrase for a single coercion note.

    ``shape`` names what the bucket was supposed to be — "list" for a `flags`
    bucket, "object" for one of red_team's three findings — so the phrasing
    matches what actually broke.
    """
    if note["bucket"] is None:
        return f"{note['model']} returned a {note['found']}, not an object"

    where = f"{note['model']}.{note['bucket']}"
    if note["found"] != shape:
        kept = f", {note['repaired']} finding recovered" if note["repaired"] else ""
        article = "an" if shape[:1] in "aeiou" else "a"
        return f"{where} was a {note['found']} rather than {article} {shape}{kept}"

    parts = []
    if note["dropped"]:
        parts.append(f"{note['dropped']} unreadable item(s) dropped")
    if note["repaired"]:
        parts.append(f"{note['repaired']} bare string(s) read as {note['field']}s")
    return f"{where}: {', '.join(parts)}"


#: Every domain here also feeds SECTION 1 through `_extract_passages`, on top
#: of its own section named in `_DOMAIN_SECTIONS`.
_FLAGS_AFFECTED_SECTIONS = {
    domain: ("SECTION 1: Consensus", section)
    for domain, section in _DOMAIN_SECTIONS.items()
    if domain != "fact_check"
}


def _normalise_flags_results(results):
    """Make every voice_style/completeness/argument_integrity/red_team payload
    safe to consolidate, before anything reads it.

    Returns ``(results, degradations)``. ``results`` is the same object when
    nothing needed coercing; otherwise a shallow copy with only the offending
    entries replaced, so the caller's dict — and the captured ensemble a
    ``--replay`` reads off disk — are left alone.

    Called once at the top of :func:`build_report`, before `_extract_passages`
    (Section 1), `_build_flags_section` / `_build_completeness` (Sections 3-5)
    and `_build_red_team` (Section 6) all read the same payload — fixing only
    one of them would just move the crash to the next.
    """
    notes_by_domain, cleaned = {}, {}
    for key, result in results.items():
        model_name, domain = key
        if result.get("failed") or not result.get("data"):
            continue
        data = result["data"]
        if domain in _FLAGS_BUCKET_FIELDS:
            new_data, notes = _coerce_flags_bucket(data, model_name, domain)
        elif domain == "red_team":
            new_data, notes = _coerce_red_team_dicts(data, model_name)
        else:
            continue
        if notes:
            notes_by_domain.setdefault(domain, []).extend(notes)
        if new_data is not data:
            cleaned[key] = {**result, "data": new_data}

    if not cleaned:
        return results, []
    results = {**results, **cleaned}
    if not notes_by_domain:
        return results, []

    degradations = []
    for domain, notes in sorted(notes_by_domain.items()):
        sections = _FLAGS_AFFECTED_SECTIONS[domain]
        shape = "object" if domain == "red_team" else "list"
        described = "; ".join(_describe_malformed_flags_bucket(n, shape) for n in notes)
        detail = (
            f"{sections[0]} and {sections[1]} are short findings this run paid "
            f"for: {len(notes)} {domain} bucket(s) came back in a shape "
            f"consolidation cannot read ({described}). Whatever was readable "
            f"was kept; the rest is missing from the report entirely. Only a "
            f"pass running without schema enforcement can return this, so a "
            f"re-run may well come back clean — treat the counts below as a "
            f"floor."
        )
        log.warning("%s: %s", domain, detail)
        degradations.append(
            {
                "section": ", ".join(sections),
                "caused_by": sorted({f"{n['model']}:{domain}" for n in notes}),
                "detail": detail,
            }
        )
    return results, degradations


# ---------------------------------------------------------------------------
# Cross-model contradiction detection
# ---------------------------------------------------------------------------


def find_contradictions(results):
    """Surface claims confirmed by one fact-check model but challenged by another.

    Returns a list of contradiction dicts.  Each entry has:
      claim           str   — the disputed claim text
      confirmed_by    list  — model names that marked it confirmed
      challenged_by   list  — model names that challenged it
      challenge_type  str   — "outdated" | "contradicted" | "unverifiable" | "mixed"

    ``unverifiable`` counts as a challenge
    --------------------------------------
    It did not, and that silence was the whole output. One model calling a claim
    *confirmed* while another cannot verify it at all is a disagreement about
    evidence, and it is the disagreement this ensemble actually produces:
    ``outdated`` and ``contradicted`` were empty across all six fact-check
    passes on 2026-09-03, while five claims were confirmed by one model and
    marked unverifiable by another. The report said ``contradictions: 0``.

    What was hidden mattered. Among those five were "I have a side job." and
    "I have a family." — unfalsifiable first-person statements that two models
    reported as *confirmed*. Surfacing the disagreement is what makes that
    visible.
    """
    stances = []

    for (model_name, domain), result in results.items():
        if domain != "fact_check" or result.get("failed") or not result.get("data"):
            continue
        data = result["data"]
        for item in data.get("confirmed", []):
            claim = item.get("claim", "")
            if claim:
                stances.append(
                    {
                        "model": model_name,
                        "claim": claim,
                        "stance": "confirmed",
                        "type": "confirmed",
                    }
                )
        for bucket in ("contradicted", "outdated", "unverifiable"):
            for item in data.get(bucket, []):
                claim = item.get("claim", "")
                if claim:
                    stances.append(
                        {
                            "model": model_name,
                            "claim": claim,
                            "stance": "challenged",
                            "type": bucket,
                        }
                    )

    contradictions = []
    # Grouped by place in the draft rather than exact string, so a claim quoted
    # at two lengths by two models is one disagreement rather than none.
    for claim, group in group_passages(stances, lambda s: s["claim"]):
        confirmed = [s for s in group if s["stance"] == "confirmed"]
        challenged = [s for s in group if s["stance"] == "challenged"]
        if not confirmed or not challenged:
            continue

        confirmed_by = sorted({s["model"] for s in confirmed})
        challenged_by = sorted({s["model"] for s in challenged})
        # One model listing a claim in two buckets is a malformed response, not
        # a cross-model disagreement.
        if confirmed_by == challenged_by and len(confirmed_by) == 1:
            continue

        types = {s["type"] for s in challenged}
        contradictions.append(
            {
                "claim": claim,
                "confirmed_by": confirmed_by,
                "challenged_by": challenged_by,
                "challenge_type": types.pop() if len(types) == 1 else "mixed",
            }
        )

    return contradictions


# ---------------------------------------------------------------------------
# Low-confidence and additional observations collectors
# ---------------------------------------------------------------------------


def _result_is_empty(result):
    """True if a successful call returned a payload with nothing in it.

    Schema-valid and empty is indistinguishable from "reviewed and found
    nothing" without this — and the two are not the same claim. Every domain's
    schema is a set of lists (``flags``, ``low_confidence``, the fact-check
    buckets) plus, for red_team, a few dicts, so "no list has an entry and no
    dict has a value" covers all of them without a per-domain table that would
    drift as the schemas change.
    """
    data = result.get("data")
    if not isinstance(data, dict) or not data:
        return True
    for value in data.values():
        if isinstance(value, list) and value:
            return False
        if isinstance(value, dict) and any(
            v not in (None, "", [], {}) for v in value.values()
        ):
            return False
        if isinstance(value, str) and value.strip():
            return False
    return True


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


#: Stated confidence, strongest first. Used only to order Section 8, and only
#: as the tiebreaker *under* corroboration.
#:
#: `_DEFAULT_CONFIDENCE_MULTIPLIERS` stays inert for the reason recorded there:
#: self-reported confidence is not calibrated and is not comparable across
#: providers. That argument is about using the level as a weight in a sum, and
#: it holds. It does not reach ordering, and it does not reach the question this
#: section actually needs answered - which of 55 observations to read first.
#:
#: The weighting was also never able to fire. It reads `confidence` off flags on
#: their way into consensus, and the schema puts `confidence` on
#: `additional_observations[]` plus three `fact_check` buckets, of which
#: consensus reads two. Measured on the 2026-09-04 maximum run: 0 of 119
#: consensus-bound findings carried a confidence, against 55 that did and never
#: reached consensus at all. The signal was collected in one place and looked
#: for in another.
_CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1}


def _confidence_rank(observation):
    """Ordering rank for a stated confidence; 0 when absent or unrecognised."""
    level = str(observation.get("confidence", "")).strip().lower()
    return _CONFIDENCE_ORDER.get(level, 0)


def _merge_additional_observations(observations):
    """Group Section 8 by passage, then rank by corroboration and confidence.

    Two models independently making the same observation is the strongest thing
    this section can tell you, and it was invisible: observations were appended
    in dict-iteration order, so an agreeing pair rendered as two unrelated
    bullets somewhere in a list of 55 (measured on the 2026-09-04 maximum run).
    Section 1 has grouped by passage since the identical problem was found
    there; this is that same :func:`group_passages` pass applied to the section
    that never received it.

    Merged entries keep the fields of the most confident observation in the
    group, so every existing consumer still reads what it read before, and gain
    ``models`` and ``model_count``. Ranking is corroboration first, stated
    confidence second - which also stops :mod:`voice_pattern_report` counting
    one article's voice problem twice because two models both noticed it.
    """
    if not observations:
        return []

    merged = []
    for passage, group in group_passages(observations, lambda o: o.get("passage", "")):
        models = sorted({o.get("source_model", "?") for o in group})
        best = max(group, key=_confidence_rank)
        merged.append(
            {**best, "passage": passage, "models": models, "model_count": len(models)}
        )

    merged.sort(key=lambda o: (o["model_count"], _confidence_rank(o)), reverse=True)
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _ensemble_width(results, ensemble_cfg, lt_voted=False):
    """How wide the ensemble behind this report actually was.

    A cost preset buys fewer models, and that is the point of it — but until
    now the only way to find out *how much* narrower a run was than the preset
    it names was to count rows in the API call table and cross-reference them
    against `_THOROUGHNESS_PRESETS` in the source. The numbers that change how
    the sections should be read are: how many domains came down to a single
    model, how many distinct models ran at all, and whether Section 1 can still
    reach consensus with that many voters.

    Measured 2026-09-05: at `economy` the `standard` map runs five domains on
    three distinct models, every one of them single-model, because `economy`
    disables grok and claude and both of the two-model domains are
    mistral-paired. The report said none of that.

    Voters are counted per *model*, not per call, because that is what
    ``_find_consensus`` counts — two findings from one model are one voter, and
    LanguageTool is a voter in its own right when it flagged anything.
    """
    ran = {
        (model, domain)
        for (model, domain), r in results.items()
        if not r.get("failed") and not r.get("skipped")
    }
    models_by_domain: dict[str, list[str]] = {}
    for model, domain in sorted(ran):
        models_by_domain.setdefault(domain, []).append(model)

    # The preset's domains, not the ones that produced results: a domain whose
    # every model was excluded has no result to be counted and would otherwise
    # vanish from a report about coverage.
    expected = list(ensemble_cfg.get("preset_domains") or sorted(models_by_domain))
    for domain in models_by_domain:
        if domain not in expected:
            expected.append(domain)

    distinct_models = sorted({model for model, _ in ran})
    min_models = int(
        ensemble_cfg.get("consensus_min_models", _DEFAULT_CONSENSUS_MIN_MODELS)
    )
    # LanguageTool is an independent source and counts toward the minimum, so a
    # run's true voter pool is the models plus LT when LT flagged something.
    voter_pool = len(distinct_models) + (1 if lt_voted else 0)
    multi_model_domains = [d for d in expected if len(models_by_domain.get(d, [])) > 1]

    return {
        "models_by_domain": {d: models_by_domain.get(d, []) for d in expected},
        "distinct_models": distinct_models,
        "domains_single_model": [
            d for d in expected if len(models_by_domain.get(d, [])) == 1
        ],
        # Which domains nothing reviewed, and why, is reported at the top level
        # as ``domains_not_run`` — with a reason, and with a note above the
        # affected section. Not repeated here: one fact, one place.
        "domains_expected": expected,
        "consensus_min_models": min_models,
        "voter_pool": voter_pool,
        "languagetool_voted": bool(lt_voted),
        # Can Section 1 flag anything at all? With fewer distinct sources than
        # the minimum, no passage can clear it however many findings agree.
        "consensus_reachable": voter_pool >= min_models,
        # Every domain single-model means no passage can reach the minimum from
        # within one domain — agreement has to come from two domains at once,
        # which is a much narrower path than the same threshold implies at a
        # preset where domains carry two models each.
        "consensus_needs_cross_domain": (
            min_models > 1 and not multi_model_domains and bool(distinct_models)
        ),
        "backfilled": list(ensemble_cfg.get("backfilled_assignments") or []),
    }


def _build_provenance(drafted_with):
    """Provenance block: what drafted this, and does that provider mark text.

    Kept in the report rather than only printed, so ``pipeline_history/``
    accumulates a durable record of which articles carry a mark. By the time
    anyone asks the question about a piece published months ago, the chat
    thread that produced it is long gone.
    """
    status = watermarking.status_for(drafted_with)
    return {"drafted_with": (drafted_with or "").strip() or None, **status}


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
    domains_not_run=None,
    drafted_with="",
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
    domains_not_run:
        ``{domain: reason}`` for domains the run should have covered but made no
        call for. Supplied by the pipeline, which is what knows the presets and
        the drafter exclusion; it cannot be recovered here, because every other
        record in this report is derived from ``results`` and a domain that was
        never attempted has no entry there to derive from.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Before anything reads a fact-check bucket, a `flags` bucket or a red_team
    # finding. Every one of these is read by two separate functions below —
    # `_extract_passages` for Section 1 and each domain's own section builder —
    # and neither checks the shape it assumes; a malformed one used to kill
    # report building wherever the first reader ran, after the ensemble had
    # been paid for in full.
    results, fact_check_degradations = _normalise_fact_check_results(results)
    results, flags_degradations = _normalise_flags_results(results)

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

    # Section 10 — opt-in expansion proposals. {} when the pass did not run,
    # which is the default.
    section_10_expansion = _build_expansion(results, ensemble_cfg)

    # Section 7 — Low-confidence observations
    section_7_low_confidence = _collect_low_confidence(results)

    # Section 8 — Cross-domain additional observations
    section_8_additional = _merge_additional_observations(
        _collect_additional_observations(results)
    )

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

    # Domains that made no call at all. Kept separate from the failures above
    # because the reader's question is different: a failed pass means the
    # section is short a model, while this means the section has no model
    # behind it and its emptiness carries no information about the draft.
    domains_not_run_details = [
        {
            "domain": domain,
            "section": _DOMAIN_SECTIONS.get(domain),
            "reason": reason,
        }
        for domain, reason in sorted((domains_not_run or {}).items())
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

    # Calls that returned a well-formed response containing nothing at all.
    # A third outcome beside "failed" and "truncated", and until now the only
    # one with nowhere to be recorded: the call succeeded, so model_failures
    # skipped it; nothing was cut off, so truncated_results skipped it; and the
    # section it should have contributed to was quietly built one model short.
    #
    # Measured 2026-09-03: gemini:voice_style spent 1,763 completion tokens and
    # returned empty arrays, perplexity:voice_style returned 14 tokens and did
    # the same. Both logged OK. Section 3 was built from three models rather
    # than five and said so nowhere.
    empty_results = [
        f"{model}:{domain}"
        for (model, domain), r in results.items()
        if not r.get("failed") and not r.get("skipped") and _result_is_empty(r)
    ]
    empty_result_details = [
        {
            "pass": f"{model}:{domain}",
            "model": r.get("model") or model,
            "domain": domain,
            "section": _DOMAIN_SECTIONS.get(domain),
            "completion_tokens": (r.get("tokens") or {}).get("completion"),
            "elapsed_seconds": r.get("elapsed_seconds"),
        }
        for (model, domain), r in results.items()
        if not r.get("failed") and not r.get("skipped") and _result_is_empty(r)
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

    report = {
        "generated": now,
        "run_number": run_number,
        "article_title": article_title,
        "publication": publication_name,
        "lt_corrections_applied": lt_result.get("change_log", []) if lt_result else [],
        # Absent is not failed. With no LanguageTool result at all there is
        # nothing to report a failure about, and defaulting to True meant a run
        # that never attempted the pass claimed it had tried and failed.
        "lt_failed": bool(lt_result.get("failed")) if lt_result else False,
        "lt_skipped": lt_result.get("skipped", False) if lt_result else False,
        # "disabled" (grammar_pass: false) or "no_credentials" — the summary
        # named the wrong one for either, sending operators to configure
        # credentials they already had.
        "lt_skipped_reason": lt_result.get("skipped_reason") if lt_result else None,
        "corrected_draft": corrected_draft,
        "primary_claim": primary_claim,
        # Which provider drafted the article, and whether that provider marks
        # its text output. Declared in the handoff, never measured: a
        # statistical watermark cannot be detected without the provider's key,
        # so this records what the author said rather than what the text shows.
        "provenance": _build_provenance(drafted_with),
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
        # Section 9 (citations) is attached by the pipeline after Pass 3.
        "section_10_expansion": section_10_expansion,
        "contradictions": contradictions,
        "model_failures": model_failures,
        "model_failure_details": model_failure_details,
        "domains_not_run": domains_not_run_details,
        "truncated_results": truncated_results,
        "empty_results": empty_results,
        "empty_result_details": empty_result_details,
        "ensemble": {
            "thoroughness": ensemble_cfg.get("thoroughness", "standard"),
            "cost_preset": ensemble_cfg.get("cost_preset"),
            "consensus_threshold": float(
                ensemble_cfg.get("consensus_threshold", _DEFAULT_CONSENSUS_THRESHOLD)
            ),
            "assignments": assignments,
            "width": _ensemble_width(
                results, ensemble_cfg, lt_voted=bool(lt_flagged_passages)
            ),
        },
    }
    # Only when there is something to say. The key is absent on a clean run —
    # ``_record_fact_check_degradation`` and ``_record_impersonation_degradation``
    # in pipeline.py both ``setdefault`` onto it later, and both the console
    # summary and ``_render_degradations`` treat an empty list and a missing
    # key alike.
    for entry in fact_check_degradations + flags_degradations:
        report.setdefault("degradations", []).append(entry)
    return report


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
