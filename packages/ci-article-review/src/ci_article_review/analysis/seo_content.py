"""Judge an article's structure from the search reader's side — one cheap call.

``analysis.seo`` checks what is mechanically checkable: heading counts, title
length, alt text, link text. None of that can tell you whether a heading
actually describes the section under it, or whether someone who clicked a
search result finds what they came for in the opening.

Deliberately narrow, because the review ensemble is already large. This pass
answers only questions asked from outside the article, by someone who arrived
from a search result and has not decided to stay:

  * Does each heading tell a scanning reader what is below it?
  * Does the opening deliver what the title and keyword promise, or bury it?
  * Does the article deliver what the title claims?

The completeness domain already asks what a reader needs and is missing; the
voice domain already flags hollow phrasing. This pass is told, in its prompt,
not to re-raise either — and told that finding nothing is a correct answer, so
a clean draft produces an empty list rather than manufactured problems.

Same call shape as ``seo_suggest``: a small fast model, cost logged under its
own pass name, and any failure degrading to "no review this run".
"""

import logging

from ci_core import redact
from ci_core import llm

from . import seo as seo_analysis
from ci_core.llm import cost

log = logging.getLogger(__name__)

#: Same cheap/fast model the suggestion pass and the citation verifier use.
_REVIEW_MODEL = "mistral-small-latest"

#: Cost-summary pass name, separate from ``seo_suggestions`` so the two calls
#: are individually attributable.
CALL_LOG_PASS = "seo_content_review"

#: More body text than the suggestion pass gets: judging whether headings match
#: their sections needs the sections, not just their titles.
_BODY_HEAD_CHARS = 6000
_BODY_TAIL_CHARS = 1000

#: A long list of structural nits is a list nobody acts on.
_MAX_FINDINGS = 8

#: Finding categories this pass may return. Anything else is dropped — an
#: unconstrained category turns into a second, unreviewed taxonomy.
_KNOWN_FINDING_TYPES = ("heading", "opening", "title_promise")

_SYSTEM_PROMPT = (
    "You assess an article's structure from the point of view of someone who "
    "just arrived from a search result and has not yet decided to stay. You "
    "are not reviewing the writing, the argument, or the facts — other passes "
    "do that.\n"
    "Answer ONLY these three questions:\n"
    "1. heading — does each heading tell a scanning reader what is in the "
    "section below it? Flag headings that are vague, cute, or could sit above "
    "any section of any article.\n"
    "2. opening — does the opening deliver what the title and the target "
    "keyword promise, or does it warm up first and bury the answer?\n"
    "3. title_promise — does the article deliver what the title claims? Flag a "
    "title that promises more, or something different, than the piece gives.\n"
    "Respond with ONLY a JSON object of the form "
    '{"findings": [{"type": "heading" | "opening" | "title_promise", '
    '"target": "<the heading or passage this is about, quoted exactly, or an '
    'empty string>", "problem": "<one sentence>", "suggestion": "<one concrete '
    'alternative>"}]}.\n'
    "Rules:\n"
    "- Return an EMPTY findings list when the article is structurally sound. "
    "That is a correct and expected answer. Do not manufacture problems to "
    "fill the list, and do not restate a strength as a finding.\n"
    "- Do NOT flag missing information, weak arguments, factual doubts, or "
    "tone and phrasing. Other review passes cover all of those, and repeating "
    "them here buries the structural findings.\n"
    "- Every suggestion must be concrete enough to act on: an actual "
    "replacement heading, not 'make it more descriptive'.\n"
    "- Quote 'target' exactly as it appears in the article so the author can "
    "find it."
)


