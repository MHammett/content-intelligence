"""Unit tests for history — run persistence and prior-run selection.

The prior run is selected by actual execution time, not by ``run_number - 1``.
Run numbers come from the handoff's ``Pipeline run:`` field, which an author
increments by hand, so two executions of the same handoff both land at the same
N — see ``test_repeat_execution_at_same_run_number_*``.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ci_article_review import consolidation, history as hist

TITLE = "Test Article"


def _ts(day, hour=0, minute=0, second=0):
    return datetime(2026, 8, day, hour, minute, second, tzinfo=timezone.utc)


def _save(root, run_number, ts, draft="body text here", claim="", consensus=None):
    """Write a report the way a real run would, via save_run."""
    report = {
        "generated": ts.isoformat(),
        "run_number": run_number,
        "article_title": TITLE,
        "corrected_draft": draft,
        "primary_claim": claim,
        "section_1_consensus": consensus or [],
    }
    paths = hist.save_run(str(root), TITLE, run_number, report, [], run_ts=ts)
    return paths["report_path"]


class TestFindPriorReport:
    def test_no_history_returns_none(self, tmp_path):
        report, path = hist.load_prior_report(str(tmp_path), TITLE)
        assert report is None
        assert path is None

    def test_first_run_has_no_prior(self, tmp_path):
        run_ts = _ts(1, 10)
        report, path = hist.load_prior_report(str(tmp_path), TITLE, before_ts=run_ts)
        assert report is None
        assert path is None
        # ...and the run's own report, saved afterwards, is not its own prior.
        _save(tmp_path, 1, run_ts)
        report, _ = hist.load_prior_report(str(tmp_path), TITLE, before_ts=run_ts)
        assert report is None

    def test_normal_increment_compares_against_previous_run(self, tmp_path):
        _save(tmp_path, 1, _ts(1, 10), draft="first draft")
        _save(tmp_path, 2, _ts(2, 10), draft="second draft")
        report, path = hist.load_prior_report(
            str(tmp_path), TITLE, before_ts=_ts(3, 10)
        )
        assert report["run_number"] == 2
        assert report["corrected_draft"] == "second draft"
        assert path.name == "run_2_20260802_100000_report.json"

    def test_repeat_execution_at_same_run_number_picks_the_repeat(self, tmp_path):
        """The bug: a second run_2 must compare against the first run_2, not run_1."""
        _save(tmp_path, 1, _ts(1, 10), draft="v20 draft")
        _save(tmp_path, 2, _ts(2, 0, 54, 52), draft="v21 draft")
        report, path = hist.load_prior_report(
            str(tmp_path), TITLE, before_ts=_ts(2, 2, 13, 55)
        )
        assert path.name == "run_2_20260802_005452_report.json"
        assert report["corrected_draft"] == "v21 draft"

    def test_history_with_repeats_at_several_run_numbers(self, tmp_path):
        _save(tmp_path, 1, _ts(1, 8))
        _save(tmp_path, 1, _ts(1, 9))
        _save(tmp_path, 2, _ts(2, 8))
        _save(tmp_path, 2, _ts(2, 9))
        _save(tmp_path, 3, _ts(3, 8))
        _save(tmp_path, 3, _ts(3, 9))

        # Each execution sees the one immediately before it, whatever its number.
        expected = [
            (_ts(1, 9), "run_1_20260801_080000_report.json"),
            (_ts(2, 8), "run_1_20260801_090000_report.json"),
            (_ts(2, 9), "run_2_20260802_080000_report.json"),
            (_ts(3, 8), "run_2_20260802_090000_report.json"),
            (_ts(3, 9), "run_3_20260803_080000_report.json"),
        ]
        for before_ts, expected_name in expected:
            path = hist.find_prior_report_path(
                str(tmp_path), TITLE, before_ts=before_ts
            )
            assert path.name == expected_name

    def test_no_before_ts_returns_newest(self, tmp_path):
        _save(tmp_path, 1, _ts(1, 10))
        _save(tmp_path, 2, _ts(2, 10))
        path = hist.find_prior_report_path(str(tmp_path), TITLE)
        assert path.name == "run_2_20260802_100000_report.json"

    def test_lower_run_number_written_later_still_wins(self, tmp_path):
        """Execution order, not numeric order, decides. An author who re-declares
        run 1 after run 2 gets compared against run 2's output."""
        _save(tmp_path, 2, _ts(2, 10), draft="run two draft")
        _save(tmp_path, 1, _ts(3, 10), draft="re-declared as run one")
        report, _ = hist.load_prior_report(str(tmp_path), TITLE, before_ts=_ts(4, 10))
        assert report["corrected_draft"] == "re-declared as run one"

    def test_double_digit_run_numbers_order_by_time_not_string(self, tmp_path):
        _save(tmp_path, 9, _ts(9, 10))
        _save(tmp_path, 10, _ts(10, 10))
        path = hist.find_prior_report_path(str(tmp_path), TITLE, before_ts=_ts(11, 10))
        assert path.name == "run_10_20260810_100000_report.json"

    def test_legacy_untimestamped_filename_uses_generated_field(self, tmp_path):
        """Early runs wrote run_N_report.json with no timestamp in the name."""
        d = tmp_path / hist._slug(TITLE)
        d.mkdir(parents=True)
        legacy = d / "run_1_report.json"
        legacy.write_text(
            json.dumps(
                {
                    "generated": _ts(1, 10).isoformat(),
                    "run_number": 1,
                    "corrected_draft": "legacy draft",
                }
            ),
            encoding="utf-8",
        )
        report, path = hist.load_prior_report(
            str(tmp_path), TITLE, before_ts=_ts(2, 10)
        )
        assert path.name == "run_1_report.json"
        assert report["corrected_draft"] == "legacy draft"

        # And it loses to a newer timestamped run, as its generated date says it should.
        _save(tmp_path, 2, _ts(2, 10), draft="newer draft")
        report, _ = hist.load_prior_report(str(tmp_path), TITLE, before_ts=_ts(3, 10))
        assert report["corrected_draft"] == "newer draft"

    def test_legacy_report_after_before_ts_is_excluded(self, tmp_path):
        d = tmp_path / hist._slug(TITLE)
        d.mkdir(parents=True)
        (d / "run_1_report.json").write_text(
            json.dumps({"generated": _ts(5, 10).isoformat(), "run_number": 1}),
            encoding="utf-8",
        )
        assert hist.find_prior_report_path(str(tmp_path), TITLE, _ts(2, 10)) is None

    def test_unreadable_report_yields_no_prior(self, tmp_path):
        d = tmp_path / hist._slug(TITLE)
        d.mkdir(parents=True)
        (d / "run_1_20260801_100000_report.json").write_text("{ not json", "utf-8")
        report, path = hist.load_prior_report(
            str(tmp_path), TITLE, before_ts=_ts(2, 10)
        )
        assert report is None
        assert path is None

    def test_other_files_in_history_dir_are_ignored(self, tmp_path):
        _save(tmp_path, 1, _ts(1, 10))
        d = tmp_path / hist._slug(TITLE)
        (d / "run_9_20260809_100000_review.md").write_text("not a report", "utf-8")
        (d / "disposition.log").write_text("note", "utf-8")
        path = hist.find_prior_report_path(str(tmp_path), TITLE, before_ts=_ts(2, 10))
        assert path.name == "run_1_20260801_100000_report.json"

    def test_history_is_per_article_title(self, tmp_path):
        _save(tmp_path, 1, _ts(1, 10))
        assert (
            hist.find_prior_report_path(str(tmp_path), "Different Article", _ts(2, 10))
            is None
        )


