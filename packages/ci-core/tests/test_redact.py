"""Tests for redact — secret scrubbing before error output."""

from ci_core.redact import mask_secret, redact_url_keys, redact_value, truncate_excerpt


class TestRedactUrlKeys:
    def test_redacts_gemini_key_query_param(self):
        err = (
            "HTTPSConnectionPool(host='generativelanguage.googleapis.com', "
            "port=443): url: /v1beta/models?key=AIzaSyABC_REALKEY123 (Caused by ...)"
        )
        out = redact_url_keys(err)
        assert "AIzaSyABC_REALKEY123" not in out
        assert "key=[REDACTED]" in out

    def test_redacts_apikey_variants(self):
        for param in ("apiKey", "api_key", "access_token", "token"):
            err = f"https://api.example.com/x?{param}=SECRETVALUE&foo=bar"
            out = redact_url_keys(err)
            assert "SECRETVALUE" not in out, f"{param} not redacted"
            assert "foo=bar" in out, "should not eat following params"

    def test_preserves_non_key_text(self):
        msg = "Connection timed out after 20s"
        assert redact_url_keys(msg) == msg

    def test_accepts_exception_object(self):
        exc = Exception("failed: https://x.com/m?key=ABC123")
        out = redact_url_keys(exc)
        assert "ABC123" not in out

    def test_case_insensitive_param_name(self):
        err = "https://x.com/m?KEY=SECRET"
        assert "SECRET" not in redact_url_keys(err)


class TestRedactValue:
    def test_redacts_known_value(self):
        assert (
            redact_value("error: sk-abc123 failed", "sk-abc123")
            == "error: [REDACTED] failed"
        )

    def test_no_op_when_value_absent(self):
        assert redact_value("clean message", "sk-abc123") == "clean message"

    def test_empty_secret_is_noop(self):
        assert redact_value("anything", "") == "anything"


class TestTruncateExcerpt:
    def test_short_text_unchanged(self):
        assert truncate_excerpt("short raw response") == "short raw response"

    def test_long_text_truncated_with_head_and_tail(self):
        text = "A" * 3000 + "B" * 3000
        out = truncate_excerpt(text, head=2000, tail=500)
        assert out.startswith("A" * 2000)
        assert out.endswith("B" * 500)
        assert "chars omitted" in out
        assert len(out) < len(text)

    def test_exactly_at_boundary_unchanged(self):
        text = "X" * 2500
        assert truncate_excerpt(text, head=2000, tail=500) == text


class TestMaskSecret:
    def test_default_head_and_tail_lengths(self):
        key = "sk-proj-abcdefghijklmnopqrstuvwxyz1234"
        assert mask_secret(key) == "sk-proj-...1234"

    def test_never_leaks_a_substring_from_the_middle(self):
        key = "sk-proj-" + "x" * 40 + "kA12"
        out = mask_secret(key)
        assert out.startswith("sk-proj-")
        assert out.endswith("kA12")
        assert key[10:-4] not in out

    def test_short_value_is_fully_masked_not_partially_revealed(self):
        out = mask_secret("shortkey")
        assert out == "*" * len("shortkey")
        assert "short" not in out
        assert "key" not in out

    def test_empty_string_reports_not_set(self):
        assert mask_secret("") == "(not set)"

    def test_none_reports_not_set(self):
        assert mask_secret(None) == "(not set)"

    def test_custom_head_and_tail(self):
        assert mask_secret("abcdefghijklmnop", head=2, tail=2) == "ab...op"


class TestHyphenatedCredentialParameters:
    """The parameter-name list missed the spellings real providers use.

    This module exists so a network error carrying a URL cannot put a
    credential into a terminal, a log file or a saved report. It matched
    `key`, `apikey`, `api_key`, `access_token` and `token` — and nothing
    hyphenated. Azure OpenAI names its parameter `api-key`; Google sends
    `X-Goog-Api-Key`. Eight of twelve real spellings tested on 2026-09-04
    passed through untouched.
    """

    SECRET_URLS = [
        "https://api.example.com/v1?key=SEKRIT",
        "https://api.example.com/v1?api_key=SEKRIT",
        "https://api.example.com/v1?apiKey=SEKRIT",
        "https://api.example.com/v1?api-key=SEKRIT",
        "https://api.example.com/v1?subscription-key=SEKRIT",
        "https://api.example.com/v1?X-Goog-Api-Key=SEKRIT",
        "https://api.example.com/v1?access_token=SEKRIT",
        "https://api.example.com/v1?refresh_token=SEKRIT",
        "https://api.example.com/v1?client_secret=SEKRIT",
        "https://api.example.com/v1?password=SEKRIT",
        "https://api.example.com/v1?auth=SEKRIT",
        "https://api.example.com/v1?sig=SEKRIT",
        "https://api.example.com/v1?signature=SEKRIT",
        "https://api.example.com/v1?a=1&session-key=SEKRIT&b=2",
    ]

    #: Parameters that merely contain a sensitive substring. Anchoring the match
    #: on `=` is what keeps these readable — `auth` cannot swallow `author`.
    INNOCENT_URLS = [
        "https://example.com/posts?author=mike",
        "https://example.com/search?keywords=grid",
        "https://example.com/list?category=news",
        "https://example.com/feed?signal=on",
    ]

    def test_every_credential_spelling_is_redacted(self):
        for url in self.SECRET_URLS:
            assert "SEKRIT" not in redact_url_keys(url), url
            assert "[REDACTED]" in redact_url_keys(url), url

    def test_innocent_parameters_are_left_alone(self):
        for url in self.INNOCENT_URLS:
            assert redact_url_keys(url) == url, url

    def test_the_rest_of_the_query_string_survives(self):
        got = redact_url_keys("https://x/v1?a=1&api-key=SEKRIT&b=2")
        assert got == "https://x/v1?a=1&api-key=[REDACTED]&b=2"

    def test_an_error_body_carrying_a_hyphenated_key_is_scrubbed(self):
        body = "Unauthorized for url: https://x/v1?api-key=SEKRIT&model=gpt"
        assert "SEKRIT" not in redact_url_keys(body)
