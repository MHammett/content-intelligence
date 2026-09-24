"""Regression tests for the Section 9 false-"does NOT support" verdicts.

Every test here pins one mechanism found by hand-checking the 2026-09-18 GPS
replay (``run_4_20260918_103325``) against the documents it judged. Of its 30
``not_addressed``/``contradicts`` entries, roughly 20 were checker errors, and
they fell into the three patterns below. README.md calls this block the most
actionable in the report, which is exactly why a false positive in it is
expensive: it spends the author's attention on a citation that was fine.

No network access: the Furuno document is represented by a fixture with the
same *shape* — the passage answering the claim sits past the point a small
excerpt window would reach, and the claim's distinctive term is sparse early
and dense late.
"""

from ci_core import extract

from ci_article_review.adapters.citation import draft_citations as dc
from ci_article_review.adapters.citation import resolver


class TestLongDocumentReachesTheRelevantPage:
    """Pattern 1: the passage that answers the claim never reached the model.

    Extraction was never the problem — pypdf returns all 9 pages of Furuno's
    rollover PDF (12,888 characters, measured 2026-09-19). The excerpt handed
    to the relevance model was, and for two reasons: the window was too small
    to hold the document, and the window that got picked was the wrong one.
    """

    def _document(self):
        """A document whose answer is late, dense, and *tied* on presence.

        This is the shape that matters, and it is the real one. Furuno's front
        matter is a revision history: it names the chapters, the command table
        and the rollover dates, so it contains every term the claim does — once
        each. The Chapter 6 list that actually answers the claim contains the
        same terms seventeen times over, at offsets 9793-11889.

        Scored by presence the two windows tie, and a strictly-greater
        comparison awards a tie to the earliest, so the head wins. The fixture
        has to reproduce the tie, not merely put the answer late: a document
        whose answer is the only place the terms appear is found by either
        scoring rule and would pin nothing.
        """
        front = (
            "Revision history. Version 1 revised the command table. Version 2 "
            "corrected the rollover date list. Version 3 added chips to this "
            "Furuno documentation. "
        )
        filler = "Background notes on unrelated matters. " * 160
        answer = (
            "Chapter 6. Receivers whose rollover date can be changed by command: "
            + (
                " ".join(
                    f"GN-80{n} accepts the command to change its rollover date."
                    for n in range(20)
                )
            )
        )
        tail = "Further unrelated background notes. " * 50
        return front + filler + answer + tail, answer

    def test_picks_the_dense_passage_not_the_head(self):
        """Occurrence counting, not presence.

        Presence-scoring made every window that merely *mentioned* the term tie
        with the one that answers the claim, and a strictly-greater comparison
        gave the tie to the earliest — reinstating the head bias
        ``select_excerpt`` exists to remove.
        """
        text, answer = self._document()
        claim = "Furuno documentation lists the chips whose rollover date can be changed by command"

        excerpt = extract.select_excerpt(text, claim, head=4000, tail=1000)

        assert "Chapter 6" in excerpt, (
            "excerpt missed the passage answering the claim; it scored a window "
            "that only mentions the claim's terms as highly as the one that "
            "repeats them"
        )
        assert answer[:60] in excerpt

    def test_repetition_does_not_let_one_term_swamp_breadth(self):
        """The count cap keeps a repeated word from beating several distinct hits."""
        spam = "rollover " * 400
        rich = (
            "The table lists each chip family, its window closing date, and the "
            "fallback date reported afterward for every affected receiver."
        )
        text = spam + ("filler. " * 300) + rich + ("filler. " * 300)

        excerpt = extract.select_excerpt(
            text,
            "the table lists each chip family window closing date and fallback reported",
            head=1500,
            tail=500,
        )

        assert rich[:50] in excerpt

    def test_document_under_budget_is_sent_whole(self):
        """A document the model could simply read whole should not be windowed.

        The real Furuno PDF is 12,888 characters. At the old 5,000-character
        budget it could never be shown in full, so every verdict about it rode
        on picking the right window; at the configured budget the question does
        not arise.
        """
        text = "Furuno rollover content. " * 500  # ~12.5k chars, PDF-sized

        excerpt = extract.select_excerpt(
            text, "a claim", head=resolver._EXCERPT_HEAD, tail=resolver._EXCERPT_TAIL
        )

        assert excerpt == text
        assert "chars omitted" not in excerpt


