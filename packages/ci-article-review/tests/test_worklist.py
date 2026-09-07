"""Tests for the worklist — the actionable half of what a run could not settle.

Every fixture in here is shaped like the 2026-09-05 Honda navigation run that
motivated the module, because the shapes are the whole difficulty. In that run:

* 8 claims carried a URL that 403'd, and 6 of them were the *same* URL. An
  item-per-claim worklist reports 8 errands where there are 2.
* Every ``url`` on a grounded citation was a ``vertexaisearch.cloud.google.com``
  redirect. The address a person can actually open — ``bianchihonda.com/...`` —
  survives only inside the ``note``, because that is where ``requests`` puts it.
* Three ``primary_source_needed`` claims named the same Furuno document and
  hedged differently after it, so grouping on the raw string makes one document
  into three errands.
* One citation had 158 characters of ``example.com`` boilerplate in its
  ``content_summary`` and no article text at all, which is why the reason a page
  could not be read is read off ``content_kind`` rather than guessed at from
  whether the summary is empty.

Each of those is a test below, and each was a bug first.
"""

import pytest

from ci_article_review import history, report_markdown, worklist
from ci_article_review.adapters.citation.disposition import (
    DISPOSITIONS,
    disposition,
)
from ci_article_review.worklist import (
    HUMAN,
    TOOLING,
    build_worklist,
    render_worklist,
)

REDIRECT = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFP8tk"
    "CcKwDb8zl7oh2IyegJgawiRZ-w67UXwmQagNg6Wvxhr-Qtz-scgGxgR_db6OeOJ33vp_UoLIw"
)
BIANCHI = (
    "https://www.bianchihonda.com/change-clock-on-accord-odyssey-or-pilot-with-"
    "navigation-repair/"
)
TEARDOWN = (
    "https://electronics360.globalspec.com/article/3652/"
    "honda-39100-sza-a710-m2-head-end-unit-teardown"
)


def _refused(claim, url=REDIRECT, real=BIANCHI, status=403, archived=False):
    """A citation whose fetch was refused, exactly as the resolver records one."""
    return {
        "claim": claim,
        "url": url,
        "resolved": False,
        "note": (
            f"Known source URL could not be fetched: {status} Client Error: "
            f"Forbidden for url: {real}"
        ),
        "wayback": {"url": url, "archived": archived},
        "fact_check_bucket": "confirmed",
    }


def _unreadable(claim, url=REDIRECT, kind="html", summary="", **extra):
    """A citation fetched successfully whose content could not be read."""
    citation = {
        "claim": claim,
        "url": url,
        "content_summary": summary,
        "resolved": True,
        "verification": "unverifiable",
        "content_kind": kind,
        "fact_check_bucket": "confirmed",
    }
    citation.update(extra)
    return citation


def _no_source(claim, bucket="confirmed"):
    return {
        "claim": claim,
        "resolved": False,
        "note": "No configured source adapter could resolve this claim",
        "fact_check_bucket": bucket,
    }


def _report(citations=(), fact_check=None):
    return {
        "section_1_consensus": [],
        "section_2_fact_check": dict(fact_check or {}),
        "section_9_citations": list(citations),
    }


def _rendered(report, **kwargs):
    return "\n".join(render_worklist(build_worklist(report, **kwargs)))


class TestActionsAreCollapsedByTarget:
    """Six claims behind one refused URL are one errand, not six.

    This is the difference between a worklist an author works through and a
    backlog they abandon, and it is measurable: the motivating run's 8 refused
    citations are 2 pages to open.
    """

    def _six_behind_one_url(self):
        return _report(
            citations=[_refused(f"claim {i}") for i in range(6)]
            + [_refused("teardown claim", real=TEARDOWN)]
        )

    def test_one_item_per_url_not_per_claim(self):
        built = build_worklist(self._six_behind_one_url())
        assert len(built["items"]) == 2, (
            "Seven refused claims behind two URLs must collapse to two actions; "
            f"got {len(built['items'])}."
        )

    def test_the_collapsed_item_says_how_many_claims_it_clears(self):
        built = build_worklist(self._six_behind_one_url())
        biggest = max(built["items"], key=lambda i: len(i["claims"]))
        assert len(biggest["claims"]) == 6
        assert "clears 6 claim(s)" in _rendered(self._six_behind_one_url())

    def test_the_header_counts_actions_and_claims_separately(self):
        md = _rendered(self._six_behind_one_url())
        assert "**2 action(s) below clear 7 claim(s)" in md, (
            "The header is the honesty budget: it must say how few actions "
            "cover how many claims, not restate the claim count twice."
        )


