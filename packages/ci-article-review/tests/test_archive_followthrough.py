"""Archiving every cited URL, and saying what happened to each.

Covers the follow-through added after run 2 of chinese-carrier-bans and
carrier-security-compliance (2026-11-10), where 30 and 28 cited URLs ended the
round without a snapshot:

- a refused connection is told apart from archive.org's policy, with the
  breaker's state (``_record_failure_kind``);
- a once-a-day refusal records the snapshot made earlier that day;
- a 404 from a host that served other captures in the pass is retried once;
- a prior job left running, handed in from outside history, is asked about;
- a full review archives the Sources list beyond the claim citations;
- ``--archive-only --github`` writes ``pipeline/archive.md`` and ``archive.json``.

Every archive.org call is stubbed at the ``wayback`` boundary.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ci_article_review.pipeline as pipeline
from ci_article_review import github_archive
from ci_article_review.adapters.citation import resolver, wayback
from ci_core.github_repo import CommitResult, FileAtRef, GitHubRepoError

W = "ci_article_review.adapters.citation.resolver.wayback"


@pytest.fixture(autouse=True)
def _history_in_tmp(tmp_history_root):
    """Nothing here may touch the working directory's pipeline_history/."""


def _entry(url, **wb):
    return {"resolved": True, "url": url, "wayback": {"archived": False, **wb}}


def _run_pass(entries, *, submit, job_status=None, check=None, poll_budget=0.5):
    """Run ``_submit_one_per_url`` with archive.org stubbed out."""
    with (
        patch(f"{W}.capture_capacity", return_value={"available": 2}),
        patch(f"{W}.submit", side_effect=submit) as sub,
        patch(
            f"{W}.check_job_status",
            side_effect=job_status or (lambda job, **k: {"state": "pending"}),
        ),
        patch(f"{W}.system_status", return_value={"ok": True}),
        patch(f"{W}.service_health_note", return_value=None),
        patch(
            f"{W}.check", side_effect=check or (lambda url, **k: {"archived": False})
        ),
        patch.object(resolver, "_CAPTURE_POLL_BUDGET_SECONDS", poll_budget),
    ):
        resolver._submit_one_per_url(entries, None, "ak", "sk")
    return sub


class TestFailureKind:
    def _record(self, sub, breaker=False):
        entry = _entry("https://example.com/a")
        with patch(f"{W}.rate_limited_out", return_value=breaker):
            resolver._record_submission(entry, sub)
        return entry["wayback"]

    def test_a_refused_connection_is_not_archive_orgs_policy(self):
        wb = self._record(
            {
                "submitted": False,
                "error": "[WinError 10061] ...",
                "error_summary": "could not reach archive.org when asked to capture "
                "— the connection was refused, dropped, or the host did not resolve",
            }
        )
        assert wb["archive_outcome"] == wayback.ARCHIVE_SUBMIT_FAILED
        assert wb["submission_failure_kind"] == resolver.FAILURE_CONNECTION
        assert wb["breaker_open_at_failure"] is False
        assert "not a refusal by archive.org" in wb["archive_outcome_detail"]
        assert "was not open" in wb["archive_outcome_detail"]

    def test_the_breaker_state_is_recorded_when_it_was_open(self):
        wb = self._record(
            {
                "submitted": False,
                "error": "x",
                "error_summary": "could not reach archive.org when asked to capture",
            },
            breaker=True,
        )
        assert wb["breaker_open_at_failure"] is True
        assert "breaker was open" in wb["archive_outcome_detail"]

    @pytest.mark.parametrize(
        "sub, kind",
        [
            (
                {"error_code": "error:too-many-daily-captures", "error": "x"},
                resolver.FAILURE_POLICY,
            ),
            ({"rate_limited": True, "error": "x"}, resolver.FAILURE_RATE_LIMITED),
            (
                {"error": "x", "error_summary": "archive.org answered HTTP 502 when"},
                resolver.FAILURE_HTTP,
            ),
            ({"error": "x"}, resolver.FAILURE_OTHER),
        ],
    )
    def test_each_other_kind(self, sub, kind):
        wb = self._record({"submitted": False, **sub})
        assert wb["submission_failure_kind"] == kind
        assert "not a refusal by archive.org" not in (
            wb.get("archive_outcome_detail") or ""
        )

    def test_a_success_carries_no_failure_kind(self):
        wb = self._record({"submitted": True, "job_id": "spn2-1"})
        assert "submission_failure_kind" not in wb


