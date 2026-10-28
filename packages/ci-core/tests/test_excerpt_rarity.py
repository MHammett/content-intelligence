"""The verifier's excerpt window finds the passage a plain search would (issue #440).

The old scorer counted every claim word alike. In a long technical document the
claim's generic vocabulary is everywhere, so the window went to wherever it was
densest: on brockovich-fact-check run 2 the EPA cooling-water document's answer
sat at offset 428,046 and the window was 350,000-370,000. These tests build
that shape small: a long body thick with the generic words, and one short
passage holding the distinctive ones.
"""

from ci_core import extract

HEAD, TAIL = 16000, 4000
BUDGET = HEAD + TAIL

_FILLER = (
    "Cooling water intake structures at power plants use energy for pumps and "
    "fans; the cooling system design affects plant energy use and operating "
    "cost at every facility reviewed. "
)


def _document(insertions, length=400_000):
    """``length`` characters of generic filler with ``insertions`` at offsets."""
    body = list((_FILLER * (length // len(_FILLER) + 1))[:length])
    text = "".join(body)
    for offset, passage in sorted(insertions, reverse=True):
        text = text[:offset] + " " + passage + " " + text[offset:]
    return text


def _window(text, claim, hints=()):
    return extract.select_excerpt_with_window(
        text, claim, head=HEAD, tail=TAIL, hints=hints
    )


class TestRareTermsLeadTheWindow:
    def test_the_one_sentence_with_the_rare_words_is_shown(self):
        answer = (
            "The disadvantages of dry cooling include an increase in noise generation."
        )
        text = _document([(300_000, answer)])
        claim = (
            "EPA's review of dry cooling at power plants names the fans as the "
            "largest operating energy load and lists increased noise among its "
            "disadvantages"
        )
        excerpt, window = _window(text, claim)
        assert "increase in noise generation" in excerpt
        assert window["windowed"] is True
        assert window["chars_read"] <= BUDGET

    def test_two_figures_far_apart_are_both_shown(self):
        first = "Data centers used about 4.4% of total U.S. electricity in 2023."
        second = (
            "By 2028 data centers could consume between 6.7% and 12% of electricity."
        )
        text = _document([(60_000, first), (320_000, second)])
        claim = "about 4.4% of U.S. electricity in 2023 and 7% to 12% by 2028"
        excerpt, window = _window(text, claim)
        assert "4.4% of total" in excerpt
        assert "6.7% and 12%" in excerpt
        assert window["how"] == "best_windows"
        assert window["chars_read"] <= BUDGET

    def test_a_claim_answered_in_one_place_gets_one_stretch(self):
        answer = "Golf course equivalents: about 150 million gallons a year for a southwestern course."
        text = _document([(200_000, answer)])
        excerpt, window = _window(
            text, "a southwestern golf course uses about 150 million gallons"
        )
        assert "150 million gallons" in excerpt
        assert window["how"] == "best_window"
        assert window["chars_read"] == BUDGET

    def test_omission_markers_account_for_every_character(self):
        first = "Data centers used about 4.4% of total U.S. electricity in 2023."
        second = (
            "By 2028 data centers could consume between 6.7% and 12% of electricity."
        )
        text = _document([(60_000, first), (320_000, second)])
        excerpt, window = _window(text, "about 4.4% in 2023 and 12% by 2028")
        omitted = sum(
            int(n)
            for n in __import__("re").findall(r"\[(\d+) chars omitted\]", excerpt)
        )
        assert omitted + window["chars_read"] == len(text)


class TestHintsAddWithoutDisplacing:
    _ROW = "Council Bluffs, IA Million gallons 1,410.3 400.1 1,010.2 6.7"
    _GOLF = "Golf course equivalents: about 150 million gallons a year for a southwestern course."

    def test_a_claim_that_never_names_the_row_finds_it_through_the_hint(self):
        # A passage that matches the claim's own words, far from the row. The
        # row shares only "gallons" with the claim, which the decoy already
        # covers, so the claim alone cannot reach it.
        decoy = (
            "The biggest campuses can reach 1.5 billion gallons a year, a real figure."
        )
        text = _document([(50_000, decoy), (300_000, self._ROW)])
        claim = (
            "Up to 1.5 billion gallons a year is a real figure for the biggest campuses"
        )
        excerpt, _ = _window(text, claim)
        assert "1,410.3" not in excerpt
        hinted, _ = _window(
            text,
            claim,
            hints=[
                "Google 2025 Environmental Report, p. 110 (Council Bluffs, IA: 1,410.3 million gallons withdrawn)"
            ],
        )
        assert "1,410.3" in hinted

    def test_a_long_hint_does_not_pull_the_window_off_the_claims_own_passage(self):
        text = _document([(120_000, self._GOLF), (300_000, self._ROW)])
        claim = "a southwestern golf course at about 150 million gallons a year"
        hint = (
            "Google, 2025 Environmental Report, water use by data center location, "
            "p. 110 (Council Bluffs, IA: 1,410.3 million gallons withdrawn, 1,010.2 "
            "million consumed, 2024), and endnote 66, p. 117"
        )
        excerpt, _ = _window(text, claim, hints=[hint])
        assert "150 million gallons a year for a southwestern" in excerpt

    def test_hints_are_ignored_when_they_match_nothing(self):
        answer = (
            "The disadvantages of dry cooling include an increase in noise generation."
        )
        text = _document([(300_000, answer)])
        claim = "dry cooling lists increased noise among its disadvantages"
        assert _window(text, claim, hints=["zzzz qqqq 98765"]) == _window(text, claim)


class TestUnchangedBehaviour:
    def test_a_short_document_is_still_sent_whole(self):
        text = "short body about cooling"
        assert _window(text, "cooling", hints=["anything"])[1]["how"] == "whole"

    def test_no_usable_terms_still_falls_back_to_head_and_tail(self):
        text = "A" * 30_000
        _excerpt, window = _window(text, "", hints=())
        assert window["how"] == "head_tail"

    def test_deterministic(self):
        text = _document([(300_000, "increase in noise generation")])
        claim = "dry cooling noise disadvantages"
        assert _window(text, claim) == _window(text, claim)
