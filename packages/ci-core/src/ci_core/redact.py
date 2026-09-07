"""Shared secret-redaction helpers.

Some provider APIs (notably Gemini AI Studio) carry the API key as a URL query
parameter.  When a network-level exception fires, the requests library embeds
the full URL — key included — in the exception string.  Printing that string
to the terminal or a log file leaks the credential.  These helpers scrub keys
out before any error text is surfaced.
"""

import re

# The sensitive word a credential parameter ends with. Hyphenated spellings are
# not hypothetical: Azure OpenAI names its parameter `api-key` and Google sends
# `X-Goog-Api-Key`. An underscore-only list matched neither, nor
# `client_secret`, `refresh_token`, `subscription-key`, `password`, `auth` or
# `sig` — eight of twelve real-world spellings tested on 2026-09-04 passed
# straight through into whatever log or report the error text reached.
_SENSITIVE_WORD = (
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token"
    r"|client[-_]?secret|subscription[-_]?key|session[-_]?key"
    r"|key|token|secret|password|passwd|credentials?|auth|signature|sig)"
)

# Any dash/underscore-separated prefix, then that word, then `=`. Anchoring the
# end on `=` is what leaves innocent parameters alone: `author=` cannot match,
# because `auth` would have to consume the whole name and `or` is left over.
# `keywords=` survives for the same reason.
_PARAM_NAME = rf"(?:[A-Za-z0-9]+[-_])*{_SENSITIVE_WORD}"

# Matches ?key=..., &apiKey=..., &api-key=... in a URL and replaces the value.
# Stops at the next &, whitespace, quote, or closing bracket so we don't eat
# the rest of the message.
_KEY_QUERY_RE = re.compile(
    rf"([?&]{_PARAM_NAME}=)[^&\s'\"<>)\]]+",
    re.IGNORECASE,
)


def redact_url_keys(text):
    """Replace key/token query-parameter values in any string with [REDACTED].

    Provider-agnostic: works even when the key value isn't available to compare
    against, because it matches on the parameter name.
    """
    return _KEY_QUERY_RE.sub(r"\1[REDACTED]", str(text))


def redact_value(text, secret):
    """Replace a known secret value with [REDACTED] wherever it appears."""
    if secret and secret in str(text):
        return str(text).replace(secret, "[REDACTED]")
    return str(text)


def truncate_excerpt(text, head=2000, tail=500):
    """Truncate long text to a head/tail excerpt with an omitted-chars marker.

    Used to persist a diagnosable slice of a raw provider response (e.g. a
    malformed-JSON failure) without bloating the report with the full payload.
    Text at or under ``head + tail`` chars is returned unchanged.
    """
    text = str(text)
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted} chars omitted]...\n{text[-tail:]}"


def mask_secret(value, head=8, tail=4):
    """Mask a secret for display, keeping just enough of both ends (the
    provider prefix and a few trailing characters) to recognize which key it
    is, with everything in between redacted.

    Values too short to reveal ``head + tail`` characters without exposing
    most of the secret are masked completely instead.
    """
    if not value:
        return "(not set)"
    value = str(value)
    if len(value) < head + tail + 4:
        return "*" * len(value)
    return f"{value[:head]}...{value[-tail:]}"


def capture_error_body(exc):
    """Return a redacted, truncated excerpt of an HTTP error response body.

    ``raise_for_status()`` raises with only the bare status line (e.g. "401
    Client Error: Unauthorized for url: ..."), never the response body — so a
    401 gives no way to tell an invalid key from a revoked one, insufficient
    credits, or a suspended account. Most providers put that distinction in
    the JSON error body. ``exc`` is expected to be a ``requests.HTTPError``
    (or anything else carrying a ``.response``); returns "" if no body is
    available, e.g. a ``Timeout``/``ConnectionError`` with no response.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        text = resp.text
    except Exception:
        return ""
    if not text:
        return ""
    return truncate_excerpt(redact_url_keys(text))