class TestTheOpenableUrlIsRecovered:
    """The author gets the publisher's address, not an expiring redirect."""

    def test_the_real_url_comes_out_of_the_failure_message(self):
        built = build_worklist(_report([_refused("c")]))
        assert built["items"][0]["target"] == BIANCHI

    def test_the_redirect_is_not_what_gets_handed_over(self):
        md = _rendered(_report([_refused("c")]))
        assert BIANCHI in md
        assert "grounding-api-redirect" not in md.split("### What a better")[0], (
            "An expiring redirect is not an address a person can act on; the "
            "recovered publisher URL must replace it in the item itself."
        )

    def test_the_swap_is_disclosed_rather_than_silent(self):
        md = _rendered(_report([_refused("c")]))
        assert "recovered from the failure message" in md

    def test_a_citation_with_no_recoverable_url_keeps_its_own(self):
        """A timeout has no "for url:" in it, so the stored URL is all there is."""
        citation = {
            "claim": "c",
            "url": "https://example.org/report.pdf",
            "resolved": False,
            "note": "Known source URL could not be fetched: timed out",
        }
        built = build_worklist(_report([citation]))
        assert built["items"][0]["target"] == "https://example.org/report.pdf"
        assert "recovered from the failure message" not in _rendered(
            _report([citation])
        )

    def test_expiring_redirects_are_counted_for_the_roadmap(self):
        built = build_worklist(_report([_refused("c"), _unreadable("d")]))
        assert built["grounding_redirects"] == 2
        assert "expires roughly 30 days" in _rendered(
            _report([_refused("c"), _unreadable("d")])
        )


class TestWhyThePipelineCouldNotIsReadOffTheRun:
    """The reason a page could not be read comes from what the resolver recorded.

    Regression: the first version inferred it from whether ``content_summary``
    was empty. The ``example.com`` citation in the 2026-09-05 run has 158
    characters of the domain's own boilerplate in that field and no article text
    whatever, so the summary-emptiness rule reported "the relevance check never
    returned a verdict" for a page whose text had never been extracted.
    """

    def test_boilerplate_in_the_summary_is_not_extracted_text(self):
        citation = _unreadable(
            "From January 18, 2007 to September 3, 2026 is 7,168 days.",
            url="https://example.com/",
            summary=(
                "[unverified text quoted from the source page] This domain is "
                "for use in documentation examples without needing permission."
            ),
        )
        md = _rendered(_report([citation]))
        assert "no article text could be extracted" in md
        assert "relevance check on it failed" not in md

    def test_a_failed_relevance_check_says_the_document_was_fine(self):
        citation = _unreadable(
            "c", summary="real text", relevance_check="could not quote supporting text"
        )
        md = _rendered(_report([citation]))
        assert "only the relevance check on it failed" in md
        assert "never the problem" in md

    def test_a_scanned_pdf_is_named_as_a_pdf(self):
        md = _rendered(_report([_unreadable("c", kind="pdf")]))
        assert "It is a PDF and no text came out of it" in md

    @pytest.mark.parametrize(
        "status,expected",
        [
            (403, "refused an automated fetch (HTTP 403)"),
            (404, "The page is gone (HTTP 404)"),
            (500, "The fetch failed with HTTP 500"),
        ],
    )
    def test_the_http_status_decides_the_wording(self, status, expected):
        md = _rendered(_report([_refused("c", status=status)]))
        assert expected in md

    @pytest.mark.parametrize(
        "wayback,expected",
        [
            ({"archived": False}, "has no snapshot of it"),
            ({"archived": None}, "did not complete this run"),
            (None, "never asked about this one"),
            (
                {"archived": True, "snapshot_url": "https://web.archive.org/x"},
                "There is an archived copy: https://web.archive.org/x",
            ),
        ],
    )
    def test_the_three_archive_states_are_kept_apart(self, wayback, expected):
        """ "No snapshot", "we never found out" and "we never asked" differ.

        They need different follow-up from the author, and collapsing them
        would assert something this run did not establish — the same
        distinction ``report_markdown._render_archive_pair`` draws.
        """
        citation = _refused("c")
        if wayback is None:
            citation.pop("wayback")
        else:
            citation["wayback"] = wayback
        assert expected in _rendered(_report([citation]))


class TestHumanWorkAndToolGapsAreSeparated:
    """One is the author's forever; the other is a roadmap."""

    def test_a_403_is_a_tool_gap(self):
        built = build_worklist(_report([_refused("c")]))
        assert built["items"][0]["blocked_by"] == TOOLING

    def test_a_404_is_the_authors_problem(self):
        built = build_worklist(_report([_refused("c", status=404)]))
        assert built["items"][0]["blocked_by"] == HUMAN

    def test_a_recognised_bot_wall_or_paywall_is_the_authors_problem(self):
        """The resolver only sets ``access_wall`` when it positively recognised
        an interstitial. That is a deliberate block, and if it is a subscription
        no fetcher ever gets past it — so it is not filed as a tool gap."""
        built = build_worklist(_report([_unreadable("c", kind="access_wall")]))
        assert built["items"][0]["blocked_by"] == HUMAN
        assert built["tool_gaps"] == []

    def test_a_named_document_with_no_url_is_the_authors_problem(self):
        built = build_worklist(
            _report(
                fact_check={
                    "primary_source_needed": [
                        {
                            "claim": "c",
                            "best_candidate_source": "Furuno SE18-100-034-02",
                        }
                    ]
                }
            )
        )
        assert built["items"][0]["blocked_by"] == HUMAN

    def test_both_kinds_are_badged_in_the_output(self):
        md = _rendered(
            _report(
                [_refused("c")],
                fact_check={
                    "primary_source_needed": [
                        {
                            "claim": "d",
                            "best_candidate_source": "Furuno SE18-100-034-02",
                        }
                    ]
                },
            )
        )
        assert "**[tool gap]**" in md
        assert "**[you]**" in md

    def test_only_tool_gaps_reach_the_roadmap_block(self):
        built = build_worklist(
            _report(
                [_refused("c"), _unreadable("d")],
                fact_check={
                    "primary_source_needed": [
                        {
                            "claim": "e",
                            "best_candidate_source": "Furuno SE18-100-034-02",
                        }
                    ]
                },
            )
        )
        assert all(g["claims"] > 0 for g in built["tool_gaps"]), (
            "A roadmap line with no claims behind it is noise."
        )
        counted = sum(g["claims"] for g in built["tool_gaps"])
        human_claims = sum(
            len(i["claims"]) for i in built["items"] if i["blocked_by"] == HUMAN
        )
        assert human_claims and counted == 2, (
            "The roadmap totals tool gaps only — counting work no tool will "
            "ever do would promise a fix that is not coming."
        )


