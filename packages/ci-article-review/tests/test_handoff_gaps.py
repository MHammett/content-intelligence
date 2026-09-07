"""Tests for handoff_gaps — what a missing handoff field cost, and the fix.

The behaviour under test is not "a warning is emitted". ``handoff_parser`` has
warned about these fields since it was written, and the run degraded silently
anyway, because a log line is not the artifact the author reads. What these
tests hold is that the *report* says which field was missing, names the
specific domains and sections that were degraded by that field in particular,
and proposes something concrete to paste — and that the proposal never becomes
part of the run.

That last one is the load-bearing test in this file. Everything else is
readable text; ``TestProposalsAreNeverApplied`` is a correctness property. A
pipeline that quietly reviewed a draft against a primary claim it inferred for
itself would produce a report that looks better than the handoff deserved,
which is the failure mode this whole feature exists to end, not to automate.
"""

import pytest

from ci_article_review import handoff_gaps
from ci_article_review.handoff_gaps import assess


_ALL_DOMAINS = (
    "fact_check",
    "voice_style",
    "completeness",
    "argument_integrity",
    "red_team",
)

_DRAFT = (
    "# Water Math Does Not Travel\n"
    "\n"
    "Applying arid-geography water figures to a Great Lakes site is not "
    "analysis, it is a category error that has now reached three separate "
    "planning documents.\n"
    "\n"
    "## Cooling\n"
    "\n"
    "Cooling draws 3 million gallons annually [1].\n"
    "\n"
    "## Sources\n"
    "\n"
    "[1] County water report, https://example.org/water\n"
)

_FULL_HANDOFF = {
    "title": "Water Math Does Not Travel",
    "draft": _DRAFT,
    "primary_claim": "Arid-geography water figures do not transfer.",
    "target_audience": "Primary: planning staff.",
    "pre_draft_analysis": "Steelmanned position: the figures are regional.",
    "sources_cited": "- County water report",
    "uncertain_sections": "The cooling paragraph.",
    "known_gaps": "No utility comment.",
    "additional_context": "Follows the March piece.",
    "history_key": "water-math",
    "drafted_with": "claude",
}


def _without(*fields):
    return {k: v for k, v in _FULL_HANDOFF.items() if k not in fields}


def _fields(gaps):
    return [g["field"] for g in gaps]


def _by_field(gaps, field):
    (gap,) = [g for g in gaps if g["field"] == field]
    return gap


class TestNothingIsReportedWhenNothingIsMissing:
    def test_a_complete_handoff_produces_no_gaps(self):
        assert assess(_FULL_HANDOFF, draft=_DRAFT, domains_ran=_ALL_DOMAINS) == []

    def test_a_field_present_but_only_whitespace_counts_as_missing(self):
        handoff = {**_FULL_HANDOFF, "primary_claim": "   \n  "}
        assert "primary_claim" in _fields(
            assess(handoff, draft=_DRAFT, domains_ran=_ALL_DOMAINS)
        )


class TestImpactNamesTheSpecificCost:
    """A generic "metadata incomplete" is what this replaces, not what it emits."""

    def test_primary_claim_names_its_domains_and_sections(self):
        gap = _by_field(
            assess(_without("primary_claim"), draft=_DRAFT, domains_ran=_ALL_DOMAINS),
            "primary_claim",
        )
        assert gap["domains"] == ["argument_integrity", "completeness", "red_team"]
        assert gap["sections"] == [
            "SECTION 4: Argument Integrity",
            "SECTION 5: Completeness and Framing",
            "SECTION 6: Red Team Findings",
        ]
        # fact_check and voice_style never read primary_claim, so naming their
        # sections here would be a false attribution.
        assert "SECTION 2" not in " ".join(gap["sections"])
        assert "SECTION 3" not in " ".join(gap["sections"])

    def test_a_narrowed_run_does_not_blame_a_pass_that_never_ran(self):
        """--only-domain completeness must not claim red_team was degraded."""
        gap = _by_field(
            assess(
                _without("primary_claim"), draft=_DRAFT, domains_ran=["completeness"]
            ),
            "primary_claim",
        )
        assert gap["domains"] == ["completeness"]
        assert gap["sections"] == ["SECTION 5: Completeness and Framing"]
        assert "only completeness ran" in gap["impact"]

    def test_sources_cited_counts_the_draft_own_citation_markers(self):
        """A measured number beats "less context" — the draft has exactly one."""
        gap = _by_field(
            assess(_without("sources_cited"), draft=_DRAFT, domains_ran=_ALL_DOMAINS),
            "sources_cited",
        )
        assert "1 marker " in gap["impact"]
        assert "marker(s)" not in gap["impact"]

    def test_claim_delta_is_only_mentioned_when_there_is_a_prior_run(self):
        first = _by_field(
            assess(_without("primary_claim"), draft=_DRAFT, domains_ran=_ALL_DOMAINS),
            "primary_claim",
        )
        later = _by_field(
            assess(
                _without("primary_claim"),
                draft=_DRAFT,
                domains_ran=_ALL_DOMAINS,
                has_prior_run=True,
            ),
            "primary_claim",
        )
        assert "claim_changed" not in first["impact"]
        assert "claim_changed" in later["impact"]

    def test_drafted_with_is_not_a_gap_when_user_config_declares_it(self):
        """The config already closed it; reporting it would be a false gap."""
        assert "drafted_with" in _fields(
            assess(_without("drafted_with"), draft=_DRAFT, domains_ran=_ALL_DOMAINS)
        )
        assert "drafted_with" not in _fields(
            assess(
                _without("drafted_with"),
                draft=_DRAFT,
                domains_ran=_ALL_DOMAINS,
                pipeline_cfg={"drafting_model": "claude"},
            )
        )

    def test_drafted_with_is_not_a_gap_when_voice_style_did_not_run(self):
        assert "drafted_with" not in _fields(
            assess(_without("drafted_with"), draft=_DRAFT, domains_ran=["fact_check"])
        )


