"""Tests for the expansion domain — the one pass that proposes instead of judging.

It inverts three assumptions the rest of the pipeline is built on, and each
inversion is pinned here because each one is easy to "fix" back:

1. **It is opt-in.** Every other domain runs on every run. This one costs money
   to propose work the author will mostly decline, and on a late revision it is
   actively unhelpful, so it runs only behind ``--expand``.
2. **Consensus does not apply.** Sections 1-6 rank a finding higher when two
   models agree. A suggestion two models made is usually just the obvious one,
   and the sole-source proposal is regularly the one that paid for the pass —
   so section 10 is a union, and nothing is dropped for standing alone.
3. **Its URLs are checked and the failures are kept.** A model that cannot find
   a source will invent one, and an invented URL beside a confident description
   is the only thing in this report that actively misleads. Dropping the
   failures would produce a tidier section that hides the pass's own error rate.
"""

from unittest.mock import patch

import ci_article_review.pipeline as pipeline
from ci_article_review import consolidation
from ci_article_review.consolidation import (
    _build_expansion,
    _find_consensus,
    build_report,
)
from ci_article_review.pipeline import _build_assignments, _verify_expansion_urls
from ci_article_review.report_markdown import _render_section_10


_ALL_MODELS = ["gemini", "openai", "mistral", "grok", "claude", "perplexity"]
_ALL_KEYS = {m: {"api_key": "k"} for m in _ALL_MODELS}
_SIMPLE_CONFIGS = {m: f"{m}-test-model" for m in _ALL_MODELS}


def _ok(data, model="test-model"):
    return {"failed": False, "data": data, "model": model, "tokens": {}}


def _source(title="EPA eGRID 2024", url="https://example.org/egrid", **extra):
    return {
        "title": title,
        "url": url,
        "where_to_look": "Table 2",
        "what_it_establishes": "regional emissions rates",
        "supports": "the grid-mix paragraph",
        "why_it_fits": "quantifies the claim already being made",
        **extra,
    }


def _topic(topic="Interconnection queue timelines"):
    return {
        "topic": topic,
        "why_it_fits": "explains the delay the piece already asserts",
        "audience_served": "planners",
        "where_it_would_go": "after the siting section",
        "closes_known_gap": None,
    }


# ---------------------------------------------------------------------------
# 1. Opt-in
# ---------------------------------------------------------------------------


class TestItIsOptIn:
    def test_absent_from_every_preset_by_default(self):
        for thoroughness in ("standard", "thorough", "maximum"):
            assignments = _build_assignments(thoroughness, _SIMPLE_CONFIGS, _ALL_KEYS)
            assert not [d for _, d in assignments if d == "expansion"], (
                f"{thoroughness} scheduled expansion without --expand"
            )

    def test_present_when_asked_for(self):
        assignments = _build_assignments(
            "standard",
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            include_expansion=True,
        )
        assert [m for m, d in assignments if d == "expansion"] == ["perplexity"]

    def test_thorough_leaves_grok_out(self):
        """Evidence, not taste: grok already runs red_team at this level, so
        adding expansion doubled its concurrent calls against the provider with
        the tightest timeout budget — and it then failed both domains on two
        consecutive live runs, recovering neither."""
        assignments = _build_assignments(
            "thorough",
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            include_expansion=True,
        )
        expansion_models = sorted(m for m, d in assignments if d == "expansion")
        assert expansion_models == ["gemini", "perplexity"]
        # Still available where the run has opted into the cost.
        assert ("grok", "expansion") in _build_assignments(
            "maximum",
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            include_expansion=True,
        )

    def test_maximum_runs_every_model_that_did_not_draft_it(self):
        assignments = _build_assignments(
            "maximum",
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            include_expansion=True,
        )
        assert sorted(m for m, d in assignments if d == "expansion") == sorted(
            _ALL_MODELS
        )

    def test_a_prompts_override_cannot_switch_it_on_permanently(self):
        """The flag answers a per-run question; a config file must not answer it.

        Otherwise the one domain deliberately kept off the revision loop is back
        on every revision, and the author never chose that per draft.
        """
        configs = dict(_SIMPLE_CONFIGS)
        configs["grok"] = {"model": "grok-test", "prompts": ["expansion"]}

        assignments = _build_assignments("standard", configs, _ALL_KEYS)
        assert not [d for _, d in assignments if d == "expansion"]

        assignments = _build_assignments(
            "standard",
            configs,
            _ALL_KEYS,
            include_expansion=True,
        )
        assert ("grok", "expansion") in assignments

    def test_no_unreviewed_warning_for_a_pass_nobody_asked_for(self, caplog):
        """The drafter-exclusion warning must stay silent on the common path."""
        with caplog.at_level("WARNING"):
            _build_assignments("standard", _SIMPLE_CONFIGS, _ALL_KEYS, "claude")
        assert "expansion" not in caplog.text


