"""Fact-check verdicts must carry the evidence the prompt requires.

``prompts/fact_check.txt`` (EVIDENCE REQUIREMENTS) says every claim in
``confirmed``, ``outdated`` or ``contradicted`` needs a directly openable
``source_url`` and a ``supporting_quote`` copied verbatim from that page, and
that a claim without them belongs in ``primary_source_needed`` or
``unverifiable``. Consolidation used to enforce only part of that, and only on
``confirmed`` (issue #334):

- the quote was never read — a verdict quoting nothing stayed confirmed;
- an unlinked source that named a document ("Honda ServiceNews B18010I") kept a
  verdict confirmed, where the prompt sends it to ``primary_source_needed``;
- the demotion ran inside ``_build_fact_check`` only, so Section 1 and
  ``find_contradictions`` still read the demoted verdict as confirmed.

Measured on the saved runs, 2026-10-05 (888 verdicts, 99 fact-check passes):
no quote was ever blank — strict mode makes the model write something — but
five were placeholders ("N/A" x3, "placeholder", "[]"), two of which stayed
confirmed, and 11 mistral verdicts confirmed an about-page draft by quoting it
back from the publication's own published copy.
"""

import pytest

from ci_article_review import consolidation
from ci_article_review.report_markdown import render_report_markdown

_URL = "https://www.nhtsa.gov/report.pdf"
_QUOTE = "The clock reads 00:00 after the rollover."


def _results(data, model="gemini"):
    return {
        (model, "fact_check"): {
            "failed": False,
            "data": data,
            "model": model,
            "tokens": {},
        }
    }


def _verdict(**fields):
    return {"claim": "The clock is stuck at 00:00.", **fields}


def _section(data, own_site_url=None):
    """Section 2 as ``build_report`` builds it: normalised first, then merged."""
    results, _ = consolidation._normalise_fact_check_results(
        _results(data), own_site_url
    )
    return consolidation._build_fact_check(results, {})


class TestNoOpenableUrl:
    def test_the_draft_cannot_confirm_itself(self):
        fc = _section(
            {"confirmed": [_verdict(source="Draft Article", source_url="N/A")]}
        )
        assert fc["confirmed"] == []
        assert len(fc["unverifiable"]) == 1

    def test_the_model_doing_arithmetic_is_not_a_source(self):
        fc = _section(
            {
                "confirmed": [
                    _verdict(
                        claim="the sum is 1024",
                        source="Manual Calculation",
                        source_url="N/A",
                    )
                ]
            }
        )
        assert fc["confirmed"] == []
        demoted = fc["unverifiable"][0]
        assert demoted["claim"] == "the sum is 1024"
        assert demoted["checked"] == "Manual Calculation"
        assert "no external source" in demoted["reason"]
        assert demoted["demoted_from"] == "confirmed"

    def test_a_named_document_without_a_url_needs_its_primary_source(self):
        """The prompt's rule: "If you cannot produce such a URL, the claim does
        not belong in these buckets: put it in primary_source_needed". This
        reverses the old behaviour, which kept it confirmed (issue #334)."""
        fc = _section(
            {
                "confirmed": [
                    _verdict(
                        source="Honda ServiceNews B18010I",
                        source_url="",
                        supporting_quote=_QUOTE,
                    )
                ]
            }
        )
        assert fc["confirmed"] == []
        moved = fc["primary_source_needed"][0]
        assert moved["best_candidate_source"] == "Honda ServiceNews B18010I"
        assert moved["best_candidate_url"] is None
        assert moved["demoted_from"] == "confirmed"

    def test_one_real_document_among_several_is_still_a_candidate(self):
        fc = _section(
            {
                "confirmed": [
                    _verdict(source="Draft Article; Bianchi Honda", source_url="N/A")
                ]
            }
        )
        assert [p["best_candidate_source"] for p in fc["primary_source_needed"]] == [
            "Draft Article; Bianchi Honda"
        ]

    def test_a_url_that_is_not_openable_does_not_count(self):
        """The prompt asks for one "starting with http:// or https://"."""
        fc = _section(
            {
                "confirmed": [
                    _verdict(
                        source="GPS.gov", source_url="gps.gov", supporting_quote=_QUOTE
                    )
                ]
            }
        )
        assert fc["confirmed"] == []
        assert len(fc["primary_source_needed"]) == 1

    def test_a_url_written_into_the_source_text_counts(self):
        """Output from before `source_url` existed put the link in `source`;
        the citation collector already reads it from there."""
        fc = _section(
            {
                "confirmed": [
                    _verdict(source=f"NHTSA bulletin, {_URL}", supporting_quote=_QUOTE)
                ]
            }
        )
        assert len(fc["confirmed"]) == 1