class TestDailyLimitAdoptsTodaysSnapshot:
    URL = "https://docs.fcc.gov/public/attachments/DOC-426316A1.pdf"
    REFUSED = {
        "submitted": False,
        "error": "already captured",
        "error_summary": "archive.org refused the request: This URL has been "
        "already captured 1 times today",
        "error_code": "error:too-many-daily-captures",
        "retry_category": "transient",
    }

    def test_todays_snapshot_is_recorded_as_archived(self):
        entry = _entry(self.URL)
        snap = f"https://web.archive.org/web/20261110120000/{self.URL}"
        _run_pass(
            [entry],
            submit=lambda url, **k: dict(self.REFUSED, url=url),
            check=lambda url, **k: {
                "archived": True,
                "snapshot_url": snap,
                "snapshot_ts": "20261110120000",
                "snapshot_age_days": 0,
                "snapshot_stale": False,
            },
        )
        wb = entry["wayback"]
        assert wb["archive_outcome"] == wayback.ARCHIVE_ARCHIVED
        assert wb["snapshot_url"] == snap
        assert wb["adopted_same_day_snapshot"] is True

    def test_an_old_snapshot_leaves_the_refusal_standing(self):
        entry = _entry(self.URL)
        _run_pass(
            [entry],
            submit=lambda url, **k: dict(self.REFUSED, url=url),
            check=lambda url, **k: {
                "archived": True,
                "snapshot_url": f"https://web.archive.org/web/2020/{url}",
                "snapshot_age_days": 900,
            },
        )
        assert entry["wayback"]["archive_outcome"] == wayback.ARCHIVE_SUBMIT_FAILED

    def test_the_lookup_uses_cdx_and_skips_the_runs_memo(self):
        entry = _entry(self.URL)
        seen = {}

        def check(url, **kw):
            seen.update(kw)
            return {"archived": False}

        with patch(f"{W}.forget_lookup") as forget:
            _run_pass(
                [entry],
                submit=lambda url, **k: dict(self.REFUSED, url=url),
                check=check,
            )
        forget.assert_any_call(self.URL)
        assert seen.get("cdx_fallback") is True


class TestNotFoundRetriedOnAServingHost:
    def _jobs(self, failing):
        """Every job succeeds except the ids in ``failing``, which 404 once."""
        calls = {}

        def status(job, **k):
            calls[job] = calls.get(job, 0) + 1
            if job in failing and calls[job] == 1:
                return {
                    "state": "failed",
                    "reason": "The target server cannot find it. (HTTP status=404)",
                    "error_code": "error:not-found",
                    "retry_category": "permanent",
                }
            return {
                "state": "success",
                "snapshot_url": f"https://web.archive.org/web/20261110/{job}",
                "snapshot_ts": "20261110000000",
            }

        return status

    def test_a_404_beside_a_successful_capture_on_the_host_is_retried_once(self):
        ok = _entry("https://www.ecfr.gov/current/title-47/section-1.1")
        gone = _entry("https://www.ecfr.gov/current/title-47/section-1.80")
        ids = iter(["job-ok", "job-404", "job-404-again"])
        sub = _run_pass(
            [ok, gone],
            submit=lambda url, **k: {"submitted": True, "job_id": next(ids)},
            job_status=self._jobs({"job-404"}),
            poll_budget=5,
        )
        assert sub.call_count == 3
        assert gone["wayback"]["capture_retried_after_not_found"] is True
        assert gone["wayback"]["archive_outcome"] == wayback.ARCHIVE_ARCHIVED

    def test_a_404_on_a_host_with_no_other_capture_is_not_second_guessed(self):
        gone = _entry("https://only.example.org/gone")
        sub = _run_pass(
            [gone],
            submit=lambda url, **k: {"submitted": True, "job_id": "job-404"},
            job_status=self._jobs({"job-404"}),
            poll_budget=5,
        )
        assert sub.call_count == 1
        assert gone["wayback"]["archive_outcome"] == wayback.ARCHIVE_CAPTURE_FAILED


