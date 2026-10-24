"""A run that drew 429s from an unmetered provider says so at the end (#389).

With no ``rate_limits:`` block the meter in ``ci_core.llm.ratelimit`` waits for
nothing, and until now a run learned its account's limit only from the 429
lines in its log. The client counts every refusal; the pipeline names the
provider, the key to set and where the figure lives, and records the count on
the report.
"""

import logging

from ci_core.llm import ratelimit

from ci_article_review import pipeline


def _refused(provider, n, model=None):
    for _ in range(n):
        ratelimit.note_refused(provider, model=model)


class TestTheWarning:
    def setup_method(self):
        ratelimit.configure(None)

    def teardown_method(self):
        ratelimit.configure(None)

    def test_an_unmetered_provider_is_named_with_the_key_to_set(self, caplog):
        _refused("mistral", 3, model="mistral-small-latest")
        report = {}
        with caplog.at_level(logging.WARNING, logger=pipeline.log.name):
            pipeline._warn_about_unmetered_refusals(report)
        (record,) = [r for r in caplog.records if "HTTP 429" in r.message]
        assert "mistral refused 3 request(s)" in record.message
        assert "rate_limits.mistral" in record.message
        assert "admin.mistral.ai" in record.message
        assert report["rate_limit_refusals"] == {"mistral": 3}

    def test_a_metered_provider_is_counted_but_not_warned_about(self, caplog):
        ratelimit.configure(
            {"mistral/mistral-small-latest": {"requests_per_minute": 100}}
        )
        _refused("mistral", 2, model="mistral-small-latest")
        report = {}
        with caplog.at_level(logging.WARNING, logger=pipeline.log.name):
            pipeline._warn_about_unmetered_refusals(report)
        assert not [r for r in caplog.records if "HTTP 429" in r.message]
        assert report["rate_limit_refusals"] == {"mistral": 2}

    def test_a_clean_run_adds_nothing(self, caplog):
        report = {}
        with caplog.at_level(logging.WARNING, logger=pipeline.log.name):
            pipeline._warn_about_unmetered_refusals(report)
        assert "rate_limit_refusals" not in report
        assert not caplog.records