class TestNoVerbatimQuote:
    @pytest.mark.parametrize("quote", ["", "   ", "N/A", "placeholder", "[]", '"none"'])
    def test_a_verdict_quoting_nothing_is_not_kept(self, quote):
        fc = _section(
            {
                "confirmed": [
                    _verdict(source="NHTSA", source_url=_URL, supporting_quote=quote)
                ]
            }
        )
        assert fc["confirmed"] == []
        demoted = fc["unverifiable"][0]
        assert demoted["demoted_from"] == "confirmed"
        assert "without a verbatim quote" in demoted["reason"]

    def test_the_url_is_kept_for_section_9_to_read(self):
        fc = _section({"confirmed": [_verdict(source="NHTSA", source_url=_URL)]})
        assert fc["unverifiable"][0]["sources_checked"] == [_URL]

    def test_url_and_quote_together_stay_confirmed(self):
        fc = _section(
            {
                "confirmed": [
                    _verdict(source="NHTSA", source_url=_URL, supporting_quote=_QUOTE)
                ]
            }
        )
        assert len(fc["confirmed"]) == 1
        assert fc.get("unverifiable", []) == []


class TestEveryVerdictBucket:
    """The prompt's rules cover all three verdict buckets, not only confirmed."""

    def test_an_unquoted_outdated_verdict_keeps_its_current_value(self):
        fc = _section(
            {
                "outdated": [
                    _verdict(
                        current_value="fixed by update 3.1",
                        source="NHTSA",
                        source_url=_URL,
                        supporting_quote="",
                    )
                ]
            }
        )
        assert fc["outdated"] == []
        demoted = fc["unverifiable"][0]
        assert demoted["demoted_from"] == "outdated"
        assert "fixed by update 3.1" in demoted["reason"]

    def test_an_unlinked_contradicted_verdict_keeps_its_contradiction(self):
        fc = _section(
            {
                "contradicted": [
                    _verdict(
                        contradiction="the clock reads 12:00",
                        source="Honda ServiceNews",
                        source_url="",
                        supporting_quote=_QUOTE,
                    )
                ]
            }
        )
        assert fc["contradicted"] == []
        moved = fc["primary_source_needed"][0]
        assert moved["demoted_from"] == "contradicted"
        assert "the clock reads 12:00" in moved["reason"]


class TestOwnPublishedCopy:
    _SITE = "https://www.mikehammett.net"
    _CLAIM = "Every claim gets checked against public records."

    def _echo(self, url, quote=None):
        return {
            "confirmed": [
                {
                    "claim": self._CLAIM,
                    "source": "About page",
                    "source_url": url,
                    "supporting_quote": quote or self._CLAIM,
                }
            ]
        }

    def test_quoting_the_claim_back_from_the_own_site_is_not_evidence(self):
        fc = _section(self._echo("https://mikehammett.net/about"), self._SITE)
        assert fc["confirmed"] == []
        demoted = fc["unverifiable"][0]
        assert demoted["sources_checked"] == [], (
            "Section 9 would resolve the same page and confirm it again"
        )
        assert "own site" in demoted["reason"]

    def test_a_different_quote_from_the_own_site_is_still_a_quote(self):
        fc = _section(
            self._echo(
                "https://www.mikehammett.net/2024/earlier-post",
                "In 2024 the county approved 14 permits.",
            ),
            self._SITE,
        )
        assert len(fc["confirmed"]) == 1

    def test_a_draft_quoting_another_site_verbatim_is_fine(self):
        """Common and legitimate: the draft quotes its source word for word."""
        fc = _section(self._echo("https://broadbandbreakfast.com/story"), self._SITE)
        assert len(fc["confirmed"]) == 1

    def test_without_a_site_url_the_check_is_skipped(self):
        fc = _section(self._echo("https://mikehammett.net/about"), None)
        assert len(fc["confirmed"]) == 1


