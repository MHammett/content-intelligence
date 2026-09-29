"""A refuted claim is told when the same run confirmed the same point.

The ensemble returns one draft sentence as several differently-worded claims.
Each is resolved on its own, against whichever URL the fact-check model that
raised that phrasing supplied, so they can disagree — and only the failures
reach Section 9's "Read, and does NOT support the claim" block.

Measured on the 2026-09-18 GPS replay: one sentence produced four entries
reading ``supports``, ``supports``, ``contradicts`` and ``not_addressed``, and
7 of the block's 30 entries had a confirmed sibling. Four of those were checked
by hand against the sources, and in each the sibling was the same point.
"""

from ci_article_review.adapters.citation import draft_citations as dc
from ci_article_review.adapters.citation import resolver
from ci_article_review import report_markdown


def _mismatch(claim, url="https://example.invalid/refuted.pdf"):
    return {
        "claim": claim,
        "url": url,
        "resolved": False,
        "verification": "content_mismatch",
        "relevance_verdict": "not_addressed",
        "relevance_reason": "the page does not discuss this",
        "note": "Source URL loaded ... does not support this specific claim.",
    }


def _confirmed(claim, url="https://example.invalid/confirmed.pdf"):
    return {
        "claim": claim,
        "url": url,
        "resolved": True,
        "verification": "checksum",
        "relevance_verdict": "supports",
    }


# The real pair, from the run this was built from: the refuted phrasing and the
# one the same run confirmed against a source the draft cites.
REFUTED = "Honda's own bulletin said plainly that the date could not be corrected manually [3]."
CONFIRMED = (
    "Honda's bulletins described the problem and said plainly there was no way "
    "to correct the date [4]."
)


class TestNotingConfirmedSiblings:
    def test_refuted_entry_learns_about_its_confirmed_sibling(self):
        results = [_mismatch(REFUTED), _confirmed(CONFIRMED)]

        resolver._note_confirmed_siblings(results)

        siblings = results[0]["confirmed_elsewhere"]
        assert [s["url"] for s in siblings] == ["https://example.invalid/confirmed.pdf"]
        assert siblings[0]["claim"] == CONFIRMED
        assert "also checked a closely-worded version" in results[0]["note"]

    def test_the_verdict_itself_is_left_alone(self):
        """Nothing is suppressed or re-ranked — the entry stays in the block.

        The confirming sibling is context for a human, not grounds for the
        pipeline to overrule a verdict it has no better information about.
        """
        results = [_mismatch(REFUTED), _confirmed(CONFIRMED)]

        resolver._note_confirmed_siblings(results)

        assert results[0]["verification"] == "content_mismatch"
        assert results[0]["relevance_verdict"] == "not_addressed"
        assert results[0]["resolved"] is False

    def test_unrelated_confirmed_claim_is_not_offered(self):
        results = [
            _mismatch("The GPS week number is ten bits wide and wraps at 1024."),
            _confirmed("Honda split its bulletins by map disc colour."),
        ]

        resolver._note_confirmed_siblings(results)

        assert "confirmed_elsewhere" not in results[0]
        assert "closely-worded" not in results[0]["note"]

    def test_a_sibling_that_was_not_confirmed_does_not_count(self):
        """Only ``checksum`` entries qualify — another refutation is not support."""
        other = _mismatch(CONFIRMED, url="https://example.invalid/other.pdf")
        results = [_mismatch(REFUTED), other]

        resolver._note_confirmed_siblings(results)

        assert "confirmed_elsewhere" not in results[0]

    def test_same_url_and_different_url_are_worded_differently(self):
        """Both happen, and they mean different things to the reader."""
        same = [
            _mismatch(REFUTED, url="https://example.invalid/doc.pdf"),
            _confirmed(CONFIRMED, url="https://example.invalid/doc.pdf"),
        ]
        resolver._note_confirmed_siblings(same)
        assert "the same source" in same[0]["note"]

        different = [_mismatch(REFUTED), _confirmed(CONFIRMED)]
        resolver._note_confirmed_siblings(different)
        assert "a different source" in different[0]["note"]

    def test_entries_that_are_not_mismatches_are_untouched(self):
        confirmed = _confirmed(CONFIRMED)
        unverifiable = {
            "claim": REFUTED,
            "verification": "unverifiable",
            "note": "could not be assessed",
        }
        results = [unverifiable, confirmed]

        resolver._note_confirmed_siblings(results)

        assert "confirmed_elsewhere" not in unverifiable
        assert unverifiable["note"] == "could not be assessed"

    def test_no_confirmed_entries_at_all_is_a_no_op(self):
        results = [_mismatch(REFUTED)]
        resolver._note_confirmed_siblings(results)
        assert "confirmed_elsewhere" not in results[0]


class TestClaimTokens:
    """The tokenizer choice is load-bearing, so it is pinned.

    ``ci_core.extract.claim_terms`` is the obvious shared candidate and the
    wrong one: it keeps only words of five characters or more, which is right
    for finding a passage in a long document and wrong for comparing two
    phrasings of one sentence.
    """

    def test_the_measured_pair_clears_the_threshold(self):
        overlap = resolver._claim_overlap(REFUTED, CONFIRMED)
        assert overlap >= resolver._SIBLING_OVERLAP

    def test_claim_terms_would_have_missed_the_same_pair(self):
        from ci_core import extract

        numbers_a, words_a = extract.claim_terms(REFUTED)
        numbers_b, words_b = extract.claim_terms(CONFIRMED)
        a, b = numbers_a | words_a, numbers_b | words_b
        assert len(a & b) / len(a | b) < resolver._SIBLING_OVERLAP

    def test_empty_claims_do_not_divide_by_zero(self):
        assert resolver._claim_overlap("", CONFIRMED) == 0.0
        assert resolver._claim_overlap(None, None) == 0.0

    def test_public_tokenizer_matches_the_internal_one(self):
        assert dc.claim_tokens("Honda bulletins") == dc._tokens("honda bulletins")


class TestRendering:
    def test_the_sibling_renders_even_though_note_is_suppressed(self):
        """``note`` is dropped whenever it repeats the relevance reason, which a
        mismatch entry's note always does — so this must render on its own."""
        entry = _mismatch(REFUTED)
        entry["note"] = (
            "... does not support this specific claim (not_addressed): the page does not discuss this"
        )
        entry["confirmed_elsewhere"] = [
            {"claim": CONFIRMED, "url": "https://example.invalid/confirmed.pdf"}
        ]

        out = "\n".join(report_markdown._render_mismatch_entry(entry))

        assert "ALSO confirmed a closely-worded version" in out
        assert "https://example.invalid/confirmed.pdf" in out
        assert CONFIRMED in out

    def test_entry_without_a_sibling_renders_unchanged(self):
        out = "\n".join(report_markdown._render_mismatch_entry(_mismatch(REFUTED)))
        assert "ALSO confirmed" not in out
