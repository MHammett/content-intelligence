"""Propose SEO metadata for an article under review — one cheap model call.

``analysis.seo`` validates SEO and reports what is missing. Nothing proposed
anything, which left two holes. The publication handoff template offers
"derive from primary claim" / "derive from opening paragraph" as SEO METADATA
values, and ``handoff_parser`` even guards against those placeholders reaching
WordPress un-derived — but no code ever derived them. And the draft submission
template has no SEO section at all, so ``no_meta_description`` fired on every
draft run against a field that format cannot supply.

This module fills both: it drafts the values, at draft-review time, where they
feed the chat revision round-trip and where seeing the intended keyword early
can reveal that the article never actually uses the phrase it should rank for.

It covers the whole SEO METADATA block, not a subset — every field
``handoff_parser._parse_seo_block`` reads and ``adapters/cms/wordpress.py``
pushes to Rank Math. Two of those fields have sensible defaults in the push
(OG title falls back to the article title, OG description to the meta
description), so for those a suggestion is offered only when it would beat the
default; the field still reports an outcome either way, because "the default
applies, and here is what it is" is information and silence is not.

Everything here is advisory. Keyword choice is strategic — what the author
wants to rank for — so candidates are returned for a human to pick from, and
nothing is written into any config, handoff, or WordPress metadata.

The call follows ``adapters/citation/resolver.py``'s ``_verify_relevance``: a
small fast model, one call, cost logged under its own pass name, and any
failure degrading to "no suggestions this run" rather than failing the run.
"""

import logging
import re

from ci_core import redact
from ci_core import llm

from . import seo as seo_analysis
from ci_core.llm import cost

log = logging.getLogger(__name__)

#: Cheap/fast model — this runs once per review and produces a few short
#: strings, so it deliberately avoids a heavyweight reasoning model. Same
#: choice, and same reasoning, as the citation relevance verifier.
_SUGGESTION_MODEL = "mistral-small-latest"

#: Cost-summary pass name. Distinct from the review passes (``model:domain``)
#: so the suggestion's cost is attributable at a glance.
CALL_LOG_PASS = "seo_suggestions"

#: How much of the article body to send. The opening carries the framing a
#: meta description has to summarize and the tail usually restates the thesis;
#: the heading outline (sent separately, in full) covers the middle, which is
#: why a blind head+tail of a 15,000-word piece isn't what goes over the wire.
_BODY_HEAD_CHARS = 3000
_BODY_TAIL_CHARS = 500

#: Cap on outline entries — a heavily-subheaded piece shouldn't crowd out the
#: body text in the prompt.
_MAX_OUTLINE_ENTRIES = 40

#: Model is asked for 3-5; this is the hard cap applied to whatever comes back.
_MAX_KEYWORD_CANDIDATES = 5

#: Schema types publication.md offers the author. Rank Math accepts more, so
#: anything outside this set is surfaced rather than dropped — flagged as
#: needing confirmation instead of silently discarded.
_KNOWN_SCHEMA_TYPES = ("Article", "NewsArticle", "BlogPosting")

#: Fallback when the publication config sets no rank_math.default_schema_type.
#: Matches the same fallback in adapters/cms/wordpress.py, so what the report
#: says would be pushed is what would actually be pushed.
_DEFAULT_SCHEMA_TYPE = "BlogPosting"

#: Single-value SEO METADATA fields, in the order publication.md lists them.
#: The focus keyword is handled separately — it is a set of candidates for a
#: human to choose between, not one proposed value.
FIELD_ORDER = ("meta_description", "og_title", "og_description", "schema_type")

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

