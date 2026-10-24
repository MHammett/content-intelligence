"""``rate_limits:`` survives ``merge_configs`` (#361).

``merge_configs`` builds its result key by key, so a top-level block it does not
name is dropped without a word. The meter's limit would then never leave
``user.yaml``.
"""

from ci_article_review.config_loader import merge_configs


def test_rate_limits_survive_the_merge():
    limits = {"mistral": {"tokens_per_minute": 100000}}
    merged = merge_configs({"rate_limits": limits}, {})
    assert merged["rate_limits"] == limits


def test_no_rate_limits_merges_to_none_set():
    assert not merge_configs({}, {}).get("rate_limits")