class TestDeltaAgainstRepeatExecution:
    """End-to-end: an unedited re-run at the same run number reports no change."""

    _DRAFT = "# Heading\n\nThe draft body, unchanged between the two executions.\n"
    _CLAIM = "Data centers have ten environmental records."

    def _build(self, tmp_path, before_ts):
        prior_report, prior_path = hist.load_prior_report(
            str(tmp_path), TITLE, before_ts=before_ts
        )
        return consolidation.build_report(
            article_title=TITLE,
            publication_name="test_pub",
            run_number=2,
            corrected_draft=self._DRAFT,
            lt_result={"change_log": []},
            results={},
            ensemble_cfg={},
            api_call_log=[],
            prior_report=prior_report,
            prior_report_path=prior_path,
            primary_claim=self._CLAIM,
        )

    def test_rerun_of_identical_draft_shows_no_change(self, tmp_path):
        # run 1: an older, materially different draft.
        _save(
            tmp_path,
            1,
            _ts(1, 10),
            draft="# Heading\n\nA completely different earlier draft with other words.\n",
            claim="An entirely different earlier claim.",
        )
        # run 2, first execution: the draft under review.
        _save(tmp_path, 2, _ts(2, 0, 54, 52), draft=self._DRAFT, claim=self._CLAIM)

        # run 2, second execution: same handoff, nothing edited.
        report = self._build(tmp_path, before_ts=_ts(2, 2, 13, 55))

        delta = report["delta"]
        assert delta["word_change_pct"] == 0.0
        assert delta["claim_changed"] is False
        assert delta["structure_changed"] is False
        assert delta["new_consensus_count"] == 0
        assert delta["resolved_consensus_count"] == 0
        assert (
            consolidation.rerun_recommended(delta, {"word_change_threshold_pct": 15})
            is False
        )

    def test_delta_records_what_it_compared_against(self, tmp_path):
        _save(tmp_path, 1, _ts(1, 10), draft="older draft")
        _save(tmp_path, 2, _ts(2, 0, 54, 52), draft=self._DRAFT, claim=self._CLAIM)

        report = self._build(tmp_path, before_ts=_ts(2, 2, 13, 55))

        compared = report["delta"]["compared_against"]
        assert compared["report"] == "run_2_20260802_005452_report.json"
        assert compared["run_number"] == 2
        assert compared["generated"] == _ts(2, 0, 54, 52).isoformat()

    def test_edited_draft_at_a_new_run_number_still_reports_change(self, tmp_path):
        _save(
            tmp_path,
            1,
            _ts(1, 10),
            draft="# Heading\n\nShort earlier body.\n",
            claim=self._CLAIM,
        )
        report = self._build(tmp_path, before_ts=_ts(2, 10))

        delta = report["delta"]
        assert delta["word_change_pct"] > 0
        assert (
            delta["compared_against"]["report"] == "run_1_20260801_100000_report.json"
        )


