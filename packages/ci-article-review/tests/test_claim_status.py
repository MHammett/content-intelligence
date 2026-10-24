"""Tests for claim_status: one net status per claim across Sections 1, 2 and 9.

Every fixture is shaped like the pipeline-and-verification-update captures of
2026-10-05/06 that motivated issue #338, where one claim could be ``confirmed``
by one model, ``primary_source_needed`` for another, classified out of scope by
a third, and ``content_mismatch`` in Section 9, with nothing saying which of
those the author should believe.
"""

from ci_article_review import claim_status, report_markdown
from ci_article_review.claim_status import (
    STATUSES,
    annotate_fact_check,
    author_hosts,
    build_claim_ledger,
    label_for,
)
from ci_article_review.fact_check_scope import ORIGIN_HANDOFF, ORIGIN_MODEL

CLAIM = "The exchange carries forty gigabits of peak traffic."


def _report(**sections):
    report = {
        "section_1_consensus": [],
        "section_2_fact_check": {
            "confirmed": [],
            "outdated": [],
            "contradicted": [],
            "unverifiable": [],
            "primary_source_needed": [],
            "out_of_scope": [],
        },
        "section_9_citations": [],
    }
    for key, value in sections.items():
        if key in report["section_2_fact_check"]:
            report["section_2_fact_check"][key] = value
        else:
            report[key] = value
    return report


def _verdict(claim=CLAIM, model="gemini", **extra):
    return {"claim": claim, "source_model": model, **extra}


def _citation(claim=CLAIM, verification="checksum", **extra):
    return {
        "claim": claim,
        "url": "https://example.org/stats",
        "final_url": "https://example.org/stats",
        "verification": verification,
        **extra,
    }


def _only(ledger):
    assert len(ledger["claims"]) == 1, ledger["claims"]
    return ledger["claims"][0]


class TestVocabulary:
    def test_every_status_has_a_label_and_the_order_is_the_precedence(self):
        keys = [key for key, _ in STATUSES]
        assert keys == [
            "excluded_by_author",
            "excluded_by_model",
            "contradicted",
            "source_does_not_support",
            "confirmed_source_read",
            "confirmed_source_not_read",
            "unverified",
        ]
        for key in keys:
            assert label_for(key) != key

    def test_counts_cover_every_claim_and_every_status(self):
        report = _report(
            confirmed=[_verdict("A claim about one thing that is confirmed.")],
            unverifiable=[_verdict("A second claim nobody could verify at all.")],
        )
        ledger = build_claim_ledger(report)
        assert set(ledger["counts"]) == {key for key, _ in STATUSES}
        assert sum(ledger["counts"].values()) == len(ledger["claims"]) == 2


