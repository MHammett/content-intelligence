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
    "OUT OF SCOPE FOR FACT-CHECK",
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
    "OUT OF SCOPE FOR FACT-CHECK",
    "KNOWN GAPS",
    "ADDITIONAL CONTEXT FOR REVIEW MODELS",
    "DRAFT",
]


#: Which domains each optional field reaches. Read from ``handoff_gaps`` rather
#: than restated here: the report's gap section and this debug line answer the
#: same question, and when they were two hand-maintained lists they disagreed —
#: this one said ``target_audience`` reached "voice_style and completeness",
#: while the prompt templates show four domains reasoning about audience.
#:
#: ``out_of_scope`` is deliberately absent. Every other field here is one whose
#: absence costs the run something on every draft, which is what makes a debug
#: line about it useful. Most articles have nothing that no source can settle,
#: so an empty OUT OF SCOPE FOR FACT-CHECK section is the normal case rather
#: than a gap, and reporting it as one would be noise on most runs. The signal
#: that the section *should* have been filled in is not its emptiness — it is
#: the fact-check models classifying claims out of scope on their own, which
#: Section 2 reports directly. See :mod:`ci_article_review.fact_check_scope`.
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
        "out_of_scope": section("OUT OF SCOPE FOR FACT-CHECK"),
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
        "out_of_scope": section("OUT OF SCOPE FOR FACT-CHECK"),
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

    publication_parameters, placeholder_publication_fields = _parse_key_value_block(
        pub_params_raw
    )

    return {
        "title": title,
        "publication": publication,
        "publication_parameters": publication_parameters,
        # Which of publication_parameters' raw fields (wordpress_category,
        # tags) are still on the template's bracketed placeholder, so
        # run_publish_pipeline can refuse rather than publish uncategorised.
        # See _RAW_PUBLICATION_PARAMETER_FIELDS.
        "placeholder_publication_fields": placeholder_publication_fields,
        "seo": _parse_seo_block(seo_raw),
        "ignored_schema_type": _ignored_schema_type(seo_raw),
        "embeds": section("EMBEDS AND SPECIAL ELEMENTS"),
        "disposition_log": section("DISPOSITION LOG"),
        "final_draft": section("FINAL DRAFT"),
    }