_SYSTEM_PROMPT = (
    "You propose SEO metadata options for an article that is still being "
    "reviewed, before its author has chosen any. You do not decide anything — "
    "the author picks from what you offer.\n"
    "Respond with ONLY a JSON object of the form "
    '{"keyword_candidates": [{"keyword": "<focus keyword or key phrase>", '
    '"rationale": "<one line>"}], "meta_description": "<one sentence or two>", '
    '"og_title": "<shorter title>", "og_description": "<social-card text>", '
    '"schema_type": "<type>", "schema_type_rationale": "<one line>"}.\n'
    "Rules:\n"
    "- Give 3 to 5 keyword candidates, strongest first. Each must be a phrase "
    "a real reader would type into a search engine, specific to what THIS "
    "article establishes — not a generic topic label for the subject area.\n"
    "- Each rationale is ONE line covering what the phrase would rank for and "
    "who is searching it. If the article never actually uses the phrase, say "
    "so in the rationale — that is useful to the author, not a reason to drop "
    "the candidate.\n"
    "- The meta description must read as prose that describes what the article "
    "establishes. It is not a keyword list, and it must not oversell: no "
    '"everything you need to know", no invented findings.\n'
    "- Omit the og_title key entirely unless the prompt explicitly asks for "
    "one.\n"
    "- og_description is the social-card text. It defaults to the meta "
    "description, and that default is usually right. Omit the key unless a "
    "distinctly social framing — a hook rather than a summary — would "
    "genuinely serve the piece better. Do not return a reworded near-copy of "
    "the meta description.\n"
    "- schema_type classifies the article for structured data. Choose from "
    f"{', '.join(_KNOWN_SCHEMA_TYPES)}: NewsArticle for reporting tied to a "
    "current event, Article for reference or explanatory writing, BlogPosting "
    "for commentary and opinion in a personal voice. Give a one-line "
    "schema_type_rationale saying which kind of piece this is."
)


def _outline(text):
    """The article's H1/H2/H3 outline, in document order, as markdown lines."""
    headings = _HEADING_RE.findall(text or "")
    lines = [f"{hashes} {title.strip()}" for hashes, title in headings]
    if len(lines) > _MAX_OUTLINE_ENTRIES:
        omitted = len(lines) - _MAX_OUTLINE_ENTRIES
        lines = lines[:_MAX_OUTLINE_ENTRIES] + [f"...[{omitted} more heading(s)]"]
    return "\n".join(lines)


def _build_user_prompt(
    text, handoff, pub_config, meta_limit, og_title_request, schema_default
):
    handoff = handoff or {}
    pub_config = pub_config or {}

    parts = [f"ARTICLE TITLE: {handoff.get('title', '')}"]
    if handoff.get("primary_claim"):
        parts.append(f"PRIMARY CLAIM: {handoff['primary_claim']}")
    if handoff.get("target_audience"):
        parts.append(f"TARGET AUDIENCE: {handoff['target_audience']}")
    if pub_config.get("publication_description"):
        parts.append(f"PUBLICATION: {pub_config['publication_description']}")
    if pub_config.get("audience"):
        parts.append(f"PUBLICATION AUDIENCE: {pub_config['audience']}")

    outline = _outline(text)
    if outline:
        parts.append(f"HEADING OUTLINE:\n{outline}")

    excerpt = redact.truncate_excerpt(
        text or "", head=_BODY_HEAD_CHARS, tail=_BODY_TAIL_CHARS
    )
    parts.append(
        f"ARTICLE TEXT (opening and close; the outline above covers the rest):\n{excerpt}"
    )

    # The previous wording ("must be under N characters — count them") was
    # overrun on consecutive runs: 157/155, then 177/155. The value is
    # deliberately never truncated (see _field), so a miss here lands on the
    # author as manual work. Stating the consequence and asking for margin gives
    # the model somewhere to land short of the ceiling rather than at it.
    parts.append(
        f"\nHARD LIMIT: the meta description must be at most {meta_limit} "
        f"characters. Search engines cut anything longer off mid-sentence, so "
        f"going over makes it unusable, not merely imperfect. Target "
        f"{max(40, meta_limit - 20)}-{meta_limit - 5} characters to leave margin. "
        f"Write it, count the characters, and rewrite it shorter if it exceeds "
        f"{meta_limit}. An og_description, if you offer one, is held to the same "
        f"limit."
    )
    parts.append(
        f"This publication's configured default schema type is "
        f"{schema_default}. Say so if that default is right for this piece; "
        f"name a different type only when the piece is genuinely a different "
        f"kind of writing."
    )
    if og_title_request:
        parts.append(og_title_request)
    return "\n\n".join(parts)


