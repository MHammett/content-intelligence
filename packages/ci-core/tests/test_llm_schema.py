"""Tests for ci_core.llm.schema — putting a response schema on the wire.

The module had no test file of its own and sat at 29% line coverage, which is
thin for the thing that decides whether a provider is obliged to return the
shape the caller asked for. Everything below guards a failure that is silent:
the call succeeds, the JSON parses, and the keys are simply not the ones
downstream reads.
"""

from ci_core.llm.schema import SCHEMA_CAPABLE, as_request_params, supports_schema

_SCHEMA = {"type": "object", "properties": {"flags": {"type": "array"}}}


class TestSupportsSchema:
    def test_every_configured_provider_is_capable(self):
        for provider in ("openai", "grok", "mistral", "perplexity", "claude"):
            assert supports_schema(provider)

    def test_an_unknown_provider_is_not(self):
        assert not supports_schema("some-new-provider")

    def test_gemini_loses_schema_support_only_when_grounded(self):
        """The one real exception, and it is a provider-side 400 rather than a
        soft degrade: googleSearch and a schema request cannot coexist."""
        assert supports_schema("gemini")
        assert not supports_schema("gemini", grounded=True)

    def test_grounding_disqualifies_nobody_else(self):
        for provider in ("openai", "grok", "mistral", "perplexity", "claude"):
            assert supports_schema(provider, grounded=True), provider

    def test_the_capable_set_and_the_predicate_agree(self):
        for provider in SCHEMA_CAPABLE:
            assert supports_schema(provider)


class TestAsRequestParams:
    def test_openai_uses_text_format_not_response_format(self):
        """The failure this guards is silent. The Responses API accepts
        `response_format` and ignores it, so a call built the other way
        succeeds, returns parseable JSON, and enforces nothing."""
        params = as_request_params("openai", "review", _SCHEMA)
        assert "response_format" not in params
        fmt = params["text"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["name"] == "review"
        assert fmt["schema"] == _SCHEMA
        assert fmt["strict"] is True

    def test_other_providers_use_response_format(self):
        for provider in ("grok", "mistral", "perplexity", "claude"):
            params = as_request_params(provider, "review", _SCHEMA)
            assert "text" not in params, provider
            js = params["response_format"]["json_schema"]
            assert js["schema"] == _SCHEMA, provider
            assert js["name"] == "review", provider
            assert js["strict"] is True, provider

    def test_a_provider_that_cannot_take_one_gets_an_empty_dict(self):
        """Empty rather than None so callers can merge unconditionally instead
        of branching at every call site."""
        assert as_request_params("some-new-provider", "review", _SCHEMA) == {}

    def test_grounded_gemini_gets_an_empty_dict(self):
        assert as_request_params("gemini", "review", _SCHEMA, grounded=True) == {}

    def test_ungrounded_gemini_gets_a_schema(self):
        params = as_request_params("gemini", "review", _SCHEMA)
        assert params["response_format"]["json_schema"]["schema"] == _SCHEMA

    def test_strict_is_always_requested_where_it_is_offered(self):
        """`strict` is the difference between a schema the model treats as a
        suggestion and one the API enforces."""
        for provider in ("openai", "grok", "mistral", "perplexity", "claude", "gemini"):
            params = as_request_params(provider, "review", _SCHEMA)
            blob = params.get("text", {}).get("format") or params[
                "response_format"
            ].get("json_schema")
            assert blob["strict"] is True, provider
