"""Tests for passage_match — deciding when two quotations are the same passage.

The module replaced three divergent answers to that question (a prefix-exact
key, a Jaccard threshold, and the citation resolver's own matching), none of
which handled nesting, which is how an ensemble quoting one draft actually
duplicates itself.
"""

import itertools

from ci_article_review.passage_match import (
    group_passages,
    normalise,
    same_passage,
    tokenise,
)

# The duplicate this module exists to catch: one claim, quoted by two models
# with a small difference in where they started or stopped.
SENTENCE = "The engine is still being built and tested."
CLAUSE = "It is not finished. The engine is still being built and tested."

# A different assertion further along the same paragraph. Containment cannot
# tell this from the pair above other than by length — which is what the ratio
# guard measures, and why it is set high.
FAR = (
    CLAUSE + " It is not a plugin I installed, it is a multi-stage review "
    "process I designed and directed a model to build, and I am releasing it "
    "on a public repository once it is solid."
)


class TestNormalise:
    def test_collapses_whitespace_and_case(self):
        assert normalise("Hello   World") == normalise("hello world")

    def test_does_not_truncate(self):
        assert len(normalise("a" * 400)) == 400

    def test_tolerates_none(self):
        assert normalise(None) == ""


class TestSamePassage:
    def test_identical_text_matches(self):
        assert same_passage(SENTENCE, SENTENCE)

    def test_the_same_claim_quoted_slightly_differently_matches(self):
        assert same_passage(SENTENCE, CLAUSE)

    def test_unrelated_passages_do_not_match(self):
        assert not same_passage(SENTENCE, "no ads and no sponsors on this site ever")

    def test_a_short_fragment_does_not_match_everything_containing_it(self):
        """The guard that keeps "I have a family." out of every paragraph that
        happens to contain it. Below the token floor, containment means nothing.
        """
        assert not same_passage(
            "I have a family.",
            "I have a day job running network infrastructure. I have a side "
            "job. I have a family.",
        )

    def test_a_sentence_lost_in_a_long_paragraph_does_not_match(self):
        """The length-ratio guard. Two models flagging different sentences of
        one paragraph must not read as agreeing with each other."""
        assert not same_passage(SENTENCE, FAR)

    def test_entity_and_quote_spellings_of_one_sentence_match(self):
        """One sentence arrives four ways after a chat client, a handoff file
        and a JSON round-trip. `&#39;` is the dangerous one: the tokeniser used
        to read the digits and invent a `39` token no other spelling has."""
        plain = "It's not finished. The engine is still being built and tested."
        variants = [
            "It&#39;s not finished. The engine is still being built and tested.",
            "It’s not finished. The engine is still being built and tested.",
            "It\x19s not finished. The engine is still being built and tested.",
        ]
        for variant in variants:
            assert same_passage(plain, variant), variant
        assert "39" not in tokenise(variants[0])

    def test_accepts_pre_tokenised_sets(self):
        assert same_passage(tokenise(SENTENCE), tokenise(CLAUSE))

    def test_empty_input_never_matches(self):
        assert not same_passage("", SENTENCE)
        assert not same_passage(SENTENCE, "")


class TestGroupPassages:
    def _groups(self, texts):
        return group_passages([{"t": t} for t in texts], lambda x: x["t"])

    def test_near_duplicate_quotations_form_one_group(self):
        assert len(self._groups([SENTENCE, CLAUSE])) == 1

    def test_the_fuller_quotation_represents_the_group(self):
        rep, items = self._groups([SENTENCE, CLAUSE])[0]
        assert rep == CLAUSE
        assert len(items) == 2

    def test_grouping_does_not_depend_on_input_order(self):
        """A first-match loop over unsorted input is order-dependent: with A
        inside B inside C, where A and C fail a direct test, ABC gave one group
        and ACB gave two. Sorting longest-first removes that, by establishing
        every container before the fragments that belong to it.
        """
        sizes = {
            len(self._groups(list(order)))
            for order in itertools.permutations([SENTENCE, CLAUSE, FAR])
        }
        assert sizes == {2}

    def test_distinct_sentences_of_one_paragraph_stay_apart_in_every_order(self):
        s1 = "The transmission queue is twelve years long."
        s2 = "Local officials were never consulted at all."
        para = f"{s1} {s2} The utility has said so repeatedly in public filings."
        sizes = {
            len(self._groups(list(order)))
            for order in itertools.permutations([s1, s2, para])
        }
        assert sizes == {3}

    def test_membership_does_not_chain_through_an_intermediate(self):
        """Every member is compared to the group's representative, never to
        another member.

        Single-link clustering let A match B and B match C, putting A and C
        together despite failing a direct test. On the real 2026-09-03 draft
        that fused four separate assertions from one paragraph into a single
        27-flag group presented as the strongest consensus in the run.
        """
        assert same_passage(SENTENCE, CLAUSE)
        assert not same_passage(CLAUSE, FAR)
        assert not same_passage(SENTENCE, FAR)
        # CLAUSE matches SENTENCE and sits inside FAR, but must not fuse them.
        groups = self._groups([SENTENCE, CLAUSE, FAR])
        assert len(groups) == 2
        assert FAR in {rep for rep, _ in groups}

    def test_unrelated_passages_stay_separate(self):
        assert len(self._groups([SENTENCE, "no ads and no sponsors, ever"])) == 2

    def test_a_sentence_inside_a_much_longer_quote_stays_separate(self):
        """Deliberate, and the conservative half of the trade-off. Containment
        alone cannot distinguish a fuller quote of one claim from a different
        claim in the same paragraph, so only near-identical quotations merge."""
        assert len(self._groups([SENTENCE, FAR])) == 2

    def test_empty_passages_are_dropped(self):
        assert self._groups(["", "   "]) == []

    def test_every_item_survives_grouping(self):
        texts = [SENTENCE, CLAUSE, FAR, "no ads and no sponsors, ever"]
        total = sum(len(items) for _, items in self._groups(texts))
        assert total == len(texts)


class TestRepresentativeSelection:
    """Which quotation stands for a group is what a human ends up reading."""

    def _rep(self, texts):
        rep, _ = group_passages([{"t": t} for t in texts], lambda x: x["t"])[0]
        return rep

    def test_a_clean_quotation_beats_a_longer_corrupted_one(self):
        """Models corrupt the passages they echo back. One sentence of the
        2026-09-03 draft returned as `It's`, `It’s`, `It\x19s` and `It&#39;s`
        from four models, while the draft held nothing but plain apostrophes.
        Ranking on length alone put whichever mangled variant was longest into
        the report heading."""
        plain = "It's not finished. The engine is still being built and tested."
        entity = (
            "It&#39;s not finished. The engine is still being built and tested. Yes."
        )
        control = "It\x19s not finished. The engine is still being built and tested."
        rep = self._rep([entity, plain, control])
        assert rep == plain

    def test_among_equally_clean_quotations_the_fullest_wins(self):
        rep = self._rep([SENTENCE, CLAUSE])
        assert rep == CLAUSE

    def test_a_corrupted_quotation_is_still_used_when_it_is_all_there_is(self):
        only = "It&#39;s not finished. The engine is still being built and tested."
        assert self._rep([only]) == only
