"""The SEO prompts that write publishable text are told the publication's rules.

``seo_suggest`` writes a meta description, an OG title and description, and
``seo_content`` writes replacement headings and openings. Both were sent only
the publication's description and audience. The rules an author enforces on
their own prose (a voice profile, ``banned_words``, ``banned_phrases``) reached
the voice_style review alone, so a suggested opening could carry exactly the
punctuation the voice check flags in the draft.

Issue #343. The profile text in these tests is a stand-in; the real one lives
in a private, git-ignored publication config.
"""

from unittest.mock import patch

from ci_article_review.analysis import seo_content, seo_style, seo_suggest

_PROFILE = "A sentence is not his if it contains PROFILE-MARKER-DASH-BAN."
_PUB_CONFIG = {
    "publication_description": "Infrastructure policy.",
    "audience": {"primary": "Local officials."},
    "style_profile": _PROFILE,
    "style_rules": {
        "banned_words": ["leverage", "robust"],
        "banned_phrases": ["at the end of the day"],
        "positive_rules": ["Lead with the number."],
    },
}
_ARTICLE = "# Title\n\nOpening paragraph.\n\n## Section\n\n" + "word " * 100
_HANDOFF = {"title": "Title", "primary_claim": "A claim.", "target_audience": "Staff."}
_KEYS = {"mistral": {"api_key": "k"}}


def _response(data):
    return {
        "failed": False,
        "data": data,
        "model": "mistral-small-latest",
        "tokens": {"prompt": 100, "completion": 50},
        "elapsed_seconds": 1.0,
    }


def _suggest_prompts(pub_config):
    with patch(
        "ci_article_review.analysis.seo_suggest.llm.call_provider",
        return_value=_response({"keyword_candidates": [], "meta_description": "m"}),
    ) as call:
        seo_suggest.generate(
            _ARTICLE, handoff=_HANDOFF, pub_config=pub_config, api_keys=_KEYS
        )
    _provider, system, user = call.call_args.args[:3]
    return system, user


def _content_prompts(pub_config):
    with patch(
        "ci_article_review.analysis.seo_content.llm.call_provider",
        return_value=_response({"findings": []}),
    ) as call:
        seo_content.review(
            _ARTICLE, handoff=_HANDOFF, pub_config=pub_config, api_keys=_KEYS
        )
    _provider, system, user = call.call_args.args[:3]
    return system, user


class TestTheBlock:
    def test_it_carries_the_profile_and_both_banned_lists(self):
        block = seo_style.style_block(_PUB_CONFIG)
        assert _PROFILE in block
        assert "leverage, robust" in block
        assert "at the end of the day" in block
        assert "- Lead with the number." in block

    def test_the_legacy_voice_profile_key_still_counts(self):
        """The voice_style prompt falls back to ``voice_profile``; so does this."""
        block = seo_style.style_block({"voice_profile": "LEGACY-PROFILE"})
        assert "LEGACY-PROFILE" in block

    def test_style_profile_wins_over_the_legacy_key(self):
        block = seo_style.style_block(
            {"style_profile": "NEW-PROFILE", "voice_profile": "OLD-PROFILE"}
        )
        assert "NEW-PROFILE" in block
        assert "OLD-PROFILE" not in block

    def test_nothing_configured_is_nothing_sent(self):
        for empty in ({}, None, {"style_rules": {}}, {"style_rules": None}):
            assert seo_style.style_block(empty) == ""

    def test_only_the_configured_parts_appear(self):
        block = seo_style.style_block({"style_rules": {"banned_words": ["synergy"]}})
        assert "synergy" in block
        assert "BANNED PHRASES" not in block
        assert "VOICE PROFILE" not in block


class TestTheSuggestionPromptGetsIt:
    def test_the_user_prompt_carries_the_rules(self):
        _system, user = _suggest_prompts(_PUB_CONFIG)
        assert _PROFILE in user
        assert "leverage, robust" in user
        assert "at the end of the day" in user

    def test_the_system_prompt_says_to_obey_them(self):
        system, _user = _suggest_prompts(_PUB_CONFIG)
        assert "PUBLICATION STYLE RULES" in system

    def test_a_publication_with_no_rules_sees_no_rules_block(self):
        _system, user = _suggest_prompts({"publication_description": "x"})
        assert "PUBLICATION STYLE RULES" not in user


class TestTheContentReviewPromptGetsIt:
    def test_the_user_prompt_carries_the_rules(self):
        _system, user = _content_prompts(_PUB_CONFIG)
        assert _PROFILE in user
        assert "leverage, robust" in user
        assert "at the end of the day" in user

    def test_the_system_prompt_says_to_obey_them(self):
        system, _user = _content_prompts(_PUB_CONFIG)
        assert "PUBLICATION STYLE RULES" in system

    def test_a_publication_with_no_rules_sees_no_rules_block(self):
        _system, user = _content_prompts({})
        assert "PUBLICATION STYLE RULES" not in user

    def test_it_is_still_told_not_to_review_the_writing(self):
        """The rules govern what it WRITES. Giving it the voice profile must not
        invite it to flag the draft's tone, which the voice domain owns."""
        system, _user = _content_prompts(_PUB_CONFIG)
        assert "Do NOT flag" in system
        assert "tone and phrasing" in system