class TestSaveRun:
    def test_saved_paths_carry_the_run_timestamp(self, tmp_path):
        ts = _ts(1, 10, 30, 15)
        paths = hist.save_run(
            str(tmp_path), TITLE, 3, {"run_number": 3, "generated": ts.isoformat()}, []
        )
        assert paths["report_path"]

        paths = hist.save_run(
            str(tmp_path),
            TITLE,
            3,
            {"run_number": 3, "generated": ts.isoformat()},
            [],
            run_ts=ts,
        )
        for key in ("report_path", "corrections_path", "markdown_path"):
            assert "run_3_20260801_103015_" in paths[key]

    def test_two_executions_at_the_same_run_number_do_not_overwrite(self, tmp_path):
        first = _save(tmp_path, 2, _ts(2, 0, 54, 52), draft="first execution")
        second = _save(tmp_path, 2, _ts(2, 2, 13, 55), draft="second execution")
        assert first != second
        d = tmp_path / hist._slug(TITLE)
        assert len(list(d.glob("run_2_*_report.json"))) == 2


def test_prior_report_timestamp_falls_back_to_mtime(tmp_path):
    """No filename stamp and no parseable generated field — mtime is the last resort."""
    d = tmp_path / hist._slug(TITLE)
    d.mkdir(parents=True)
    path = d / "run_1_report.json"
    path.write_text(json.dumps({"run_number": 1}), encoding="utf-8")

    ts = hist._report_timestamp(path)
    assert ts.tzinfo is not None
    assert abs(ts - datetime.now(timezone.utc)) < timedelta(minutes=5)


