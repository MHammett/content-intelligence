"""Tests for the assignment builders — what they schedule, and what they drop.

``_build_assignments`` covers the built-in domains a thoroughness preset asks
for; ``_build_custom_assignments`` covers the ones a publication defines in
``custom_domains``. Both had the same two defects, and both are tested here so
the pair cannot drift.

Why this file exists
--------------------
A 2026-08-12 maximum-thoroughness run made 25 model calls where the preset asks
for 30 (6 models x 5 domains). Nothing in the run output said why. The cause was
``enabled: false`` on the claude entry in user.yaml — working as designed, but
finding that took two days and a direct call to ``_build_assignments``, because
the run logs the assignments it made and stays silent about the model it
dropped. The skip-reporting tests below pin the output that closes that gap.

The normalisation tests cover the other half of that debugging session: called
directly with the simple config form that user.example.yaml documents
(``openai: gpt-5.5``), the function used to raise ``AttributeError: 'str' object
has no attribute 'get'``. The real pipeline normalises via config_loader first,
so production never hit it — the contract just did not say so.

``_build_custom_assignments`` carried both defects unchanged: it warned about
unknown model names and unresolvable prompts but dropped disabled and
uncredentialled models in silence, and it read every ``model_configs`` value as
a dict. A custom domain configured for three models could come back with one and
say nothing about the other two.
"""

import logging
import re

from contextlib import ExitStack
from unittest.mock import patch

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review.pipeline import _build_assignments, _build_custom_assignments


_ALL_MODELS = ["gemini", "openai", "mistral", "grok", "claude", "perplexity"]

#: Every model credentialled — so a test's skips come from what it configures.
_ALL_KEYS = {m: {"api_key": "k"} for m in _ALL_MODELS}

#: Simple form, as user.example.yaml ships it.
_SIMPLE_CONFIGS = {
    "openai": "gpt-5.4",
    "gemini": "gemini-2.5-flash",
    "mistral": "mistral-large-latest",
    "perplexity": "sonar-reasoning-pro",
    "grok": "grok-4.3",
    "claude": "claude-opus-4-8",
}


def _skip_for(skips, model_name):
    """Return the single skip line naming ``model_name``, asserting there is one."""
    matches = [s for s in skips if s.startswith(f"{model_name} ")]
    assert len(matches) == 1, (
        f"Expected exactly one skip line for {model_name!r}, got {matches!r}"
    )
    return matches[0]


#: The domains every run gets. expansion is excluded — it is opt-in behind
#: --expand, so it is absent from a default ensemble by design rather than by
#: some model being unavailable.
_DEFAULT_DOMAINS = [d for d in pipeline._DOMAIN_PROMPTS if d != "expansion"]


def _pub(**domains):
    """A publication config carrying nothing but the custom domains given."""
    return {"custom_domains": domains}


def _domain(*models, prompt="Check it against the house style."):
    """One custom_domains entry in its simplest complete form."""
    return {"prompt": prompt, "models": list(models)}


