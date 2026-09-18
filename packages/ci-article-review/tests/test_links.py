"""Tests for analysis.links — URL extraction, SSRF guard, parallel validation."""

from unittest.mock import MagicMock, patch

import requests

from ci_article_review.analysis import links


class TestExtractUrls:
    def test_strips_trailing_period(self):
        assert links.extract_urls("See https://example.com.") == ["https://example.com"]

    def test_strips_trailing_comma(self):
        assert links.extract_urls("https://example.com, and more") == [
            "https://example.com"
        ]

    def test_preserves_query_string(self):
        urls = links.extract_urls("Go to https://example.com/p?q=1&r=2 now")
        assert urls == ["https://example.com/p?q=1&r=2"]

    def test_dedupes(self):
        text = "https://a.com and https://a.com again"
        assert links.extract_urls(text) == ["https://a.com"]

    def test_no_urls(self):
        assert links.extract_urls("no links here") == []


class TestSsrfGuard:
    def test_blocks_cloud_metadata(self):
        assert (
            links._is_public_host("http://169.254.169.254/latest/meta-data/") is False
        )

    def test_blocks_localhost(self):
        assert links._is_public_host("http://localhost:6379/") is False

    def test_blocks_loopback_ip(self):
        assert links._is_public_host("http://127.0.0.1/admin") is False

    def test_blocks_private_ranges(self):
        for url in ("http://192.168.1.1/", "http://10.0.0.5/", "http://172.16.0.1/"):
            assert links._is_public_host(url) is False, url

    def test_allows_public_host(self):
        assert links._is_public_host("https://www.fhwa.dot.gov/") is True

    def test_no_hostname_rejected(self):
        assert links._is_public_host("not-a-url") is False

    def test_check_http_skips_internal(self):
        result = links._check_http("http://169.254.169.254/")
        assert result["ok"] is False
        assert "SSRF guard" in result["error"]


class TestValidateLinks:
    def test_empty_text_returns_empty(self):
        assert links.validate_links("", check_wayback=False) == []

    def test_preserves_url_order(self):
        text = "First https://zzz.example.com then https://aaa.example.com"

        # Mock the per-URL worker so no network happens
        def fake_check(url, timeout):
            return {"status_code": 200, "ok": True, "redirected_to": None}

        with patch(
            "ci_article_review.analysis.links._check_http", side_effect=fake_check
        ):
            results = links.validate_links(text, check_wayback=False)
        assert [r["url"] for r in results] == [
            "https://zzz.example.com",
            "https://aaa.example.com",
        ]

    def test_internal_url_not_sent_to_wayback(self):
        text = "Internal http://localhost:8080/x"
        with patch("ci_article_review.analysis.links.wayback_check") as mock_wb:
            results = links.validate_links(text, check_wayback=True)
        # SSRF-guarded host should never hit Wayback
        mock_wb.assert_not_called()
        assert results[0]["ok"] is False

    def test_wayback_stale_days_threaded_through(self):
        # The wayback_stale_days arg must reach wayback_check as stale_days.
        text = "See https://example.com"

        def fake_http(url, timeout):
            return {"status_code": 200, "ok": True, "redirected_to": None}

        with (
            patch(
                "ci_article_review.analysis.links._check_http", side_effect=fake_http
            ),
            patch(
                "ci_article_review.analysis.links.wayback_check",
                return_value={"archived": True},
            ) as mock_wb,
        ):
            links.validate_links(text, check_wayback=True, wayback_stale_days=90)
        _, kwargs = mock_wb.call_args
        assert kwargs.get("stale_days") == 90


