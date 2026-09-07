"""Unit tests for the litellm shim in ci_core.llm.client.

What is tested here is the seam, not litellm. litellm's own suite covers SSE
framing, provider auth, and error classes; re-testing those through a mock would
only assert that our mock matches our assumptions. What this file covers is
everything that would break silently if the shim mapped something wrong:

  * the two streaming timeouts — the first-byte allowance and the inter-chunk
    stall detector, which need opposite sizes and whose regression looks like
    "the provider got slow" rather than like a bug;
  * ``num_retries=0`` — because litellm calls credit exhaustion a rate limit,
    and its retry loop would sit on a dead account until the run's wall clock
    ran out;
  * OpenAI on ``responses()`` — measured at 79s of total silence on
    ``completion()``, which the read-gap timeout cannot survive;
  * the provider extras the pipeline reads back out (citations, grounding
    chunks, cached tokens, truncation), each of which fails by going quietly
    empty rather than by raising.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from ci_core.llm import client


# ---------------------------------------------------------------------------
# litellm-shaped mock streams
# ---------------------------------------------------------------------------


def _delta(content=None, finish_reason=None):
    return SimpleNamespace(
        delta=SimpleNamespace(content=content), finish_reason=finish_reason
    )


def _usage(prompt=100, completion=50, cached=None, anthropic_cached=None):
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=details,
        cache_read_input_tokens=anthropic_cached,
    )


def _chunk(content=None, finish_reason=None, usage=None, **extras):
    """One streamed chunk. Absent attributes must read as None, as litellm's do."""
    base = {
        "choices": [_delta(content, finish_reason)] if content or finish_reason else [],
        "usage": usage,
        "citations": None,
        "search_results": None,
        "vertex_ai_grounding_metadata": None,
    }
    base.update(extras)
    return SimpleNamespace(**base)


def _completion_stream(
    text='{"flags": []}', finish_reason="stop", usage=None, **extras
):
    """A completion stream that emits ``text`` one chunk at a time, then usage."""
    chunks = [_chunk(content=piece) for piece in text]
    chunks.append(_chunk(finish_reason=finish_reason))
    chunks.append(_chunk(usage=usage if usage is not None else _usage(), **extras))
    return chunks


def _responses_stream(text='{"flags": []}', status="completed", usage=None):
    """An OpenAI Responses API event stream."""
    events = [SimpleNamespace(type="response.created")]
    events.append(
        SimpleNamespace(type="response.reasoning_summary_text.delta", delta="thinking…")
    )
    for piece in text:
        events.append(SimpleNamespace(type="response.output_text.delta", delta=piece))
    events.append(
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=usage if usage is not None else _usage(),
                status=status,
                incomplete_details=None,
            ),
        )
    )
    return events


def _http_error(status, body=""):
    """An exception shaped like the ones litellm raises for HTTP failures."""
    exc = Exception(f"HTTP {status}")
    exc.status_code = status
    exc.response = SimpleNamespace(status_code=status, text=body)
    return exc


def _call(provider="mistral", **kwargs):
    defaults = dict(
        system_prompt="sys",
        user_prompt="user",
        api_key="key",
        retry=False,
        retry_delay=0,
    )
    defaults.update(kwargs)
    return client.call(provider, **defaults)


# ---------------------------------------------------------------------------
# The read-gap timeout
# ---------------------------------------------------------------------------