class TestUnfilledTemplatePlaceholders:
    """A left-in placeholder is worse than a blank: the brackets get sent.

    ``_extract_field`` returns "[One or two sentences: ...]" as happily as it
    returns a real claim, so the run looks fully specified while every domain
    reasons about an instruction addressed to the author.
    """

    def test_a_bracketed_placeholder_is_reported_as_missing(self):
        handoff = {
            **_FULL_HANDOFF,
            "primary_claim": "[One or two sentences: the single thing this argues.]",
        }
        gap = _by_field(
            assess(handoff, draft=_DRAFT, domains_ran=_ALL_DOMAINS), "primary_claim"
        )
        assert gap["placeholder"] is True
        assert "placeholder" in gap["impact"]

    def test_a_real_claim_containing_brackets_is_not_a_placeholder(self):
        handoff = {
            **_FULL_HANDOFF,
            "primary_claim": "The [sic] figures do not transfer between regions.",
        }
        assert "primary_claim" not in _fields(
            assess(handoff, draft=_DRAFT, domains_ran=_ALL_DOMAINS)
        )


class TestProposalsAreConcreteAndSourced:
    def test_target_audience_is_proposed_from_the_publication_config(self):
        gap = _by_field(
            assess(
                _without("target_audience"),
                pub_config={
                    "audience": {
                        "primary": "Planning staff who cite this in a docket.",
                        "secondary": "Utility engineers.",
                    }
                },
                draft=_DRAFT,
                domains_ran=_ALL_DOMAINS,
            ),
            "target_audience",
        )
        assert gap["suggestion"] == (
            "TARGET AUDIENCE\n"
            "Primary: Planning staff who cite this in a docket.\n"
            "Secondary: Utility engineers."
        )
        assert "publication config" in gap["suggestion_basis"]

    def test_no_audience_anywhere_says_so_rather_than_proposing_nothing(self):
        gap = _by_field(
            assess(
                _without("target_audience"),
                pub_config={},
                draft=_DRAFT,
                domains_ran=_ALL_DOMAINS,
            ),
            "target_audience",
        )
        assert gap["suggestion"] is None
        assert gap["guidance"]
        assert "no audience description reached any prompt" in gap["impact"]

    def test_primary_claim_candidate_comes_from_the_draft_opening(self):
        gap = _by_field(
            assess(_without("primary_claim"), draft=_DRAFT, domains_ran=_ALL_DOMAINS),
            "primary_claim",
        )
        assert gap["suggestion"].startswith("PRIMARY CLAIM\n")
        assert "arid-geography water figures" in gap["suggestion"]
        # The candidate must not be presented as an answer.
        assert "not the claim" in gap["suggestion_basis"]

    def test_sources_are_proposed_from_the_draft_citation_block(self):
        gap = _by_field(
            assess(_without("sources_cited"), draft=_DRAFT, domains_ran=_ALL_DOMAINS),
            "sources_cited",
        )
        assert "https://example.org/water" in gap["suggestion"]

    def test_history_key_is_proposed_as_the_slug_the_run_actually_used(self):
        from ci_article_review.history import _slug

        gap = _by_field(
            assess(_without("history_key"), draft=_DRAFT, domains_ran=_ALL_DOMAINS),
            "history_key",
        )
        slug = _slug(_FULL_HANDOFF["title"])
        assert gap["suggestion"] == f"History key: {slug}"
        assert slug in gap["impact"]

    def test_a_title_too_short_to_slug_is_not_proposed_back_as_the_key(self):
        """history._slug collapses these to "untitled" — a shared directory."""
        gap = _by_field(
            assess(
                {"title": "Ab", "draft": _DRAFT},
                draft=_DRAFT,
                domains_ran=_ALL_DOMAINS,
            ),
            "history_key",
        )
        assert gap["suggestion"] is None
        assert "untitled" in gap["guidance"]

    def test_every_gap_offers_either_a_proposal_or_guidance(self):
        """No entry may say what broke without saying what to do about it."""
        for gap in assess({}, draft=_DRAFT, domains_ran=_ALL_DOMAINS):
            assert gap["suggestion"] or gap["guidance"], gap["field"]


