"""The warning a run gives before it spends the ensemble on a draft that
history already has a run of (issue #454). Advisory only: it must never stop
or change a run, and it must stay silent for a replay or retry, which spend
nothing on the ensemble."""

import json
import logging

import pytest

from ci_article_review import pipeline
from ci_article_review.history import _slug

DRAFT = "The grid serving Northern Illinois produces 0.40 lbs of NOx per MWh."
HANDOFF = {"title": "Unchanged draft warning test article", "draft": DRAFT}


def _history(root, *, with_capture=True, draft=DRAFT):
    d = root / _slug(pipeline._history_key(HANDOFF))
    d.mkdir(parents=True, exist_ok=True)
    report = {
        "run_number": 4,
        "corrected_draft": draft,
        "ensemble": {"cost_preset": "maximum"},
        "cost_summary": {"total_usd": 10.3012},
    }
    path = d / "run_4_20261028_112001_report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    capture = d / "run_4_20261028_112001_results.json"
    if with_capture:
        capture.write_text("{}", encoding="utf-8")
    return capture


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_history_root", lambda: str(tmp_path))
    return tmp_path


def test_names_the_run_and_the_replay_command(history, caplog):
    capture = _history(history)
    with caplog.at_level(logging.WARNING, logger="ci_article_review.pipeline"):
        matches = pipeline._warn_if_draft_already_reviewed(
            DRAFT, HANDOFF, "drafts/handoff.md", "mikehammett"
        )
    assert len(matches) == 1
    text = caplog.text
    assert "unchanged since run 4 (maximum, $10.30)" in text
    assert (
        f"uv run ci-review --draft drafts/handoff.md --publication mikehammett "
        f"--replay {capture} --offline"
    ) in text


def test_says_when_no_capture_survives(history, caplog):
    _history(history, with_capture=False)
    with caplog.at_level(logging.WARNING, logger="ci_article_review.pipeline"):
        pipeline._warn_if_draft_already_reviewed(DRAFT, HANDOFF, "h.md", "pub")
    assert "--replay is not available" in caplog.text
    assert "uv run ci-review" not in caplog.text


def test_silent_for_a_new_draft(history, caplog):
    _history(history, draft="Something else entirely, a different article.")
    with caplog.at_level(logging.WARNING, logger="ci_article_review.pipeline"):
        assert pipeline._warn_if_draft_already_reviewed(DRAFT, HANDOFF, "h", "p") == []
    assert caplog.text == ""


def test_a_failing_lookup_never_stops_the_run(history, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history unreadable")

    monkeypatch.setattr(pipeline.reproducibility, "prior_runs_of_this_draft", boom)
    assert pipeline._warn_if_draft_already_reviewed(DRAFT, HANDOFF, "h", "p") == []
