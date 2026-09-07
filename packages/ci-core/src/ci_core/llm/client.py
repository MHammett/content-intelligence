"""The litellm shim — one call path to every provider.

This replaces the six hand-rolled adapters, the raw-SSE accumulators, and the
token-normalisation table that preceded it (~3,350 lines). What is left here is
the part litellm does not do: mapping our call contract onto it, keeping our own
retry policy, and pulling the provider-specific fields the pipeline reads back
out of the response.

Two call surfaces, not one
--------------------------
Five providers go through :func:`litellm.completion`. OpenAI goes through
:func:`litellm.responses`, and that is not a style preference — it is measured.
``completion()`` routes reasoning models through Chat Completions, which sends
**zero bytes** while the model thinks: same model, effort, and prompt, TTFB was
79.1s with 100% of the call silent. ``responses()`` streams reasoning summaries
instead — TTFB 0.8s, 1318 summary deltas. Since the read-gap timeout below is
the only thing standing between a slow call and a hung one, a surface that goes
silent for 79s is unusable. Do not "simplify" OpenAI onto ``completion()``.

Three timeout layers, not one
-----------------------------
Every call streams, and the waiting splits into three jobs that need opposite
sizes. Conflating any two of them breaks one of the others:

1. **First-byte allowance** (``stream_read_timeout``, the socket read timeout).
   Generous, and per-model: it has to survive a grounded provider's search and
   a reasoning model's silent thinking before any byte is emitted.
2. **Inter-chunk gap** (``stream_gap_timeout``, enforced by
   :func:`_iter_with_gap`) — the liveness detector. Tight and roughly constant:
   a stream that has started emitting keeps emitting, so a long silence
   mid-response means the connection is dead, not slow.
3. **Wall-clock backstop** (the pipeline's sliding-scale ``timeout_seconds``,
   enforced by ``ci_article_review.pipeline._run_reviews_in_parallel`` via
   :func:`ci_core.concurrency.run_all_with_timeout`) — "this call is alive but
   I am not waiting any longer". Deliberately ignored here.

Layers 1 and 2 were a single value until 2026-08-15, which meant the stall
detector had to be sized for the *search* phase. That is how perplexity's read
timeout reached 500s: each bump chased slow-but-alive calls, and the side effect
was that a genuinely dead sonar connection took over eight minutes to notice —
the exact thing a stall detector exists to prevent.

:func:`_stream_timeout` builds the ``httpx.Timeout`` for layer 1. litellm passes
it through intact for OpenAI and Azure; for the other five providers it coerces
the object down to ``float(timeout.read)`` (``CompletionTimeout.resolve``). That
coercion is harmless — httpx applies a bare float per-operation — and the only
thing lost is the separate 30s connect bound, which those five replace with the
read value: more permissive, never less. Layer 2 does not depend on the socket
at all, which is the point: iterating the stream blocks inside a socket read, so
a tight gap can only be enforced from outside it.

Retries stay ours
-----------------
``num_retries=0`` on every call. litellm classifies credit exhaustion as
``RateLimitError`` — measured: an OpenAI account with no credits comes back as a
429, identical by status to a per-minute limit a short wait would clear. Its own
retry logic would therefore sit and retry a dead account until the wall-clock
backstop fires. Our policy — one retry on a genuinely transient failure, then
give up and report — is applied in :func:`_with_retry`, with the narrow
terminal-vs-transient check the 429 makes unavoidable. The full classifier
belongs upstream, where each provider's exhaustion wording is better known than
it is here.

Two known gaps in litellm that this file works around, both worth reporting
upstream: the credit-exhaustion classification above, and litellm's per-provider
parameter allowlist rejecting ``reasoning_effort`` for Mistral even though the
model accepts it (see :func:`_provider_params`).

Result contract
---------------
Unchanged from the adapters, because the pipeline, ci-style-profile, and the
report generator all read it:

  ``failed``           bool
  ``raw``              assembled response text
  ``data``             parsed JSON payload (success only)
  ``model``            the model that actually answered
  ``tokens``           ``{"prompt": int, "completion": int}``, plus
                       ``cached`` when the provider served part of the
                       prompt from its cache
  ``elapsed_seconds``  float
  ``error``            redacted exception text (failures only)
  ``error_body``       redacted excerpt of the HTTP error body

plus per-provider extras: ``citations`` / ``search_results`` (Perplexity),
``grounding_chunks`` / ``grounding_available`` (Gemini), ``truncated``,
``fallback_from``, ``misconfiguration_warning``.
"""

import logging
import queue
import threading
import time

import httpx
import litellm

from .. import redact
from .. import text_repair
from . import cache as cache_mod
from . import schema as schema_mod
from .json_utils import extract_json_with_salvage
from .tokens import normalize_tokens

log = logging.getLogger(__name__)

# litellm prints a "provider list" banner and update nags on first use, and logs
# a two-line "LiteLLM completion() model=..." banner at INFO for every call — 30
# calls a run, doubled, interleaved with the pipeline's own progress output. This
# is a library inside a CLI whose stdout is a report an operator reads; its
# warnings and errors still come through, and -v still raises the level back.
litellm.suppress_debug_info = True
litellm.telemetry = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# Connection-establishment timeout. Small and constant — opening a socket has
# nothing to do with how long the model will think. Honored as-is for OpenAI
# and Azure; see the module docstring for what the other five do with it.
DEFAULT_CONNECT_TIMEOUT = 30

# Default FIRST-BYTE allowance (seconds) when a model config sets no
# ``stream_read_timeout``. This covers everything before generation starts:
# queueing, a grounded model's web search, and any reasoning the provider does
# without putting bytes on the wire. Grounded models override it upward because
# their search runs before the first token.
DEFAULT_READ_TIMEOUT = 120
GROUNDED_READ_TIMEOUT = 160

# Default inter-chunk GAP (seconds) — the liveness detector, and a different job
# from the value above. Once a stream has produced its first chunk a healthy
# provider keeps emitting: the worst gap ever observed here is ~8s, from
# gpt-5.5's xhigh reasoning-summary deltas. Past a minute the connection is
# dead, not slow.
#
# Deliberately NOT sized to survive slow generation. Sizing the stall detector
# for slow-but-alive calls is what drove perplexity's first-byte value
# 160 -> 280 -> 350 -> 500, and a 500s stall detector means an abandoned call
# holds its socket for over eight minutes. Slow-but-alive is the wall-clock
# backstop's job; this one only catches dead.
DEFAULT_GAP_TIMEOUT = 60

