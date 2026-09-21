"""``--offline`` and the grammar pass.

``--offline`` is documented as skipping every pass that reaches the network, and
``--replay --offline`` as a run that makes no network calls at all. The grammar
pass was the one it missed: it was gated on ``grammar_pass`` and on having
credentials and never on ``offline``, so a replay of an unpublished draft POSTed
the draft's full text to the LanguageTool server (found 2026-09-19; the example
config's placeholder credentials are enough for the request to go out and come
back a 400). The SEO model calls had the same gap and were closed the same way.

The stub sits on ``languagetool.check_text``, the one function the request goes
through, so these tests hold however ``run`` is refactored. Every "does not
reach" test has an online control beside it that proves the stub can see a call:
without that a test whose stub was never reachable would pass on any code.
"""

import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from ci_article_review.report_markdown import render_report_markdown

#: One table for both renderers, so the console and the saved report cannot
#: word the same reason two ways — or, as they once did, word every reason as
#: "no credentials".
_WORDING = {
    "disabled": "grammar_pass is set to false in the pipeline config",
    "no_credentials": "no LanguageTool credentials configured",
    "offline": "--offline was set, and the grammar check is a network call",
}

_HOSTED = {"username": "me@example.test", "api_key": "k"}
_SELF_HOSTED = {"server_url": "http://localhost:8010/v2/check"}


def _config(*, grammar_pass=True, languagetool=_HOSTED):
    """The end-to-end fixture's config, with the grammar pass switched on."""
    from .test_pipeline_end_to_end import _CONFIG

    config = copy.deepcopy(_CONFIG)
    config["pipeline"]["grammar_pass"] = grammar_pass
    if languagetool is not None:
        config["api_keys"]["languagetool"] = dict(languagetool)
    config["publication"]["languagetool"] = {"auto_apply": ["GRAMMAR"]}
    return config


def _correction(draft):
    """A LanguageTool match the pass would auto-apply to ``draft``."""
    start = draft.index("It is important")
    return {
        "offset": start,
        "length": len("It is important"),
        "message": "Use a contraction.",
        "rule": {"id": "TEST_RULE", "category": {"id": "GRAMMAR"}},
        "replacements": [{"value": "It's important"}],
        "context": {"text": "It is important to note"},
    }


def _run(tmp_path, config, **run_kwargs):
    """Run the whole pipeline; return its report and the ``check_text`` stub."""
    from .test_pipeline_end_to_end import _HANDOFF, _stubbed_run

    check = MagicMock(return_value={"matches": [_correction(_HANDOFF["draft"])]})
    patches = [
        patch("ci_article_review.pipeline.merge_configs", return_value=config),
        patch("ci_article_review.adapters.grammar.languagetool.check_text", check),
    ]
    with _stubbed_run(tmp_path, extra_patches=patches, **run_kwargs) as report:
        pass
    return report, check