class TestEveryReaderSeesTheSameBuckets:
    """The demotion used to happen inside Section 2 only."""

    _CLAIM = "The clock is stuck at 00:00."

    def _two_models(self):
        return {
            ("gemini", "fact_check"): {
                "failed": False,
                "data": {
                    "confirmed": [
                        {
                            "claim": self._CLAIM,
                            "source": "Manual Calculation",
                            "source_url": "N/A",
                        }
                    ]
                },
                "model": "gemini",
                "tokens": {},
            },
            ("openai", "fact_check"): {
                "failed": False,
                "data": {"unverifiable": [{"claim": self._CLAIM, "reason": "r"}]},
                "model": "openai",
                "tokens": {},
            },
        }

    def test_a_demoted_verdict_is_no_longer_a_confirmation_in_contradictions(self):
        results, _ = consolidation._normalise_fact_check_results(self._two_models())
        assert consolidation.find_contradictions(results) == []

    def test_a_demoted_verdict_votes_in_section_1_as_what_it_became(self):
        results, _ = consolidation._normalise_fact_check_results(self._two_models())
        passages = consolidation._extract_passages(
            "gemini", "fact_check", results[("gemini", "fact_check")]
        )
        assert [flag["type"] for _, flag in passages] == ["unverifiable"]

    def test_build_report_applies_it_end_to_end(self):
        report = consolidation.build_report(
            article_title="t",
            publication_name="p",
            run_number=1,
            corrected_draft=self._CLAIM,
            lt_result=None,
            results=self._two_models(),
            ensemble_cfg={},
            api_call_log=[],
        )
        assert report["section_2_fact_check"]["confirmed"] == []
        assert report["contradictions"] == []

    def test_a_cut_off_pass_still_reports_the_buckets_it_never_sent(self):
        """Moving a verdict creates `unverifiable` where the model wrote none.

        Truncation is read off which bucket keys are present, so reading it
        after the move reported `unverifiable` and `primary_source_needed` as
        delivered by a pass that was cut off inside `contradicted`.
        """
        results = {
            ("gemini", "fact_check"): {
                "failed": False,
                "truncated": True,
                "data": {
                    "confirmed": [_verdict(source="NHTSA", source_url=_URL)],
                    "outdated": [],
                },
                "raw": '{"confirmed": [...], "outdated": [], "contradicted": [{"cl',
                "model": "gemini",
                "tokens": {},
            }
        }
        report = consolidation.build_report(
            article_title="t",
            publication_name="p",
            run_number=1,
            corrected_draft="d",
            lt_result=None,
            results=results,
            ensemble_cfg={},
            api_call_log=[],
        )
        (detail,) = report["truncated_result_details"]
        assert detail["last_bucket"] == "outdated"
        assert detail["missing_buckets"][:3] == [
            "contradicted",
            "unverifiable",
            "primary_source_needed",
        ]
        assert len(report["section_2_fact_check"]["unverifiable"]) == 1

    def test_the_callers_results_are_not_mutated(self):
        results = self._two_models()
        before = repr(results)
        consolidation._normalise_fact_check_results(results)
        assert repr(results) == before

    def test_existing_unverifiable_findings_are_preserved(self):
        fc = _section(
            {
                "confirmed": [_verdict(source="Draft Article", source_url="N/A")],
                "unverifiable": [{"claim": "already here", "reason": "r"}],
            }
        )
        assert {u["claim"] for u in fc["unverifiable"]} == {
            "already here",
            "The clock is stuck at 00:00.",
        }


class TestSection2SaysWhatMoved:
    def test_the_count_of_moved_verdicts_is_printed(self):
        fc = _section(
            {
                "confirmed": [
                    _verdict(source="NHTSA", source_url=_URL, supporting_quote=_QUOTE),
                    _verdict(claim="other", source="NHTSA", source_url=_URL),
                ]
            }
        )
        md = render_report_markdown(
            {
                "generated": "2026-10-05T00:00:00+00:00",
                "run_number": 1,
                "article_title": "t",
                "publication": "p",
                "section_2_fact_check": fc,
            }
        )
        assert "1 verdict(s) came back without the evidence the" in md
        assert "1 of 1 verdict(s) arrived with a verbatim supporting quote" in md
        assert "Demoted from: confirmed" in md