class TestOneClaimBelongsToOneAction:
    """The same errand must not appear in two groups.

    In the motivating run the Electronics360 teardown arrived twice: once as a
    refused URL carrying the publisher's own address, and once as a
    ``primary_source_needed`` document naming that same teardown with only an
    expiring redirect. Both are one page to open.
    """

    def _both_routes(self):
        return _report(
            [_refused("teardown claim", real=TEARDOWN)],
            fact_check={
                "primary_source_needed": [
                    {
                        "claim": "teardown claim",
                        "best_candidate_source": (
                            "Electronics360 teardown of Honda part 39100-SZA-A710-M2"
                        ),
                        "best_candidate_url": REDIRECT,
                    }
                ]
            },
        )

    def test_the_claim_appears_once(self):
        built = build_worklist(self._both_routes())
        owners = [i for i in built["items"] if "teardown claim" in i["claims"]]
        assert len(owners) == 1

    def test_the_action_with_an_address_wins_over_the_one_with_a_search(self):
        built = build_worklist(self._both_routes())
        assert built["items"][0]["action"] == "find_copy"
        assert built["items"][0]["target"] == TEARDOWN

    def test_an_action_left_with_no_claims_is_dropped_entirely(self):
        built = build_worklist(self._both_routes())
        assert len(built["items"]) == 1, (
            "A document errand whose only claim is already covered by a page "
            "the author is opening anyway is not a second errand."
        )

    def test_a_claim_named_twice_by_two_models_is_still_one(self):
        built = build_worklist(
            _report([_refused("same claim"), _refused("same claim")])
        )
        assert built["items"][0]["claims"] == ["same claim"]
        assert built["claims_total"] == 1


class TestNamedDocumentsCollapseAcrossTheModelsHedges:
    """One document hedged three ways is one document."""

    FURUNO = (
        "Furuno, GPS/GNSS Receiver GPS Week Number Rollover, document SE18-100-034-02"
    )

    def _three_hedges(self):
        return _report(
            fact_check={
                "primary_source_needed": [
                    {
                        "claim": "a",
                        "best_candidate_source": f"{self.FURUNO} or equivalent official Furuno documentation.",
                    },
                    {
                        "claim": "b",
                        "best_candidate_source": f"{self.FURUNO} or equivalent official Furuno documentation containing the full table.",
                    },
                    {
                        "claim": "c",
                        "best_candidate_source": f"{self.FURUNO} or equivalent official Furuno documentation containing this specific table.",
                    },
                ]
            }
        )

    def test_three_hedges_are_one_errand(self):
        built = build_worklist(self._three_hedges())
        assert len(built["items"]) == 1
        assert len(built["items"][0]["claims"]) == 3

    def test_the_fullest_description_survives_as_the_display_name(self):
        """The hedge is cut for grouping only. It often carries the only pointer
        the model gave to *where* the document lives, which is the most specific
        part of the next step."""
        built = build_worklist(
            _report(
                fact_check={
                    "primary_source_needed": [
                        {
                            "claim": "a",
                            "best_candidate_source": "Official Honda/Acura service information system.",
                        },
                        {
                            "claim": "b",
                            "best_candidate_source": (
                                "Official Honda/Acura service information system "
                                "(e.g., techinfo.honda.com or acurazine.com for "
                                "archived bulletins)."
                            ),
                        },
                    ]
                }
            )
        )
        assert len(built["items"]) == 1
        assert "techinfo.honda.com" in built["items"][0]["target"]

    def test_a_bare_or_is_not_treated_as_a_hedge(self):
        """ "Official Honda service manual or bulletin for 2011+ Odyssey ..." is
        one description. Cutting at the first "or" throws away the half that
        says which vehicle, and merges two unrelated documents."""
        built = build_worklist(
            _report(
                fact_check={
                    "primary_source_needed": [
                        {
                            "claim": "a",
                            "best_candidate_source": "Official Honda service manual or bulletin for 2011+ Odyssey diagnostic menu access.",
                        },
                        {
                            "claim": "b",
                            "best_candidate_source": "Official Honda service manual or bulletin for 2006 Pilot antenna routing.",
                        },
                    ]
                }
            )
        )
        assert len(built["items"]) == 2

    def test_the_display_name_does_not_end_in_a_doubled_period(self):
        md = _rendered(self._three_hedges())
        assert ".." not in md

    def test_a_real_url_is_offered_as_a_click_rather_than_a_search(self):
        built = build_worklist(
            _report(
                fact_check={
                    "primary_source_needed": [
                        {
                            "claim": "a",
                            "best_candidate_source": "Honda ServiceNews A21120A",
                            "best_candidate_url": "https://static.nhtsa.gov/odi/tsbs/2022/MC-1.pdf",
                        }
                    ]
                }
            )
        )
        assert (
            "https://static.nhtsa.gov/odi/tsbs/2022/MC-1.pdf"
            in built["items"][0]["next_step"]
        )
        assert "starts as a search" not in built["items"][0]["next_step"]


