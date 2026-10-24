"""The readers of the claim ledger: worklist, Section 2 header, Section 9 header.

Part 2 of issue #338. Part 1 built ``report["claim_ledger"]`` once, after
citation resolution. These tests hold the readers to it: the worklist's
"possible error" banner must come from the ledger and say which status earned
it, and the two section headers must state the ledger's counts, so a reader
meets the net status before the per-model verdicts and per-citation tiers that
used to be the only thing on the page.
"""

from ci_article_review import report_markdown
from ci_article_review.claim_status import (
    build_claim_ledger,
    label_for,
    status_by_claim,
)
from ci_article_review.worklist import build_worklist, render_worklist

CLAIM = "The exchange carries forty gigabits of peak traffic."


def _refused(claim):
    return {
        "claim": claim,
        "url": "https://example.org/refused",
        "resolved": False,
        "note": (
            "Known source URL could not be fetched: 403 Client Error: "
            "Forbidden for url: https://example.org/refused"
        ),
        "wayback": {"url": "https://example.org/refused", "archived": False},
        "fact_check_bucket": "confirmed",
    }


def _report(citations=(), fact_check=None, ledger=None):
    report = {
        "section_1_consensus": [],
        "section_2_fact_check": dict(fact_check or {}),
        "section_9_citations": list(citations),
    }
    if ledger is not None:
        report["claim_ledger"] = ledger
    return report


def _ledger(**status_by_text):
    """A ledger with the given claim -> status pairs and nothing else."""
    claims = [
        {"claim": text, "status": status, "why": "test", "evidence": {"variants": []}}
        for text, status in status_by_text.items()
    ]
    counts = {}
    for entry in claims:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {"claims": claims, "counts": counts, "citations_resolved": True}


def _rendered(report):
    return "\n".join(render_worklist(build_worklist(report)))


class TestStatusByClaim:
    def test_maps_the_claim_and_every_variant(self):
        report = _report(
            fact_check={
                "confirmed": [{"claim": CLAIM, "source_model": "gemini"}],
                "unverifiable": [{"claim": CLAIM + "..", "source_model": "openai"}],
            }
        )
        mapping = status_by_claim(build_claim_ledger(report))
        assert mapping[CLAIM] == "confirmed_source_not_read"
        assert mapping[CLAIM + ".."] == "confirmed_source_not_read"

    def test_an_empty_or_missing_ledger_maps_nothing(self):
        assert status_by_claim({}) == {}
        assert status_by_claim(None) == {}


class TestTheWorklistReadsTheLedger:
    def test_the_ledger_decides_what_is_suspect(self):
        # No contradicted bucket, no mismatch: only the ledger says so.
        report = _report(
            [_refused("suspect"), _refused("routine")],
            ledger=_ledger(suspect="contradicted", routine="confirmed_source_not_read"),
        )
        items = build_worklist(report)["items"]
        assert items[0]["possibly_wrong"] is True
        assert items[0]["possibly_wrong_status"] == "contradicted"
        assert "suspect" in items[0]["claims"]

    def test_a_source_that_does_not_support_is_suspect_too(self):
        report = _report(
            [_refused("suspect")], ledger=_ledger(suspect="source_does_not_support")
        )
        item = build_worklist(report)["items"][0]
        assert item["possibly_wrong"] is True
        assert item["possibly_wrong_status"] == "source_does_not_support"

    def test_a_confirmed_or_unverified_claim_is_not(self):
        for status in (
            "confirmed_source_read",
            "confirmed_source_not_read",
            "unverified",
            "excluded_by_author",
        ):
            report = _report([_refused("claim")], ledger=_ledger(claim=status))
            item = build_worklist(report)["items"][0]
            assert item["possibly_wrong"] is False, status
            assert item["possibly_wrong_status"] is None

    def test_contradicted_outranks_does_not_support_on_one_item(self):
        # Two claims behind the same refused URL: one action, two statuses.
        report = _report(
            [_refused("a"), _refused("b")],
            ledger=_ledger(a="source_does_not_support", b="contradicted"),
        )
        item = build_worklist(report)["items"][0]
        assert sorted(item["claims"]) == ["a", "b"]
        assert item["possibly_wrong_status"] == "contradicted"

    def test_the_banner_says_which_status_earned_it(self):
        md = _rendered(_report([_refused("s")], ledger=_ledger(s="contradicted")))
        assert "contradicted" in md
        assert "possible error in the article, not a gap in its sourcing" in md
        md = _rendered(
            _report([_refused("s")], ledger=_ledger(s="source_does_not_support"))
        )
        assert "read and does not support" in md
        assert "possible error in the article, not a gap in its sourcing" in md
        # The old wording named two buckets the ledger no longer distinguishes.
        assert "contradicted or came back outdated" not in md

    def test_an_old_report_without_a_ledger_gets_one_built(self):
        """Same rule, not a second one: a report saved before the ledger existed
        is judged by building the ledger from it, so the banner cannot disagree
        with what a current run would say."""
        report = _report(
            [
                _refused("suspect"),
                {
                    "claim": "suspect",
                    "url": "https://example.org/read",
                    "verification": "content_mismatch",
                    "relevance_verdict": "not_addressed",
                },
            ]
        )
        item = build_worklist(report)["items"][0]
        assert item["possibly_wrong"] is True
        assert item["possibly_wrong_status"] == "source_does_not_support"

    def test_a_variant_quotation_is_matched(self):
        long = "Fourteen pieces are sitting in the pipeline right now, next to the ten"
        short = (
            "Fourteen pieces are sitting in the pipeline right now, next to the ten."
        )
        report = _report(
            [_refused(short)],
            fact_check={"contradicted": [{"claim": long, "source_model": "gemini"}]},
        )
        assert build_worklist(report)["items"][0]["possibly_wrong"] is True