class TestSkipReporting:
    """Every model the preset asks for but does not run explains itself."""

    def test_disabled_model_is_reported_with_its_reason(self):
        """The 2026-08-12 run, reproduced: 25 calls instead of 30, and why."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        skips = []
        assignments = _build_assignments("maximum", configs, _ALL_KEYS, skips=skips)

        assert len(assignments) == 25
        assert not any(m == "claude" for m, _ in assignments)

        line = _skip_for(skips, "claude")
        assert "disabled" in line
        assert "enabled: false" in line
        # The five domains it would have run are named, not just counted.
        # expansion is not among them: it is opt-in, so a run that did not ask
        # for it has not "skipped" it.
        for domain in _DEFAULT_DOMAINS:
            assert domain in line, f"{domain!r} missing from skip line: {line}"
        assert "expansion" not in line

    def test_expansion_joins_the_skip_line_when_it_was_asked_for(self):
        """--expand makes it a domain the run wanted, so its absence counts."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        skips = []
        _build_assignments(
            "maximum", configs, _ALL_KEYS, skips=skips, include_expansion=True
        )

        line = _skip_for(skips, "claude")
        for domain in pipeline._DOMAIN_PROMPTS:
            assert domain in line, f"{domain!r} missing from skip line: {line}"

    def test_missing_credentials_are_reported_as_such(self):
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "grok"}

        skips = []
        assignments = _build_assignments("maximum", _SIMPLE_CONFIGS, keys, skips=skips)

        assert len(assignments) == 25
        line = _skip_for(skips, "grok")
        assert "no credentials" in line
        assert "disabled" not in line

    def test_prompts_override_names_the_domains_it_excluded(self):
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {
            "model": "claude-opus-4-8",
            "prompts": ["fact_check", "completeness"],
        }

        skips = []
        assignments = _build_assignments("maximum", configs, _ALL_KEYS, skips=skips)

        claude_domains = {d for m, d in assignments if m == "claude"}
        assert claude_domains == {"fact_check", "completeness"}

        line = _skip_for(skips, "claude")
        assert "prompts" in line
        # The three preset domains the override dropped, named.
        for domain in ("voice_style", "argument_integrity", "red_team"):
            assert domain in line, f"{domain!r} missing from skip line: {line}"
        # ...and not the two it kept, which appear only in the parenthetical
        # naming what the override limited it to.
        assert "not run: " in line
        not_run = line.split("not run: ", 1)[1].split(" (", 1)[0]
        assert "fact_check" not in not_run
        assert "completeness" not in not_run

    def test_one_line_per_skipped_model_not_per_domain(self):
        """A model dropped from five domains gets one line, not five."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        configs["grok"] = {"enabled": False, "model": "grok-4.3"}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "perplexity"}

        skips = []
        _build_assignments("maximum", configs, keys, skips=skips)

        assert len(skips) == 3
        assert {s.split(" ", 1)[0] for s in skips} == {"claude", "grok", "perplexity"}

    def test_nothing_is_reported_when_the_full_preset_runs(self):
        """No false alarms: a complete ensemble reports no skips at all."""
        skips = []
        assignments = _build_assignments(
            "maximum", _SIMPLE_CONFIGS, _ALL_KEYS, skips=skips
        )

        assert len(assignments) == 30
        assert skips == []

    def test_a_disabled_model_without_credentials_reports_one_reason(self):
        """Both gates fail; the report stays one line and names the first."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "claude"}

        skips = []
        _build_assignments("maximum", configs, keys, skips=skips)

        line = _skip_for(skips, "claude")
        assert "disabled" in line
        assert "no credentials" not in line

    def test_the_skipped_domains_reconcile_with_the_preset(self):
        """Assignments plus reported skips account for every preset slot.

        All four drop reasons at once, so the arithmetic has to hold across a
        model-level block, an override, and the drafter rule together.
        """
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        configs["mistral"] = {
            "model": "mistral-large-latest",
            "prompts": ["red_team"],
        }
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "grok"}

        skips = []
        assignments = _build_assignments(
            "maximum", configs, keys, "openai", skips=skips
        )

        preset_slots = sum(
            len(models)
            for domain, models in pipeline._THOROUGHNESS_PRESETS["maximum"].items()
            # The expansion slots are not in play: this run did not pass
            # include_expansion, so they were never asked for and nothing
            # reports skipping them.
            if domain != "expansion"
        )
        # Each line states its own count; that count is what has to reconcile.
        reported_skips = sum(
            int(re.search(r"(\d+) domain\(s\) not run", s).group(1)) for s in skips
        )
        assert len(assignments) + reported_skips == preset_slots

    def test_a_model_outside_the_preset_reports_its_own_prompts_entry(self):
        """standard has no perplexity slot; an explicit prompts: entry adds one."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["perplexity"] = {
            "model": "sonar-reasoning-pro",
            "prompts": ["fact_check"],
        }
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "perplexity"}

        skips = []
        assignments = _build_assignments("standard", configs, keys, skips=skips)

        assert ("perplexity", "fact_check") not in assignments
        line = _skip_for(skips, "perplexity")
        assert "no credentials" in line
        assert "fact_check" in line

    def test_an_unreported_model_is_one_the_preset_never_wanted(self):
        """standard asks for no perplexity, so a keyless perplexity is not a skip."""
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "perplexity"}

        skips = []
        _build_assignments("standard", _SIMPLE_CONFIGS, keys, skips=skips)

        assert skips == []

    def test_the_drafting_model_exclusion_is_reported_too(self):
        """The quietest drop of the four: nothing in the config asks for it."""
        skips = []
        assignments = _build_assignments(
            "maximum", _SIMPLE_CONFIGS, _ALL_KEYS, "claude", skips=skips
        )

        assert ("claude", "voice_style") not in assignments
        assert len(assignments) == 29

        line = _skip_for(skips, "claude")
        assert "voice_style" in line
        assert "drafted this article" in line
        # The four domains it still reviews are not in the not-run list.
        for domain in ("fact_check", "completeness", "argument_integrity", "red_team"):
            assert domain not in line.split("not run: ", 1)[1]

    def test_a_model_dropped_two_ways_still_gets_one_line(self):
        """A prompts: override and the drafter rule, attributed separately."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {
            "model": "claude-opus-4-8",
            "prompts": ["fact_check", "voice_style"],
        }

        skips = []
        _build_assignments("maximum", configs, _ALL_KEYS, "claude", skips=skips)

        line = _skip_for(skips, "claude")
        # voice_style survived the override, then the drafter rule took it.
        assert "drafted this article" in line
        assert "prompts: override" in line
        assert "4 domain(s) not run" in line

    def test_the_drafter_rule_is_not_reported_for_other_models(self):
        """openai drafted nothing here, so its voice_style pass is not a skip."""
        skips = []
        _build_assignments("maximum", _SIMPLE_CONFIGS, _ALL_KEYS, "claude", skips=skips)

        assert {s.split(" ", 1)[0] for s in skips} == {"claude"}

    def test_skips_argument_is_optional(self):
        """Existing callers that pass neither skips nor a drafter keep working."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        assert len(_build_assignments("maximum", configs, _ALL_KEYS)) == 25

    def test_reporting_does_not_change_what_is_assigned(self):
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"model": "claude-opus-4-8", "prompts": ["fact_check"]}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "grok"}

        with_skips = _build_assignments("thorough", configs, keys, skips=[])
        without_skips = _build_assignments("thorough", configs, keys)
        assert with_skips == without_skips


class TestConfigFormNormalisation:
    """Both config forms user.example.yaml documents reach here intact."""

    def test_simple_string_form_does_not_raise(self):
        """Was: AttributeError: 'str' object has no attribute 'get'."""
        assignments = _build_assignments("maximum", _SIMPLE_CONFIGS, _ALL_KEYS)
        assert len(assignments) == 30

    def test_both_forms_agree(self):
        extended = {name: {"model": model} for name, model in _SIMPLE_CONFIGS.items()}
        assert _build_assignments("maximum", _SIMPLE_CONFIGS, _ALL_KEYS) == (
            _build_assignments("maximum", extended, _ALL_KEYS)
        )

    def test_forms_can_be_mixed(self):
        """user.example.yaml: 'Simple-form entries are unaffected — you can mix.'"""
        mixed = dict(_SIMPLE_CONFIGS)
        mixed["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        mixed["grok"] = {"model": "grok-4.3", "prompts": ["red_team"]}

        skips = []
        assignments = _build_assignments("maximum", mixed, _ALL_KEYS, skips=skips)

        assert {d for m, d in assignments if m == "grok"} == {"red_team"}
        assert not any(m == "claude" for m, _ in assignments)
        assert {s.split(" ", 1)[0] for s in skips} == {"claude", "grok"}

    def test_string_form_still_honours_a_missing_key(self):
        """Normalisation must not invent credentials."""
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "mistral"}
        assignments = _build_assignments("maximum", _SIMPLE_CONFIGS, keys)
        assert not any(m == "mistral" for m, _ in assignments)

    def test_string_form_gemini_is_not_mistaken_for_vertex(self):
        """Normalisation fills in provider; the api-key path must still apply.

        _model_has_credentials treats provider: vertex_ai as needing a project
        rather than a key, so the default provider filled in here matters.
        """
        assignments = _build_assignments(
            "maximum", {"gemini": "gemini-2.5-flash"}, {"gemini": {"api_key": "k"}}
        )
        assert {d for m, d in assignments if m == "gemini"} == set(_DEFAULT_DOMAINS)

    def test_empty_prompts_override_runs_nothing_rather_than_raising(self):
        """`prompts:` with no value parses as None; it used to raise TypeError."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"model": "claude-opus-4-8", "prompts": None}

        skips = []
        assignments = _build_assignments("maximum", configs, _ALL_KEYS, skips=skips)

        assert not any(m == "claude" for m, _ in assignments)
        assert "prompts" in _skip_for(skips, "claude")