class TestClaimsOnlyTheAuthorCanSettle:
    """Observation, arithmetic and projection, kept apart from research."""

    def test_an_unverifiable_claim_nobody_searched_for_is_the_authors(self):
        built = build_worklist(
            _report(
                fact_check={
                    "unverifiable": [
                        {
                            "claim": "On September 3, 2026 it reported January 18, 2007.",
                            "sources_checked": [],
                            "reason": (
                                "This is a direct observation by the author from "
                                "a specific vehicle."
                            ),
                        }
                    ]
                }
            )
        )
        assert built["items"][0]["action"] == "stand_behind"
        assert built["items"][0]["blocked_by"] == HUMAN

    def test_the_passes_own_reason_is_quoted_not_paraphrased(self):
        md = _rendered(
            _report(
                fact_check={
                    "unverifiable": [
                        {
                            "claim": "c",
                            "sources_checked": [],
                            "reason": "This is a direct observation by the author.",
                        }
                    ]
                }
            )
        )
        assert '"This is a direct observation by the author."' in md

    def test_a_claim_that_was_actually_searched_for_is_not_the_authors(self):
        """ "Searched everywhere and found nothing" is a research problem. Filing
        it as "only you can settle this" tells the author to stop looking."""
        built = build_worklist(
            _report(
                fact_check={
                    "unverifiable": [
                        {
                            "claim": "c",
                            "sources_checked": ["https://example.org/"],
                            "reason": "Nothing found.",
                        }
                    ]
                }
            )
        )
        assert built["items"] == []

    def test_confirmed_with_nothing_behind_it_is_surfaced_as_work(self):
        """Section 9 files these under "No source identified", which reads as a
        coverage shortfall. The fact-check pass called them confirmed and no
        document was ever read — that is something to act on."""
        built = build_worklist(
            _report(
                [
                    _no_source(
                        "From January 18, 2007 to September 3, 2026 is 7,168 days."
                    ),
                    _no_source("Divide by seven and you get 1,024."),
                ]
            )
        )
        assert len(built["items"]) == 1
        assert len(built["items"][0]["claims"]) == 2
        assert built["items"][0]["blocked_by"] == HUMAN

    def test_the_pipeline_admits_it_has_no_arithmetic_checker(self):
        md = _rendered(_report([_no_source("2 + 2 is 4.")]))
        assert "no arithmetic checker" in md

    def test_an_unverifiable_no_source_claim_is_not_double_filed(self):
        """The same claim reaches the builder as a fact-check entry and as a
        citation. It is one thing for the author to look at."""
        claim = "Apply the same logic and the moment is August 17, 2027."
        built = build_worklist(
            _report(
                [_no_source(claim, bucket="unverifiable")],
                fact_check={
                    "unverifiable": [
                        {
                            "claim": claim,
                            "sources_checked": [],
                            "reason": "A projection.",
                        }
                    ]
                },
            )
        )
        assert built["claims_total"] == 1
        assert sum(claim in i["claims"] for i in built["items"]) == 1

    def test_these_are_grouped_as_one_sitting_not_fifteen_errands(self):
        built = build_worklist(_report([_no_source(f"claim {i}") for i in range(9)]))
        assert len(built["items"]) == 1, (
            "Nine desk checks are one pass through a list. Splitting them into "
            "nine numbered items pushes real errands off the end of the cap."
        )


class TestRanking:
    """Highest risk first, then reach."""

    def test_a_contradicted_claim_outranks_a_bigger_but_unremarkable_item(self):
        report = _report(
            [_refused("suspect", real=TEARDOWN)]
            + [_refused(f"routine {i}") for i in range(5)],
            fact_check={
                "contradicted": [
                    {"claim": "suspect", "contradiction": "says otherwise"}
                ]
            },
        )
        built = build_worklist(report)
        assert built["items"][0]["target"] == TEARDOWN
        assert built["items"][0]["possibly_wrong"] is True
        assert len(built["items"][1]["claims"]) == 5

    def test_a_suspect_item_says_why_it_is_first(self):
        md = _rendered(
            _report(
                [_refused("suspect")],
                fact_check={"contradicted": [{"claim": "suspect"}]},
            )
        )
        assert "possible error in the article, not a gap in its sourcing" in md

    def test_a_source_read_and_found_not_to_back_the_claim_also_counts(self):
        report = _report(
            [
                _refused("suspect"),
                {
                    "claim": "suspect",
                    "url": "https://example.org/read",
                    "verification": "content_mismatch",
                    "relevance_verdict": "contradicts",
                },
            ]
        )
        assert build_worklist(report)["items"][0]["possibly_wrong"] is True

    def test_reach_breaks_the_tie_among_unremarkable_items(self):
        built = build_worklist(
            _report([_refused("a"), _refused("b"), _refused("c", real=TEARDOWN)])
        )
        assert len(built["items"][0]["claims"]) == 2