# HTTP statuses worth one retry. Everything else is reported as-is: retrying a
# 400 or a 401 just spends the budget twice to learn the same thing.
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# Providers that accept a temperature. Anthropic's reasoning models reject any
# value other than 1 outright — `claude-opus-4-8 does not support
# temperature=0.2` is a hard 400, so sending it broke every Claude call in the
# ensemble. The adapter this replaced never sent one to Anthropic either; this
# preserves that. litellm.drop_params would also silence it, but globally, which
# would hide the next parameter mismatch instead of surfacing it.
_SENDS_TEMPERATURE = frozenset({"gemini", "mistral", "grok", "perplexity"})
_TEMPERATURE = 0.2

# Providers that accept `response_format: {"type": "json_object"}`, i.e. the
# provider itself guarantees parseable JSON rather than the prompt merely asking
# for it. Restored after an audit on 2026-08-16 found the migration had dropped
# it — grok in every preset it runs, mistral in the three that give it no
# reasoning effort. The prompt was the only thing asking, and json_utils was
# quietly absorbing the difference.
#
# The absentees are absent for a reason, each checked rather than assumed:
# OpenAI's Responses API has no such parameter at all (the old adapter only sent
# it on the Azure Chat Completions path), Perplexity does not support it,
# and Anthropic has no equivalent.
_JSON_OBJECT_PROVIDERS = frozenset({"grok", "mistral"})

# Providers whose live web search is an opt-in parameter rather than a property
# of the model. Proven on 2026-08-16 with a question training cannot answer —
# the newest litellm release on PyPI. Both said "I do not know" without it and
# answered with a cited, correct version with it.
#
# Perplexity and Gemini are deliberately absent: sonar always searches, and
# gemini's search is a tool set elsewhere in this function. Adding them here
# would send a parameter that either does nothing or conflicts.
#
# Why this matters more than a parameter usually does: fact_check runs six
# models, and before this only two of them could look anything up. The other
# four were checking claims against training recall, which is exactly the
# weakness the citation tiers exist to expose.
_WEB_SEARCH_PROVIDERS = frozenset({"grok", "claude"})

# ...except that Mistral's adapter treated response_format as incompatible with
# reasoning mode and sent one or the other, never both. A live check on
# 2026-08-16 found the combination now succeeds, so that may no longer hold —
# but "one call worked" is not enough to widen a constraint someone put here
# after presumably watching it fail, and the reasoning presets are the expensive
# ones to be wrong about. Restored as it was; revisit with evidence.
_JSON_OBJECT_EXCLUDES_REASONING = frozenset({"mistral"})


# ---------------------------------------------------------------------------
# Provider table
# ---------------------------------------------------------------------------
#
# ``prefix`` is litellm's provider routing prefix. ``surface`` picks the call
# shape (see the module docstring on why OpenAI differs). ``fallbacks`` is tried
# in order when the primary model returns a capacity error, and a fallback that
# answers is reported loudly — a quietly degraded review is worse than a failed
# one, because it still produces findings that look authoritative.

_PROVIDERS = {
    "openai": {
        "prefix": "",
        "surface": "responses",
        "default_model": "gpt-5.4",
        "fallbacks": ["gpt-5.4-mini"],
        "read_timeout": DEFAULT_READ_TIMEOUT,
        # Not the 60s default, and not a new number: before the stall detector
        # existed, OpenAI's only liveness bound was its socket read timeout,
        # which httpx applies per read — an effective 120s gap. Measured
        # 2026-08-16, gpt-5.5 at xhigh exceeds 60s between reasoning-summary
        # deltas when the pipeline runs six of them at once (a single call
        # shows a 1.3s worst gap; concurrency is what stretches it). Tightening
        # to 60s failed two OpenAI domains a run, so this restores exactly the
        # bound the provider already had rather than inventing a looser one.
        "gap_timeout": 120,
    },
    "gemini": {
        "prefix": "gemini/",
        "surface": "completion",
        "default_model": "gemini-2.5-flash",
        "fallbacks": ["gemini-2.5-flash-lite"],
        "read_timeout": GROUNDED_READ_TIMEOUT,
    },
    "mistral": {
        "prefix": "mistral/",
        "surface": "completion",
        "default_model": "mistral-large-latest",
        "fallbacks": ["mistral-small-latest"],
        "read_timeout": DEFAULT_READ_TIMEOUT,
    },
    "grok": {
        "prefix": "xai/",
        "surface": "completion",
        "default_model": "grok-4.3",
        "fallbacks": ["grok-build-0.1"],
        "read_timeout": DEFAULT_READ_TIMEOUT,
    },
    "claude": {
        "prefix": "anthropic/",
        "surface": "completion",
        "default_model": "claude-opus-4-8",
        "fallbacks": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "read_timeout": DEFAULT_READ_TIMEOUT,
    },
    "perplexity": {
        "prefix": "perplexity/",
        "surface": "completion",
        "default_model": "sonar-reasoning-pro",
        "fallbacks": ["sonar-pro", "sonar"],
        "read_timeout": GROUNDED_READ_TIMEOUT,
    },
}

PROVIDERS = tuple(_PROVIDERS)


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------


def _stream_timeout(cfg, default_read):
    """Build the streaming timeout. ``read`` is the inter-chunk gap.

    A per-model ``stream_read_timeout`` overrides the provider default. The
    sliding-scale ``timeout_seconds`` is deliberately NOT used here — that value
    is the pipeline's per-task wall-clock backstop, enforced by a thread
    wrapper, and using it as a socket timeout would defeat the whole point of
    streaming (a long-but-healthy generation would be killed).
    """
    read = float((cfg or {}).get("stream_read_timeout") or default_read)
    return httpx.Timeout(
        connect=float(DEFAULT_CONNECT_TIMEOUT), read=read, write=read, pool=read
    )


def _gap_timeout(cfg, provider=None):
    """Inter-chunk stall allowance.

    A per-model ``stream_gap_timeout`` wins; otherwise the provider's own
    default, which is the shared 60s for everything except OpenAI (see the
    provider table for why).
    """
    override = (cfg or {}).get("stream_gap_timeout")
    if override:
        return float(override)
    spec = _PROVIDERS.get(provider) or {}
    return float(spec.get("gap_timeout") or DEFAULT_GAP_TIMEOUT)


class StreamStalled(Exception):
    """A stream that started producing chunks went silent.

    Carries 504 so it lands in the retryable set: a stall means the connection
    died, and a new socket genuinely fixes that — the same reasoning that makes
    a dropped keepalive worth one retry.
    """

    status_code = 504


class MalformedJSONError(Exception):
    """A completed call's content didn't parse as JSON.

    Carries ``assembled`` — the same dict ``_invoke()`` returns — so a final,
    un-retried failure can still report token usage and finish_reason. Unlike
    ``StreamStalled`` this has no HTTP status: the call succeeded, only the
    parse failed, so it needs its own branch in ``_with_retry`` to be retried
    at all.
    """

    def __init__(self, assembled):
        super().__init__("malformed JSON response")
        self.assembled = assembled