class TestReconcile:
    def test_a_job_handed_in_from_outside_history_is_asked_about(self):
        entry = _entry("https://example.com/p")
        with patch(
            f"{W}.check_job_status",
            return_value={
                "state": "success",
                "snapshot_url": "https://web.archive.org/web/1/https://example.com/p",
            },
        ) as status:
            left = resolver._reconcile_prior_captures(
                [entry], None, "ak", "sk", {entry["url"]: {"job_id": "spn2-x"}}
            )
        status.assert_called_once()
        assert left == []
        assert entry["wayback"]["archive_outcome"] == wayback.ARCHIVE_ARCHIVED

    def test_a_prior_404_is_submitted_again_not_dropped(self):
        entry = _entry("https://www.ecfr.gov/current/title-47/section-64.2010")
        with patch(
            f"{W}.check_job_status",
            return_value={
                "state": "failed",
                "error_code": "error:not-found",
                "retry_category": "permanent",
            },
        ):
            left = resolver._reconcile_prior_captures(
                [entry], None, "ak", "sk", {entry["url"]: {"job_id": "spn2-y"}}
            )
        assert left == [entry]

    def test_another_permanent_failure_is_still_dropped(self):
        entry = _entry("https://example.com/bad")
        with patch(
            f"{W}.check_job_status",
            return_value={
                "state": "failed",
                "error_code": "error:invalid-url-syntax",
                "retry_category": "permanent",
            },
        ):
            left = resolver._reconcile_prior_captures(
                [entry], None, "ak", "sk", {entry["url"]: {"job_id": "spn2-z"}}
            )
        assert left == []


DRAFT = """Body [1] and [2].

## Sources

1. A claim source. https://example.com/claim
2. A data query. https://stat.ripe.net/data/x.json?resource=AS1
3. Added after the review. https://example.com/new
"""


class TestArchiveDraftSources:
    def test_handled_urls_keep_pass_3s_status_and_the_rest_are_submitted(self):
        submitted = []

        def fake_submit(results, *a, **k):
            submitted.extend(r["url"] for r in results)
            for r in results:
                r["wayback"]["archive_outcome"] = wayback.ARCHIVE_PENDING

        with (
            patch.object(
                pipeline.wayback, "check", return_value={"archived": False}
            ) as chk,
            patch(
                "ci_article_review.adapters.citation.resolver._submit_missing_archives",
                fake_submit,
            ),
        ):
            results = pipeline.archive_draft_sources(
                DRAFT,
                {},
                handled={
                    "https://example.com/claim": {"archived": True, "snapshot_url": "s"}
                },
                known={
                    "https://stat.ripe.net/data/x.json?resource=AS1": {
                        "archived": False
                    }
                },
                cdx_fallback=False,
            )
        assert [r["url"] for r in results] == [
            "https://example.com/claim",
            "https://stat.ripe.net/data/x.json?resource=AS1",
            "https://example.com/new",
        ]
        assert results[0]["source"] == "claim"
        assert results[0]["wayback"]["snapshot_url"] == "s"
        assert submitted == [
            "https://stat.ripe.net/data/x.json?resource=AS1",
            "https://example.com/new",
        ]
        # The link pass's answer is reused; only the unseen URL is looked up.
        chk.assert_called_once_with("https://example.com/new", cdx_fallback=False)

    def test_no_citation_block_is_none(self):
        assert pipeline.archive_draft_sources("No sources here.", {}) is None

    def test_the_full_review_pass_never_raises(self):
        with patch.object(pipeline, "archive_draft_sources", side_effect=RuntimeError):
            assert (
                pipeline._archive_remaining_sources(DRAFT, [], None, {}, None) is None
            )


