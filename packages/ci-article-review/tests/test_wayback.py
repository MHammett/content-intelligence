"""Tests for adapters.citation.wayback — the citation-review-specific policy
that stays local (fallback-status scoping, human-readable summaries), on top
of the shared archive.org engine now tested in the spn-client package."""

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