def _extract_raw_field(text, label):
    """The single-line, first-occurrence value of a label — placeholder or not.

    Split out of ``_extract_field`` so a caller that must tell a bracketed
    placeholder apart from a genuinely blank line can reuse the same anchored
    extraction without going through the collapse-to-blank step below. See
    ``_parse_key_value_block``'s ``wordpress_category`` and ``tags``.
    """
    # [ \t]*, not \s*: \s crosses the newline, so a label left blank took the
    # whole next line as its value — a blank "Author:" above "History key:
    # a-piece" became the author "History key: a-piece", and citation
    # verification was told that is who "I" refers to.
    #
    # (.*), not (.+). This searches the whole document, so a blank label that
    # failed to match here would let the search run on to the next line that
    # starts with the same label: an "Author:" in SOURCES ALREADY CITED, or in
    # the draft itself. The first occurrence is the field, and blank reads as
    # "", the same as absent.
    match = re.search(rf"^{re.escape(label)}[ \t]*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _log_unset_placeholder(label, value):
    log.info(
        "Handoff field %r is not set: its value is bracketed, so it reads as "
        "the template's placeholder. Remove the brackets if it is real: %s",
        label,
        value,
    )


def _extract_field(text, label):
    # A value still on its template placeholder reads as "" too. The templates'
    # placeholders for these labels are bracketed, and a label left on one was
    # read as real: the unfilled draft template's History key named the
    # pipeline_history directory, so every handoff that left it shared one;
    # its Author was what citation verification was told "I" is; and
    # publication.md's "Article: [title]" became the WordPress post title. The
    # first occurrence is still the field when it is a placeholder, so this
    # never falls through to a later line with the same label either.
    value = _extract_raw_field(text, label)
    if _is_bracketed_placeholder(value):
        _log_unset_placeholder(label, value)
        return ""
    return value


#: The label for each PUBLICATION PARAMETERS field pipeline.py reads.
#: "WordPress author:" is the current spelling; "Author:" is the original and
#: still parsed, because existing publication handoffs use it — see
#: TestTheTwoAuthorLabelsAreDistinct. "Status:" is not read by anything; the
#: line exists to remind whoever fills in the template that ``--publish-live``
#: is the actual switch, not this field.
_PUBLICATION_PARAMETER_LABELS = {
    "status": "Status:",
    "post_type": "Post type:",
    "wordpress_category": "WordPress category:",
    "tags": "Tags:",
    "wordpress_author": "WordPress author:",
    "author": "Author:",
}

#: Fields read raw rather than through _extract_field's placeholder collapse.
#:
#: Every other PUBLICATION PARAMETERS field has a safe fallback for "unset":
#: Post type defaults to post, and WordPress author (with the legacy Author)
#: falls back to the authenticated WordPress user. Collapsing an unfilled
#: placeholder to the same "" a blank line produces is exactly right for
#: those. Category and tags do not have one — an absent category is never
#: checked, so a post with none silently files under WordPress's own
#: "Uncategorized", live or not. Collapsing their placeholder to "" would
#: trade today's (accidental) protection — the placeholder text fails a live
#: WordPress term lookup and refuses the publish — for a silent uncategorised
#: one instead. run_publish_pipeline checks these two for a placeholder
#: itself, via parse_publication_handoff's "placeholder_publication_fields",
#: and refuses before anything is sent.
_RAW_PUBLICATION_PARAMETER_FIELDS = ("wordpress_category", "tags")


def _parse_key_value_block(text):
    """Parse the PUBLICATION PARAMETERS fields pipeline.py reads.

    This used to split every "key: value" line in the section into a dict
    entry — fine while every field fit on one line, until a placeholder
    wrapped onto more: "Post type: [post (default) | page -- use page for
    standing pages such as" is one line, but its continuation, "About or
    Contact: no date, no category, not in the blog feed. A page", has its own
    colon too, so it became its own key ("about_or_contact") that nothing
    reads — harmless clutter today only because no real field happens to
    collide with a continuation line's text, not a rule anything enforces.

    Each field is looked up by its own label instead, anchored to the start
    of a line the same way a header field or an SEO METADATA field is, so a
    continuation line is never mistaken for a field of its own, and the first
    occurrence wins.

    Returns ``(params, placeholder_fields)``: the parsed dict (every key in
    ``_PUBLICATION_PARAMETER_LABELS``, always present, "" when absent or
    blank), and the subset of ``_RAW_PUBLICATION_PARAMETER_FIELDS`` whose
    value is still a bracketed placeholder.
    """
    params = {}
    placeholder_fields = set()
    for key, label in _PUBLICATION_PARAMETER_LABELS.items():
        if key in _RAW_PUBLICATION_PARAMETER_FIELDS:
            raw = _extract_raw_field(text, label)
            if _is_bracketed_placeholder(raw):
                _log_unset_placeholder(label, raw)
                placeholder_fields.add(key)
            params[key] = raw
        else:
            params[key] = _extract_field(text, label)
    return params, placeholder_fields


def _parse_seo_block(text):
    seo = {}
    field_map = {
        "Focus keyword": "focus_keyword",
        "SEO title": "seo_title",
        "Meta description": "meta_description",
        "OG title": "og_title",
        "OG description": "og_description",
    }
    for label, key in field_map.items():
        # Read exactly as a header field is. A blank label is blank, not the
        # next line: a blank "SEO title:" once put "Meta description: ..." in
        # the <title> tag. And the first line with the label is the field,
        # whether or not a blank one was left with a trailing space; under
        # (.+) that invisible space decided whether a later line was read. A
        # bracketed placeholder comes back blank as well; _extract_field logs it.
        value = _extract_field(text, f"{label}:")
        # The template's own alternatives, written out without their brackets
        # ("derive from primary claim", "use article title"), also mean "leave
        # this to the default".
        if value and not value.startswith("derive") and not value.startswith("use "):
            seo[key] = value
    return seo


def _is_bracketed_placeholder(value):
    """True when a label's value is still one of the template's ``[...]``.

    Every header and SEO METADATA placeholder in the templates is bracketed,
    and a field left on one was read as real. The unfilled publication
    template's focus keyword went to Rank Math as "[keyword or phrase | or:
    derive from primary claim]" while the publish reported success.

    The rule is structural. The value opens with "[", and either closes it
    only as its last character or never closes it. The unclosed case is a
    placeholder that wraps onto further lines, as the SEO title's does and the
    draft template's Author, History key and Drafted with do. Only a label's
    own line is read, so its "]" is never seen.

    "Starts with [" alone would be simpler, and would be safe for most labels:
    a focus keyword, an author or a history key has no reason to open with a
    bracket. A title can. A leading tag such as "[Case Study] How We Cut
    Costs" is a common click-through device, and some sites put shortcodes
    such as "[year]" in SEO titles. So a value that only begins with a
    bracketed span is kept. What the rule cannot tell from a placeholder is a
    value typed inside the brackets, "[fiber buildout]". That is dropped too,
    and ``_extract_field`` logs it by label so the author can see which it was.
    """
    if not value.startswith("["):
        return False
    close = value.find("]")
    return close in (-1, len(value) - 1)


#: The SEO METADATA line the template used to offer for structured data. It is
#: not an SEO field: nothing sets schema from a handoff (see
#: ``adapters.cms.wordpress.rank_math_meta``), so it never enters ``seo``. It
#: is still looked for, because handoffs written from an older copy of the
#: template carry it, and a line dropped in silence reads as a line applied.
_SCHEMA_TYPE_LINE = re.compile(r"^Schema type:[ \t]*(.*)$", re.MULTILINE)


def _ignored_schema_type(text):
    """The value of a leftover ``Schema type:`` line, or "" if there is none."""
    match = _SCHEMA_TYPE_LINE.search(text)
    return match.group(1).strip() if match else ""
