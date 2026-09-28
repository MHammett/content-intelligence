"""Tests for --archive-only's own logic (``pipeline.run_archive_only``).

CLI-level argparse/dispatch wiring for this flag lives in test_pipeline_cli.py
(``TestArchiveOnlyArgparse``, ``TestArchiveOnlyFlowsIntoRunArchiveOnly``) — this
file covers what ``run_archive_only`` itself does: extracting URLs from the
draft's own citation block, feeding them through the same archiving primitives
Pass 3 uses (``wayback.check`` / ``resolver._submit_missing_archives``), and
rendering outcomes with ``report_markdown``'s own vocabulary rather than a
second copy of it.

Every test here mocks ``wayback.check``, ``_submit_missing_archives`` and
config loading at the module boundary, so none of it touches the network —
the whole point of ``--archive-only`` is that archiving is pure HTTP to
archive.org, and these tests only need to prove this code path builds the
right inputs and reads the right outputs, not that archive.org answers them.
"""

from unittest.mock import patch

import ci_article_review.pipeline as pipeline

_MINIMAL_CONFIG = {"api_keys": {}, "pipeline": {}}


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _run_archive_only(
    draft_path, wayback_check=None, submit=None, render=None, config=None
):
    """Call run_archive_only with the network/config boundary mocked out.

    Returns (mock_check, mock_submit, mock_render, printed_output).
    """
    with (
        patch("ci_article_review.pipeline.load_user_config", return_value={}),
        patch("ci_article_review.pipeline.load_publication_config", return_value={}),
        patch(
            "ci_article_review.pipeline.merge_configs",
            return_value=config or _MINIMAL_CONFIG,
        ),
        patch(
            "ci_article_review.adapters.citation.wayback.check",
            side_effect=wayback_check
            or (lambda url: {"archived": False, "snapshot_url": None}),
        ) as mock_check,
        patch(
            "ci_article_review.adapters.citation.resolver._submit_missing_archives",
            side_effect=submit,
        ) as mock_submit,
        patch(
            "ci_article_review.report_markdown._render_archive_pair",
            side_effect=render
            or (lambda citation, indent="  ": [f"  - Archive: stub {citation['url']}"]),
        ) as mock_render,
    ):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pipeline.run_archive_only(draft_path, "myblog")

    return mock_check, mock_submit, mock_render, buf.getvalue()


