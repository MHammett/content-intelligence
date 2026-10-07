"""Perplexity's Agent API, the "agent" surface of ci_core.llm.client.

Perplexity retired Sonar Chat Completions ("supported until September 27,
2026") in favour of its Agent API. An id with a vendor prefix
(``perplexity/sonar``) goes there, through Perplexity's own SDK; a bare
``sonar*`` id still goes to Sonar through litellm.

Three layers, like the rest of the client's tests:

* the SDK stubbed at ``client._perplexity_agent``, the seam tests patch the way
  they patch ``client.litellm``, for how a stream is read;
* ``httpx.Client.send`` replaced, so the SDK's own request building and stream
  parsing run in full and nothing leaves the machine, for what is sent;
* a real stream, captured from the live API on 2026-09-19, replayed through
  both. It carries ``"truncation": ""``, which a stream built from the docs
  does not, and which crashes the litellm this repo pins at its last event.
  The 45 search-result ``snippet`` strings (excerpts of other people's pages)
  were replaced with a placeholder; every other byte is as recorded.
"""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ci_core.llm import client, cost

_CAPTURE = (
    Path(__file__).parent / "fixtures" / "perplexity_agent" / "stream_2026-09-19.sse"
)

#: The schema the capture was requested with: the prompt never named these
#: keys, and the answer carried exactly them.
_CAPTURE_SCHEMA = {
    "name": "claim_check",
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string"},
            "latest_version_found": {"type": "string"},
            "pages_consulted": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "latest_version_found", "pages_consulted"],
        "additionalProperties": False,
    },
}


