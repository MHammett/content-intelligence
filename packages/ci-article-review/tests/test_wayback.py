"""Tests for adapters.citation.wayback — the citation-review-specific policy
that stays local (fallback-status scoping, human-readable summaries), on top
of the shared archive.org engine now tested in the spn-client package."""

import logging

import requests
from unittest.mock import MagicMock

from ci_article_review.adapters.citation import wayback


class TestFallbackScoping:
    """Which failures an archive snapshot may stand in for — the policy both
    analysis/links.py and the citation resolver read from."""

    def test_origin_refusals_qualify(self):
        assert wayback.fallback_reason_for_status(401) == "auth_required"
        assert wayback.fallback_reason_for_status(403) == "blocked"
        assert wayback.fallback_reason_for_status(429) == "rate_limited"

    def test_gone_and_origin_errors_do_not_qualify(self):
        # 404/410: the resource is genuinely gone and that must surface.
        # 5xx: the origin's own failure, not a refusal aimed at us.
        for status in (200, 404, 410, 500, 502, 503, None):
            assert wayback.fallback_reason_for_status(status) is None

    def test_unreachable_origins_qualify(self):
        assert (
            wayback.fallback_reason_for_exception(requests.exceptions.Timeout())
            == "timeout"
        )
        assert (
            wayback.fallback_reason_for_exception(requests.exceptions.ReadTimeout())
            == "timeout"
        )
        # ConnectTimeout subclasses both Timeout and ConnectionError; the more
        # specific "timeout" wins.
        assert (
            wayback.fallback_reason_for_exception(requests.exceptions.ConnectTimeout())
            == "timeout"
        )
        assert (
            wayback.fallback_reason_for_exception(
                requests.exceptions.ConnectionError("NameResolutionError")
            )
            == "unreachable"
        )

    def test_http_error_dispatches_on_status(self):
        resp = MagicMock(status_code=403)
        exc = requests.exceptions.HTTPError("403", response=resp)
        assert wayback.fallback_reason_for_exception(exc) == "blocked"

        resp404 = MagicMock(status_code=404)
        gone = requests.exceptions.HTTPError("404", response=resp404)
        assert wayback.fallback_reason_for_exception(gone) is None

    def test_http_error_without_response_does_not_qualify(self):
        exc = requests.exceptions.HTTPError("no response attached")
        assert wayback.fallback_reason_for_exception(exc) is None

    def test_unrelated_exception_does_not_qualify(self):
        assert wayback.fallback_reason_for_exception(ValueError("nope")) is None

    def test_every_reason_has_a_label(self):
        reasons = set(wayback._FALLBACK_STATUSES.values()) | {"timeout", "unreachable"}
        assert reasons <= set(wayback.FALLBACK_REASON_LABELS)


class TestFormatSummary:
    def test_not_archived(self):
        summary = wayback.format_summary({"url": "https://x.com", "archived": False})
        assert "Not archived" in summary

    def test_network_error(self):
        """A null must not read as "not archived" — it means we never asked."""
        summary = wayback.format_summary(
            {"url": "https://x.com", "archived": None, "error": "timeout"}
        )
        assert "NOT CHECKED" in summary
        assert "timeout" in summary
        assert "says nothing about whether the page is archived" in summary
        assert "Not archived" not in summary

    def test_archived(self):
        wb = {
            "archived": True,
            "snapshot_age_days": 30,
            "snapshot_stale": False,
            "snapshot_url": "https://web.archive.org/...",
        }
        summary = wayback.format_summary(wb)
        assert "30d" in summary
        assert "[STALE]" not in summary


class TestReExports:
    """The engine functions/constants this module re-exports from spn_client
    must keep working under their original names — resolver.py and
    pipeline.py import them from here, not from spn_client directly."""

    def test_engine_functions_are_reexported(self):
        assert wayback.check is not None
        assert wayback.submit is not None
        assert wayback.check_job_status is not None
        assert wayback.capture_capacity is not None
        assert wayback.system_status is not None
        assert wayback.service_health_note is not None
        assert wayback.snapshot_raw_url is not None
        assert wayback.reset_rate_limit_state is not None

    def test_archive_outcome_constants_are_reexported(self):
        assert wayback.ARCHIVE_ARCHIVED == "archived"
        assert wayback.ARCHIVE_SUBMITTED == "submitted"
        assert wayback.ARCHIVE_PENDING == "pending"
        assert wayback.ARCHIVE_CAPTURE_FAILED == "capture_failed"
        assert wayback.ARCHIVE_SUBMIT_FAILED == "submit_failed"
        assert wayback.ARCHIVE_NOT_ATTEMPTED == "not_attempted"

    def test_every_public_spn_client_name_is_reachable(self):
        """The migration left `rate_limited_out` behind and nobody noticed until
        a run needed it. This fails the next time the library grows a name this
        module does not pass on, rather than leaving it to be rediscovered."""
        import spn_client

        public = {n for n in dir(spn_client) if not n.startswith("_") and n != "client"}
        missing = sorted(n for n in public if not hasattr(wayback, n))
        assert missing == [], f"not re-exported from spn_client: {missing}"


