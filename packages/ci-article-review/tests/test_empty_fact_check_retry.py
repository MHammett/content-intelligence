"""An empty fact_check answer is retried once when its siblings found items.

Issue #342. A call that returns a schema-valid payload with every bucket empty
is ``failed: False`` with a truthy ``data`` dict, so recovery (which selects
``failed``) and substitution (which asks for a non-empty ``data``) both treated
it as a good result and the model went missing from Section 2 without a retry.

The decision, recorded here because it is not obvious. Retry once, only in
``fact_check``, only when another model in that domain found items:

* ``fact_check`` files every claim it reviews into one of seven buckets, so an
  all-empty answer says no claim was reviewed. The other domains return flags,
  and "no flags" is an ordinary answer, so a retry there would spend money on
  every clean draft.
* Seven of the 16 saved grok ``fact_check`` results are all-empty, and no other
  model's is (0 of 99). Six of the seven came with siblings that found 9 to 61
  items on the same draft, and the empties are 167 to 1,080 completion tokens
  against 3,870 to 27,567 for the answers, so these are short refusals to do
  the work, not drafts with nothing to check.
* When every model is empty, or the model is alone in its domain, nothing says
  the answer is wrong, and nothing is spent.

How often the retry gets a real answer has not been measured, because that
needs a live call; the log line and the billing record make the next
``maximum`` run say.
"""

import copy
from unittest.mock import patch

from ci_article_review import pipeline

_BUCKETS = (
    "confirmed",
    "outdated",
    "contradicted",
    "unverifiable",
    "primary_source_needed",
    "out_of_scope",
    "additional_observations",
)


def _data(items=0):
    data = {bucket: [] for bucket in _BUCKETS}
    data["confirmed"] = [{"claim": f"c{i}"} for i in range(items)]
    return data


def _answer(model, domain="fact_check", items=1, **extra):
    return {
        "failed": False,
        "data": _data(items),
        "model": f"{model}-m",
        "tokens": {"prompt": 900, "completion": 500 if items == 0 else 5000},
        "_model": model,
        "_domain": domain,
        **extra,
    }


def _cfg(**overrides):
    cfg = {"recovery_passes": 1, "recovery_delay_seconds": 0}
    cfg.update(overrides)
    return cfg


class _Runner:
    """A runner that hands back a fixed result sequence and counts its calls."""

    def __init__(self, name, *results):
        self.name = name
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return copy.deepcopy(result)

    def pair(self):
        return (self.name, self)


def _recover(raw, runners, **cfg):
    return pipeline._recover_failed_calls(
        raw, [r.pair() for r in runners], _cfg(**cfg), {}, task_timeout=5
    )


class TestWhichEmptyAnswersAreRetried:
    def test_an_empty_answer_beside_a_sibling_that_found_items_is_retried(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=9))

        out = _recover(raw, [grok])

        assert grok.calls == 1
        assert out["grok:fact_check"]["data"]["confirmed"], "the retry's answer is kept"

    def test_every_model_empty_is_taken_as_an_answer_and_not_retried(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=0),
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=9))
        gemini = _Runner("gemini:fact_check", _answer("gemini", items=9))

        _recover(raw, [grok, gemini])

        assert (grok.calls, gemini.calls) == (0, 0)

    def test_a_model_alone_in_its_domain_is_not_retried(self):
        raw = {"grok:fact_check": _answer("grok", items=0)}
        grok = _Runner("grok:fact_check", _answer("grok", items=9))

        _recover(raw, [grok])

        assert grok.calls == 0

    def test_a_failed_sibling_is_not_evidence_the_domain_has_items(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": {"failed": True, "error": "stream stalled"},
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=9))
        gemini = _Runner("gemini:fact_check", {"failed": True, "error": "stalled"})

        _recover(raw, [grok, gemini])

        assert grok.calls == 0

    def test_only_fact_check_is_treated_this_way(self):
        """ "No flags" is a normal answer in the flag domains; retrying it would
        spend money on every clean draft."""
        raw = {
            "grok:voice_style": _answer("grok", domain="voice_style", items=0),
            "openai:voice_style": _answer("openai", domain="voice_style", items=7),
        }
        grok = _Runner("grok:voice_style", _answer("grok", "voice_style", items=3))

        _recover(raw, [grok])

        assert grok.calls == 0

    def test_it_is_off_when_recovery_is_off(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=9))

        _recover(raw, [grok], recovery_passes=0)

        assert grok.calls == 0

    def test_a_failed_call_and_an_empty_one_are_retried_in_the_same_pass(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
            "mistral:fact_check": {"failed": True, "error": "stream stalled"},
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=9))
        mistral = _Runner("mistral:fact_check", _answer("mistral", items=4))

        out = _recover(raw, [grok, mistral])

        assert (grok.calls, mistral.calls) == (1, 1)
        assert not out["mistral:fact_check"]["failed"]