class TestReadGapTimeout:
    """The read timeout is the gap BETWEEN chunks, not the generation budget.

    Everything about the layer's tolerance for slow models rests on this. If it
    ever silently becomes a total-time bound, healthy long generations start
    dying at the ceiling and it reads like a provider regression.
    """

    @pytest.mark.parametrize(
        "provider,expected",
        [
            ("openai", 120),
            ("mistral", 120),
            ("grok", 120),
            ("claude", 120),
            # Grounded providers search before the first token arrives.
            ("gemini", 160),
            ("perplexity", 160),
        ],
    )
    def test_provider_default_read_gap(self, provider, expected):
        timeout = client._stream_timeout(
            None, client._PROVIDERS[provider]["read_timeout"]
        )
        assert timeout.read == expected

    @pytest.mark.parametrize("provider", client.PROVIDERS)
    def test_stream_read_timeout_override_is_honored(self, provider):
        """The per-model override in presets.yaml must reach the socket.

        Every value in presets.yaml (mistral 200, perplexity 500, gemini 260)
        was set in response to a real production timeout. An override that
        stopped being plumbed through would re-open every one of those.
        """
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream() if provider == "openai" else _completion_stream()

        target = "responses" if provider == "openai" else "completion"
        with patch.object(client.litellm, target, side_effect=_capture):
            _call(provider, provider_config={"stream_read_timeout": 222})

        assert isinstance(seen["timeout"], httpx.Timeout)
        assert seen["timeout"].read == 222, (
            f"{provider} ignored stream_read_timeout — got {seen['timeout'].read}"
        )

    def test_read_gap_is_not_derived_from_timeout_seconds(self):
        """``timeout_seconds`` is the pipeline's wall-clock backstop, not a socket
        timeout. Feeding it to the socket would kill long healthy generations."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("mistral", provider_config={"timeout_seconds": 900})

        assert seen["timeout"].read == 120

    def test_connect_timeout_stays_small_and_constant(self):
        timeout = client._stream_timeout({"stream_read_timeout": 500}, 120)
        assert timeout.connect == 30
        assert timeout.read == 500


# ---------------------------------------------------------------------------
# Call surfaces and routing
# ---------------------------------------------------------------------------


class TestCallSurface:
    def test_openai_uses_responses_not_completion(self):
        """Measured: completion() sends zero bytes for 79s on a reasoning model.

        The read-gap timeout cannot tell that apart from a hung socket, so this
        is a correctness constraint, not a preference.
        """
        with (
            patch.object(
                client.litellm, "responses", return_value=_responses_stream()
            ) as responses,
            patch.object(client.litellm, "completion") as completion,
        ):
            result = _call("openai")

        assert responses.called
        assert not completion.called
        assert result["failed"] is False

    @pytest.mark.parametrize(
        "provider", ["gemini", "mistral", "grok", "claude", "perplexity"]
    )
    def test_other_providers_use_completion(self, provider):
        with (
            patch.object(
                client.litellm, "completion", return_value=_completion_stream()
            ) as completion,
            patch.object(client.litellm, "responses") as responses,
        ):
            _call(provider)

        assert completion.called
        assert not responses.called

    @pytest.mark.parametrize(
        "provider,model,expected",
        [
            ("gemini", "gemini-2.5-pro", "gemini/gemini-2.5-pro"),
            ("grok", "grok-4.3", "xai/grok-4.3"),
            ("claude", "claude-opus-4-8", "anthropic/claude-opus-4-8"),
            ("perplexity", "sonar-pro", "perplexity/sonar-pro"),
            ("mistral", "mistral-large-latest", "mistral/mistral-large-latest"),
            # OpenAI is litellm's default route and takes no prefix.
            ("openai", "gpt-5.4", "gpt-5.4"),
        ],
    )
    def test_model_ids_get_litellm_provider_prefix(self, provider, model, expected):
        assert client._qualified(provider, model) == expected

    def test_already_qualified_model_is_left_alone(self):
        """So an operator can pin an exact litellm route in user.yaml."""
        assert client._qualified("gemini", "vertex_ai/gemini-2.5-pro") == (
            "vertex_ai/gemini-2.5-pro"
        )

    def test_unknown_provider_raises_keyerror(self):
        with pytest.raises(KeyError, match="Unknown provider"):
            _call("not-a-provider")


class TestRetryPolicy:
    def test_num_retries_zero_is_always_passed(self):
        """litellm reports credit exhaustion as a RateLimitError.

        Left to its own retry loop it would treat a dead account as a transient
        limit and burn the wall-clock budget rediscovering that.
        """
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("grok")

        assert seen["num_retries"] == 0

    def test_transient_status_retried_once(self):
        attempts = []

        def _flaky(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise _http_error(429)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_flaky):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 2
        assert result["failed"] is False

    def test_non_transient_status_not_retried(self):
        """Retrying a 401 spends the budget twice to learn the same thing."""
        attempts = []

        def _always_401(**kwargs):
            attempts.append(1)
            raise _http_error(401, '{"error": "invalid api key"}')

        with patch.object(client.litellm, "completion", side_effect=_always_401):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 1
        assert result["failed"] is True

    def test_malformed_json_is_retried_once(self):
        """A call that streamed fine but returned prose gets the same one-retry
        treatment as a dropped socket — the parse failure has no HTTP status of
        its own to land it in _RETRYABLE_STATUS."""
        attempts = []

        def _flaky(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                return _completion_stream("I'd be happy to help!")
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_flaky):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 2
        assert result["failed"] is False

    def test_persistent_malformed_json_still_fails_cleanly(self):
        """Both attempts return prose: same failure shape as the un-retried
        case, not a raised exception."""
        attempts = []

        def _always_prose(**kwargs):
            attempts.append(1)
            return _completion_stream("I'd be happy to help!")

        with patch.object(client.litellm, "completion", side_effect=_always_prose):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 2
        assert result["failed"] is True
        assert result["error"] == "Malformed JSON response"
        assert result["raw"] == "I'd be happy to help!"
        assert result["tokens"] == {"prompt": 100, "completion": 50}


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


class TestResultContract:
    def test_successful_call_shape(self):
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream('{"flags": [{"passage": "a"}]}'),
        ):
            result = _call("mistral", model="mistral-large-latest")

        assert result["failed"] is False
        assert result["data"] == {"flags": [{"passage": "a"}]}
        assert result["raw"] == '{"flags": [{"passage": "a"}]}'
        assert result["model"] == "mistral-large-latest"
        assert result["tokens"] == {"prompt": 100, "completion": 50}
        assert isinstance(result["elapsed_seconds"], float)

    def test_malformed_json_is_a_failure_that_keeps_the_text(self):
        """The review pipeline needs structured findings, so prose is a failure —
        but call_text() rescues the text, so it must survive on ``raw``."""
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream("I'd be happy to help!"),
        ):
            result = _call("mistral")

        assert result["failed"] is True
        assert result["error"] == "Malformed JSON response"
        assert result["raw"] == "I'd be happy to help!"

    def test_missing_usage_does_not_fail_the_call(self):
        """A missing token count must not turn a usable response into a failure."""
        no_usage = [_chunk(content='{"flags": []}'), _chunk(finish_reason="stop")]
        with patch.object(client.litellm, "completion", return_value=no_usage):
            result = _call("mistral")

        assert result["failed"] is False
        assert result["tokens"] == {"prompt": 0, "completion": 0}

    def test_error_body_is_captured(self):
        """A bare 401 status cannot distinguish an invalid key from a revoked one
        or from an account out of credit. Providers put that in the body."""
        with patch.object(
            client.litellm,
            "completion",
            side_effect=_http_error(
                401, '{"error": {"message": "insufficient credit"}}'
            ),
        ):
            result = _call("mistral")

        assert result["failed"] is True
        assert "insufficient credit" in result["error_body"]

    def test_api_key_in_error_url_is_redacted(self):
        """Gemini carries the API key as a URL query parameter, so the key lands
        in the exception text — and from there in a log or a report."""
        exc = _http_error(400, "POST https://x.googleapis.com/v1?key=SECRET123 failed")
        exc.args = ("https://x.googleapis.com/v1beta?key=SECRET123 returned 400",)

        with patch.object(client.litellm, "completion", side_effect=exc):
            result = _call("gemini")

        assert "SECRET123" not in result["error"]
        assert "SECRET123" not in result["error_body"]
        assert "[REDACTED]" in result["error"]


class TestTokens:
    def test_cached_tokens_read_from_details(self):
        """Cached is a subset of prompt, so the fixture keeps it under the
        total — normalize_tokens clamps, and a fixture that ignores that tests
        the clamp rather than the read."""
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream(usage=_usage(prompt=1000, cached=800)),
        ):
            result = _call("grok")

        assert result["tokens"]["cached"] == 800
        assert result["tokens"]["prompt"] == 1000

    def test_nested_details_object_is_not_read_as_absent(self):
        """normalize_tokens tests the details value with isinstance(dict), so a
        details field left as an object reads as no cache hit at all — zero on a
        call that cached most of its prompt. That is the failure this area
        already had once under a different name."""
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=50,
            prompt_tokens_details=SimpleNamespace(cached_tokens=800),
        )
        assert client._read_tokens(usage)["cached"] == 800

    def test_anthropic_cache_key_is_read(self):
        """Anthropic reports cache hits on its own key rather than in the
        details object; litellm passes the name straight through.

        The prompt total has to exceed the cached count, because under litellm
        `prompt_tokens` is the *inclusive* figure — a fixture claiming 640
        cached tokens inside a 100-token prompt describes a call that cannot
        happen, and only passed while the cached value was being added on top
        of the total rather than read out of it.
        """
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream(
                usage=_usage(prompt=1000, anthropic_cached=640)
            ),
        ):
            result = _call("claude")

        assert result["tokens"]["cached"] == 640
        assert result["tokens"]["prompt"] == 1000

    def test_a_cold_call_omits_the_cached_key(self):
        """Matches normalize_tokens: absent means no cache hit, and cost.py
        reads it with .get(). A hardcoded 0 would claim a measurement that
        never happened."""
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            result = _call("mistral")

        assert "cached" not in result["tokens"]


class TestTruncation:
    def test_finish_reason_length_marks_truncated(self):
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream('{"flags": []}', finish_reason="length"),
        ):
            result = _call("mistral")

        assert result["failed"] is False
        assert result["truncated"] is True

    def test_salvage_is_reachable_through_the_shim(self):
        """json_utils' salvage recovers complete array elements from a response
        cut off at the output-token ceiling. litellm has no equivalent, so the
        shim has to route through it — an easy thing to drop and never notice,
        because the call just starts failing as 'malformed JSON'."""
        cut_off = '{"flags": [{"passage": "one"}, {"passage": "two"}, {"pass'
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream(cut_off, finish_reason="length"),
        ):
            result = _call("mistral")

        assert result["failed"] is False
        assert result["truncated"] is True
        assert result["data"] == {"flags": [{"passage": "one"}, {"passage": "two"}]}

    def test_responses_incomplete_maps_to_truncated(self):
        """The Responses API reports the ceiling as an incomplete status with
        reason max_output_tokens, not as finish_reason='length'."""
        events = _responses_stream()
        events[-1].response.status = "incomplete"
        events[-1].response.incomplete_details = SimpleNamespace(
            reason="max_output_tokens"
        )

        with patch.object(client.litellm, "responses", return_value=events):
            result = _call("openai")

        assert result["truncated"] is True

    def test_clean_response_not_marked_truncated(self):
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            result = _call("mistral")

        assert "truncated" not in result


# ---------------------------------------------------------------------------
# Provider extras
# ---------------------------------------------------------------------------


class TestPerplexityExtras:
    def test_citations_and_search_results_surface(self):
        stream = _completion_stream(
            citations=["https://epa.gov/a", "https://nasa.gov/b"],
            search_results=[{"title": "A", "url": "https://epa.gov/a"}],
        )
        with patch.object(client.litellm, "completion", return_value=stream):
            result = _call("perplexity")

        assert result["citations"] == ["https://epa.gov/a", "https://nasa.gov/b"]
        assert result["search_results"] == [{"title": "A", "url": "https://epa.gov/a"}]
        assert result["grounding_available"] is True

    def test_no_citations_reports_grounding_unavailable(self):
        """Section 9 of the report reads this; a silent True would claim
        grounded sources that were never fetched."""
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            result = _call("perplexity")

        assert result["citations"] == []
        assert result["grounding_available"] is False


class TestGeminiGrounding:
    _META = {
        "groundingChunks": [
            {
                "web": {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ1",
                    "title": "epa.gov",
                }
            },
            {
                "web": {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ2",
                    "title": "harvard.edu",
                }
            },
        ]
    }

    def test_grounding_chunks_extracted_in_resolver_shape(self):
        """``[{"uri", "title"}, ...]`` is a contract: resolve_grounding_urls
        consumes exactly this, and every uri is a redirect wrapper that expires
        in ~30 days, so it must be resolved before anything stores one."""
        stream = _completion_stream(vertex_ai_grounding_metadata=[self._META])
        with patch.object(client.litellm, "completion", return_value=stream):
            result = _call("gemini")

        assert result["grounding_chunks"] == [
            {
                "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ1",
                "title": "epa.gov",
            },
            {
                "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ2",
                "title": "harvard.edu",
            },
        ]
        assert result["grounding_available"] is True

    def test_last_non_empty_metadata_wins(self):
        """Verified against a live call: Gemini emits an empty {} metadata
        object on an early chunk and the populated one last. Keeping the first
        non-None value yields no sources at all on a call that really did
        ground — and it fails silently, as an empty list."""
        chunks = _completion_stream()
        chunks.insert(0, _chunk(vertex_ai_grounding_metadata=[{}]))
        chunks.append(_chunk(vertex_ai_grounding_metadata=[self._META]))

        with patch.object(client.litellm, "completion", return_value=chunks):
            result = _call("gemini")

        assert len(result["grounding_chunks"]) == 2

    def test_ungrounded_call_reports_no_chunks(self):
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            result = _call("gemini")

        assert result["grounding_chunks"] == []
        assert result["grounding_available"] is False

    def test_search_grounding_tool_is_requested(self):
        """Without it Gemini answers fact_check from training recall, which is
        the entire reason it is in that ensemble."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("gemini")

        assert seen["tools"] == [{"googleSearch": {}}]


