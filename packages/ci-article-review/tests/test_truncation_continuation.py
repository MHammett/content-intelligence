"""A pass cut off at its output ceiling is asked once for what it never sent.

Measured 2026-10-23, a ``maximum`` run on a 169,940-character draft:
mistral:fact_check spent its 50,000-token ceiling on 154 ``confirmed`` entries
and delivered no other bucket. The run detected it (``truncated``, the
missing-bucket list, "Never arrived:") and did nothing about it, because
recovery retries failed calls and a truncated pass succeeded.

``pipeline._continue_truncated_passes`` makes one follow-up call per such pass,
under a schema of only the missing buckets, and merges the answer under the
original. These tests drive it through a fake runner maker shaped like the
pipeline's own ``_make_runner``; the end-to-end suite drives it through
``run_draft_pipeline``.
"""

import copy
import json
from unittest.mock import patch

import pytest

from ci_article_review import consolidation, pipeline, schemas
from ci_article_review.consolidation import build_report
from ci_article_review.report_markdown import render_report_markdown

_ORDER = list(schemas.FACT_CHECK["properties"])


def _entries(n, prefix="c"):
    return [{"claim": f"{prefix}{i}"} for i in range(n)]


def _cut_before(bucket, data):
    """A truncated fact_check result whose raw opened ``bucket`` and stopped."""
    return {
        "failed": False,
        "data": data,
        "raw": json.dumps(data)[:-1] + f', "{bucket}": [{{"claim": "half a',
        "model": "mistral-small-latest",
        "tokens": {"prompt": 9000, "completion": 2000},
        "max_tokens": 2000,
        "truncated": True,
        "elapsed_seconds": 20.0,
        "_model": "mistral",
        "_domain": "fact_check",
    }


def _cut_early():
    """Cut inside ``unverifiable``: five buckets never arrived."""
    return _cut_before(
        "unverifiable", {"contradicted": _entries(1, "x"), "outdated": []}
    )


def _answer(buckets, **extra):
    return {
        "failed": False,
        "data": {b: _entries(2, b[:3]) for b in buckets},
        "model": "mistral-small-latest",
        "tokens": {"prompt": 9100, "completion": 900},
        "max_tokens": 2000,
        "elapsed_seconds": 9.0,
        **extra,
    }


def _clean(model="gemini", domain="fact_check"):
    return {
        "failed": False,
        "data": {b: [] for b in _ORDER},
        "model": f"{model}-m",
        "tokens": {"prompt": 1, "completion": 1},
        "_model": model,
        "_domain": domain,
    }


