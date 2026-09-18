"""The escalation tier, and the SSRF guard that had to reach it.

``impersonating_get`` was written for link *validation*, which records a status
code and nothing else. Citation verification now uses it too — and that caller
checksums the body and hands it to a model, which is the case ``DEFAULT_HEADERS``
singles out as needing the fail-closed default. ``allow_redirects=True`` does
not provide it: it validates the URL you pass and then follows an
attacker-chosen chain unchecked, exactly the gap ``safe_get`` exists to close.

``curl_cffi`` is an optional extra, so every test here installs a fake one
rather than depending on it being present. That also lets the absent case be
tested at all.

The cost of that choice is worth stating plainly: because the fake is always
used, **these tests pass whether or not the real dependency works**. They said
nothing while the tier was inert for a month, and they would say nothing about
a curl_cffi whose Chrome profile has aged into being fingerprinted. Only a live
fetch answers that — see docs/CITATIONS.md, "Escalating past a bot block".
"""

import sys
import types
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from ci_core import http


def _resp(status_code=200, url="https://example.com/doc", location=None, body=b"hi"):
    headers = {"Content-Type": "text/html"}
    if location is not None:
        headers["Location"] = location
    return type(
        "R",
        (),
        {
            "status_code": status_code,
            "url": url,
            "content": body,
            "headers": headers,
        },
    )()


class _FakeCurlOpt:
    """Stands in for ``curl_cffi.const.CurlOpt``: only the member used here."""

    SSL_OPTIONS = "CURLOPT_SSL_OPTIONS"