def _clean_candidates(raw):
    """Normalize whatever came back into ``[{keyword, rationale}]``, capped.

    Tolerant of a model that returns bare strings instead of objects: a
    keyword with no rationale is still a usable candidate, and dropping it
    over shape would waste the call.
    """
    candidates = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            keyword, rationale = item, ""
        elif isinstance(item, dict):
            keyword = item.get("keyword") or item.get("phrase") or ""
            rationale = item.get("rationale") or item.get("reason") or ""
        else:
            continue
        keyword = str(keyword).strip()
        if not keyword:
            continue
        candidates.append({"keyword": keyword, "rationale": str(rationale).strip()})
        if len(candidates) == _MAX_KEYWORD_CANDIDATES:
            break
    return candidates


def _field(label, value="", limit=None, rationale="", default_note=""):
    """One SEO METADATA field's outcome, in the shape every renderer expects.

    A field always reports something. Either ``value`` holds a proposal — with
    its length measured against ``limit`` where one applies — or
    ``default_note`` explains which default takes effect instead. An
    over-limit value is reported with its count, not truncated into a dangling
    clause and not dropped: cutting at a word boundary reads worse than the
    author trimming it, and dropping throws away the only draft the run made.
    """
    value = str(value or "").strip()
    return {
        "label": label,
        "value": value,
        "rationale": str(rationale or "").strip(),
        "chars": len(value) if value else None,
        "limit": limit if value else None,
        "over_limit": bool(value and limit is not None and len(value) > limit),
        "default_note": "" if value else str(default_note or ""),
    }


def _normalized(text):
    """Casefolded, whitespace-collapsed text, for near-duplicate comparison."""
    return " ".join(str(text or "").split()).casefold()


def _schema_type_field(data, schema_default):
    """Classify the article for structured data, against the configured default.

    Always reports: confirming that the publication's default is right for
    this piece is as useful as naming a different type, and the author has no
    other prompt to think about it. A type outside the set publication.md
    offers is kept but flagged — Rank Math accepts more types than the
    template lists, so an unfamiliar answer may be right, but it is not
    something to act on unchecked.
    """
    raw = str(data.get("schema_type") or "").strip()
    rationale = str(data.get("schema_type_rationale") or "").strip()

    if not raw:
        return _field(
            "Schema type",
            default_note=(
                f"No type proposed — the configured default ({schema_default}) "
                f"is what the push would set."
            ),
        )

    # Match case-insensitively so "newsarticle" resolves to the canonical
    # spelling Rank Math expects rather than being flagged as unrecognized.
    canonical = next(
        (t for t in _KNOWN_SCHEMA_TYPES if t.casefold() == raw.casefold()), None
    )
    field = _field("Schema type", value=canonical or raw, rationale=rationale)
    field["recognized"] = canonical is not None
    field["configured_default"] = schema_default
    field["differs_from_default"] = (field["value"] or "").casefold() != str(
        schema_default
    ).casefold()
    return field


def _skipped(reason):
    log.info("SEO suggestions: %s", reason)
    return {"status": "skipped", "reason": reason}, None


def _failed(reason, call_log_entry=None):
    log.warning("SEO suggestions unavailable — %s", reason)
    return {"status": "failed", "reason": reason}, call_log_entry