SOURCE = {
    "kind": "github",
    "repo": "owner/docs",
    "ref": "round/an-article/r1",
    "commit_sha": "a" * 40,
    "round": 1,
    "pull_request": 24,
    "files": [
        {"path": "articles/an-article/draft.md", "blob_sha": "d" * 40},
        {"path": "articles/an-article/metadata.md", "blob_sha": "m" * 40},
    ],
}

RESULTS = [
    {
        "marker": "1",
        "url": "https://example.com/claim",
        "wayback": {
            "archived": True,
            "snapshot_url": "https://web.archive.org/web/1/https://example.com/claim",
            "snapshot_age_days": 0,
        },
    },
    {
        "marker": "2",
        "url": "https://example.com/a|b",
        "wayback": {
            "archived": False,
            "archive_outcome": wayback.ARCHIVE_SUBMIT_FAILED,
            "archive_outcome_detail": "could not reach archive.org",
            "submission_failure_kind": "connection",
            "breaker_open_at_failure": False,
        },
    },
    {
        "marker": "3",
        "url": "https://example.com/p",
        "wayback": {
            "archived": False,
            "archive_outcome": wayback.ARCHIVE_PENDING,
            "submission_job_id": "spn2-p",
        },
    },
]


class FakeClient:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.commits = []
        self.closed = False

    def info(self):
        return SimpleNamespace(private=True, can_push=True)

    def read(self, path, ref):
        if path not in self.existing:
            raise GitHubRepoError("not_found", "missing")
        return FileAtRef(self.existing[path], "blob-" + path[-4:], "c" * 40)

    def commit_files(self, branch, files, message, **kw):
        self.commits.append((branch, files, message, kw))
        return CommitResult("e" * 40, "https://github.com/x/commit/e", tuple(files), ())

    def close(self):
        self.closed = True


class TestGithubArchive:
    def test_the_files_name_their_commit_and_every_gap(self):
        handoff = {"document_source": SOURCE}
        rows = github_archive.status_rows(RESULTS)
        md = github_archive.render_markdown(
            handoff, rows, generated="2026-11-10T22:00:00+00:00"
        )
        assert "aaaaaaaaaaaa" in md
        assert "Fresh snapshot 1" in md and "no snapshot 1" in md
        assert "capture still running 1" in md
        assert "example.com/a\\|b" in md  # a pipe in a URL does not break the table
        assert "could not reach archive.org" in md
        doc = json.loads(github_archive.render_json(handoff, rows, generated="g"))
        assert doc["draft_blob_sha"] == "d" * 40
        assert [r["status"] for r in doc["urls"]] == ["archived", "missing", "pending"]
        assert doc["urls"][1]["failure_kind"] == "connection"

    def test_write_commits_both_files_to_the_round_branch_replacing_its_own(self):
        md = "articles/an-article/pipeline/archive.md"
        client = FakeClient(existing={md: b"old"})
        github_archive.write_archive_status(
            {"document_source": SOURCE},
            RESULTS,
            settings={"repo": "owner/docs"},
            api_keys={"openai": {"api_key": "sk-secret"}},
            client=client,
            out=lambda m: None,
        )
        branch, files, message, kw = client.commits[0]
        assert branch == "round/an-article/r1"
        assert set(files) == {md, "articles/an-article/pipeline/archive.json"}
        assert kw["replace"] == {md: "blob-e.md"}
        assert "sk-secret" in kw["refuse_if_contains"]
        assert message.startswith("archive: an-article r1 at aaaaaaaaaaaa")

    def test_a_read_that_is_not_an_open_round_is_refused(self):
        source = dict(SOURCE, pull_request=None)
        with pytest.raises(github_archive.GitHubDocumentsError):
            github_archive.write_archive_status(
                {"document_source": source},
                RESULTS,
                settings={},
                api_keys={},
                client=FakeClient(),
            )

    def test_pending_jobs_come_back_from_the_last_status_file(self):
        rows = github_archive.status_rows(RESULTS)
        data = github_archive.render_json(
            {"document_source": SOURCE}, rows, generated="g"
        ).encode("utf-8")
        client = FakeClient(
            existing={"articles/an-article/pipeline/archive.json": data}
        )
        prior = github_archive.prior_pending_jobs(
            {"document_source": SOURCE}, {}, client=client
        )
        assert prior == {
            "https://example.com/p": {
                "job_id": "spn2-p",
                "run_number": None,
                "generated": "g",
            }
        }

    def test_no_status_file_is_no_prior_jobs(self):
        assert (
            github_archive.prior_pending_jobs(
                {"document_source": SOURCE}, {}, client=FakeClient()
            )
            == {}
        )