# ---------------------------------------------------------------------------
# Operator-facing guidance
# ---------------------------------------------------------------------------


class TestFallbackChain:
    def test_capacity_error_falls_back_and_says_so(self):
        """A quietly degraded review is worse than a failed one: it still
        produces findings, and they still look authoritative."""
        calls = []

        def _first_model_at_capacity(**kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise _http_error(503, "model overloaded")
            return _completion_stream()

        with patch.object(
            client.litellm, "completion", side_effect=_first_model_at_capacity
        ):
            result = _call("claude", model="claude-opus-4-8")

        assert result["failed"] is False
        assert result["fallback_from"] == "claude-opus-4-8"
        assert result["model"] == "claude-sonnet-4-6"

    def test_successful_primary_reports_no_fallback(self):
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            result = _call("claude", model="claude-opus-4-8")

        assert "fallback_from" not in result

    def test_non_capacity_failure_does_not_walk_the_chain(self):
        """A bad key fails on every model; trying three of them just triples
        the time to the same answer."""
        calls = []

        def _bad_key(**kwargs):
            calls.append(kwargs["model"])
            raise _http_error(401, "invalid key")

        with patch.object(client.litellm, "completion", side_effect=_bad_key):
            result = _call("claude")

        assert len(calls) == 1
        assert result["failed"] is True


class TestMisconfigurationRetry:
    def test_reasoning_rejection_retries_without_it_and_warns(self):
        """The preset asked for a reasoning model and user.yaml overrode it with
        one that cannot reason. Run the domain anyway, but say so — the output
        is degraded and it is the config that needs fixing, not the run."""
        calls = []

        def _reject_reasoning(**kwargs):
            calls.append(kwargs)
            if "reasoning_effort" in kwargs:
                raise _http_error(
                    400,
                    '{"error": {"code": "unknown_parameter", "param": "reasoning"}}',
                )
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_reject_reasoning):
            result = _call("mistral", provider_config={"reasoning_effort": "high"})

        assert result["failed"] is False
        assert "[MISCONFIGURATION]" in result["misconfiguration_warning"]
        assert "reasoning_effort" in result["misconfiguration_warning"]
        assert len(calls) == 2

    def test_unrelated_400_does_not_trigger_the_retry(self):
        calls = []

        def _other_400(**kwargs):
            calls.append(kwargs)
            raise _http_error(400, '{"error": {"message": "context length exceeded"}}')

        with patch.object(client.litellm, "completion", side_effect=_other_400):
            result = _call("mistral", provider_config={"reasoning_effort": "high"})

        assert result["failed"] is True
        assert "misconfiguration_warning" not in result
        assert len(calls) == 1