class TestProposalsAreNeverApplied:
    """The proposal is text for the author, never an input to the run."""

    def test_assess_does_not_mutate_the_handoff(self):
        handoff = {"title": "Water Math Does Not Travel", "draft": _DRAFT}
        before = dict(handoff)
        gaps = assess(handoff, draft=_DRAFT, domains_ran=_ALL_DOMAINS)
        assert handoff == before
        # And a proposal really was produced — otherwise this proves nothing.
        assert _by_field(gaps, "primary_claim")["suggestion"]

    def test_a_proposed_value_never_lands_under_its_own_field_name(self):
        handoff = {"title": "T", "draft": _DRAFT}
        assess(handoff, draft=_DRAFT, domains_ran=_ALL_DOMAINS)
        for field in ("primary_claim", "target_audience", "sources_cited"):
            assert not handoff.get(field)


class TestOrdering:
    def test_gaps_are_ordered_worst_first(self):
        gaps = assess({}, draft=_DRAFT, domains_ran=_ALL_DOMAINS)
        ranks = [handoff_gaps._SEVERITY_ORDER[g["severity"]] for g in gaps]
        assert ranks == sorted(ranks)
        assert gaps[0]["severity"] == "critical"


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "primary_claim",
        "target_audience",
        "pre_draft_analysis",
        "sources_cited",
        "uncertain_sections",
        "known_gaps",
        "additional_context",
        "history_key",
        "drafted_with",
    ],
)
def test_every_field_states_a_consequence_not_just_an_absence(field):
    """Each impact must survive deleting the field name from it.

    The regression this guards is a return to "PRIMARY CLAIM is missing" — a
    restatement of the label with no cost attached. Requiring a substantial
    remainder is a crude proxy, but it is the one that fails when someone
    shortens an entry back into its warning.
    """
    gap = _by_field(assess({}, draft=_DRAFT, domains_ran=_ALL_DOMAINS), field)
    remainder = gap["impact"].replace(gap["label"], "")
    assert len(remainder.split()) > 25


class TestTheClaimCandidateSkipsWhatIsNotTheArgument:
    """Regression from a real replay, not a hypothetical.

    A 135K-char draft opened with an italicised author disclosure, and the
    first-long-prose-block rule proposed that as the article's primary claim:
    "Mike Hammett works at a local ISP in Northern Illinois...". A proposal
    that wrong is worse than none — it teaches the author to stop reading the
    block.
    """

    DRAFT = (
        "# Data Centers Don't Have an Environmental Record\n"
        "\n"
        "*Mike Hammett works at a local ISP in Northern Illinois and has "
        "worked in internet infrastructure there for two decades. He has no "
        "financial position in any of them beyond broad-market index funds.\n"
        "\n"
        "---\n"
        "\n"
        "## TL;DR\n"
        "\n"
        "- Data center environmental impact varies by grid and geography.\n"
        "\n"
        "## The Core Problem\n"
        "\n"
        "The environmental claims addressed here start with real cases "
        "somewhere else. They arrive in Northern Illinois without the grid, "
        "cooling design, hydrology and regulatory context that made them true.\n"
    )

    def _candidate(self, draft):
        gap = _by_field(
            assess({"title": "T"}, draft=draft, domains_ran=_ALL_DOMAINS),
            "primary_claim",
        )
        return gap["suggestion"]

    def test_an_italicised_author_disclosure_is_not_proposed_as_the_claim(self):
        candidate = self._candidate(self.DRAFT)
        assert "Mike Hammett works at a local ISP" not in candidate

    def test_the_first_real_prose_paragraph_is_proposed_instead(self):
        candidate = self._candidate(self.DRAFT)
        assert "The environmental claims addressed here start with real cases" in (
            candidate
        )

    def test_a_bold_standfirst_is_skipped_on_the_same_rule(self):
        draft = (
            "# T\n\n**An editor's note that runs long enough to clear the "
            "fifteen-word floor set on candidate paragraphs.**\n\n"
            "The actual argument of the piece begins in this paragraph and "
            "runs for more than fifteen words.\n"
        )
        assert "editor's note" not in self._candidate(draft)

    def test_emphasis_inside_a_paragraph_does_not_skip_it(self):
        """Only a block that *opens* emphasised is a disclosure."""
        draft = (
            "# T\n\nThe claim is that *arid-geography* water figures do not "
            "transfer between regions, and three planning documents now say "
            "otherwise.\n"
        )
        assert "water figures do not" in self._candidate(draft)


