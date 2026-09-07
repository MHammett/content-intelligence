"""JSON schemas for the review domains, mirroring the prompts.

Each domain prompt ends with a RETURN FORMAT block. These schemas are that block
expressed so a provider can *enforce* it, rather than the model being asked
nicely and the pipeline hoping. Verified 2026-08-16: every provider the ensemble
uses honours a schema — the exception is gemini while grounded, which rejects
the combination outright and so keeps prompt-only JSON on every domain, not
just fact_check: ``ci_core.llm.client._provider_params`` sets gemini's search
tool unconditionally, with no per-domain check, so a grounded gemini call runs
schema-free across all five review domains.

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

#: URLs the model actually opened while checking a claim. Array rather than a
#: single string because a grounded model consults several, and the resolver
#: already accepts an ordered candidate list.
_URL_ARRAY = {"type": "array", "items": {"type": "string"}}

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
    # `sources_checked` and `best_candidate_url` were added 2026-09-04. The
    # schema previously asked for a URL only in the three buckets where the
    # model had already concluded the source supports the claim, and asked for
    # prose in the two where it had not — which are precisely the claims
    # SECTION 9 could still go and resolve.
    #
    # The cost of that was measured: perplexity filled `source_url` on 3 of 3
    # `confirmed` findings and on 0 of 14 in these two buckets, while holding 15
    # relevant citations it had just retrieved. Across all six models, 50
    # `unverifiable` findings carried no URL at all — gemini's `checked` field
    # read "Google Search" — and `best_candidate_source` came back as prose like
    # "the publication's own post archive or sitemap.xml".
    #
    # Asking the model which pages it read is claim-level attribution from the
    # only party that knows it. An earlier attempt to recover this took the
    # first entry of the provider's response-level citation list and attached it
    # to every claim in the response, which stamped one energy report onto 44
    # unrelated claims; see `_collect_citation_claims`. This asks instead of
    # guessing.
    unverifiable=_array_of(
        claim=_STR,
        checked=_STR,
        sources_checked=_URL_ARRAY,
        reason=_STR,
    ),
    primary_source_needed=_array_of(
        claim=_STR,
        best_candidate_source=_STR,
        best_candidate_url=_NULLABLE_STR,
    ),
    # Not a verdict — a statement that no verdict was ever available. See
    # ``fact_check_scope`` for why that is worth a bucket of its own rather
    # than a sixth flavour of "unverifiable". No URL fields here for the
    # same reason the bucket exists: there is no page to name.
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


EXPANSION = _obj(
    sources=_array_of(
        title=_STR,
        url=_NULLABLE_STR,
        where_to_look=_STR,
        what_it_establishes=_STR,
        supports=_STR,
        why_it_fits=_STR,
    ),
    topics=_array_of(
        topic=_STR,
        why_it_fits=_STR,
        audience_served=_STR,
        where_it_would_go=_STR,
        closes_known_gap=_NULLABLE_STR,
    ),
    angles=_array_of(
        angle=_STR,
        why_it_fits=_STR,
        extends_or_complicates={"type": "string", "enum": ["extends", "complicates"]},
        cost_to_adopt=_STR,
    ),
    data_points=_array_of(
        data_point=_STR,
        why_it_strengthens=_STR,
        where_to_find_it=_STR,
        url=_NULLABLE_STR,
    ),
    additional_observations=_ADDITIONAL_OBSERVATIONS,
)


#: Domain name -> schema. Keys match ``pipeline._DOMAIN_PROMPTS``.
BY_DOMAIN = {
    "fact_check": FACT_CHECK,
    "voice_style": VOICE_STYLE,
    "completeness": COMPLETENESS,
    "argument_integrity": ARGUMENT_INTEGRITY,
    "red_team": RED_TEAM,
    "expansion": EXPANSION,
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
