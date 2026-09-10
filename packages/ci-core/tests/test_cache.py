"""The cache breakpoint module, which had no tests at all.

Everything here is about one failure mode: caching that silently does nothing.
There is no error when a breakpoint is misplaced, absent, or below a model's
minimum — the request succeeds and the bill is just higher. So the only things
worth asserting are the ones that would otherwise fail in silence.
"""

import pytest

from ci_core.llm import cache, cost


class TestAnthropicGetsABreakpoint:
    """Anthropic caches nothing without an explicit marker — measured
    2026-08-16, same prefix sent twice, zero cached both times."""

    def test_the_prefix_block_carries_the_marker(self):
        blocks = cache.as_message_content("claude", "the article", " the task")
        assert blocks[0]["text"] == "the article"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_the_remainder_is_not_marked(self):
        """A second breakpoint after the varying tail would write an entry that
        is never read back — a pure surcharge."""
        blocks = cache.as_message_content("claude", "the article", " the task")
        assert "cache_control" not in blocks[1]

    def test_no_block_is_ever_empty(self):
        """An empty text block is a 400 from Anthropic.

        This is the ordinary case with ``prompt_cache_layout`` off: the whole
        user prompt is the cacheable prefix, so the remainder is "".
        """
        blocks = cache.as_message_content("claude", "the whole prompt", "")
        assert all(block["text"] for block in blocks)

    def test_an_empty_remainder_still_gets_the_marker(self):
        """The breakpoint is the entire point; dropping the empty block must
        not drop the marker with it."""
        blocks = cache.as_message_content("claude", "the whole prompt", "")
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}


class TestNothingIsLost:
    """Whatever the shape, the model must receive every byte of the prompt."""

    @pytest.mark.parametrize("remainder", ["", " the task", "\n\ntrailing"])
    def test_the_blocks_rejoin_to_the_original_prompt(self, remainder):
        blocks = cache.as_message_content("claude", "the article", remainder)
        assert "".join(b["text"] for b in blocks) == "the article" + remainder

    @pytest.mark.parametrize("provider", ["openai", "gemini", "grok", "mistral"])
    def test_implicit_cachers_get_the_prompt_unchanged(self, provider):
        """No reason to complicate a request that already works."""
        assert cache.as_message_content(provider, "a", "b") == "ab"

    def test_no_prefix_means_no_restructuring(self):
        assert cache.as_message_content("claude", "", "just the prompt") == (
            "just the prompt"
        )


class TestOpenAIKeyIsRoutingOnly:
    def test_the_same_prefix_yields_the_same_key(self):
        """Concurrent calls sharing a prefix must land on the same cache."""
        a = cache.as_request_params("openai", "an article")
        b = cache.as_request_params("openai", "an article")
        assert a == b and a["prompt_cache_key"]

    def test_a_different_prefix_yields_a_different_key(self):
        a = cache.as_request_params("openai", "article one")
        b = cache.as_request_params("openai", "article two")
        assert a != b

    def test_the_article_itself_never_travels_in_the_key(self):
        """It is already in the body; sending it twice is pure waste."""
        params = cache.as_request_params("openai", "a distinctive article body")
        assert "distinctive" not in params["prompt_cache_key"]

    @pytest.mark.parametrize("provider", ["claude", "gemini", "grok", "mistral"])
    def test_no_other_provider_gets_the_key(self, provider):
        assert cache.as_request_params(provider, "an article") == {}


class TestTheInstrumentCanReadItsOwnResults:
    """Every model that can be told to cache must have a cached rate priced.

    Without one, ``cost.py`` bills cache reads at the full input rate, so
    turning caching on moves the reported cost by nothing and reads as "the
    feature does not work". Every Claude row was missing this until 2026-09-10
    — the same blind-instrument failure as the pre-#103 OpenAI A/B.

    Asserted as an invariant over whatever rows exist rather than against a
    fixed model list, so a newly added Claude model cannot reintroduce it.
    """

    def test_every_provider_that_needs_a_marker_prices_its_cached_reads(self):
        missing = [
            model
            for model, price in cost._PRICING.items()
            if any(model.startswith(p) for p in cache.MARKS_A_BREAKPOINT)
            and len(price) < 3
        ]
        assert not missing, f"no cached rate configured for {missing}"

    def test_a_cached_read_is_always_cheaper_than_an_uncached_one(self):
        """A typo that made cached >= input would silently over-report forever."""
        for model, price in cost._PRICING.items():
            if len(price) > 2:
                assert price[2] < price[0], f"{model}: cached rate is not a discount"

    def test_a_cache_read_is_actually_billed_at_the_cheaper_rate(self):
        """End to end through the real cost path, not just the table."""
        entry = {
            "model": "claude-opus-5",
            "tokens": {"prompt": 100_000, "completion": 0, "cached": 90_000},
        }
        cold = {
            "model": "claude-opus-5",
            "tokens": {"prompt": 100_000, "completion": 0},
        }
        assert cost._entry_cost(entry)[0] < cost._entry_cost(cold)[0]