class TestUnemphasisedBoilerplateIsAlsoSkipped:
    """The second half of the same regression, from the ``--url`` path.

    ``extract_article`` strips the HTML emphasis that marks a disclosure as an
    aside, so a published page reaches the assessor with its AI-use notice as
    plain text — invisible to the italics rule, and proposed as the claim.
    Measured on a real published article before this rule existed.
    """

    def _candidate(self, draft):
        return _by_field(
            assess({"title": "T"}, draft=draft, domains_ran=_ALL_DOMAINS),
            "primary_claim",
        )["suggestion"]

    def test_a_plain_text_ai_disclosure_is_skipped(self):
        draft = (
            "AI tools were used in the research, drafting, and editing of this "
            "article. All factual claims are sourced to primary documents and "
            "verified by the author.\n"
            "\n"
            "Since publishing my original article, many readers have asked "
            "whether these facilities are responsible for higher electric "
            "bills. This article explains how the system actually works.\n"
        )
        candidate = self._candidate(draft)
        assert "AI tools were used" not in candidate
        assert "many readers have asked" in candidate

    @pytest.mark.parametrize(
        "opener",
        [
            "Disclosure: the author holds shares in one of the companies named "
            "below and has done so for several years now.",
            "The views expressed here are the author's own and not those of any "
            "employer, client or industry body he works with.",
            "This article was written with the assistance of AI, and every "
            "factual claim in it was checked against a primary document.",
        ],
    )
    def test_the_common_disclosure_openers_are_skipped(self, opener):
        draft = (
            f"{opener}\n\nThe argument of this piece is that regional figures "
            "do not transfer between grids, and three planning documents now "
            "assume otherwise.\n"
        )
        assert "do not transfer between grids" in self._candidate(draft)

    def test_a_long_paragraph_is_never_dropped_for_saying_disclosure(self):
        """The word ceiling is what keeps this a boilerplate rule, not a topic one."""
        argument = (
            "Full disclosure of the utility's rate case was ordered by the "
            "commission after four years of litigation, and the documents it "
            "produced are the reason this article can say what the capacity "
            "auction actually cost ratepayers rather than repeating the "
            "developer's estimate, which is the number every prior account of "
            "this dispute has used without checking it against the filings "
            "that were sealed until the order came down last spring.\n"
        )
        assert "Full disclosure of the utility" in self._candidate(f"{argument}\n")


class TestTheProposedSourceListSaysWhatItLeftOut:
    """A silent cap reads as "these are all your sources" — it is not."""

    def _draft_with(self, marker_count):
        entries = "\n".join(
            f"[{n}] Source number {n}, https://example.org/{n}"
            for n in range(1, marker_count + 1)
        )
        return f"# T\n\nBody text.\n\n## Sources\n\n{entries}\n"

    def test_a_short_list_is_shown_whole_with_no_note(self):
        gap = _by_field(
            assess({"title": "T"}, draft=self._draft_with(3), domains_ran=_ALL_DOMAINS),
            "sources_cited",
        )
        bullets = [ln for ln in gap["suggestion"].splitlines() if ln.startswith("- ")]
        assert len(bullets) == 3
        assert "more in the draft" not in gap["suggestion"]

    def test_a_long_list_is_capped_and_says_how_many_it_dropped(self):
        gap = _by_field(
            assess(
                {"title": "T"}, draft=self._draft_with(73), domains_ran=_ALL_DOMAINS
            ),
            "sources_cited",
        )
        assert "and 53 more in the draft's citation block" in gap["suggestion"]
        assert "73 markers" in gap["impact"]


