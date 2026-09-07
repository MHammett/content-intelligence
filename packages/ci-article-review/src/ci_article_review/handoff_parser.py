"""
Parse Template A (draft submission) and Template C (publication handoff) documents.
"""

import re
import logging

log = logging.getLogger(__name__)


def _extract_section(text, header, next_headers=None):
    """Extract content between a header and the next known header.

    `next_headers` should include every OTHER header in the document's
    section set, not just the ones that come after `header` in canonical
    order. The regex below is non-greedy, so it already stops at whichever
    candidate header appears first in the actual text — passing only
    canonically-later headers as candidates is what let an out-of-order
    header (e.g. a chat model emitting two optional sections swapped) get
    swallowed into the preceding section instead of bounding it.

    Falls back to end-of-string if none of next_headers actually appear in
    the text (e.g. a boundary-only marker header, like DRAFT in
    METADATA_HEADERS, that isn't present in a metadata-only file) — otherwise
    the lookahead never matches and the section is silently dropped.
    """
    if next_headers:
        boundary = (
            r"(?=\n(?:" + "|".join(re.escape(h) for h in next_headers) + r")\s*\n|\Z)"
        )
    else:
        boundary = r"\Z"
    pattern = rf"^{re.escape(header)}\s*\n(.*?){boundary}"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


DRAFT_HEADERS = [
    "DRAFT SUBMISSION HANDOFF",
    "PRIMARY CLAIM",
    "TARGET AUDIENCE",
    "PRE-DRAFT ANALYSIS SUMMARY",
    "SOURCES ALREADY CITED",
    "UNCERTAIN SECTIONS",
    "KNOWN GAPS",
    "ADDITIONAL CONTEXT FOR REVIEW MODELS",
    "DRAFT",
]

PUB_HEADERS = [
    "PUBLICATION HANDOFF",
    "PUBLICATION PARAMETERS",
    "SEO METADATA",
    "EMBEDS AND SPECIAL ELEMENTS",
    "DISPOSITION LOG",
    "FINAL DRAFT",
]

# Same section set as DRAFT_HEADERS minus the leading banner and the DRAFT
# section itself — "DRAFT" stays in the list as a boundary marker only (not
# extracted) so a full handoff document can be pointed at --metadata without
# its article text leaking into ADDITIONAL CONTEXT.
METADATA_HEADERS = [
    "PRIMARY CLAIM",
    "TARGET AUDIENCE",
    "PRE-DRAFT ANALYSIS SUMMARY",
    "SOURCES ALREADY CITED",
    "UNCERTAIN SECTIONS",
    "KNOWN GAPS",
    "ADDITIONAL CONTEXT FOR REVIEW MODELS",
    "DRAFT",
]


#: Which domains each optional field reaches. Read from ``handoff_gaps`` rather
#: than restated here: the report's gap section and this debug line answer the
#: same question, and when they were two hand-maintained lists they disagreed —
#: this one said ``target_audience`` reached "voice_style and completeness",
#: while the prompt templates show four domains reasoning about audience.
_OPTIONAL_FIELD_IMPACT = (
    "sources_cited",
    "uncertain_sections",
    "known_gaps",
    "target_audience",
)


def _note_empty_optional_fields(results):
    """Debug-log which optional handoff fields came back empty.

    These are legitimately blank often enough that a warning would be noisy,
    but a rename or malformed header from a chat model produces the exact
    same empty string as a deliberate omission — so at least leave a debug
    trail of which review domains lose context as a result.

    The author-facing version of this — what each absence actually cost, and
    the line to paste to close it — is built by ``handoff_gaps.assess`` and
    rendered into the report itself. This stays a debug line.
    """
    from .handoff_gaps import FIELD_DOMAINS

    for field in _OPTIONAL_FIELD_IMPACT:
        if not results.get(field):
            domains = " and ".join(FIELD_DOMAINS.get(field, ()))
            log.debug(
                f"No '{field}' section found (or it was empty). "
                f"{domains} review will have less context as a result."
            )