class TestTheListIsCappedAndSaysSo:
    """A worklist of forty items nobody works through is worse than eight."""

    def _many(self):
        return _report(
            [_refused(f"claim {i}", real=f"https://example.org/{i}") for i in range(20)]
        )

    def test_the_cap_holds(self):
        assert len(build_worklist(self._many(), limit=8)["items"]) == 8

    def test_what_was_cut_is_counted_rather_than_dropped_silently(self):
        built = build_worklist(self._many(), limit=8)
        assert len(built["held_back"]) == 12
        md = _rendered(self._many(), limit=8)
        # Twenty singleton actions: the cut lands inside a tie, so the
        # wording has to admit that rather than call the rest lower-ranked.
        assert "12 further action(s) covering 12 further claim(s)" in md
        assert "which of them got printed is arbitrary" in md

    def test_the_header_does_not_claim_coverage_it_does_not_have(self):
        md = _rendered(self._many(), limit=8)
        assert "**8 action(s) below clear 8 claim(s)" in md

    def test_what_is_cut_is_always_the_tail_of_the_ranking(self):
        built = build_worklist(self._many(), limit=8)
        assert all(not item["possibly_wrong"] for item in built["held_back"]), (
            "A suspect claim must never be the thing that falls off the end."
        )


class TestItReferencesTheReportRatherThanRepeatingIt:
    def test_every_item_names_the_section_it_came_from(self):
        built = build_worklist(
            _report(
                [_refused("a"), _unreadable("b"), _no_source("c")],
                fact_check={
                    "primary_source_needed": [
                        {"claim": "d", "best_candidate_source": "Some document"}
                    ],
                    "unverifiable": [
                        {"claim": "e", "sources_checked": [], "reason": "Mine."}
                    ],
                },
            )
        )
        assert built["items"], "fixture produced no items"
        for item in built["items"]:
            assert item["section"].startswith("SECTION "), item

    def test_long_claims_are_clipped_to_one_line(self):
        long_claim = "When Honda documented the 2022 round, " + "x" * 400
        md = _rendered(_report([_refused(long_claim)]))
        assert long_claim not in md
        assert "…" in md

    def test_a_long_claim_list_is_summarised_not_dumped(self):
        md = _rendered(_report([_refused(f"claim {i}") for i in range(6)]))
        assert "and 3 more" in md
        assert "claim 5" not in md


class TestAnEmptyWorklistIsStillHonest:
    def test_nothing_outstanding_renders_a_sentence_not_a_broken_table(self):
        md = _rendered(_report([]))
        assert "Nothing outstanding" in md

    def test_a_fully_verified_run_produces_no_items(self):
        built = build_worklist(
            _report(
                [
                    {
                        "claim": "c",
                        "url": "https://example.org/",
                        "verification": "checksum",
                        "fact_check_bucket": "confirmed",
                    }
                ]
            )
        )
        assert built["items"] == []

    def test_a_report_missing_its_sections_entirely_does_not_crash(self):
        assert build_worklist({})["items"] == []


class TestDispositionComesFromTheOneSharedVocabulary:
    """Citation buckets are classified in exactly one place.

    This module first shipped with its own six-line copy of the rule, guarded by
    a test that the copy agreed with ``report_markdown``'s. Upstream then
    extracted ``adapters.citation.disposition`` — a leaf holding the vocabulary
    and a pure function — precisely because two callers classifying
    independently had already drifted apart once and reported a citation two
    different ways. A third copy would be that same bug, so the copy is gone.

    What is worth pinning now is not that the copies agree (there is one), but
    that a bucket this module keys behaviour on still exists in the vocabulary.
    Every group heading names the SECTION 9 block its claims came from, so a
    renamed bucket would silently point the author at the wrong block.
    """

    CASES = [
        {"verification": "checksum", "url": "u"},
        {"verification": "content_mismatch", "url": "u"},
        {"verification": "unverifiable", "url": "u"},
        {"verification": "pointer", "url": "u"},
        {"url": "u"},
        {},
        {"verification": None, "url": "u"},
        {"verification": "nonsense", "url": "u"},
        {"verification": "nonsense"},
    ]

    def test_there_is_only_one_classifier(self):
        assert worklist._disposition is disposition
        assert report_markdown.disposition is disposition

    @pytest.mark.parametrize("citation", CASES)
    def test_every_citation_lands_in_a_known_bucket(self, citation):
        assert worklist._disposition(citation) in {k for k, _ in DISPOSITIONS}

    @pytest.mark.parametrize(
        "bucket", ["unverifiable", "fetch_failed", "no_source", "content_mismatch"]
    )
    def test_the_buckets_this_module_branches_on_still_exist(self, bucket):
        assert bucket in {k for k, _ in DISPOSITIONS}, (
            f"worklist keys behaviour on the {bucket!r} disposition; if it has "
            "been renamed, the group that reads it silently goes empty and its "
            "heading points at a SECTION 9 block the claims are no longer in."
        )

    def test_read_dispositions_are_a_subset_of_the_vocabulary(self):
        assert set(worklist._READ_DISPOSITIONS) <= {k for k, _ in DISPOSITIONS}


