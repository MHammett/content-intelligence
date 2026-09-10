"""The cache-friendly layout actually produces a shared prefix.

Providers cache on an exact *leading* prefix. The per-domain instruction
normally sits ahead of the article and differs for every domain, so a provider's
five calls in one run share a long article and not one cacheable byte —
measured 0 cached tokens on every call, 2026-08-12. ``prompt_cache_layout``
moves the instruction to the end, leaving a constant stub plus the article as a
shared prefix; the same measurement then showed 1792/2368 cached on calls 2+.

**These tests exist because the golden report cannot cover this.**
``test_pipeline_end_to_end.py`` stubs ``_run_domain``, and that is exactly where
the setting is applied, so flipping the flag there produces an empty golden diff
by construction — evidence the code never ran, not evidence it works. What is
mechanically checkable is checked here, offline and for free: that the prefix is
byte-identical across domains, and that relocating the instruction does not lose
any of it.

What no offline test can answer is whether the *findings* hold up when the task
comes after a 33k-token document instead of before it. That needs a live run
diffed against a prior live run of the same article — and reading that diff
means checking ``model_failures`` first, because a run that lost a provider
looks exactly like a behavioural change.
"""

from unittest.mock import patch

import pytest

from ci_article_review.pipeline import (
    _CACHE_LAYOUT_SYSTEM,
    _DOMAIN_PROMPTS,
    _cache_friendly_layout,
    _run_domain,
)

DOMAINS = [
    "fact_check",
    "voice_style",
    "completeness",
    "argument_integrity",
    "red_team",
]

DRAFT = "The article body, long enough to be worth caching. " * 40


def _calls_for(prompt_cache_layout):
    """Run every domain through _run_domain, returning [(system, user), ...]."""
    captured = []

    def _fake_call(provider, system, user, api_key, **kwargs):
        captured.append((system, user))
        return {"raw": "{}"}

    with patch("ci_article_review.pipeline.llm.call_provider", side_effect=_fake_call):
        for domain in DOMAINS:
            _run_domain(
                "openai",
                domain,
                DRAFT,
                {"title": "A Real Article Title"},
                {},
                {"openai": {"api_key": "k"}},
                {"prompt_cache_layout": prompt_cache_layout},
                {"openai": {"model": "gpt-5.4"}},
            )
    return captured


class TestSharedPrefix:
    def test_without_the_setting_no_two_domains_share_a_prefix(self):
        """The baseline the setting exists to fix: five calls, zero shared bytes."""
        systems = [system for system, _ in _calls_for(False)]
        assert len(set(systems)) == len(DOMAINS)

    def test_every_domain_sends_an_identical_system_prompt(self):
        systems = [system for system, _ in _calls_for(True)]
        assert set(systems) == {_CACHE_LAYOUT_SYSTEM}

    def test_the_article_is_byte_identical_across_domains(self):
        """System + article is the cacheable prefix; it must not vary by domain."""
        users = [user for _, user in _calls_for(True)]
        prefixes = {user.split("YOUR REVIEW TASK")[0] for user in users}
        assert len(prefixes) == 1

    def test_the_shared_prefix_is_substantial(self):
        """A shared prefix of a few tokens would not be worth the rearrangement."""
        system, user = _calls_for(True)[0]
        prefix = system + user.split("YOUR REVIEW TASK")[0]
        assert len(prefix) > len(DRAFT)


class TestNothingIsLost:
    """Relocating the instruction must not drop any of it."""

    @pytest.mark.parametrize("index,domain", list(enumerate(DOMAINS)))
    def test_each_domain_still_receives_its_own_prompt(self, index, domain):
        from ci_article_review.pipeline import _load_prompt

        _, user = _calls_for(True)[index]
        # The domain prompt is rendered before relocation, so compare on a
        # distinctive line of the source prompt rather than the whole file.
        source = _load_prompt(_DOMAIN_PROMPTS[domain])
        first_line = source.splitlines()[0].strip()
        assert first_line in user

    def test_the_draft_survives(self):
        _, user = _calls_for(True)[0]
        assert DRAFT.strip() in user

    def test_the_task_comes_after_the_article(self):
        """The whole point: document first, task last."""
        _, user = _calls_for(True)[0]
        assert user.index(DRAFT.strip()) < user.index("YOUR REVIEW TASK")


class TestLayoutHelper:
    """_cache_friendly_layout in isolation."""

    def test_it_returns_the_constant_system(self):
        system, _, _ = _cache_friendly_layout("domain instruction", "article")
        assert system == _CACHE_LAYOUT_SYSTEM

    def test_it_keeps_both_halves(self):
        _, user, _ = _cache_friendly_layout("domain instruction", "article")
        assert "article" in user
        assert "domain instruction" in user

    def test_the_original_system_moves_to_the_end(self):
        _, user, _ = _cache_friendly_layout("domain instruction", "article")
        assert user.index("article") < user.index("domain instruction")


