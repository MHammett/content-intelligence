"""Tests for handoff_parser — raw-draft / metadata-only handoff synthesis.

Covers build_handoff_from_raw_text, parse_metadata_only, and
build_handoff_from_raw_draft_and_metadata, added alongside the --raw-draft /
--metadata CLI flags (commit 803440c) but previously untested.
"""

from pathlib import Path

import pytest

from ci_article_review import handoff_parser
from ci_article_review.handoff_parser import (
    build_handoff_from_raw_draft_and_metadata,
    build_handoff_from_raw_text,
    parse_draft_submission,
    parse_metadata_only,
    parse_publication_handoff,
)


class TestBuildHandoffFromRawText:
    def test_basic_case(self):
        text = "Just some plain article text with no headers at all."
        handoff = build_handoff_from_raw_text(text, source_name="my-draft")
        assert handoff == {
            "title": "my-draft",
            "draft": text,
            "run_number": 1,
        }

    def test_h1_title_extracted(self):
        text = "# The Real Title\n\nBody text follows here."
        handoff = build_handoff_from_raw_text(text, source_name="fallback-name")
        assert handoff["title"] == "The Real Title"
        assert handoff["draft"] == text.strip()

    def test_falls_back_to_source_name_without_h1(self):
        text = "No heading here, just body copy."
        handoff = build_handoff_from_raw_text(text, source_name="fallback-name")
        assert handoff["title"] == "fallback-name"

    def test_falls_back_to_default_source_name(self):
        text = "No heading, no source_name passed."
        handoff = build_handoff_from_raw_text(text)
        assert handoff["title"] == "Untitled"

    def test_warns_when_text_looks_like_full_handoff_banner(self, caplog):
        text = "DRAFT SUBMISSION HANDOFF\nGenerated: 2026-06-08\n\nSome content."
        with caplog.at_level("WARNING"):
            handoff = build_handoff_from_raw_text(text)
        assert any("already be a handoff document" in r.message for r in caplog.records)
        # The whole file is still treated as draft body.
        assert handoff["draft"] == text.strip()

    def test_warns_when_text_contains_primary_claim_header(self, caplog):
        text = "Some intro.\nPRIMARY CLAIM\nThis is the claim.\n"
        with caplog.at_level("WARNING"):
            build_handoff_from_raw_text(text)
        assert any("already be a handoff document" in r.message for r in caplog.records)

    def test_no_handoff_warning_for_plain_draft(self, caplog):
        text = "Just a normal article with no special headers."
        with caplog.at_level("WARNING"):
            build_handoff_from_raw_text(text)
        assert not any(
            "already be a handoff document" in r.message for r in caplog.records
        )

    def test_always_warns_that_fields_are_empty(self, caplog):
        text = "Plain draft text."
        with caplog.at_level("WARNING"):
            build_handoff_from_raw_text(text)
        assert any(
            "PRIMARY CLAIM, TARGET AUDIENCE" in r.message for r in caplog.records
        )


SAMPLE_METADATA = """DRAFT SUBMISSION HANDOFF
Generated: 2026-06-08
Pipeline run: 3
Article: How Fiber Reaches Rural Towns
Publication: mikehammett

PRIMARY CLAIM
Rural fiber buildouts are economically viable once subsidy math is right.

TARGET AUDIENCE
Municipal officials and local journalists.

PRE-DRAFT ANALYSIS SUMMARY
Steelmanned position: subsidies distort the market.

SOURCES ALREADY CITED
FCC broadband map, 2025 edition.

UNCERTAIN SECTIONS
The cost-per-mile figures in paragraph 4.

KNOWN GAPS
No discussion of satellite alternatives.

ADDITIONAL CONTEXT FOR REVIEW MODELS
Prior articles covered urban buildouts; this one is rural-focused.
"""


