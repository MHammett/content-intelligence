"""Telling a provider which part of a prompt is worth caching.

The pipeline sends five calls per model per run that share a long prefix — the
same article, reviewed five different ways. Serving that prefix from a cache on
calls 2 through 5 is most of the input cost of a run.

Providers disagree about how much they will do unasked, and the difference is
not a detail. Measured 2026-08-16, same prefix sent twice:

===========  =========================================================
provider     behaviour
===========  =========================================================
anthropic    **nothing** without an explicit ``cache_control`` marker.
             With one: 5,412 of 5,426 tokens read from cache on call 2.
             Without: zero, both calls.
openai       caches automatically above ~1024 tokens. ``prompt_cache_key``
             does not turn caching on; it routes requests sharing a key to
             the same cache, which matters once calls run concurrently.
grok         caches automatically.
gemini       caches implicitly.
mistral      no published cache-read rate; nothing to ask for.
perplexity   likewise.
===========  =========================================================

So Anthropic is the one that needs telling, and it is also the most expensive
model in the ensemble — exactly the wrong provider to have been silently not
caching.

Minimum sizes bite, and they do not move monotonically with the version.
``claude-haiku-4-5`` needs **4096** tokens before it will cache anything;
``claude-opus-5`` needs **512** (halved from opus-4-8's 1024, so prompts that
were previously too short now create entries with no code change); sonnet-5 and
opus-4-8 need 1024. A first attempt at the measurement above used haiku with a
2,874-token prefix, read zero, and looked like proof that ``cache_control``
does nothing at all. Below the minimum there is no error — just
``cache_creation_input_tokens: 0``.
"""

__all__ = ["as_message_content", "as_request_params", "MARKS_A_BREAKPOINT"]

#: Providers that need to be told where the cacheable prefix ends. Everyone
#: else either caches implicitly or does not cache at all.
MARKS_A_BREAKPOINT = frozenset({"claude"})


def as_message_content(provider, prefix, remainder):
    """User-message content for a prompt with a cacheable ``prefix``.

    Returns a plain string for providers that cache implicitly — no reason to
    complicate a request that already works. For Anthropic, returns the text
    blocks it needs, with the breakpoint marked on the first: everything up
    to and including that block is what gets cached, so the shared article is
    inside it and the per-domain task is not.

    ``remainder`` is empty whenever the whole user prompt is cacheable — which
    is the ordinary case with ``prompt_cache_layout`` off, since the user
    prompt is then just the draft and carries no per-domain text. An empty
    text block is a 400 from Anthropic, so emit one block, not two.
    """
    if provider not in MARKS_A_BREAKPOINT or not prefix:
        return prefix + remainder

    blocks = [
        {
            "type": "text",
            "text": prefix,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if remainder:
        blocks.append({"type": "text", "text": remainder})
    return blocks


def as_request_params(provider, prefix):
    """Extra request parameters that improve cache behaviour, or ``{}``.

    OpenAI caches without being asked, so this does not enable anything. What
    ``prompt_cache_key`` buys is routing: requests carrying the same key are
    steered to the same cache, which is the difference between five concurrent
    calls sharing one warm prefix and each of them missing on a cold one.

    The key is derived from the prefix rather than supplied by the caller, so
    two runs of the same article share it and two different articles cannot
    collide.
    """
    if provider != "openai" or not prefix:
        return {}

    # A hash, not the text: the key travels in the request and there is no
    # reason to send the article twice.
    import hashlib

    digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]
    return {"prompt_cache_key": f"ci-review-{digest}"}