class TestAtMostOneRetry:
    def test_a_second_empty_answer_is_not_retried_again(self):
        """The money is bounded: one more call per empty answer, however many
        recovery passes are configured."""
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=0))

        out = _recover(raw, [grok], recovery_passes=3)

        assert grok.calls == 1
        assert pipeline.consolidation.result_is_empty(out["grok:fact_check"])

    def test_a_retry_that_fails_leaves_the_original_empty_answer_standing(self):
        """The model did answer, and the answer was empty. Turning it into a
        failure would report an outage that did not happen."""
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        grok = _Runner("grok:fact_check", {"failed": True, "error": "stream stalled"})

        out = _recover(raw, [grok])

        assert out["grok:fact_check"]["failed"] is False
        assert pipeline.consolidation.result_is_empty(out["grok:fact_check"])


class TestWhatTheRetryCost:
    """Every attempt that was billed has to reach the call log, kept or not."""

    def test_the_empty_answer_it_replaced_is_billed(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=9))

        out = _recover(raw, [grok])

        billed = out["grok:fact_check"]["discarded_attempts"]
        assert billed["count"] == 1
        assert billed["costed"] == 1
        assert billed["tokens"] == {"prompt": 900, "completion": 500}
        assert billed["reasons"] == ["EmptyResult"]

    def test_a_retry_that_was_not_kept_is_billed_too(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        grok = _Runner("grok:fact_check", _answer("grok", items=0))

        out = _recover(raw, [grok])

        billed = out["grok:fact_check"]["discarded_attempts"]
        assert billed["count"] == 1
        assert billed["tokens"] == {"prompt": 900, "completion": 500}
        assert billed["reasons"] == ["EmptyResult"]

    def test_a_failed_retry_is_billed_under_its_own_reason(self):
        raw = {
            "grok:fact_check": _answer("grok", items=0),
            "gemini:fact_check": _answer("gemini", items=12),
        }
        failed = {
            "failed": True,
            "error": "stream stalled before the first chunk",
            "tokens": {"prompt": 900, "completion": 0},
        }
        grok = _Runner("grok:fact_check", failed)

        out = _recover(raw, [grok])

        assert out["grok:fact_check"]["discarded_attempts"]["reasons"] == [
            "StreamStalled"
        ]


class TestThePipelineReachesIt:
    """Through ``run_draft_pipeline``, with the model call stubbed, so a retry
    that is correct in isolation but never wired in is caught."""

    def _config(self):
        from .test_pipeline_end_to_end import _CONFIG

        config = copy.deepcopy(_CONFIG)
        config["pipeline"]["thoroughness"] = "thorough"
        config["api_keys"]["perplexity"] = {"api_key": "k"}
        config["models"]["perplexity"] = {"model": "sonar"}
        return config

    def _run(self, tmp_path, first):
        from .test_pipeline_end_to_end import _fake_run_domain, _stubbed_run

        seen = {"perplexity:fact_check": 0}

        def _stub(model_name, domain, *a, **kw):
            if (model_name, domain) == ("perplexity", "fact_check"):
                seen["perplexity:fact_check"] += 1
                if seen["perplexity:fact_check"] == 1:
                    return copy.deepcopy(first)
            return _fake_run_domain(model_name, domain, *a, **kw)

        with _stubbed_run(
            tmp_path,
            extra_patches=[
                patch(
                    "ci_article_review.pipeline.merge_configs",
                    return_value=self._config(),
                ),
                patch("ci_article_review.pipeline._run_domain", side_effect=_stub),
            ],
        ) as report:
            pass
        return report, seen["perplexity:fact_check"]

    def test_an_empty_answer_beside_a_working_sibling_is_asked_again(self, tmp_path):
        report, attempts = self._run(tmp_path, _answer("perplexity", items=0))
        assert attempts == 2

        row = next(
            e for e in report["api_call_log"] if e["pass"] == "perplexity:fact_check"
        )
        assert row["status"] == "ok", "the retry's answer is the one reported"
        assert not report["empty_results"], "no 'returned nothing' warning is left"
        assert row["discarded_attempts"]["reasons"] == ["EmptyResult"]

    def test_a_clean_ensemble_makes_no_extra_call(self, tmp_path):
        report, attempts = self._run(tmp_path, _answer("perplexity", items=3))
        assert attempts == 1
