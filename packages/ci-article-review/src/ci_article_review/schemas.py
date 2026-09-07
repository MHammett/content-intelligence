"""JSON schemas for the review domains, mirroring the prompts.

Each domain prompt ends with a RETURN FORMAT block. These schemas are that block
expressed so a provider can *enforce* it, rather than the model being asked
nicely and the pipeline hoping. Verified 2026-08-16: every provider the ensemble
uses honours a schema — the exception is gemini while grounded, which rejects
the combination outright, so it keeps prompt-only JSON on fact_check.

Why bother, when the prompt already says what shape to return: a loose
instruction to gpt-5.4-mini came back as ``{"ai_speak": ..., "suggestion": ...}``
when the caller wanted ``{"flags": [...]}``. Consolidation reads named buckets,
so a response like that contributes nothing while reporting as a success.

**These must track the prompts.** A field added to a RETURN FORMAT block and not
added here is a field the model is now forbidden to return — strict mode means
``additionalProperties: false``. ``test_schemas.py`` compares the two and fails
when they drift.

On strict mode and honesty: strict requires every property to appear in
``required``, which for a fact-check is a feature rather than a nuisance. The
prompt already says a claim without a quotable source does not belong in
``confirmed`` — it belongs in ``unverifiable`` or ``primary_source_needed``.
Requiring the evidence fields on the buckets that assert a verdict enforces the
rule the prompt states. The genuinely optional field, ``note``, is nullable so
the model can decline it without inventing one.
"""

from ci_article_review.fact_check_scope import CLAIM_TYPES

_CONFIDENCE = {"type": "string", "enum": ["high", "medium", "low"]}

#: The categories a claim can be put out of scope under. Imported rather
#: than restated so the prompt, the schema and the config filter that reads
#: them cannot drift — the enum is what makes ``exclude_types`` meaningful.
_CLAIM_TYPE = {"type": "string", "enum": list(CLAIM_TYPES)}


def _obj(**properties):
    """A strict object: every property required, nothing else permitted."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _array_of(**properties):
    return {"type": "array", "items": _obj(**properties)}


_STR = {"type": "string"}
_NULLABLE_STR = {"type": ["string", "null"]}

#: Every domain carries this: findings outside its own remit.
_ADDITIONAL_OBSERVATIONS = _array_of(
    category=_STR, passage=_STR, observation=_STR, confidence=_CONFIDENCE
)


VOICE_STYLE = _obj(
    flags=_array_of(
        passage=_STR,
        problem=_STR,
        suggested_rewrite=_STR,
        steelman_considered=_STR,
    ),
    low_confidence=_array_of(passage=_STR, observation=_STR),
    additional_observations=_ADDITIONAL_OBSERVATIONS,
)

COMPLETENESS = _obj(
    flags=_array_of(
        what_is_missing=_STR,
        passage_reference=_STR,
        audience_affected=_STR,
        steelman_considered=_STR,
    ),
    low_confidence=_array_of(observation=_STR, passage_reference=_STR),
    additional_observations=_ADDITIONAL_OBSERVATIONS,
)

ARGUMENT_INTEGRITY = _obj(
    flags=_array_of(
        passage=_STR,
        logical_problem=_STR,
        steelman_considered=_STR,
        why_it_survived=_STR,
    ),
    low_confidence=_array_of(passage=_STR, observation=_STR),
    additional_observations=_ADDITIONAL_OBSERVATIONS,
)

FACT_CHECK = _obj(
    confirmed=_array_of(
        claim=_STR,
        source=_STR,
        source_url=_STR,
        supporting_quote=_STR,
        confidence=_CONFIDENCE,
        note=_NULLABLE_STR,
    ),
    outdated=_array_of(
        claim=_STR,
        current_value=_STR,
        source=_STR,
        source_url=_STR,
        supporting_quote=_STR,
        confidence=_CONFIDENCE,
    ),
    contradicted=_array_of(
        claim=_STR,
        contradiction=_STR,
        source=_STR,
        source_url=_STR,
        supporting_quote=_STR,
        confidence=_CONFIDENCE,
    ),
    unverifiable=_array_of(claim=_STR, checked=_STR, reason=_STR),
    primary_source_needed=_array_of(claim=_STR, best_candidate_source=_STR),
    # Not a verdict — a statement that no verdict was ever available. See
    # ``fact_check_scope`` for why that is worth a bucket of its own rather than
    # a sixth flavour of "unverifiable".
    out_of_scope=_array_of(claim=_STR, claim_type=_CLAIM_TYPE, reason=_STR),
    additional_observations=_ADDITIONAL_OBSERVATIONS,
)

RED_TEAM = _obj(
    most_vulnerable_claim=_obj(
        passage=_STR,
        attack_vector=_STR,
        supporting_evidence_for_attack=_STR,
    ),
    highest_audience_risk=_obj(passage=_STR, risk=_STR, audience_segment=_STR),
    highest_credibility_risk=_obj(passage=_STR, risk=_STR, attack_vector=_STR),
    additional_observations=_ADDITIONAL_OBSERVATIONS,
)


#: Domain name -> schema. Keys match ``pipeline._DOMAIN_PROMPTS``.
BY_DOMAIN = {
    "fact_check": FACT_CHECK,
    "voice_style": VOICE_STYLE,
    "completeness": COMPLETENESS,
    "argument_integrity": ARGUMENT_INTEGRITY,
    "red_team": RED_TEAM,
}


def for_domain(domain):
    """The ``{"name", "schema"}`` pair for ``domain``, or None.

    None for a custom domain defined in a publication config: those carry a
    prompt but no schema, and asking a provider to enforce a shape nobody
    declared would reject perfectly good output.
    """
    schema = BY_DOMAIN.get(domain)
    if schema is None:
        return None
    return {"name": domain, "schema": schema}