class _Runners:
    """Stands in for ``_make_runner``: records each call and serves an answer."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, model_name, domain, continuation=None):
        name = f"{model_name}:{domain}"

        def run():
            self.calls.append((name, copy.deepcopy(continuation)))
            served = self.answers[name]
            return served(continuation) if callable(served) else copy.deepcopy(served)

        return name, run


def _continue(raw_results, answers, **cfg):
    runners = _Runners(answers)
    out, continued_from = pipeline._continue_truncated_passes(
        raw_results,
        runners,
        {"provider_stagger_seconds": 0, **cfg},
        {},
        60,
    )
    return out, continued_from, runners.calls


class TestWhichPassesAreContinued:
    def test_only_a_truncated_pass_with_missing_buckets(self):
        complete_but_cut = _cut_before(
            "additional_observations",
            {b: [] for b in _ORDER if b != "additional_observations"}
            | {"additional_observations": _entries(1)},
        )
        complete_but_cut["_model"] = "grok"
        custom = {**_cut_early(), "_model": "claude", "_domain": "my_custom_domain"}
        raw = {
            "mistral:fact_check": _cut_early(),
            "gemini:fact_check": _clean(),
            "openai:fact_check": {
                "failed": True,
                "error": "boom",
                "truncated": True,
                "_model": "openai",
                "_domain": "fact_check",
            },
            # Cut inside the last bucket: nothing whole is missing to ask for.
            "grok:fact_check": complete_but_cut,
            # No schema, so nothing to say which buckets are missing.
            "claude:my_custom_domain": custom,
        }
        assert (
            consolidation._truncation_extent(
                "fact_check", complete_but_cut["data"], complete_but_cut["raw"]
            )["missing_buckets"]
            == []
        )
        missing = consolidation._truncation_extent(
            "fact_check",
            raw["mistral:fact_check"]["data"],
            raw["mistral:fact_check"]["raw"],
        )["missing_buckets"]
        _, _, calls = _continue(raw, {"mistral:fact_check": _answer(missing)})
        assert [name for name, _ in calls] == ["mistral:fact_check"]

    def test_a_clean_run_makes_no_call(self):
        raw = {"gemini:fact_check": _clean()}
        out, continued_from, calls = _continue(raw, {})
        assert calls == [] and continued_from == {}
        assert out == raw

    def test_off_when_the_config_says_so(self):
        raw = {"mistral:fact_check": _cut_early()}
        out, continued_from, calls = _continue(raw, {}, continue_truncated_passes=False)
        assert calls == [] and continued_from == {}
        assert out["mistral:fact_check"]["truncated"] is True

    def test_the_follow_up_is_told_what_arrived_and_asked_for_the_rest(self):
        raw = {"mistral:fact_check": _cut_early()}
        missing = _ORDER[_ORDER.index("unverifiable") :]
        _, _, calls = _continue(raw, {"mistral:fact_check": _answer(missing)})
        ((_, continuation),) = calls
        assert continuation == {
            "delivered": {"contradicted": 1, "outdated": 0},
            "missing": missing,
        }


class TestMerging:
    _MISSING = _ORDER[_ORDER.index("unverifiable") :]

    def _run(self, answer):
        original = _cut_early()
        raw = {"mistral:fact_check": original}
        out, continued_from, calls = _continue(raw, {"mistral:fact_check": answer})
        return original, out["mistral:fact_check"], continued_from, calls

    def test_every_missing_bucket_arrives_and_the_pass_is_whole(self):
        original, merged, continued_from, _ = self._run(_answer(self._MISSING))
        assert list(merged["data"]) == ["contradicted", "outdated", *self._MISSING]
        assert merged["truncated"] is False
        assert merged["continued"] == {
            "asked_for": self._MISSING,
            "last_bucket": "outdated",
            "cut_in": "unverifiable",
            "delivered": {"contradicted": 1, "outdated": 0},
            "recovered_buckets": self._MISSING,
            "still_missing": [],
        }
        assert continued_from["mistral:fact_check"] is original
        # The original is left as it was: it may be a capture's object.
        assert original["truncated"] is True and "continued" not in original

    def test_the_original_wins_and_unrequested_buckets_are_dropped(self):
        """A schema-free provider can re-send a bucket it was told it sent."""
        answer = _answer(self._MISSING)
        answer["data"]["contradicted"] = _entries(5, "dup")
        _, merged, _, _ = self._run(answer)
        assert merged["data"]["contradicted"] == _entries(1, "x")
        assert "contradicted" not in merged["continued"]["recovered_buckets"]

    def test_the_follow_up_is_recorded_for_billing(self):
        answer = _answer(
            self._MISSING,
            searches=3,
            discarded_attempts={"count": 1, "costed": 0, "reasons": ["StreamStalled"]},
        )
        _, merged, _, _ = self._run(answer)
        (record,) = merged["continuations"]
        assert record == {
            "buckets_requested": self._MISSING,
            "model": "mistral-small-latest",
            "tokens": {"prompt": 9100, "completion": 900},
            "elapsed_seconds": 9.0,
            "max_tokens": 2000,
            "truncated": False,
            "failed": False,
            "error": None,
            "searches": 3,
            "discarded_attempts": {
                "count": 1,
                "costed": 0,
                "reasons": ["StreamStalled"],
            },
        }
        # The first call's own figures are untouched.
        assert merged["tokens"] == {"prompt": 9000, "completion": 2000}

    def test_a_follow_up_cut_off_too_keeps_only_the_buckets_it_finished(self):
        data = {"unverifiable": _entries(2, "u"), "primary_source_needed": _entries(1)}
        answer = {
            **_answer([]),
            "data": data,
            "raw": json.dumps(data)[:-3] + ', {"claim": "half',
            "truncated": True,
        }
        _, merged, _, calls = self._run(answer)
        assert len(calls) == 1, "never recursive"
        assert merged["continued"]["recovered_buckets"] == ["unverifiable"]
        assert merged["continued"]["still_missing"] == self._MISSING[1:]
        assert "primary_source_needed" not in merged["data"]
        assert merged["truncated"] is True
        assert merged["continuations"][0]["truncated"] is True

    def test_a_follow_up_cut_off_before_finishing_anything_changes_nothing(self):
        data = {"unverifiable": _entries(1, "u")}
        answer = {
            **_answer([]),
            "data": data,
            "raw": json.dumps(data)[:-2] + ', {"claim": "half',
            "truncated": True,
        }
        original, merged, _, _ = self._run(answer)
        assert merged["data"] == original["data"]
        assert merged["continued"]["still_missing"] == self._MISSING
        assert merged["continued"]["recovered_buckets"] == []

    def test_a_failed_follow_up_keeps_the_original(self, caplog):
        answer = {
            "failed": True,
            "error": "stream stalled",
            "model": "mistral-small-latest",
            "tokens": {"prompt": 0, "completion": 0},
        }
        with caplog.at_level("WARNING", logger="pipeline"):
            original, merged, _, _ = self._run(answer)
        assert merged["data"] == original["data"]
        assert merged["truncated"] is True
        assert merged["continued"]["still_missing"] == self._MISSING
        record = merged["continuations"][0]
        assert record["failed"] is True and record["error"] == "stream stalled"
        assert "the follow-up failed" in caplog.text

    def test_it_logs_one_line_per_continuation(self, caplog):
        with caplog.at_level("INFO", logger="pipeline"):
            self._run(_answer(self._MISSING))
        assert (
            "Continuation: mistral:fact_check asked for 5 missing bucket(s) "
            "(unverifiable, primary_source_needed, out_of_scope, confirmed, "
            "additional_observations), got 5, 9100+900 tokens"
        ) in caplog.text


class TestAPassAlreadyContinued:
    """``--retry-failed`` over a capture whose continuation fell short."""

    def test_only_what_is_still_missing_is_asked_for(self):
        first = _cut_early()
        missing = _ORDER[_ORDER.index("unverifiable") :]
        once, _, _ = _continue(
            {"mistral:fact_check": first},
            {"mistral:fact_check": _answer(missing[:2], truncated=False)},
        )
        once = once["mistral:fact_check"]
        assert once["continued"]["still_missing"] == missing[2:]

        twice, continued_from, calls = _continue(
            {"mistral:fact_check": once},
            {"mistral:fact_check": _answer(missing[2:])},
        )
        ((_, continuation),) = calls
        assert continuation["missing"] == missing[2:]
        merged = twice["mistral:fact_check"]
        assert merged["continued"]["recovered_buckets"] == missing
        assert merged["continued"]["asked_for"] == missing
        assert merged["continued"]["cut_in"] == "unverifiable"
        assert merged["truncated"] is False
        assert len(merged["continuations"]) == 2
        assert continued_from["mistral:fact_check"] is once


class TestTheFollowUpRequest:
    """``_run_domain`` builds the continuation call from the original's parts."""

    def _call(self, **kw):
        sent = {}

        def fake_call(model_name, system, user, api_key, **kwargs):
            sent.update(system=system, user=user, **kwargs)
            return {"failed": False, "data": {}, "tokens": {}}

        with patch.object(pipeline.llm, "call_provider", side_effect=fake_call):
            pipeline._run_domain(
                "mistral",
                "fact_check",
                "A draft. " * 50,
                {"title": "T"},
                {},
                {},
                {},
                {"mistral": {"model": "mistral-small-latest"}},
                **kw,
            )
        return sent

    def test_same_prompt_and_ceiling_plus_a_block_and_a_reduced_schema(self):
        plain = self._call()
        missing = ["out_of_scope", "confirmed", "additional_observations"]
        follow = self._call(
            continuation={
                "delivered": {"contradicted": 3, "outdated": 0, "confirmed": 154},
                "missing": missing,
            }
        )
        assert follow["system"].startswith(plain["system"])
        block = follow["system"][len(plain["system"]) :]
        assert "- confirmed: 154 entries" in block
        assert "- outdated: 0 entries" in block
        assert ", ".join(missing) in block
        assert follow["user"] == plain["user"]
        assert follow["response_schema"]["schema"] == schemas.subset(
            "fact_check", missing
        )
        assert plain["response_schema"]["schema"] == schemas.FACT_CHECK
        # Same ceiling and provider settings: the follow-up is not a bigger call.
        assert follow["provider_config"] == plain["provider_config"]

    def test_an_explicit_ceiling_stays_explicit(self):
        sent = {}

        def fake_call(model_name, system, user, api_key, **kwargs):
            sent.update(kwargs)
            return {"failed": False, "data": {}, "tokens": {}}

        with patch.object(pipeline.llm, "call_provider", side_effect=fake_call):
            pipeline._run_domain(
                "mistral",
                "fact_check",
                "A draft.",
                {"title": "T"},
                {},
                {},
                {},
                {"mistral": {"model": "mistral-small-latest", "max_tokens": 1234}},
                continuation={"delivered": {}, "missing": ["confirmed"]},
            )
        assert sent["provider_config"]["max_tokens"] == 1234


