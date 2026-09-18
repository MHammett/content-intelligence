"""Tests for adapters.citation.wayback — the citation-review-specific policy
that stays local (fallback-status scoping, human-readable summaries), on top
of the shared archive.org engine now tested in the spn-client package."""

import datetime
import ipaddress
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
import urllib3
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
        reasons = set(wayback._FALLBACK_STATUSES.values()) | {
            "timeout",
            "tls_untrusted",
            "unreachable",
        }
        assert reasons <= set(wayback.FALLBACK_REASON_LABELS)


def _wrapped_like_urllib3(ssl_error):
    """``ssl_error`` inside the chain requests really raises (see conftest)."""
    return requests.exceptions.SSLError(
        urllib3.exceptions.MaxRetryError(
            urllib3.HTTPSConnectionPool("www.example.org", 443),
            "/page",
            reason=urllib3.exceptions.SSLError(ssl_error),
        )
    )


class TestCertificateFailures:
    """A certificate that fails verification is ``tls_untrusted``, not
    ``unreachable``.

    ``requests.exceptions.SSLError`` subclasses ``ConnectionError``, so every
    www.ntia.gov link in the 2026-09-17 smoke test was filed as an unreachable
    origin — the reason a hostname that does not resolve gets, and one the
    expansion pass reads as a sign the URL was invented.
    """

    def test_the_certificate_failure_is_its_own_reason(self):
        exc = requests.exceptions.SSLError(
            ssl.SSLCertVerificationError(
                1,
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "unable to get local issuer certificate (_ssl.c:1010)",
            )
        )
        assert isinstance(exc, requests.exceptions.ConnectionError), (
            "the premise: requests files SSLError under ConnectionError"
        )
        assert wayback.fallback_reason_for_exception(exc) == "tls_untrusted"

    def test_it_is_found_inside_the_chain_requests_really_raises(
        self, certificate_failure
    ):
        assert (
            wayback.fallback_reason_for_exception(certificate_failure())
            == "tls_untrusted"
        )

    def test_it_is_matched_on_type_not_on_openssls_wording(self):
        """truststore raises the same type with the platform's message, which
        says nothing about "certificate verify failed"."""
        err = ssl.SSLCertVerificationError(
            "A certificate chain processed, but terminated in a root "
            "certificate which is not trusted by the trust provider."
        )
        err.verify_message = err.args[0]
        err.verify_code = 0x800B0109
        exc = _wrapped_like_urllib3(err)
        assert "verify failed" not in str(exc)
        assert wayback.fallback_reason_for_exception(exc) == "tls_untrusted"

    @pytest.mark.parametrize(
        "ssl_error",
        [
            ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"),
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
        ],
        ids=["protocol-mismatch", "handshake-reset"],
    )
    def test_a_tls_failure_that_is_not_about_the_certificate_stays_unreachable(
        self, ssl_error
    ):
        """The connection failed; nobody declined to trust it. Sweeping every
        SSLError into tls_untrusted would relabel these as certificate
        problems, which is the same mistake in the other direction."""
        exc = _wrapped_like_urllib3(ssl_error)
        assert wayback.fallback_reason_for_exception(exc) == "unreachable"
        assert wayback.certificate_error(exc) is None

    def test_a_timeout_is_still_a_timeout(self):
        assert (
            wayback.fallback_reason_for_exception(requests.exceptions.ReadTimeout())
            == "timeout"
        )

    def test_an_error_raised_while_handling_one_is_not_one(self, certificate_failure):
        """``__context__`` records what was being handled, not what caused the
        exception, so it is not followed."""
        try:
            try:
                raise certificate_failure()
            except requests.exceptions.SSLError:
                raise requests.exceptions.ConnectionError("getaddrinfo failed")
        except requests.exceptions.ConnectionError as later:
            assert later.__context__ is not None
            assert wayback.fallback_reason_for_exception(later) == "unreachable"

    def test_an_explicit_cause_is_followed(self):
        err = ssl.SSLCertVerificationError(1, "certificate verify failed")
        try:
            try:
                raise err
            except ssl.SSLError as inner:
                raise requests.exceptions.ConnectionError("wrapped") from inner
        except requests.exceptions.ConnectionError as outer:
            assert wayback.certificate_error(outer) is err

    def test_a_cyclic_chain_does_not_hang(self):
        a = requests.exceptions.ConnectionError("a")
        b = requests.exceptions.ConnectionError(a)
        a.args = (b,)
        assert wayback.certificate_error(a) is None

    def test_it_has_a_label_that_does_not_say_unreachable(self):
        label = wayback.FALLBACK_REASON_LABELS["tls_untrusted"]
        assert "certificate" in label
        assert "unreachable" not in label


