"""Tracing a fact-check claim back to the citation the draft attaches to it.

The bug this module was written for: a claim whose fact-check item named no URL
fell back to a *response-level* grounded URL — the first entry of the provider's
search-results list for the whole response. One real run stamped a single LBNL
energy report onto 44 unrelated claims (Yorkville water rates, ICNIRP exposure
limits, IARC classifications) and reported each as unsupported by it. Two real
findings sat inside 47 false positives.

The cases below are the ones that decide whether the replacement is trustworthy.
A wrong anchor recreates the same failure at lower volume, so "returns nothing"
is a correct answer here and is asserted as often as a positive match.
"""

import pytest

from ci_article_review.adapters.citation import draft_citations as dc


DRAFT = """\
Illinois generates 53% of its electricity from nuclear power and 13% from wind. [2]

Google and Kairos Power signed a master plant development agreement on
October 14, 2024, targeting 500 MW by 2035. [24a] Amazon anchored a $500 million
financing round for X-energy on October 16, 2024. [24c]

## Citations

[2] U.S. Energy Information Administration, Illinois State Energy Profile, 2024
data. Nuclear 53%, wind 13%. https://eia.example/illinois

[24a] Kairos Power, "Google and Kairos Power Partner to Deploy 500 MW."
https://kairos.example/google

[24c] X-energy, "Amazon Invests in X-energy to Support Advanced SMRs."
https://x-energy.example/amazon
"""


class TestCitationBlock:
    def test_the_block_is_found_by_its_heading(self):
        body, block = dc.split_citation_block(DRAFT)
        assert "Illinois generates" in body
        assert "[2] U.S. Energy Information" in block
        assert "Illinois generates" not in block

    def test_a_draft_with_no_block_is_all_body(self):
        body, block = dc.split_citation_block("Just prose. No citations.")
        assert block == ""
        assert body == "Just prose. No citations."

    @pytest.mark.parametrize(
        "heading", ["## Citations", "### Sources", "## Works Cited", "## References"]
    )
    def test_the_common_heading_spellings_are_all_recognised(self, heading):
        draft = f"Body text. [1]\n\n{heading}\n\n[1] A source. https://example.org/a"
        _, block = dc.split_citation_block(draft)
        assert "https://example.org/a" in block

    def test_entries_keep_their_urls_and_sub_numbering(self):
        entries = dc.parse_citation_block(dc.split_citation_block(DRAFT)[1])
        assert entries["2"]["urls"] == ["https://eia.example/illinois"]
        assert entries["24a"]["urls"] == ["https://kairos.example/google"]
        assert entries["24c"]["urls"] == ["https://x-energy.example/amazon"]

    def test_an_entry_with_no_url_is_still_recorded(self):
        """ "The draft cites nothing here" and "cites something unfetchable"
        are different problems, and only the first is the author's to fix."""
        entries = dc.parse_citation_block("[5] A print-only report, 2024.")
        assert entries["5"]["urls"] == []

    def test_a_numbered_list_block_is_read_like_bracketed_entries(self):
        """A drafting tool writes the block as a markdown list and the body
        still cites ``[2]``; reading none of it made every lookup miss."""
        draft = (
            "Nuclear is 53%. [2] Heat reuse saves gas. [10]\n\n### Sources\n\n"
            "1. First, 2024. https://example.org/one\n"
            "2. EIA, Illinois profile,\n   continued line. https://eia.example/il\n"
            "10. Tenth. https://example.org/ten\n"
        )
        c = dc.DraftCitations(draft)
        assert c.marker_count == 3
        assert c.entries["2"]["urls"] == ["https://eia.example/il"]
        assert "continued line" in c.entries["2"]["text"]
        assert c.entries["10"]["urls"] == ["https://example.org/ten"]
        assert c.candidates_for("Nuclear is 53%") == ["https://eia.example/il"]

    def test_a_numbered_line_inside_a_bracketed_entry_does_not_split_it(self):
        entries = dc.parse_citation_block(
            "[1] Report, see\n2. its second chapter. https://example.org/a\n"
        )
        assert list(entries) == ["1"]
        assert entries["1"]["urls"] == ["https://example.org/a"]


