"""Host classification: a DNS failure is not a private address.

A real `maximum` run refused https://pcb.illinois.gov/... — a public state
government host — during a transient DNS blip, and told the reader the source
"resolves to a private, loopback, or link-local address". It does not. That is
the overstated-confidence failure the audit exists to remove, reintroduced by
the fail-closed default.

Two defects, one cause: "could not resolve" was folded into "non-public".
"""

from unittest.mock import patch

import pytest
import requests

from ci_core import http


class TestClassifyHost:
    def test_a_public_host_is_public(self):
        with patch.object(
            http.socket,
            "getaddrinfo",
            return_value=[(0, 0, 0, "", ("93.184.216.34", 0))],
        ):
            assert http.classify_host("https://example.com/x") == http.HOST_PUBLIC

    @pytest.mark.parametrize(
        "addr", ["127.0.0.1", "10.1.5.22", "192.168.1.1", "169.254.169.254"]
    )
    def test_private_addresses_are_non_public(self, addr):
        with patch.object(
            http.socket, "getaddrinfo", return_value=[(0, 0, 0, "", (addr, 0))]
        ):
            assert http.classify_host("http://h/x") == http.HOST_NON_PUBLIC

    def test_dns_failure_is_its_own_outcome(self):
        with patch.object(http.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            assert (
                http.classify_host("https://pcb.illinois.gov/x")
                == http.HOST_UNRESOLVABLE
            )

    def test_a_url_with_no_host_is_non_public(self):
        assert http.classify_host("not-a-url") == http.HOST_NON_PUBLIC


class TestGuardDistinguishesTheTwoFailures:
    def test_a_private_address_raises_unsafeurl(self):
        with patch.object(
            http.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 0))]
        ):
            with pytest.raises(http.UnsafeURLError, match="non-public"):
                http._guard("http://localhost/x")

    def test_dns_failure_raises_a_connection_error_not_a_refusal(self):
        """It must look like an unreachable origin, because that is what it is."""
        with patch.object(http.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            with pytest.raises(requests.exceptions.ConnectionError, match="resolve"):
                http._guard("https://pcb.illinois.gov/x")

    def test_a_dns_failure_is_never_reported_as_a_private_address(self):
        """The specific false statement from the real run."""
        with patch.object(http.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            try:
                http._guard("https://pcb.illinois.gov/x")
            except Exception as exc:
                assert not isinstance(exc, http.UnsafeURLError)
                assert "private" not in str(exc).lower()
                assert "link-local" not in str(exc).lower()


class TestDnsFailureStaysWaybackEligible:
    def test_the_wayback_fallback_recognises_it_as_unreachable(self):
        """PR #59 made unreachable origins archive-eligible; a refusal stole that."""
        from ci_article_review.adapters.citation import wayback

        with patch.object(http.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            try:
                http._guard("https://pcb.illinois.gov/x")
            except Exception as exc:
                assert wayback.fallback_reason_for_exception(exc) == "unreachable"


class TestIsPublicHostKeepsItsBooleanContract:
    def test_link_validation_still_fails_open_on_dns_error(self):
        with patch.object(http.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            assert (
                http.is_public_host("https://h/x", fail_open_on_dns_error=True) is True
            )

    def test_the_default_is_still_fail_closed(self):
        with patch.object(http.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            assert http.is_public_host("https://h/x") is False


class TestClassifyHostDoesNotFailOpen:
    """Validating nothing is not the same as validating a public address.

    The loop returned HOST_PUBLIC after falling through without checking a
    single address — so an empty ``getaddrinfo`` result, or addresses that do
    not parse as IPs, were reported as safe to fetch. That is the one direction
    an SSRF guard must not fail in.
    """

    def test_an_empty_resolution_is_unresolvable_not_public(self):
        from unittest.mock import patch

        with patch("ci_core.http.socket.getaddrinfo", return_value=[]):
            assert http.classify_host("https://x.example/") == http.HOST_UNRESOLVABLE

    def test_addresses_that_do_not_parse_are_unresolvable_not_public(self):
        from unittest.mock import patch

        infos = [(2, 1, 6, "", ("not-an-ip", 0))]
        with patch("ci_core.http.socket.getaddrinfo", return_value=infos):
            assert http.classify_host("https://x.example/") == http.HOST_UNRESOLVABLE

    def test_a_real_public_address_is_still_public(self):
        from unittest.mock import patch

        infos = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with patch("ci_core.http.socket.getaddrinfo", return_value=infos):
            assert http.classify_host("https://x.example/") == http.HOST_PUBLIC

    def test_one_unparseable_address_does_not_mask_a_private_one(self):
        from unittest.mock import patch

        infos = [(2, 1, 6, "", ("not-an-ip", 0)), (2, 1, 6, "", ("127.0.0.1", 0))]
        with patch("ci_core.http.socket.getaddrinfo", return_value=infos):
            assert http.classify_host("https://x.example/") == http.HOST_NON_PUBLIC