def _clean_findings(raw):
    """Keep well-formed findings of a known type, capped.

    A finding with no stated problem is not usable, and a type outside the
    three this pass asks about means the model answered a question it wasn't
    asked — most likely one of the domains the prompt told it to leave alone.
    """
    findings = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        finding_type = str(item.get("type", "")).strip().lower()
        problem = str(item.get("problem", "")).strip()
        if finding_type not in _KNOWN_FINDING_TYPES or not problem:
            continue
        findings.append(
            {
                "type": finding_type,
                "target": str(item.get("target", "")).strip(),
                "problem": problem,
                "suggestion": str(item.get("suggestion", "")).strip(),
            }
        )
        if len(findings) == _MAX_FINDINGS:
            break
    return findings


def _build_user_prompt(text, handoff, keyword_candidates):
    handoff = handoff or {}
    parts = [f"ARTICLE TITLE: {handoff.get('title', '')}"]
    if handoff.get("primary_claim"):
        parts.append(f"PRIMARY CLAIM: {handoff['primary_claim']}")
    if handoff.get("target_audience"):
        parts.append(f"TARGET AUDIENCE: {handoff['target_audience']}")

    if keyword_candidates:
        phrases = ", ".join(
            c["keyword"] for c in keyword_candidates[:5] if c.get("keyword")
        )
        if phrases:
            parts.append(
                "KEYWORD PHRASES UNDER CONSIDERATION (no one has chosen yet — "
                f"judge the opening against what a searcher for these wants): {phrases}"
            )

    parts.append(
        "ARTICLE:\n"
        + redact.truncate_excerpt(
            text or "", head=_BODY_HEAD_CHARS, tail=_BODY_TAIL_CHARS
        )
    )
    return "\n\n".join(parts)


def review(text, handoff=None, pub_config=None, api_keys=None, suggestions=None):
    """Assess structure for a search reader. Returns ``(result, call_log_entry)``.

    ``suggestions`` is the output of ``seo_suggest.generate``; its keyword
    candidates give this pass the search intent to judge the opening against.
    It is optional — without it the pass still runs, just without that framing.

    ``result`` always has a ``status``: ``ok`` (with ``findings``, possibly
    empty — a structurally sound article is the expected happy path),
    ``skipped`` when no call was made, or ``failed``. Never raises.
    """
    seo_rules = (pub_config or {}).get("seo_rules") or {}
    if not seo_analysis.content_review_enabled(seo_rules):
        reason = "disabled via seo_rules.content_review"
        log.info("SEO content review: %s", reason)
        return {"status": "skipped", "reason": reason}, None

    api_key = ((api_keys or {}).get("mistral") or {}).get("api_key", "")
    if not api_key:
        reason = "no mistral API key configured"
        log.info("SEO content review: %s", reason)
        return {"status": "skipped", "reason": reason}, None

    candidates = (suggestions or {}).get("keyword_candidates") or []
    user_prompt = _build_user_prompt(text, handoff, candidates)

    try:
        result = llm.call_provider(
            "mistral", _SYSTEM_PROMPT, user_prompt, api_key, model=_REVIEW_MODEL
        )
    except Exception as e:
        log.warning("SEO content review call raised: %s", e)
        return {"status": "failed", "reason": f"content review raised: {e}"}, None

    call_log_entry = cost.call_log_entry(CALL_LOG_PASS, result, _REVIEW_MODEL)

    if result.get("failed"):
        reason = f"content review failed: {result.get('error', 'unknown error')}"
        log.warning("SEO content review unavailable — %s", reason)
        return {"status": "failed", "reason": reason}, call_log_entry

    data = result.get("data")
    if not isinstance(data, dict):
        reason = "content review returned no usable JSON object"
        log.warning("SEO content review unavailable — %s", reason)
        return {"status": "failed", "reason": reason}, call_log_entry

    findings = _clean_findings(data.get("findings"))
    log.info(
        "SEO content review: %d structural finding(s)%s",
        len(findings),
        "" if findings else " — nothing flagged",
    )
    return {
        "status": "ok",
        "model": call_log_entry["model"],
        "findings": findings,
    }, call_log_entry