class TestParseMetadataOnly:
    def test_parses_all_fields(self):
        result = parse_metadata_only(SAMPLE_METADATA)
        assert result["title"] == "How Fiber Reaches Rural Towns"
        assert result["publication"] == "mikehammett"
        assert result["run_number"] == 3
        assert (
            result["primary_claim"]
            == "Rural fiber buildouts are economically viable once subsidy math is right."
        )
        assert result["target_audience"] == "Municipal officials and local journalists."
        assert (
            result["pre_draft_analysis"]
            == "Steelmanned position: subsidies distort the market."
        )
        assert result["sources_cited"] == "FCC broadband map, 2025 edition."
        assert (
            result["uncertain_sections"] == "The cost-per-mile figures in paragraph 4."
        )
        assert result["known_gaps"] == "No discussion of satellite alternatives."
        assert (
            result["additional_context"]
            == "Prior articles covered urban buildouts; this one is rural-focused."
        )
        assert "draft" not in result

    def test_missing_primary_claim_warns(self, caplog):
        text = "Article: Some Title\n\nTARGET AUDIENCE\nEveryone.\n"
        with caplog.at_level("WARNING"):
            result = parse_metadata_only(text)
        assert result["primary_claim"] == ""
        assert any("PRIMARY CLAIM" in r.message for r in caplog.records)

    def test_missing_pre_draft_analysis_debug_logs(self, caplog):
        text = (
            "Article: Some Title\n\nPRIMARY CLAIM\nThe claim.\n\n"
            "TARGET AUDIENCE\nEveryone.\n"
        )
        with caplog.at_level("DEBUG"):
            result = parse_metadata_only(text)
        assert result["pre_draft_analysis"] == ""
        assert any(
            "PRE-DRAFT ANALYSIS SUMMARY" in r.message and r.levelname == "DEBUG"
            for r in caplog.records
        )

    def test_last_section_not_lost_without_trailing_draft_marker(self):
        # Regression test for the _extract_section boundary-fix bug: a
        # metadata-only file with no trailing DRAFT marker must still
        # extract its final section (ADDITIONAL CONTEXT FOR REVIEW MODELS)
        # instead of silently losing it because the lookahead required the
        # next header to literally appear in the text.
        text = (
            "Article: Some Title\n\n"
            "PRIMARY CLAIM\nThe claim.\n\n"
            "ADDITIONAL CONTEXT FOR REVIEW MODELS\n"
            "This is the final section with no DRAFT marker after it.\n"
        )
        result = parse_metadata_only(text)
        assert (
            result["additional_context"]
            == "This is the final section with no DRAFT marker after it."
        )


class TestBuildHandoffFromRawDraftAndMetadata:
    def test_combines_draft_and_metadata(self):
        draft_text = "This is the plain article body, no headers."
        handoff = build_handoff_from_raw_draft_and_metadata(
            draft_text, SAMPLE_METADATA, source_name="fallback"
        )
        assert handoff["draft"] == draft_text
        assert handoff["title"] == "How Fiber Reaches Rural Towns"
        assert (
            handoff["primary_claim"]
            == "Rural fiber buildouts are economically viable once subsidy math is right."
        )

    def test_title_falls_back_to_draft_h1_when_metadata_has_no_article_line(self):
        draft_text = "# Draft-Derived Title\n\nBody text."
        metadata_text = "PRIMARY CLAIM\nThe claim.\n"
        handoff = build_handoff_from_raw_draft_and_metadata(
            draft_text, metadata_text, source_name="fallback"
        )
        assert handoff["title"] == "Draft-Derived Title"

    def test_title_falls_back_to_source_name_without_article_or_h1(self):
        draft_text = "Just body text, no heading."
        metadata_text = "PRIMARY CLAIM\nThe claim.\n"
        handoff = build_handoff_from_raw_draft_and_metadata(
            draft_text, metadata_text, source_name="fallback-name"
        )
        assert handoff["title"] == "fallback-name"

    def test_metadata_article_line_wins_over_draft_h1(self):
        draft_text = "# Draft Title\n\nBody text."
        handoff = build_handoff_from_raw_draft_and_metadata(
            draft_text, SAMPLE_METADATA, source_name="fallback"
        )
        assert handoff["title"] == "How Fiber Reaches Rural Towns"


SAMPLE_DRAFT_SUBMISSION = """DRAFT SUBMISSION HANDOFF
Generated: 2026-08-09
Pipeline run: 2
Article: Test Article
Publication: mikehammett

PRIMARY CLAIM
Some claim.

TARGET AUDIENCE
General readers.

PRE-DRAFT ANALYSIS SUMMARY
Analysis here.

SOURCES ALREADY CITED
Source A, Source B.

KNOWN GAPS
This is the known gaps content.

UNCERTAIN SECTIONS
This is the uncertain sections content.

ADDITIONAL CONTEXT FOR REVIEW MODELS
Context here.

DRAFT
The actual article body.
"""