class TestFirstPersonFramingIsConditional:
    """Pattern 2: claims about a document read as first-person claims.

    The verifier was told the article's author on *every* call. It learned that
    the author was part of every claim and started requiring pages to mention
    him — producing verdicts like "the page does not mention Mike Hammett or
    any first-person claim" against "It does spell out four-digit variants
    elsewhere in the table", which is a claim about the cited PDF's contents
    with no first person in it anywhere.
    """

    def test_claim_about_a_document_gets_no_author_framing(self):
        for claim in (
            "It does spell out four-digit variants elsewhere in the table",
            "August 2026 is not one of them, and the satellites are working perfectly",
            "Furuno's rollover table does not list that exact part number",
            "The full list of dates [table]",
        ):
            prompt = resolver._build_verification_prompt(
                claim, "page text", author="Mike Hammett"
            )
            assert "Mike Hammett" not in prompt, (
                f"author named for a claim with no first person in it: {claim!r}"
            )
            assert "First-person wording" not in prompt

    def test_genuinely_first_person_claim_still_names_the_author(self):
        """The 2026-09-04 fix this must not undo.

        Without a named author the verifier has an "I" with no referent, and
        answered not_addressed for "I have a family." against a page carrying
        the author's own bio.
        """
        prompt = resolver._build_verification_prompt(
            "I have a family.", "page text", author="Mike Hammett"
        )

        assert "Mike Hammett" in prompt
        assert "First-person wording refers to them" in prompt

    def test_first_person_detection_boundaries(self):
        assert resolver._is_first_person("I have a family.")
        assert resolver._is_first_person("we have not been able to obtain a copy")
        assert resolver._is_first_person("My car stopped working")
        assert resolver._is_first_person("I'm certain of it")
        # Whole words only: these must not trip it.
        assert not resolver._is_first_person("Miami is warm and Ourense is in Spain")
        assert not resolver._is_first_person("Honda published bulletins for the Pilot")
        assert not resolver._is_first_person("")
        assert not resolver._is_first_person(None)


class TestEveryCitedSourceIsReached:
    """Pattern 3: a marker the draft cited was never checked.

    ``MAX_CANDIDATES`` capped a *flattened* URL list built marker-by-marker, so
    one marker carrying two URLs could spend another marker's slot. Measured on
    the GPS draft: a claim citing markers [9], [3] and [4] produced five
    candidate URLs, the cap kept three, and marker [4] was never fetched — then
    the claim was reported unsupported.
    """

    DRAFT = """Intro paragraph.

The bulletins cover the affected models. [9][3][4]

## Sources

[3] Honda ServiceNews A21120A. https://example.invalid/a21120a.pdf
[4] Honda ServiceNews A21120B. https://example.invalid/a21120b-v1.pdf and
the reissue at https://example.invalid/a21120b-v2.pdf
[9] Acura ServiceNews B21120A. https://example.invalid/b21120a.pdf and
https://example.invalid/b23040a.pdf
"""

    def test_every_cited_marker_contributes_before_any_marker_repeats(self):
        index = dc.DraftCitations(self.DRAFT)

        ordered = index._urls_for_keys(["9", "3", "4"])

        assert ordered[:3] == [
            "https://example.invalid/b21120a.pdf",
            "https://example.invalid/a21120a.pdf",
            "https://example.invalid/a21120b-v1.pdf",
        ], "expected one URL per marker before any marker's second"

    def test_cap_does_not_drop_a_cited_marker(self):
        index = dc.DraftCitations(self.DRAFT)

        candidates = index.candidates_for("The bulletins cover the affected models.")

        for marker_url in (
            "https://example.invalid/b21120a.pdf",
            "https://example.invalid/a21120a.pdf",
            "https://example.invalid/a21120b-v1.pdf",
        ):
            assert marker_url in candidates, (
                f"{marker_url} was cited for this claim and never checked"
            )

    def test_second_version_of_a_bundled_marker_is_reachable(self):
        """Source [4] bundles two versions; the text may be in the second."""
        index = dc.DraftCitations(self.DRAFT)

        candidates = index.candidates_for("The bulletins cover the affected models.")

        assert "https://example.invalid/a21120b-v2.pdf" in candidates