class TestTheWorklistIsNotAReportSection:
    """It is the author's list, and it stays out of the paste-into-a-model loop.

    ``revise_after_review_prompt.md`` tells the author to paste SECTION 1
    through SECTION N into a chat model and ask it to revise the draft. A list
    of documents nobody has read yet is the last thing to hand a model in that
    context: it is an invitation to invent them. Numbering the worklist as a
    section would sweep it in, and ``test_templates_current`` would then require
    the prompt to ask for it by name.
    """

    def test_the_heading_is_not_numbered_as_a_section(self):
        assert not worklist.HEADING.startswith("## SECTION")

    def test_it_does_not_add_itself_to_the_pasted_section_range(self):
        import re

        from pathlib import Path

        source = Path(report_markdown.__file__).read_text(encoding="utf-8")
        assert not re.search(r'"## SECTION (\d+)[^"]*WORKLIST', source)

    def test_the_console_tells_the_author_not_to_paste_it(self):
        from pathlib import Path

        from ci_article_review import pipeline

        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        assert "do NOT paste" in source


class TestItReachesTheAuthor:
    """Built is not delivered — it has to be in the files the run writes."""

    def _report_with_gaps(self):
        return {
            "generated": "2026-09-05T07:37:23+00:00",
            "run_number": 1,
            "article_title": "Honda Navigation Clock Stuck at 0:00",
            "publication": "mikehammett",
            "section_2_fact_check": {},
            "section_9_citations": [_refused("a claim")],
        }

    def test_the_review_markdown_opens_with_it(self):
        md = report_markdown.render_report_markdown(self._report_with_gaps())
        assert worklist.HEADING in md
        assert md.index(worklist.HEADING) < md.index("## SECTION 1"), (
            "The worklist is the only part of the report the author acts on "
            "directly; everything below it is the evidence it points at."
        )

    def test_it_is_also_written_as_its_own_file(self, tmp_path):
        paths = history.save_run(
            str(tmp_path),
            "Honda Navigation Clock Stuck",
            1,
            self._report_with_gaps(),
            [],
        )
        assert paths["worklist_path"], "save_run did not report a worklist path"
        from pathlib import Path

        written = Path(paths["worklist_path"]).read_text(encoding="utf-8")
        assert written.startswith(worklist.HEADING)
        assert BIANCHI in written

    def test_the_standalone_file_and_the_review_cannot_disagree(self, tmp_path):
        report = self._report_with_gaps()
        paths = history.save_run(
            str(tmp_path), "Honda Navigation Clock Stuck", 1, report, []
        )
        from pathlib import Path

        standalone = Path(paths["worklist_path"]).read_text(encoding="utf-8").rstrip()
        review = Path(paths["markdown_path"]).read_text(encoding="utf-8")
        assert standalone in review, (
            "One builder feeds both files; if the rendered text differs, the "
            "author is working from a worklist the report does not agree with."
        )

    def test_a_failure_to_write_the_worklist_does_not_lose_the_report(
        self, tmp_path, monkeypatch
    ):
        """The worklist is supplementary. Losing it must not lose the run."""

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(history, "build_worklist", boom)
        paths = history.save_run(
            str(tmp_path), "Honda Navigation Clock", 1, self._report_with_gaps(), []
        )
        assert paths["report_path"]
        assert paths["markdown_path"]
        assert paths["worklist_path"] is None


class TestBadInputDegradesRatherThanRaising:
    """The worklist now renders inside the review, so it must not break it.

    Before this module existed, a malformed entry in ``section_9_citations``
    reached only Section 9's renderer. It now also reaches the worklist at the
    top of the same file, so anything that raises here takes down a review that
    used to render.
    """

    def test_a_non_dict_citation_is_skipped_not_fatal(self):
        report = _report(["a bare string", None, _refused("a real claim")])
        built = build_worklist(report)
        assert len(built["items"]) == 1
        assert built["items"][0]["claims"] == ["a real claim"]

    def test_a_non_dict_fact_check_entry_is_skipped(self):
        built = build_worklist(
            _report(
                fact_check={
                    "primary_source_needed": [
                        "oops",
                        {"claim": "c", "best_candidate_source": "A document"},
                    ],
                    "unverifiable": [None],
                }
            )
        )
        assert len(built["items"]) == 1

    def test_a_fact_check_section_of_the_wrong_type_is_survivable(self):
        assert build_worklist({"section_2_fact_check": "not a dict"})["items"] == []

    def test_rendering_a_worklist_of_junk_produces_a_string_not_a_traceback(self):
        report = _report(
            ["oops", None, 42],
            fact_check={"primary_source_needed": ["oops"], "unverifiable": [None]},
        )
        assert isinstance(_rendered(report), str)

    def test_the_worklist_is_not_what_breaks_a_review_built_from_junk(self):
        """Scope note, pinned so it does not get mistaken for a regression here.

        ``_render_section_2`` and ``_render_section_9`` both raise on a non-dict
        entry, and both did so before this module existed — verified against
        ``git show HEAD`` while writing these tests. The worklist renders ahead
        of them in the same file, so it must not become a *second* place that
        raises; that is what the test above pins. Making the whole review
        tolerate junk is a separate change to a separate module.
        """
        junk = ["oops"]
        assert isinstance(_rendered(_report(junk)), str)
        with pytest.raises(AttributeError):
            report_markdown._render_section_9(junk)


