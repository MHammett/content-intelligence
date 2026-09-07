"""Reduced cost is meant to reduce functionality. It is not meant to do it quietly.

Measured 2026-09-05. ``economy`` (configs/presets.yaml) disables grok and claude
and runs ``thoroughness: standard``, whose map pairs mistral with claude in
``argument_integrity`` and with grok in ``red_team``. Disabling those two
therefore costs *two* domains their second model, and perplexity -- which
economy configures as a cheap grounded model -- is assigned nothing by that map
at all. The run came out as five domains on three distinct models, every one of
them single-model, with the cheapest available second opinion idle, and the report said none of it: the only record of the ensemble's
real width was the API call table, which you can only read as width if you
already know ``_THOROUGHNESS_PRESETS`` by heart.

Three things are covered here.

Width reporting
    The report states, for the preset actually used, how many domains ran a
    single model, how many distinct models ran, and what that does to Section 1.
    Consensus is the part whose *meaning* changes: it needs
    ``consensus_min_models`` distinct sources, so where every domain has one
    model no passage can reach it from inside a single domain, and where the
    whole voter pool is below the minimum Section 1 cannot flag anything at all.

Backfill
    A domain left narrower than its own preset entry asked for is topped back up
    from the models still available, ordered by which model is carrying least,
    so the calls the preset already budgeted buy distinct-model coverage.
    Rebalancing the fixed lists instead would fix one arrangement of disabled
    models; this fixes whichever arrangement a run actually has.

The never-assigned domain
    ``_domains_with_nothing_usable`` derived its domains from ``raw_results``,
    which only holds domains that were *attempted*. A domain whose every model
    was excluded produces no result key, so the substitution pass could not see
    the one case where the loss was total. The expected set now comes from the
    preset.
"""

from unittest.mock import patch

from ci_article_review import pipeline
from ci_article_review.consolidation import _ensemble_width
from ci_article_review.report_markdown import (
    _render_ensemble_width,
    render_report_markdown,
)


_ALL_MODELS = ("gemini", "perplexity", "openai", "mistral", "grok", "claude")
_ALL_KEYS = {m: {"api_key": "k"} for m in _ALL_MODELS}

#: configs/presets.yaml ``economy``, verbatim: grok and claude off, four left.
_ECONOMY = {
    "openai": {"model": "gpt-5.6-luna"},
    "gemini": {"model": "gemini-2.5-flash"},
    "mistral": {"model": "mistral-small-latest"},
    "perplexity": {"model": "sonar"},
    "grok": {"enabled": False, "model": "grok-4.3"},
    "claude": {"enabled": False, "model": "claude-haiku-4-5-20251001"},
}

_DOMAINS = [
    "fact_check",
    "voice_style",
    "completeness",
    "argument_integrity",
    "red_team",
]


def _by_domain(pairs):
    out = {}
    for model, domain in pairs:
        out.setdefault(domain, []).append(model)
    return out


class TestBackfillAtEconomy:
    """The measured case: what ``economy`` runs, before and after."""

    def test_the_measured_shape_is_what_it_was(self):
        """Pins the defect, so the fix below is measured against a real number."""
        pairs = pipeline._build_assignments(
            "standard", _ECONOMY, _ALL_KEYS, backfill=False
        )
        by_domain = _by_domain(pairs)

        assert len(pairs) == 5
        assert {m for m, _ in pairs} == {"gemini", "openai", "mistral"}
        assert all(len(v) == 1 for v in by_domain.values())

    def test_backfill_widens_it(self):
        pairs = pipeline._build_assignments("standard", _ECONOMY, _ALL_KEYS)
        by_domain = _by_domain(pairs)

        assert len(pairs) == 7, "the preset budgets 7 calls; all 7 should be spent"
        assert {m for m, _ in pairs} == {
            "gemini",
            "openai",
            "mistral",
            "perplexity",
        }, "economy configures perplexity and the map never used it; it should run"
        single = [d for d, ms in by_domain.items() if len(ms) == 1]
        assert sorted(single) == ["completeness", "fact_check", "voice_style"]

    def test_the_domains_that_lost_a_model_are_the_ones_topped_up(self):
        """argument_integrity lost claude, red_team lost grok. Both regain one."""
        by_domain = _by_domain(
            pipeline._build_assignments("standard", _ECONOMY, _ALL_KEYS)
        )

        assert len(by_domain["argument_integrity"]) == 2
        assert len(by_domain["red_team"]) == 2
        assert "mistral" in by_domain["argument_integrity"]
        assert "mistral" in by_domain["red_team"]

    def test_what_was_added_is_reported(self):
        backfills = []
        pipeline._build_assignments(
            "standard", _ECONOMY, _ALL_KEYS, backfills=backfills
        )

        assert len(backfills) == 2
        joined = " ".join(backfills)
        assert "argument_integrity" in joined and "red_team" in joined
        # Names the absent model it stands in for, so the line explains itself.
        assert "claude" in joined and "grok" in joined