class TestUrlExtractionFromSourcesSection:
    """No network anywhere in this class: wayback.check/submit are stubbed."""

    def test_extracts_every_url_including_a_multi_url_entry(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "Body text with a claim. [1] Another claim. [2]\n\n"
            "## Sources\n\n"
            "[1] First source. https://a.example/one\n\n"
            "[2] Second source, two documents. "
            "https://b.example/two-a and https://b.example/two-b\n",
        )
        mock_check, mock_submit, _, _ = _run_archive_only(draft)

        checked_urls = {call.args[0] for call in mock_check.call_args_list}
        assert checked_urls == {
            "https://a.example/one",
            "https://b.example/two-a",
            "https://b.example/two-b",
        }

        results = mock_submit.call_args.args[0]
        by_marker = {}
        for entry in results:
            by_marker.setdefault(entry["marker"], []).append(entry["url"])
        assert by_marker["1"] == ["https://a.example/one"]
        assert by_marker["2"] == [
            "https://b.example/two-a",
            "https://b.example/two-b",
        ]
        assert all(entry["resolved"] is True for entry in results)

    def test_url_cited_under_two_markers_is_checked_once(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n"
            "[1] First mention. https://shared.example/doc\n\n"
            "[2] Same document, cited again. https://shared.example/doc\n",
        )
        mock_check, mock_submit, _, _ = _run_archive_only(draft)

        assert mock_check.call_count == 1

        results = mock_submit.call_args.args[0]
        assert [r["marker"] for r in results] == ["1", "2"]
        assert all(r["url"] == "https://shared.example/doc" for r in results)
        # Each entry gets its own dict, not a shared object two mutations
        # inside _submit_missing_archives could step on.
        assert results[0]["wayback"] is not results[1]["wayback"]
        assert results[0]["wayback"] == results[1]["wayback"]

    def test_no_citation_block_does_nothing(self, tmp_path):
        draft = _write(tmp_path, "draft.md", "Just body text, no citation block.\n")
        mock_check, mock_submit, _, out = _run_archive_only(draft)

        mock_check.assert_not_called()
        mock_submit.assert_not_called()
        assert "Nothing to archive" in out
        assert "Estimated cost: $0.0000 (no model calls)" in out

    def test_citation_block_with_no_urls_does_nothing(self, tmp_path):
        draft = _write(
            tmp_path, "draft.md", "## Sources\n\n[1] A source with no URL at all.\n"
        )
        mock_check, mock_submit, _, out = _run_archive_only(draft)

        mock_check.assert_not_called()
        mock_submit.assert_not_called()
        assert "Nothing to archive" in out
        assert "Estimated cost: $0.0000 (no model calls)" in out

    def test_a_full_handoff_documents_draft_section_is_still_read(self, tmp_path):
        """--draft mode's file has headers above the article; the block still parses."""
        draft = _write(
            tmp_path,
            "handoff.md",
            "PRIMARY CLAIM: Something happened.\n\n"
            "TARGET AUDIENCE: General readers\n\n"
            "DRAFT:\n\n"
            "Body text. [1]\n\n"
            "## Sources\n\n"
            "[1] A source. https://a.example/one\n",
        )
        mock_check, mock_submit, _, _ = _run_archive_only(draft)

        assert {c.args[0] for c in mock_check.call_args_list} == {
            "https://a.example/one"
        }
        assert len(mock_submit.call_args.args[0]) == 1


class TestOutputReusesReportMarkdownVocabulary:
    """The whole point of calling _render_archive_pair is not re-inventing its wording."""

    def test_render_archive_pair_called_once_per_url_with_its_own_citation_dict(
        self, tmp_path
    ):
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n[1] One source. https://a.example/one\n",
        )
        _, _, mock_render, out = _run_archive_only(draft)

        mock_render.assert_called_once()
        citation = mock_render.call_args.args[0]
        assert citation["url"] == "https://a.example/one"
        assert citation["resolved"] is True
        assert "[1]" in out
        assert "stub https://a.example/one" in out

    def test_prints_estimated_cost_zero_with_results(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n[1] One source. https://a.example/one\n",
        )
        *_, out = _run_archive_only(draft)
        assert "Estimated cost: $0.0000 (no model calls)" in out

    def test_summary_separates_fresh_stale_and_missing(self, tmp_path):
        """ "Have a snapshot" is not one fact: a stale copy predates the page as
        it reads now, and the first version of this summary counted it as done."""
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n"
            "[1] Fresh. https://a.example/fresh\n\n"
            "[2] Stale. https://a.example/stale\n\n"
            "[3] Missing. https://a.example/missing\n",
        )

        def fake_check(url):
            if url.endswith("fresh"):
                return {
                    "archived": True,
                    "snapshot_url": "https://web.archive.org/web/2/fresh",
                    "snapshot_stale": False,
                }
            if url.endswith("stale"):
                return {
                    "archived": True,
                    "snapshot_url": "https://web.archive.org/web/1/stale",
                    "snapshot_stale": True,
                }
            return {"archived": False, "snapshot_url": None}

        *_, out = _run_archive_only(draft, wayback_check=fake_check)
        assert (
            "3 citation URL(s): 1 with a fresh Wayback snapshot, "
            "1 with only a stale one, 1 with none." in out
        )