class TestOfflineSkipsTheGrammarPass:
    @pytest.mark.parametrize("server", [_HOSTED, _SELF_HOSTED], ids=["hosted", "self"])
    def test_an_online_run_sends_the_draft_and_applies_the_corrections(
        self, tmp_path, server
    ):
        """The control: the stub is the seam the request goes through."""
        from .test_pipeline_end_to_end import _HANDOFF

        report, check = _run(tmp_path, _config(languagetool=server))

        check.assert_called_once()
        assert check.call_args.args[0] == _HANDOFF["draft"]
        assert report["lt_skipped"] is False
        assert report["lt_skipped_reason"] is None
        assert len(report["lt_corrections_applied"]) == 1
        assert report["corrected_draft"] == _HANDOFF["draft"].replace(
            "It is important", "It's important", 1
        )

    @pytest.mark.parametrize("server", [_HOSTED, _SELF_HOSTED], ids=["hosted", "self"])
    def test_offline_never_reaches_languagetool(self, tmp_path, server):
        """A self-hosted server is skipped too: it is still reached over the
        network, and ``server_url`` may name any host, not only this one."""
        report, check = _run(tmp_path, _config(languagetool=server), offline=True)

        check.assert_not_called()
        assert report["lt_skipped"] is True

    def test_it_says_why_and_reviews_the_draft_as_written(self, tmp_path):
        from .test_pipeline_end_to_end import _HANDOFF

        report, _ = _run(tmp_path, _config(), offline=True)

        assert report["lt_skipped_reason"] == "offline"
        # A decision, not a failure: `lt_failed` is what tells a reader the
        # request went out and broke.
        assert report["lt_failed"] is False
        assert report["lt_corrections_applied"] == []
        assert report["corrected_draft"] == _HANDOFF["draft"]

    def test_a_replay_of_a_capture_does_not_reach_it_either(self, tmp_path):
        """The scenario the gap was found in: `--replay --offline`."""
        from .test_pipeline_end_to_end import _stubbed_run

        with _stubbed_run(tmp_path / "original", offline=True):
            pass
        capture = next((tmp_path / "original" / "history").rglob("*_results.json"))

        report, check = _run(
            tmp_path / "replay",
            _config(),
            offline=True,
            replay_results=str(capture),
        )

        check.assert_not_called()
        assert report["replayed_from"] == str(capture)
        assert report["lt_skipped_reason"] == "offline"

    def test_the_config_is_the_reason_when_both_apply(self, tmp_path):
        """An online run would not have run it either — the way
        ``links_skipped_reason`` ranks them."""
        report, check = _run(tmp_path, _config(grammar_pass=False), offline=True)

        check.assert_not_called()
        assert report["lt_skipped_reason"] == "disabled"

    def test_missing_credentials_are_the_reason_when_both_apply(self, tmp_path):
        report, check = _run(tmp_path, _config(languagetool=None), offline=True)

        check.assert_not_called()
        assert report["lt_skipped_reason"] == "no_credentials"

    def test_the_saved_report_says_it_too(self, tmp_path):
        """What a later reader opens is the file, not the returned dict."""
        _run(tmp_path, _config(), offline=True)

        (path,) = (tmp_path / "history").rglob("*_report.json")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["lt_skipped"] is True
        assert saved["lt_skipped_reason"] == "offline"


class TestTheReportNamesWhyGrammarWasSkipped:
    """Every skip path sets ``lt_skipped``, and the summary printed the
    credentials message for all of them — telling an operator with working
    credentials to go and configure credentials."""

    _REPORT = {
        "article_title": "A Title",
        "run_number": 1,
        "generated": "2026-09-19T00:00:00+00:00",
        "section_1_consensus": [],
        "section_2_fact_check": {},
        "section_3_voice": [],
        "section_4_argument": [],
        "section_5_completeness": [],
        "section_6_red_team": {},
        "section_7_low_confidence": [],
        "lt_corrections_applied": [],
        "lt_skipped": True,
    }

    def _console(self, capsys, **overrides):
        from ci_article_review.pipeline import _print_draft_summary

        _print_draft_summary({**self._REPORT, **overrides}, {})
        return capsys.readouterr().out

    def _markdown(self, **overrides):
        from .test_report_markdown import _base_report

        return render_report_markdown(_base_report(lt_skipped=True, **overrides))

    @pytest.mark.parametrize("reason", _WORDING)
    def test_the_console_summary(self, reason, capsys):
        out = self._console(capsys, lt_skipped_reason=reason)
        assert f"Grammar pass: skipped ({_WORDING[reason]} — run a manual" in out

    @pytest.mark.parametrize("reason", _WORDING)
    def test_the_saved_review(self, reason):
        md = self._markdown(lt_skipped_reason=reason)
        assert f"LanguageTool: skipped ({_WORDING[reason]})" in md

    def test_a_report_saved_before_reasons_were_recorded_keeps_its_message(
        self, capsys
    ):
        """No ``lt_skipped_reason`` key at all — an old report."""
        assert _WORDING["no_credentials"] in self._console(capsys)
        assert _WORDING["no_credentials"] in self._markdown()

    @pytest.mark.parametrize("reason", ["disabled", "offline"])
    def test_only_the_credentials_reason_says_credentials(self, reason, capsys):
        missing = _WORDING["no_credentials"]
        assert missing not in self._console(capsys, lt_skipped_reason=reason)
        assert missing not in self._markdown(lt_skipped_reason=reason)