class TestTheDrafterIsExcluded:
    """Stronger than the voice_style case: the drafting model already searched
    this space and dropped what it is about to propose again."""

    def test_the_drafting_model_does_not_propose(self):
        assignments = _build_assignments(
            "maximum",
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            "perplexity",
            include_expansion=True,
        )
        assert ("perplexity", "expansion") not in assignments
        assert ("gemini", "expansion") in assignments

    def test_it_is_declared_alongside_voice_style(self):
        assert "expansion" in pipeline._DRAFTER_EXCLUDED_DOMAINS


# ---------------------------------------------------------------------------
# 2. Union, not consensus
# ---------------------------------------------------------------------------


class TestUnionNotConsensus:
    def test_a_sole_source_proposal_survives(self):
        results = {("grok", "expansion"): _ok({"topics": [_topic()]})}
        out = _build_expansion(results, {})
        assert len(out["topics"]) == 1
        assert out["topics"][0]["proposed_by"] == ["grok"]
        assert out["topics"][0]["convergent"] is False

    def test_nothing_reaches_section_1(self):
        """Expansion has no passage to key on and no business being ranked."""
        results = {
            ("grok", "expansion"): _ok({"sources": [_source()], "topics": [_topic()]})
        }
        consensus, _ = _find_consensus(results, [], {})
        assert consensus == []

    def test_the_same_url_from_two_models_merges_and_records_both(self):
        results = {
            ("perplexity", "expansion"): _ok({"sources": [_source()]}),
            # Same page, different title and trailing slash.
            ("gemini", "expansion"): _ok(
                {
                    "sources": [
                        _source(title="eGRID data", url="https://example.org/egrid/")
                    ]
                }
            ),
        }
        out = _build_expansion(results, {})
        assert len(out["sources"]) == 1
        assert sorted(out["sources"][0]["proposed_by"]) == ["gemini", "perplexity"]
        assert out["sources"][0]["convergent"] is True

    def test_distinct_proposals_are_both_kept(self):
        results = {
            ("perplexity", "expansion"): _ok({"topics": [_topic("Queue timelines")]}),
            ("gemini", "expansion"): _ok({"topics": [_topic("Water rights")]}),
        }
        out = _build_expansion(results, {})
        assert len(out["topics"]) == 2
        assert all(t["convergent"] is False for t in out["topics"])

    def test_convergence_does_not_reorder(self):
        """Two models agreeing must not promote a proposal above a sole one.

        This is the assertion that stops section 10 from quietly becoming a
        consensus ranking again.
        """
        results = {
            ("perplexity", "expansion"): _ok(
                {"topics": [_topic("Sole"), _topic("Shared")]}
            ),
            ("gemini", "expansion"): _ok({"topics": [_topic("Shared")]}),
        }
        out = _build_expansion(results, {})
        assert [t["topic"] for t in out["topics"]] == ["Sole", "Shared"]

    def test_ordering_is_stable_rather_than_weighted(self):
        """Section 10 carries no weights of its own any more.

        It had some — perplexity 1.3, gemini 1.2 — on the reasoning that a model
        which cannot fetch is guessing at URLs. The 2026-09-03 audit deleted the
        static grounding bonus for precisely that reasoning: it guessed which
        models ground rather than observing whether they did, and was wrong in
        both directions. Weights here only ever ordered the list, so ordering
        now falls back to encounter order and nothing is silently ranked by a
        number nobody measured.
        """
        results = {
            ("claude", "expansion"): _ok({"topics": [_topic("From claude")]}),
            ("perplexity", "expansion"): _ok({"topics": [_topic("From perplexity")]}),
        }
        out = _build_expansion(results, {})
        assert {t["topic"] for t in out["topics"]} == {
            "From claude",
            "From perplexity",
        }
        assert len(out["models"]) == 2

    def test_a_configured_weight_is_still_honoured(self):
        """Dropping the defaults did not remove the mechanism — ensemble.weights
        in user.yaml still orders the section for anyone who wants it to."""
        results = {
            ("claude", "expansion"): _ok({"topics": [_topic("From claude")]}),
            ("perplexity", "expansion"): _ok({"topics": [_topic("From perplexity")]}),
        }
        cfg = {"weights": {"perplexity": {"expansion": 2.0}}}
        out = _build_expansion(results, cfg)
        assert out["topics"][0]["topic"] == "From perplexity"
        assert out["models"][0] == "perplexity"

    def test_a_pass_that_did_not_run_is_distinguishable_from_one_that_found_nothing(
        self,
    ):
        assert _build_expansion({}, {}) == {}
        ran = _build_expansion({("grok", "expansion"): _ok({"topics": []})}, {})
        assert ran != {}
        assert ran["topics"] == []

    def test_a_failed_call_contributes_nothing(self):
        results = {
            ("grok", "expansion"): {"failed": True, "error": "timeout", "data": None}
        }
        assert _build_expansion(results, {}) == {}

    def test_build_report_always_carries_the_key(self):
        """Absent on a default run, but present — so report readers can rely on it."""
        report = build_report(
            "T", "pub", 1, "draft", None, {}, {}, [], primary_claim="c"
        )
        assert report["section_10_expansion"] == {}