class TestPrecedence:
    def test_author_exclusion_beats_everything(self):
        report = _report(
            contradicted=[_verdict(model="openai")],
            out_of_scope=[
                {
                    "claim": CLAIM,
                    "excluded": True,
                    "excluded_by": ORIGIN_HANDOFF,
                    "withdrawn_verdicts": [
                        {"model": "openai", "bucket": "contradicted"}
                    ],
                }
            ],
            section_9_citations=[_citation(verification="content_mismatch")],
        )
        entry = _only(build_claim_ledger(report))
        assert entry["status"] == "excluded_by_author"
        # The withdrawn verdict is named, not lost.
        assert "openai" in entry["why"] and "contradicted" in entry["why"]

    def test_model_exclusion_only_when_honoured(self):
        honoured = _report(
            confirmed=[_verdict()],
            out_of_scope=[
                {"claim": CLAIM, "excluded": True, "excluded_by": ORIGIN_MODEL}
            ],
        )
        assert _only(build_claim_ledger(honoured))["status"] == "excluded_by_model"

        reported_only = _report(
            confirmed=[_verdict()],
            out_of_scope=[
                {"claim": CLAIM, "excluded": False, "excluded_by": ORIGIN_MODEL}
            ],
        )
        entry = _only(build_claim_ledger(reported_only))
        assert entry["status"] == "confirmed_source_not_read"

    def test_one_contradicted_or_outdated_verdict_is_enough(self):
        for bucket in ("contradicted", "outdated"):
            report = _report(
                confirmed=[_verdict(model="gemini"), _verdict(model="openai")],
                **{bucket: [_verdict(model="perplexity")]},
            )
            entry = _only(build_claim_ledger(report))
            assert entry["status"] == "contradicted", bucket
            assert "perplexity" in entry["why"]

    def test_unverifiable_is_not_a_contradiction(self):
        report = _report(
            confirmed=[_verdict(model="gemini")],
            unverifiable=[_verdict(model="perplexity")],
        )
        assert _only(build_claim_ledger(report))["status"] == (
            "confirmed_source_not_read"
        )

    def test_a_page_read_outranks_a_model_assertion(self):
        report = _report(
            confirmed=[_verdict(model="gemini")],
            section_9_citations=[
                _citation(
                    verification="content_mismatch",
                    relevance_verdict="not_addressed",
                    fact_check_bucket="confirmed",
                )
            ],
        )
        entry = _only(build_claim_ledger(report))
        assert entry["status"] == "source_does_not_support"
        assert "not_addressed" in entry["why"]

    def test_a_source_that_contradicts_is_a_contradiction(self):
        report = _report(
            confirmed=[_verdict()],
            section_9_citations=[
                _citation(
                    verification="content_mismatch", relevance_verdict="contradicts"
                )
            ],
        )
        assert _only(build_claim_ledger(report))["status"] == "contradicted"

    def test_a_reask_concession_is_a_contradiction(self):
        for action in ("correct_claim", "withdraw"):
            report = _report(
                confirmed=[_verdict()],
                section_9_citations=[
                    _citation(
                        verification="content_mismatch",
                        relevance_verdict="not_addressed",
                        reask={
                            "action": action,
                            "corrected_claim": "forty-two gigabits",
                            "asked_model": "gemini",
                        },
                    )
                ],
            )
            entry = _only(build_claim_ledger(report))
            assert entry["status"] == "contradicted", action
            assert entry["corrected_claim"] == "forty-two gigabits"

    def test_a_reask_that_stands_changes_nothing(self):
        report = _report(
            confirmed=[_verdict()],
            section_9_citations=[
                _citation(
                    verification="content_mismatch",
                    relevance_verdict="not_addressed",
                    reask={"action": "stand"},
                )
            ],
        )
        entry = _only(build_claim_ledger(report))
        assert entry["status"] == "source_does_not_support"
        assert "corrected_claim" not in entry

    def test_checksum_is_confirmed_with_source_read(self):
        report = _report(
            primary_source_needed=[_verdict()],
            section_9_citations=[_citation(verification="checksum")],
        )
        assert _only(build_claim_ledger(report))["status"] == "confirmed_source_read"

    def test_a_citation_nobody_read_leaves_the_model_verdict(self):
        for tier in ("fetch_failed", "pointer", "unverifiable", "resolution_error"):
            report = _report(
                confirmed=[_verdict()],
                section_9_citations=[_citation(verification=tier)],
            )
            assert _only(build_claim_ledger(report))["status"] == (
                "confirmed_source_not_read"
            ), tier

    def test_nothing_at_all_is_unverified(self):
        report = _report(unverifiable=[_verdict()], primary_source_needed=[_verdict()])
        assert _only(build_claim_ledger(report))["status"] == "unverified"

    def test_the_strongest_of_several_citations_decides(self):
        report = _report(
            confirmed=[_verdict()],
            section_9_citations=[
                _citation(verification="checksum"),
                _citation(verification="content_mismatch", url="https://b.example"),
            ],
        )
        assert _only(build_claim_ledger(report))["status"] == (
            "source_does_not_support"
        )


class TestSelfAttestation:
    def test_a_supporting_page_on_the_authors_site_is_flagged(self):
        report = _report(
            confirmed=[_verdict()],
            section_9_citations=[
                _citation(
                    final_url="https://www.mikehammett.net/about/",
                    url="https://mikehammett.net/about/",
                )
            ],
        )
        entry = _only(build_claim_ledger(report, author_hosts=("mikehammett.net",)))
        assert entry["status"] == "confirmed_source_read"
        assert entry["source_is_authors"] is True
        assert "author's own" in entry["why"]

    def test_a_third_party_page_is_not(self):
        report = _report(confirmed=[_verdict()], section_9_citations=[_citation()])
        entry = _only(build_claim_ledger(report, author_hosts=("mikehammett.net",)))
        assert entry["source_is_authors"] is False

    def test_author_hosts_come_from_the_wordpress_site_url(self):
        pub = {"wordpress": {"site_url": "https://www.mikehammett.net"}}
        assert author_hosts(pub) == ("mikehammett.net",)
        assert author_hosts({}) == ()
        assert author_hosts({"wordpress": {"site_url": ""}}) == ()


class TestJoining:
    def test_the_same_claim_quoted_two_ways_is_one_entry(self):
        long = "Fourteen pieces are sitting in the pipeline right now, next to the ten"
        short = (
            "Fourteen pieces are sitting in the pipeline right now, next to the ten."
        )
        report = _report(
            confirmed=[_verdict(long, model="gemini")],
            unverifiable=[_verdict(short, model="perplexity")],
            section_9_citations=[_citation(long, verification="content_mismatch")],
        )
        entry = _only(build_claim_ledger(report))
        assert entry["status"] == "source_does_not_support"
        assert sorted(entry["evidence"]["buckets"]) == ["confirmed", "unverifiable"]

    def test_short_claims_are_not_merged_into_each_other(self):
        report = _report(
            confirmed=[
                _verdict("I have a day job."),
                _verdict("I have a side job."),
            ]
        )
        assert len(build_claim_ledger(report)["claims"]) == 2

    def test_section_1_is_a_reference_only(self):
        passage = f"More is coming. {CLAIM} And that is the point."
        report = _report(
            confirmed=[_verdict()],
            section_1_consensus=[
                {"passage": passage, "models": ["claude:red_team"], "flags": []}
            ],
        )
        entry = _only(build_claim_ledger(report))
        assert entry["status"] == "confirmed_source_not_read"
        assert entry["evidence"]["section_1_passages"] == [passage]

    def test_a_claim_absent_from_section_1_links_nothing(self):
        report = _report(
            confirmed=[_verdict()],
            section_1_consensus=[{"passage": "Something else entirely.", "flags": []}],
        )
        assert _only(build_claim_ledger(report))["evidence"]["section_1_passages"] == []

    def test_text_where_a_record_was_expected_is_skipped(self):
        report = _report(
            confirmed=["not a record", _verdict()],
            out_of_scope=[None],
            section_9_citations=["also text"],
        )
        assert _only(build_claim_ledger(report))["status"] == (
            "confirmed_source_not_read"
        )

    def test_a_missing_or_malformed_section_2_gives_an_empty_ledger(self):
        for fact_check in (None, {}, "salvaged text"):
            ledger = build_claim_ledger({"section_2_fact_check": fact_check})
            assert ledger["claims"] == []
            assert sum(ledger["counts"].values()) == 0