def build_handoff_from_raw_text(text, source_name="Untitled"):
    """Synthesize a minimal handoff dict from a plain draft with no handoff headers.

    Mirrors build_handoff_from_url: the pipeline only strictly needs title and
    draft, so a file that is just the article body (e.g. pasted straight out of
    a chat session) can be run directly instead of requiring the full
    DRAFT SUBMISSION HANDOFF template. Optional fields (primary_claim,
    pre_draft_analysis, etc.) are left unset — review models get less context
    than a full handoff provides.
    """
    draft = text.strip()

    title_match = re.search(r"^#\s+(.+)$", draft, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else source_name

    if draft.lstrip().startswith("DRAFT SUBMISSION HANDOFF") or re.search(
        r"^PRIMARY CLAIM\s*$", draft, re.MULTILINE
    ):
        log.warning(
            "This file looks like it may already be a handoff document (found "
            "handoff-style headers) but is being read as a raw draft — its "
            "PRIMARY CLAIM, TARGET AUDIENCE, etc. sections will be ignored and "
            "the whole file will be sent as the draft text. Use --draft instead "
            "of --raw-draft if this file is already a full handoff document."
        )

    log.warning(
        "Running in raw-draft mode: only title and draft text were extracted. "
        "PRIMARY CLAIM, TARGET AUDIENCE, PRE-DRAFT ANALYSIS SUMMARY, and other "
        "handoff fields are empty, so review models will have less context than "
        "a full handoff document provides."
    )

    return {"title": title, "draft": draft, "run_number": 1}


def parse_metadata_only(text):
    """Parse a metadata-only file: PRIMARY CLAIM, TARGET AUDIENCE, etc. with no DRAFT.

    Same section format as parse_draft_submission, minus the draft text —
    for use with --raw-draft --metadata, where the article body lives in a
    separate file. If the metadata file happens to be a full handoff document
    (DRAFT section included), that section is used only as a boundary marker
    and its content is discarded here.
    """

    def section(header):
        next_h = [h for h in METADATA_HEADERS if h != header]
        return _extract_section(text, header, next_h or None)

    title = _extract_field(text, "Article:")
    publication = _extract_field(text, "Publication:")
    run_number = _extract_field(text, "Pipeline run:")
    # Optional. Names the model that drafted the article so the pipeline can
    # keep it out of voice_style — see _drafting_model() in pipeline.py.
    # Optional. Who first-person wording in the draft refers to, for citation
    # verification: an "I have a family." claim cannot be checked against a page
    # without knowing whose family. Falls back to the publication's own
    # author_name when absent, so single-author publications need not repeat it
    # per article; set it here for a guest or co-authored piece.
    author = _extract_field(text, "Author:")
    drafted_with = _extract_field(text, "Drafted with:")
    # Optional. Pins the history directory so revising the title does not fork
    # the article's history — see _history_key() in pipeline.py.
    history_key = _extract_field(text, "History key:")
    primary_claim = section("PRIMARY CLAIM")

    if not primary_claim:
        log.warning(
            "Metadata file is missing 'PRIMARY CLAIM'. "
            "The review models will receive an empty primary_claim — results may be generic or misdirected."
        )

    results = {
        "title": title,
        "publication": publication,
        "run_number": int(run_number) if run_number and run_number.isdigit() else 1,
        "primary_claim": primary_claim,
        "target_audience": section("TARGET AUDIENCE"),
        "pre_draft_analysis": section("PRE-DRAFT ANALYSIS SUMMARY"),
        "sources_cited": section("SOURCES ALREADY CITED"),
        "uncertain_sections": section("UNCERTAIN SECTIONS"),
        "known_gaps": section("KNOWN GAPS"),
        "additional_context": section("ADDITIONAL CONTEXT FOR REVIEW MODELS"),
        "author": author,
        "drafted_with": drafted_with,
        "history_key": history_key,
    }

    if not results["pre_draft_analysis"]:
        log.debug(
            "No PRE-DRAFT ANALYSIS SUMMARY found. "
            "Argument and completeness models will have less context — consider adding one."
        )
    _note_empty_optional_fields(results)

    return results


def build_handoff_from_raw_draft_and_metadata(
    draft_text, metadata_text, source_name="Untitled"
):
    """Combine a plain draft file with a separate metadata file into a full handoff dict.

    Lets the article body stay a single clean paste (no risk of a chat UI
    mangling the DRAFT section inside a much longer handoff document) while
    still supplying PRIMARY CLAIM / TARGET AUDIENCE / etc. for full review context.
    """
    draft = draft_text.strip()
    handoff = parse_metadata_only(metadata_text)

    if not handoff["title"]:
        title_match = re.search(r"^#\s+(.+)$", draft, re.MULTILINE)
        handoff["title"] = title_match.group(1).strip() if title_match else source_name

    handoff["draft"] = draft
    return handoff


def parse_draft_submission(text):
    def section(header):
        next_h = [h for h in DRAFT_HEADERS if h != header]
        return _extract_section(text, header, next_h or None)

    title = _extract_field(text, "Article:")
    publication = _extract_field(text, "Publication:")
    run_number = _extract_field(text, "Pipeline run:")
    # Optional. Names the model that drafted the article so the pipeline can
    # keep it out of voice_style — see _drafting_model() in pipeline.py.
    # Optional. Who first-person wording in the draft refers to, for citation
    # verification: an "I have a family." claim cannot be checked against a page
    # without knowing whose family. Falls back to the publication's own
    # author_name when absent, so single-author publications need not repeat it
    # per article; set it here for a guest or co-authored piece.
    author = _extract_field(text, "Author:")
    drafted_with = _extract_field(text, "Drafted with:")
    # Optional. Pins the history directory so revising the title does not fork
    # the article's history — see _history_key() in pipeline.py.
    history_key = _extract_field(text, "History key:")

    # Fields that directly fill prompt template variables — warn if missing
    # so the user knows before a model call produces oddly generic output.
    _REQUIRED_FIELDS = {
        "title": ("Article:", title),
        "primary_claim": ("PRIMARY CLAIM", section("PRIMARY CLAIM")),
        "draft": ("DRAFT", section("DRAFT")),
    }
    for field, (label, value) in _REQUIRED_FIELDS.items():
        if not value:
            log.warning(
                f"Handoff document is missing '{label}'. "
                f"The review models will receive an empty {field} — results may be generic or misdirected."
            )

    # Advisory fields — missing is common and acceptable, but worth noting at debug level
    _ADVISORY_FIELDS = {
        "pre_draft_analysis": (
            "PRE-DRAFT ANALYSIS SUMMARY",
            section("PRE-DRAFT ANALYSIS SUMMARY"),
        ),
    }

    results = {
        "title": title,
        "publication": publication,
        "run_number": int(run_number) if run_number and run_number.isdigit() else 1,
        "primary_claim": _REQUIRED_FIELDS["primary_claim"][1],
        "target_audience": section("TARGET AUDIENCE"),
        "pre_draft_analysis": _ADVISORY_FIELDS["pre_draft_analysis"][1],
        "sources_cited": section("SOURCES ALREADY CITED"),
        "uncertain_sections": section("UNCERTAIN SECTIONS"),
        "known_gaps": section("KNOWN GAPS"),
        "additional_context": section("ADDITIONAL CONTEXT FOR REVIEW MODELS"),
        "author": author,
        "drafted_with": drafted_with,
        "history_key": history_key,
        "draft": _REQUIRED_FIELDS["draft"][1],
    }

    if not results["pre_draft_analysis"]:
        log.debug(
            "No PRE-DRAFT ANALYSIS SUMMARY found. "
            "Argument and completeness models will have less context — consider adding one."
        )
    _note_empty_optional_fields(results)

    return results


def parse_publication_handoff(text):
    def section(header):
        next_h = [h for h in PUB_HEADERS if h != header]
        return _extract_section(text, header, next_h or None)

    title = _extract_field(text, "Article:")
    publication = _extract_field(text, "Publication:")

    pub_params_raw = section("PUBLICATION PARAMETERS")
    seo_raw = section("SEO METADATA")

    return {
        "title": title,
        "publication": publication,
        "publication_parameters": _parse_key_value_block(pub_params_raw),
        "seo": _parse_seo_block(seo_raw),
        "embeds": section("EMBEDS AND SPECIAL ELEMENTS"),
        "disposition_log": section("DISPOSITION LOG"),
        "final_draft": section("FINAL DRAFT"),
    }


def _extract_field(text, label):
    match = re.search(rf"^{re.escape(label)}\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_key_value_block(text):
    result = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip().lower().replace(" ", "_")] = value.strip()
    return result


def _parse_seo_block(text):
    seo = {}
    field_map = {
        "Focus keyword": "focus_keyword",
        "Meta description": "meta_description",
        "OG title": "og_title",
        "OG description": "og_description",
        "Schema type": "schema_type",
    }
    for label, key in field_map.items():
        match = re.search(rf"^{re.escape(label)}:\s*(.+)$", text, re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if not value.startswith("derive") and not value.startswith("use "):
                seo[key] = value
    return seo
