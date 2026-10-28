"""Which source Section 9 reports a claim against, and what a re-ask may change.

Three defects from brockovich-fact-check run 2 (2026-10-28):

* #441: a claim's failure was reported against the *preceding* span's citation
  (a TV guest listing) because the draft's own source was read only in part and
  a partial read ranks below a clean "not addressed".
* #442: a dated figure cited to an archived copy was judged against the live
  page first; the snapshot's supporting quote, the bare number "9,683", failed
  the 12-character floor; the re-ask then proposed the live page's newer number.
* #448: re-asks ran in report order and stopped at 12, leaving a "contradicts"
  misread unchecked while "not addressed" entries were asked.
"""

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review.adapters.citation import draft_citations as dc
from ci_article_review.adapters.citation import reask, resolver

# The shape of run 2's draft: an uncited TV-appearance span, then the facility
# count cited to its own data file.
DRAFT = """\
She went on national television to make the case. [1] As of its September 27 update, the vetted layer lists 545 projects: 139 operational, 166 under construction. [51]

The headline figure was 9,683 reports as of October 4. [2]

## Citations
[1] BroadwayWorld, Real Time with Bill Maher guest lineup: https://tv.example/lineup
[51] Vetted facility data file (data-centers-map.json): https://site.example/map.json
[2] Community Reports Statistics, data through October 4, 2026 (live page has since updated): https://site.example/stats.html (archived October 10, 2026: https://web.archive.org/web/20261010093313/https://site.example/stats.html)
"""


class TestNeighbourUrls:
    def test_the_preceding_spans_citation_is_a_neighbour(self):
        cited = dc.DraftCitations(DRAFT)
        claim = "the vetted layer lists 545 projects: 139 operational, 166 under construction"
        assert cited.candidates_for(claim)[0] == "https://site.example/map.json"
        assert cited.neighbour_urls_for(claim) == ["https://tv.example/lineup"]

    def test_a_named_marker_has_no_neighbours(self):
        cited = dc.DraftCitations(DRAFT)
        assert cited.neighbour_urls_for("545 projects [51]") == []

    def test_the_claim_entry_carries_them(self):
        fact_check = {
            "confirmed": [
                {
                    "claim": "the vetted layer lists 545 projects: 139 operational, 166 under construction"
                }
            ]
        }
        (entry,) = pipeline._collect_citation_claims(fact_check, DRAFT)
        assert entry["neighbour_urls"] == ["https://tv.example/lineup"]

    def test_a_url_the_model_named_is_never_a_neighbour(self):
        fact_check = {
            "confirmed": [
                {
                    "claim": "the vetted layer lists 545 projects: 139 operational, 166 under construction",
                    "source_url": "https://tv.example/lineup",
                }
            ]
        }
        (entry,) = pipeline._collect_citation_claims(fact_check, DRAFT)
        assert "neighbour_urls" not in entry


class TestArchivedCopyFirst:
    def test_an_entry_saying_the_live_page_moved_puts_its_snapshot_first(self):
        entries = dc.parse_citation_block(DRAFT.split("## Citations", 1)[1])
        assert entries["2"]["urls"][0].startswith("https://web.archive.org/")

    def test_a_dated_claim_prefers_the_snapshot_even_without_that_note(self):
        draft = DRAFT.replace(" (live page has since updated)", "")
        cited = dc.DraftCitations(draft)
        urls = cited.candidates_for("9,683 reports as of October 4")
        assert urls[0].startswith("https://web.archive.org/")

    def test_an_undated_claim_keeps_the_authors_order(self):
        draft = DRAFT.replace(" (live page has since updated)", "")
        cited = dc.DraftCitations(draft)
        assert cited.candidates_for("The headline figure was 9,683 reports")[0] == (
            "https://site.example/stats.html"
        )


def _result(url, verification, verdict=None, partial=False):
    return {
        "url": url,
        "verification": verification,
        "relevance_verdict": verdict,
        "read_coverage": {"partial": partial},
        "note": "",
    }


class TestTheHeadlineFailureIsTheClaimsOwnSource:
    def _resolve(self, monkeypatch, outcomes, secondary=()):
        monkeypatch.setattr(
            resolver, "_resolve_known_url", lambda claim, url, **kw: dict(outcomes[url])
        )
        return resolver._resolve_candidates(
            "545 projects", list(outcomes), secondary_urls=set(secondary)
        )

    def test_a_partial_read_of_the_own_source_beats_a_clean_miss_on_a_neighbour(
        self, monkeypatch
    ):
        outcomes = {
            "https://site.example/map.json": _result(
                "https://site.example/map.json", "partial_read", "not_addressed", True
            ),
            "https://tv.example/lineup": _result(
                "https://tv.example/lineup", "content_mismatch", "not_addressed"
            ),
        }
        best = self._resolve(
            monkeypatch, outcomes, secondary={"https://tv.example/lineup"}
        )
        assert best["url"] == "https://site.example/map.json"
        assert best["alternates_checked"] == ["https://tv.example/lineup"]

    def test_without_neighbour_marking_the_old_ranking_holds(self, monkeypatch):
        outcomes = {
            "https://site.example/map.json": _result(
                "https://site.example/map.json", "partial_read", "not_addressed", True
            ),
            "https://tv.example/lineup": _result(
                "https://tv.example/lineup", "content_mismatch", "not_addressed"
            ),
        }
        assert (
            self._resolve(monkeypatch, outcomes)["url"] == "https://tv.example/lineup"
        )

    def test_a_neighbour_can_still_support_the_claim(self, monkeypatch):
        outcomes = {
            "https://site.example/map.json": _result(
                "https://site.example/map.json", "partial_read", "not_addressed", True
            ),
            "https://tv.example/lineup": _result(
                "https://tv.example/lineup", "checksum", "supports"
            ),
        }
        best = self._resolve(
            monkeypatch, outcomes, secondary={"https://tv.example/lineup"}
        )
        assert best["verification"] == "checksum"

    def test_a_snapshot_that_supports_after_a_live_contradiction_says_why(
        self, monkeypatch
    ):
        snap = (
            "https://web.archive.org/web/20261010093313/https://site.example/stats.html"
        )
        outcomes = {
            "https://site.example/stats.html": _result(
                "https://site.example/stats.html", "content_mismatch", "contradicts"
            ),
            snap: _result(snap, "checksum", "supports"),
        }
        best = self._resolve(monkeypatch, outcomes)
        assert best["url"] == snap
        assert "archived copy the draft cites supports this" in best["note"]