class TestBackfillLimits:
    def test_it_never_exceeds_what_the_preset_asked_for(self):
        """The budget is the preset's own count, not the widest pool available."""
        by_domain = _by_domain(
            pipeline._build_assignments("standard", _ECONOMY, _ALL_KEYS)
        )
        preset = pipeline._THOROUGHNESS_PRESETS["standard"]

        for domain, models in by_domain.items():
            assert len(models) <= len(preset[domain]), domain

    def test_it_stops_at_two_models_even_where_the_preset_wants_three(self):
        """Two is corroboration; a third voter on an already-corroborated
        passage is the lowest-value call in the ensemble. Measured 2026-09-05:
        topping `thorough` up to full width with one key missing cost +30% and
        left exactly as many domains uncorroborated as stopping at two."""
        configs = {m: {} for m in _ALL_MODELS}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "claude"}
        by_domain = _by_domain(pipeline._build_assignments("thorough", configs, keys))

        # thorough asks for three models on argument_integrity and red_team.
        assert len(pipeline._THOROUGHNESS_PRESETS["thorough"]["red_team"]) == 3
        assert len(by_domain["argument_integrity"]) == 2
        assert len(by_domain["red_team"]) == 2
        # But voice_style, left with one, is still brought up to two.
        assert len(by_domain["voice_style"]) == 2

    def test_a_fully_available_ensemble_is_untouched(self):
        """Nothing is short, so nothing is added -- at every preset."""
        for thoroughness in ("standard", "thorough", "maximum"):
            configs = {m: {} for m in _ALL_MODELS}
            backfills = []
            with_bf = pipeline._build_assignments(
                thoroughness, configs, _ALL_KEYS, backfills=backfills
            )
            without = pipeline._build_assignments(
                thoroughness, configs, _ALL_KEYS, backfill=False
            )
            assert with_bf == without, thoroughness
            assert backfills == [], thoroughness

    def test_it_cannot_conjure_a_model_that_has_no_credentials(self):
        pairs = pipeline._build_assignments(
            "standard", _ECONOMY, {"gemini": {"api_key": "k"}}
        )
        assert {m for m, _ in pairs} == {"gemini"}

    def test_it_honours_a_prompts_override(self):
        configs = dict(_ECONOMY, perplexity={"prompts": ["fact_check"]})
        pairs = pipeline._build_assignments("standard", configs, _ALL_KEYS)
        assert {d for m, d in pairs if m == "perplexity"} <= {"fact_check"}

    def test_it_honours_the_drafting_model_exclusion(self):
        pairs = pipeline._build_assignments(
            "standard", _ECONOMY, _ALL_KEYS, "perplexity"
        )
        assert ("perplexity", "voice_style") not in pairs

    def test_disabled_models_stay_disabled(self):
        pairs = pipeline._build_assignments("standard", _ECONOMY, _ALL_KEYS)
        assert not {m for m, _ in pairs} & {"grok", "claude"}