class TestCertificateFailureSummary:
    def test_it_keeps_the_verifiers_reason_and_drops_the_wrapper(
        self, certificate_failure
    ):
        exc = certificate_failure("certificate has expired")
        assert "HTTPSConnectionPool" in str(exc), "the raw form this replaces"

        summary = wayback.certificate_failure_summary(exc)
        assert (
            summary == "TLS certificate could not be verified: certificate has expired"
        )

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ReadTimeout("read timed out"),
            requests.exceptions.ConnectionError("getaddrinfo failed"),
            _wrapped_like_urllib3(ssl.SSLError(1, "[SSL] wrong version number")),
        ],
    )
    def test_anything_else_gets_none(self, exc):
        assert wayback.certificate_failure_summary(exc) is None

    def test_archive_orgs_own_certificate_is_not_called_unreachable(
        self, certificate_failure
    ):
        """The same misfiling on the archive side: a snapshot read that failed
        verification read "the connection was refused, dropped, or the host
        did not resolve" — none of which happened."""
        summary = wayback.transport_failure_summary(
            certificate_failure(host="web.archive.org"), "for the snapshot"
        )
        assert summary == (
            "archive.org's TLS certificate could not be verified for the "
            "snapshot (unable to get local issuer certificate)"
        )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A client that rejects the handshake resets the connection. That is
        # the test passing, not an error.
        pass


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture(scope="module")
def untrusted_https_url(tmp_path_factory):
    """A loopback HTTPS server with a self-signed certificate no client trusts."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    out = tmp_path_factory.mktemp("untrusted_tls")
    cert_path, key_path = out / "cert.pem", out / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    server = _QuietServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    _serve(server)
    yield f"https://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def plaintext_port_as_https_url():
    """``https://`` to a server that only speaks plain HTTP."""
    server = _serve(_QuietServer(("127.0.0.1", 0), _Handler))
    yield f"https://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


class TestRealHandshakes:
    """The chain ``certificate_error`` walks, as a real handshake builds it.

    The tests above construct the exception by hand, which only proves the code
    reads the shape its author believed in. Here ``ssl``, urllib3 and requests
    build it. Loopback only; and plain ``requests`` verifies with certifi, so
    the Windows chain engine — which goes online for an unknown root — is never
    consulted.
    """

    def test_an_untrusted_certificate_is_tls_untrusted(self, untrusted_https_url):
        with pytest.raises(requests.exceptions.SSLError) as caught:
            requests.get(untrusted_https_url, timeout=5)

        assert wayback.fallback_reason_for_exception(caught.value) == "tls_untrusted"
        summary = wayback.certificate_failure_summary(caught.value)
        assert summary.startswith("TLS certificate could not be verified: ")
        assert "HTTPSConnectionPool" not in summary

    def test_a_tls_failure_unrelated_to_certificates_stays_unreachable(
        self, plaintext_port_as_https_url
    ):
        with pytest.raises(requests.exceptions.ConnectionError) as caught:
            requests.get(plaintext_port_as_https_url, timeout=5)

        assert wayback.fallback_reason_for_exception(caught.value) == "unreachable"
        assert wayback.certificate_failure_summary(caught.value) is None


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