class TestOldCapturesKeepTheirOrder:
    """Captures written before ``confirmed`` moved last read in their own order."""

    def test_a_legacy_capture_cut_inside_confirmed_lost_six_buckets(self):
        data = {"confirmed": _entries(154)}
        raw = json.dumps(data)[:-2] + ', {"claim": "half'
        got = consolidation._truncation_extent("fact_check", data, raw)
        assert got["missing_buckets"] == [
            "outdated",
            "contradicted",
            "unverifiable",
            "primary_source_needed",
            "out_of_scope",
            "additional_observations",
        ]
        assert got["last_bucket"] == got["cut_in"] == "confirmed"

    def test_a_current_capture_cut_inside_confirmed_lost_only_the_tail(self):
        data = {b: [] for b in _ORDER[: _ORDER.index("confirmed")]}
        data["confirmed"] = _entries(10)
        raw = json.dumps(data)[:-2] + ', {"claim": "half'
        got = consolidation._truncation_extent("fact_check", data, raw)
        assert got["missing_buckets"] == ["additional_observations"]
        assert got["cut_in"] == "confirmed"

    def test_a_skipped_bucket_in_todays_order_is_still_skipped(self):
        """A schema-free model can leave a bucket out; that is not a loss."""
        data = {"contradicted": [], "unverifiable": []}
        got = consolidation._truncation_extent("fact_check", data, None)
        assert "outdated" not in got["missing_buckets"]
        assert got["last_bucket"] == "unverifiable"


