"""Tests for consolidation.find_contradictions."""

from ci_article_review.consolidation import find_contradictions


def _result(
    model, domain, confirmed=None, contradicted=None, outdated=None, failed=False
):
    return (model, domain), {
        "failed": failed,
        "data": {
            "confirmed": confirmed or [],
            "contradicted": contradicted or [],
            "outdated": outdated or [],
        },
    }


class TestFindContradictions:
    def test_no_contradiction_when_both_confirm(self):
        r1 = _result("gemini", "fact_check", confirmed=[{"claim": "GDP grew 2%"}])
        r2 = _result("perplexity", "fact_check", confirmed=[{"claim": "GDP grew 2%"}])
        results = dict([r1, r2])
        assert find_contradictions(results) == []

    def test_detects_confirmed_vs_contradicted(self):
        r1 = _result(
            "gemini", "fact_check", confirmed=[{"claim": "Unemployment is 4%"}]
        )
        r2 = _result(
            "perplexity", "fact_check", contradicted=[{"claim": "Unemployment is 4%"}]
        )
        results = dict([r1, r2])
        contradictions = find_contradictions(results)
        assert len(contradictions) == 1
        c = contradictions[0]
        assert "gemini" in c["confirmed_by"]
        assert "perplexity" in c["challenged_by"]
        assert c["challenge_type"] == "contradicted"

    def test_detects_confirmed_vs_outdated(self):
        r1 = _result("gemini", "fact_check", confirmed=[{"claim": "CPI is 3.2%"}])
        r2 = _result("perplexity", "fact_check", outdated=[{"claim": "CPI is 3.2%"}])
        results = dict([r1, r2])
        contradictions = find_contradictions(results)
        assert len(contradictions) == 1
        assert contradictions[0]["challenge_type"] == "outdated"

    def test_mixed_challenge_type(self):
        r1 = _result("gemini", "fact_check", confirmed=[{"claim": "Rate is 5%"}])
        r2 = _result("perplexity", "fact_check", contradicted=[{"claim": "Rate is 5%"}])
        r3 = _result("openai", "fact_check", outdated=[{"claim": "Rate is 5%"}])
        results = dict([r1, r2, r3])
        contradictions = find_contradictions(results)
        assert len(contradictions) == 1
        assert contradictions[0]["challenge_type"] == "mixed"

    def test_skips_non_fact_check_domains(self):
        r1 = _result("openai", "voice_style", confirmed=[{"claim": "Some passage"}])
        r2 = _result(
            "claude", "argument_integrity", contradicted=[{"claim": "Some passage"}]
        )
        results = dict([r1, r2])
        assert find_contradictions(results) == []

    def test_skips_failed_results(self):
        r1 = _result("gemini", "fact_check", confirmed=[{"claim": "X is true"}])
        r2 = _result(
            "perplexity",
            "fact_check",
            contradicted=[{"claim": "X is true"}],
            failed=True,
        )
        results = dict([r1, r2])
        assert find_contradictions(results) == []

    def test_empty_results_no_crash(self):
        assert find_contradictions({}) == []

    def test_no_overlap_no_contradiction(self):
        r1 = _result("gemini", "fact_check", confirmed=[{"claim": "Claim A"}])
        r2 = _result(
            "perplexity",
            "fact_check",
            contradicted=[{"claim": "Claim B totally different"}],
        )
        results = dict([r1, r2])
        # No passage key overlap → no contradiction
        assert find_contradictions(results) == []

    def test_returns_confirmed_by_and_challenged_by(self):
        r1 = _result("gemini", "fact_check", confirmed=[{"claim": "Inflation is 2%"}])
        r2 = _result(
            "perplexity", "fact_check", confirmed=[{"claim": "Inflation is 2%"}]
        )
        r3 = _result(
            "openai", "fact_check", contradicted=[{"claim": "Inflation is 2%"}]
        )
        results = dict([r1, r2, r3])
        contradictions = find_contradictions(results)
        assert len(contradictions) == 1
        c = contradictions[0]
        assert set(c["confirmed_by"]) == {"gemini", "perplexity"}
        assert c["challenged_by"] == ["openai"]


class TestUnverifiableIsAChallenge:
    """One model confirming what another cannot verify is a disagreement.

    Only ``outdated`` and ``contradicted`` counted, and those are the two
    buckets this ensemble almost never fills: both were empty across all six
    fact-check passes on 2026-09-03 while five claims were confirmed by one
    model and marked unverifiable by another. The report said 0 contradictions.
    Among the hidden five: "I have a family." — reported as *confirmed* by a
    model that cannot possibly have checked it.
    """

    def _res(self, model, confirmed=None, unverifiable=None):
        return (model, "fact_check"), {
            "failed": False,
            "data": {
                "confirmed": confirmed or [],
                "contradicted": [],
                "outdated": [],
                "unverifiable": unverifiable or [],
            },
        }

    def test_confirmed_versus_unverifiable_is_reported(self):
        results = dict(
            [
                self._res("openai", confirmed=[{"claim": "I have a family."}]),
                self._res("mistral", unverifiable=[{"claim": "I have a family."}]),
            ]
        )
        found = find_contradictions(results)
        assert len(found) == 1
        assert found[0]["challenge_type"] == "unverifiable"
        assert found[0]["confirmed_by"] == ["openai"]
        assert found[0]["challenged_by"] == ["mistral"]

    def test_all_models_unverifiable_is_agreement_not_conflict(self):
        results = dict(
            [
                self._res("openai", unverifiable=[{"claim": "I have a family."}]),
                self._res("mistral", unverifiable=[{"claim": "I have a family."}]),
            ]
        )
        assert find_contradictions(results) == []

    def test_one_model_in_two_buckets_is_not_a_cross_model_conflict(self):
        """A malformed response, not a disagreement between models."""
        results = dict(
            [
                self._res(
                    "openai",
                    confirmed=[{"claim": "I have a family."}],
                    unverifiable=[{"claim": "I have a family."}],
                ),
            ]
        )
        assert find_contradictions(results) == []

    def test_the_same_claim_quoted_at_two_lengths_still_matches(self):
        """Exact-string keying missed these entirely."""
        short = "I have a day job running network infrastructure."
        long = short + " I have a side job. I have a family."
        results = dict(
            [
                self._res("openai", confirmed=[{"claim": short}]),
                self._res("claude", unverifiable=[{"claim": long}]),
            ]
        )
        found = find_contradictions(results)
        assert len(found) == 1
        assert found[0]["confirmed_by"] == ["openai"]
        assert found[0]["challenged_by"] == ["claude"]