class TestWithoutSection9:
    def test_an_offline_replay_says_citations_were_not_resolved(self):
        report = _report(confirmed=[_verdict()])
        ledger = build_claim_ledger(report)
        assert ledger["citations_resolved"] is False
        assert all(
            e["status"] in ("confirmed_source_not_read", "unverified")
            or e["status"].startswith("excluded")
            for e in ledger["claims"]
        )

    def test_a_resolved_run_says_so(self):
        report = _report(confirmed=[_verdict()], section_9_citations=[_citation()])
        assert build_claim_ledger(report)["citations_resolved"] is True


class TestAnnotation:
    def test_every_section_2_entry_gets_the_net_status(self):
        report = _report(
            confirmed=[_verdict(model="gemini")],
            primary_source_needed=[_verdict(model="perplexity")],
            out_of_scope=[
                {"claim": CLAIM, "excluded": False, "excluded_by": ORIGIN_MODEL},
                {
                    "claim": "I have a family.",
                    "excluded": True,
                    "excluded_by": ORIGIN_HANDOFF,
                },
            ],
            section_9_citations=[
                _citation(
                    verification="content_mismatch", relevance_verdict="not_addressed"
                )
            ],
        )
        ledger = build_claim_ledger(report)
        annotate_fact_check(report["section_2_fact_check"], ledger)
        fc = report["section_2_fact_check"]
        assert fc["confirmed"][0]["net_status"] == "source_does_not_support"
        assert fc["primary_source_needed"][0]["net_status"] == "source_does_not_support"
        assert fc["out_of_scope"][0]["net_status"] == "source_does_not_support"
        assert fc["out_of_scope"][1]["net_status"] == "excluded_by_author"

    def test_annotation_survives_text_entries(self):
        fact_check = {"confirmed": ["text", _verdict()]}
        ledger = build_claim_ledger({"section_2_fact_check": fact_check})
        annotate_fact_check(fact_check, ledger)
        assert fact_check["confirmed"][1]["net_status"] == "confirmed_source_not_read"

    def test_section_2_renders_the_label_not_the_key(self):
        report = _report(
            confirmed=[_verdict()],
            section_9_citations=[
                _citation(
                    verification="content_mismatch", relevance_verdict="not_addressed"
                )
            ],
        )
        annotate_fact_check(report["section_2_fact_check"], build_claim_ledger(report))
        text = "\n".join(
            report_markdown._render_section_2(report["section_2_fact_check"], report)
        )
        assert f"Net status: {label_for('source_does_not_support')}" in text
        assert "source_does_not_support" not in text

    def test_out_of_scope_entries_render_it_too(self):
        report = _report(
            out_of_scope=[
                {"claim": CLAIM, "excluded": True, "excluded_by": ORIGIN_HANDOFF}
            ]
        )
        annotate_fact_check(report["section_2_fact_check"], build_claim_ledger(report))
        text = "\n".join(
            report_markdown._render_section_2(report["section_2_fact_check"], report)
        )
        assert f"Net status: {label_for('excluded_by_author')}" in text

    def test_an_old_report_without_a_ledger_renders_as_before(self):
        report = _report(confirmed=[_verdict()])
        text = "\n".join(
            report_markdown._render_section_2(report["section_2_fact_check"], report)
        )
        assert "Net status" not in text


class TestConsoleLine:
    def test_the_summary_line_names_every_nonzero_status(self):
        report = _report(
            confirmed=[_verdict("A claim about one thing that is confirmed.")],
            unverifiable=[_verdict("A second claim nobody could verify at all.")],
        )
        ledger = build_claim_ledger(report)
        line = claim_status.summary_line(ledger)
        assert line.startswith("Claim ledger: 2 claim(s)")
        assert label_for("confirmed_source_not_read").lower() in line.lower()
        assert label_for("unverified").lower() in line.lower()
        assert label_for("contradicted").lower() not in line.lower()
        assert "Section 9 not resolved" in line

    def test_an_empty_ledger_has_a_line_too(self):
        assert claim_status.summary_line(build_claim_ledger({})) == (
            "Claim ledger: 0 claim(s)"
        )