class TestTheHeaderDoesNotOverclaimCoverage:
    """The opening sentence is a claim about coverage. It has to be true.

    An earlier version read "N actions clear M of the M claims this run could
    not settle", which asserted that the list covered everything unsettled. It
    never did, and never should: three kinds of unsettled claim are findings to
    read rather than errands to run, and both real 2026-09-05 runs had some.
    Padding them into items would bloat the list; dropping them silently would
    make it look complete. They are counted instead.
    """

    def test_a_pointer_citation_is_counted_but_not_listed(self):
        """A portal that might be about the right topic, with nothing retrieved
        from it, is a research lead — a poor errand and real bulk."""
        report = _report(
            [
                {
                    "claim": "a pointer claim",
                    "url": "https://example.org/portal",
                    "verification": "pointer",
                },
                _refused("a real errand"),
            ]
        )
        built = build_worklist(report)
        assert built["no_action"] == 1
        assert all("a pointer claim" not in i["claims"] for i in built["items"])

    def test_a_claim_searched_for_and_not_confirmed_is_counted_not_listed(self):
        built = build_worklist(
            _report(
                [_refused("a real errand")],
                fact_check={
                    "unverifiable": [
                        {
                            "claim": "searched and not found",
                            "sources_checked": ["https://example.org/"],
                            "reason": "Nothing found.",
                        }
                    ]
                },
            )
        )
        assert built["no_action"] == 1

    def test_a_contradiction_with_no_document_either_way_is_counted(self):
        built = build_worklist(
            _report(
                [_refused("a real errand")],
                fact_check={"contradicted": [{"claim": "disputed, nothing fetched"}]},
            )
        )
        assert built["no_action"] == 1

    def test_the_count_is_rendered_rather_than_kept_internal(self):
        md = _rendered(
            _report(
                [_refused("a real errand")],
                fact_check={"contradicted": [{"claim": "disputed, nothing fetched"}]},
            )
        )
        assert "1 further unsettled claim(s) have no action attached" in md

    def test_nothing_is_said_when_every_unsettled_claim_has_an_action(self):
        md = _rendered(_report([_refused("a real errand")]))
        assert "no action attached" not in md

    def test_a_claim_that_was_read_is_not_counted_as_unsettled(self):
        """Retrieval settled it, whichever way the verdict went. A source read
        and found not to back the claim is a finding, not an outstanding fetch."""
        built = build_worklist(
            _report(
                [
                    {
                        "claim": "read and supported",
                        "url": "https://example.org/",
                        "verification": "checksum",
                    },
                    {
                        "claim": "read and refuted",
                        "url": "https://example.org/",
                        "verification": "content_mismatch",
                    },
                ]
            )
        )
        assert built["no_action"] == 0

    def test_the_capped_tail_is_not_double_counted_as_unactioned(self):
        """Held-back items still have actions attached — they are just not
        printed. Counting them here would report the same claim twice, in two
        different registers."""
        built = build_worklist(
            _report(
                [
                    _refused(f"claim {i}", real=f"https://example.org/{i}")
                    for i in range(20)
                ]
            ),
            limit=3,
        )
        assert built["no_action"] == 0
        assert len(built["held_back"]) == 17


class TestTiesAreBrokenByWhatIsActuallyFindable:
    """Reach runs out as a ranking signal long before the list does.

    Found on the 161-citation data-centre run: 45 ``find_document`` actions that
    each clear exactly one claim. Tied on reach, they fell through to sorting by
    target string — so *which* eight of the forty-five got printed was
    alphabetical accident, under a header promising a ranking by value.
    """

    def _tied_documents(self):
        return _report(
            fact_check={
                "primary_source_needed": [
                    {"claim": "z", "best_candidate_source": "Zebra standard ZS-1"},
                    {
                        "claim": "a",
                        "best_candidate_source": "Aardvark standard AS-1",
                    },
                    {
                        "claim": "m",
                        "best_candidate_source": "Manatee standard MS-1",
                        "best_candidate_url": "https://example.org/manatee.pdf",
                    },
                ]
            }
        )

    def test_a_document_you_can_click_outranks_one_you_must_search_for(self):
        built = build_worklist(self._tied_documents())
        assert built["items"][0]["target_url"] == "https://example.org/manatee.pdf", (
            "All three clear one claim each. The one with a candidate URL is a "
            "minute's work; the others are open-ended searches."
        )

    def test_alphabetical_order_is_not_what_decides(self):
        built = build_worklist(self._tied_documents())
        assert built["items"][0]["target"].startswith("Manatee"), (
            "Sorting by target put 'Aardvark' first for no reason anyone could act on."
        )

    def test_the_cut_still_keeps_the_findable_one_when_the_list_is_capped(self):
        built = build_worklist(self._tied_documents(), limit=1)
        assert built["items"][0]["target_url"]
        assert len(built["held_back"]) == 2