class TestArchiveOnlyFromAHandoff:
    def test_it_archives_the_round_and_writes_it_back(self, capsys):
        handoff = {"draft": DRAFT, "document_source": SOURCE}
        with (
            patch.object(pipeline, "load_user_config", return_value={}),
            patch.object(pipeline, "load_publication_config", return_value={}),
            patch.object(
                pipeline,
                "merge_configs",
                return_value={
                    "api_keys": {},
                    "pipeline": {"wayback_lookup_cache": False},
                },
            ),
            patch.object(pipeline.wayback, "check", return_value={"archived": False}),
            patch(
                "ci_article_review.adapters.citation.resolver._submit_missing_archives"
            ) as sub,
            patch.object(
                github_archive,
                "prior_pending_jobs",
                return_value={"u": {"job_id": "j"}},
            ),
            patch.object(github_archive, "write_archive_status") as write,
        ):
            results = pipeline.run_archive_only(
                None, "myblog", handoff=handoff, write_back=True
            )
        assert len(results) == 3
        assert sub.call_args.kwargs["prior_pending"] == {"u": {"job_id": "j"}}
        write.assert_called_once()
        assert write.call_args.args[0] is handoff
        assert "3 citation URL(s)" in capsys.readouterr().out


class TestWriteBackCarriesTheArchiveStatus:
    def test_a_report_with_a_sources_archive_writes_both_files(self, tmp_path):
        from ci_article_review import github_writeback

        name = "run_2_20261110_170032"
        report = {
            "document_source": SOURCE,
            "generated": "2026-11-10T17:00:32",
            "source_archive": RESULTS,
        }
        path = tmp_path / f"{name}_report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        (tmp_path / f"{name}_review.md").write_text("review", encoding="utf-8")
        plan = github_writeback.plan_files(path, report, include_capture=False)
        assert "pipeline/archive.md" in plan.names
        assert "pipeline/archive.json" in plan.names

    def test_a_report_without_one_writes_neither(self, tmp_path):
        from ci_article_review import github_writeback

        name = "run_2_20261110_170032"
        report = {"document_source": SOURCE}
        path = tmp_path / f"{name}_report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        (tmp_path / f"{name}_review.md").write_text("review", encoding="utf-8")
        plan = github_writeback.plan_files(path, report, include_capture=False)
        assert "pipeline/archive.md" not in plan.names