def _close_stream(stream):
    """Best-effort release of the socket behind a litellm stream.

    litellm's ``CustomStreamWrapper`` exposes no ``close()``, and neither does
    the per-provider iterator inside it — but that iterator does hold both an
    ``http_response`` and a ``streaming_response`` generator, and closing either
    unblocks a reader parked in a socket read. Where none of them is present the
    reader is simply abandoned: it is a daemon thread, so the worst case is one
    idle thread until the (generous) first-byte socket timeout fires, which is
    still strictly better than not detecting the stall at all.
    """
    inner = getattr(stream, "completion_stream", None)
    for holder in (inner, stream):
        if holder is None:
            continue
        for attr in ("http_response", "streaming_response"):
            target = getattr(holder, attr, None)
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                    return True
                except Exception:
                    pass
    return False


def _iter_with_gap(stream, first_byte, gap, is_progress=None):
    """Yield chunks, aborting if the stream goes quiet for too long.

    Two separate allowances: ``first_byte`` until the stream produces real work,
    and ``gap`` between every chunk after that.

    Why a reader thread rather than a clock check in the loop: iterating the
    stream blocks inside a socket read, so the consumer only regains control
    when a chunk arrives or the socket's own timeout fires. The socket timeout
    has to stay generous enough for a grounded model's search phase, so it
    cannot double as a tight stall detector. Reading in a daemon thread and
    consuming through a queue decouples the two — the consumer's ``Queue.get``
    timeout enforces the gap regardless of what the socket will wait for.

    ``is_progress`` decides which chunk ends the first-byte phase, and it is not
    optional bookkeeping. The Responses API emits ``response.created``,
    ``response.in_progress`` and ``response.output_item.added`` within the first
    second — before the model has done any thinking — so treating "first chunk"
    as "first progress" starts the tight gap clock against the reasoning phase.
    Measured: an isolated xhigh call shows a 1.3s worst gap and passes, but the
    pipeline's six concurrent xhigh calls on a real draft blew straight through
    a 60s gap and every OpenAI domain failed. The thinking phase is what the
    first-byte allowance is *for*; only real output ends it.
    """
    chunks = queue.Queue()  # unbounded: a bounded queue could block the reader
    # in ``put`` after the consumer has given up and stopped draining.
    done = object()

    def _read():
        try:
            for chunk in stream:
                chunks.put(chunk)
        except BaseException as exc:  # noqa: BLE001 — relayed to the consumer
            chunks.put(exc)
        finally:
            chunks.put(done)

    reader = threading.Thread(target=_read, name="litellm-stream-reader", daemon=True)
    reader.start()

    budget = first_byte
    started = False
    while True:
        try:
            item = chunks.get(timeout=budget)
        except queue.Empty:
            _close_stream(stream)
            phase = "mid-stream" if started else "before the first chunk"
            raise StreamStalled(
                f"stream stalled {phase}: nothing received for {budget}s"
            )
        if item is done:
            return
        if isinstance(item, BaseException):
            raise item
        if not started and (is_progress is None or is_progress(item)):
            started = True
            budget = gap
        yield item


def _resolve_model(provider, model_arg, cfg):
    return (
        model_arg or (cfg or {}).get("model") or _PROVIDERS[provider]["default_model"]
    )


def _qualified(provider, model):
    """Prefix a bare model id for litellm's provider routing.

    A model that already carries a ``provider/`` prefix is left alone so an
    operator can pin an exact litellm route in user.yaml.
    """
    prefix = _PROVIDERS[provider]["prefix"]
    if not prefix or "/" in model:
        return model
    return f"{prefix}{model}"


def _provider_params(provider, cfg, response_schema=None):
    """Per-provider request parameters drawn from the model config.

    Only parameters the presets actually set are mapped. Anything unrecognised
    in the model config is left out rather than forwarded blindly — a stray key
    reaching a provider is a 400 mid-run, which costs a whole domain's review.
    """
    cfg = cfg or {}
    params = {}

    if provider == "gemini":
        # Search grounding. This is the entire reason gemini is in the fact_check
        # ensemble; without it the model is answering from training recall.
        params["tools"] = [{"googleSearch": {}}]
        budget = cfg.get("thinking_budget")
        if budget is not None:
            params["thinking"] = {"type": "enabled", "budget_tokens": int(budget)}

    elif provider == "claude":
        budget = cfg.get("thinking_budget")
        effort = cfg.get("effort")
        if budget is not None:
            params["thinking"] = {"type": "enabled", "budget_tokens": int(budget)}
            # Room for the answer on top of the thinking allowance, which is
            # spent before the response begins.
            default_max_tokens = int(budget) + 4096
        elif effort:
            params["reasoning_effort"] = effort
            default_max_tokens = 16000
        else:
            # Was a flat 4096, and `cfg["max_tokens"]` was ignored on all three
            # branches, so there was no way to raise it from config either.
            # Measured 2026-09-05 on a standard-preset run (135514 chars,
            # effort=none): claude:argument_integrity stopped at exactly 4096
            # output tokens and came back PARTIAL — salvage kept the complete
            # findings and discarded the rest. Every other provider finished.
            #
            # 8000 matches the default mistral's no-effort path already uses,
            # and claude-haiku-4-5's real max_output_tokens is 64000, so this
            # is still 8x below the provider's ceiling rather than near it.
            default_max_tokens = 8000
        params["max_tokens"] = int(cfg.get("max_tokens", default_max_tokens))
        # No temperature — see _SENDS_TEMPERATURE.

    elif provider in ("mistral", "grok", "perplexity"):
        effort = cfg.get("reasoning_effort")
        if effort:
            params["reasoning_effort"] = effort
            if provider == "mistral":
                # litellm's per-provider allowlist does not include
                # reasoning_effort for Mistral, so it raises UnsupportedParamsError
                # before the request is ever sent — client-side, in 0.05s. The
                # parameter is real: mistral-medium-3-5 accepts "high" and "none"
                # and 400s on "low"/"medium", which is a distinction only the
                # provider could be making. Verified 2026-08-14 that the call
                # succeeds with reasoning once the parameter is allowed through.
                #
                # This is the narrow fix rather than litellm.drop_params=True.
                # drop_params would make the call succeed by silently discarding
                # reasoning, turning a loud failure into a quiet quality
                # regression across all five domains — and it is global, so it
                # would hide the next parameter mismatch too.
                params["allowed_openai_params"] = ["reasoning_effort"]
        if provider == "mistral":
            # Default scales with effort rather than a flat cap. Measured
            # 2026-08-18 on a maximum-preset run (135514 chars, effort=high):
            # 4 of 5 domains hit exactly 8000 output tokens and got cut off
            # mid-JSON (salvage found nothing recoverable); the one domain
            # that finished used 7231, just under the old ceiling. The model
            # itself supports far more (mistral-medium-3-5's real
            # max_output_tokens is 262144 per litellm's model map) — 8000 was
            # a self-imposed cap, not a provider limit. 16000 matches the
            # budget claude's own effort="high" path already uses
            # (_provider_params above) and is still 16x below the real
            # ceiling; raise cfg["max_tokens"] explicitly if a domain still
            # truncates at 16000.
            default_max_tokens = 16000 if effort == "high" else 8000
            params["max_tokens"] = int(cfg.get("max_tokens", default_max_tokens))

        # Ask the provider to guarantee JSON where it can. A schema, when the
        # caller supplied one, is strictly better and is applied below for every
        # provider; this is the fallback for a call with no schema, restoring
        # what the adapters sent before the migration dropped it.
        if (
            not response_schema
            and provider in _JSON_OBJECT_PROVIDERS
            and not (provider in _JSON_OBJECT_EXCLUDES_REASONING and effort)
        ):
            params["response_format"] = {"type": "json_object"}

    # Perplexity search controls, when an operator sets them.
    for key in ("search_mode", "search_recency_filter", "search_domain_filter"):
        if provider == "perplexity" and cfg.get(key) is not None:
            params[key] = cfg[key]

    # Live web search, for the providers that offer it as an option. Perplexity
    # and Gemini are absent because search is not optional for them — sonar
    # always searches, and gemini's googleSearch tool is set above regardless.
    #
    # The pipeline resolves `web_search` per domain before calling, so by the
    # time it arrives it is a plain bool. Only fact_check has any use for it,
    # and every search bills.
    if provider in _WEB_SEARCH_PROVIDERS and (cfg or {}).get("web_search"):
        params["web_search_options"] = {
            "search_context_size": cfg.get("search_context_size", "medium")
        }

    # A schema, where the provider enforces one. Gemini is excluded while
    # grounded — the provider 400s on that combination, and its search matters
    # more here than the shape guarantee does.
    if response_schema:
        params.update(
            schema_mod.as_request_params(
                provider,
                response_schema["name"],
                response_schema["schema"],
                grounded=bool(params.get("tools")),
            )
        )

    return params


