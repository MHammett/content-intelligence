"""A claim that only *nearly* locates itself still reaches the draft's citations.

``_MIN_SCORE`` is a cliff. A claim scoring 0.54 gets no draft citation at all,
so the only thing left to check is whatever URL the fact-check model supplied.
On the 2026-09-18 GPS run that is what happened to "when the system internal
calendar is reset to May 19, 2002": it scored 0.450, the draft's own ``[4]``
never applied, and the model's URL — a June 2023 revision that had dropped the
sentence — was reported as failing to support it. ``[4]``'s January 2022 URL
carries the wording verbatim.

Swept over 33 captures (2,904 scored claims): 232 land in the 0.40-0.55 band,
48 of those reach the "does NOT support" block, and 28 of the 48 have a
draft-cited URL the run never looked at.
"""

from ci_article_review.adapters.citation import draft_citations as dc
from ci_article_review.adapters.citation import resolver


# Two paragraphs, each with its own citation, and a sentence in the second that
# only partly echoes it — the near-miss shape.
DRAFT = """The rollover arithmetic is covered in the receiver documentation. [1]

Honda's bulletins described the problem and said plainly there was no way to
correct the date, and the clocks recovered that August. [4]

## Sources

[1] Furuno rollover document. https://example.invalid/furuno.pdf
[4] Honda ServiceNews A21120B. https://example.invalid/a21120b-2022.pdf
"""


class TestTheBandAndTheMargin:
    def test_a_decisive_near_miss_offers_the_span_citations(self):
        index = dc.DraftCitations(DRAFT)
        claim = (
            "there was no way to correct the date shown on the navigation "
            "screen after startup"
        )

        best = max(
            dc._score(dc._tokens(claim), dc._numbers(claim), s) for s in index._segments
        )
        assert dc._NEAR_MISS_FLOOR <= best < dc._MIN_SCORE, (
            f"fixture must sit in the near-miss band, scored {best}"
        )
        assert index.candidates_for(claim) == [], "the cliff must still bite"
        assert "https://example.invalid/a21120b-2022.pdf" in (
            index.fallback_candidates_for(claim)
        )

    def test_a_confident_match_needs_no_fallback(self):
        """Above the threshold the normal path already answers."""
        index = dc.DraftCitations(DRAFT)
        claim = (
            "Honda's bulletins described the problem and said plainly there was "
            "no way to correct the date, and the clocks recovered that August."
        )

        assert index.candidates_for(claim), "this one should match normally"
        assert index.fallback_candidates_for(claim) == []

    def test_a_tie_inside_the_band_is_refused(self):
        """The band alone is not enough — the margin is what admits a match."""
        draft = """Generators ran hard in the heat. [1]

Generators ran hard in the heat. [2]

## Sources

[1] First. https://example.invalid/one.pdf
[2] Second. https://example.invalid/two.pdf
"""
        index = dc.DraftCitations(draft)
        claim = "generators ran hard during the heat"

        scores = sorted(
            (
                dc._score(dc._tokens(claim), dc._numbers(claim), s)
                for s in index._segments
            ),
            reverse=True,
        )
        assert scores[0] - scores[1] < dc._DECISIVE_MARGIN, "fixture must be a tie"
        assert index.fallback_candidates_for(claim) == []

    def test_nothing_below_the_floor_is_offered(self):
        index = dc.DraftCitations(DRAFT)
        assert index.fallback_candidates_for("entirely unrelated zebra taxonomy") == []

    def test_no_citation_block_is_handled(self):
        assert (
            dc.DraftCitations("Body only, no sources.").fallback_candidates_for(
                "anything"
            )
            == []
        )


class TestWeakSourcesAreDeclared:
    """A rescue that lands on the wrong citation must not read as verified."""

    CLAIM = "there was no way to correct the date"
    WEAK = "https://example.invalid/a21120b-2022.pdf"

    def _claims(self):
        return [
            {
                "claim": self.CLAIM,
                "known_urls": ["https://example.invalid/model.pdf", self.WEAK],
                "weak_source_urls": [self.WEAK],
            }
        ]

    def test_a_resolution_resting_on_a_weak_source_says_so(self):
        results = [
            {
                "claim": self.CLAIM,
                "url": self.WEAK,
                "verification": "checksum",
                "note": "Source URL fetched and checksummed.",
            }
        ]

        resolver._note_weak_span_sources(results, self._claims())

        assert results[0]["weak_span_match"] is True
        assert "not the one the draft cites for this sentence" in results[0]["note"]

    def test_a_resolution_on_a_normal_source_is_untouched(self):
        results = [
            {
                "claim": self.CLAIM,
                "url": "https://example.invalid/model.pdf",
                "verification": "checksum",
                "note": "Source URL fetched and checksummed.",
            }
        ]

        resolver._note_weak_span_sources(results, self._claims())

        assert "weak_span_match" not in results[0]
        assert results[0]["note"] == "Source URL fetched and checksummed."

    def test_a_refuted_weak_source_is_declared_too(self):
        """The caveat is about which document was read, not about the verdict."""
        results = [
            {
                "claim": self.CLAIM,
                "url": self.WEAK,
                "verification": "content_mismatch",
                "relevance_verdict": "not_addressed",
                "note": "... does not support this specific claim.",
            }
        ]

        resolver._note_weak_span_sources(results, self._claims())

        assert results[0]["weak_span_match"] is True

    def test_claims_without_weak_sources_are_a_no_op(self):
        results = [{"claim": self.CLAIM, "url": self.WEAK, "verification": "checksum"}]
        resolver._note_weak_span_sources(results, [{"claim": self.CLAIM}])
        assert "weak_span_match" not in results[0]