class TestTheCapSaysWhetherItCutOnValueOrOnATie:
    """ "Lower-ranked" is a claim about the omitted items. It has to be true."""

    def test_a_cut_inside_a_tie_is_admitted_as_arbitrary(self):
        report = _report(
            [_refused(f"claim {i}", real=f"https://example.org/{i}") for i in range(10)]
        )
        built = build_worklist(report, limit=4)
        assert built["cut_inside_a_tie"] is True
        md = _rendered(report, limit=4)
        assert "which of them got printed is arbitrary" in md
        assert "not as lesser" in md

    def test_a_cut_on_a_real_difference_says_so(self):
        """Three claims behind one URL, then singletons: the boundary is earned."""
        report = _report(
            [_refused("big a"), _refused("big b"), _refused("big c")]
            + [
                _refused(f"small {i}", real=f"https://example.org/{i}")
                for i in range(4)
            ]
        )
        built = build_worklist(report, limit=1)
        assert built["cut_inside_a_tie"] is False
        md = _rendered(report, limit=1)
        assert "They rank below every action above." in md
        assert "arbitrary" not in md

    def test_nothing_is_claimed_when_nothing_was_cut(self):
        built = build_worklist(_report([_refused("c")]))
        assert built["cut_inside_a_tie"] is False
        assert "not listed" not in _rendered(_report([_refused("c")]))

    def test_a_suspect_claim_still_never_falls_off_the_end(self):
        """The tie-break must not reorder around the one rank that matters."""
        report = _report(
            [_refused("suspect", real=TEARDOWN)]
            + [
                _refused(f"routine {i}", real=f"https://example.org/{i}")
                for i in range(10)
            ],
            fact_check={"contradicted": [{"claim": "suspect"}]},
        )
        built = build_worklist(report, limit=3)
        assert built["items"][0]["possibly_wrong"] is True
        assert all(not i["possibly_wrong"] for i in built["held_back"])


class TestTheAddressPrefersWhereTheFetchActuallyLanded:
    """``final_url`` beats the redirector the citation stores.

    The resolver follows redirects and records where it ended up, but only when
    that differs from what was requested — so the field's presence *is* the
    signal that ``url`` was a redirector. For a page that fetched and could not
    be read, this is the one case where the run knows the publisher's own
    address and the citation still shows a 271-character `vertexaisearch` link
    that expires roughly 30 days after the run. Handing the author the expiring
    one was giving them a dead link with a durable one in the same dict.
    """

    LANDED = "https://www.jalopnik.com/honda-clocks-stuck-20-years-in-the-past"

    def _landed(self, **extra):
        return _unreadable("a claim", url=REDIRECT, final_url=self.LANDED, **extra)

    def test_the_landing_address_is_what_the_author_gets(self):
        built = build_worklist(_report([self._landed()]))
        assert built["items"][0]["target"] == self.LANDED

    def test_the_expiring_redirect_is_not_offered(self):
        md = _rendered(_report([self._landed()]))
        assert REDIRECT not in md.split("### What a better")[0]

    def test_the_mismatch_with_section_9_is_explained(self):
        """SECTION 9 shows the stored URL. An unexplained difference reads as a
        bug, so the item says which one it is showing and why."""
        md = _rendered(_report([self._landed()]))
        assert "where the fetch actually landed" in md
        assert "SECTION 9 will show the other one" in md

    def test_the_expiry_warning_is_dropped_once_it_no_longer_applies(self):
        """Telling the author a durable publisher URL expires in 30 days would
        be false, and would push them to redo work already done."""
        md = _rendered(_report([self._landed()]))
        assert "expire roughly 30 days" not in md

    def test_the_warning_still_fires_when_only_the_redirect_is_known(self):
        md = _rendered(_report([_unreadable("a claim", url=REDIRECT)]))
        assert "expire roughly 30 days" in md

    def test_citations_landing_on_one_page_collapse_to_one_action(self):
        """Two redirectors resolving to the same article are one page to open.
        Grouping on the stored URL would have made them two."""
        built = build_worklist(
            _report(
                [
                    _unreadable("claim a", url=REDIRECT, final_url=self.LANDED),
                    _unreadable("claim b", url=REDIRECT + "XYZ", final_url=self.LANDED),
                ]
            )
        )
        assert len(built["items"]) == 1
        assert len(built["items"][0]["claims"]) == 2

    def test_a_final_url_equal_to_the_stored_one_is_not_treated_as_a_redirect(self):
        plain = "https://example.org/article"
        built = build_worklist(_report([_unreadable("c", url=plain, final_url=plain)]))
        assert built["items"][0]["target"] == plain
        assert "SECTION 9 will show the other one" not in _rendered(
            _report([_unreadable("c", url=plain, final_url=plain)])
        )

    def test_a_refused_fetch_still_reads_its_address_from_the_note(self):
        """A fetch that never landed has no ``final_url``; the note is all there
        is, and that path must not regress."""
        built = build_worklist(_report([_refused("c")]))
        assert built["items"][0]["target"] == BIANCHI
        assert "recovered from the failure message" in _rendered(
            _report([_refused("c")])
        )

    def test_final_url_wins_over_the_note_when_both_are_present(self):
        """Landing beats being refused: one is where the run got to, the other
        is where it was turned away."""
        c = _refused("c")
        c["final_url"] = self.LANDED
        built = build_worklist(_report([c]))
        assert built["items"][0]["target"] == self.LANDED