class TestSegmentation:
    def test_a_marker_cites_the_text_before_it(self):
        """The detail everything else rests on.

        Attaching a marker to the sentence that follows it shifts every citation
        in a paragraph one sentence late — a mapping that looks plausible and is
        wrong throughout.
        """
        body, block = dc.split_citation_block(DRAFT)
        entries = dc.parse_citation_block(block)
        segments = dc.segment_body(body, entries)
        kairos = next(s for s in segments if "24a" in s["markers"])
        assert "Google and Kairos" in kairos["text"]
        assert "Amazon" not in kairos["text"]

    def test_an_uncited_list_item_does_not_borrow_the_next_ones_marker(self):
        """The xAI case.

        A summary list whose middle bullet was deliberately uncited took the
        markers off the bullet below it, pointing Virginia noise sources at a
        Tennessee air-permit claim.
        """
        draft = (
            "- Water use is up. [1]\n"
            "- xAI ran 35 unpermitted turbines in Memphis.\n"
            "- Virginia found no noise complaints. [2]\n"
            "\n## Citations\n\n"
            "[1] Water report. https://water.example\n\n"
            "[2] Virginia noise study. https://noise.example\n"
        )
        cited = dc.DraftCitations(draft)
        assert cited.candidates_for("xAI ran 35 unpermitted turbines in Memphis") == []

    def test_text_after_the_last_marker_is_uncited(self):
        body, block = dc.split_citation_block(
            "Cited sentence. [1] Trailing uncited sentence.\n\n"
            "## Citations\n\n[1] A. https://a.example"
        )
        segments = dc.segment_body(body, dc.parse_citation_block(block))
        assert len(segments) == 1
        assert "Trailing uncited" not in segments[0]["text"]

    def test_a_bracketed_number_the_block_never_defines_is_not_a_citation(self):
        body, block = dc.split_citation_block(
            "A quoted table row [99] and a real cite. [1]\n\n"
            "## Citations\n\n[1] A. https://a.example"
        )
        segments = dc.segment_body(body, dc.parse_citation_block(block))
        assert [s["markers"] for s in segments] == [["1"]]


class TestCandidateLookup:
    def test_a_claim_is_traced_to_the_source_the_draft_cites_for_it(self):
        cited = dc.DraftCitations(DRAFT)
        got = cited.candidates_for(
            "Illinois generates 53% of its electricity from nuclear power"
        )
        assert got[0] == "https://eia.example/illinois"

    def test_neighbouring_claims_get_different_sources(self):
        """The off-by-one regression test.

        Both sentences sit in one paragraph and differ mainly by company name;
        a matcher that attaches markers forward gives both the same URL.
        """
        cited = dc.DraftCitations(DRAFT)
        google = cited.candidates_for(
            "Google and Kairos Power signed a master plant development agreement"
        )
        amazon = cited.candidates_for(
            "Amazon anchored a $500 million financing round for X-energy"
        )
        assert google[0] == "https://kairos.example/google"
        assert amazon[0] == "https://x-energy.example/amazon"

    def test_a_claim_that_matches_nothing_gets_nothing(self):
        cited = dc.DraftCitations(DRAFT)
        assert cited.candidates_for("IARC classified ELF-EMF as Group 2B in 2002") == []

    def test_a_draft_with_no_citation_block_anchors_nothing(self):
        cited = dc.DraftCitations("Illinois generates 53% from nuclear power.")
        assert not cited
        assert cited.candidates_for("Illinois generates 53% from nuclear power") == []

    def test_a_marker_named_in_the_claim_itself_wins(self):
        """A model writing "cited in [24c] but not public" is naming the
        citation outright, which beats any similarity score."""
        cited = dc.DraftCitations(DRAFT)
        got = cited.candidates_for(
            "Some claim with no textual overlap whatsoever",
            source_text="Company records, cited in [24c] but not public",
        )
        assert got == ["https://x-energy.example/amazon"]

    def test_the_candidate_list_is_capped(self):
        draft = (
            "One heavily cited sentence about nuclear power in Illinois. "
            "[1][2][3][4][5]\n\n## Citations\n\n"
            + "\n\n".join(
                f"[{n}] Source {n}. https://example.org/{n}" for n in range(1, 6)
            )
        )
        cited = dc.DraftCitations(draft)
        got = cited.candidates_for(
            "One heavily cited sentence about nuclear power in Illinois"
        )
        assert len(got) == dc.MAX_CANDIDATES

    def test_the_preceding_span_is_offered_as_an_escalation_target(self):
        """A span's opening sentence often belongs to the marker before it.

        Here the eGRID sentence opens a span that ends at [2], the EIA profile.
        [1] must stay reachable or the claim is reported unsupported by a source
        the author never cited for it.
        """
        draft = (
            "EPA eGRID 2023 shows a spread in NOx output across grid regions. [1] "
            "The RFCW region emits 0.422 lbs of NOx per megawatt-hour. "
            "Illinois generates 53% of its power from nuclear. [2]\n\n"
            "## Citations\n\n"
            "[1] EPA eGRID 2023 Summary Tables. https://epa.example/egrid\n\n"
            "[2] EIA Illinois State Energy Profile. https://eia.example/illinois\n"
        )
        cited = dc.DraftCitations(draft)
        got = cited.candidates_for(
            "The RFCW region emits 0.422 lbs of NOx per megawatt-hour"
        )
        assert "https://epa.example/egrid" in got

    def test_an_empty_draft_is_harmless(self):
        for draft in ("", None):
            cited = dc.DraftCitations(draft)
            assert not cited
            assert cited.candidates_for("anything") == []