def _continued_result(answer_buckets, *, first=None, outcome=None):
    """A fact_check result as the continuation pass leaves it."""
    first = first or _cut_early()
    missing = consolidation._truncation_extent(
        "fact_check", first["data"], first["raw"]
    )["missing_buckets"]
    answer = (
        outcome
        if outcome is not None
        else _answer([b for b in missing if b in answer_buckets])
    )
    out, _, _ = _continue({"mistral:fact_check": first}, {"mistral:fact_check": answer})
    return out["mistral:fact_check"]


class TestTheReport:
    def _report(self, result):
        return build_report(
            "T", "p", 1, "d", None, {("mistral", "fact_check"): result}, {}, []
        )

    def test_a_continued_pass_is_still_listed_as_cut_off(self):
        result = _continued_result(_ORDER)
        assert result["truncated"] is False
        report = self._report(result)
        assert report["truncated_results"] == ["mistral:fact_check"]
        (detail,) = report["truncated_result_details"]
        missing = _ORDER[_ORDER.index("unverifiable") :]
        assert detail["continued"] is True
        assert detail["originally_missing"] == missing
        assert detail["recovered_buckets"] == missing
        assert detail["still_missing"] == detail["missing_buckets"] == []
        assert detail["cut_in"] == "unverifiable"
        assert detail["continuation_outcome"] == "complete"

    def test_the_recovered_buckets_reach_section_2(self):
        result = _continued_result(_ORDER)
        report = self._report(result)
        assert report["section_2_fact_check"]["unverifiable"], (
            "the follow-up's buckets never reached consolidation"
        )

    def test_a_cut_partway_through_confirmed_names_it_as_possibly_short(self):
        first = _cut_before(
            "confirmed",
            {b: [] for b in _ORDER[: _ORDER.index("confirmed")]}
            | {"confirmed": _entries(154)},
        )
        # The raw stopped inside a confirmed entry, not before the bucket.
        first["raw"] = json.dumps(first["data"])[:-2] + ', {"claim": "half'
        result = _continued_result(_ORDER, first=first)
        (detail,) = self._report(result)["truncated_result_details"]
        assert detail["cut_in_entries"] == 154
        md = render_report_markdown(self._report(result))
        assert (
            "cut off partway through `confirmed` after 154 entries; a follow-up "
            "call delivered `additional_observations`; `confirmed` may be "
            "incomplete"
        ) in md
        section = md.split("## SECTION 2")[1].split("## SECTION 3")[0]
        assert "Incomplete: mistral-small-latest" in section

    def test_a_pass_repaired_in_full_says_nothing_is_missing(self):
        md = render_report_markdown(self._report(_continued_result(_ORDER)))
        header = md.split("## ⚠ Truncated model passes")[1].split("\n## ")[0]
        assert "a follow-up call delivered `unverifiable`" in header
        assert "has the rest of this model's output, from the follow-up call" in header
        section = md.split("## SECTION 2")[1].split("## SECTION 3")[0]
        assert "Cut off, then continued: mistral-small-latest" in section
        assert "Nothing from that model is known to be missing here" in section
        assert "Incomplete:" not in section

    def test_a_failed_follow_up_says_what_never_arrived_and_why(self):
        failed = {"failed": True, "error": "boom", "tokens": {}}
        md = render_report_markdown(self._report(_continued_result([], outcome=failed)))
        assert (
            "`unverifiable`, `primary_source_needed`, `out_of_scope`, `confirmed` "
            "and `additional_observations` never arrived (the follow-up call failed)"
        ) in md

    def test_the_worklist_caveat_names_what_was_recovered(self):
        from ci_article_review.worklist import _truncation_caveat

        result = _continued_result(_ORDER[: _ORDER.index("confirmed")])
        (detail,) = self._report(result)["truncated_result_details"]
        (line, _) = _truncation_caveat([detail])
        assert "confirmed, additional_observations never arrived" in line
        assert (
            "a follow-up call recovered unverifiable, primary_source_needed, "
            "out_of_scope" in line
        )


@pytest.mark.parametrize("domain", sorted(schemas.BY_DOMAIN))
def test_every_schema_domain_can_be_continued(domain):
    """The block and the reduced schema build for every domain with a schema."""
    order = list(schemas.BY_DOMAIN[domain]["properties"])
    block = pipeline._continuation_block(
        {"delivered": {order[0]: 2}, "missing": order[1:]}
    )
    assert f"- {order[0]}: 2 entries" in block
    assert "{" not in block.replace("{}", ""), "an unrendered placeholder"
    assert list(schemas.subset(domain, order[1:])["properties"]) == order[1:]