class TestMultiSourceFailureIsNotReportedAsRefutation:
    """A claim spread across several sources is not refuted by any one of them.

    The GPS draft's "Honda and Acura issued bulletins ... covering the 2001-03
    CL, 2001-02 MDX, 2000-03 RL, 2000-03 TL, 2000-04 Odyssey and 2003-05 Pilot
    [5][6][7]" is true of the three bulletins together and of none alone. The
    checker read the Odyssey/Pilot one, correctly saw no CL or MDX in it, and
    the run reported the sentence contradicted — true of that document, false
    of the sentence.
    """

    def test_note_says_each_source_was_judged_alone(self, monkeypatch):
        def fake_resolve(claim, url, **kwargs):
            return {
                "url": url,
                "resolved": False,
                "verification": "content_mismatch",
                "relevance_verdict": "contradicts",
                "relevance_reason": "lists Odyssey and Pilot only",
                "note": "Source URL loaded ... does not support this specific claim.",
            }

        monkeypatch.setattr(resolver, "_resolve_known_url", fake_resolve)

        result = resolver._resolve_candidates(
            "Honda and Acura issued bulletins covering the CL, MDX, RL, TL, "
            "Odyssey and Pilot",
            ["https://example.invalid/1", "https://example.invalid/2"],
        )

        assert result["checked_individually"] is True
        assert "judged separately" in result["note"]
        assert "not evidence the claim is wrong" in result["note"]

    def test_single_source_failure_keeps_its_plain_wording(self):
        """One cited source, one verdict — no joint-support caveat to add."""

        def fake_resolve(claim, url, **kwargs):
            return {
                "url": url,
                "resolved": False,
                "verification": "content_mismatch",
                "relevance_verdict": "contradicts",
                "note": "Source URL loaded ... does not support this specific claim.",
            }

        import pytest

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(resolver, "_resolve_known_url", fake_resolve)
            result = resolver._resolve_candidates(
                "a claim", ["https://example.invalid/only"]
            )

        assert "checked_individually" not in result
        assert "judged separately" not in result["note"]


class TestRateLimitedCheckIsNotAFalseNegative:
    """A 429 must not read as "the source does not support the claim".

    The live run behind this work took repeated Mistral 429s, so the question
    is load-bearing: if a rate-limited relevance call degraded to a verdict,
    every throttled claim would become a false positive in the block. It does
    not — a failed call returns ``checked: False`` and the caller reports
    "could not assess". This pins that, because it is the kind of honesty that
    is easy to lose in a refactor.
    """

    def test_failed_call_does_not_produce_a_verdict(self, monkeypatch):
        monkeypatch.setattr(
            resolver.llm,
            "call_provider",
            lambda *a, **k: {"failed": True, "error": "429 rate limit exceeded"},
        )

        verdict_info, _call_log = resolver._verify_relevance(
            "a claim", "page text " * 50, {"mistral": {"api_key": "x"}}
        )

        assert verdict_info["checked"] is False
        assert verdict_info.get("verdict") is None
        assert "429" in verdict_info["reason"]

    def test_raised_exception_does_not_produce_a_verdict(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(resolver.llm, "call_provider", boom)

        verdict_info, call_log = resolver._verify_relevance(
            "a claim", "page text " * 50, {"mistral": {"api_key": "x"}}
        )

        assert verdict_info["checked"] is False
        assert call_log is None