class _CurlError(Exception):
    """curl_cffi's exceptions carry curl's error number as ``code``."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@contextmanager
def _fake_curl_cffi(responses):
    """Stand in for the optional ``curl_cffi`` extra.

    ``impersonating_get`` imports it inside the function body, so the module
    only has to be in ``sys.modules`` by the time it is called. An exception
    in ``responses`` is raised by that call instead of returned.
    """
    calls = []

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    requests_mod = types.ModuleType("curl_cffi.requests")
    requests_mod.get = _get
    const_mod = types.ModuleType("curl_cffi.const")
    const_mod.CurlOpt = _FakeCurlOpt
    module = types.ModuleType("curl_cffi")
    module.requests = requests_mod
    module.const = const_mod
    with patch.dict(
        sys.modules,
        {
            "curl_cffi": module,
            "curl_cffi.requests": requests_mod,
            "curl_cffi.const": const_mod,
        },
    ):
        yield calls


@contextmanager
def _hosts(mapping, default="93.184.216.34"):
    """Resolve each hostname in ``mapping`` to the address given for it."""

    def _getaddrinfo(host, *_args, **_kwargs):
        return [(0, 0, 0, "", (mapping.get(host, default), 0))]

    with patch.object(http.socket, "getaddrinfo", side_effect=_getaddrinfo):
        yield


class TestTheHappyPath:
    def test_a_public_url_is_fetched_and_returned(self):
        with _hosts({}), _fake_curl_cffi([_resp()]) as calls:
            resp = http.impersonating_get("https://example.com/doc")

        assert resp is not None
        assert resp.status_code == 200
        # Redirect following is ours now, not curl_cffi's — that is the whole
        # point, since its own following is what skipped the guard.
        assert calls[0][1]["allow_redirects"] is False
        assert calls[0][1]["impersonate"] == "chrome"

    def test_a_redirect_to_another_public_host_is_followed(self):
        with (
            _hosts({}),
            _fake_curl_cffi(
                [
                    _resp(302, location="https://elsewhere.example/real"),
                    _resp(200, url="https://elsewhere.example/real"),
                ]
            ) as calls,
        ):
            resp = http.impersonating_get("https://example.com/doc")

        assert resp is not None
        assert resp.url == "https://elsewhere.example/real"
        assert [c[0] for c in calls] == [
            "https://example.com/doc",
            "https://elsewhere.example/real",
        ]

    def test_a_relative_location_is_resolved_before_it_is_validated(self):
        with (
            _hosts({}),
            _fake_curl_cffi(
                [
                    _resp(301, location="/moved"),
                    _resp(200, url="https://example.com/moved"),
                ]
            ) as calls,
        ):
            assert http.impersonating_get("https://example.com/doc") is not None

        assert calls[1][0] == "https://example.com/moved"


class TestTheGuardReachesEveryHop:
    """The reason this function could not be handed a body-consuming caller
    as it was."""

    def test_a_redirect_into_a_private_range_is_refused(self):
        with (
            _hosts({"internal.example": "10.0.0.5"}),
            _fake_curl_cffi(
                [
                    _resp(302, location="http://internal.example/secrets"),
                    _resp(200, body=b"internal data"),
                ]
            ) as calls,
        ):
            assert http.impersonating_get("https://example.com/doc") is None

        # Refused *before* the request, not after reading the body: one call
        # went out, and the internal hop never did.
        assert len(calls) == 1

    def test_a_redirect_to_the_cloud_metadata_endpoint_is_refused(self):
        with (
            _hosts({"metadata.example": "169.254.169.254"}),
            _fake_curl_cffi(
                [
                    _resp(302, location="http://metadata.example/latest/meta-data/"),
                    _resp(200, body=b"iam credentials"),
                ]
            ) as calls,
        ):
            assert http.impersonating_get("https://example.com/doc") is None

        assert len(calls) == 1

    def test_the_first_url_is_guarded_too(self):
        with (
            _hosts({"internal.example": "127.0.0.1"}),
            _fake_curl_cffi([_resp(200)]) as calls,
        ):
            assert http.impersonating_get("http://internal.example/x") is None

        assert calls == []

    def test_an_endless_redirect_chain_gives_up(self):
        with (
            _hosts({}),
            _fake_curl_cffi(
                [_resp(302, location="https://example.com/loop")] * 20
            ) as calls,
        ):
            assert http.impersonating_get("https://example.com/doc") is None

        assert len(calls) == http._MAX_REDIRECTS + 1


class TestFailuresAllLookTheSame:
    """Every failure returns None, because callers treat None as "the block
    held" and were written before this function had more than one way to fail.
    """

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_an_error_status_is_not_content(self, status):
        with _hosts({}), _fake_curl_cffi([_resp(status)]):
            assert http.impersonating_get("https://example.com/doc") is None

    def test_a_raising_transport_is_not_content(self):
        with _hosts({}), _fake_curl_cffi([OSError("tls fail")]) as calls:
            assert http.impersonating_get("https://example.com/doc") is None
        # The request was actually attempted: None came from the transport
        # raising, not from an import failing before it.
        assert len(calls) == 1

    def test_the_optional_extra_being_absent_is_not_an_error(self):
        """The dependency ships as ``ci-core[unblock]``, so an end user who
        did not ask for the extra has no curl_cffi and must still get a clean
        degradation rather than an ImportError.

        Development checkouts do have it — ``ci-core[unblock]`` is in the root
        dev group as of 2026-09-06, because while it was absent the escalation
        tier was inert in every ``uv run`` and nothing said so."""
        real_import = __import__

        def _no_curl_cffi(name, *args, **kwargs):
            if name == "curl_cffi":
                raise ImportError("No module named 'curl_cffi'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_no_curl_cffi):
            assert http.impersonating_get("https://example.com/doc") is None

    def test_a_redirect_with_no_location_ends_the_chain(self):
        with _hosts({}), _fake_curl_cffi([_resp(302)]):
            # A 3xx carrying nowhere to go is an error status, not content.
            assert http.impersonating_get("https://example.com/doc") is None


class TestAbsentIsNotTheSameAsBlocked:
    """The two outcomes ``impersonating_get`` flattens into ``None``.

    Keeping them apart is the whole point: a block that held is a fact about
    the source, a missing extra is a fact about this machine, and only the
    second is fixable by installing something. Reporting them identically is
    what let the tier sit inert for a month.
    """

    def test_available_is_true_when_the_extra_imports(self):
        with _fake_curl_cffi([]):
            assert http.impersonation_available() is True

    def test_available_is_false_when_the_extra_is_absent(self):
        real_import = __import__

        def _no_curl_cffi(name, *args, **kwargs):
            if name == "curl_cffi":
                raise ImportError("No module named 'curl_cffi'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_no_curl_cffi):
            assert http.impersonation_available() is False

    def test_the_absent_extra_is_warned_about(self, caplog):
        real_import = __import__

        def _no_curl_cffi(name, *args, **kwargs):
            if name == "curl_cffi":
                raise ImportError("No module named 'curl_cffi'")
            return real_import(name, *args, **kwargs)

        with patch.object(http, "_unavailable_warned", False):
            with patch("builtins.__import__", side_effect=_no_curl_cffi):
                with caplog.at_level("WARNING", logger=http.log.name):
                    assert http.impersonating_get("https://example.com/doc") is None
        assert "unblock" in caplog.text
        assert "curl_cffi" in caplog.text

    def test_the_warning_is_emitted_once_not_per_url(self, caplog):
        """A 15-link article printing this 15 times trains the reader to skip it."""
        real_import = __import__

        def _no_curl_cffi(name, *args, **kwargs):
            if name == "curl_cffi":
                raise ImportError("No module named 'curl_cffi'")
            return real_import(name, *args, **kwargs)

        with patch.object(http, "_unavailable_warned", False):
            with patch("builtins.__import__", side_effect=_no_curl_cffi):
                with caplog.at_level("WARNING", logger=http.log.name):
                    for _ in range(5):
                        http.impersonating_get("https://example.com/doc")
        assert caplog.text.count("uv sync --extra unblock") == 1

    def test_a_held_block_is_not_reported_as_a_missing_extra(self, caplog):
        """curl_cffi present and the origin still refuses: no install advice."""
        with _hosts({}), _fake_curl_cffi([_resp(403)]):
            with caplog.at_level("WARNING", logger=http.log.name):
                assert http.impersonating_get("https://example.com/doc") is None
        assert "uv sync --extra unblock" not in caplog.text


#: CURLSSLOPT_NATIVE_CA (1<<4) | CURLSSLOPT_NO_PARTIALCHAIN (1<<2), spelled out
#: from curl/curl.h rather than read back from the module, so a wrong bit in
#: ci_core.http fails here instead of agreeing with itself.
_NATIVE_CA_WITHOUT_PARTIAL_CHAINS = (1 << 4) | (1 << 2)


class TestTheEscalationTrustsWhatTheOSTrusts:
    """curl_cffi does its own TLS, so the truststore fix to ``os_trust_get``
    never reached this tier. On www.ntia.gov, whose chain ends at a root
    Windows trusts and certifi dropped, it failed with curl error 60 — and
    ``impersonating_get`` turned that into a silent ``None``.
    """

    def test_on_windows_curl_uses_the_os_store_and_refuses_partial_chains(
        self, monkeypatch
    ):
        monkeypatch.setattr(http, "_CURL_NATIVE_CA", True)
        with _hosts({}), _fake_curl_cffi([_resp()]) as calls:
            assert http.impersonating_get("https://www.ntia.gov/") is not None

        kwargs = calls[0][1]
        assert kwargs["curl_options"] == {
            _FakeCurlOpt.SSL_OPTIONS: _NATIVE_CA_WITHOUT_PARTIAL_CHAINS
        }
        # curl_cffi verifies unless told not to, and ``verify`` is the only way
        # to tell it. A source fetch that can be intercepted proves nothing.
        assert "verify" not in kwargs

    def test_elsewhere_curl_keeps_its_default_bundle(self, monkeypatch):
        """curl documents NATIVE_CA for OpenSSL-family backends on Windows
        only, so no other platform is handed it."""
        monkeypatch.setattr(http, "_CURL_NATIVE_CA", False)
        with _hosts({}), _fake_curl_cffi([_resp()]) as calls:
            http.impersonating_get("https://www.ntia.gov/")

        assert calls[0][1]["curl_options"] == {}
        assert "verify" not in calls[0][1]

    def test_every_redirect_hop_carries_the_same_trust(self, monkeypatch):
        monkeypatch.setattr(http, "_CURL_NATIVE_CA", True)
        with (
            _hosts({}),
            _fake_curl_cffi(
                [
                    _resp(301, location="https://www.ntia.gov/moved"),
                    _resp(200, url="https://www.ntia.gov/moved"),
                ]
            ) as calls,
        ):
            assert http.impersonating_get("https://www.ntia.gov/doc") is not None

        assert [c[1]["curl_options"] for c in calls] == [
            {_FakeCurlOpt.SSL_OPTIONS: _NATIVE_CA_WITHOUT_PARTIAL_CHAINS}
        ] * 2

    def test_a_certificate_failure_is_reported_not_swallowed(self, caplog):
        """Still ``None`` — callers treat that as "the block held" and fall
        through to the archive — but a certificate failure means the request
        never reached the page, so it is said out loud."""
        err = _CurlError(
            60,
            "Failed to perform, curl: (60) SSL certificate OpenSSL verify "
            "result: unable to get local issuer certificate (20).",
        )
        with _hosts({}), _fake_curl_cffi([err]):
            with caplog.at_level("WARNING", logger=http.log.name):
                assert http.impersonating_get("https://www.ntia.gov/doc") is None

        assert "failed certificate verification" in caplog.text
        assert "https://www.ntia.gov/doc" in caplog.text
        assert "unable to get local issuer certificate" in caplog.text

    @pytest.mark.parametrize(
        "outcome",
        [
            _CurlError(28, "curl: (28) Operation timed out"),
            _CurlError(6, "curl: (6) Could not resolve host"),
            OSError("connection reset"),
            _resp(403),
        ],
        ids=["timeout", "dns", "no-code", "held-block"],
    )
    def test_other_failures_do_not_claim_a_certificate_problem(self, outcome, caplog):
        with _hosts({}), _fake_curl_cffi([outcome]):
            with caplog.at_level("WARNING", logger=http.log.name):
                assert http.impersonating_get("https://example.com/doc") is None

        assert "certificate" not in caplog.text