# ---------------------------------------------------------------------------
# 3. URL verification — failures are marked, never dropped
# ---------------------------------------------------------------------------


def _link_results(mapping):
    """Stub validate_links. Values mirror the real result dicts links.py returns.

    Keys are URLs; values are the result dict minus ``url``, which is filled in.
    """

    def _fake(text, check_wayback=True, **kwargs):
        out = []
        for url in text.split():
            result = dict(mapping.get(url, {"ok": False, "status_code": 404}))
            result["url"] = url
            out.append(result)
        return out

    return _fake


_OK = {"ok": True, "status_code": 200, "verified_via": "direct"}
_DEAD = {"ok": False, "status_code": 404}
_NO_HOST = {
    "ok": False,
    "status_code": None,
    "error": "NameResolutionError: Failed to resolve",
    "origin_failure": "unreachable",
}
_BLOCKED = {"ok": False, "status_code": 403, "origin_failure": "blocked"}
_ARCHIVED = {
    "ok": True,
    "status_code": 403,
    "origin_failure": "blocked",
    "verified_via": "wayback_fallback",
    "wayback_snapshot_url": "https://web.archive.org/web/2020/https://x.example/doc",
}


class TestUrlVerdictsAreTiered:
    """A 403 is the origin refusing us; it is not evidence of invention.

    The first live run reported two real sleep-society position statements
    (ESRS, Contemporary Pediatrics — both 403) under the same "very likely
    invented" banner as ``sleep.aasm.org``, a hostname that does not resolve.
    That is the error CITATIONS.md forbids everywhere else in this pipeline:
    "we could not read it" reported as "it is not there".
    """

    def _verify(self, expansion, mapping):
        with patch(
            "ci_article_review.analysis.links.validate_links", _link_results(mapping)
        ):
            _verify_expansion_urls(expansion)
        return expansion

    def test_a_404_is_called_invented(self):
        expansion = {"sources": [_source(url="https://x.example/gone")]}
        self._verify(expansion, {"https://x.example/gone": _DEAD})
        assert expansion["sources"][0]["url_status"] == "missing"
        assert expansion["url_check"]["missing"] == 1

    def test_a_hostname_that_does_not_resolve_is_called_invented(self):
        expansion = {"sources": [_source(url="https://nope.example/doc")]}
        self._verify(expansion, {"https://nope.example/doc": _NO_HOST})
        assert expansion["sources"][0]["url_status"] == "missing"
        assert expansion["url_check"]["missing"] == 1

    def test_a_403_is_not(self):
        """The regression this whole tier split exists for."""
        expansion = {"sources": [_source(url="https://esrs.example/statement.pdf")]}
        self._verify(expansion, {"https://esrs.example/statement.pdf": _BLOCKED})

        item = expansion["sources"][0]
        assert item["url_status"] == "blocked"
        assert expansion["url_check"]["blocked"] == 1
        assert expansion["url_check"]["missing"] == 0, (
            "a blocked page must not count toward the invented total"
        )
        assert "invent" not in item["url_error"].lower()

    def test_an_archive_recovery_says_the_source_is_real(self):
        expansion = {"sources": [_source(url="https://x.example/doc")]}
        self._verify(expansion, {"https://x.example/doc": _ARCHIVED})

        item = expansion["sources"][0]
        assert item["url_status"] == "archived"
        assert item["url_snapshot"].startswith("https://web.archive.org/")
        assert expansion["url_check"] == {
            "checked": 1,
            "resolved": 0,
            "archived": 1,
            "blocked": 0,
            "missing": 0,
            "not_citable": 0,
            "no_url": 0,
            "followed_redirects": 0,
        }

    def test_every_failure_is_kept_whatever_its_tier(self):
        expansion = {
            "sources": [
                _source(title="Real", url="https://x.example/ok"),
                _source(title="Blocked", url="https://x.example/403"),
                _source(title="Invented", url="https://x.example/404"),
            ]
        }
        self._verify(
            expansion,
            {
                "https://x.example/ok": _OK,
                "https://x.example/403": _BLOCKED,
                "https://x.example/404": _DEAD,
            },
        )
        assert [s["title"] for s in expansion["sources"]] == [
            "Real",
            "Blocked",
            "Invented",
        ], "no tier may drop a proposal"