class TestReasoningParameters:
    def test_openai_requests_reasoning_summary(self):
        """summary="auto" is what makes the thinking phase audible on the wire.
        Without it this surface goes as quiet as Chat Completions, and the whole
        reason for using responses() is gone."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai", provider_config={"reasoning_effort": "xhigh"})

        assert seen["reasoning"] == {"effort": "xhigh", "summary": "auto"}

    def test_openai_without_reasoning_sends_no_temperature(self):
        """gpt-5.x refuses it at any effort, including none.

        This asserted the opposite, against a mock that accepts anything —
        which is how the bug survived. Live standard-preset run, 2026-09-04:
        every openai call with no reasoning effort came back HTTP 400,
        "Unsupported parameter: 'temperature' is not supported with this
        model", in under a second.
        """
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai")

        assert "reasoning" not in seen
        assert "temperature" not in seen

    def test_claude_effort_maps_to_reasoning_effort(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("claude", provider_config={"effort": "high"})

        assert seen["reasoning_effort"] == "high"

    def test_gemini_thinking_budget_is_forwarded(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("gemini", provider_config={"thinking_budget": 16000})

        assert seen["thinking"] == {"type": "enabled", "budget_tokens": 16000}

    def test_unknown_config_keys_are_not_forwarded(self):
        """A stray key reaching a provider is a 400 mid-run, which costs a whole
        domain's review — so the mapping is an allowlist, not a passthrough."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call(
                "mistral",
                provider_config={
                    "model": "mistral-medium-3-5",
                    "timeout_seconds": 300,
                    "stream_read_timeout": 200,
                    "enabled": True,
                    "prompts": ["fact_check"],
                },
            )

        for leaked in ("timeout_seconds", "stream_read_timeout", "enabled", "prompts"):
            assert leaked not in seen


class TestMistralChunkedContent:
    def test_typed_content_chunks_keep_only_text(self):
        """Mistral's reasoning models stream content as a list of typed chunks
        rather than a string; the thinking parts are not part of the answer."""
        chunks = [
            _chunk(
                content=[
                    {"type": "thinking", "text": "Let me consider the schema…"},
                    {"type": "text", "text": '{"flags": []}'},
                ]
            ),
            _chunk(finish_reason="stop"),
            _chunk(usage=_usage()),
        ]
        with patch.object(client.litellm, "completion", return_value=chunks):
            result = _call("mistral")

        assert result["raw"] == '{"flags": []}'
        assert result["data"] == {"flags": []}


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------
#
# These three cases were found by running 25 concurrent calls against real
# providers, not by reading the code. Each one arrives with a status code that
# means something different from what it says.


class TestFailureClassification:
    def test_exhausted_account_is_not_retried(self):
        """Measured on litellm 1.96.2: an OpenAI account with no credits raises
        RateLimitError with status 429 — identical by status to a per-minute
        limit a short wait would clear. Waiting does not refill a wallet."""
        attempts = []

        def _dead_account(**kwargs):
            attempts.append(1)
            raise _http_error(
                429,
                "You have no credits remaining. Add credits to continue using the API.",
            )

        with patch.object(client.litellm, "completion", side_effect=_dead_account):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 1, "retried an exhausted account"
        assert result["failed"] is True

    def test_genuine_rate_limit_is_still_retried(self):
        """The guard must not swallow real rate limits — those do clear."""
        attempts = []

        def _throttled(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise _http_error(429, "Request rate limit exceeded, try again later.")
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_throttled):
            result = _call("perplexity", retry=True, retry_delay=0)

        assert len(attempts) == 2
        assert result["failed"] is False

    def test_dropped_keepalive_connection_is_retried(self):
        """The failure this retry exists for. A cached HTTP client handing back
        a connection the provider already closed surfaces as status 500 with
        WinError 10038; it hit ~10% of same-host calls spaced a second or two
        apart, which is exactly this pipeline's spacing. A new socket fixes it."""
        attempts = []

        def _stale_socket(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise _http_error(
                    500,
                    "[WinError 10038] An operation was attempted on something "
                    "that is not a socket",
                )
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_stale_socket):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 2
        assert result["failed"] is False

    def test_dropped_connection_does_not_walk_the_fallback_chain(self):
        """litellm wraps mid-stream failures in MidStreamFallbackError, which
        synthesises status 503 when the underlying error carries none. Matching
        "503" in the message would read a dropped socket as "model at capacity"
        and silently downgrade to a weaker model over a network blip."""
        calls = []

        def _mid_stream_drop(**kwargs):
            calls.append(kwargs["model"])
            raise _http_error(
                500, "MidStreamFallbackError: APIConnectionError - connection reset"
            )

        with patch.object(client.litellm, "completion", side_effect=_mid_stream_drop):
            result = _call("claude", model="claude-opus-4-8", retry=False)

        assert len(calls) == 1, (
            f"walked the fallback chain on a connection error: {calls}"
        )
        assert result["failed"] is True
        assert "fallback_from" not in result

    def test_internal_status_is_not_leaked_to_callers(self):
        """`_status` is a routing detail; the report renders the result dict."""
        with patch.object(
            client.litellm, "completion", side_effect=_http_error(401, "bad key")
        ):
            failure = _call("mistral")
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            success = _call("mistral")

        for key in ("_status", "_terminal"):
            assert key not in failure
            assert key not in success

    def test_exhausted_account_is_not_retried_when_wrapped_as_503(self):
        """Mid-stream, litellm wraps the same dead account in
        MidStreamFallbackError, which synthesises status 503. Guarding only 429
        lets the wrapped form through and spends a retry_delay per call — 30 of
        them on a maximum run, all to relearn that the wallet is empty."""
        attempts = []

        def _wrapped_dead_account(**kwargs):
            attempts.append(1)
            raise _http_error(
                503,
                "MidStreamFallbackError: litellm.APIError: You have no credits "
                "remaining. Add credits to continue using the API.",
            )

        with patch.object(
            client.litellm, "completion", side_effect=_wrapped_dead_account
        ):
            result = _call("mistral", retry=True, retry_delay=0)

        assert len(attempts) == 1, (
            "a wrapped exhausted-account error was retried or walked the "
            f"fallback chain — {len(attempts)} attempts"
        )
        assert result["failed"] is True

    def test_genuine_503_capacity_error_is_still_retried(self):
        """The guard is content-based, so a real overload must still retry."""
        attempts = []

        def _overloaded(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise _http_error(503, "model is overloaded, try again")
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_overloaded):
            result = _call("claude", retry=True, retry_delay=0)

        assert len(attempts) == 2
        assert result["failed"] is False