def _snippets(node):
    """Every ``snippet`` value anywhere under a decoded event."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "snippet":
                yield value
            else:
                yield from _snippets(value)
    elif isinstance(node, list):
        for value in node:
            yield from _snippets(value)


def test_the_capture_holds_no_text_from_other_peoples_pages():
    """The capture is committed to a public repository, and a search result's
    snippet is an excerpt of someone else's page. A fresh capture must have them
    replaced before it is checked in, not only the one that is there now."""
    found = []
    for block in _CAPTURE.read_text(encoding="utf-8").split("\n\n"):
        lines = block.split("\n")
        if len(lines) > 1 and lines[0].startswith("event:"):
            found.extend(_snippets(json.loads(lines[1].removeprefix("data: "))))
    assert found, "the capture has no search results left to check"
    assert set(found) == {"[third-party snippet removed]"}


def _call(model="perplexity/sonar", retry=False, **provider_config):
    return client.call(
        "perplexity",
        "SYSTEM",
        "USER",
        "key",
        retry=retry,
        retry_delay=0,
        model=model,
        provider_config=provider_config,
        response_schema=_CAPTURE_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Canned events, for the stubbed seam
# ---------------------------------------------------------------------------


def _delta(text, index=1):
    return SimpleNamespace(
        type="response.output_text.delta", delta=text, output_index=index
    )


def _message(text):
    return {
        "type": "message",
        "id": "m",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _results(*urls, queries=("q",)):
    return {
        "type": "search_results",
        "results": [
            {"id": i, "url": u, "title": f"T{i}", "snippet": "s", "source": "web"}
            for i, u in enumerate(urls, 1)
        ],
        "queries": list(queries),
    }


def _completed(output, usage="default", status="completed"):
    if usage == "default":
        usage = {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_tokens_details": {"cached_tokens": 0},
            "cost": {"currency": "USD", "total_cost": 0.0123},
            "tool_calls_details": {"search_web": {"invocation": 2}},
        }
    response = {"status": status, "model": "perplexity/sonar", "output": output}
    if usage is not None:
        response["usage"] = usage
    return SimpleNamespace(type="response.completed", response=response)


def _stub(monkeypatch, *streams):
    """Answer each call to the seam with the next stream; record the params."""
    sent = []
    queue = list(streams)

    def _agent(api_key, timeout, params):
        sent.append(params)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(client, "_perplexity_agent", _agent)
    return sent


# ---------------------------------------------------------------------------
# Which surface
# ---------------------------------------------------------------------------


class TestRouting:
    def test_a_sonar_id_still_goes_to_litellm(self, monkeypatch):
        """Sonar keeps working exactly as it did until it is switched off."""
        seen = {}

        def _completion(**kwargs):
            seen.update(kwargs)
            choice = SimpleNamespace(
                delta=SimpleNamespace(content='{"a": 1}'), finish_reason="stop"
            )
            return [SimpleNamespace(choices=[choice], usage=None)]

        def _agent(*args, **kwargs):
            raise AssertionError("a Sonar id reached the Agent API")

        monkeypatch.setattr(client.litellm, "completion", _completion)
        monkeypatch.setattr(client, "_perplexity_agent", _agent)
        result = _call(model="sonar-reasoning-pro")

        assert result["failed"] is False
        assert seen["model"] == "perplexity/sonar-reasoning-pro"
        assert result["searches"] == 1  # Sonar's per-request fee, unchanged

    def test_an_agent_id_never_reaches_litellm(self, monkeypatch):
        def _completion(**kwargs):
            raise AssertionError("an Agent API id went to litellm")

        monkeypatch.setattr(client.litellm, "completion", _completion)
        _stub(monkeypatch, [_completed([_message('{"a": 1}')])])
        result = _call()

        assert result["failed"] is False
        assert result["data"] == {"a": 1}

    @pytest.mark.parametrize(
        "model, agent",
        [
            ("perplexity/sonar", True),
            ("perplexity/kimi-k3", True),
            ("openai/gpt-5.6-luna", True),
            ("sonar", False),
            ("sonar-reasoning-pro", False),
        ],
    )
    def test_a_vendor_prefix_is_what_decides(self, model, agent):
        """No Sonar id has a slash, so the two sets cannot overlap."""
        assert client._uses_agent_api("perplexity", model) is agent
        assert client._uses_agent_api("openai", model) is False


# ---------------------------------------------------------------------------
# What is sent: the SDK's own request, captured at the transport
# ---------------------------------------------------------------------------


@pytest.fixture
def wire(monkeypatch):
    """Answer every request with the captured real stream; record them all."""
    sent = []

    def _send(http_client, request, *args, **kwargs):
        sent.append(
            {
                "url": str(request.url),
                "body": json.loads(request.content),
                "stream": kwargs.get("stream"),
            }
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=_CAPTURE.read_bytes(),
        )

    monkeypatch.setattr(httpx.Client, "send", _send)
    return sent


class TestRequestOnTheWire:
    def test_a_plain_request(self, wire):
        result = _call()

        assert result["failed"] is False, result
        (request,) = wire
        assert request["url"] == "https://api.perplexity.ai/v1/responses"
        assert request["stream"] is True
        body = request["body"]
        assert body["model"] == "perplexity/sonar"
        assert body["instructions"] == "SYSTEM"
        assert body["input"] == "USER"
        assert body["stream"] is True
        # Not kept for retrieval: the draft is unpublished work.
        assert body["store"] is False
        assert body["tools"] == [{"type": "web_search"}]
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "claim_check",
                "schema": _CAPTURE_SCHEMA["schema"],
                "strict": True,
            },
        }
        # Each left to the model unless the config names it.
        for key in ("temperature", "reasoning", "max_output_tokens", "max_steps"):
            assert key not in body, key

    def test_the_search_controls_move_onto_the_tool(self, wire):
        _call(
            search_domain_filter=["energy.gov"],
            search_recency_filter="month",
            search_context_size="high",
        )
        (tool,) = wire[0]["body"]["tools"]
        assert tool == {
            "type": "web_search",
            "filters": {
                "search_domain_filter": ["energy.gov"],
                "search_recency_filter": "month",
            },
            "search_context_size": "high",
        }

    def test_search_mode_has_no_equivalent_and_says_so_once(
        self, wire, monkeypatch, caplog
    ):
        monkeypatch.setattr(client, "_agent_warnings_given", set())
        _call(search_mode="academic")
        _call(search_mode="academic")

        assert "search_mode" not in json.dumps(wire[0]["body"])
        said = [r for r in caplog.records if "search_mode" in r.getMessage()]
        assert len(said) == 1

    def test_the_tuning_a_config_names_is_sent(self, wire):
        _call(reasoning_effort="medium", max_steps=5, max_tokens=12000, temperature=0.3)
        body = wire[0]["body"]
        assert body["reasoning"] == {"effort": "medium"}
        assert body["max_steps"] == 5
        assert body["max_output_tokens"] == 12000
        assert body["temperature"] == 0.3

    def test_an_anthropic_model_gets_the_ceiling_it_requires(self, wire):
        """The API answers 400 for an anthropic/* model without one."""
        _call(model="anthropic/claude-sonnet-5")
        assert wire[0]["body"]["max_output_tokens"] == 16000

    def test_the_sdk_does_not_retry_on_its_own(self, monkeypatch):
        """Retries are ours. The SDK's default of two would bill attempts the
        cost summary never saw."""
        sends = []

        def _send(http_client, request, *args, **kwargs):
            sends.append(request)
            return httpx.Response(
                429,
                request=request,
                json={"error": {"message": "Too many requests", "type": "rate_limit"}},
            )

        monkeypatch.setattr(httpx.Client, "send", _send)
        result = _call()

        assert result["failed"] is True
        assert len(sends) == 1


# ---------------------------------------------------------------------------
# The real stream
# ---------------------------------------------------------------------------


class TestTheCapturedStream:
    def test_it_is_the_stream_that_breaks_litellm(self):
        """litellm 1.96.2's Literal["auto", "disabled"] rejects this, leaves
        the response a dict, and its logging hook raises on it (1.103.0 no
        longer does). If a new capture replaces this one, keep that."""
        assert b'"truncation":""' in _CAPTURE.read_bytes()

    def test_everything_the_pipeline_reads_comes_back(self, wire):
        result = _call()

        assert result["failed"] is False, result
        assert set(result["data"]) == {
            "verdict",
            "latest_version_found",
            "pages_consulted",
        }
        assert result["tokens"] == {"prompt": 5520, "completion": 152}
        assert result["searches"] == 1
        assert result["grounding_available"] is True
        assert result["provider_cost_usd"] == pytest.approx(0.00426)
        assert len(result["citations"]) == 15
        assert result["citations"][0] == "https://pypi.org/project/litellm/"
        # Sonar's shape, which the capture and the report already read.
        first = result["search_results"][0]
        assert first["url"] == "https://pypi.org/project/litellm/"
        assert {"title", "url", "snippet"} <= set(first)
        assert "id" not in first

    def test_the_table_prices_it_at_what_perplexity_billed(self, wire):
        """pricing.yaml's rows, checked against the bill on the response."""
        result = _call()
        entry = cost.call_log_entry("perplexity:fact_check", result)
        summary = cost.calculate([entry])

        assert summary["pricing_known"] is True
        assert summary["by_pass"][0]["total_usd"] == pytest.approx(
            result["provider_cost_usd"]
        )
        assert entry["provider_cost_usd"] == pytest.approx(0.00426)


# ---------------------------------------------------------------------------
# Reading a stream
# ---------------------------------------------------------------------------


class TestReadingTheStream:
    def test_the_last_message_is_the_answer(self, monkeypatch):
        """One output item per step: a note written before a second search
        must not be spliced in front of the JSON."""
        _stub(
            monkeypatch,
            [
                _delta("Let me check that.", index=0),
                _delta('{"a": 2}', index=2),
                _completed(
                    [
                        _message("Let me check that."),
                        _results("https://a.example"),
                        _message('{"a": 2}'),
                    ]
                ),
            ],
        )
        assert _call()["data"] == {"a": 2}

    def test_a_stream_cut_short_still_reports_what_it_found(self, monkeypatch):
        """No final response: the deltas are the answer, the search events the
        sources, and with no usage the search count is unknown, not zero."""
        _stub(
            monkeypatch,
            [
                SimpleNamespace(
                    type="response.reasoning.search_results",
                    results=[{"url": "https://a.example", "title": "A"}],
                ),
                _delta('{"a": 3}'),
            ],
        )
        result = _call()

        assert result["data"] == {"a": 3}
        assert result["citations"] == ["https://a.example"]
        assert result["grounding_available"] is True
        assert result["searches"] is None
        assert result["tokens"]["prompt"] == 0

    def test_no_search_is_zero_searches_and_ungrounded(self, monkeypatch):
        usage = {"input_tokens": 900, "output_tokens": 40, "total_tokens": 940}
        _stub(monkeypatch, [_completed([_message('{"a": 1}')], usage=usage)])
        result = _call()

        assert result["searches"] == 0
        assert result["grounding_available"] is False
        assert result["citations"] == []

    def test_every_search_invocation_is_counted(self, monkeypatch):
        _stub(
            monkeypatch,
            [_completed([_results("https://a.example"), _message('{"a": 1}')])],
        )
        assert _call()["searches"] == 2

    def test_sources_are_deduplicated_across_searches_and_fetches(self, monkeypatch):
        fetched = {
            "type": "fetch_url_results",
            "contents": [{"url": "https://c.example", "title": "C", "snippet": "x"}],
        }
        _stub(
            monkeypatch,
            [
                _completed(
                    [
                        _results("https://a.example", "https://b.example"),
                        _results("https://b.example"),
                        fetched,
                        _message('{"a": 1}'),
                    ]
                )
            ],
        )
        result = _call()
        assert result["citations"] == [
            "https://a.example",
            "https://b.example",
            "https://c.example",
        ]
        assert [r["url"] for r in result["search_results"]] == result["citations"]

    def test_an_event_it_does_not_know_is_ignored(self, monkeypatch):
        _stub(
            monkeypatch,
            [
                SimpleNamespace(type="response.something_new", payload={"x": 1}),
                _completed([_message('{"a": 1}')]),
            ],
        )
        assert _call()["data"] == {"a": 1}

    def test_incomplete_reads_as_cut_off_at_the_ceiling(self, monkeypatch):
        """Even where what arrived parses: the report has to say the answer
        was cut short, and against which ceiling."""
        _stub(
            monkeypatch,
            [_completed([_message('{"a": 1}')], status="incomplete")],
        )
        result = _call(max_tokens=100)

        assert result["failed"] is False
        assert result["truncated"] is True
        assert result["max_tokens"] == 100


# ---------------------------------------------------------------------------
# When it goes wrong
# ---------------------------------------------------------------------------


def _failed(code, message="went wrong", kind=None):
    return SimpleNamespace(
        type="response.failed",
        error={"code": code, "message": message, "type": kind or code},
    )


class TestFailures:
    def test_a_failed_response_is_a_failed_call_that_says_why(self, monkeypatch):
        _stub(monkeypatch, [_failed("invalid_request", "schema is not valid")])
        result = _call()

        assert result["failed"] is True
        assert "invalid_request" in result["error"]
        assert "schema is not valid" in result["error"]

    def test_a_rate_limited_response_retries_once(self, monkeypatch):
        sent = _stub(
            monkeypatch,
            [_failed("rate_limit")],
            [_completed([_message('{"a": 1}')])],
        )
        result = _call(retry=True)

        assert result["failed"] is False
        assert len(sent) == 2
        assert result["discarded_attempts"]["reasons"] == ["AgentResponseFailed"]

    def test_a_capacity_failure_walks_to_perplexitys_own_model(self, monkeypatch):
        sent = _stub(
            monkeypatch,
            [_failed("model_overloaded")],
            [_completed([_message('{"a": 1}')])],
        )
        result = _call(model="perplexity/kimi-k3")

        assert [p["model"] for p in sent] == ["perplexity/kimi-k3", "perplexity/sonar"]
        assert result["failed"] is False
        assert result["fallback_from"] == "perplexity/kimi-k3"

    def test_a_dropped_connection_retries_once_without_walking_the_chain(
        self, monkeypatch
    ):
        sent = _stub(
            monkeypatch,
            client.AgentTransportError("connection reset", 500),
            [_completed([_message('{"a": 1}')])],
        )
        result = _call(model="perplexity/kimi-k3", retry=True)

        assert result["failed"] is False
        assert [p["model"] for p in sent] == ["perplexity/kimi-k3"] * 2

    def test_an_http_error_before_the_stream_is_reported_with_its_body(
        self, monkeypatch
    ):
        sends = []

        def _send(http_client, request, *args, **kwargs):
            sends.append(request)
            return httpx.Response(
                401,
                request=request,
                json={
                    "error": {
                        "message": "Invalid API key provided.",
                        "type": "invalid_api_key",
                        "code": 401,
                    }
                },
            )

        monkeypatch.setattr(httpx.Client, "send", _send)
        result = _call(retry=True)

        assert result["failed"] is True
        assert len(sends) == 1, "a 401 is not worth a retry"
        assert "invalid_api_key" in result["error_body"]

    def test_a_connection_failure_from_the_sdk_retries_once(self, monkeypatch):
        sends = []

        def _send(http_client, request, *args, **kwargs):
            sends.append(request)
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch.setattr(httpx.Client, "send", _send)
        result = _call(retry=True)

        assert result["failed"] is True
        assert len(sends) == 2


class TestStalls:
    def test_a_stream_that_goes_silent_is_a_stall_before_the_first_chunk(self):
        """Search events are signs of life, not output: the first-byte
        allowance still governs until the answer begins."""
        release = threading.Event()

        def _events():
            yield SimpleNamespace(type="response.created")
            yield SimpleNamespace(
                type="response.reasoning.search_queries", queries=["q"]
            )
            release.wait(timeout=5)

        timing = client._StreamTiming(0.2, 5)
        try:
            with pytest.raises(client.StreamStalled, match="before the first chunk"):
                client._consume_agent_stream(_events(), 0.2, 5, timing)
        finally:
            release.set()
        assert timing.as_dict()["first_byte_censored"] is True

    def test_once_the_answer_starts_the_gap_budget_applies(self):
        release = threading.Event()

        def _events():
            yield _delta('{"a":')
            release.wait(timeout=5)

        started = time.monotonic()
        try:
            with pytest.raises(client.StreamStalled, match="mid-stream"):
                client._consume_agent_stream(_events(), 5, 0.2)
        finally:
            release.set()
        assert time.monotonic() - started < 2
