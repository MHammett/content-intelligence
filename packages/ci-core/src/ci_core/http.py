"""Shared outbound-HTTP constants for the Content Intelligence platform.

The User-Agent is a *platform* identifier (per docs/NAMING.md): outbound HTTP
calls from any package present the brand `content-intelligence/<version>`, not a
per-component name. Defining it once here keeps every caller in sync and prevents
the string from drifting back to per-package values.
"""

import ipaddress
import logging
import socket
from importlib import metadata
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

try:
    _VERSION = metadata.version("ci-core")
except metadata.PackageNotFoundError:  # pragma: no cover - source/dev checkout
    _VERSION = "0.1.0"

#: Outbound HTTP User-Agent shared by all Content Intelligence packages.
USER_AGENT = f"content-intelligence/{_VERSION}"

# DEFAULT_HEADERS keeps the honest platform User-Agent (see docstring above)
# plus the Accept / Accept-Language headers a real browser always sends. A bare
# User-Agent-only request is itself a bot signal to some WAFs regardless of the
# UA string's contents.
#
# This is the DEFAULT identity, not a policy ceiling. Citation verification is
# allowed to escalate past it — see ``impersonating_get`` below — because the
# job is checking that sources an article already cites say what it claims, and
# an unverifiable citation is a worse outcome than a spoofed User-Agent.
#
# What escalation actually buys, measured 2026-08-12 against six real 403s:
#   * browser headers alone:  0/6.  The hard blocks are Cloudflare, which
#     returns 403 to a full browser header set on the domain root — it
#     fingerprints the TLS handshake, and no header string changes that.
#   * TLS impersonation:      2/5.  congress.gov (488 KB) and a CDC PDF
#     (661 KB) came back in full.
# Three academic publishers (ASME, Wiley/AGU, Royal Society) refuse all of it
# and return a ~6 KB challenge page. Those are not solvable by fingerprinting
# and are very likely subscription gates rather than bot gates.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# SSRF-guarded fetching
# ---------------------------------------------------------------------------
#
# This lives in ci_core rather than in ci-article-review's analysis/links.py,
# where the guard originally sat, for one concrete reason: adapters/citation/
# could not import from analysis/ without closing an import cycle, so the
# citation resolver fetched model-supplied URLs with no validation at all while
# user-supplied URLs were checked. The trust ordering was exactly inverted, and
# the cause was module placement. Putting the guard where every package can
# reach it makes the safe call the convenient one.

