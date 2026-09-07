"""Unit tests for ci-article-review's own adapters and modules."""

import json
import pytest
import requests
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# SSE streaming mock helpers
# ---------------------------------------------------------------------------
# The review adapters POST with stream=True and consume the response via
# resp.iter_lines(). These helpers build mock responses that yield provider-shaped
# SSE `data:` lines so the accumulators in ci_core/llm/streaming.py parse them
# exactly as they would a live stream.


def _sse_mock(lines, status=200):
    """A mock streaming response whose iter_lines() yields the given SSE lines."""
    mock = MagicMock()
    mock.status_code = status
    mock.raise_for_status = MagicMock()
    mock.close = MagicMock()
    # Return a fresh iterator each call so a retry that re-reads still works.
    mock.iter_lines.side_effect = lambda *a, **k: iter(list(lines))
    return mock


def _error_mock(status, body_text):
    """A mock response whose raise_for_status() raises HTTPError with a body.

    Mirrors what ``requests`` does for a real 4xx/5xx: the response object is
    attached to the exception via ``.response`` (still readable via ``.text``
    since nothing has consumed the stream yet), and ``raise_for_status()``'s own
    message is just the bare status line — the body only comes from ``.text``.
    """
    mock = MagicMock()
    mock.status_code = status
    mock.text = body_text
    mock.close = MagicMock()

    def _raise():
        raise requests.exceptions.HTTPError(
            f"{status} Client Error: for url: https://example.invalid", response=mock
        )

    mock.raise_for_status.side_effect = _raise
    return mock


def _split(text, n=3):
    """Split text into roughly n non-empty chunks to exercise delta accumulation."""
    if not text:
        return [""]
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)]


def _sse_chat_lines(content, usage=None, finish_reason="stop", extras=None, n=3):
    """OpenAI-compatible chat-completions stream (OpenAI/Grok/Mistral/Perplexity)."""
    text = content if isinstance(content, str) else json.dumps(content)
    lines = []
    for piece in _split(text, n):
        lines.append(
            "data: "
            + json.dumps({"choices": [{"index": 0, "delta": {"content": piece}}]})
        )
        lines.append("")
    final = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    final["usage"] = (
        usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 50}
    )
    if extras:
        final.update(extras)
    lines.append("data: " + json.dumps(final))
    lines.append("data: [DONE]")
    return lines


def _sse_responses_lines(content, usage=None, reasoning_deltas=None, n=3):
    """OpenAI Responses API stream (openai.com default + web_search paths).

    ``reasoning_deltas``, if given, emits ``response.reasoning_summary_text.delta``
    events before the answer deltas — reasoning models stream these during the
    silent "thinking" phase, ahead of any output text.
    """
    text = content if isinstance(content, str) else json.dumps(content)
    lines = []
    for piece in reasoning_deltas or []:
        lines.append(
            "data: "
            + json.dumps(
                {"type": "response.reasoning_summary_text.delta", "delta": piece}
            )
        )
        lines.append("")
    for piece in _split(text, n):
        lines.append(
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": piece})
        )
        lines.append("")
    lines.append(
        "data: "
        + json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "usage": usage
                    if usage is not None
                    else {"input_tokens": 100, "output_tokens": 50}
                },
            }
        )
    )
    lines.append("data: [DONE]")
    return lines


def _sse_anthropic_lines(content, input_tokens=100, output_tokens=50, n=3):
    """Anthropic /v1/messages stream."""
    text = content if isinstance(content, str) else json.dumps(content)
    lines = [
        "event: message_start",
        "data: "
        + json.dumps(
            {
                "type": "message_start",
                "message": {
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}
                },
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        "",
    ]
    for piece in _split(text, n):
        lines.append(
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                }
            )
        )
        lines.append("")
    lines.append(
        "data: "
        + json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": output_tokens},
            }
        )
    )
    lines.append("data: " + json.dumps({"type": "message_stop"}))
    return lines