class TestWaybackFallbackOnUnreadableOrigin:
    """A link the origin refused (401/403/429) or that we never reached
    (timeout, DNS/connection error) should fall back to a Wayback snapshot;
    404/410/5xx should not."""

    def _head_response(self, status_code):
        resp = MagicMock()
        resp.status_code = status_code
        resp.url = "https://example.com/blocked"
        return resp

    def _get_response(self, status):
        """A GET the origin also refuses — the 403 retry finds no better answer.

        _check_http now retries a 403 with GET (some servers reject HEAD
        specifically). These cases cover the origin refusing both, so the
        Wayback fallback still has to carry the link.
        """
        resp = MagicMock(status_code=status)
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.headers = {}
        resp.url = "https://example.com/blocked"
        resp.history = []
        return resp

    def test_403_recovers_via_wayback_snapshot(self):
        snapshot_url = (
            "https://web.archive.org/web/20240101000000/https://example.com/blocked"
        )
        snap_resp = MagicMock(status_code=200)

        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                return_value=self._head_response(403),
            ),
            patch(
                "ci_article_review.analysis.links.os_trust_get",
                return_value=self._get_response(403),
            ),
            patch(
                "ci_article_review.analysis.links.safe_get",
                return_value=snap_resp,
            ) as mock_get,
            patch(
                "ci_article_review.analysis.links.wayback_check",
                return_value={"archived": True, "snapshot_url": snapshot_url},
            ),
        ):
            result = links._check_http("https://example.com/blocked")

        assert result["ok"] is True
        assert result["status_code"] == 403
        assert result["verified_via"] == "wayback_fallback"
        assert result["origin_failure"] == "blocked"
        assert result["wayback_snapshot_url"] == snapshot_url
        mock_get.assert_called_once()

    def test_403_with_no_snapshot_stays_blocked(self):
        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                return_value=self._head_response(403),
            ),
            patch(
                "ci_article_review.analysis.links.os_trust_get",
                return_value=self._get_response(403),
            ),
            patch(
                "ci_article_review.analysis.links.wayback_check",
                return_value={"archived": False},
            ),
        ):
            result = links._check_http("https://example.com/blocked")

        assert result["ok"] is False
        assert result["status_code"] == 403
        assert result["verified_via"] == "direct"
        assert "wayback_snapshot_url" not in result

    def test_404_does_not_attempt_wayback_fallback(self):
        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                return_value=self._head_response(404),
            ),
            patch("ci_article_review.analysis.links.wayback_check") as mock_wb,
        ):
            result = links._check_http("https://example.com/gone")

        assert result["ok"] is False
        assert result["status_code"] == 404
        assert "origin_failure" not in result
        mock_wb.assert_not_called()

    def test_410_does_not_attempt_wayback_fallback(self):
        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                return_value=self._head_response(410),
            ),
            patch("ci_article_review.analysis.links.wayback_check") as mock_wb,
        ):
            result = links._check_http("https://example.com/gone-for-good")

        assert result["ok"] is False
        mock_wb.assert_not_called()

    def test_5xx_does_not_attempt_wayback_fallback(self):
        """The origin's own failure isn't something an archive copy stands in
        for — see wayback._FALLBACK_STATUSES for the reasoning."""
        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                return_value=self._head_response(503),
            ),
            patch("ci_article_review.analysis.links.wayback_check") as mock_wb,
        ):
            result = links._check_http("https://example.com/down")

        assert result["ok"] is False
        mock_wb.assert_not_called()

    def _check_with_head_failure(self, exc, wayback_result):
        """Run _check_http where the HEAD request raises ``exc``."""
        snap_resp = MagicMock(status_code=200)
        with (
            patch("ci_article_review.analysis.links.os_trust_head", side_effect=exc),
            patch(
                "ci_article_review.analysis.links.safe_get",
                return_value=snap_resp,
            ) as mock_get,
            patch(
                "ci_article_review.analysis.links.wayback_check",
                return_value=wayback_result,
            ),
        ):
            result = links._check_http("https://example.com/slow")
        return result, mock_get

    def test_timeout_recovers_via_wayback_snapshot(self):
        snapshot_url = (
            "https://web.archive.org/web/20240101000000/https://example.com/slow"
        )
        result, mock_get = self._check_with_head_failure(
            requests.exceptions.ConnectTimeout("connect timed out"),
            {"archived": True, "snapshot_url": snapshot_url},
        )

        assert result["ok"] is True
        assert result["verified_via"] == "wayback_fallback"
        assert result["origin_failure"] == "timeout"
        assert result["wayback_snapshot_url"] == snapshot_url
        # The origin really did fail; the report must not read as a clean fetch.
        assert result["error"] == "timeout"
        mock_get.assert_called_once()

    def test_dns_failure_recovers_via_wayback_snapshot(self):
        snapshot_url = (
            "https://web.archive.org/web/20240101000000/https://example.com/slow"
        )
        # requests wraps urllib3's NameResolutionError in a ConnectionError.
        result, _ = self._check_with_head_failure(
            requests.exceptions.ConnectionError(
                "NameResolutionError: getaddrinfo failed [Errno 11002]"
            ),
            {"archived": True, "snapshot_url": snapshot_url},
        )

        assert result["ok"] is True
        assert result["verified_via"] == "wayback_fallback"
        assert result["origin_failure"] == "unreachable"
        assert "getaddrinfo failed" in result["error"]

    def test_timeout_with_no_snapshot_stays_broken(self):
        result, _ = self._check_with_head_failure(
            requests.exceptions.ReadTimeout("read timed out"), {"archived": False}
        )

        assert result["ok"] is False
        assert result["error"] == "timeout"
        # Still classified as unread-not-dead, so the report can say so.
        assert result["origin_failure"] == "timeout"
        assert "wayback_snapshot_url" not in result

    def test_a_certificate_failure_recovers_via_wayback_snapshot(
        self, certificate_failure
    ):
        snapshot_url = (
            "https://web.archive.org/web/20240101000000/https://example.com/slow"
        )
        result, mock_get = self._check_with_head_failure(
            certificate_failure(), {"archived": True, "snapshot_url": snapshot_url}
        )

        assert result["ok"] is True
        assert result["verified_via"] == "wayback_fallback"
        assert result["origin_failure"] == "tls_untrusted"
        assert result["wayback_snapshot_url"] == snapshot_url
        mock_get.assert_called_once()

    def test_a_certificate_failure_reports_the_reason_not_the_exception(
        self, certificate_failure
    ):
        """What every www.ntia.gov link carried on 2026-09-17: ``unreachable``,
        with ``HTTPSConnectionPool(...)`` as the error."""
        result, _ = self._check_with_head_failure(
            certificate_failure(), {"archived": False}
        )

        assert result["ok"] is False
        assert result["origin_failure"] == "tls_untrusted"
        assert result["error"] == (
            "TLS certificate could not be verified: "
            "unable to get local issuer certificate"
        )

    def test_a_certificate_failure_is_never_escalated(self, certificate_failure):
        """Both tiers verify against the same roots, so a browser fingerprint
        cannot get past a certificate the plain fetch rejected."""
        with patch("ci_article_review.analysis.links.impersonating_get") as mock_imp:
            result, _ = self._check_with_head_failure(
                certificate_failure(), {"archived": False}
            )

        mock_imp.assert_not_called()
        assert result["origin_failure"] == "tls_untrusted"

    def test_unrelated_exception_gets_no_fallback(self):
        """Only unreachable-origin failures qualify; a malformed URL doesn't."""
        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                side_effect=requests.exceptions.InvalidURL("bad url"),
            ),
            patch("ci_article_review.analysis.links.wayback_check") as mock_wb,
        ):
            result = links._check_http("https://example.com/bad")

        assert result["ok"] is False
        assert "origin_failure" not in result
        mock_wb.assert_not_called()

    def test_stale_snapshot_still_flagged_when_it_satisfies_a_timeout(self):
        """A 245-day-old snapshot recovering a timeout is not silently current."""
        snapshot_url = (
            "https://web.archive.org/web/20240101000000/https://example.com/slow"
        )
        wb = {
            "archived": True,
            "snapshot_url": snapshot_url,
            "snapshot_age_days": 245,
            "snapshot_stale": True,
        }
        snap_resp = MagicMock(status_code=200)
        with (
            patch(
                "ci_article_review.analysis.links.os_trust_head",
                side_effect=requests.exceptions.Timeout("timed out"),
            ),
            patch(
                "ci_article_review.analysis.links.safe_get",
                return_value=snap_resp,
            ),
            patch("ci_article_review.analysis.links.wayback_check", return_value=wb),
        ):
            entry = links._check_one(
                "https://example.com/slow",
                check_wayback=True,
                http_timeout=1,
                wayback_timeout=1,
            )

        assert entry["verified_via"] == "wayback_fallback"
        assert entry["wayback"]["snapshot_stale"] is True
        assert entry["wayback"]["snapshot_age_days"] == 245