#: Enough of a currency report and a handoff to get run_draft_pipeline as far
#: as the assignment block, which is all the wiring tests below need.
_CURRENCY = {
    "warnings": [],
    "notices": [],
    "registry_warning": False,
    "registry_stale": False,
    "registry_date": "",
    "registry_age_days": 0,
}
_HANDOFF = {
    "title": "A Title That Is Comfortably Long Enough",
    "draft": "# A Title That Is Comfortably Long Enough\n\n## Section\n\nBody.",
    "primary_claim": "The claim.",
    "run_number": 1,
}


class TestSkipsReachTheRunOutput:
    """The reasons are only useful if the run actually prints them."""

    def test_empty_ensemble_logs_why_before_it_exits(self, caplog):
        """The run that produces nothing is the one that most needs the reasons.

        _build_assignments is deliberately left unpatched here — this covers the
        wiring from config to log line, which patching it out would skip.
        """
        caplog.set_level(logging.INFO, logger="pipeline")

        config = {
            "api_keys": {"mistral": {"api_key": "k"}},
            "pipeline": {"link_validation": False, "grammar_pass": False},
            "publication": {},
            "delta": {},
            "ensemble": {},
            # Credentialled but switched off; every other model has no key.
            "models": {"mistral": {"enabled": False, "model": "mistral-large-latest"}},
        }

        with ExitStack() as stack:
            for target, kwargs in (
                ("load_user_config", {"return_value": {"pipeline": {}}}),
                ("load_publication_config", {"return_value": {}}),
                ("merge_configs", {"return_value": config}),
                ("check_model_currency", {"return_value": _CURRENCY}),
                ("_build_custom_assignments", {"return_value": ([], {})}),
            ):
                stack.enter_context(
                    patch(f"ci_article_review.pipeline.{target}", **kwargs)
                )
            stack.enter_context(
                patch(
                    "ci_article_review.pipeline.seo_suggest.generate",
                    return_value=({"status": "skipped", "reason": "test"}, None),
                )
            )
            stack.enter_context(
                patch(
                    "ci_article_review.pipeline.seo_content.review",
                    return_value=({"status": "skipped", "reason": "test"}, None),
                )
            )
            with pytest.raises(SystemExit):
                pipeline.run_draft_pipeline(None, "myblog", handoff=dict(_HANDOFF))

        skip_lines = [
            r.getMessage() for r in caplog.records if "Skipped:" in r.getMessage()
        ]
        # standard preset: mistral disabled, the other four have no credentials.
        assert len(skip_lines) == 5
        assert all(
            r.levelno == logging.INFO
            for r in caplog.records
            if "Skipped:" in r.getMessage()
        )

        mistral_line = next(s for s in skip_lines if "mistral" in s)
        assert "disabled" in mistral_line
        assert "no credentials" in next(s for s in skip_lines if "claude" in s)