class TestBackfillPrefersDistinctCoverage:
    def test_the_least_loaded_model_is_chosen_first(self):
        """Coverage is the point: a third domain on openai buys less than a
        first domain on perplexity, though both are one more call."""
        by_domain = _by_domain(
            pipeline._build_assignments("standard", _ECONOMY, _ALL_KEYS)
        )

        # openai already carries voice_style and completeness; perplexity none.
        assert "perplexity" in by_domain["argument_integrity"]
        assert "openai" not in by_domain["argument_integrity"]

    def test_fact_check_keeps_to_search_grounded_models(self):
        """Grounding is why gemini is in that ensemble at all."""
        configs = {m: {} for m in _ALL_MODELS}
        by_domain = _by_domain(
            pipeline._build_assignments("thorough", configs, _ALL_KEYS)
        )
        assert set(by_domain["fact_check"]) <= set(pipeline._SEARCH_GROUNDED_MODELS)


class TestExpectedDomainsComeFromThePreset:
    """The gap in the substitution pass: a domain nobody was assigned to."""

    def test_a_never_assigned_domain_is_reported_empty(self):
        """It has no result key at all, so the results alone cannot show it."""
        results = {"gemini:fact_check": {"failed": False, "data": {"a": [1]}}}
        assert pipeline._domains_with_nothing_usable(results) == []
        assert pipeline._domains_with_nothing_usable(results, _DOMAINS) == [
            "argument_integrity",
            "completeness",
            "red_team",
            "voice_style",
        ]

    def test_an_attempted_and_failed_domain_is_still_reported(self):
        results = {"gemini:fact_check": {"failed": True, "error": "stalled"}}
        assert pipeline._domains_with_nothing_usable(results, ["fact_check"]) == [
            "fact_check"
        ]

    def test_a_usable_domain_is_not_reported(self):
        results = {"gemini:fact_check": {"failed": False, "data": {"flags": [1]}}}
        assert pipeline._domains_with_nothing_usable(results, ["fact_check"]) == []

    def test_the_preset_supplies_the_set(self):
        assert pipeline._preset_domains("standard") == _DOMAINS
        assert set(pipeline._preset_domains("maximum")) == set(_DOMAINS)

    def test_an_unknown_thoroughness_falls_back_to_standard(self):
        assert pipeline._preset_domains("nonsense") == _DOMAINS

    def test_omitting_the_set_keeps_the_old_behaviour(self):
        """Callers that pass nothing see exactly what they saw before."""
        results = {"gemini:fact_check": {"failed": True}}
        assert pipeline._domains_with_nothing_usable(results) == ["fact_check"]


class TestSubstitutionReachesANeverAssignedDomain:
    def test_the_documented_edge_case_gets_a_substitute(self):
        """docs/CONFIGURATION.md, "Drafting model": at ``standard`` voice_style
        is a single model, and drafting with that model leaves the domain with
        no reviewer. It produced no result key, so it never reached
        substitution."""
        results = {"gemini:fact_check": {"failed": False, "data": {"flags": [1]}}}

        def _make_runner(model, domain):
            return (
                f"{model}:{domain}",
                lambda: {"failed": False, "data": {"flags": [1]}},
            )

        def _ran(names, runners, *a):
            return {
                n: {"failed": False, "data": {"flags": [1]}, "_model": n.split(":")[0]}
                for n, _ in runners
            }

        with patch.object(pipeline, "_run_reviews_for_names", side_effect=_ran):
            out = pipeline._substitute_for_empty_domains(
                results,
                _make_runner,
                {},
                {m: {} for m in _ALL_MODELS},
                _ALL_KEYS,
                60,
                drafting_model="openai",
                expected_domains=["fact_check", "voice_style"],
            )

        assert any(k.endswith(":voice_style") for k in out), (
            "voice_style was never assigned and never substituted"
        )
        assert "openai:voice_style" not in out, (
            "the drafting model must not be the substitute for its own prose"
        )