class TestParseDraftSubmission:
    def test_parses_in_canonical_order(self):
        result = parse_draft_submission(SAMPLE_DRAFT_SUBMISSION)
        assert result["title"] == "Test Article"
        assert result["known_gaps"] == "This is the known gaps content."
        assert result["uncertain_sections"] == "This is the uncertain sections content."
        assert result["draft"] == "The actual article body."

    def test_swapped_known_gaps_and_uncertain_sections_dont_bleed(self):
        # Regression test: KNOWN GAPS and UNCERTAIN SECTIONS swapped from
        # canonical order (e.g. a chat model regenerating metadata per
        # handoff_templates/revise_after_review_prompt.md). Before the fix,
        # KNOWN GAPS's boundary regex only knew about canonically-later
        # headers, so it skipped past the out-of-order UNCERTAIN SECTIONS
        # header and swallowed it whole.
        doc = SAMPLE_DRAFT_SUBMISSION.replace(
            "KNOWN GAPS\nThis is the known gaps content.\n\n"
            "UNCERTAIN SECTIONS\nThis is the uncertain sections content.\n\n",
            "UNCERTAIN SECTIONS\nThis is the uncertain sections content.\n\n"
            "KNOWN GAPS\nThis is the known gaps content.\n\n",
        )
        result = parse_draft_submission(doc)
        assert result["known_gaps"] == "This is the known gaps content."
        assert result["uncertain_sections"] == "This is the uncertain sections content."

    def test_swapped_sources_cited_and_target_audience_dont_bleed(self):
        doc = SAMPLE_DRAFT_SUBMISSION.replace(
            "TARGET AUDIENCE\nGeneral readers.\n\n"
            "PRE-DRAFT ANALYSIS SUMMARY\nAnalysis here.\n\n"
            "SOURCES ALREADY CITED\nSource A, Source B.\n\n",
            "SOURCES ALREADY CITED\nSource A, Source B.\n\n"
            "TARGET AUDIENCE\nGeneral readers.\n\n"
            "PRE-DRAFT ANALYSIS SUMMARY\nAnalysis here.\n\n",
        )
        result = parse_draft_submission(doc)
        assert result["target_audience"] == "General readers."
        assert result["pre_draft_analysis"] == "Analysis here."
        assert result["sources_cited"] == "Source A, Source B."

    def test_missing_sources_cited_debug_logs(self, caplog):
        text = "Article: Some Title\n\nPRIMARY CLAIM\nThe claim.\n\nDRAFT\nBody.\n"
        with caplog.at_level("DEBUG"):
            result = parse_draft_submission(text)
        assert result["sources_cited"] == ""
        assert any(
            "sources_cited" in r.message and r.levelname == "DEBUG"
            for r in caplog.records
        )

    def test_missing_target_audience_debug_logs(self, caplog):
        text = "Article: Some Title\n\nPRIMARY CLAIM\nThe claim.\n\nDRAFT\nBody.\n"
        with caplog.at_level("DEBUG"):
            result = parse_draft_submission(text)
        assert result["target_audience"] == ""
        assert any(
            "target_audience" in r.message and r.levelname == "DEBUG"
            for r in caplog.records
        )


SAMPLE_PUB_HANDOFF = """PUBLICATION HANDOFF
Generated: 2026-08-09
Article: Test Article
Publication: mikehammett

PUBLICATION PARAMETERS
category: news

SEO METADATA
Focus keyword: fiber

EMBEDS AND SPECIAL ELEMENTS
An embedded chart.

DISPOSITION LOG
Approved by editor.

FINAL DRAFT
The final article body.
"""


class TestParsePublicationHandoff:
    def test_parses_in_canonical_order(self):
        result = parse_publication_handoff(SAMPLE_PUB_HANDOFF)
        assert result["embeds"] == "An embedded chart."
        assert result["disposition_log"] == "Approved by editor."
        assert result["final_draft"] == "The final article body."

    def test_swapped_embeds_and_disposition_log_dont_bleed(self):
        doc = SAMPLE_PUB_HANDOFF.replace(
            "EMBEDS AND SPECIAL ELEMENTS\nAn embedded chart.\n\n"
            "DISPOSITION LOG\nApproved by editor.\n\n",
            "DISPOSITION LOG\nApproved by editor.\n\n"
            "EMBEDS AND SPECIAL ELEMENTS\nAn embedded chart.\n\n",
        )
        result = parse_publication_handoff(doc)
        assert result["embeds"] == "An embedded chart."
        assert result["disposition_log"] == "Approved by editor."


