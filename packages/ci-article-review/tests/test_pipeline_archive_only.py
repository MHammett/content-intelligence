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
            or (lambda url, **kwargs: {"archived": False, "snapshot_url": None}),
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

        def fake_check(url, **kwargs):
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

        def fake_check(url, **kwargs):
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
            wayback_check=lambda url, **kw: dict(self._STALE),
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
            draft, wayback_check=lambda url, **kw: dict(self._STALE), submit=recaptured
        )
        assert "Re-archive attempt" not in out
        assert "1 with a fresh Wayback snapshot" in out

    def test_a_fresh_snapshot_nobody_resubmitted_adds_no_line(self, tmp_path):
        draft = _write(
            tmp_path, "draft.md", "## Sources\n\n[1] Fine. https://a.example/x\n"
        )
        fresh = {**self._STALE, "snapshot_stale": False}
        *_, out = _run_archive_only(draft, wayback_check=lambda url, **kw: dict(fresh))
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


class TestArchiveOnlyHonoursTheHistoryRootOverride:
    """The archiving pass looks for earlier captures in the history it is given."""

    def test_submission_is_handed_the_override(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared"
        monkeypatch.setenv("CI_HISTORY_ROOT", str(shared))
        draft = _write(
            tmp_path,
            "draft.md",
            "A claim. [1]\n\n## Sources\n\n[1] A source. https://a.example/one\n",
        )

        _, mock_submit, _, _ = _run_archive_only(draft)

        mock_submit.assert_called_once()
        assert mock_submit.call_args.args[2] == str(shared)

    def test_unset_it_is_still_the_working_directory_s_history(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "A claim. [1]\n\n## Sources\n\n[1] A source. https://a.example/one\n",
        )

        _, mock_submit, _, _ = _run_archive_only(draft)

        assert mock_submit.call_args.args[2] == pipeline.HISTORY_ROOT


def _run_with_real_submit(draft_path, check, submit):
    """Run ``run_archive_only`` with the *real* ``_submit_missing_archives``.

    Only archive.org's own HTTP surface is stubbed: ``check`` and ``submit``
    (what the pipeline decides from, and decides to do), plus the two calls the
    real submission pass makes around them. With no credentials configured that
    is ``capture_capacity`` and, once a submission has failed, ``system_status``;
    ``_reconcile_prior_captures`` and ``_poll_capture_outcomes`` return before
    any request without a key pair. ``classify_host`` is stubbed so the example
    URLs need no DNS. Returns (mock_submit, printed_output).
    """
    import contextlib
    import io

    from ci_article_review.adapters.citation.resolver import HOST_PUBLIC

    with (
        patch("ci_article_review.pipeline.load_user_config", return_value={}),
        patch("ci_article_review.pipeline.load_publication_config", return_value={}),
        patch("ci_article_review.pipeline.merge_configs", return_value=_MINIMAL_CONFIG),
        patch("ci_article_review.adapters.citation.wayback.check", side_effect=check),
        patch(
            "ci_article_review.adapters.citation.wayback.submit", side_effect=submit
        ) as mock_submit,
        patch(
            "ci_article_review.adapters.citation.wayback.capture_capacity",
            return_value={"daily_exhausted": False, "available": None},
        ),
        patch(
            "ci_article_review.adapters.citation.wayback.system_status",
            return_value={"known": False},
        ),
        patch(
            "ci_article_review.adapters.citation.resolver.classify_host",
            return_value=HOST_PUBLIC,
        ),
    ):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pipeline.run_archive_only(draft_path, "myblog")
    return mock_submit, buf.getvalue()


# What spn-client's check() returns for a page whose newest capture is a revisit
# record, as measured 2026-09-28 for one NHTSA PDF: the availability API only knew
# the 2024 capture, and CDX supplied the 2026-09-27 revisit after verifying its
# digest against a 200 capture.
_REVISIT_PROMOTED = {
    "archived": True,
    "found_via": "cdx",
    "snapshot_is_revisit": True,
    "snapshot_url": "https://web.archive.org/web/20260927204213/https://a.example/doc.pdf",
    "snapshot_ts": "20260927204213",
    "snapshot_age_days": 6,
    "snapshot_stale": False,
    "snapshot_status": "200",
    "snapshot_is_error_capture": False,
}

# What spn-client's submit() returns for a URL at its per-day capture cap.
_CAP_REFUSAL = {
    "url": "https://a.example/doc.pdf",
    "submitted": False,
    "job_id": None,
    "archived": False,
    "error": (
        "This URL has been already captured 1 times today, which is a daily limit "
        "we have set for that Resource type. Please try again tomorrow."
    ),
    "error_summary": (
        "archive.org refused the request: This URL has been already captured 1 "
        "times today, which is a daily limit we have set for that Resource type. "
        "Please try again tomorrow."
    ),
    "error_code": "error:too-many-daily-captures",
    "retry_category": "transient",
}

_ONE_PDF = "## Sources\n\n[1] A PDF. https://a.example/doc.pdf\n"


class TestTheCdxSecondOpinion:
    """The reason ``--archive-only`` asks for ``cdx_fallback``: an unchanged page
    reads stale on every run through the availability API alone, so every run
    re-submits it. content-intelligence#289."""

    def test_every_lookup_asks_for_the_second_opinion(self, tmp_path):
        draft = _write(
            tmp_path,
            "draft.md",
            "## Sources\n\n[1] One. https://a.example/one\n\n"
            "[2] Two. https://a.example/two\n",
        )
        mock_check, *_ = _run_archive_only(draft)

        assert mock_check.call_count == 2
        for call in mock_check.call_args_list:
            assert call.kwargs == {"cdx_fallback": True}

    def test_a_page_whose_newest_capture_is_a_revisit_is_not_resubmitted(
        self, tmp_path
    ):
        draft = _write(tmp_path, "draft.md", _ONE_PDF)
        mock_submit, out = _run_with_real_submit(
            draft,
            check=lambda url, **kw: dict(_REVISIT_PROMOTED),
            submit=AssertionError("a fresh capture must not be re-requested"),
        )

        mock_submit.assert_not_called()
        assert "20260927204213" in out
        assert "a revisit record" in out
        assert "STALE" not in out
        assert "1 citation URL(s): 1 with a fresh Wayback snapshot" in out

    def test_a_stale_page_with_nothing_newer_is_still_submitted(self, tmp_path):
        """The fallback only ever promotes. A page CDX has nothing newer for keeps
        its stale answer, and the submission pass still acts on it."""
        stale = {
            "archived": True,
            "snapshot_url": "https://web.archive.org/web/20240812234508/https://a.example/doc.pdf",
            "snapshot_ts": "20240812234508",
            "snapshot_age_days": 777,
            "snapshot_stale": True,
        }
        draft = _write(tmp_path, "draft.md", _ONE_PDF)
        mock_submit, out = _run_with_real_submit(
            draft,
            check=lambda url, **kw: dict(stale),
            submit=lambda url, **kw: {
                "url": url,
                "submitted": True,
                "job_id": None,
                "archived": True,
                "snapshot_url": "https://web.archive.org/web/20261003120000/" + url,
                "snapshot_ts": "20261003120000",
                "snapshot_age_days": 0,
                "snapshot_stale": False,
            },
        )

        mock_submit.assert_called_once()
        assert "20261003120000" in out

    def test_a_failed_second_opinion_is_said_out_loud(self, tmp_path):
        """Otherwise a CDX lookup that timed out reads exactly like a confirmed
        absence."""
        draft = _write(tmp_path, "draft.md", _ONE_PDF)
        _, out = _run_with_real_submit(
            draft,
            check=lambda url, **kw: {
                "archived": False,
                "cdx_error": "archive.org did not answer within the timeout",
            },
            submit=lambda url, **kw: dict(_CAP_REFUSAL),
        )

        assert "Archive lookup note" in out
        assert "archive.org did not answer within the timeout" in out
        assert "does not prove the page is unarchived" in out


class TestADailyCapRefusalIsNotCalledNotArchived:
    """The consumer half of spn-client's refusal handling: the real submission
    pass records the refusal's code and category, and the report says what is
    actually known. The old wording said "It is NOT archived" for a URL archive.org
    had just said it already captured today."""

    def test_the_refusal_flows_from_submit_to_the_printed_line(self, tmp_path):
        draft = _write(tmp_path, "draft.md", _ONE_PDF)
        mock_submit, out = _run_with_real_submit(
            draft,
            check=lambda url, **kw: {"archived": False},
            submit=lambda url, **kw: dict(_CAP_REFUSAL),
        )

        mock_submit.assert_called_once()
        assert "NOT RE-SUBMITTED TODAY" in out
        assert "already captured 1 times today" in out
        assert "retry tomorrow" in out
        assert "It is NOT archived" not in out
        assert "SUBMISSION FAILED" not in out

    def test_a_refusal_with_no_code_keeps_the_original_wording(self, tmp_path):
        """A transport failure carries no archive.org code, so it has nothing to
        key on and must read as it always did."""
        draft = _write(tmp_path, "draft.md", _ONE_PDF)
        _, out = _run_with_real_submit(
            draft,
            check=lambda url, **kw: {"archived": False},
            submit=lambda url, **kw: {
                "url": url,
                "submitted": False,
                "job_id": None,
                "error": "connection refused",
                "error_summary": "could not reach archive.org when asked to capture",
            },
        )

        assert "SUBMISSION FAILED" in out
        assert "It is NOT archived. Archive it by hand, or re-run." in out
        assert "NOT RE-SUBMITTED TODAY" not in out


class TestInstalledSpnClientHasWhatArchiveOnlyCalls:
    """Every other test here stubs ``wayback.check``, so none of them can notice
    an installed spn-client that predates what ``run_archive_only`` calls: the
    CLI would raise ``TypeError`` on its first lookup while the suite stayed
    green. This is the one test that looks at the real signature, so it is what
    holds the dependency pin in ``pyproject.toml`` to a release that has them."""

    def test_check_accepts_cdx_fallback(self):
        import inspect

        from ci_article_review.adapters.citation import wayback

        assert "cdx_fallback" in inspect.signature(wayback.check).parameters, (
            "run_archive_only calls wayback.check(url, cdx_fallback=True); raise "
            "the spn-client pin in packages/ci-article-review/pyproject.toml to "
            "the release that added it"
        )

    def test_submit_reports_a_refusal_code_and_category(self):
        from ci_article_review.adapters.citation import wayback

        for key in ("error_code", "retry_category"):
            assert key in wayback.SubmitResult.__annotations__, (
                f"_record_submission reads {key!r} from wayback.submit(); raise "
                "the spn-client pin to the release that returns it"
            )
