"""A date range the fact-check model copied from its own source label is not a model year.

The 2026-09-18 GPS run (``run_3_20260918_014044``, issue #302). The draft says
"[3] covers the Accord, Odyssey, Pilot, Ridgeline and Crosstour" and its
reference list says "[3] Honda ServiceNews A21120A, 2022-23, via NHTSA". Mistral
returned the claim as

    Honda ServiceNews A21120A (2022-23) covers the Accord, Odyssey, Pilot, ...

with ``source`` "Honda ServiceNews A21120A (2022-23), via NHTSA": the marker
became the reference entry, and the entry's date range came with it. The
citation verifier took "2022-23" for the model years in question and refuted the
claim against a page listing exactly the vehicles named: "does not include the
2022-23 Accord, Odyssey, Pilot, Ridgeline, or Crosstour."

The fix hands the verifier the claim without the copied parenthetical, and
nothing else. The report keeps the model's own wording.

No network access.
"""

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review.adapters.citation import resolver

#: The draft's reference list writes the range without parentheses.
GPS_DRAFT = """\
Honda split its own bulletins by map disc: [3] covers the Accord, Odyssey,
Pilot, Ridgeline and Crosstour, [4] the Civic, CR-V, CR-Z, Element, Fit and
Insight.

## Sources

[3] Honda ServiceNews A21120A, 2022-23, via NHTSA. https://example.invalid/a21120a

[4] Honda ServiceNews A21120B, 2022-23, via NHTSA. https://example.invalid/a21120b
"""

A21120A_CLAIM = (
    "Honda ServiceNews A21120A (2022-23) covers the Accord, Odyssey, Pilot, "
    "Ridgeline and Crosstour"
)
A21120A_SOURCE = "Honda ServiceNews A21120A (2022-23), via NHTSA"
A21120A_CLEAN = "Honda ServiceNews A21120A covers the Accord, Odyssey, Pilot, Ridgeline and Crosstour"


class TestClaimForVerifier:
    def test_a_range_copied_from_the_source_label_is_dropped(self):
        assert (
            pipeline._claim_for_verifier(A21120A_CLAIM, A21120A_SOURCE, GPS_DRAFT)
            == A21120A_CLEAN
        )

    def test_a_month_and_year_copied_from_the_label_is_dropped_too(self):
        claim = (
            "Honda ServiceNews B18010I (January 2018) covers 2001-03 CL, "
            "2001-02 MDX and 2003-05 Pilot"
        )
        source = "Honda ServiceNews B18010I (January 2018), via NHTSA"

        assert pipeline._claim_for_verifier(claim, source, GPS_DRAFT) == (
            "Honda ServiceNews B18010I covers 2001-03 CL, 2001-02 MDX and 2003-05 Pilot"
        )

    def test_a_parenthetical_the_author_wrote_stays(self):
        """Measured on the grok run of 2026-09-09: the draft's own wording.

        "IARC Monograph 98 (2010)" is part of the assertion, and it is in the
        model's source label as well. Matching on the label alone would delete
        it, which is why the draft is consulted.
        """
        draft = (
            "A classification confirmed in IARC Monograph 98 (2010) and "
            "reaffirmed in Volume 124 (2020)."
        )
        claim = (
            "shift work is probably carcinogenic, confirmed in IARC Monograph "
            "98 (2010) and reaffirmed in Volume 124 (2020)"
        )
        source = "IARC Monographs Volume 98 (2010) and Volume 124 (2020)"

        assert pipeline._claim_for_verifier(claim, source, draft) == claim

    def test_the_draft_check_survives_a_different_dash(self):
        draft = "The bulletin (2022–23) was revised."
        claim = "Bulletin A21120A (2022-23) was revised"

        assert (
            pipeline._claim_for_verifier(claim, "Bulletin A21120A (2022-23)", draft)
            == claim
        )

    def test_a_parenthetical_the_label_does_not_carry_stays(self):
        claim = "The A21120A bulletin (2022-23) covers the Accord"

        assert (
            pipeline._claim_for_verifier(
                claim, "Honda ServiceNews A21120A, via NHTSA", ""
            )
            == claim
        )

    def test_a_parenthetical_without_a_year_stays(self):
        claim = "Bulletin A21120A (Version 2) covers the Accord"

        assert (
            pipeline._claim_for_verifier(claim, "Bulletin A21120A (Version 2)", "")
            == claim
        )

    def test_no_source_means_nothing_to_compare_against(self):
        assert (
            pipeline._claim_for_verifier(A21120A_CLAIM, "", GPS_DRAFT) == A21120A_CLAIM
        )

    def test_a_claim_that_is_only_the_parenthetical_is_left_alone(self):
        assert pipeline._claim_for_verifier("(2022-23)", "Bulletin (2022-23)", "") == (
            "(2022-23)"
        )

    def test_punctuation_is_not_left_stranded(self):
        claim = "Bulletin A21120A (2022-23), issued in June, covers the Accord"

        assert pipeline._claim_for_verifier(
            claim, "Bulletin A21120A (2022-23)", ""
        ) == ("Bulletin A21120A, issued in June, covers the Accord")