class TestCustomDomainSkipReporting:
    """A publication's custom domain accounts for the models it did not get."""

    def test_three_models_configured_one_runs_and_the_other_two_say_why(self):
        """The defect stated plainly: 3 models in, 1 out, and silence."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "grok"}

        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("openai", "claude", "grok")), configs, keys, skips
        )

        assert assignments == [("openai", "house_style")]
        assert len(skips) == 2
        assert "disabled" in _skip_for(skips, "claude")
        assert "no credentials" in _skip_for(skips, "grok")

    def test_a_disabled_model_is_reported_with_its_reason(self):
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        skips = []
        assignments, prompts = _build_custom_assignments(
            _pub(house_style=_domain("gemini", "claude")), configs, _ALL_KEYS, skips
        )

        assert assignments == [("gemini", "house_style")]
        assert prompts == {"house_style": "Check it against the house style."}

        line = _skip_for(skips, "claude")
        assert "disabled" in line
        assert "enabled: false" in line
        assert "house_style" in line

    def test_missing_credentials_are_reported_as_such(self):
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "grok"}

        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(seo_angle=_domain("grok", "openai")), _SIMPLE_CONFIGS, keys, skips
        )

        assert assignments == [("openai", "seo_angle")]
        line = _skip_for(skips, "grok")
        assert "no credentials" in line
        assert "disabled" not in line

    def test_a_disabled_model_without_credentials_reports_one_reason(self):
        """Both gates fail; the report stays one line and names the first."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "claude"}

        skips = []
        _build_custom_assignments(
            _pub(house_style=_domain("claude")), configs, keys, skips
        )

        line = _skip_for(skips, "claude")
        assert "disabled" in line
        assert "no credentials" not in line

    def test_one_line_per_model_not_per_domain(self):
        """A model dropped from three custom domains gets one line, not three."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        skips = []
        _build_custom_assignments(
            _pub(
                house_style=_domain("gemini", "claude"),
                seo_angle=_domain("claude"),
                legal_risk=_domain("claude", "openai"),
            ),
            configs,
            _ALL_KEYS,
            skips,
        )

        line = _skip_for(skips, "claude")
        assert "3 custom domain(s) not run" in line
        for domain in ("house_style", "seo_angle", "legal_risk"):
            assert domain in line, f"{domain!r} missing from skip line: {line}"

    def test_a_domain_that_loses_every_model_still_explains_itself(self):
        """No assignment means no 'Custom:' line to hang the reason off."""
        skips = []
        assignments, prompts = _build_custom_assignments(
            _pub(house_style=_domain("openai", "claude")), _SIMPLE_CONFIGS, {}, skips
        )

        assert assignments == []
        # The prompt still resolved — it is the models that went missing.
        assert "house_style" in prompts
        assert {s.split(" ", 1)[0] for s in skips} == {"openai", "claude"}

    def test_nothing_is_reported_when_every_named_model_runs(self):
        """No false alarms: a fully staffed custom domain reports no skips."""
        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("gemini", "openai", "claude")),
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            skips,
        )

        assert len(assignments) == 3
        assert skips == []

    def test_a_model_named_twice_in_one_domain_counts_once(self):
        """The assignment list dedupes; the skip report has to agree."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        skips = []
        _build_custom_assignments(
            _pub(house_style=_domain("claude", "claude")), configs, _ALL_KEYS, skips
        )

        assert "1 custom domain(s) not run: house_style" in _skip_for(skips, "claude")

    def test_unknown_model_names_warn_rather_than_appearing_as_skips(self, caplog):
        """That gate already had a log line; it must not now report twice."""
        caplog.set_level(logging.WARNING, logger="pipeline")

        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("gpt9000", "openai")),
            _SIMPLE_CONFIGS,
            _ALL_KEYS,
            skips,
        )

        assert assignments == [("openai", "house_style")]
        assert skips == []
        assert any("unknown model" in r.getMessage() for r in caplog.records)

    def test_a_domain_with_no_prompt_reports_no_model_skips(self, caplog):
        """The domain never ran at all; its warning is the whole story."""
        caplog.set_level(logging.WARNING, logger="pipeline")

        skips = []
        assignments, prompts = _build_custom_assignments(
            {"custom_domains": {"house_style": {"models": ["openai", "claude"]}}},
            _SIMPLE_CONFIGS,
            {},
            skips,
        )

        assert (assignments, prompts) == ([], {})
        assert skips == []
        assert any("no prompt" in r.getMessage() for r in caplog.records)

    def test_skips_argument_is_optional(self):
        """Existing three-argument callers keep working unchanged."""
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("gemini", "claude")), configs, _ALL_KEYS
        )
        assert assignments == [("gemini", "house_style")]

    def test_reporting_does_not_change_what_is_assigned(self):
        configs = dict(_SIMPLE_CONFIGS)
        configs["claude"] = {"enabled": False, "model": "claude-opus-4-8"}
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "grok"}
        pub = _pub(house_style=_domain("gemini", "claude", "grok", "openai"))

        assert _build_custom_assignments(pub, configs, keys, []) == (
            _build_custom_assignments(pub, configs, keys)
        )