class TestALookupThatDidNotCompleteIsNotCountedAsNoSnapshot:
    """The second live run: archive.org refused both lookups, each source printed
    "NOT CHECKED", and the summary still said "0 with a snapshot, 2 with none"."""

    def test_unchecked_urls_are_reported_as_such_not_as_missing(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n"
            "[1] Refused lookup. https://a.example/one\n\n"
            "[2] Confirmed absent. https://a.example/two\n",
        )

        def fake_check(url):
            if url.endswith("one"):
                return {"archived": None, "error": "archive.org refused the lookup"}
            return {"archived": False, "snapshot_url": None}

        *_, out = _run_archive_only(draft, wayback_check=fake_check)
        assert "1 with none, 1 not checked" in out
        assert "says nothing about whether they are archived" in out

    def test_no_unchecked_clause_when_every_lookup_completed(self, tmp_path):
        draft = _write(
            tmp_path, "draft.md", "## Sources\n\n[1] One. https://a.example/one\n"
        )
        *_, out = _run_archive_only(draft)
        assert "not checked" not in out


class TestAStaleSnapshotDoesNotHideTheReArchiveOutcome:
    """The bug the first live run exposed. ``_render_archive_pair`` shows a
    snapshot that exists and stops, so a stale one whose re-capture was
    requested and refused printed only the old snapshot flagged STALE, and the
    outcome the mode exists to report went unsaid."""

    _STALE = {
        "archived": True,
        "snapshot_url": "https://web.archive.org/web/20240812234508/https://a.example/x",
        "snapshot_stale": True,
    }

    def _submit_that_records(self, outcome, detail):
        def submit(results, *args, **kwargs):
            for entry in results:
                entry["wayback"]["archive_outcome"] = outcome
                entry["wayback"]["archive_outcome_detail"] = detail

        return submit

    def test_a_refused_recapture_is_reported_beside_the_stale_snapshot(self, tmp_path):
        draft = _write(
            tmp_path, "draft.md", "## Sources\n\n[1] Old. https://a.example/x\n"
        )
        *_, out = _run_archive_only(
            draft,
            wayback_check=lambda url: dict(self._STALE),
            submit=self._submit_that_records(
                "submit_failed", "archive.org refused the request: try tomorrow"
            ),
        )
        assert (
            "Re-archive attempt: submission failed — archive.org refused the "
            "request: try tomorrow." in out
        )
        assert "1 with only a stale one" in out

    def test_a_successful_recapture_adds_no_extra_line(self, tmp_path):
        draft = _write(
            tmp_path, "draft.md", "## Sources\n\n[1] Old. https://a.example/x\n"
        )

        def recaptured(results, *args, **kwargs):
            wb = results[0]["wayback"]
            wb["archive_outcome"] = "archived"
            wb["snapshot_url"] = (
                "https://web.archive.org/web/20260927/https://a.example/x"
            )
            wb["snapshot_stale"] = False

        *_, out = _run_archive_only(
            draft, wayback_check=lambda url: dict(self._STALE), submit=recaptured
        )
        assert "Re-archive attempt" not in out
        assert "1 with a fresh Wayback snapshot" in out

    def test_a_fresh_snapshot_nobody_resubmitted_adds_no_line(self, tmp_path):
        draft = _write(
            tmp_path, "draft.md", "## Sources\n\n[1] Fine. https://a.example/x\n"
        )
        fresh = {**self._STALE, "snapshot_stale": False}
        *_, out = _run_archive_only(draft, wayback_check=lambda url: dict(fresh))
        assert "Re-archive attempt" not in out


class TestArchiveOnlyTouchesNoModelProvider:
    """The literal requirement: zero calls to the shared model-provider funnel.

    ci_core.llm.call_provider is the single choke point every model call in
    this codebase goes through — the ensemble (pipeline.py), citation
    relevance checking (resolver.py), citation reask (reask.py) and both SEO
    passes (seo_suggest.py, seo_content.py) all call it and nothing else.
    Patching it here and asserting it is never called is a stronger guarantee
    than checking each of those call sites individually: it would fail for any
    of them, including ones added later.
    """

    def test_zero_calls_to_call_provider(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n[1] One source. https://a.example/one\n",
        )
        with patch("ci_core.llm.call_provider") as mock_call_provider:
            _run_archive_only(draft)

        mock_call_provider.assert_not_called()