class TestWidthMetrics:
    def _results(self, pairs, failed=()):
        return {
            p: {"failed": p in failed, "data": {"flags": [{"passage": "x"}]}}
            for p in pairs
        }

    def test_it_counts_distinct_models_and_single_model_domains(self):
        width = _ensemble_width(
            self._results(
                [
                    ("gemini", "fact_check"),
                    ("openai", "voice_style"),
                    ("openai", "completeness"),
                    ("mistral", "argument_integrity"),
                    ("mistral", "red_team"),
                ]
            ),
            {"preset_domains": _DOMAINS},
        )
        assert width["distinct_models"] == ["gemini", "mistral", "openai"]
        assert len(width["domains_single_model"]) == 5

    def test_a_failed_pass_is_not_a_voter(self):
        width = _ensemble_width(
            self._results(
                [("gemini", "fact_check"), ("mistral", "red_team")],
                failed={("gemini", "fact_check")},
            ),
            {"preset_domains": ["fact_check", "red_team"]},
        )
        assert width["distinct_models"] == ["mistral"]
        assert width["models_by_domain"]["fact_check"] == []

    def test_a_domain_nobody_reviewed_still_appears_in_the_table(self):
        """Naming it is `domains_not_run`'s job — that block carries the reason
        and puts a note above the empty section itself. The width table still
        has to list the domain, or a reader counting coverage cannot tell it was
        expected at all."""
        width = _ensemble_width(
            self._results([("gemini", "fact_check")]),
            {"preset_domains": ["fact_check", "voice_style"]},
        )
        assert width["models_by_domain"]["voice_style"] == []
        assert "voice_style" in width["domains_expected"]

    def test_consensus_is_unreachable_below_the_minimum(self):
        width = _ensemble_width(
            self._results([("gemini", "fact_check")]),
            {"preset_domains": ["fact_check"], "consensus_min_models": 2},
        )
        assert width["voter_pool"] == 1
        assert width["consensus_reachable"] is False

    def test_languagetool_counts_toward_the_pool(self):
        """It is a genuinely independent source, and _find_consensus counts it."""
        width = _ensemble_width(
            self._results([("gemini", "fact_check")]),
            {"preset_domains": ["fact_check"], "consensus_min_models": 2},
            lt_voted=True,
        )
        assert width["voter_pool"] == 2
        assert width["consensus_reachable"] is True

    def test_all_single_model_domains_need_cross_domain_agreement(self):
        width = _ensemble_width(
            self._results([("gemini", "fact_check"), ("openai", "voice_style")]),
            {"preset_domains": ["fact_check", "voice_style"]},
        )
        assert width["consensus_needs_cross_domain"] is True

    def test_a_two_model_domain_clears_the_cross_domain_caveat(self):
        width = _ensemble_width(
            self._results(
                [
                    ("gemini", "fact_check"),
                    ("mistral", "red_team"),
                    ("grok", "red_team"),
                ]
            ),
            {"preset_domains": ["fact_check", "red_team"]},
        )
        assert width["consensus_needs_cross_domain"] is False