class TestNonCitableAndMisleadingUrls:
    def _verify(self, expansion, mapping):
        with patch(
            "ci_article_review.analysis.links.validate_links", _link_results(mapping)
        ):
            _verify_expansion_urls(expansion)
        return expansion

    _WRAPPER = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
        "AUZIYQHJAouv7N7jTzKZletzZt7T"
    )

    def test_a_wrapper_is_followed_to_the_page_it_names(self):
        """Rejecting these on shape threw the source away.

        Gemini's grounding metadata returns every citation as one of these, so
        rejecting them discarded gemini's sourcing wholesale. Following the
        redirect is the established workaround, and it recovers more than a
        URL: the one wrapper live run 1 produced resolved to a Facebook page
        carrying three economic figures gemini had cited. The wrapper concealed
        that provenance, and rejecting it concealed the concealment.
        """
        real = "https://www.example.org/the-actual-article"
        expansion = {"sources": [_source(url=self._WRAPPER)]}
        self._verify(
            expansion,
            {
                self._WRAPPER: {**_OK, "redirected_to": real},
                real: _OK,
            },
        )
        item = expansion["sources"][0]
        assert item["url"] == real, "the proposal should now carry the real URL"
        assert item["url_via_redirect"] == self._WRAPPER, "provenance kept"
        assert item["url_status"] == "ok"
        assert expansion["url_check"]["followed_redirects"] == 1
        assert expansion["url_check"]["not_citable"] == 0

    def test_an_expired_wrapper_is_not_citable_and_says_why(self):
        """They are documented as temporary. An expired one names nothing."""
        expansion = {"sources": [_source(url=self._WRAPPER)]}
        self._verify(expansion, {self._WRAPPER: _DEAD})

        item = expansion["sources"][0]
        assert item["url_status"] == "not_citable"
        assert "expire" in item["url_error"]
        assert expansion["url_check"]["not_citable"] == 1

    def test_a_wrapper_pointing_at_another_wrapper_is_not_adopted(self):
        expansion = {"sources": [_source(url=self._WRAPPER)]}
        self._verify(
            expansion,
            {self._WRAPPER: {**_OK, "redirected_to": self._WRAPPER + "/again"}},
        )
        assert expansion["sources"][0]["url_status"] == "not_citable"

    def test_a_redirect_to_a_different_document_is_flagged(self):
        """Live: a Smithsonian URL for the 1974 DST experiment resolved 200 and
        landed on an article about the casting of The Godfather."""
        url = "https://www.smithsonianmag.com/smart-news/what-happened-180979732/"
        expansion = {"sources": [_source(url=url)]}
        self._verify(
            expansion,
            {
                url: {
                    **_OK,
                    "redirected_to": (
                        "https://www.smithsonianmag.com/smithsonian-institution/"
                        "studio-executives-180979732/"
                    ),
                }
            },
        )
        item = expansion["sources"][0]
        assert item["url_status"] == "redirected"
        assert "studio-executives" in item["url_redirected_to"]

    def test_a_cosmetic_redirect_is_not_flagged(self):
        """http to https, www, a trailing slash — all the same document."""
        url = "http://example.org/doc"
        expansion = {"sources": [_source(url=url)]}
        self._verify(
            expansion, {url: {**_OK, "redirected_to": "https://www.example.org/doc/"}}
        )
        assert expansion["sources"][0]["url_status"] == "ok"