class TestPlaceholderTitlesGetNoDirectory:
    """pipeline_history/ had "t/" and "title/" beside real articles.

    A one-character title is a parse failure or a template placeholder, not an
    article. Giving it a directory means it never accumulates a second run and
    only makes the real ones harder to find.
    """

    def test_a_one_character_title_is_untitled(self):
        assert hist._slug("t") == "untitled"

    def test_the_literal_word_title_is_untitled(self):
        assert hist._slug("title") == "untitled"

    def test_a_real_title_keeps_its_slug(self):
        slug = hist._slug("Data Centers Don't Have an Environmental Record")
        assert slug.startswith("data-centers-dont-have")

    def test_punctuation_does_not_pad_a_short_title(self):
        """ "a.b.c" is three characters of content, not five."""
        assert hist._slug("a.b.c") == "untitled"


class TestLookupsDoNotCreateDirectories:
    """Reading history must not write to it.

    ``find_prior_report_path`` resolved its directory through ``_run_dir``,
    which creates. Every run asks for a delta baseline through that path, so
    every first run of an article created its directory before anything decided
    to write one — and any run whose title was short or a placeholder created
    ``untitled/``. Deleting the strays did not stop them: an empty ``untitled/``
    reappeared in real history days after the last cleanup, from a lookup alone.
    """

    def test_prior_report_lookup_creates_nothing(self, tmp_path):
        assert hist.find_prior_report_path(str(tmp_path), TITLE) is None
        assert list(tmp_path.iterdir()) == []

    def test_a_placeholder_title_lookup_creates_no_untitled_dir(self, tmp_path):
        """The exact shape that put an empty untitled/ in pipeline_history."""
        assert hist.find_prior_report_path(str(tmp_path), "t") is None
        assert not (tmp_path / "untitled").exists()

    def test_run_number_lookup_creates_nothing(self, tmp_path):
        assert hist.existing_run_numbers(str(tmp_path), TITLE) == set()
        assert list(tmp_path.iterdir()) == []

    def test_repeated_lookups_stay_clean(self, tmp_path):
        for title in (TITLE, "t", "title", "Another Article Entirely"):
            hist.find_prior_report_path(str(tmp_path), title)
            hist.existing_run_numbers(str(tmp_path), title)
        assert list(tmp_path.iterdir()) == []

    def test_saving_still_creates_the_directory(self, tmp_path):
        """The writer keeps creating — only the read path stopped."""
        _save(tmp_path, 1, _ts(1))
        assert (tmp_path / hist._slug(TITLE)).is_dir()

    def test_lookup_still_finds_a_directory_that_exists(self, tmp_path):
        """Not creating must not mean not finding."""
        _save(tmp_path, 1, _ts(1))
        assert hist.find_prior_report_path(str(tmp_path), TITLE) is not None
        assert hist.existing_run_numbers(str(tmp_path), TITLE) == {1}


class TestRunNumberCollision:
    """The handoff declares the run number and authors forget to bump it."""

    def test_existing_numbers_are_read_from_disk(self, tmp_path):
        run_dir = tmp_path / hist._slug("A Real Article Title Here")
        run_dir.mkdir(parents=True)
        (run_dir / "run_3_20260101_000000_report.json").write_text(
            "{}", encoding="utf-8"
        )
        (run_dir / "run_16_20260101_000000_report.json").write_text(
            "{}", encoding="utf-8"
        )
        got = hist.existing_run_numbers(tmp_path, "A Real Article Title Here")
        assert got == {3, 16}

    def test_an_article_with_no_history_returns_empty(self, tmp_path):
        assert hist.existing_run_numbers(tmp_path, "Never Seen This Title") == set()

    def test_an_unreadable_history_root_never_raises(self, tmp_path):
        """A numbering nicety must not be able to fail a run."""
        assert (
            hist.existing_run_numbers(tmp_path / "nope", "Some Article Title") == set()
        )