# ---------------------------------------------------------------------------
# Response reading
# ---------------------------------------------------------------------------


def _text_of(delta):
    """Pull text out of a streamed delta.

    Mistral's reasoning models send ``content`` as a list of typed chunks
    (``{"type": "thinking"}`` / ``{"type": "text"}``) rather than a string; only
    the text chunks belong in the answer.
    """
    piece = getattr(delta, "content", None)
    if isinstance(piece, list):
        out = []
        for part in piece:
            get = (
                part.get
                if isinstance(part, dict)
                else lambda k, d=None: getattr(part, k, d)
            )
            if get("type") == "text":
                out.append(get("text", "") or "")
        return "".join(out)
    return piece or ""


# Chunk types that mean the model has actually started producing. Anything
# before one of these is still the first-byte phase, however chatty the
# provider's stream framing is.
_RESPONSES_PROGRESS_EVENTS = (
    "response.output_text.delta",
    "response.reasoning_summary_text.delta",
)


def _responses_is_progress(event):
    return getattr(event, "type", None) in _RESPONSES_PROGRESS_EVENTS


def _completion_is_progress(chunk):
    """A chat-completions chunk carrying real output.

    Providers open with keep-alive or role-only chunks whose delta has no
    content; those are framing, not progress.
    """
    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if delta is not None and _text_of(delta):
            return True
        if getattr(choice, "finish_reason", None):
            return True
    return getattr(chunk, "usage", None) is not None


def _provider_field(chunk, *names):
    """A provider-specific extra from a streamed chunk, under any of ``names``.

    litellm puts anything without a home in the OpenAI schema into
    ``provider_specific_fields``, and where it hangs that dict depends on the
    provider *and* on whether the response is streamed. Anthropic, measured
    2026-08-16:

      non-streamed  message.provider_specific_fields["citations"]
      streamed      choices[].delta.provider_specific_fields["citation"]

    Different level, and singular rather than plural. Reading only the obvious
    place is why claude's search first looked like it returned nothing: the
    search had run, the results were on the wire, and the shim was looking one
    level up and one letter off.
    """
    holders = [chunk]
    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if delta is not None:
            holders.append(delta)

    for holder in holders:
        fields = getattr(holder, "provider_specific_fields", None)
        if not fields:
            continue
        for name in names:
            value = (
                fields.get(name)
                if isinstance(fields, dict)
                else getattr(fields, name, None)
            )
            if value:
                return value
    return None


def _direct_field(chunk, name):
    """``name`` read straight off the chunk or any of its deltas.

    The sibling of :func:`_provider_field`, for fields that sit in the OpenAI
    schema proper rather than in ``provider_specific_fields``. OpenAI's own
    ``annotations`` is one, so it is reachable by neither the plain
    ``getattr(chunk, ...)`` above nor the provider-specific walk.
    """
    holders = [chunk]
    for choice in getattr(chunk, "choices", None) or []:
        for attr in ("delta", "message"):
            holder = getattr(choice, attr, None)
            if holder is not None:
                holders.append(holder)
    for holder in holders:
        value = getattr(holder, name, None)
        if value:
            return value
    return None


def _url_citations(annotations):
    """Source URLs carried by OpenAI Responses-API ``annotations``.

    A grounded OpenAI call reports what its search read as annotations on the
    message — ``{"type": "url_citation", "url": ..., "title": ...}`` — rather
    than under either name Perplexity and Anthropic use. Nothing read that, so
    ``openai:fact_check`` on 2026-09-03 spent 84,634 prompt tokens and $0.85
    doing live search, reported ``grounding_available: None``, and contributed
    not one source to Section 9. The evidence was bought and discarded.

    Tolerant about shape: litellm hands these back as dicts or as objects
    depending on the path, and an annotation without a URL is not a citation.
    """
    urls = []
    for annotation in annotations or []:
        if isinstance(annotation, dict):
            kind, url = annotation.get("type"), annotation.get("url")
        else:
            kind, url = (
                getattr(annotation, "type", None),
                getattr(annotation, "url", None),
            )
        # Untyped annotations carrying a URL still count — the type name is the
        # part most likely to be renamed by a provider or a shim.
        if url and (not kind or "citation" in str(kind)):
            urls.append(url)
    return urls