def _sse_gemini_lines(content, prompt_tokens=120, completion_tokens=60, n=3):
    """Gemini streamGenerateContent?alt=sse stream."""
    text = content if isinstance(content, str) else json.dumps(content)
    lines = []
    for piece in _split(text, n):
        lines.append(
            "data: "
            + json.dumps({"candidates": [{"content": {"parts": [{"text": piece}]}}]})
        )
        lines.append("")
    lines.append(
        "data: "
        + json.dumps(
            {
                "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": prompt_tokens,
                    "candidatesTokenCount": completion_tokens,
                },
            }
        )
    )
    return lines


# ---------------------------------------------------------------------------
# LanguageTool adapter
# ---------------------------------------------------------------------------


class TestLanguageTool:
    def _make_match(self, offset, length, category_id, replacement, rule_id="RULE"):
        return {
            "offset": offset,
            "length": length,
            "message": "Test message",
            "rule": {"id": rule_id, "category": {"id": category_id}},
            "replacements": [{"value": replacement}],
            "context": {"text": "context text"},
        }

    def test_apply_corrections_auto_apply(self):
        from ci_article_review.adapters.grammar.languagetool import apply_corrections

        text = "Teh quick brown fox"
        matches = [self._make_match(0, 3, "TYPOS", "The")]
        corrected, log = apply_corrections(text, matches, {"TYPOS"}, set())
        assert corrected == "The quick brown fox"
        assert len(log) == 1
        assert log[0]["original"] == "Teh"
        assert log[0]["replacement"] == "The"

    def test_apply_corrections_suppressed(self):
        from ci_article_review.adapters.grammar.languagetool import apply_corrections

        text = "Running fast."
        matches = [self._make_match(0, 7, "SENTENCE_FRAGMENT", "Run")]
        corrected, log = apply_corrections(
            text, matches, {"TYPOS"}, {"SENTENCE_FRAGMENT"}
        )
        assert corrected == text
        assert len(log) == 0

    def test_apply_corrections_not_auto_apply_category(self):
        from ci_article_review.adapters.grammar.languagetool import apply_corrections

        text = "Some text here."
        matches = [self._make_match(0, 4, "STYLE", "Different")]
        corrected, log = apply_corrections(text, matches, {"TYPOS"}, set())
        assert corrected == text
        assert len(log) == 0

    def test_apply_corrections_multiple_reverse_order(self):
        from ci_article_review.adapters.grammar.languagetool import apply_corrections

        text = "Teh cat sat on teh mat"
        matches = [
            self._make_match(0, 3, "TYPOS", "The"),
            self._make_match(15, 3, "TYPOS", "the"),
        ]
        corrected, log = apply_corrections(text, matches, {"TYPOS"}, set())
        assert "Teh" not in corrected
        assert "teh" not in corrected

    def test_run_languagetool_failure(self):
        from ci_article_review.adapters.grammar.languagetool import run

        lt_config = {"auto_apply": ["TYPOS"], "flag_for_review": [], "suppress": []}
        with patch(
            "ci_article_review.adapters.grammar.languagetool.check_text",
            side_effect=Exception("API down"),
        ):
            result = run("Test text", lt_config, "user@example.com", "key", retry=False)
        assert result["failed"] is True
        assert result["corrected_text"] == "Test text"
        assert "elapsed_seconds" in result

    def test_run_languagetool_success(self):
        from ci_article_review.adapters.grammar.languagetool import run

        lt_config = {
            "auto_apply": ["TYPOS"],
            "flag_for_review": ["STYLE"],
            "suppress": [],
        }
        mock_response = {
            "matches": [
                {
                    "offset": 0,
                    "length": 3,
                    "message": "Spelling",
                    "rule": {"id": "SPELL", "category": {"id": "TYPOS"}},
                    "replacements": [{"value": "The"}],
                    "context": {"text": "Teh quick"},
                }
            ]
        }
        with patch(
            "ci_article_review.adapters.grammar.languagetool.check_text",
            return_value=mock_response,
        ):
            result = run("Teh quick", lt_config, "user@example.com", "key")
        assert result["failed"] is False
        assert result["corrected_text"] == "The quick"
        assert len(result["change_log"]) == 1
        assert "elapsed_seconds" in result

    def test_run_passes_custom_url_through(self):
        """A self-hosted server URL reaches check_text, not the public default."""
        from ci_article_review.adapters.grammar.languagetool import run

        lt_config = {"auto_apply": [], "flag_for_review": [], "suppress": []}
        with patch(
            "ci_article_review.adapters.grammar.languagetool.check_text",
            return_value={"matches": []},
        ) as mock_check:
            run(
                "Some text",
                lt_config,
                None,
                None,
                url="http://localhost:8010/v2/check",
            )
        mock_check.assert_called_once_with(
            "Some text", None, None, url="http://localhost:8010/v2/check"
        )

    def test_check_text_omits_auth_when_not_supplied(self):
        """A self-hosted server takes no auth — don't send empty credentials."""
        from ci_article_review.adapters.grammar.languagetool import check_text

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"matches": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.Session.post", return_value=mock_resp) as mock_post:
            check_text("Some text", None, None, url="http://localhost:8010/v2/check")
        _, kwargs = mock_post.call_args
        assert "username" not in kwargs["data"]
        assert "apiKey" not in kwargs["data"]
        assert mock_post.call_args[0][0] == "http://localhost:8010/v2/check"

    def test_check_text_includes_auth_when_supplied(self):
        from ci_article_review.adapters.grammar.languagetool import check_text

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"matches": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.Session.post", return_value=mock_resp) as mock_post:
            check_text("Some text", "user@example.com", "key")
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["username"] == "user@example.com"
        assert kwargs["data"]["apiKey"] == "key"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_invalid_publication_name_raises(self):
        from ci_article_review.config_loader import validate_publication_name

        with pytest.raises(ValueError, match="Invalid publication name"):
            validate_publication_name("../../etc/shadow")

    def test_invalid_publication_name_with_slash(self):
        from ci_article_review.config_loader import validate_publication_name

        with pytest.raises(ValueError):
            validate_publication_name("my/blog")

    def test_valid_publication_name(self):
        from ci_article_review.config_loader import validate_publication_name

        validate_publication_name("my-blog")
        validate_publication_name("myblog")
        validate_publication_name("my_blog_2024")


