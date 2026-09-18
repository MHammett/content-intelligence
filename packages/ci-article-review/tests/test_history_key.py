"""An article's history directory survives a change of title.

The directory is slugged from whatever key it is given, which made the title the
de-facto primary key. Revising the title then forked the history: one article
became "…They Have Eight of Them.", "…Ten of Them." and "…Twelve of Them.",
which slugged to three directories. Two things break quietly when that happens.
Delta comparison looks for the prior run in the new directory and finds none, so
a revised article reports as a first run. And voice_pattern_report counts
distinct articles by directory to decide whether a phrasing habit recurs across
the body of work — at MIN_ARTICLES = 3, one article split three ways can clear
that bar entirely on its own.

``History key:`` in the handoff pins the directory to something stable.
"""

from ci_article_review.handoff_parser import parse_draft_submission
from ci_article_review.history import _slug
from ci_article_review.pipeline import _history_key


class TestHistoryKeyResolution:
    def test_explicit_key_wins_over_the_title(self):
        handoff = {"history_key": "dc-environment", "title": "Some Long Title"}
        assert _history_key(handoff) == "dc-environment"

    def test_title_is_used_when_no_key_is_given(self):
        assert _history_key({"title": "Some Long Title"}) == "Some Long Title"

    def test_blank_key_falls_back_to_the_title(self):
        handoff = {"history_key": "   ", "title": "Some Long Title"}
        assert _history_key(handoff) == "Some Long Title"

    def test_missing_everything_is_empty_not_an_error(self):
        assert _history_key({}) == ""


class TestTitleRevisionsShareOneDirectory:
    """The regression this exists to prevent."""

    TITLES = [
        "Data Centers Don't Have an Environmental Record. They Have Eight of Them.",
        "Data Centers Don't Have an Environmental Record. They Have Ten of Them.",
        "Data Centers Don't Have an Environmental Record. They Have Twelve of Them.",
    ]

    def test_titles_alone_fork_into_separate_directories(self):
        """Documents the old behaviour, so the fix is visibly a fix."""
        slugs = {_slug(_history_key({"title": t})) for t in self.TITLES}
        assert len(slugs) == 3

    def test_a_shared_key_keeps_them_together(self):
        slugs = {
            _slug(_history_key({"history_key": "dc-environment", "title": t}))
            for t in self.TITLES
        }
        assert slugs == {"dc-environment"}


class TestHandoffParsing:
    def _handoff(self, extra_line=""):
        return parse_draft_submission(
            "DRAFT SUBMISSION HANDOFF\n"
            "Article: A Real Title Here\n"
            "Publication: mikehammett\n"
            f"{extra_line}"
            "PRIMARY CLAIM\nA claim.\n\n"
            "DRAFT\nBody text.\n"
        )

    def test_history_key_is_parsed(self):
        got = self._handoff("History key: dc-environment\n")
        assert got["history_key"] == "dc-environment"

    def test_absent_line_yields_empty_string(self):
        assert self._handoff()["history_key"] == ""

    def test_handoff_without_the_line_still_keys_on_title(self):
        assert _history_key(self._handoff()) == "A Real Title Here"

    def test_an_unfilled_key_still_keys_on_title(self):
        """The template's own placeholder, left in. It used to be the key, so
        every handoff that left it filed under one shared directory, as a
        blank key did before it stopped taking the next line."""
        got = self._handoff(
            "History key: [Optional but recommended. A short stable name for "
            "this piece, used\n"
            "as its history directory.]\n"
        )
        assert got["history_key"] == ""
        assert _history_key(got) == "A Real Title Here"
