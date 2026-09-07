"""Tests for the provider text-watermarking registry.

The registry makes claims about third parties that go stale fast, so most of
what matters here is how it behaves when it does not know something: an
unlisted provider, a typo, an undeclared drafter. Every one of those has to
come back "unknown" rather than "no", because reporting an unverified provider
as clean is the single error this module exists to prevent.
"""

import datetime

import pytest

from ci_core.config_helpers import PackagedConfigError
from ci_core.llm import watermarking as w

TODAY = datetime.date(2026, 9, 5)


class TestRegistryContents:
    def test_registry_loaded_with_a_date(self):
        assert w.PROVIDERS
        assert w.REGISTRY_DATE

    def test_every_status_is_a_string_not_a_yaml_boolean(self):
        # "yes"/"no" unquoted in YAML parse as booleans, which would make every
        # status comparison below silently false.
        for name, entry in w.PROVIDERS.items():
            assert isinstance(entry["status"], str), name

    def test_every_status_is_one_of_the_known_values(self):
        for name, entry in w.PROVIDERS.items():
            assert entry["status"] in w._VALID_STATUSES, name

    def test_a_marking_provider_cites_a_source(self):
        # A "yes" that nobody can trace is not worth reporting to an author.
        for name, entry in w.PROVIDERS.items():
            if entry["status"] == "yes":
                assert entry.get("source"), f"{name} claims yes with no source"


class TestStatusLookup:
    def test_claude_is_recorded_as_marking_text(self):
        assert w.status_for("claude", today=TODAY)["marked"] is True

    def test_a_non_marking_provider_is_not_marked(self):
        assert w.status_for("openai", today=TODAY)["marked"] is False

    def test_partial_counts_as_marked(self):
        # An unresolved API path is a reason to assume the mark is there, not a
        # reason to assume it is not.
        result = w.status_for("gemini", today=TODAY)
        assert result["status"] == "partial"
        assert result["marked"] is True

    def test_lookup_is_case_and_whitespace_insensitive(self):
        assert w.status_for("  CLAUDE  ", today=TODAY)["status"] == "yes"

    def test_an_unlisted_provider_is_unknown_not_no(self):
        result = w.status_for("some-new-model", today=TODAY)
        assert result["status"] == w.UNKNOWN
        assert result["marked"] is False
        assert "not in the watermarking registry" in result["note"]

    def test_an_undeclared_drafter_is_unknown(self):
        result = w.status_for("", today=TODAY)
        assert result["status"] == w.UNKNOWN
        assert result["provider"] is None

    def test_none_is_handled_like_an_empty_string(self):
        assert w.status_for(None, today=TODAY)["status"] == w.UNKNOWN

    def test_every_result_restates_that_it_is_declared_not_measured(self):
        for provider in ("claude", "openai", "nonesuch", ""):
            assert "not measured" in w.status_for(provider, today=TODAY)["basis"]


class TestStaleness:
    def test_a_freshly_dated_registry_is_ok(self):
        assert w.staleness(today=TODAY)[0] == "ok"

    def test_notice_threshold(self):
        later = TODAY + datetime.timedelta(days=w.STALE_NOTICE_DAYS + 1)
        assert w.staleness(today=later)[0] == "notice"

    def test_warning_threshold(self):
        later = TODAY + datetime.timedelta(days=w.STALE_WARNING_DAYS + 1)
        assert w.staleness(today=later)[0] == "warning"

    def test_staleness_travels_with_every_lookup(self):
        # The answer is only as good as the day the table was checked, so the
        # caveat has to ride along rather than being logged once at import.
        later = TODAY + datetime.timedelta(days=w.STALE_WARNING_DAYS + 1)
        result = w.status_for("claude", today=later)
        assert result["registry_staleness"] == "warning"
        assert result["registry_age_days"] > w.STALE_WARNING_DAYS


class TestMalformedRegistry:
    def test_an_unknown_status_value_is_an_error_not_a_downgrade(self, monkeypatch):
        # Under a plain .get() a typo becomes "unknown", which reads as "we
        # checked and could not tell" rather than "this file is broken".
        import ci_core.config_helpers as helpers

        monkeypatch.setattr(
            helpers,
            "load_packaged_yaml",
            lambda *_a, **_k: {"providers": {"claude": {"status": "definately"}}},
        )
        monkeypatch.setattr(w, "load_packaged_yaml", helpers.load_packaged_yaml)
        with pytest.raises(PackagedConfigError, match="definately"):
            w._load_registry()

    def test_a_missing_providers_mapping_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            w, "load_packaged_yaml", lambda *_a, **_k: {"providers": []}
        )
        with pytest.raises(PackagedConfigError, match="must be a mapping"):
            w._load_registry()