class TestBareFigureQuotes:
    PAGE = "Data through October 4, 2026\n\n9,683\n\nTotal Reports\n\nsubmitted through the form"

    def test_the_claims_own_figure_is_grounded(self):
        assert resolver._quote_is_grounded(
            "9,683", self.PAGE, "9,683 reports as of October 4"
        )

    def test_a_figure_the_claim_does_not_state_is_not(self):
        assert not resolver._quote_is_grounded(
            "9,683", self.PAGE, "9,733 reports as of October 22"
        )

    def test_without_the_claim_the_floor_still_applies(self):
        assert not resolver._quote_is_grounded("9,683", self.PAGE)

    @pytest.mark.parametrize("quote", ["12", "7%", "2026 data", "Total"])
    def test_short_non_figures_and_tiny_figures_still_fail(self, quote):
        assert not resolver._quote_is_grounded(
            quote, self.PAGE + " 12 7% 2026 data", f"{quote} claim"
        )


class TestDatedCorrectionsAreDeclined:
    def test_a_correction_that_moves_the_date_is_recorded_as_stand(self):
        result = {"claim": "9,683 reports as of October 4"}
        recorded = {
            "action": "correct_claim",
            "corrected_claim": "9,733 reports as of October 22, 2026",
            "reason": "The page says 9,733.",
        }
        out = reask._keep_the_claims_date(result, recorded)
        assert out["action"] == "stand"
        assert out["declined_correction"] == "9,733 reports as of October 22, 2026"
        assert "oct 4" in out["reason"]

    def test_a_correction_that_keeps_the_date_stands(self):
        result = {"claim": "9,683 reports as of October 4"}
        recorded = {
            "action": "correct_claim",
            "corrected_claim": "9,688 reports as of Oct. 4",
        }
        assert (
            reask._keep_the_claims_date(result, recorded)["action"] == "correct_claim"
        )

    def test_an_undated_claim_is_untouched(self):
        recorded = {
            "action": "correct_claim",
            "corrected_claim": "about 17.4 billion gallons",
        }
        assert (
            reask._keep_the_claims_date({"claim": "17 billion gallons"}, recorded)
            is recorded
        )


class TestReaskOrder:
    def _refuted(self, n_not_addressed, n_contradicts, partial=False):
        rows = [
            dict(
                _result(
                    f"https://n{i}.example",
                    "content_mismatch",
                    "not_addressed",
                    partial,
                ),
                claim=f"n{i}",
                source_model="openai",
            )
            for i in range(n_not_addressed)
        ]
        rows += [
            dict(
                _result(f"https://c{i}.example", "content_mismatch", "contradicts"),
                claim=f"c{i}",
                source_model="openai",
            )
            for i in range(n_contradicts)
        ]
        return rows

    def _run(self, monkeypatch, results, limit):
        asked = []

        def fake_one(result, provider, api_key, provider_config, call_log, author):
            asked.append(result["claim"])
            return {"action": "stand", "reason": "", "asked_model": provider}

        monkeypatch.setattr(reask, "reask_one", fake_one)
        monkeypatch.setattr(reask, "has_credentials", lambda *a, **k: True)
        reask.reask_refuted(results, api_keys={"openai": {"api_key": "k"}}, limit=limit)
        return asked

    def test_contradictions_go_first(self, monkeypatch):
        asked = self._run(monkeypatch, self._refuted(5, 2), limit=3)
        assert asked[:2] == ["c0", "c1"]
        assert len(asked) == 3

    def test_no_contradiction_is_cut_by_the_limit(self, monkeypatch):
        asked = self._run(monkeypatch, self._refuted(3, 4), limit=2)
        assert {"c0", "c1", "c2", "c3"} <= set(asked)

    def test_a_whole_read_is_asked_before_a_partial_one(self, monkeypatch):
        rows = self._refuted(1, 0, partial=True) + [
            dict(
                _result("https://w.example", "content_mismatch", "not_addressed"),
                claim="whole",
                source_model="openai",
            )
        ]
        assert self._run(monkeypatch, rows, limit=1) == ["whole"]