class TestAGeneratedFileFailingDoesNotCostTheRun:
    """``save_run`` writes the report JSON, then three files derived from it.

    The derived writes used to call their renderer *inside* ``with open(...)``
    while catching only ``OSError``. So a renderer bug left a zero-byte file on
    disk and propagated out of ``save_run``, losing the corrections log written
    below it — the run kept its report JSON and nothing else.
    """

    def _report(self):
        return {
            "generated": _ts(1).isoformat(),
            "run_number": 1,
            "article_title": TITLE,
            "section_1_consensus": [],
        }

    def _corrections(self):
        return [{"category": "spelling", "original": "teh", "replacement": "the"}]

    def _files(self, root):
        return {p.name: p.stat().st_size for p in root.rglob("*") if p.is_file()}

    def test_a_broken_markdown_renderer_leaves_the_rest_of_the_run(
        self, tmp_path, monkeypatch
    ):
        def boom(report):
            raise ValueError("simulated renderer bug")

        monkeypatch.setattr(hist, "render_report_markdown", boom)
        paths = hist.save_run(
            str(tmp_path), TITLE, 1, self._report(), self._corrections(), run_ts=_ts(1)
        )

        assert paths["markdown_path"] is None
        assert paths["report_path"] is not None
        # The one the old behaviour lost: it is written after the markdown.
        assert paths["corrections_path"] is not None
        assert "the" in Path(paths["corrections_path"]).read_text(encoding="utf-8")

    def test_a_broken_renderer_leaves_no_zero_byte_file_behind(
        self, tmp_path, monkeypatch
    ):
        """An empty review.md is worse than an absent one — it reads as a run
        that rendered and found nothing to say.
        """

        def boom(report):
            raise ValueError("simulated renderer bug")

        monkeypatch.setattr(hist, "render_report_markdown", boom)
        hist.save_run(str(tmp_path), TITLE, 1, self._report(), [], run_ts=_ts(1))

        written = self._files(tmp_path)
        assert not any(name.endswith("_review.md") for name in written), written
        # Scoped to the derived documents: an empty corrections.log is normal
        # for a run with no corrections, and is not what this pins.
        derived = {n: size for n, size in written.items() if n.endswith(".md")}
        assert derived and all(size > 0 for size in derived.values()), written

    def test_a_broken_worklist_builder_is_survivable_too(self, tmp_path, monkeypatch):
        """The worklist write has the same shape and arrived later, so it is
        pinned separately rather than assumed to be covered.
        """

        def boom(report):
            raise KeyError("simulated builder bug")

        monkeypatch.setattr(hist, "build_worklist", boom)
        paths = hist.save_run(
            str(tmp_path), TITLE, 1, self._report(), self._corrections(), run_ts=_ts(1)
        )

        assert paths["worklist_path"] is None
        assert paths["markdown_path"] is not None
        assert paths["corrections_path"] is not None
        assert not any(name.endswith("_worklist.md") for name in self._files(tmp_path))

    def test_the_traceback_is_logged_rather_than_swallowed(
        self, tmp_path, monkeypatch, caplog
    ):
        """Catching broadly is only defensible because the failure is still
        reported — the report JSON is already safe on disk by this point.
        """

        def boom(report):
            raise ValueError("simulated renderer bug")

        monkeypatch.setattr(hist, "render_report_markdown", boom)
        with caplog.at_level(logging.ERROR):
            hist.save_run(str(tmp_path), TITLE, 1, self._report(), [], run_ts=_ts(1))

        assert "simulated renderer bug" in caplog.text
        assert "Traceback" in caplog.text

    def test_a_healthy_run_still_writes_every_file(self, tmp_path):
        paths = hist.save_run(
            str(tmp_path), TITLE, 1, self._report(), self._corrections(), run_ts=_ts(1)
        )

        for key in (
            "report_path",
            "markdown_path",
            "worklist_path",
            "corrections_path",
        ):
            assert paths[key] is not None, key
            assert Path(paths[key]).stat().st_size > 0, key