def _consume_completion_stream(stream, first_byte, gap):
    """Drain a ``litellm.completion(stream=True)`` response.

    Returns the assembled text plus everything the pipeline reads off the
    response: usage, finish reason, and provider extras.

    Note the grounding handling. Gemini emits ``vertex_ai_grounding_metadata``
    on more than one chunk, and the early ones are empty ``{}`` — the populated
    object with ``groundingChunks`` arrives last. Taking the first non-None
    value therefore yields nothing at all, silently, on a call that really did
    ground. Last-non-empty wins.
    """
    parts = []
    usage = None
    finish_reason = None
    citations = []
    search_results = []
    grounding_meta = {}

    for chunk in _iter_with_gap(stream, first_byte, gap, _completion_is_progress):
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            if delta is not None:
                parts.append(_text_of(delta))
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage

        # Perplexity puts these on the chunk itself; Anthropic tucks them into
        # provider_specific_fields, which is why a first look at claude's search
        # results reported zero citations and read as "search did not run". It
        # had run. Both spellings are read here so the two look alike to the
        # pipeline, which only wants to know what the model actually consulted.
        cites = getattr(chunk, "citations", None) or _provider_field(chunk, "citations")
        if cites:
            citations = list(cites)
        results = getattr(chunk, "search_results", None) or _provider_field(
            chunk, "web_search_results"
        )
        if results:
            search_results = list(results)

        meta = getattr(chunk, "vertex_ai_grounding_metadata", None)
        if meta:
            for entry in meta if isinstance(meta, list) else [meta]:
                if isinstance(entry, dict) and entry:
                    grounding_meta = entry

    # Repaired here, at the point the provider's bytes become our strings,
    # so everything downstream -- the JSON parse, the text shim,
    # search-result snippets -- sees the same corrected text. See
    # ci_core.text_repair for what Perplexity does to General Punctuation.
    return text_repair.repair_tree(
        {
            "content": "".join(parts),
            "usage": usage,
            "finish_reason": finish_reason,
            "citations": citations,
            "search_results": search_results,
            # Only the Responses API surfaces web-search events; this keeps the two
            # consumers returning the same keys so _extras_from needs no branch.
            "web_search_used": False,
            "grounding_metadata": grounding_meta,
        }
    )


def _consume_responses_stream(stream, first_byte, gap):
    """Drain a ``litellm.responses(stream=True)`` response (OpenAI).

    The Responses API streams typed events. ``response.output_text.delta``
    carries answer text; ``response.completed`` carries final usage. The
    ``response.reasoning_summary_text.delta`` events carry no answer content,
    but reading them off the socket is what resets the read-gap timer during
    the model's thinking phase — which is the whole reason this surface exists.
    Everything else is ignored; not crashing on it is enough.
    """
    parts = []
    reasoning_parts = []
    citations = []
    searched = False
    usage = None
    status = None

    for event in _iter_with_gap(stream, first_byte, gap, _responses_is_progress):
        etype = getattr(event, "type", None)
        if etype == "response.output_text.delta":
            parts.append(getattr(event, "delta", "") or "")
        elif etype == "response.reasoning_summary_text.delta":
            reasoning_parts.append(getattr(event, "delta", "") or "")
        # `etype` is a str-subclass enum: `==` against the dotted value works,
        # but `str()` renders it as "ResponsesAPIStreamEvents.WEB_SEARCH_CALL_..."
        # instead. Read `.value` so a prefix test sees the wire name.
        elif str(getattr(etype, "value", etype) or "").startswith(
            "response.web_search_call"
        ):
            # The only evidence that a search happened. Measured 2026-09-04
            # against a live grounded call: these events carry an ``item_id``
            # and a sequence number and nothing else — no query, no URLs, no
            # results. They are still worth reading, because "did this call
            # consult live sources" is exactly what ``grounding_available``
            # claims to answer, and without them openai answered "no" while
            # spending 8,661 prompt tokens searching.
            searched = True
        elif etype == "response.content_part.done":
            # Where OpenAI puts url_citation annotations when it has any.
            part = getattr(event, "part", None)
            for url in _url_citations(getattr(part, "annotations", None)):
                if url not in citations:
                    citations.append(url)
        elif etype in ("response.completed", "response.incomplete"):
            resp = getattr(event, "response", None)
            if resp is not None:
                usage = getattr(resp, "usage", None) or usage
                status = getattr(resp, "status", None) or status
                incomplete = getattr(resp, "incomplete_details", None)
                if incomplete is not None:
                    status = getattr(incomplete, "reason", None) or status

    return text_repair.repair_tree(
        {
            "content": "".join(parts),
            "usage": usage,
            # The Responses API reports truncation as an incomplete status with
            # reason "max_output_tokens" rather than finish_reason="length".
            "finish_reason": "length" if status == "max_output_tokens" else status,
            "reasoning_summary": "".join(reasoning_parts),
            # Usually empty even on a grounded call, and that is the provider's
            # doing rather than a gap here: OpenAI attaches url_citation
            # annotations to cited spans of prose, and every review domain asks for
            # a JSON schema, which has no prose to cite. Measured 2026-09-04 —
            # search ran, the answer was right, `annotations` came back `[]`.
            "citations": citations,
            "search_results": [],
            "web_search_used": searched,
            "grounding_metadata": {},
        }
    )


def _usage_as_dict(usage):
    """litellm's usage object as a plain dict, nested details included.

    ``completion()`` returns a pydantic ``Usage``; ``responses()`` returns a
    ``ResponseAPIUsage`` with entirely different field names. Both expose
    ``model_dump()``; anything else falls back to attribute scraping so an
    unexpected type degrades to zeros rather than raising mid-run.

    The nesting matters as much as the top level. ``normalize_tokens`` reads the
    cached count out of ``prompt_tokens_details`` / ``input_tokens_details`` and
    tests those with ``isinstance(..., dict)``, so a details value left as an
    object reads as absent — silently reporting zero cached tokens on a call
    that hit the cache. That is the exact failure this whole area already had
    once, so the conversion goes one level down rather than trusting the
    top-level dump to have flattened it.
    """
    if usage is None:
        return {}

    def _as_dict(value):
        for method in ("model_dump", "dict"):
            fn = getattr(value, method, None)
            if callable(fn):
                try:
                    dumped = fn()
                    if isinstance(dumped, dict):
                        return dumped
                except Exception:
                    pass
        if isinstance(value, dict):
            return value
        if hasattr(value, "__dict__") and not isinstance(value, type):
            scraped = {
                key: getattr(value, key)
                for key in dir(value)
                if not key.startswith("_") and not callable(getattr(value, key, None))
            }
            if scraped:
                return scraped
        return None

    top = _as_dict(usage) or {}
    return {
        key: (_as_dict(value) or value) if key.endswith("_details") else value
        for key, value in top.items()
    }