# ---------------------------------------------------------------------------
# History / slug safety
# ---------------------------------------------------------------------------


class TestHistory:
    def test_slug_strips_special_chars(self):
        from ci_article_review.history import _slug

        assert "/" not in _slug("My Article: Part 1/2")
        assert ":" not in _slug("My Article: Part 1/2")

    def test_slug_windows_reserved_names(self):
        from ci_article_review.history import _slug

        for reserved in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT9"):
            result = _slug(reserved)
            assert result.lower() not in {
                "con",
                "prn",
                "aux",
                "nul",
                "com1",
                "com2",
                "com3",
                "com4",
                "com5",
                "com6",
                "com7",
                "com8",
                "com9",
                "lpt1",
                "lpt2",
                "lpt3",
                "lpt4",
                "lpt5",
                "lpt6",
                "lpt7",
                "lpt8",
                "lpt9",
            }, f"Reserved name {reserved!r} was not escaped — got {result!r}"

    def test_slug_path_traversal_neutralized(self):
        from ci_article_review.history import _slug

        result = _slug("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result


# ---------------------------------------------------------------------------
# History — write error handling
# ---------------------------------------------------------------------------


class TestHistoryWriteErrors:
    def test_report_write_failure_returns_none_paths(self, tmp_path):
        import ci_article_review.history as hist

        report = {"article_title": "Test Article", "run_number": 1}
        with patch("builtins.open", side_effect=OSError("disk full")):
            paths = hist.save_run(str(tmp_path), "Test Article", 1, report, [])
        assert paths["report_path"] is None
        assert paths["corrections_path"] is None

    def test_corrections_write_failure_returns_report_path(self, tmp_path):
        import ci_article_review.history as hist

        report = {"article_title": "Test Article", "run_number": 1}
        call_count = {"n": 0}
        real_open = open

        def selective_fail(path, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 4:  # fourth open is the corrections log
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=selective_fail):
            paths = hist.save_run(str(tmp_path), "Test Article", 1, report, [])
        assert paths["report_path"] is not None
        assert paths["corrections_path"] is None

    def test_worklist_write_failure_returns_other_paths(self, tmp_path):
        import ci_article_review.history as hist

        report = {"article_title": "Test Article", "run_number": 1}
        call_count = {"n": 0}
        real_open = open

        def selective_fail(path, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:  # third open is the worklist
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=selective_fail):
            paths = hist.save_run(str(tmp_path), "Test Article", 1, report, [])
        assert paths["report_path"] is not None
        assert paths["markdown_path"] is not None
        assert paths["corrections_path"] is not None
        assert paths["worklist_path"] is None

    def test_markdown_write_failure_returns_other_paths(self, tmp_path):
        import ci_article_review.history as hist

        report = {"article_title": "Test Article", "run_number": 1}
        call_count = {"n": 0}
        real_open = open

        def selective_fail(path, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:  # second open is the markdown review
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=selective_fail):
            paths = hist.save_run(str(tmp_path), "Test Article", 1, report, [])
        assert paths["report_path"] is not None
        assert paths["corrections_path"] is not None
        assert paths["markdown_path"] is None


# ---------------------------------------------------------------------------
# WordPress — term ID resolution
# ---------------------------------------------------------------------------


class TestWordPressTermResolution:
    def _mock_term_response(self, term_id, slug):
        mock = MagicMock()
        mock.ok = True
        mock.json.return_value = [{"id": term_id, "slug": slug}]
        mock.raise_for_status = MagicMock()
        return mock

    def _mock_empty_response(self):
        mock = MagicMock()
        mock.ok = True
        mock.json.return_value = []
        mock.raise_for_status = MagicMock()
        return mock

    def test_integer_ids_pass_through(self):
        from ci_article_review.adapters.cms.wordpress import _lookup_term_ids

        result, unresolved = _lookup_term_ids(
            "http://site.com/wp-json/wp/v2", {}, "categories", [5, 12]
        )
        assert result == [5, 12]
        assert unresolved == []

    def test_slug_resolved_to_id(self):
        from ci_article_review.adapters.cms.wordpress import _lookup_term_ids

        with patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get:
            mock_get.return_value = self._mock_term_response(42, "data-centers")
            result, unresolved = _lookup_term_ids(
                "http://site.com/wp-json/wp/v2", {}, "categories", ["data-centers"]
            )
        assert result == [42]
        assert unresolved == []

    def test_unknown_slug_omitted_with_warning(self):
        from ci_article_review.adapters.cms.wordpress import _lookup_term_ids

        with patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get:
            mock_get.return_value = self._mock_empty_response()
            result, unresolved = _lookup_term_ids(
                "http://site.com/wp-json/wp/v2", {}, "tags", ["nonexistent-tag"]
            )
        assert result == []
        # Returned, not just logged — the caller has to be able to act on it.
        assert unresolved == ["nonexistent-tag"]

    def test_mixed_ids_and_slugs(self):
        from ci_article_review.adapters.cms.wordpress import _lookup_term_ids

        with patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get:
            mock_get.return_value = self._mock_term_response(99, "energy")
            result, _unresolved = _lookup_term_ids(
                "http://site.com/wp-json/wp/v2", {}, "tags", [7, "energy"]
            )
        assert 7 in result
        assert 99 in result

    def test_single_string_accepted(self):
        from ci_article_review.adapters.cms.wordpress import _lookup_term_ids

        with patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get:
            mock_get.return_value = self._mock_term_response(3, "tech")
            result, _unresolved = _lookup_term_ids(
                "http://site.com/wp-json/wp/v2", {}, "categories", "tech"
            )
        assert result == [3]

    def test_empty_input_returns_empty(self):
        from ci_article_review.adapters.cms.wordpress import _lookup_term_ids

        assert _lookup_term_ids(
            "http://site.com/wp-json/wp/v2", {}, "categories", []
        ) == ([], [])
        assert _lookup_term_ids(
            "http://site.com/wp-json/wp/v2", {}, "categories", None
        ) == ([], [])


# ---------------------------------------------------------------------------
# Handoff parser — empty next_headers fix
# ---------------------------------------------------------------------------


class TestHandoffParser:
    def test_last_section_extracted(self):
        from ci_article_review.handoff_parser import _extract_section

        doc = "HEADER ONE\nfirst content\n\nHEADER TWO\nlast content here"
        result = _extract_section(doc, "HEADER TWO", next_headers=None)
        assert result == "last content here"

    def test_middle_section_extracted(self):
        from ci_article_review.handoff_parser import _extract_section

        doc = "HEADER ONE\nfirst content\n\nHEADER TWO\nmiddle content\n\nHEADER THREE\nthird content"
        result = _extract_section(doc, "HEADER TWO", next_headers=["HEADER THREE"])
        assert "middle content" in result
        assert "third content" not in result

    def test_missing_required_field_logs_warning(self):
        """Missing title/primary_claim/draft should log a warning, not crash."""
        from ci_article_review.handoff_parser import parse_draft_submission

        # Minimal doc with no Article: field and no DRAFT section
        doc = "DRAFT SUBMISSION HANDOFF\n\nPRIMARY CLAIM\nSome claim\n\nDRAFT\nArticle text here."
        with patch("ci_article_review.handoff_parser.log") as mock_log:
            result = parse_draft_submission(doc)
        # title is empty — should have warned
        warning_messages = [str(call) for call in mock_log.warning.call_args_list]
        assert any("Article:" in m for m in warning_messages)
        # draft was present — should not have warned about it
        assert result["draft"] == "Article text here."

    def test_missing_draft_logs_warning(self):
        from ci_article_review.handoff_parser import parse_draft_submission

        doc = "Article: My Article\n\nPRIMARY CLAIM\nSome claim here\n\n"
        with patch("ci_article_review.handoff_parser.log") as mock_log:
            result = parse_draft_submission(doc)
        warning_messages = [str(call) for call in mock_log.warning.call_args_list]
        assert any("DRAFT" in m for m in warning_messages)
        assert result["draft"] == ""


# ---------------------------------------------------------------------------
# check.py — provider dispatch
# ---------------------------------------------------------------------------


class TestCheckProviderDispatch:
    def _ok_response(self, body):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = body
        mock.raise_for_status = MagicMock()
        return mock

    def test_check_openai_calls_correct_url(self):
        import ci_article_review.check as check

        resp = self._ok_response({"choices": [{"message": {"content": "ok"}}]})
        with patch(
            "ci_article_review.check.requests.post", return_value=resp
        ) as mock_post:
            check.check_openai("key", "gpt-4o")
        url = mock_post.call_args[0][0]
        assert "api.openai.com" in url
        assert "gpt-4o" in str(mock_post.call_args)

    def test_check_openai_azure_uses_api_key_header(self):
        import ci_article_review.check as check

        resp = self._ok_response({"choices": [{"message": {"content": "ok"}}]})
        cfg = {
            "endpoint": "https://res.openai.azure.com",
            "deployment": "my-dep",
            "model": "gpt-4o",
        }
        with patch(
            "ci_article_review.check.requests.post", return_value=resp
        ) as mock_post:
            check.check_openai_azure("key", cfg)
        headers = mock_post.call_args[1]["headers"]
        assert "api-key" in headers
        assert "Authorization" not in headers
        url = mock_post.call_args[0][0]
        assert "my-dep" in url

    def test_check_openai_azure_missing_endpoint_raises(self):
        import ci_article_review.check as check

        with pytest.raises(Exception, match="endpoint"):
            check.check_openai_azure("key", {"deployment": "dep"})

    def test_check_mistral_azure_uses_bearer_auth(self):
        import ci_article_review.check as check

        resp = self._ok_response({"choices": [{"message": {"content": "ok"}}]})
        cfg = {
            "endpoint": "https://Mistral-abc.eastus2.inference.ai.azure.com",
            "model": "mistral-large",
        }
        with patch(
            "ci_article_review.check.requests.post", return_value=resp
        ) as mock_post:
            check.check_mistral_azure("key", cfg)
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer key"

    def test_check_claude_uses_x_api_key_header(self):
        import ci_article_review.check as check

        resp = self._ok_response({"content": [{"type": "text", "text": "ok"}]})
        with patch(
            "ci_article_review.check.requests.post", return_value=resp
        ) as mock_post:
            result = check.check_claude("key", "claude-opus-4-5")
        headers = mock_post.call_args[1]["headers"]
        assert headers["x-api-key"] == "key"
        assert "replied" in result