class TestCustomDomainConfigForms:
    """Both config forms user.example.yaml documents reach here intact."""

    def test_simple_string_form_does_not_raise(self):
        """Was: AttributeError: 'str' object has no attribute 'get'."""
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("openai", "gemini")), _SIMPLE_CONFIGS, _ALL_KEYS
        )
        assert assignments == [("openai", "house_style"), ("gemini", "house_style")]

    def test_both_forms_agree(self):
        extended = {name: {"model": model} for name, model in _SIMPLE_CONFIGS.items()}
        pub = _pub(house_style=_domain("openai", "claude", "mistral"))
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "mistral"}

        simple_skips, extended_skips = [], []
        assert _build_custom_assignments(
            pub, _SIMPLE_CONFIGS, keys, simple_skips
        ) == _build_custom_assignments(pub, extended, keys, extended_skips)
        assert simple_skips == extended_skips

    def test_forms_can_be_mixed(self):
        """user.example.yaml: 'Simple-form entries are unaffected — you can mix.'"""
        mixed = dict(_SIMPLE_CONFIGS)
        mixed["claude"] = {"enabled": False, "model": "claude-opus-4-8"}

        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("openai", "claude")), mixed, _ALL_KEYS, skips
        )

        assert assignments == [("openai", "house_style")]
        assert {s.split(" ", 1)[0] for s in skips} == {"claude"}

    def test_string_form_still_honours_a_missing_key(self):
        """Normalisation must not invent credentials."""
        keys = {m: v for m, v in _ALL_KEYS.items() if m != "mistral"}

        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("mistral")), _SIMPLE_CONFIGS, keys, skips
        )

        assert assignments == []
        assert "no credentials" in _skip_for(skips, "mistral")

    def test_string_form_gemini_is_not_mistaken_for_vertex(self):
        """Normalisation fills in provider; the api-key path must still apply.

        _model_has_credentials treats provider: vertex_ai as needing a project
        rather than a key, so the default provider filled in here matters.
        """
        skips = []
        assignments, _ = _build_custom_assignments(
            _pub(house_style=_domain("gemini")),
            {"gemini": "gemini-2.5-flash"},
            {"gemini": {"api_key": "k"}},
            skips,
        )

        assert assignments == [("gemini", "house_style")]
        assert skips == []