class TestTheCacheablePrefix:
    """The layout hands back where the shared part ends, rather than leaving the
    caller to find the boundary again by string surgery.

    Two copies of that boundary would drift, and a breakpoint placed one
    character off does not fail — it just caches the wrong span, or nothing, and
    the only symptom is a bill that did not go down.
    """

    def test_the_prefix_is_a_real_prefix_of_the_user_message(self):
        _, user, prefix = _cache_friendly_layout("domain instruction", "article")
        assert user.startswith(prefix)

    def test_the_prefix_holds_the_article_and_not_the_task(self):
        _, _, prefix = _cache_friendly_layout("domain instruction", "the article")
        assert "the article" in prefix
        assert "domain instruction" not in prefix

    def test_the_prefix_is_identical_across_domains(self):
        """The whole point: five domains, one shared prefix to cache."""
        prefixes = {
            _cache_friendly_layout(f"instruction for {d}", "same article")[2]
            for d in ("fact_check", "voice_style", "completeness")
        }
        assert len(prefixes) == 1

    def test_a_different_article_gets_a_different_prefix(self):
        a = _cache_friendly_layout("same instruction", "article one")[2]
        b = _cache_friendly_layout("same instruction", "article two")[2]
        assert a != b


def _calls_for_claude(prompt_cache_layout):
    """Run every domain, returning [(user, cache_prefix), ...] as sent."""
    captured = []

    def _fake_call(provider, system, user, api_key, **kwargs):
        captured.append((user, kwargs.get("cache_prefix")))
        return {"raw": "{}"}

    with patch("ci_article_review.pipeline.llm.call_provider", side_effect=_fake_call):
        for domain in DOMAINS:
            _run_domain(
                "claude",
                domain,
                DRAFT,
                {"title": "A Real Article Title"},
                {},
                {"claude": {"api_key": "k"}},
                {"prompt_cache_layout": prompt_cache_layout},
                {"claude": {"model": "claude-opus-5"}},
            )
    return captured


class TestTheBreakpointWithTheLayoutOff:
    """A marker still ships when the layout is off — for a different reason.

    With the layout off nothing is shared across domains (``system`` carries
    the per-domain instruction and renders ahead of ``messages``), so this buys
    nothing between calls. What it buys is caching *within* one call, which is
    where the input tokens actually are: on run_2 of honda-navigation-clock
    (2026-09-09) fact_check alone was 141,259 of claude's 169,791 input tokens,
    because server-side web_search loops and re-reads a growing context on every
    internal turn. The four domains without search sat at ~9,500 each.

    Nothing about the prompt moves, so none of the quality question that
    PLAN.md §5 decision 2 closed is reopened here.
    """

    def test_a_prefix_is_sent_even_with_the_layout_off(self):
        """The regression this guards: cache_prefix was None here, so no marker
        was ever sent and Anthropic cached nothing at all."""
        assert all(prefix for _, prefix in _calls_for_claude(False))

    def test_the_prefix_is_the_whole_user_prompt(self):
        """Everything up to and including the marked block is what gets cached.
        Marking less would leave the tail of the article uncached for free."""
        for user, prefix in _calls_for_claude(False):
            assert prefix == user

    def test_the_draft_is_inside_the_cached_span(self):
        _, prefix = _calls_for_claude(False)[0]
        assert DRAFT.strip() in prefix

    def test_no_domain_text_leaks_into_the_prefix(self):
        """A per-domain byte in the prefix would make each domain's entry
        unique — a write premium on every call and never a read."""
        from ci_article_review.pipeline import _load_prompt

        for domain, (_, prefix) in zip(DOMAINS, _calls_for_claude(False)):
            first_line = _load_prompt(_DOMAIN_PROMPTS[domain]).splitlines()[0].strip()
            assert first_line not in prefix

    def test_the_layout_still_narrows_the_prefix_when_it_is_on(self):
        """With the layout on, the domain instruction moves into the user
        message, so the cacheable span must stop short of it rather than
        swallow the whole prompt.

        The constant ``YOUR REVIEW TASK`` header is deliberately *inside* the
        span — it is byte-identical across domains, so excluding it would give
        up cacheable bytes for nothing. Only the instruction itself varies.
        """
        from ci_article_review.pipeline import _load_prompt

        for domain, (user, prefix) in zip(DOMAINS, _calls_for_claude(True)):
            assert prefix and prefix != user
            first_line = _load_prompt(_DOMAIN_PROMPTS[domain]).splitlines()[0].strip()
            assert first_line not in prefix