class TestCaptureOptions:
    """``submit()``'s optional SPN2 capture arguments, assembled from config."""

    def test_nothing_configured_sends_nothing(self):
        """An omitted key is how spn-client is told "archive.org's default".
        Sending an explicit False for each would put fields in the capture
        request that nobody asked about."""
        assert wayback.capture_options(None, None) == {}
        assert wayback.capture_options({}, {}) == {}

    def test_off_and_empty_values_are_omitted_not_sent(self):
        opts = wayback.capture_options(
            {"capture_screenshot": False, "use_user_agent": ""}, None
        )
        assert opts == {}

    def test_set_values_pass_through(self):
        opts = wayback.capture_options(
            {"js_behavior_timeout": 15, "skip_first_archive": True}, None
        )
        assert opts == {"js_behavior_timeout": 15, "skip_first_archive": True}

    def test_an_unknown_key_is_dropped_with_a_warning(self, caplog):
        """A typo must not fail a review that would otherwise complete — but it
        must not pass silently either, or the run quietly ignores what the
        author configured."""
        with caplog.at_level(logging.WARNING):
            opts = wayback.capture_options({"capture_screenshots": True}, None)
        assert opts == {}
        assert any("capture_screenshots" in r.getMessage() for r in caplog.records)

    def test_a_wrong_type_is_dropped_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            opts = wayback.capture_options({"js_behavior_timeout": "fifteen"}, None)
        assert opts == {}
        assert any("js_behavior_timeout" in r.getMessage() for r in caplog.records)

    def test_a_bool_does_not_satisfy_an_int_option(self):
        """bool subclasses int, so a naive isinstance check would let
        ``js_behavior_timeout: true`` through and send archive.org a 1."""
        assert wayback.capture_options({"js_behavior_timeout": True}, None) == {}

    def test_a_timeout_outside_archive_orgs_range_is_dropped(self, caplog):
        """0-30 is archive.org's bound, not ours. Sending 60 spends a capture
        request to be told no."""
        with caplog.at_level(logging.WARNING):
            assert wayback.capture_options({"js_behavior_timeout": 60}, None) == {}
            assert wayback.capture_options({"js_behavior_timeout": -1}, None) == {}
        assert wayback.capture_options({"js_behavior_timeout": 0}, None) == {}
        assert wayback.capture_options({"js_behavior_timeout": 30}, None) == {
            "js_behavior_timeout": 30
        }

    def test_secret_options_come_from_credentials_not_settings(self):
        """A password belongs in the channel the rest of this repo's secrets use,
        not in a settings file that gets copied between checkouts."""
        from_settings = wayback.capture_options(
            {"target_password": "hunter2", "capture_cookie": "session=abc"}, None
        )
        assert from_settings == {}

        from_creds = wayback.capture_options(
            None,
            {
                "access_key": "AK",
                "secret_key": "SK",
                "target_username": "u",
                "target_password": "hunter2",
                "capture_cookie": "session=abc",
            },
        )
        assert from_creds == {
            "target_username": "u",
            "target_password": "hunter2",
            "capture_cookie": "session=abc",
        }
        # The S3 keys are passed separately by the caller and must not be
        # duplicated into the capture options.
        assert "access_key" not in from_creds
        assert "secret_key" not in from_creds

    def test_every_documented_option_is_accepted(self):
        """Guards against the list here drifting from spn-client's signature."""
        import inspect

        import spn_client

        params = set(inspect.signature(spn_client.submit).parameters)
        known = set(wayback.CAPTURE_SETTING_OPTIONS) | set(
            wayback.CAPTURE_SECRET_OPTIONS
        )
        assert known <= params, f"not real submit() kwargs: {sorted(known - params)}"
        # Everything submit() takes beyond the plumbing should be reachable.
        plumbing = {"url", "access_key", "secret_key", "timeout", "stale_days"}
        assert not (params - plumbing - known), (
            f"submit() option not exposed: {sorted(params - plumbing - known)}"
        )