class TestCustomSkipsReachTheRunOutput:
    """Same wiring check as the built-in domains, for the custom ones."""

    def test_custom_domain_skips_are_logged(self, caplog):
        """_build_custom_assignments is left unpatched: config to log line."""
        caplog.set_level(logging.INFO, logger="pipeline")

        config = {
            "api_keys": {"mistral": {"api_key": "k"}},
            "pipeline": {"link_validation": False, "grammar_pass": False},
            # The custom domain asks for two models; neither can run.
            "publication": {
                "custom_domains": {
                    "house_style": {"prompt": "p", "models": ["mistral", "claude"]}
                }
            },
            "delta": {},
            "ensemble": {},
            "models": {"mistral": {"enabled": False, "model": "mistral-large-latest"}},
        }

        with ExitStack() as stack:
            for target, kwargs in (
                ("load_user_config", {"return_value": {"pipeline": {}}}),
                ("load_publication_config", {"return_value": {}}),
                ("merge_configs", {"return_value": config}),
                ("check_model_currency", {"return_value": _CURRENCY}),
            ):
                stack.enter_context(
                    patch(f"ci_article_review.pipeline.{target}", **kwargs)
                )
            for target in ("seo_suggest.generate", "seo_content.review"):
                stack.enter_context(
                    patch(
                        f"ci_article_review.pipeline.{target}",
                        return_value=({"status": "skipped", "reason": "test"}, None),
                    )
                )
            with pytest.raises(SystemExit):
                pipeline.run_draft_pipeline(None, "myblog", handoff=dict(_HANDOFF))

        custom_lines = [
            r.getMessage()
            for r in caplog.records
            if "Custom skipped:" in r.getMessage() and r.levelno == logging.INFO
        ]
        assert len(custom_lines) == 2
        assert "disabled" in next(s for s in custom_lines if "mistral" in s)
        assert "no credentials" in next(s for s in custom_lines if "claude" in s)
        assert all("house_style" in s for s in custom_lines)