class TestTheAuthorGap:
    """A missing author is only a gap when the draft actually speaks as one.

    The field exists because citation verification cannot check a first-person
    claim without a name. Measured 2026-09-04 against a page that does name the
    author: told nothing, the verifier bound "I" to the first person on the
    page; told only not to guess, it returned `not_addressed` against that same
    page. Both are silent failures that land in Section 9 looking like the
    draft's fault.
    """

    FIRST_PERSON_DRAFT = (
        "# T\n"
        "\n"
        "I co-founded the exchange in 2015, and my notes from the hearing show "
        "three separate outages that quarter.\n"
    )
    IMPERSONAL_DRAFT = (
        "# T\n"
        "\n"
        "The figures do not transfer between regions, and three planning "
        "documents now assume otherwise without checking.\n"
    )

    def _fields(self, handoff, **kw):
        return [g["field"] for g in assess(handoff, domains_ran=_ALL_DOMAINS, **kw)]

    def test_it_fires_when_no_author_is_resolvable_anywhere(self):
        gap = _by_field(
            assess(
                {"title": "T"},
                draft=self.FIRST_PERSON_DRAFT,
                domains_ran=_ALL_DOMAINS,
            ),
            "author",
        )
        assert gap["severity"] == "degrading"
        assert gap["sections"] == ["SECTION 9: Citations"]
        # Citation resolution is a pass, not a review domain — naming a domain
        # here would put a "Built without" note on a section it never touched.
        assert gap["domains"] == []

    def test_the_publication_config_default_closes_it(self):
        """A single-author publication sets author_name once and is covered."""
        assert "author" not in self._fields(
            {"title": "T"},
            pub_config={"author_name": "Mike Hammett"},
            draft=self.FIRST_PERSON_DRAFT,
        )

    def test_the_handoff_line_closes_it(self):
        assert "author" not in self._fields(
            {"title": "T", "author": "Guest Writer"}, draft=self.FIRST_PERSON_DRAFT
        )

    def test_an_impersonal_draft_is_not_nagged(self):
        """Nothing to anchor means nothing was lost — do not report a cost."""
        assert "author" not in self._fields({"title": "T"}, draft=self.IMPERSONAL_DRAFT)

    def test_resolved_first_person_claims_are_counted_when_available(self):
        gap = _by_field(
            assess(
                {"title": "T"},
                draft=self.FIRST_PERSON_DRAFT,
                domains_ran=_ALL_DOMAINS,
                citations=[
                    {"claim": "I co-founded the exchange."},
                    {"claim": "My notes show three outages."},
                    {"claim": "The grid served 41 percent from nuclear."},
                ],
            ),
            "author",
        )
        assert "2 claims put through citation resolution" in gap["impact"]

    def test_it_falls_back_to_draft_sentences_when_nothing_resolved(self):
        """Offline runs resolve no citations; the draft still shows the need."""
        gap = _by_field(
            assess(
                {"title": "T"},
                draft=self.FIRST_PERSON_DRAFT,
                domains_ran=_ALL_DOMAINS,
                citations=[],
            ),
            "author",
        )
        assert "sentence" in gap["impact"]
        assert "citation resolution" not in gap["impact"].split("speak")[0]

    def test_the_guidance_points_at_the_config_not_the_handoff(self):
        """The config default is the fix that covers --raw-draft and --url too."""
        gap = _by_field(
            assess(
                {"title": "T"},
                draft=self.FIRST_PERSON_DRAFT,
                domains_ran=_ALL_DOMAINS,
            ),
            "author",
        )
        assert "author_name" in gap["guidance"]
        assert "--raw-draft" in gap["guidance"]


class TestFirstPersonDetection:
    """Half case-sensitive on purpose — see the constant's own note."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("I asked the county board.", True),
            ("My notes show three.", True),
            ("Send the filing to me.", True),
            ("That is mine.", True),
            ("We asked the county board.", False),
            ("Our coverage area is Northern Illinois.", False),
            ("The grid is complex.", False),
            ("That is, i.e., an example.", False),
        ],
    )
    def test_pronoun_matching(self, text, expected):
        assert bool(handoff_gaps._FIRST_PERSON_RE.search(text)) is expected

    def test_editorial_we_does_not_count_as_a_personal_claim(self):
        """ "We" is corporate or editorial far more often than it is one person."""
        draft = "We asked the county board. Our reporting found three outages."
        assert handoff_gaps._first_person_sentences(draft) == []