def _read_tokens(usage):
    """Token counts for one call, via the shared :func:`normalize_tokens`.

    The migration originally reimplemented this inline, on the assumption that
    litellm reconciles the providers' spelling disagreements for us. Measured
    2026-08-16, it does not: a ``responses()`` call returns ``input_tokens`` and
    ``input_tokens_details`` with ``prompt_tokens_details`` absent entirely, so
    reading the Chat Completions name reported **zero cached tokens for every
    OpenAI call** — the same blindness that nearly got prompt-cache layout
    written off as ineffective, since the null result looked convincing.

    Delegating to ``normalize_tokens`` rather than duplicating it also picks up
    the two subtleties it already encodes: Anthropic reports only the *uncached*
    remainder in ``input_tokens`` and carries the cache fields separately, so
    they add rather than overlap; and Gemini bills thinking tokens at the output
    rate, so they belong in ``completion``.
    """
    return normalize_tokens(_usage_as_dict(usage))


def _grounding_chunks(metadata):
    """Extract ``[{"uri", "title"}, ...]`` from Gemini's grounding metadata.

    This shape is a contract, not an internal detail: ``resolve_grounding_urls``
    consumes it, and every ``uri`` here is a
    ``vertexaisearch.cloud.google.com/grounding-api-redirect/...`` wrapper that
    expires in roughly 30 days. They must be resolved to real source URLs before
    anything stores one — a citation pointing at a dead redirect looks like a
    source and is not one.
    """
    out = []
    for chunk in (metadata or {}).get("groundingChunks") or []:
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if isinstance(web, dict) and web.get("uri"):
            out.append({"uri": web["uri"], "title": web.get("title", "")})
    return out


# ---------------------------------------------------------------------------
# Errors and retry
# ---------------------------------------------------------------------------


def _status_of(exc):
    """HTTP status behind a litellm exception, or None."""
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


# A dead account is not a rate limit, whatever the status code says.
#
# Measured against litellm 1.96.2 on 2026-08-14: an OpenAI account with no
# credits raises RateLimitError with status_code 429 — indistinguishable by
# status from a genuine per-minute limit that a short wait would clear. Retrying
# it is pure waste: it fails again, having spent retry_delay to learn nothing,
# and on a 30-call run that is 30 pointless waits.
#
# This is the narrow, local half of the terminal-vs-transient classifier. The
# full version is being offered upstream (UPSTREAM.md #1), where it belongs —
# litellm is better placed to know each provider's exhaustion wording than we
# are. What is kept here is only enough to stop our own single retry.
_TERMINAL_QUOTA_MARKERS = (
    "no credits remaining",
    "insufficient credit",
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "payment required",
    "account is not active",
)


def _is_terminal_quota_error(exc):
    """True when a 429 means the wallet is empty, not that we went too fast.

    Reads the response body as well as the exception text: litellm folds the
    provider's message into the exception for OpenAI, but others put it only in
    the body, and this has to be right for all of them.
    """
    parts = [str(exc), str(getattr(exc, "message", "") or "")]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parts.append(str(response.text or ""))
        except Exception:
            pass
    text = " ".join(parts).lower()
    return any(marker in text for marker in _TERMINAL_QUOTA_MARKERS)


def _is_capacity_error(status):
    """True when the provider is out of capacity for this model — worth a fallback.

    Takes the recorded status rather than the exception text. Substring-matching
    "503" in a message is wrong now that litellm wraps mid-stream failures in
    ``MidStreamFallbackError``, which synthesises status 503 when the underlying
    error carries none: a dropped socket would read as "model at capacity" and
    silently walk the fallback chain, downgrading the model over a network blip.
    """
    return status == 503


def _error_body(exc):
    """Redacted excerpt of a provider's error body.

    The status line alone cannot distinguish an invalid key from a revoked one
    or from an account out of credit; providers put that in the body. Gemini
    puts the API key in the URL query string, so this goes through
    ``redact_url_keys`` before it can reach a log or a report.
    """
    body = ""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.text or ""
        except Exception:
            body = ""
    if not body:
        body = getattr(exc, "message", "") or ""
    return redact.truncate_excerpt(redact.redact_url_keys(body)) if body else ""


def _record_discarded(discarded, exc):
    """Note an attempt that was thrown away, and its usage if we have it.

    A retry replaces the failed attempt's result with the next one's, so the
    tokens the provider already generated — and billed for — vanished from the
    run's accounting entirely. Seven attempts were discarded this way on
    2026-09-03 while the summary printed ``Estimated cost: $3.4878 (exact)``.

    Usage is only sometimes recoverable. ``MalformedJSONError`` carries the whole
    assembled response, so its token counts are exact. A stalled stream or a
    transport error has no usage to read — those are counted but not costed,
    rather than quietly rolling an invented number into the total.
    """
    if discarded is None:
        return
    assembled = getattr(exc, "assembled", None)
    usage = assembled.get("usage") if isinstance(assembled, dict) else None
    discarded.append({"reason": exc.__class__.__name__, "usage": usage})


def _discarded_field(discarded):
    """``{"discarded_attempts": ...}`` if anything was thrown away, else ``{}``.

    Spread into each of the three result shapes so success, malformed-JSON and
    hard failure all report abandoned attempts the same way, and none of them
    has to remember the key name.
    """
    return {"discarded_attempts": _summarise_discarded(discarded)} if discarded else {}


def _summarise_discarded(discarded):
    """Fold recorded discarded attempts into counts and recoverable tokens.

    ``costed`` is how many of them carried usage we can price. The rest are real
    spend with no number attached, which is exactly why the run stops calling its
    total "exact" when any are present.
    """
    prompt = completion = costed = 0
    for attempt in discarded:
        usage = attempt.get("usage")
        if not usage:
            continue
        tokens = _read_tokens(usage)
        prompt += tokens.get("prompt", 0)
        completion += tokens.get("completion", 0)
        costed += 1
    return {
        "count": len(discarded),
        "costed": costed,
        "reasons": sorted({a.get("reason", "unknown") for a in discarded}),
        "tokens": {"prompt": prompt, "completion": completion},
    }