class TestOffSiteSnapshots:
    """A capture that landed on another site is not an archive of the cited page."""

    BOT_WALL = "https://web.archive.org/web/20261110140334/https://unblock.federalregister.gov/"

    def _entry(self, url, snap):
        return {"url": url, "wayback": {"archived": True, "snapshot_url": snap}}

    def test_an_ecfr_capture_of_the_bot_check_is_not_archived(self):
        entry = self._entry(
            "https://www.ecfr.gov/current/title-47/section-1.50007", self.BOT_WALL
        )
        assert resolver.demote_off_site_snapshots([entry]) == [entry]
        wb = entry["wayback"]
        assert wb["archived"] is False
        assert "snapshot_url" not in wb
        assert wb["off_site_snapshot_url"] == self.BOT_WALL
        assert wb["archive_outcome"] == wayback.ARCHIVE_CAPTURE_FAILED
        assert "federalregister.gov" in wb["archive_outcome_detail"]

    @pytest.mark.parametrize(
        "url, snap",
        [
            (
                "https://docs.fcc.gov/public/attachments/DA-26-563A1.pdf",
                "http://web.archive.org/web/20261110174938/https://docs.fcc.gov/public/attachments/DA-26-563A1.pdf",
            ),
            (
                "https://www.rfc-editor.org/rfc/rfc6811",
                "https://web.archive.org/web/20261110173905/https://www.rfc-editor.org/info/rfc6811/",
            ),
            (
                "https://docs.fcc.gov/x.pdf",
                "https://web.archive.org/web/20261110000000id_/https://www.fcc.gov/x.pdf",
            ),
            (
                "https://web.archive.org/web/20190718193106/https://blogs.oracle.com/x",
                "https://web.archive.org/web/20190718193106/https://blogs.oracle.com/x",
            ),
        ],
    )
    def test_a_capture_on_the_same_site_is_kept(self, url, snap):
        entry = self._entry(url, snap)
        assert resolver.demote_off_site_snapshots([entry]) == []
        assert entry["wayback"]["snapshot_url"] == snap

    def test_a_bot_wall_capture_does_not_make_the_host_look_serving(self):
        walled = _entry("https://www.ecfr.gov/current/title-47/section-1.1")
        gone = _entry("https://www.ecfr.gov/current/title-47/section-1.80")
        ids = iter(["job-wall", "job-404"])

        def status(job, **k):
            if job == "job-404":
                return {
                    "state": "failed",
                    "error_code": "error:not-found",
                    "retry_category": "permanent",
                }
            return {"state": "success", "snapshot_url": self.BOT_WALL}

        sub = _run_pass(
            [walled, gone],
            submit=lambda url, **k: {"submitted": True, "job_id": next(ids)},
            job_status=status,
            poll_budget=5,
        )
        assert sub.call_count == 2
        assert walled["wayback"]["capture_redirected_off_site"] is True
        assert "capture_retried_after_not_found" not in gone["wayback"]


class TestACitedSnapshotIsArchived:
    URL = "https://web.archive.org/web/20190718193106/https://blogs.oracle.com/x"

    def test_it_is_archived_fresh_and_never_submitted(self):
        submitted = []

        def fake_submit(results, *a, **k):
            submitted.extend(r["url"] for r in results if r["url"] == self.URL)

        draft = f"Body [1].\n\n## Sources\n\n1. Oracle, via the Wayback Machine. {self.URL}\n"
        stale = {"archived": True, "is_archive_url": True, "snapshot_stale": True}
        with (
            patch.object(pipeline.wayback, "check", return_value=stale),
            patch(
                "ci_article_review.adapters.citation.resolver._submit_missing_archives",
                fake_submit,
            ),
        ):
            results = pipeline.archive_draft_sources(draft, {})
        wb = results[0]["wayback"]
        assert wb["snapshot_stale"] is False and wb["snapshot_url"] == self.URL
        assert pipeline.archive_status_counts(results)["fresh"] == 1
        rows = github_archive.status_rows(results)
        assert rows[0]["status"] == github_archive.STATUS_ARCHIVED

    def test_the_submission_pass_skips_it(self):
        entry = _entry(self.URL, archived=True, snapshot_stale=True)
        with patch.object(resolver, "_submit_one_per_url") as sub:
            resolver._submit_missing_archives([entry], {})
        sub.assert_not_called()
