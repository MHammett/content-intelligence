"""The relevance check asks for more than one retry (#389).

Mistral limits mistral-small to 100 requests a minute on the account measured
2026-10-24, and three concurrent checks at ~1.1s each run at ~160 a minute, so
a 429 there is a burst, not a fault. With the default single retry a second
refusal cost the claim its verdict; the check is well under a cent, so it asks
for a few more.
"""

from unittest.mock import patch

from ci_article_review.adapters.citation import resolver


def test_the_relevance_check_asks_for_a_bounded_number_of_retries():
    with patch(
        "ci_article_review.adapters.citation.resolver.llm.call_provider",
        return_value={"failed": True, "error": "x"},
    ) as call:
        resolver._verify_relevance(
            "a claim", "page text " * 50, {"mistral": {"api_key": "k"}}
        )
    assert call.call_args.kwargs["retry"] == resolver._VERIFICATION_RETRIES
    assert 2 <= resolver._VERIFICATION_RETRIES <= 5, "a few, not a loop"


def test_other_mistral_small_callers_keep_the_default():
    """Only the sub-cent, per-claim call opts in; the SEO passes do not."""
    from ci_article_review.analysis import seo_content, seo_suggest

    for module in (seo_content, seo_suggest):
        with patch(
            f"{module.__name__}.llm.call_provider",
            return_value={"failed": True, "error": "x"},
        ) as call:
            try:
                if module is seo_content:
                    module.review("draft " * 50, {"mistral": {"api_key": "k"}})
                else:
                    module.suggest("draft " * 50, {"mistral": {"api_key": "k"}})
            except Exception:  # noqa: BLE001 - the signature is not under test
                pass
        if call.called:
            assert "retry" not in call.call_args.kwargs
