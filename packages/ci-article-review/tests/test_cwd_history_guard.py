"""The guard that keeps tests out of the working directory's ``pipeline_history/``.

``cwd_history_untouched`` (conftest.py) fails a test that creates that
directory, or looks for history in it. Creating is caught by looking at the
directory afterwards. Looking is caught by ``cwd_history_reads``, which wraps
the history readers, and what these tests pin is that the wrapping reaches every
one of them: a reader it misses is a test that can read a developer's real
history and still pass in CI, where there is none.

The tests here look in the working directory's history on purpose, so each one
empties ``cwd_history_reads`` once it has checked it. Left full, the guard would
fail the tests that show it works.

None of them reads anything. Each names a directory that does not exist, so a
lookup stops at its first ``stat``: an article no run has written, or a
directory inside the history that nothing has made.
"""

import ast
from pathlib import Path

import pytest

import ci_article_review
from ci_article_review import history, history_analytics, pipeline, reproducibility
from ci_article_review.adapters.citation import resolver

_ARTICLE = "an article no real run has written"

#: Inside the working directory's history, and missing. The readers that list a
#: directory (``iter_reports``) are pointed here rather than at the history
#: itself, which in a checkout that has run reviews is hundreds of reports.
_MISSING_INSIDE = str(Path("pipeline_history") / "_no_such_dir_for_the_guard_tests")


def _readers(root, missing_inside):
    """One call per way history is read, each given the root to look in.

    ``root`` is what the ``_existing_run_dir`` callers take: they ``stat`` one
    slug beneath it and never list it. ``missing_inside`` is what the callers
    that list a directory take.
    """
    return {
        "history._existing_run_dir": lambda: history._existing_run_dir(root, _ARTICLE),
        "history.existing_run_numbers": lambda: history.existing_run_numbers(
            root, _ARTICLE
        ),
        "history.load_prior_report": lambda: history.load_prior_report(root, _ARTICLE),
        # Its own copy of ``_existing_run_dir``, imported by value.
        "reproducibility._load_prior_reports": lambda: (
            reproducibility._load_prior_reports(root, _ARTICLE)
        ),
        "history_analytics.iter_reports": lambda: list(
            history_analytics.iter_reports(missing_inside)
        ),
        "history_analytics.load_reports": lambda: history_analytics.load_reports(
            missing_inside
        ),
        # How a draft run compares a citation with the last time it was fetched.
        "resolver.build_checksum_index": lambda: resolver.build_checksum_index(
            missing_inside
        ),
        "resolver.build_pending_capture_index": lambda: (
            resolver.build_pending_capture_index(missing_inside)
        ),
    }


_READER_IDS = list(_readers("pipeline_history", _MISSING_INSIDE))


class TestReadsAreRecorded:
    @pytest.mark.parametrize("reader", _READER_IDS)
    def test_a_lookup_in_the_cwd_history_is_recorded(self, reader, cwd_history_reads):
        _readers("pipeline_history", _MISSING_INSIDE)[reader]()

        assert cwd_history_reads, f"{reader} looked in the working directory unseen"
        cwd_history_reads.clear()

    @pytest.mark.parametrize("reader", _READER_IDS)
    def test_a_lookup_anywhere_else_is_not(self, reader, tmp_path, cwd_history_reads):
        _readers(tmp_path, tmp_path / "missing")[reader]()

        assert cwd_history_reads == []

    def test_the_replay_tree_is_inside_the_cwd_history(self, cwd_history_reads):
        # A --replay run reads and writes pipeline_history/_replay/.
        history._existing_run_dir(Path("pipeline_history") / "_replay", _ARTICLE)

        assert cwd_history_reads
        cwd_history_reads.clear()

    def test_a_directory_that_only_shares_the_prefix_is_not(
        self, tmp_path, cwd_history_reads
    ):
        # ``pipeline_history_old`` starts with the same characters and is not it.
        history._existing_run_dir(Path("pipeline_history_old"), _ARTICLE)

        assert cwd_history_reads == []


class TestTmpHistoryRootIsTheFix:
    def test_it_takes_every_read_a_draft_run_makes_out_of_the_cwd(
        self, tmp_history_root, cwd_history_reads
    ):
        """What the guard's message tells a test to do has to work.

        ``run_draft_pipeline`` asks for the article's run numbers and prior
        report, measures reproducibility against the runs it finds, and hands
        ``HISTORY_ROOT`` to ``resolve_citations``, which indexes every report
        under it.
        """
        root = pipeline.HISTORY_ROOT
        history.existing_run_numbers(root, _ARTICLE)
        history.load_prior_report(root, _ARTICLE)
        reproducibility._load_prior_reports(root, _ARTICLE)
        resolver.build_checksum_index(root)
        resolver.build_pending_capture_index(root)

        assert cwd_history_reads == []
        assert Path(root) == tmp_history_root

    def test_without_it_the_same_reads_are_recorded(self, cwd_history_reads):
        """The control for the test above, which would pass if nothing were watched."""
        root = pipeline.HISTORY_ROOT
        for read in (
            lambda: history.existing_run_numbers(root, _ARTICLE),
            lambda: history.load_prior_report(root, _ARTICLE),
            lambda: reproducibility._load_prior_reports(root, _ARTICLE),
        ):
            cwd_history_reads.clear()
            read()
            assert cwd_history_reads
        cwd_history_reads.clear()


class TestWatchingChangesNoAnswer:
    """A wrapper that dropped an argument or a return value would break every
    test that reads history for real, and say nothing about why."""

    def test_the_readers_still_find_what_is_there(self, tmp_path, cwd_history_reads):
        article_dir = tmp_path / "an-article-title"
        article_dir.mkdir()
        report = article_dir / "run_3_20200101_000000_report.json"
        report.write_text("{}", encoding="utf-8")

        assert history._existing_run_dir(tmp_path, "an article title") == article_dir
        assert history.existing_run_numbers(tmp_path, "an article title") == {3}
        assert list(history_analytics.iter_reports(tmp_path)) == [
            ("an-article-title", report, {})
        ]
        assert (
            len(reproducibility._load_prior_reports(tmp_path, "an article title")) == 1
        )
        assert cwd_history_reads == []

    def test_a_keyword_argument_reaches_the_reader(self, tmp_path):
        (tmp_path / "an-article-title").mkdir()

        assert history._existing_run_dir(
            history_root=tmp_path, article_title="an article title"
        ) == (tmp_path / "an-article-title")


#: Where a history reader is bound by ``from x import name``, as
#: ``(file within ci_article_review, name)``. ``cwd_history_reads`` patches each
#: one where it is bound, so a pair missing from its list is a reader the guard
#: does not see.
_BOUND_BY_VALUE = {("reproducibility.py", "_existing_run_dir")}


def test_no_history_reader_is_bound_by_value_where_the_guard_does_not_look():
    package = Path(ci_article_review.__file__).parent
    found = set()
    for source in package.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name in ("_existing_run_dir", "iter_reports"):
                    found.add((source.relative_to(package).as_posix(), alias.name))

    assert found == _BOUND_BY_VALUE, (
        f"{sorted(found ^ _BOUND_BY_VALUE)} binds a history reader by value, so "
        "patching the module that defines it does not reach that copy. Add it to "
        "_HISTORY_READERS in conftest.py, and to _BOUND_BY_VALUE here."
    )