def _with_retry(fn, retry, retry_delay, label, discarded=None):
    """Run ``fn``, retrying once on a genuinely transient failure.

    ``num_retries=0`` is set on the litellm call itself, so this is the only
    retry in the stack. That is deliberate: litellm reports credit exhaustion as
    a ``RateLimitError``, and its retry loop would treat a dead account as a
    transient limit and keep going until the wall-clock backstop killed the run.

    The case this exists for, measured: a dropped keepalive connection surfaces
    as ``InternalServerError`` (status 500) carrying ``[WinError 10038] An
    operation was attempted on something that is not a socket`` — the cached
    HTTP client handing back a connection the provider had already closed. It
    showed up on roughly 10% of same-host calls made a second or two apart,
    which is exactly the spacing this pipeline uses. A new socket fixes it
    completely, so it must be retried; without that, a recoverable blip costs a
    whole domain's review.

    ``StreamStalled`` and ``MalformedJSONError`` are checked ahead of the
    status-code branch below and are always retryable. ``StreamStalled``
    already carries a synthetic ``status_code = 504`` and so already matched
    ``_RETRYABLE_STATUS`` on its own — this branch just makes that contract
    explicit instead of leaving it as an accident of which status got
    attached. ``MalformedJSONError`` has no HTTP status at all (the call
    succeeded; only the JSON parse failed), so without this branch it would
    never retry.
    """
    try:
        return fn()
    except (StreamStalled, MalformedJSONError) as exc:
        if not retry:
            raise
        _record_discarded(discarded, exc)
        log.warning(
            f"{label} {exc.__class__.__name__}. Waiting {retry_delay}s before one retry."
        )
        time.sleep(retry_delay)
        return fn()
    except Exception as exc:
        status = _status_of(exc)
        if not retry or status not in _RETRYABLE_STATUS:
            raise
        # Checked against every retryable status, not just 429. An exhausted
        # account reaches us as a 429 directly, but mid-stream it arrives
        # wrapped in MidStreamFallbackError, which synthesises 503 — same dead
        # wallet, different number. The test is what the provider said, not
        # which status code the wrapper picked.
        if _is_terminal_quota_error(exc):
            log.error(
                f"{label} reported HTTP {status}, but the account is exhausted. "
                f"Not retrying — a wait will not refill it."
            )
            raise
        _record_discarded(discarded, exc)
        log.warning(f"{label} HTTP {status}. Waiting {retry_delay}s before one retry.")
        time.sleep(retry_delay)
        return fn()


def _is_reasoning_param_error(body_text):
    """True if a 400 body says the reasoning parameter is unsupported."""
    lower = (body_text or "").lower()
    return (
        "unknown_parameter" in lower or "unsupported parameter" in lower
    ) and "reasoning" in lower


# ---------------------------------------------------------------------------
# One attempt
# ---------------------------------------------------------------------------


def _attempt(
    provider,
    model,
    system_prompt,
    user_prompt,
    api_key,
    cfg,
    timeout,
    retry,
    retry_delay,
    with_reasoning=True,
    response_schema=None,
    cache_prefix=None,
):
    """One model call, start to finish, as a result dict. Never raises."""
    spec = _PROVIDERS[provider]
    params = _provider_params(provider, cfg, response_schema) if with_reasoning else {}
    label = f"{provider} {model}"
    # Two budgets, two jobs: the socket read timeout is the first-byte
    # allowance, and the gap is the liveness detector applied after the stream
    # has started. See DEFAULT_GAP_TIMEOUT for why one value cannot be both.
    first_byte = timeout.read
    gap = _gap_timeout(cfg, provider)
    t0 = time.monotonic()

    def _invoke():
        if spec["surface"] == "responses":
            kwargs = {
                "model": _qualified(provider, model),
                "instructions": system_prompt,
                "input": user_prompt,
                "stream": True,
                "timeout": timeout,
                "num_retries": 0,
                "api_key": api_key,
            }
            effort = (cfg or {}).get("reasoning_effort")
            if with_reasoning and effort:
                # summary="auto" is what makes the thinking phase audible on the
                # wire; without it this surface goes as quiet as Chat Completions.
                kwargs["reasoning"] = {"effort": effort, "summary": "auto"}
            # No temperature on this path either, whatever the reasoning effort.
            #
            # There used to be an `else` here sending one, under a comment
            # correctly stating that gpt-5.x rejects temperature outright. The
            # comment described the model; the branch sent it anyway, to exactly
            # the calls the comment was about — the ones with no reasoning
            # effort. Every openai call without an effort therefore 400'd with
            # "Unsupported parameter: 'temperature' is not supported with this
            # model".
            #
            # The maximum preset hid it, because openai runs at xhigh there and
            # took the branch above. economy, standard and balanced set no
            # effort, so openai failed on every domain it was assigned. Caught
            # on a live standard-preset run, 2026-09-04: openai:voice_style and
            # openai:completeness both dead in under a second.
            #
            # ``_SENDS_TEMPERATURE`` has excluded openai all along; this path
            # simply never consulted it.

            # Live web search. The pipeline resolves this per domain before
            # calling (only fact_check has any use for it, and it bills per
            # search), so by the time it arrives it is a plain bool.
            if (cfg or {}).get("web_search"):
                kwargs["tools"] = [{"type": "web_search_preview"}]

            # Routing, not enablement: OpenAI caches anyway, but a shared key
            # steers concurrent calls at the same warm prefix.
            kwargs.update(cache_mod.as_request_params("openai", cache_prefix))

            # The schema goes on as `text.format` here rather than through
            # params, because this surface takes none of the completion() shape.
            if with_reasoning and response_schema:
                kwargs.update(
                    schema_mod.as_request_params(
                        "openai",
                        response_schema["name"],
                        response_schema["schema"],
                    )
                )

            return _consume_responses_stream(
                litellm.responses(**kwargs), first_byte, gap
            )

        if provider in _SENDS_TEMPERATURE:
            params.setdefault("temperature", _TEMPERATURE)

        # A cacheable prefix, where the provider needs telling where it ends.
        # Anthropic caches nothing without this; the rest either cache
        # implicitly or not at all, and get a plain string.
        if cache_prefix and user_prompt.startswith(cache_prefix):
            content = cache_mod.as_message_content(
                provider, cache_prefix, user_prompt[len(cache_prefix) :]
            )
        else:
            content = user_prompt

        return _consume_completion_stream(
            litellm.completion(
                model=_qualified(provider, model),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                stream=True,
                stream_options={"include_usage": True},
                timeout=timeout,
                num_retries=0,
                api_key=api_key,
                **params,
            ),
            first_byte,
            gap,
        )

    def _extras_from(assembled):
        grounding = _grounding_chunks(assembled["grounding_metadata"])
        citations = assembled["citations"]
        extras = {}
        if provider == "gemini":
            extras["grounding_chunks"] = grounding
            extras["grounding_available"] = bool(grounding)
        elif (
            provider == "perplexity"
            or citations
            or assembled["search_results"]
            or assembled.get("web_search_used")
        ):
            # Perplexity always reports these; grok and claude do too once their
            # search is switched on. Keyed off the payload rather than the
            # provider name so a newly-grounded provider surfaces without
            # another branch — the pipeline collects response-level sources by
            # shape, not by who sent them.
            extras["citations"] = citations
            extras["search_results"] = assembled["search_results"]
            # True when a search ran, even if the provider named no sources.
            # openai does exactly that under a JSON schema, and reporting it as
            # ungrounded understated a call that had just spent 84,634 prompt
            # tokens consulting the live web.
            extras["grounding_available"] = bool(
                citations
                or assembled["search_results"]
                or assembled.get("web_search_used")
            )
        return extras

    def _invoke_and_parse():
        # Parsing lives inside the retried callable, not after it: a call that
        # streamed fine but returned prose needs the same one-retry treatment
        # as a dropped socket, and _with_retry only sees what this raises.
        assembled = _invoke()
        parsed, truncated = extract_json_with_salvage(assembled["content"])
        if parsed is None:
            raise MalformedJSONError(assembled)
        return assembled, parsed, truncated

    discarded = []
    try:
        assembled, parsed, truncated = _with_retry(
            _invoke_and_parse, retry, retry_delay, label, discarded
        )
    except MalformedJSONError as exc:
        elapsed = round(time.monotonic() - t0, 2)
        assembled = exc.assembled
        content = assembled["content"]
        tokens = _read_tokens(assembled["usage"])
        hit_ceiling = assembled["finish_reason"] == "length"
        log.warning(
            f"{label} returned non-JSON content after {elapsed}s"
            + (" (cut off at the output-token ceiling)" if hit_ceiling else "")
        )
        return {
            "failed": True,
            "error": "Malformed JSON response",
            "raw": content,
            "model": model,
            "tokens": tokens,
            "elapsed_seconds": elapsed,
            **_extras_from(assembled),
            # A call that failed, retried and failed again was billed twice.
            # Attaching this only to the success path would have left the
            # most expensive outcome — two full attempts, no usable result —
            # as the one the cost summary still could not see.
            **_discarded_field(discarded),
        }
    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 2)
        body = _error_body(exc)
        safe = redact.redact_url_keys(exc)
        log.error(
            f"{label} call failed after {elapsed}s: {safe}"
            + (f" | {body}" if body else "")
        )
        return {
            "failed": True,
            "error": safe,
            "error_body": body,
            "raw": None,
            "model": model,
            "tokens": {"prompt": 0, "completion": 0},
            "elapsed_seconds": elapsed,
            "grounding_available": False,
            # Internal: the fallback decision reads these rather than
            # substring-matching the message. Stripped before the result
            # reaches the report.
            "_status": _status_of(exc),
            "_terminal": _is_terminal_quota_error(exc),
            **_discarded_field(discarded),
        }

    elapsed = round(time.monotonic() - t0, 2)
    content = assembled["content"]
    tokens = _read_tokens(assembled["usage"])
    extras = _extras_from(assembled)

    # finish_reason="length" means the ceiling cut the response off even when
    # the salvage path recovered parseable JSON from what arrived.
    truncated = truncated or assembled["finish_reason"] == "length"
    if truncated:
        log.warning(
            f"{label} response was truncated (hit the output-token ceiling at "
            f"{tokens['completion']} output tokens) after {elapsed}s; kept the "
            f"complete elements, discarded the rest."
        )
    else:
        log.debug(f"{label} call succeeded in {elapsed}s")

    result = {
        "failed": False,
        "raw": content,
        "data": parsed,
        "model": model,
        "tokens": tokens,
        "elapsed_seconds": elapsed,
        **extras,
    }
    if truncated:
        result["truncated"] = True
    # Attempts the provider generated and billed, whose output this call then
    # threw away and replaced. Kept separate from ``tokens`` so the successful
    # call's own numbers stay honest; cost accounting adds them.
    result.update(_discarded_field(discarded))
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _strip_internal(result):
    """Drop the routing-only keys before the result reaches a caller.

    The pipeline writes this dict into the run report; an underscore-prefixed
    key would show up there as if it meant something to a reader.
    """
    for key in ("_status", "_terminal"):
        result.pop(key, None)
    return result