def generate(text, handoff=None, pub_config=None, api_keys=None, seo_result=None):
    """Draft SEO metadata for ``text``. Returns ``(suggestions, call_log_entry)``.

    ``seo_result`` is the dict from ``seo.analyze`` for the same article; an
    OG title is only *requested* when it flagged the title as too long, since
    that is the case OG title exists to solve. The field is still reported
    either way — as the article-title default when no override is needed.

    ``suggestions`` always has a ``status``: ``ok`` when the model answered
    usefully, ``skipped`` when no call was made (disabled, or no credentials),
    ``failed`` when the call ran and didn't produce anything usable. The two
    non-ok states carry a ``reason``. An ``ok`` result carries
    ``keyword_candidates`` plus a ``fields`` map keyed by ``FIELD_ORDER``, each
    entry shaped by ``_field``. ``call_log_entry`` is a cost-tracking dict in
    the pipeline's ``api_call_log`` shape when a call was attempted, else None.

    Never raises — a suggestion is a nicety, and no failure here may cost the
    author a review run.
    """
    seo_rules = (pub_config or {}).get("seo_rules") or {}
    if not seo_analysis.suggestions_enabled(seo_rules):
        return _skipped("disabled via seo_rules.suggestions")

    api_key = ((api_keys or {}).get("mistral") or {}).get("api_key", "")
    if not api_key:
        return _skipped("no mistral API key configured")

    meta_limit = seo_analysis.meta_description_limit(seo_rules)
    title_limit = seo_analysis.title_limit(seo_rules)
    schema_default = ((pub_config or {}).get("rank_math") or {}).get(
        "default_schema_type"
    ) or _DEFAULT_SCHEMA_TYPE

    seo_result = seo_result or {}
    title_too_long = any(
        issue.get("type") == "title_too_long" for issue in seo_result.get("issues", [])
    )
    og_title_request = ""
    if title_too_long:
        og_title_request = (
            f"OG TITLE REQUESTED: the article title is "
            f"{seo_result.get('title_length', 0)} characters, over this "
            f"publication's {title_limit}-character ceiling. Suggest an "
            f"og_title of at most {title_limit} characters that keeps the "
            f"meaning and reads as a title, not a truncation."
        )

    user_prompt = _build_user_prompt(
        text, handoff, pub_config, meta_limit, og_title_request, schema_default
    )

    try:
        result = llm.call_provider(
            "mistral",
            _SYSTEM_PROMPT,
            user_prompt,
            api_key,
            model=_SUGGESTION_MODEL,
        )
    except Exception as e:
        log.warning("SEO suggestion call raised: %s", e)
        return {"status": "failed", "reason": f"suggestion call raised: {e}"}, None

    call_log_entry = cost.call_log_entry(CALL_LOG_PASS, result, _SUGGESTION_MODEL)

    if result.get("failed"):
        return _failed(
            f"suggestion call failed: {result.get('error', 'unknown error')}",
            call_log_entry,
        )

    data = result.get("data")
    if not isinstance(data, dict):
        return _failed("suggestion call returned no usable JSON object", call_log_entry)

    candidates = _clean_candidates(data.get("keyword_candidates"))
    meta_field = _field(
        "Meta description", value=data.get("meta_description"), limit=meta_limit
    )

    if not candidates and not meta_field["value"]:
        return _failed(
            "suggestion call returned neither keyword candidates nor a description",
            call_log_entry,
        )

    if title_too_long:
        og_title_field = _field(
            "OG title",
            value=data.get("og_title"),
            limit=title_limit,
            default_note=(
                "No shorter title proposed — the push would use the article "
                f"title as-is, at {seo_result.get('title_length', 0)} characters."
            ),
        )
    else:
        og_title_field = _field(
            "OG title",
            default_note=(
                f"The article title is used as-is — it is within the "
                f"{title_limit}-character ceiling, so no override is needed."
            ),
        )

    og_description = str(data.get("og_description") or "").strip()
    if og_description and _normalized(og_description) == _normalized(
        meta_field["value"]
    ):
        # A reworded copy of the meta description is not a second option, and
        # the push already falls back to the meta description on its own.
        og_description = ""
    og_description_field = _field(
        "OG description",
        value=og_description,
        limit=meta_limit,
        default_note=(
            "The meta description is used — no separate social-card framing "
            "was worth proposing."
        ),
    )

    suggestions = {
        "status": "ok",
        "model": call_log_entry["model"],
        "keyword_candidates": candidates,
        "fields": {
            "meta_description": meta_field,
            "og_title": og_title_field,
            "og_description": og_description_field,
            "schema_type": _schema_type_field(data, schema_default),
        },
    }

    proposed = [name for name in FIELD_ORDER if suggestions["fields"][name]["value"]]
    log.info(
        "SEO suggestions: %d keyword candidate(s); proposed %s",
        len(candidates),
        ", ".join(proposed) if proposed else "no single-value fields",
    )
    over = [
        suggestions["fields"][name]["label"]
        for name in FIELD_ORDER
        if suggestions["fields"][name]["over_limit"]
    ]
    if over:
        log.warning(
            "SEO suggestions over their character limit: %s. The model is told the "
            "ceiling in the prompt and overran it anyway (157/155 and 177/155 on "
            "consecutive runs) — the value is kept rather than truncated, because a "
            "machine cut at a word boundary reads worse than the author trimming it. "
            "It is reported with its count so the trim is a deliberate edit.",
            ", ".join(over),
        )
    return suggestions, call_log_entry