class TestUrlVerificationBasics:
    def test_a_null_url_is_a_lead_not_a_failure(self):
        """The prompt asks for null over a guess; scoring it as a failure would
        punish the honest answer and push models back toward inventing URLs."""
        expansion = {"sources": [_source(url=None)]}
        with patch(
            "ci_article_review.analysis.links.validate_links", _link_results({})
        ):
            _verify_expansion_urls(expansion)

        assert expansion["sources"][0]["url_status"] == "no_url"
        assert expansion["url_check"]["no_url"] == 1
        assert expansion["url_check"]["missing"] == 0

    def test_data_point_urls_are_checked_too(self):
        expansion = {
            "data_points": [
                {
                    "data_point": "2024 load growth",
                    "why_it_strengthens": "quantifies it",
                    "where_to_find_it": "EIA-861",
                    "url": "https://eia.example/861",
                }
            ]
        }
        with patch(
            "ci_article_review.analysis.links.validate_links",
            _link_results({"https://eia.example/861": _OK}),
        ):
            _verify_expansion_urls(expansion)
        assert expansion["data_points"][0]["url_status"] == "ok"

    def test_offline_checks_nothing_and_claims_nothing(self):
        expansion = {"sources": [_source(), _source(url=None)]}
        with patch(
            "ci_article_review.analysis.links.validate_links",
            side_effect=AssertionError("offline must not hit the network"),
        ):
            _verify_expansion_urls(expansion, offline=True)

        assert expansion["sources"][0]["url_status"] == "unchecked"
        assert expansion["sources"][1]["url_status"] == "no_url"

    def test_an_empty_expansion_is_left_alone(self):
        assert _verify_expansion_urls({}) == {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_it_says_the_pass_did_not_run(self):
        out = "\n".join(_render_section_10({}))
        assert "--expand" in out

    def test_ran_but_proposed_nothing_reads_differently(self):
        out = "\n".join(
            _render_section_10(
                {"sources": [], "topics": [], "angles": [], "data_points": []}
            )
        )
        assert "proposed nothing" in out
        assert "--expand" not in out

    def test_a_dead_link_is_called_out_loudly(self):
        expansion = {
            "sources": [
                dict(
                    _source(title="Invented"),
                    proposed_by=["grok"],
                    convergent=False,
                    url_status="missing",
                    url_error="HTTP 404",
                )
            ],
            "url_check": {"checked": 1, "resolved": 0, "missing": 1},
            "models": ["grok"],
        }
        out = "\n".join(_render_section_10(expansion))
        assert "LINK DEAD" in out
        assert "very likely invented" in out

    def test_a_blocked_link_is_not_accused(self):
        """The banner is drawn from `missing` alone."""
        expansion = {
            "sources": [
                dict(
                    _source(title="Real but blocked"),
                    proposed_by=["gemini"],
                    convergent=False,
                    url_status="blocked",
                    url_error="blocked (403) — the page very likely exists",
                )
            ],
            "url_check": {"checked": 1, "resolved": 0, "blocked": 1, "missing": 0},
            "models": ["gemini"],
        }
        out = "\n".join(_render_section_10(expansion))
        assert "blocked, not disproved" in out
        assert "invented" not in out, (
            "a 403 must not be reported as a likely fabrication"
        )

    def test_proposals_name_who_made_them(self):
        expansion = {
            "topics": [dict(_topic(), proposed_by=["gemini", "grok"], convergent=True)],
            "models": ["gemini", "grok"],
        }
        out = "\n".join(_render_section_10(expansion))
        assert "gemini, grok" in out
        assert "also proposed independently" in out

    def test_it_frames_itself_as_a_menu(self):
        """The heading and preamble are what stop it competing with triage."""
        out = "\n".join(_render_section_10({"topics": [], "models": []}))
        assert "SECTION 10" in out
        assert "not findings" in out.lower()


class TestRetryFailedAcrossAConfigChange:
    """Dropping grok from `thorough` made a latent gap reachable.

    ``--retry-failed`` reads the names to re-attempt from a capture written by
    an earlier run. Any assignment change since then — this preset edit, a
    disabled model, a run without --expand — leaves names that cannot be
    scheduled now. They used to be filtered out in silence while the caller
    went on reporting them as attempted.
    """

    def test_an_unschedulable_name_is_reported_not_swallowed(self, caplog):
        runners = [("gemini:expansion", lambda: {"failed": False, "data": {}})]
        with caplog.at_level("WARNING"):
            pipeline._run_reviews_for_names(
                ["gemini:expansion", "grok:expansion"],
                runners,
                {"parallel_review_calls": False},
                {},
                60,
            )
        assert "grok:expansion" in caplog.text
        assert "NOT re-attempted" in caplog.text

    def test_silence_when_every_name_can_be_scheduled(self, caplog):
        runners = [("gemini:expansion", lambda: {"failed": False, "data": {}})]
        with caplog.at_level("WARNING"):
            pipeline._run_reviews_for_names(
                ["gemini:expansion"], runners, {"parallel_review_calls": False}, {}, 60
            )
        assert "NOT re-attempted" not in caplog.text


class TestTheCeilingReachesTheModel:
    """The prompt states a per-bucket ceiling substituted from one constant.

    Two live runs returned 34 and 25 candidates for a ~1,000-word draft against
    a prompt that only said "do not pad". An unbounded instruction to be
    selective is not an instruction.
    """

    def test_the_placeholder_is_substituted(self):
        """A literal {max_per_bucket} would reach the model as gibberish."""
        rendered = pipeline._render_prompt(
            pipeline._load_prompt("expansion.txt"),
            max_per_bucket=pipeline._EXPANSION_MAX_PER_BUCKET,
        )
        assert "{max_per_bucket}" not in rendered
        assert f"at most {pipeline._EXPANSION_MAX_PER_BUCKET} per bucket" in rendered

    def test_the_prompt_still_declares_the_placeholder(self):
        """Guards the other direction: dropping it from the prompt would leave
        the constant wired to nothing and silently restore the old behaviour."""
        assert "{max_per_bucket}" in pipeline._load_prompt("expansion.txt")


class TestPaddingIsVisible:
    """Nothing is trimmed, so the ceiling only works if going over is reported."""

    def _expansion(self, **per_bucket):
        out = {"models": ["perplexity", "gemini"], "max_per_bucket": 3}
        for bucket, entries in per_bucket.items():
            out[bucket] = [
                {"title": t, "topic": t, "proposed_by": [m], "convergent": False}
                for t, m in entries
            ]
        return out

    def test_counts_are_broken_out_per_model(self):
        expansion = self._expansion(
            sources=[("a", "perplexity"), ("b", "perplexity"), ("c", "gemini")]
        )
        out = "\n".join(_render_section_10(expansion))
        assert "perplexity 2" in out
        assert "gemini 1" in out

    def test_a_model_over_the_ceiling_is_named(self):
        expansion = self._expansion(
            sources=[(str(n), "gemini") for n in range(5)] + [("x", "perplexity")]
        )
        out = "\n".join(_render_section_10(expansion))
        assert "Over the 3-per-bucket ceiling" in out
        assert "gemini" in out.split("Over the")[1].split(".")[0]
        assert "Nothing was trimmed" in out

    def test_no_note_when_everyone_stayed_under(self):
        expansion = self._expansion(sources=[("a", "perplexity"), ("b", "gemini")])
        out = "\n".join(_render_section_10(expansion))
        assert "ceiling" not in out

    def test_a_report_saved_without_a_ceiling_still_renders(self):
        """Reports written before the ceiling existed carry no max_per_bucket."""
        expansion = self._expansion(sources=[("a", "perplexity")])
        del expansion["max_per_bucket"]
        out = "\n".join(_render_section_10(expansion))
        assert "perplexity 1" in out
        assert "ceiling" not in out


# ---------------------------------------------------------------------------
# Does the page say what the proposal claims it says?
# ---------------------------------------------------------------------------


def _verified(item, **result):
    """Run source verification with a stubbed resolver returning ``result``."""
    expansion = {"sources": [item]}
    with patch(
        "ci_article_review.adapters.citation.resolver.verify_source_supports",
        return_value=result,
    ):
        pipeline._verify_expansion_sources(expansion, {"mistral": {"api_key": "k"}})
    return expansion


class TestSourcesAreReadNotJustPinged:
    """A URL answering 200 is not the same as a page saying what was claimed.

    Live run 3 produced exactly that gap: a Smithsonian link that resolved and
    served an article about the casting of The Godfather. It was caught only
    because the server redirected — a 200 with unrelated content would have
    been invisible. Section 9 has held citations to "fetched, read, and
    confirmed" all along; proposals were being held to a weaker standard than
    the citations they might become.
    """

    def test_a_page_that_backs_the_claim_is_marked_supported(self):
        expansion = _verified(
            _source(url="https://x.example/doc", url_status="ok"),
            verification="checksum",
            relevance_reason="states the figure directly",
            relevance_quote="The 2024 figure was 41 percent.",
        )
        item = expansion["sources"][0]
        assert item["source_verdict"] == "supported"
        assert item["source_verdict_quote"].startswith("The 2024 figure")
        assert expansion["source_check"]["supported"] == 1

    def test_a_page_that_does_not_back_it_is_marked_and_kept(self):
        expansion = _verified(
            _source(url="https://x.example/godfather", url_status="ok"),
            verification="content_mismatch",
            relevance_reason="the page is about film casting",
        )
        item = expansion["sources"][0]
        assert item["source_verdict"] == "does_not_support"
        assert len(expansion["sources"]) == 1, "an adverse verdict must not drop it"
        assert expansion["source_check"]["does_not_support"] == 1

    def test_an_unreadable_page_is_not_an_adverse_finding(self):
        """The rule the URL tiers exist for, applied one level deeper."""
        expansion = _verified(
            _source(url="https://x.example/pdf", url_status="ok"),
            verification="unverifiable",
            note="no text layer in the PDF",
        )
        item = expansion["sources"][0]
        assert item["source_verdict"] == "unreadable"
        assert expansion["source_check"]["does_not_support"] == 0

    def test_a_verification_error_never_fails_the_run(self):
        expansion = {"sources": [_source(url="https://x.example/d", url_status="ok")]}
        with patch(
            "ci_article_review.adapters.citation.resolver.verify_source_supports",
            side_effect=RuntimeError("boom"),
        ):
            pipeline._verify_expansion_sources(expansion, {})
        assert expansion["sources"][0]["source_verdict"] == "unreadable"

    def test_dead_and_non_citable_urls_are_not_fetched(self):
        """No page to read, so no model call to spend."""
        expansion = {
            "sources": [
                _source(
                    title="dead", url="https://x.example/404", url_status="missing"
                ),
                _source(
                    title="redirector", url="https://x/g", url_status="not_citable"
                ),
            ]
        }
        with patch(
            "ci_article_review.adapters.citation.resolver.verify_source_supports",
            side_effect=AssertionError("must not fetch an unreadable candidate"),
        ):
            pipeline._verify_expansion_sources(expansion, {})
        assert expansion["source_check"]["unchecked"] == 2
        assert expansion["source_check"]["checked"] == 0

    def test_offline_reads_nothing(self):
        expansion = {"sources": [_source(url="https://x.example/d", url_status="ok")]}
        with patch(
            "ci_article_review.adapters.citation.resolver.verify_source_supports",
            side_effect=AssertionError("offline must not fetch"),
        ):
            pipeline._verify_expansion_sources(expansion, {}, offline=True)
        assert expansion["sources"][0]["source_verdict"] == "unchecked"

    def test_a_source_stating_nothing_to_establish_is_skipped(self):
        item = _source(url="https://x.example/d", url_status="ok")
        item["what_it_establishes"] = ""
        expansion = {"sources": [item]}
        with patch(
            "ci_article_review.adapters.citation.resolver.verify_source_supports",
            side_effect=AssertionError("nothing to verify against"),
        ):
            pipeline._verify_expansion_sources(expansion, {})
        assert expansion["sources"][0]["source_verdict"] == "unchecked"


class TestCrossBucketReuse:
    """The per-bucket ceiling counts per bucket, so it cannot see this.

    In live run 1 all eight data points with a URL pointed at a source already
    listed, so "34 candidates" overstated the new ground by a quarter.
    """

    def test_a_data_point_reusing_a_source_url_is_flagged(self):
        expansion = {
            "sources": [_source(url="https://x.example/report")],
            "data_points": [
                {"data_point": "a figure", "url": "https://X.Example/report/"},
                {"data_point": "another", "url": "https://y.example/other"},
            ],
        }
        pipeline._flag_expansion_reuse(expansion)
        assert expansion["data_points"][0]["reuses_proposed_source"] is True
        assert "reuses_proposed_source" not in expansion["data_points"][1]
        assert expansion["cross_bucket_reuse"] == 1

    def test_it_shows_up_in_the_rendered_section(self):
        expansion = {
            "models": ["perplexity"],
            "sources": [dict(_source(), proposed_by=["perplexity"])],
            "data_points": [
                {
                    "data_point": "a figure",
                    "url": "https://example.org/egrid",
                    "proposed_by": ["perplexity"],
                    "reuses_proposed_source": True,
                }
            ],
            "cross_bucket_reuse": 1,
            "url_check": {"checked": 2, "resolved": 2},
        }
        out = "\n".join(_render_section_10(expansion))
        assert "overstates how much new ground" in out
        assert "not new ground" in out


class TestOneUrlNormalisation:
    """Three copies of this drifted apart inside a single change."""

    def test_the_pipeline_uses_consolidation_s_definition(self):
        from ci_article_review import consolidation

        assert pipeline._redirect_is_material(
            "https://a.example/one", "https://a.example/two"
        )
        # Cosmetic differences are the same page under the shared key.
        assert not pipeline._redirect_is_material(
            "http://a.example/one", "https://www.a.example/one/"
        )
        assert consolidation.url_key("http://WWW.A.example/one/") == "a.example/one"


class TestUrlKeyErrsTowardNotMerging:
    """Identity is decided in code, not by a model: it is syntax, the two URLs
    being compared come from different models that never saw each other, and a
    nondeterministic answer to a decidable question would merge differently on
    every run. Each rule below is chosen so the error mode is a duplicate line,
    never a deleted proposal.
    """

    def test_path_case_is_significant(self):
        """The bug this class exists for. Paths are case-sensitive on nearly
        every server, so folding the whole URL merged two documents — the
        destructive direction."""
        assert consolidation.url_key(
            "https://example.org/Report.pdf"
        ) != consolidation.url_key("https://example.org/report.pdf")

    def test_host_case_is_not(self):
        assert consolidation.url_key(
            "https://EXAMPLE.org/Doc"
        ) == consolidation.url_key("https://example.org/Doc")

    def test_a_fragment_is_the_same_document(self):
        """Never sent to the server, so it cannot name a different page."""
        assert consolidation.url_key(
            "https://example.org/doc#section-3"
        ) == consolidation.url_key("https://example.org/doc")

    def test_tracking_parameters_are_noise(self):
        assert consolidation.url_key(
            "https://example.org/doc?utm_source=x&utm_campaign=y"
        ) == consolidation.url_key("https://example.org/doc")

    def test_other_query_parameters_are_identity(self):
        """A blanket strip would collapse two different documents."""
        assert consolidation.url_key(
            "https://example.org/p?id=42"
        ) != consolidation.url_key("https://example.org/p?id=43")

    def test_a_tracked_url_keeps_its_real_parameters(self):
        assert consolidation.url_key(
            "https://example.org/p?id=42&utm_source=x"
        ) == consolidation.url_key("https://example.org/p?id=42")

    def test_scheme_www_and_trailing_slash_are_cosmetic(self):
        assert consolidation.url_key(
            "http://www.example.org/doc/"
        ) == consolidation.url_key("https://example.org/doc")

    def test_query_argument_order_is_not_identity(self):
        """Borrowed from w3lib's canonicalize_url rather than depending on it."""
        assert consolidation.url_key(
            "https://example.org/p?b=2&a=1"
        ) == consolidation.url_key("https://example.org/p?a=1&b=2")

    def test_percent_encoding_case_is_not_identity(self):
        """%2f and %2F are the same octet."""
        assert consolidation.url_key(
            "https://example.org/%2Fa"
        ) == consolidation.url_key("https://example.org/%2fa")

    def test_the_prompt_tells_models_not_to_send_search_wrappers(self):
        """The other half of the split: models emit citable URLs, code decides
        identity. Gemini returned grounding redirects until this was explicit."""
        prompt = pipeline._load_prompt("expansion.txt")
        assert "redirect through a search tool" in prompt
        assert "follow it and return where it lands" in prompt


class TestUngroundedExpansionIsWarnedAbout:
    """Only for providers whose requests a ``web_search`` entry actually changes.

    This warning was wrong when first written. After four live runs in which
    gemini produced nearly every dead URL, it told the author to add
    ``web_search: [fact_check, expansion]`` under ``models.gemini``. That key is
    never read for gemini — ``ci_core.llm.client`` attaches googleSearch to every
    gemini call unconditionally, so it was already searching. The advice would
    have sent the author to fix something that was not broken, and left the real
    cause unexamined.
    """

    def test_a_provider_that_honours_the_flag_is_named(self, caplog):
        configs = {"openai": {"model": "gpt-5.6", "web_search": ["fact_check"]}}
        with caplog.at_level("WARNING"):
            pipeline._warn_on_ungrounded_expansion([("openai", "expansion")], configs)
        assert "openai" in caplog.text
        assert "web_search: [fact_check, expansion]" in caplog.text

    def test_it_stops_once_the_flag_covers_the_domain(self, caplog):
        configs = {"openai": {"model": "g", "web_search": ["fact_check", "expansion"]}}
        with caplog.at_level("WARNING"):
            pipeline._warn_on_ungrounded_expansion([("openai", "expansion")], configs)
        assert caplog.text == ""

    def test_gemini_is_never_told_to_set_a_key_it_does_not_read(self, caplog):
        """The regression this class was rewritten for."""
        with caplog.at_level("WARNING"):
            pipeline._warn_on_ungrounded_expansion(
                [("gemini", "expansion")], {"gemini": {"model": "gemini-2.5-flash"}}
            )
        assert caplog.text == ""

    def test_the_llm_layer_still_grounds_gemini_unconditionally(self):
        """Pins the fact the exemption rests on. If this stops being true, the
        exemption is wrong and this test says so rather than the warning
        quietly going missing."""
        from ci_core.llm import client

        params = client._provider_params("gemini", {})
        assert params.get("tools") == [{"googleSearch": {}}]

    def test_search_native_providers_need_no_config(self, caplog):
        with caplog.at_level("WARNING"):
            pipeline._warn_on_ungrounded_expansion(
                [("perplexity", "expansion")], {"perplexity": {"model": "sonar-pro"}}
            )
        assert caplog.text == ""

    def test_other_domains_are_not_its_business(self, caplog):
        with caplog.at_level("WARNING"):
            pipeline._warn_on_ungrounded_expansion(
                [("openai", "fact_check")], {"openai": {"model": "g"}}
            )
        assert caplog.text == ""

    def test_web_search_true_counts_as_grounded(self, caplog):
        with caplog.at_level("WARNING"):
            pipeline._warn_on_ungrounded_expansion(
                [("openai", "expansion")],
                {"openai": {"model": "g", "web_search": True}},
            )
        assert caplog.text == ""