class TestTemperature:
    """Anthropic rejects any temperature but 1 on its reasoning models.

    Sending 0.2 to claude-opus-4-8 is a hard 400 — it broke every Claude call in
    the ensemble on the first real run after the migration, in 0.07s, before a
    single token. The adapter this replaced never sent a temperature to
    Anthropic; this keeps that.
    """

    @pytest.mark.parametrize("provider", ["gemini", "mistral", "grok", "perplexity"])
    def test_providers_that_accept_temperature_get_it(self, provider):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call(provider)

        assert seen["temperature"] == 0.2

    def test_claude_is_sent_no_temperature(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("claude", provider_config={"effort": "high"})

        assert "temperature" not in seen, (
            "claude-opus-4-8 rejects temperature != 1 with a 400 before any token"
        )

    def test_openai_reasoning_path_sends_no_temperature(self):
        """temperature is incompatible with reasoning on the Responses API."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai", provider_config={"reasoning_effort": "high"})

        assert "temperature" not in seen


class TestMistralReasoningEscapeHatch:
    """litellm's allowlist is stricter than Mistral's actual API.

    ``reasoning_effort`` on mistral-medium-3-5 raises UnsupportedParamsError
    client-side, in 0.05s, before any request goes out — it failed all five
    domains on the first real maximum-preset run. The parameter is genuinely
    supported: the model accepts "high" and "none" and 400s on "low"/"medium",
    a distinction only the provider could be drawing.
    """

    def test_reasoning_effort_is_allowed_through(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("mistral", provider_config={"reasoning_effort": "high"})

        assert seen["reasoning_effort"] == "high"
        assert seen["allowed_openai_params"] == ["reasoning_effort"]

    def test_no_escape_hatch_when_no_reasoning_requested(self):
        """Nothing to allow through, so nothing is asked for."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("mistral")

        assert "allowed_openai_params" not in seen

    def test_drop_params_stays_off_globally(self):
        """drop_params=True would make this call succeed by silently discarding
        reasoning — a quiet quality regression instead of a loud failure — and
        it is global, so it would mask the next mismatch too."""
        assert client.litellm.drop_params is False


class TestMistralMaxTokens:
    """The default scales with effort instead of a flat cap.

    Measured 2026-08-18 on a maximum-preset run (135514 chars, effort=high):
    4 of 5 mistral domains hit exactly the old 8000-token ceiling and got cut
    off mid-JSON, unrecoverable by salvage. The model's real limit
    (mistral-medium-3-5) is 262144 output tokens — 8000 was self-imposed, not
    a provider constraint.
    """

    def test_default_is_8000_without_reasoning(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("mistral")

        assert seen["max_tokens"] == 8000

    def test_default_raises_to_16000_at_high_effort(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("mistral", provider_config={"reasoning_effort": "high"})

        assert seen["max_tokens"] == 16000

    def test_explicit_max_tokens_overrides_either_default(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call(
                "mistral",
                provider_config={"reasoning_effort": "high", "max_tokens": 24000},
            )

        assert seen["max_tokens"] == 24000


class TestWebSearch:
    """Live search on the Responses API.

    The pipeline resolves `web_search` per domain before calling, because only
    fact_check has any use for it and every search bills. By the time it reaches
    here it is a plain bool.
    """

    def test_search_tool_requested_when_enabled(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai", provider_config={"web_search": True})

        assert seen["tools"] == [{"type": "web_search_preview"}]

    def test_no_search_tool_when_disabled_or_absent(self):
        for cfg in ({"web_search": False}, {}):
            seen = {}

            def _capture(**kwargs):
                seen.update(kwargs)
                return _responses_stream()

            with patch.object(client.litellm, "responses", side_effect=_capture):
                _call("openai", provider_config=cfg)

            assert "tools" not in seen, f"unrequested search tool for cfg={cfg}"

    def test_search_with_reasoning_sends_no_temperature(self):
        """The bug this pairing caused: `web_search` was switched on against a
        payload that hardcoded temperature, and gpt-5.x rejects it outright —
        HTTP 400 on every search call, falling through to the non-search path
        for a warning and a wasted round trip and no live search at all."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call(
                "openai",
                provider_config={"web_search": True, "reasoning_effort": "high"},
            )

        assert "temperature" not in seen
        assert seen["reasoning"] == {"effort": "high", "summary": "auto"}
        assert seen["tools"] == [{"type": "web_search_preview"}]

    def test_search_without_reasoning_sends_no_temperature_either(self):
        """The premise of the old version — "a non-reasoning model accepts it" —
        is not true of gpt-5.x, which is the only family this path serves.

        Determinism on the models that *do* honour a temperature is unaffected:
        they go through the Chat Completions path, which reads
        ``_SENDS_TEMPERATURE`` and still sets one.
        """
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai", provider_config={"web_search": True})

        assert "temperature" not in seen


# ---------------------------------------------------------------------------
# The stall detector
# ---------------------------------------------------------------------------


def _slow_stream(chunks, first_delay=0.0, gap_delay=0.0):
    """A stream that sleeps before the first chunk and between the rest."""

    def _gen():
        if first_delay:
            time.sleep(first_delay)
        for i, chunk in enumerate(chunks):
            if i and gap_delay:
                time.sleep(gap_delay)
            yield chunk

    return _gen()


class TestStallDetector:
    """One socket timeout cannot be both a first-byte allowance and a stall
    detector, and conflating them is what drove perplexity's value to 500s —
    at which point a genuinely dead connection took over eight minutes to
    notice, removing the one property a stall detector exists to provide.

    So: `stream_read_timeout` waits for the stream to *start*, and
    `stream_gap_timeout` catches a started stream that *died*.
    """

    def test_a_stalled_stream_is_caught_by_the_gap_not_the_socket(self):
        """The case the split exists for: a generous first-byte allowance and a
        tight gap. A stream that starts then dies must be caught by the gap."""
        stream = _slow_stream(
            [_chunk(content="{"), _chunk(content='"a": 1}')], gap_delay=0.5
        )
        with pytest.raises(client.StreamStalled, match="mid-stream"):
            list(client._iter_with_gap(stream, first_byte=30.0, gap=0.05))

    def test_a_slow_first_byte_is_allowed_by_the_first_byte_budget(self):
        """A grounded model searches before emitting anything. That silence is
        healthy and must not trip the tight gap."""
        stream = _slow_stream([_chunk(content="ok")], first_delay=0.3)
        received = list(client._iter_with_gap(stream, first_byte=5.0, gap=0.05))
        assert len(received) == 1

    def test_a_stream_that_never_starts_is_caught_before_the_first_chunk(self):
        stream = _slow_stream([_chunk(content="ok")], first_delay=5.0)
        with pytest.raises(client.StreamStalled, match="before the first chunk"):
            list(client._iter_with_gap(stream, first_byte=0.05, gap=30.0))

    def test_a_healthy_stream_passes_every_chunk_through_in_order(self):
        chunks = [_chunk(content=c) for c in "hello"]
        received = list(client._iter_with_gap(_slow_stream(chunks), 5.0, 5.0))
        assert [c.choices[0].delta.content for c in received] == list("hello")

    def test_a_reader_exception_reaches_the_consumer(self):
        """A provider error raised mid-iteration must not be swallowed by the
        reader thread and reported as a stall."""

        def _explodes():
            yield _chunk(content="{")
            raise RuntimeError("provider blew up")

        with pytest.raises(RuntimeError, match="provider blew up"):
            list(client._iter_with_gap(_explodes(), 5.0, 5.0))

    def test_a_stall_is_retried(self):
        """A stall means the connection died, and a new socket fixes that —
        the same reasoning that makes a dropped keepalive worth one retry."""
        attempts = []

        def _stall_once(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                # Two chunks: the gap only elapses *between* them, so a
                # single-chunk stream would end cleanly and never stall.
                return _slow_stream(
                    [_chunk(content="{"), _chunk(content='"a": 1}')], gap_delay=5.0
                )
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_stall_once):
            result = _call(
                "mistral",
                retry=True,
                retry_delay=0,
                provider_config={"stream_gap_timeout": 0.05},
            )

        assert len(attempts) == 2
        assert result["failed"] is False

    def test_gap_default_and_override(self):
        assert client._gap_timeout(None) == 60
        assert client._gap_timeout({}) == 60
        assert client._gap_timeout({"stream_gap_timeout": 15}) == 15

    def test_gap_is_independent_of_the_first_byte_allowance(self):
        """perplexity's 500s first-byte allowance must not become a 500s stall
        detector — that is the regression the split undoes."""
        cfg = {"stream_read_timeout": 500}
        assert client._stream_timeout(cfg, 160).read == 500
        assert client._gap_timeout(cfg) == 60


class TestFirstByteEndsOnRealOutput:
    """Framing events must not start the stall clock.

    The Responses API emits `response.created`, `response.in_progress` and
    `response.output_item.added` inside the first second — before the model has
    thought at all. Treating "first chunk" as "first progress" starts the tight
    gap clock against the reasoning phase, which is exactly what the generous
    first-byte allowance exists to cover.

    This passed every unit test and an isolated live call (1.3s worst gap). It
    failed on the pipeline's six concurrent xhigh calls against a real draft,
    where every OpenAI domain died on a 60s gap.
    """

    def test_responses_framing_events_do_not_end_the_first_byte_phase(self):
        def _stream():
            yield SimpleNamespace(type="response.created")
            yield SimpleNamespace(type="response.in_progress")
            yield SimpleNamespace(type="response.output_item.added")
            # The model now thinks for longer than the gap allows.
            time.sleep(0.3)
            yield SimpleNamespace(
                type="response.reasoning_summary_text.delta", delta="…"
            )

        received = list(
            client._iter_with_gap(
                _stream(),
                first_byte=5.0,
                gap=0.05,
                is_progress=client._responses_is_progress,
            )
        )
        assert len(received) == 4

    def test_a_stall_after_real_output_is_still_caught(self):
        """The predicate must not disable the detector, only delay its start."""

        def _stream():
            yield SimpleNamespace(type="response.created")
            yield SimpleNamespace(
                type="response.reasoning_summary_text.delta", delta="a"
            )
            time.sleep(5)
            yield SimpleNamespace(type="response.output_text.delta", delta="b")

        with pytest.raises(client.StreamStalled, match="mid-stream"):
            list(
                client._iter_with_gap(
                    _stream(), 5.0, 0.05, client._responses_is_progress
                )
            )

    def test_completion_role_only_chunk_is_not_progress(self):
        """Providers open with a role-only delta carrying no content."""
        role_only = _chunk()
        role_only.choices = [
            SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason=None)
        ]
        assert client._completion_is_progress(role_only) is False
        assert client._completion_is_progress(_chunk(content="hi")) is True

    def test_completion_finish_and_usage_count_as_progress(self):
        """A stream whose only remaining chunks are the terminator and usage has
        plainly not stalled."""
        assert client._completion_is_progress(_chunk(finish_reason="stop")) is True
        assert client._completion_is_progress(_chunk(usage=_usage())) is True

    def test_openai_keeps_its_pre_migration_liveness_bound(self):
        """Not a looser number, the same one.

        Before the stall detector existed, OpenAI's only liveness bound was its
        socket read timeout, which httpx applies per read — an effective 120s
        gap. gpt-5.5 at xhigh exceeds 60s between reasoning-summary deltas once
        six of them run at once, so a 60s detector is *tighter* than what the
        provider had and failed two domains a run.
        """
        assert client._gap_timeout(None, "openai") == 120
        for provider in ("gemini", "mistral", "grok", "claude", "perplexity"):
            assert client._gap_timeout(None, provider) == 60

    def test_a_per_model_override_beats_the_provider_default(self):
        assert client._gap_timeout({"stream_gap_timeout": 15}, "openai") == 15


class TestStructuredOutput:
    """`response_format: json_object` makes the provider guarantee JSON.

    The migration dropped it. Nothing went red, because json_utils quietly
    absorbed the difference — a fenced or prose-wrapped answer still parsed, and
    a genuinely malformed one looked like the model's fault. The only signal was
    that the parameter had stopped being sent, which no test was watching.
    """

    def _sent(self, provider, cfg=None):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call(provider, provider_config=cfg or {})
        return seen.get("response_format")

    _JSON = {"type": "json_object"}

    def test_grok_always_asks_for_json(self):
        """The old adapter put it in the base payload, unconditionally."""
        assert self._sent("grok") == self._JSON
        assert self._sent("grok", {"reasoning_effort": "high"}) == self._JSON

    def test_mistral_asks_for_json_only_when_not_reasoning(self):
        """Mirrors the adapter exactly: one or the other, never both."""
        assert self._sent("mistral") == self._JSON
        assert self._sent("mistral", {"reasoning_effort": "high"}) is None

    @pytest.mark.parametrize("provider", ["perplexity", "gemini", "claude"])
    def test_providers_that_cannot_take_it_are_not_sent_it(self, provider):
        """Each checked individually rather than generalised from grok — a first
        pass at this audit claimed five of six providers had lost the parameter,
        and only one had."""
        assert self._sent(provider) is None

    def test_openai_is_not_sent_it_either(self):
        """The Responses API has no response_format; the old adapter only sent
        one on the Azure Chat Completions path, which this shim does not use."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai")

        assert "response_format" not in seen

    def test_salvage_still_backs_it_up(self):
        """A provider guarantee is not a ceiling guarantee. A response cut off
        at the output limit is still truncated JSON, so json_utils stays in the
        path rather than being retired on the strength of this."""
        cut_off = '{"flags": [{"passage": "one"}, {"pass'
        with patch.object(
            client.litellm,
            "completion",
            return_value=_completion_stream(cut_off, finish_reason="length"),
        ):
            result = _call("grok")

        assert result["failed"] is False
        assert result["truncated"] is True
        assert result["data"] == {"flags": [{"passage": "one"}]}


class TestOptInWebSearch:
    """Live search for grok and claude, where it is an opt-in parameter.

    Distinct from TestWebSearch above, which covers OpenAI's Responses API
    tool. Same capability, three different spellings across three providers.

    Proven real on 2026-08-16 rather than assumed: asked for the newest litellm
    release on PyPI, both grok and claude answered "I do not know" without the
    parameter and returned a cited, correct version with it. An earlier check
    looked inconclusive only because it asked something both models knew from
    training, and because claude's citations arrive somewhere the shim was not
    reading.
    """

    def _sent(self, provider, cfg):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call(provider, provider_config=cfg)
        return seen

    @pytest.mark.parametrize("provider", ["grok", "claude"])
    def test_search_is_requested_when_enabled(self, provider):
        sent = self._sent(provider, {"web_search": True})
        assert sent["web_search_options"] == {"search_context_size": "medium"}

    @pytest.mark.parametrize("provider", ["grok", "claude"])
    def test_no_search_parameter_when_disabled(self, provider):
        assert "web_search_options" not in self._sent(provider, {})
        assert "web_search_options" not in self._sent(provider, {"web_search": False})

    def test_context_size_is_overridable(self):
        sent = self._sent("grok", {"web_search": True, "search_context_size": "high"})
        assert sent["web_search_options"]["search_context_size"] == "high"

    @pytest.mark.parametrize("provider", ["perplexity", "gemini"])
    def test_always_searching_providers_are_not_sent_the_parameter(self, provider):
        """sonar always searches, and gemini's search is a tool set elsewhere.

        Sending it would be either a no-op or a conflict, and it would imply the
        capability is off when the flag is absent — which for these two is
        exactly backwards.
        """
        assert "web_search_options" not in self._sent(provider, {"web_search": True})


class TestSearchMetadataReachesTheCaller:
    def test_citations_from_provider_specific_fields_are_read(self):
        """Anthropic tucks citations into provider_specific_fields rather than
        exposing them as a chunk attribute. Reading only the top-level name
        reported zero citations for a search that had in fact run — which is
        how claude's search first looked like it did nothing."""
        chunks = _completion_stream()
        chunks.append(
            _chunk(provider_specific_fields={"citations": ["https://epa.gov/a"]})
        )
        with patch.object(client.litellm, "completion", return_value=chunks):
            result = _call("claude", provider_config={"web_search": True})

        assert result["citations"] == ["https://epa.gov/a"]
        assert result["grounding_available"] is True

    def test_grounding_is_keyed_off_the_payload_not_the_provider_name(self):
        """A newly-grounded provider should surface without another branch."""
        chunks = _completion_stream()
        chunks.append(_chunk(citations=["https://nasa.gov/x"]))
        with patch.object(client.litellm, "completion", return_value=chunks):
            result = _call("grok", provider_config={"web_search": True})

        assert result["grounding_available"] is True

    def test_a_model_that_searched_and_found_nothing_says_so(self):
        """grounding_available must reflect what came back, not what was asked
        for — an empty search is not the same as a grounded answer."""
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream()
        ):
            result = _call("grok", provider_config={"web_search": True})

        assert result.get("grounding_available") is not True


class TestCacheBreakpoint:
    """Anthropic caches nothing unless told where the cacheable part ends.

    Measured 2026-08-16 on claude-opus-4-8, the same 5,426-token prefix twice:
    with a cache_control marker, call 1 wrote 5,412 tokens and call 2 read them
    back; without one, both calls cached zero. Everyone else caches implicitly
    and gets a plain string, because complicating a request that already works
    buys nothing.
    """

    def _sent(self, provider, prefix, remainder="TASK"):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call(provider, user_prompt=prefix + remainder, cache_prefix=prefix)
        return seen["messages"][-1]["content"]

    def test_claude_gets_a_marked_breakpoint(self):
        content = self._sent("claude", "ARTICLE")
        assert isinstance(content, list)
        assert content[0]["text"] == "ARTICLE"
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[1]["text"] == "TASK"
        assert "cache_control" not in content[1]

    @pytest.mark.parametrize("provider", ["grok", "mistral", "perplexity", "gemini"])
    def test_implicit_cachers_get_a_plain_string(self, provider):
        assert self._sent(provider, "ARTICLE") == "ARTICLETASK"

    def test_no_prefix_means_no_restructuring(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("claude", user_prompt="whole thing")

        assert seen["messages"][-1]["content"] == "whole thing"

    def test_a_prefix_that_does_not_match_is_ignored_rather_than_guessed(self):
        """A caller that hands over a prefix the prompt does not start with has
        a bug; splitting anyway would cache a span nobody chose."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("claude", user_prompt="actual prompt", cache_prefix="something else")

        assert seen["messages"][-1]["content"] == "actual prompt"

    def test_openai_gets_a_routing_key_derived_from_the_prefix(self):
        """Not enablement — OpenAI caches anyway. The key steers concurrent
        calls at the same warm prefix instead of each missing on a cold one."""
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _responses_stream()

        with patch.object(client.litellm, "responses", side_effect=_capture):
            _call("openai", user_prompt="ARTICLETASK", cache_prefix="ARTICLE")

        assert seen["prompt_cache_key"].startswith("ci-review-")

    def test_the_routing_key_is_stable_per_article_and_differs_across_them(self):
        from ci_core.llm import cache as cache_mod

        one = cache_mod.as_request_params("openai", "article one")
        one_again = cache_mod.as_request_params("openai", "article one")
        two = cache_mod.as_request_params("openai", "article two")
        assert one == one_again
        assert one != two

    def test_the_routing_key_does_not_carry_the_article(self):
        from ci_core.llm import cache as cache_mod

        key = cache_mod.as_request_params("openai", "secret article text")[
            "prompt_cache_key"
        ]
        assert "secret" not in key


class TestNarrowedPunctuationRepair:
    """Perplexity narrows General Punctuation to its low byte, so U+2019 lands
    in the response as a C0 control character. It arrives by two different
    routes and each needs its own repair point — see ci_core.text_repair.

    Search-result snippets come out of litellm's parse of the provider
    envelope, so they carry a real control character and are repaired at the
    stream boundary. The model's own answer JSON carries an *escape* instead,
    which is still plain text at that point and only becomes a control
    character when the answer is parsed.
    """

    def test_search_result_snippets_are_repaired_at_the_stream(self):
        stream = _completion_stream(
            citations=["https://example.org/a"],
            search_results=[
                {"title": "A", "snippet": "the site" + chr(0x19) + "s owner"}
            ],
        )
        with patch.object(client.litellm, "completion", return_value=stream):
            result = _call("perplexity")

        assert result["search_results"][0]["snippet"] == "the site’s owner"

    def test_answer_json_is_repaired_through_the_parse(self):
        body = '{"flags": ["Laboratory' + chr(92) + 'u0019s congressional report"]}'
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream(text=body)
        ):
            result = _call("perplexity")

        assert result["data"]["flags"] == ["Laboratory’s congressional report"]

    def test_a_clean_response_is_left_alone(self):
        body = '{"flags": ["nothing — wrong here"]}'
        with patch.object(
            client.litellm, "completion", return_value=_completion_stream(text=body)
        ):
            result = _call("perplexity")

        assert result["data"]["flags"] == ["nothing — wrong here"]


class TestOpenAIGroundingIsDetectedFromSearchEvents:
    """A grounded openai call reported itself ungrounded.

    Measured 2026-09-04 against the live API: `web_search` on, 8,661 prompt
    tokens consumed, the right answer returned — and `grounding_available:
    None`, `citations: []`. Two separate reasons:

    * OpenAI streams through `litellm.responses`, whose consumer hardcoded
      `citations: []`. A first attempt at this read annotations off the *Chat
      Completions* chunks, a path openai never takes.
    * Even in the right place there was nothing to read. OpenAI attaches
      `url_citation` annotations to cited spans of prose, and every review
      domain asks for a JSON schema, so there is no prose to cite. The
      annotations array comes back empty on a search that demonstrably ran.

    What the stream does carry is `response.web_search_call.*`. Those events
    hold an item_id and a sequence number — no query, no URLs — but they answer
    the question `grounding_available` actually asks: did this call consult
    live sources.
    """

    class _Event:
        def __init__(self, type_, **kw):
            self.type = type_
            for k, v in kw.items():
                setattr(self, k, v)

    def _drain(self, events):
        return client._consume_responses_stream(iter(events), 60, 60)

    def test_a_search_event_marks_the_call_grounded(self):
        out = self._drain(
            [
                self._Event("response.web_search_call.in_progress"),
                self._Event("response.web_search_call.completed"),
                self._Event("response.output_text.delta", delta='{"ok":true}'),
            ]
        )
        assert out["web_search_used"] is True

    def test_no_search_event_leaves_it_unset(self):
        out = self._drain(
            [self._Event("response.output_text.delta", delta='{"ok":true}')]
        )
        assert out["web_search_used"] is False

    def test_the_event_type_is_read_as_its_wire_value(self):
        """`type` is a str-subclass enum: `==` against the dotted name works,
        but `str()` renders it "ResponsesAPIStreamEvents.WEB_SEARCH_CALL_..."
        — which is how a first version of this silently detected nothing."""
        import enum

        class _Kind(str, enum.Enum):
            SEARCHING = "response.web_search_call.searching"

        out = self._drain([self._Event(_Kind.SEARCHING)])
        assert out["web_search_used"] is True

    def test_annotations_are_still_read_when_the_provider_sends_any(self):
        part = self._Event(
            "part", annotations=[{"type": "url_citation", "url": "https://eia.gov/x"}]
        )
        out = self._drain([self._Event("response.content_part.done", part=part)])
        assert out["citations"] == ["https://eia.gov/x"]

    def test_both_consumers_return_the_same_keys(self):
        """So `_extras_from` needs no per-surface branch."""
        responses = set(self._drain([]))
        completion = set(client._consume_completion_stream(iter([]), 60, 60))
        assert "web_search_used" in responses
        assert "web_search_used" in completion


class TestUrlCitationExtraction:
    def test_dict_annotations(self):
        assert client._url_citations(
            [{"type": "url_citation", "url": "https://eia.gov/a"}]
        ) == ["https://eia.gov/a"]

    def test_object_annotations(self):
        class _Ann:
            type = "url_citation"
            url = "https://eia.gov/a"

        assert client._url_citations([_Ann()]) == ["https://eia.gov/a"]

    def test_an_annotation_with_no_url_is_not_a_citation(self):
        assert client._url_citations([{"type": "file_citation", "file_id": "f"}]) == []

    def test_nothing_at_all_is_handled(self):
        assert client._url_citations(None) == []


class TestOpenAINeverReceivesTemperature:
    """gpt-5.x rejects `temperature` outright, at any reasoning effort.

    The Responses-API path sent one whenever no reasoning effort was set, under
    a comment that correctly said the model would refuse it. `_SENDS_TEMPERATURE`
    has excluded openai all along; that path never consulted it.

    The maximum preset masked this — openai runs at xhigh there and took the
    other branch — so it only showed on economy, standard and balanced, where
    every openai call died with HTTP 400 in under a second. Found on a live
    standard run, 2026-09-04.
    """

    def test_openai_is_not_in_the_temperature_allowlist(self):
        assert "openai" not in client._SENDS_TEMPERATURE

    def test_the_responses_path_sends_no_temperature_without_an_effort(self):
        import inspect

        source = inspect.getsource(client)
        start = source.index("litellm.responses(**kwargs)")
        window = source[max(0, start - 3000) : start]
        assert 'kwargs["temperature"]' not in window, (
            "the Responses-API path sets a temperature again — gpt-5.x returns "
            "HTTP 400 for it regardless of reasoning effort"
        )

    def test_a_provider_that_accepts_one_still_gets_it(self):
        """The fix must not strip temperature from the providers that want it."""
        for provider in ("gemini", "mistral", "grok", "perplexity"):
            assert provider in client._SENDS_TEMPERATURE, provider


class TestClaudeOutputCeiling:
    """`claude` had a flat 4096-token cap that config could not raise.

    Measured 2026-09-05 on a standard-preset run (135514 chars, effort=none):
    `claude:argument_integrity` stopped at exactly 4096 output tokens and came
    back PARTIAL, salvage keeping the complete findings and discarding the rest,
    while every other provider finished. `cfg["max_tokens"]` was ignored on all
    three branches, so there was no way to raise it without editing the client.
    """

    def _seen(self, **provider_config):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return _completion_stream()

        with patch.object(client.litellm, "completion", side_effect=_capture):
            _call("claude", provider_config=provider_config)
        return seen

    def test_the_no_effort_default_clears_the_measured_truncation(self):
        assert self._seen()["max_tokens"] == 8000

    def test_high_effort_keeps_its_larger_budget(self):
        assert self._seen(effort="high")["max_tokens"] == 16000

    def test_a_thinking_budget_still_leaves_room_for_the_answer(self):
        """The budget is spent before the response starts, so the ceiling has to
        exceed it or the model thinks and then has nothing left to answer with."""
        seen = self._seen(thinking_budget=10000)
        assert seen["max_tokens"] == 14096
        assert seen["thinking"]["budget_tokens"] == 10000

    def test_config_can_raise_the_ceiling_on_every_branch(self):
        """The point of the fix: a domain that still truncates is now a config
        change rather than a client edit."""
        assert self._seen(max_tokens=32000)["max_tokens"] == 32000
        assert self._seen(effort="high", max_tokens=32000)["max_tokens"] == 32000
        assert (
            self._seen(thinking_budget=10000, max_tokens=32000)["max_tokens"] == 32000
        )

    def test_the_default_stays_well_under_the_provider_ceiling(self):
        """claude-haiku-4-5 reports max_output_tokens 64000. Raising the default
        to the ceiling would trade one silent failure for a cost surprise."""
        assert self._seen()["max_tokens"] <= 16000