class TestTheSection2HeaderStatesTheCounts:
    def test_nonzero_statuses_in_precedence_order(self):
        fact_check = {"confirmed": [{"claim": "x", "source_model": "gemini"}]}
        report = _report(
            fact_check=fact_check,
            ledger=_ledger(
                x="confirmed_source_not_read",
                y="excluded_by_author",
                z="source_does_not_support",
            ),
        )
        text = "\n".join(report_markdown._render_section_2(fact_check, report))
        assert "**Net status across 3 claim(s):**" in text
        line = next(ln for ln in text.splitlines() if "Net status across" in ln)
        a = line.index(label_for("excluded_by_author").lower())
        b = line.index(label_for("source_does_not_support").lower())
        c = line.index(label_for("confirmed_source_not_read").lower())
        assert a < b < c
        assert label_for("contradicted").lower() not in line

    def test_an_unresolved_run_says_so(self):
        fact_check = {"confirmed": [{"claim": "x", "source_model": "gemini"}]}
        ledger = _ledger(x="confirmed_source_not_read")
        ledger["citations_resolved"] = False
        text = "\n".join(
            report_markdown._render_section_2(
                fact_check, _report(fact_check=fact_check, ledger=ledger)
            )
        )
        assert "Section 9 did not run" in text

    def test_absent_without_a_ledger(self):
        fact_check = {"confirmed": [{"claim": "x", "source_model": "gemini"}]}
        text = "\n".join(
            report_markdown._render_section_2(
                fact_check, _report(fact_check=fact_check)
            )
        )
        assert "Net status across" not in text


class TestTheSection9HeaderStatesTheNetStatus:
    def _citation(self, claim, verification):
        return {
            "claim": claim,
            "url": "https://example.org/p",
            "final_url": "https://example.org/p",
            "verification": verification,
            "fact_check_bucket": "confirmed",
        }

    def test_counts_only_the_claims_in_this_section(self):
        citations = [
            self._citation("a", "checksum"),
            self._citation("b", "content_mismatch"),
        ]
        report = _report(
            citations,
            fact_check={
                "confirmed": [
                    {"claim": "a", "source_model": "gemini"},
                    {"claim": "b", "source_model": "gemini"},
                    {"claim": "c", "source_model": "gemini"},
                ]
            },
        )
        ledger = build_claim_ledger(report)
        text = "\n".join(report_markdown._render_section_9(citations, ledger=ledger))
        line = next(ln for ln in text.splitlines() if "Net status of the" in ln)
        assert "2 claim(s)" in line
        assert f"1 {label_for('confirmed_source_read').lower()}" in line
        assert f"1 {label_for('source_does_not_support').lower()}" in line
        # "c" has no citation and must not be counted here.
        assert label_for("confirmed_source_not_read").lower() not in line

    def test_says_the_bucket_is_not_the_net_status(self):
        citations = [self._citation("a", "content_mismatch")]
        report = _report(
            citations,
            fact_check={"confirmed": [{"claim": "a", "source_model": "gemini"}]},
        )
        text = "\n".join(
            report_markdown._render_section_9(
                citations, ledger=build_claim_ledger(report)
            )
        )
        assert "Bucket" in text and "net status" in text

    def test_absent_without_a_ledger(self):
        citations = [self._citation("a", "checksum")]
        text = "\n".join(report_markdown._render_section_9(citations))
        assert "Net status of the" not in text

    def test_the_full_report_passes_the_ledger_through(self):
        citations = [self._citation("a", "content_mismatch")]
        report = {
            "generated": "2026-10-24T00:00:00+00:00",
            "run_number": 1,
            "article_title": "T",
            "publication": "p",
            "lt_corrections_applied": [],
            "lt_failed": False,
            "lt_skipped": False,
            "model_failures": [],
            "delta": None,
            "section_1_consensus": [],
            "section_2_fact_check": {
                "confirmed": [{"claim": "a", "source_model": "gemini"}]
            },
            "section_3_voice": [],
            "section_4_argument": [],
            "section_5_completeness": [],
            "section_6_red_team": {},
            "section_7_low_confidence": [],
            "section_8_additional": [],
            "section_9_citations": citations,
        }
        report["claim_ledger"] = build_claim_ledger(report)
        md = report_markdown.render_report_markdown(report)
        assert "Net status across 1 claim(s)" in md
        assert "Net status of the" in md