class TestWidthIsRendered:
    def _report(self, **width):
        base = {
            "models_by_domain": {"fact_check": ["gemini"], "red_team": ["mistral"]},
            "distinct_models": ["gemini", "mistral"],
            "domains_single_model": ["fact_check", "red_team"],
            "domains_expected": ["fact_check", "red_team"],
            "consensus_min_models": 2,
            "voter_pool": 2,
            "consensus_reachable": True,
            "consensus_needs_cross_domain": True,
            "backfilled": [],
        }
        base.update(width)
        return {
            "ensemble": {
                "cost_preset": "economy",
                "thoroughness": "standard",
                "width": base,
            }
        }

    def test_the_three_numbers_are_stated(self):
        out = "\n".join(_render_ensemble_width(self._report()))
        assert "economy" in out
        assert "Distinct models that ran:** 2" in out
        assert "Domains run by a single model:** 2 of 2" in out

    def test_every_domain_is_listed_with_its_models(self):
        """So width never has to be reverse-engineered from the call table."""
        out = "\n".join(_render_ensemble_width(self._report()))
        assert "| fact_check | gemini |" in out
        assert "| red_team | mistral |" in out

    def test_the_cross_domain_caveat_is_explained(self):
        out = "\n".join(_render_ensemble_width(self._report()))
        assert "only across domains" in out
        assert "2 distinct sources" in out

    def test_an_unreachable_consensus_is_stated_loudly(self):
        out = "\n".join(
            _render_ensemble_width(
                self._report(consensus_reachable=False, voter_pool=1)
            )
        )
        assert "SECTION 1 CANNOT FLAG ANYTHING" in out
        assert "not that the models found nothing" in out

    def test_fact_check_names_what_its_loss_costs(self):
        out = "\n".join(_render_ensemble_width(self._report()))
        assert "Section 2 and Section 9" in out

    def test_it_does_not_narrate_an_unreviewed_domain(self):
        """`_render_domains_not_run` and `_not_run_note` own that ground — with
        a reason, and with a note directly above the empty section. Width lists
        the domain in its table and says nothing further, so one fact does not
        get two voices that can drift apart."""
        out = "\n".join(
            _render_ensemble_width(self._report(models_by_domain={"voice_style": []}))
        )
        assert "| voice_style |" in out
        assert "no reviewer at all" not in out
        assert "not because the draft is clean" not in out

    def test_backfills_are_disclosed(self):
        out = "\n".join(
            _render_ensemble_width(
                self._report(backfilled=["perplexity added to argument_integrity"])
            )
        )
        assert "Backfilled for corroboration" in out
        assert "perplexity added to argument_integrity" in out

    def test_a_report_without_a_width_block_renders_unchanged(self):
        """Reports written before this section must not gain an empty heading."""
        assert _render_ensemble_width({}) == []
        assert _render_ensemble_width({"ensemble": {}}) == []
        assert _render_ensemble_width({"ensemble": {"width": {}}}) == []

    def test_it_reaches_the_full_report(self):
        report = dict(self._report(), generated="now", run_number=1)
        out = render_report_markdown(report)
        assert "## Ensemble Width" in out
        # Header context, above the sections it changes how to read.
        assert out.index("## Ensemble Width") < out.index("## SECTION 1")


class TestBackfillIsOffUnderCalibrationFilters:
    """`--only-model gemini` exists to price one cell, not to widen the ensemble.

    Backfill runs inside `_build_assignments`, before the calibration filters
    are applied, so without this the filter keeps every backfilled row that
    happens to name the filtered model: the e2e fixture's `--only-model gemini`
    would buy gemini:argument_integrity and gemini:red_team on top of the one
    call the flag asked for. Same reasoning that scopes the substitution pass
    and suppresses the SEO calls on a calibration run.
    """

    def test_only_model_buys_exactly_the_cell_it_asked_for(self, tmp_path):
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        attempted = []

        def _stub(model_name, domain, *a, **kw):
            attempted.append(f"{model_name}:{domain}")
            return _fake_run_domain(model_name, domain, *a, **kw)

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch("ci_article_review.pipeline._run_domain", side_effect=_stub)
            ],
            only_model="gemini",
        ):
            pass

        assert attempted == ["gemini:fact_check"], (
            f"a calibration run bought more than the cell it asked for: {attempted}"
        )

    def test_an_unfiltered_run_still_backfills(self, tmp_path):
        """The suppression is scoped to calibration, not a quiet global off."""
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        attempted = []

        def _stub(model_name, domain, *a, **kw):
            attempted.append(f"{model_name}:{domain}")
            return _fake_run_domain(model_name, domain, *a, **kw)

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch("ci_article_review.pipeline._run_domain", side_effect=_stub)
            ],
        ):
            pass

        # The fixture credentials gemini, openai and mistral at `standard`, so
        # argument_integrity (claude) and red_team (grok) are each short one.
        assert len(attempted) == 7, attempted
        assert "gemini:argument_integrity" in attempted
        assert "gemini:red_team" in attempted
