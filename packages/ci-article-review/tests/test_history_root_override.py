"""``CI_HISTORY_ROOT``: one history directory for every worktree.

``pipeline_history/`` is resolved against the working directory, so each git
worktree had its own, and removing a worktree deleted its runs (issue #266).
The variable moves all of it. These tests pin what "all of it" means: the
resolver, the two report CLIs that read history, the citation indexes that scan
it, and the pipeline's own writers (the daily log, saved runs, the replay tree
and the archive-only lookup), which are covered in ``test_pipeline_end_to_end.py``
and ``test_pipeline_archive_only.py``.

Every directory here is under ``tmp_path``. The suite-wide fixture
``history_root_env_unset`` (conftest.py) clears the variable first, so nothing
here depends on, or can write into, the developer's real shared store.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review import history_analytics, voice_pattern_report
from ci_article_review.adapters.citation import resolver

ENV = "CI_HISTORY_ROOT"


class TestResolveHistoryRoot:
    def test_unset_is_todays_relative_default(self):
        assert history_analytics.resolve_history_root() == "pipeline_history"

    def test_the_variable_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path / "shared"))
        assert history_analytics.resolve_history_root() == str(tmp_path / "shared")

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_variable_is_unset(self, blank, monkeypatch):
        """``set CI_HISTORY_ROOT=`` in cmd leaves an empty value, not no value."""
        monkeypatch.setenv(ENV, blank)
        assert history_analytics.resolve_history_root() == "pipeline_history"

    def test_a_caller_supplied_default_is_what_unset_falls_back_to(self):
        assert history_analytics.resolve_history_root("elsewhere") == "elsewhere"

    def test_the_variable_beats_a_caller_supplied_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path))
        assert history_analytics.resolve_history_root("elsewhere") == str(tmp_path)

    def test_a_leading_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows reads this
        monkeypatch.setenv(ENV, "~/shared-history")
        assert Path(history_analytics.resolve_history_root()) == (
            tmp_path / "shared-history"
        )

    def test_it_is_read_on_every_call(self, tmp_path, monkeypatch):
        """Not cached: a wrapper that exports the variable after import is honoured."""
        assert history_analytics.resolve_history_root() == "pipeline_history"
        monkeypatch.setenv(ENV, str(tmp_path))
        assert history_analytics.resolve_history_root() == str(tmp_path)


class TestPipelineRoot:
    def test_unset_is_the_module_constant(self):
        assert pipeline._history_root() == pipeline.HISTORY_ROOT

    def test_a_patched_constant_still_moves_it_when_unset(self, tmp_path):
        """What ``tmp_history_root`` and ``_stubbed_run`` rely on."""
        with patch.object(pipeline, "HISTORY_ROOT", str(tmp_path / "h")):
            assert pipeline._history_root() == str(tmp_path / "h")

    def test_the_variable_beats_the_constant(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path / "shared"))
        with patch.object(pipeline, "HISTORY_ROOT", str(tmp_path / "h")):
            assert pipeline._history_root() == str(tmp_path / "shared")


class TestHistoryReportCli:
    def test_default_follows_the_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path))
        args = history_analytics.build_parser().parse_args([])
        assert args.history_root == str(tmp_path)

    def test_default_is_unchanged_when_unset(self):
        args = history_analytics.build_parser().parse_args([])
        assert args.history_root == "pipeline_history"

    def test_the_flag_beats_the_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path / "shared"))
        args = history_analytics.build_parser().parse_args(
            ["--history-root", str(tmp_path / "flag")]
        )
        assert args.history_root == str(tmp_path / "flag")

    def test_build_report_without_a_root_reads_the_variable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(ENV, str(tmp_path))
        result = history_analytics.build_history_report()
        assert result["history_root"] == str(tmp_path)

    def test_the_help_names_the_variable(self):
        assert ENV in history_analytics.build_parser().format_help()


class TestVoicePatternsCli:
    def test_default_follows_the_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path))
        args = voice_pattern_report.build_parser().parse_args([])
        assert args.history_root == str(tmp_path)

    def test_the_flag_beats_the_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path / "shared"))
        args = voice_pattern_report.build_parser().parse_args(
            ["--history-root", str(tmp_path / "flag")]
        )
        assert args.history_root == str(tmp_path / "flag")

    def test_build_report_without_a_root_reads_the_variable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(ENV, str(tmp_path))
        result = voice_pattern_report.build_voice_pattern_report()
        assert result["history_root"] == str(tmp_path)


class TestCitationIndexesScanTheOverride:
    """Both indexes default to "the history", and that is now the override."""

    @pytest.mark.parametrize(
        "index", [resolver.build_checksum_index, resolver.build_pending_capture_index]
    )
    def test_a_default_scan_goes_to_the_variable(self, index, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path))
        seen = []
        monkeypatch.setattr(
            history_analytics,
            "load_reports",
            lambda root, *a, **k: seen.append(root) or [],
        )

        index()

        assert seen == [str(tmp_path)]


class TestMainWritesItsLogUnderTheOverride:
    """``main()`` creates the history directory before it does anything else.

    On a checkout with the variable set that must be the shared directory, and
    the working directory must not grow a ``pipeline_history/`` of its own: that
    stray directory is the very thing that was being lost with each worktree.
    """

    def test_the_daily_log_and_directory(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        monkeypatch.setenv(ENV, str(shared))
        argv = [
            "pipeline.py",
            "--raw-draft",
            "draft.md",
            "--publication",
            "myblog",
            "--archive-only",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("ci_article_review.pipeline.logging.FileHandler") as file_handler,
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_archive_only"),
        ):
            pipeline.main()

        assert shared.is_dir()
        assert Path(file_handler.call_args.args[0]).parent == shared