#: Redirect hops followed before giving up. Each hop is re-validated.
_MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a non-public address, or redirects to one."""


#: Outcomes of classifying a URL's host. "Unresolvable" is deliberately its own
#: outcome rather than being folded into "non-public": conflating the two makes
#: the code assert something false about a source. A real run refused
#: https://pcb.illinois.gov/... — a public government host — during a transient
#: DNS blip, and reported it as resolving "to a private, loopback, or
#: link-local address". It does not. Saying so is the overstated-confidence
#: failure this project exists to avoid.
HOST_PUBLIC = "public"
HOST_NON_PUBLIC = "non_public"
HOST_UNRESOLVABLE = "unresolvable"


def classify_host(url):
    """Return HOST_PUBLIC / HOST_NON_PUBLIC / HOST_UNRESOLVABLE for ``url``.

    A missing hostname counts as non-public — there is nothing to validate, so
    refusing is the safe reading.
    """
    host = urlparse(url).hostname
    if not host:
        return HOST_NON_PUBLIC
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return HOST_UNRESOLVABLE

    checked = 0
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        checked += 1
        if not ip.is_global or ip.is_loopback or ip.is_private or ip.is_link_local:
            return HOST_NON_PUBLIC

    # Reaching here having validated nothing is not evidence that the host is
    # public — it is the absence of evidence either way. Returning HOST_PUBLIC
    # for it made the guard fail open on precisely the inputs it could not
    # reason about: an empty ``getaddrinfo`` result, or addresses that do not
    # parse as IPs. "Unresolvable" is the honest answer and every caller already
    # handles it — ``safe_get`` turns it into the ConnectionError requests would
    # have raised anyway, and link validation fails open there deliberately, to
    # get an accurate DNS message rather than a security refusal it cannot
    # justify.
    if not checked:
        return HOST_UNRESOLVABLE
    return HOST_PUBLIC


def is_public_host(url, *, fail_open_on_dns_error=False):
    """True if ``url``'s host resolves only to public, routable addresses.

    Rejects loopback, private, link-local and other non-global addresses —
    which covers the cloud-metadata endpoint (169.254.169.254), localhost, and
    anything on the LAN.

    ``fail_open_on_dns_error`` decides what an unresolvable hostname means for
    callers that only want a boolean. Link *validation* wants True: the goal
    there is an accurate error message for the author, and letting the HTTP
    layer produce the real DNS error beats reporting it as a security refusal.

    Callers that fetch should prefer ``safe_get``/``safe_head``, which use
    ``classify_host`` directly and can therefore tell "could not resolve" apart
    from "resolved somewhere it should not".

    Not airtight on its own: DNS can change between this check and the socket
    connect (rebinding). ``safe_get`` narrows that window by re-validating each
    redirect hop; closing it entirely would mean connecting to a pinned IP with
    an explicit Host header, which is more machinery than this threat model
    needs.
    """
    outcome = classify_host(url)
    if outcome == HOST_UNRESOLVABLE:
        return bool(fail_open_on_dns_error)
    return outcome == HOST_PUBLIC


def _guard(url):
    """Raise if ``url`` must not be fetched; return cleanly if it may be.

    Two different failures, reported as two different things:

    * resolved to a non-public address -> ``UnsafeURLError``. A refusal.
    * could not be resolved at all -> ``requests.exceptions.ConnectionError``,
      which is exactly what ``requests`` itself raises for a DNS failure. That
      keeps the security posture (we still never open the socket) while letting
      the caller's existing handling treat it as an unreachable origin — which
      means ``wayback.fallback_reason_for_exception`` classifies it
      "unreachable" and an archived copy is tried, the behaviour PR #59 added
      and an SSRF refusal was wrongly stealing.
    """
    outcome = classify_host(url)
    if outcome == HOST_NON_PUBLIC:
        raise UnsafeURLError(
            f"Refusing to fetch a non-public/internal host (SSRF guard): {url}"
        )
    if outcome == HOST_UNRESOLVABLE:
        raise requests.exceptions.ConnectionError(
            f"Could not resolve host for {url} (DNS lookup failed)"
        )


def safe_get(url, *, timeout=15, headers=None, allow_redirects=True, **kwargs):
    """``requests.get`` with the SSRF guard applied to every hop.

    Automatic redirect following is disabled and re-implemented so each
    ``Location`` is validated before it is followed. Plain
    ``allow_redirects=True`` validates the URL you pass and then follows an
    attacker-chosen chain unchecked, which makes the initial check decorative.

    Raises ``UnsafeURLError`` if the target — or any hop — is non-public, or if
    the chain exceeds ``_MAX_REDIRECTS``. Every other failure is a normal
    ``requests`` exception, so callers keep their existing error handling.
    """
    headers = headers if headers is not None else DEFAULT_HEADERS
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        _guard(current)
        resp = requests.get(
            current, timeout=timeout, headers=headers, allow_redirects=False, **kwargs
        )
        if not allow_redirects or not resp.is_redirect:
            return resp
        location = resp.headers.get("Location")
        if not location:
            return resp
        # Relative Locations are legal and common; resolve before validating.
        current = requests.compat.urljoin(current, location)
    raise UnsafeURLError(f"Too many redirects (>{_MAX_REDIRECTS}) starting at {url}")


def safe_head(url, *, timeout=15, headers=None, **kwargs):
    """``requests.head`` behind the same guard, redirects re-validated per hop.

    Returns ``(response, final_url)`` — callers checking link health need to
    report where a redirect actually landed.
    """
    headers = headers if headers is not None else DEFAULT_HEADERS
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        _guard(current)
        resp = requests.head(
            current, timeout=timeout, headers=headers, allow_redirects=False, **kwargs
        )
        if not resp.is_redirect:
            return resp, current
        location = resp.headers.get("Location")
        if not location:
            return resp, current
        current = requests.compat.urljoin(current, location)
    raise UnsafeURLError(f"Too many redirects (>{_MAX_REDIRECTS}) starting at {url}")


__all__ = [
    "USER_AGENT",
    "DEFAULT_HEADERS",
    "UnsafeURLError",
    "HOST_PUBLIC",
    "HOST_NON_PUBLIC",
    "HOST_UNRESOLVABLE",
    "classify_host",
    "is_public_host",
    "safe_get",
    "safe_head",
    "impersonation_available",
]


# ---------------------------------------------------------------------------
# Escalated fetching for citation verification
# ---------------------------------------------------------------------------

#: Browser-shaped headers used with TLS impersonation. Pointless on their own —
#: see the measurement above — but a fingerprinted request that still announces
#: itself as a script gets refused by some WAFs on the mismatch alone.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def impersonation_available():
    """True when the optional ``unblock`` extra (``curl_cffi``) is importable.

    Callers use this to tell two outcomes apart that ``impersonating_get``
    deliberately reports the same way. It returns ``None`` both when a block
    genuinely held and when there was never an escalation to attempt, because
    every caller was written to treat ``None`` as "the block held" — which is
    the right default for a *fetch*, and the wrong one for *reporting*, since
    only one of the two is fixable by installing something.
    """
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


#: Set once the missing-extra warning has been emitted. A 15-link article would
#: otherwise print the same line 15 times, which trains the reader to skip it.
_unavailable_warned = False


def _warn_impersonation_unavailable():
    """Say once, per process, that the escalation tier is not installed.

    This is the line whose absence let the tier sit inert from 2026-08-12 to
    2026-09-06: `curl_cffi` was in no dependency group, so no ``uv run``
    invocation had it, ``impersonating_get`` returned ``None`` every time, and
    the run looked exactly like one where every blocked host simply stayed
    blocked. Nothing anywhere said the attempt had not been made.
    """
    global _unavailable_warned
    if _unavailable_warned:
        return
    _unavailable_warned = True
    log.warning(
        "TLS-impersonation escalation is unavailable: the optional 'unblock' "
        "extra (curl_cffi) is not installed, so blocked links fall straight "
        "through to the archive fallback without an escalation attempt. "
        "Install with: uv sync --extra unblock"
    )


def impersonating_get(url, timeout=30):
    """Fetch ``url`` with a browser TLS fingerprint, or return ``None``.

    The last escalation tier for a link that refuses an honest request. Uses
    ``curl_cffi`` to reproduce Chrome's TLS handshake, which is what Cloudflare
    actually fingerprints — plain ``requests`` is identifiable no matter what
    headers it sends.

    ``None`` when ``curl_cffi`` is not installed (it is an optional extra), when
    the fetch raises, when a hop fails the SSRF guard, or when the response is
    still an error. Callers treat that exactly as they treated the original
    block, so nothing regresses if the dependency is absent.

    Redirects are followed hop by hop with ``_guard`` applied to each, for the
    same reason ``safe_get`` does it: ``allow_redirects=True`` validates the URL
    you pass and then follows an attacker-chosen chain unchecked. That gap was
    tolerable while the only caller recorded a status code, and is not now that
    citation verification checksums this body and hands it to a model — the case
    ``DEFAULT_HEADERS`` above singles out as needing the fail-closed default. A
    refused hop returns ``None`` rather than raising, because every other
    failure here does and callers are written to treat ``None`` as "the block
    held".

    This does not defeat a genuine paywall or a JS/CAPTCHA challenge, and no
    attempt is made to: the three academic publishers in the measurement above
    return a challenge page to this too.

    Whether this tier works at all depends on the installed ``curl_cffi``
    version, because ``impersonate="chrome"`` resolves to whatever
    ``DEFAULT_CHROME`` that release ships and Cloudflare fingerprints stale
    profiles. Measured 2026-09-06: 0.16.0 (``chrome146``) was challenged on
    congress.gov and bianchihonda.com; 0.16.1+ (``chrome150``) read both in
    full. The extra pins a floor for this reason — see
    ``packages/ci-core/pyproject.toml``.

    Debugging a 403 that reaches here: this function returns ``None`` for every
    failure, so check the response headers directly. ``cf-mitigated:
    challenge`` means the fingerprint was rejected and the fix is a newer
    curl_cffi; its absence means the origin refuses browsers too, which is a
    subscription or JS gate and not worth chasing.
    """
    try:
        from curl_cffi import requests as _cffi
    except ImportError:
        _warn_impersonation_unavailable()
        return None
    current = url
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            _guard(current)
            resp = _cffi.get(
                current,
                impersonate="chrome",
                timeout=timeout,
                headers=BROWSER_HEADERS,
                allow_redirects=False,
            )
            if not 300 <= resp.status_code < 400:
                break
            location = resp.headers.get("Location")
            if not location:
                break
            # Relative Locations are legal and common; resolve before validating.
            current = requests.compat.urljoin(current, location)
        else:
            return None
    except Exception:
        return None
    # >= 300 rather than >= 400: the loop also breaks on a 3xx carrying no
    # Location, which is a redirect we cannot follow rather than a document.
    # Handing that back would let a caller checksum a redirect stub and feed it
    # to a model as the source's content.
    if resp.status_code >= 300:
        return None
    return resp