class TestCollectedClaimCarriesWhatTheVerifierSees:
    def _fact_check(self):
        return {
            "confirmed": [
                {
                    "claim": A21120A_CLAIM,
                    "source": A21120A_SOURCE,
                    "source_url": "https://example.invalid/a21120a",
                }
            ]
        }

    def test_the_model_wording_is_kept_and_the_cleaned_one_added(self):
        (entry,) = pipeline._collect_citation_claims(self._fact_check(), GPS_DRAFT)

        assert entry["claim"] == A21120A_CLAIM
        assert entry["verify_claim"] == A21120A_CLEAN

    def test_a_claim_that_needed_no_cleaning_gains_no_key(self):
        fact_check = {
            "confirmed": [
                {
                    "claim": "a plain claim",
                    "source": "Example (2022-23)",
                    "source_url": "https://example.invalid/x",
                }
            ]
        }

        (entry,) = pipeline._collect_citation_claims(fact_check, GPS_DRAFT)

        assert "verify_claim" not in entry

    def test_buckets_with_no_source_text_are_untouched(self):
        fact_check = {"unverifiable": [{"claim": A21120A_CLAIM}]}

        (entry,) = pipeline._collect_citation_claims(fact_check, GPS_DRAFT)

        assert "verify_claim" not in entry


class _Response:
    """The few attributes ``_resolve_known_url`` reads from a fetched page."""

    def __init__(self, text):
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": "text/plain"}
        self.encoding = "utf-8"
        self.url = "https://example.invalid/a21120a"

    def raise_for_status(self):
        return None


_PAGE = (
    "AFFECTED VEHICLES Year Model Trim Level 2006-12 Accord Coupe and Sedan "
    "2010-12 Accord Crosstour 2006-07 Accord Hybrid 2005-10 Odyssey 2006-11 "
    "Pilot 2006-14 Ridgeline. " * 6
)


@pytest.fixture
def seen_by_verifier(monkeypatch):
    """Fetch a fixed page and record the claim text the relevance check is given."""
    seen = []

    def fake_verify(claim, content, api_keys, author=None):
        seen.append(claim)
        return {"checked": True, "verdict": "not_addressed", "reason": "r"}, None

    monkeypatch.setattr(resolver, "safe_get", lambda url, timeout=15: _Response(_PAGE))
    monkeypatch.setattr(
        resolver.wayback, "check", lambda url, timeout=10: {"archived": False}
    )
    monkeypatch.setattr(resolver, "_verify_relevance", fake_verify)
    return seen


class TestVerifierIsShownTheCleanedClaim:
    def test_the_relevance_check_never_sees_the_copied_range(self, seen_by_verifier):
        resolver._resolve_known_url(
            A21120A_CLAIM,
            "https://example.invalid/a21120a",
            verify_claim=A21120A_CLEAN,
        )

        assert seen_by_verifier == [A21120A_CLEAN]
        assert "2022-23" not in seen_by_verifier[0]

    def test_the_result_still_carries_the_model_wording(self, seen_by_verifier):
        result = resolver._resolve_known_url(
            A21120A_CLAIM,
            "https://example.invalid/a21120a",
            verify_claim=A21120A_CLEAN,
        )

        assert result["claim"] == A21120A_CLAIM

    def test_without_a_cleaned_claim_the_original_is_checked(self, seen_by_verifier):
        resolver._resolve_known_url(A21120A_CLAIM, "https://example.invalid/a21120a")

        assert seen_by_verifier == [A21120A_CLAIM]

    def test_resolve_citations_carries_it_from_the_claim_entry(
        self, seen_by_verifier, monkeypatch
    ):
        """The whole path: collector entry in, verifier input and report out."""
        monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_note_breaker_state", lambda: None)
        monkeypatch.setattr(resolver, "_verify_archive_matches", lambda results: None)

        (entry,) = pipeline._collect_citation_claims(
            {
                "confirmed": [
                    {
                        "claim": A21120A_CLAIM,
                        "source": A21120A_SOURCE,
                        "source_url": "https://example.invalid/a21120a",
                    }
                ]
            },
            GPS_DRAFT,
        )

        (result,) = resolver.resolve_citations(
            [entry], [], {"mistral": {"api_key": "k"}}
        )

        # More than one call is expected: the draft cites [4] near the claim as
        # well, and a source that does not support it sends the check on to the
        # next. What matters is that none of them was shown the copied range.
        assert seen_by_verifier
        assert set(seen_by_verifier) == {A21120A_CLEAN}
        assert result["claim"] == A21120A_CLAIM
        assert result["verified_as"] == A21120A_CLEAN

    def test_a_plain_string_claim_still_resolves(self, seen_by_verifier, monkeypatch):
        monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_note_breaker_state", lambda: None)
        monkeypatch.setattr(resolver, "_verify_archive_matches", lambda results: None)

        resolver.resolve_citations(
            [{"claim": "a claim", "known_urls": ["https://example.invalid/x"]}],
            [],
            {"mistral": {"api_key": "k"}},
        )

        assert seen_by_verifier == ["a claim"]