class TestSeoMetadataBlock:
    def _parse(self, seo_lines):
        return parse_publication_handoff(
            "PUBLICATION HANDOFF\nArticle: T\n\n"
            f"SEO METADATA\n{seo_lines}\n\n"
            "FINAL DRAFT\nBody.\n"
        )

    def test_a_blank_field_does_not_take_the_next_line(self):
        """Blank means "use the default", not "use the line below".

        The value pattern allowed any whitespace after the colon, newlines
        included, so a blank "SEO title:" read "Meta description: ..." as its
        value — which the push would then send to Rank Math as the <title>.
        """
        seo = self._parse(
            "SEO title:\n"
            "Meta description: A description.\n"
            "OG description:\n"
            "OG title: A title"
        )["seo"]
        assert seo == {"meta_description": "A description.", "og_title": "A title"}

    def test_placeholder_values_are_still_dropped(self):
        seo = self._parse(
            "Focus keyword: derive from primary claim\nOG title: use article title"
        )["seo"]
        assert seo == {}

    #: The five fields as publication.md writes them, unfilled. The SEO title's
    #: placeholder wraps onto further lines there, so its "]" is not on the
    #: label's line.
    _UNFILLED = (
        "Focus keyword: [keyword or phrase | or: derive from primary claim]\n"
        "SEO title: [the search-facing title, 20-60 chars | or: use OG title.\n"
        "Separate from the post title.]\n"
        "Meta description: [under 155 characters | or: derive from opening paragraph]\n"
        "OG title: [or: use article title]\n"
        "OG description: [or: use meta description]"
    )

    def test_bracketed_placeholders_are_dropped(self):
        """The template writes every placeholder in brackets, and only the
        bare "derive"/"use" forms were dropped. Reproduced 2026-09-18: the
        unfilled template sent all five to Rank Math verbatim."""
        assert self._parse(self._UNFILLED)["seo"] == {}

    def test_filled_fields_survive_next_to_placeholders(self):
        result = self._parse("Focus keyword: fiber\nOG title: [or: use article title]")
        assert result["seo"] == {"focus_keyword": "fiber"}

    @pytest.mark.parametrize(
        "title", ["[Case Study] How We Cut Fiber Costs", "[2026] Fiber Costs [Guide]"]
    )
    def test_a_value_that_only_begins_with_a_bracketed_tag_is_kept(self, title):
        """A leading bracketed tag is real title practice, not a placeholder.
        Only a value that is one bracketed span, or never closes its bracket,
        reads as one."""
        assert self._parse(f"SEO title: {title}")["seo"] == {"seo_title": title}

    def test_a_value_typed_inside_the_brackets_is_dropped_and_logged(self, caplog):
        """It cannot be told from a placeholder, so it is dropped like one.
        The log names it, so the author can see the value was theirs."""
        with caplog.at_level("INFO", logger="ci_article_review.handoff_parser"):
            seo = self._parse("Focus keyword: [fiber buildout]")["seo"]
        assert seo == {}
        assert any(
            "Focus keyword" in r.getMessage() and "[fiber buildout]" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.parametrize("blank", ["SEO title:", "SEO title: "])
    def test_a_blank_field_does_not_borrow_a_later_line_with_that_label(self, blank):
        """The first line with the label is the field, as for header fields.
        The old pattern read the later line only when the blank one had no
        trailing space, so an invisible character decided the <title>."""
        seo = self._parse(f"{blank}\nOG title: A title\nSEO title: Stray")["seo"]
        assert seo == {"og_title": "A title"}

    def test_schema_type_is_not_an_seo_field(self):
        """Nothing sets schema from a handoff, so it never reaches ``seo``."""
        result = self._parse("Focus keyword: fiber\nSchema type: AboutPage")
        assert result["seo"] == {"focus_keyword": "fiber"}

    def test_a_leftover_schema_type_line_is_still_reported(self):
        """Older template copies carry it. Found, so the publish can say so."""
        result = self._parse("Schema type: other — AboutPage")
        assert result["ignored_schema_type"] == "other — AboutPage"

    @pytest.mark.parametrize(
        "seo_lines", ["Focus keyword: fiber", "Schema type:\nOG title: A title"]
    )
    def test_nothing_to_report_without_a_schema_value(self, seo_lines):
        assert self._parse(seo_lines)["ignored_schema_type"] == ""


class TestAuthorIdentity:
    """Who first-person wording in the draft refers to.

    Citation verification cannot check "I have a family." against a page
    without knowing whose family. Measured 2026-09-04: with no author supplied,
    the verifier bound "I" to the first person named on a team page and offered
    a stranger's wife and grandchildren as supporting evidence.
    """

    def _handoff(self, extra=""):
        return (
            "Article: A Test Piece\n"
            "Publication: mikehammett\n"
            f"{extra}"
            "Pipeline run: 1\n\n"
            "## PRIMARY CLAIM\nA claim.\n\n"
            "## DRAFT\nSome body text.\n"
        )

    def test_an_author_line_is_read(self):
        got = parse_draft_submission(self._handoff("Author: Jane Guest\n"))
        assert got["author"] == "Jane Guest"

    def test_it_is_optional(self):
        """Single-author publications set it once in the publication config
        rather than repeating it on every article."""
        got = parse_draft_submission(self._handoff())
        assert not got.get("author")

    def test_a_raw_draft_carries_none(self):
        """--raw-draft has no metadata at all, so the publication default is
        the only thing standing between the verifier and an unattributed 'I'."""
        got = build_handoff_from_raw_text("Just prose.", source_name="x")
        assert not got.get("author")


class TestTheTwoAuthorLabelsAreDistinct:
    """Template A's `Author:` and Template C's meant different things.

    Template A names who "I" is, for citation verification. Template C named a
    WordPress *login username*, for the post payload. Same label, and a byline
    pasted into the second is an account that does not exist. Template C's
    field is now `WordPress author:`; the old spelling still parses, because
    existing publication handoffs use it.
    """

    def _params(self, author_line):
        text = (
            "PUBLICATION HANDOFF\n"
            "Article: T\n"
            "\n"
            "PUBLICATION PARAMETERS\n"
            f"{author_line}\n"
            "\n"
            "FINAL DRAFT\n"
            "Body text.\n"
        )
        return parse_publication_handoff(text)["publication_parameters"]

    def test_the_current_spelling_parses(self):
        assert self._params("WordPress author: mikeh") == {"wordpress_author": "mikeh"}

    def test_the_legacy_spelling_still_parses(self):
        assert self._params("Author: mikeh") == {"author": "mikeh"}

    def test_the_draft_template_author_is_a_different_field(self):
        """Template A's Author: lands on the handoff, not in publish params."""
        text = (
            "DRAFT SUBMISSION HANDOFF\n"
            "Article: T\n"
            "Author: Mike Hammett\n"
            "\n"
            "PRIMARY CLAIM\n"
            "A claim.\n"
            "\n"
            "DRAFT\n"
            "Body.\n"
        )
        assert parse_draft_submission(text)["author"] == "Mike Hammett"


class TestABlankLabelIsBlank:
    """A header label left blank means "not provided", not "the next line".

    The value pattern allowed any whitespace after the label, newlines
    included, so a blank label took the whole next line as its value.
    Reproduced 2026-09-17: a blank ``Author:`` above ``History key: a-piece``
    parsed as the author "History key: a-piece", which citation verification
    was then told is who "I" refers to.
    """

    #: Every header label the draft and metadata parsers read, in template
    #: order, with the key it lands on and a value to fill it with.
    #: ``Pipeline run:`` is left out: its value is only used when it is all
    #: digits, so a next line taken in its place never got through.
    _HEADER = {
        "Article:": ("title", "A Piece"),
        "Publication:": ("publication", "mikehammett"),
        "Author:": ("author", "Jane Guest"),
        "History key:": ("history_key", "a-piece"),
        "Drafted with:": ("drafted_with", "claude"),
    }

    def _handoff(self, blank):
        lines = [
            f"{label} " if label == blank else f"{label} {value}"
            for label, (_, value) in self._HEADER.items()
        ]
        return (
            "DRAFT SUBMISSION HANDOFF\n"
            + "\n".join(lines)
            + "\n\nPRIMARY CLAIM\nA claim.\n\nDRAFT\nBody.\n"
        )

    def test_a_blank_author_does_not_take_the_next_line(self):
        got = parse_draft_submission(
            "Article: A Piece\n"
            "Author:\n"
            "History key: a-piece\n"
            "\n"
            "PRIMARY CLAIM\nA claim.\n\n"
            "DRAFT\nBody.\n"
        )
        assert got["author"] == ""
        assert got["history_key"] == "a-piece"

    def test_a_blank_history_key_does_not_take_the_next_line(self):
        """The key names the run's pipeline_history directory. In template
        order the next line is ``Drafted with:``, so every article left blank
        here was filed under one shared "drafted-with-claude" directory. Blank
        now reads as absent, which ``_history_key`` resolves to the title."""
        got = parse_draft_submission(
            "Article: A Piece\n"
            "History key:\n"
            "Drafted with: claude\n"
            "\n"
            "PRIMARY CLAIM\nA claim.\n\n"
            "DRAFT\nBody.\n"
        )
        assert got["history_key"] == ""
        assert got["drafted_with"] == "claude"

    @pytest.mark.parametrize("parse", [parse_draft_submission, parse_metadata_only])
    @pytest.mark.parametrize("blank", list(_HEADER))
    def test_no_header_label_takes_the_next_line(self, parse, blank):
        """Blanked with a trailing space, as deleting a value leaves it. The
        last label is followed by an empty line and then PRIMARY CLAIM, which
        the old pattern crossed as well."""
        got = parse(self._handoff(blank))
        for label, (key, value) in self._HEADER.items():
            assert got[key] == ("" if label == blank else value), label

    def test_a_blank_publication_title_does_not_take_the_next_line(self):
        """The publish path sends this to WordPress as the post title."""
        got = parse_publication_handoff(
            "PUBLICATION HANDOFF\n"
            "Article:\n"
            "Publication: mikehammett\n"
            "\n"
            "FINAL DRAFT\nBody.\n"
        )
        assert got["title"] == ""
        assert got["publication"] == "mikehammett"

    def test_a_blank_label_does_not_borrow_a_later_line_with_that_label(self):
        """The first occurrence is the field. Skipping a blank one would read
        the next line that starts with the same label, wherever it is — here a
        source's byline in SOURCES ALREADY CITED."""
        got = parse_draft_submission(
            "Article: A Piece\n"
            "Author:\n"
            "\n"
            "PRIMARY CLAIM\nA claim.\n\n"
            "SOURCES ALREADY CITED\n"
            "Title: 2025 Broadband Map\n"
            "Author: FCC staff\n\n"
            "DRAFT\nBody.\n"
        )
        assert got["author"] == ""


#: The templates as they ship. A copy of one with a line not filled in is how a
#: placeholder reaches the parser.
_TEMPLATES = Path(handoff_parser.__file__).parent / "handoff_templates"


class TestAnUnfilledLabelIsBlank:
    """A header label left on its bracketed template placeholder is not set.

    ``_extract_field`` returned the value verbatim, so a label nobody filled in
    was read as a real one. Reproduced 2026-09-18 by parsing the shipped
    templates unfilled: the History key placeholder named the run's
    pipeline_history directory, so every handoff that left it shared one, and
    the Author placeholder was what citation verification was told "I" refers
    to. It now reads as absent, like a blank label, so each field's own
    fallback applies: the title for the history key, the publication's
    ``author_name`` for the author, ``pipeline.drafting_model`` for the drafter.
    """

    _HEADER = TestABlankLabelIsBlank._HEADER

    def _handoff(self, unfilled, placeholder):
        lines = [
            f"{label} {placeholder}" if label == unfilled else f"{label} {value}"
            for label, (_, value) in self._HEADER.items()
        ]
        return (
            "DRAFT SUBMISSION HANDOFF\n"
            + "\n".join(lines)
            + "\n\nPRIMARY CLAIM\nA claim.\n\nDRAFT\nBody.\n"
        )

    @pytest.mark.parametrize(
        "placeholder",
        [
            "[Your article title]",
            # Wrapped, as three of the draft template's are. Only the label's
            # own line is read, so its "]" is never seen.
            "[Optional. The model you drafted this with — claude, openai,\n"
            "gemini, mistral, grok or perplexity.]",
        ],
        ids=["closed", "wrapped"],
    )
    @pytest.mark.parametrize("parse", [parse_draft_submission, parse_metadata_only])
    @pytest.mark.parametrize("unfilled", list(_HEADER))
    def test_no_header_label_reads_its_placeholder(self, unfilled, parse, placeholder):
        got = parse(self._handoff(unfilled, placeholder))
        for label, (key, value) in self._HEADER.items():
            assert got[key] == ("" if label == unfilled else value), label

    @pytest.mark.parametrize(
        "parse, template",
        [
            (parse_draft_submission, "draft_submission.template.md"),
            (parse_metadata_only, "metadata_only.md"),
        ],
    )
    def test_an_unfilled_template_sets_no_header_field(self, parse, template):
        """The reproduction, on the templates as they ship."""
        got = parse((_TEMPLATES / template).read_text(encoding="utf-8"))
        keys = [key for key, _ in self._HEADER.values()]
        assert {key: got[key] for key in keys} == dict.fromkeys(keys, "")

    def test_an_unfilled_publication_template_has_no_title(self):
        """publication.md's "Article: [title]" became the WordPress post
        title, and Rank Math's title whenever the SEO and OG titles were
        unset. The publish path now refuses a handoff with no title."""
        got = parse_publication_handoff(
            (_TEMPLATES / "publication.md").read_text(encoding="utf-8")
        )
        assert got["title"] == ""
        assert got["publication"] == ""

    def test_an_unfilled_draft_title_is_warned_about_as_missing(self, caplog):
        with caplog.at_level("WARNING"):
            got = parse_draft_submission(
                self._handoff("Article:", "[Your article title]")
            )
        assert got["title"] == ""
        assert any("missing 'Article:'" in r.getMessage() for r in caplog.records)

    def test_an_unfilled_metadata_title_falls_back_to_the_draft_heading(self):
        """--raw-draft --metadata takes the title from the draft's heading
        when the metadata names none, and a placeholder now names none."""
        handoff = build_handoff_from_raw_draft_and_metadata(
            "# Draft-Derived Title\n\nBody text.",
            (_TEMPLATES / "metadata_only.md").read_text(encoding="utf-8"),
            source_name="fallback",
        )
        assert handoff["title"] == "Draft-Derived Title"

    def test_a_title_that_only_begins_with_a_bracketed_tag_is_kept(self):
        """The rule the SEO title already follows: a leading tag is real
        title practice, not a placeholder."""
        title = "[Case Study] How We Cut Fiber Costs"
        got = parse_draft_submission(
            f"Article: {title}\n\nPRIMARY CLAIM\nA claim.\n\nDRAFT\nBody.\n"
        )
        assert got["title"] == title

    def test_a_placeholder_does_not_borrow_a_later_line_with_that_label(self):
        """The first occurrence is the field, as for a blank label. A
        placeholder there is not a reason to read the next "Author:" in the
        document — here a source's byline in SOURCES ALREADY CITED."""
        got = parse_draft_submission(
            "Article: A Piece\n"
            'Author: [optional — who "I" refers to in the draft. Only needed '
            "when it is not\n"
            "         the publication's usual byline.]\n"
            "\n"
            "PRIMARY CLAIM\nA claim.\n\n"
            "SOURCES ALREADY CITED\n"
            "Title: 2025 Broadband Map\n"
            "Author: FCC staff\n\n"
            "DRAFT\nBody.\n"
        )
        assert got["author"] == ""

    def test_the_log_names_the_label_and_what_it_held(self, caplog):
        """A value typed inside the brackets cannot be told from a
        placeholder, so it is dropped like one. The log says which label and
        what it held, so the author can see the value was theirs."""
        with caplog.at_level("INFO", logger="ci_article_review.handoff_parser"):
            got = parse_draft_submission(self._handoff("Author:", "[Jane Guest]"))
        assert got["author"] == ""
        assert any(
            "'Author:'" in r.getMessage() and "[Jane Guest]" in r.getMessage()
            for r in caplog.records
        )