def call(
    provider,
    system_prompt,
    user_prompt,
    api_key,
    retry=True,
    retry_delay=10,
    model=None,
    provider_config=None,
    response_schema=None,
    cache_prefix=None,
):
    """Call ``provider`` and return the shared result dict.

    Walks the fallback chain on capacity errors and retries once without
    reasoning parameters when a model rejects them.
    """
    if provider not in _PROVIDERS:
        raise KeyError(
            f"Unknown provider {provider!r}. Known providers: "
            f"{', '.join(sorted(_PROVIDERS))}"
        )

    cfg = provider_config or {}
    requested = _resolve_model(provider, model, cfg)
    timeout = _stream_timeout(cfg, _PROVIDERS[provider]["read_timeout"])
    chain = [requested] + [
        m for m in _PROVIDERS[provider]["fallbacks"] if m != requested
    ]

    result = None
    for attempt_model in chain:
        result = _attempt(
            provider,
            attempt_model,
            system_prompt,
            user_prompt,
            api_key,
            cfg,
            timeout,
            retry,
            retry_delay,
            response_schema=response_schema,
            cache_prefix=cache_prefix,
        )

        # A model that rejects the reasoning parameter is a misconfiguration, not
        # a provider fault: the preset asked for a reasoning model and user.yaml
        # overrode it with one that cannot reason. Retry without it so the domain
        # still gets reviewed, but say so loudly — the output is degraded and the
        # operator needs to fix the config, not the run.
        if (
            result.get("failed")
            and cfg.get("reasoning_effort")
            and _is_reasoning_param_error(result.get("error_body", ""))
        ):
            msg = (
                f"[MISCONFIGURATION] {provider} {attempt_model} rejected "
                f"reasoning_effort={cfg.get('reasoning_effort')!r}. Do not override the "
                f"{provider} model in user.yaml with a non-reasoning variant on a "
                f"balanced+ preset. Retrying without reasoning — output may be degraded."
            )
            log.error(msg)
            result = _attempt(
                provider,
                attempt_model,
                system_prompt,
                user_prompt,
                api_key,
                cfg,
                timeout,
                retry,
                retry_delay,
                with_reasoning=False,
                response_schema=response_schema,
                cache_prefix=cache_prefix,
            )
            result["misconfiguration_warning"] = msg

        if not result.get("failed"):
            _strip_internal(result)
            if attempt_model != requested:
                result["fallback_from"] = requested
                log.warning(
                    f"{provider} used FALLBACK model {attempt_model!r} because "
                    f"{requested!r} was unavailable (capacity). Review quality "
                    f"may be reduced."
                )
            return result

        # An exhausted account fails identically on every model in the chain,
        # so walking it just multiplies the same failure by three and reports
        # the last model's name as the one that broke.
        if (
            _is_capacity_error(result.get("_status"))
            and not result.get("_terminal")
            and attempt_model != chain[-1]
        ):
            log.warning(
                f"{provider} {attempt_model} unavailable (capacity). "
                f"Trying next fallback model."
            )
            continue
        _strip_internal(result)
        return result

    if result is not None:
        _strip_internal(result)
    return result
