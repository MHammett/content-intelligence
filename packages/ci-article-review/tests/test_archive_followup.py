"""A review hands its archive.org work on; a slow pass does it later (issue #497).

On 2026-11-15 archive.org refused this machine mid-review twice: after 176
requests, then after only 76. The review now makes no capture requests and no
snapshot comparisons. It records them, and ``archive_followup`` does them in a
process of its own, one at a time, after archive.org's breaker has closed.

Every archive.org call is stubbed at the ``wayback`` boundary, and no test
starts a process: conftest replaces ``archive_followup.spawn``.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
import spn_client.client as engine
from filelock import FileLock

from ci_article_review import archive_followup, pipeline
from ci_article_review.adapters.citation import archive_deferral, resolver, wayback
from ci_core.http import HOST_NON_PUBLIC, HOST_PUBLIC

# The real one, taken before conftest's autouse fixture replaces it.
REAL_SPAWN = archive_followup.spawn

W = "ci_article_review.adapters.citation.resolver.wayback"
URL = "https://www.ntsb.gov/SIR9601.pdf"
SNAP = f"https://web.archive.org/web/20261115181042/{URL}"
TEXT = "y" * 400


def _cit(url=URL, **wb):
    return {
        "resolved": True,
        "url": url,
        "checksum": resolver.sha256_checksum(TEXT),
        "checksum_basis": "extracted_text",
        "wayback": {"archived": False, **wb},
    }


@pytest.fixture
def restore_pacing(monkeypatch):
    """``_slow_down`` writes module globals; put every one back afterwards."""
    for mod, names in (
        (
            engine,
            (
                "_MIN_INTERVAL_SECONDS",
                "_AUTHENTICATED_SUBMIT_INTERVAL_SECONDS",
                "_ANONYMOUS_SUBMIT_INTERVAL_SECONDS",
            ),
        ),
        (
            resolver,
            (
                "_MAX_SUBMIT_PARALLEL",
                "_MAX_MATCH_PARALLEL",
                "_MATCH_READ_INTERVAL_SECONDS",
                "_MATCH_TIMEOUT_SECONDS",
                "_SUBMIT_TIMEOUT_SECONDS",
                "_CAPTURE_POLL_BUDGET_SECONDS",
            ),
        ),
    ):
        for name in names:
            monkeypatch.setattr(mod, name, getattr(mod, name))


class TestTheReviewDefers:
    def test_no_capture_is_requested_and_the_report_says_why(self):
        deferral = archive_deferral.begin()
        entry = _cit()
        with (
            patch(f"{W}.submit") as submit,
            patch(f"{W}.capture_capacity") as capacity,
            patch(f"{W}.check_job_status") as status,
            patch.object(resolver, "classify_host", return_value=HOST_PUBLIC),
        ):
            resolver._submit_missing_archives(
                [entry], {"access_key": "a", "secret_key": "s"}
            )
        assert not submit.called and not capacity.called and not status.called
        wb = entry["wayback"]
        assert wb["archive_outcome"] == wayback.ARCHIVE_NOT_ATTEMPTED
        assert wb["archive_deferred"] is True
        assert "after it" in wb["archive_outcome_detail"]
        [item] = deferral.items()
        assert item["capture"] is True
        # The follow-up submits on the lookup's evidence, not on "deferred".
        assert item["wayback"] == {"archived": False}
        assert item["checksum"] == entry["checksum"]

    def test_a_non_public_host_is_still_refused_not_queued(self):
        deferral = archive_deferral.begin()
        entry = _cit("http://10.0.0.5/x")
        with patch.object(resolver, "classify_host", return_value=HOST_NON_PUBLIC):
            resolver._submit_missing_archives([entry])
        assert len(deferral) == 0
        assert "not public" in entry["wayback"]["archive_outcome_detail"]

    def test_two_citations_of_one_page_are_one_item(self):
        deferral = archive_deferral.begin()
        with patch.object(resolver, "classify_host", return_value=HOST_PUBLIC):
            resolver._submit_missing_archives([_cit(), _cit()])
        assert len(deferral) == 1

    def test_a_comparison_is_deferred_unless_already_remembered(self):
        deferral = archive_deferral.begin()
        known = _cit(
            "https://example.org/known",
            snapshot_url=SNAP.replace(URL, "https://example.org/known"),
        )
        new = _cit(snapshot_url=SNAP)
        wayback.remember_archive_match(
            known["wayback"]["snapshot_url"], known["checksum"], "identical", "d"
        )
        with patch.object(resolver, "safe_get") as get:
            resolver._verify_archive_matches([known, new])
        assert not get.called
        assert known["archive_match"] == "identical"
        assert new["archive_match"] == resolver.ARCHIVE_MATCH_UNCHECKED
        assert new["archive_match_detail"] == archive_deferral.DEFERRED_MATCH_DETAIL
        assert [i["url"] for i in deferral.items()] == [URL]

    def test_without_a_deferral_the_work_is_done_inline(self):
        archive_deferral.end()
        with (
            patch(f"{W}.capture_capacity", return_value={"available": 2}),
            patch(
                f"{W}.submit", return_value={"submitted": True, "job_id": "j"}
            ) as submit,
            patch(f"{W}.check_job_status", return_value={"state": "pending"}),
            patch(f"{W}.check", return_value={"archived": False}),
            patch.object(resolver, "classify_host", return_value=HOST_PUBLIC),
            patch.object(resolver, "_CAPTURE_POLL_BUDGET_SECONDS", 0.1),
        ):
            resolver._submit_missing_archives(
                [_cit()], {"access_key": "a", "secret_key": "s"}
            )
        assert submit.called


class TestThePipelineHandsItOn:
    def test_the_work_file_is_written_and_the_pass_started(
        self, tmp_path, no_background_archiving, monkeypatch
    ):
        monkeypatch.setattr(pipeline, "_history_root", lambda: str(tmp_path))
        report = tmp_path / "slug" / "run_2_20261115_210328_report.json"
        report.parent.mkdir()
        items = [
            {
                "url": URL,
                "capture": True,
                "compare": False,
                "wayback": {"archived": False},
            }
        ]
        pipeline._start_archive_followup(report, items, "configs", "mikehammett")
        [work] = no_background_archiving
        assert work.name == "run_2_20261115_210328_archive_work.json"
        data = json.loads(work.read_text(encoding="utf-8"))
        assert data["items"] == items
        assert data["publication"] == "mikehammett"
        assert data["history_root"] == str(tmp_path.resolve())


def _work(tmp_path, items):
    report = tmp_path / "slug" / "run_1_20261115_175842_report.json"
    report.parent.mkdir(exist_ok=True)
    return archive_followup.write_work(
        report, items, history_root=tmp_path, config_dir=tmp_path, publication="p"
    )


@pytest.fixture
def no_config(monkeypatch):
    monkeypatch.setattr(
        archive_followup,
        "_load_creds",
        lambda work: (
            {"access_key": "a", "secret_key": "s"},
            {"wayback_shared_rate_limit": False},
        ),
    )


@pytest.mark.usefixtures("restore_pacing", "no_config")
class TestTheFollowUpPass:
    def _item(self):
        item = _cit()
        return {
            **{k: item[k] for k in ("url", "checksum", "checksum_basis")},
            "capture": True,
            "compare": False,
            "wayback": {"archived": False},
        }

    def test_it_captures_compares_and_writes_what_it_did(self, tmp_path):
        work = _work(tmp_path, [self._item()])
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        with (
            patch(f"{W}.capture_capacity", return_value={"available": 2}),
            patch(
                f"{W}.submit",
                return_value={
                    "submitted": True,
                    "archived": True,
                    "job_id": None,
                    "snapshot_url": SNAP,
                    "snapshot_ts": "20261115181042",
                    "snapshot_age_days": 0,
                },
            ) as submit,
            patch(f"{W}.check_job_status", return_value={"state": "pending"}),
            patch(f"{W}.check", return_value={"archived": False}),
            patch.object(resolver, "classify_host", return_value=HOST_PUBLIC),
            patch.object(resolver, "safe_get", return_value=resp),
            patch.object(resolver, "_extract_fetched", return_value=(TEXT, "html")),
        ):
            done = archive_followup.run(work)
        assert submit.call_args.args[0] == URL
        data = json.loads(done.read_text(encoding="utf-8"))
        [row] = data["items"]
        assert row["wayback"]["archive_outcome"] == wayback.ARCHIVE_ARCHIVED
        assert row["archive_match"] == resolver.ARCHIVE_MATCH_IDENTICAL
        # The next review reads both from the shared store, asking nobody.
        wayback.open_lookup_cache(tmp_path)
        assert (
            wayback.cached_archive_match(SNAP, row["checksum"])["verdict"]
            == "identical"
        )

    def test_it_goes_slowly(self, tmp_path):
        archive_followup._slow_down()
        assert engine._MIN_INTERVAL_SECONDS >= archive_followup.REQUEST_INTERVAL_SECONDS
        assert (
            engine._AUTHENTICATED_SUBMIT_INTERVAL_SECONDS
            >= archive_followup.REQUEST_INTERVAL_SECONDS
        )
        assert resolver._MAX_SUBMIT_PARALLEL == 1
        assert (
            resolver._CAPTURE_POLL_BUDGET_SECONDS
            == archive_followup.CAPTURE_POLL_SECONDS
        )

    def test_a_second_pass_waits_for_the_first(self, tmp_path, monkeypatch):
        work = _work(tmp_path, [self._item()])
        monkeypatch.setattr(archive_followup, "MAX_LOCK_WAIT_SECONDS", 0.1)
        with FileLock(str(tmp_path / archive_followup.LOCK_FILENAME)):
            with patch(f"{W}.submit") as submit:
                assert archive_followup.run(work) is None
        assert not submit.called

    def test_it_waits_out_an_open_breaker(self, tmp_path, monkeypatch):
        states = iter([True, True, False])
        monkeypatch.setattr(wayback, "rate_limited_out", lambda: next(states))
        monkeypatch.setattr(wayback, "reopen_breaker_if_cooled", lambda: None)
        slept = []
        assert archive_followup._wait_for_breaker(3600, sleep=slept.append)
        assert len(slept) == 2

    def test_it_gives_up_on_a_breaker_that_never_closes(self, tmp_path, monkeypatch):
        work = _work(tmp_path, [self._item()])
        monkeypatch.setattr(wayback, "rate_limited_out", lambda: True)
        monkeypatch.setattr(archive_followup, "MAX_BREAKER_WAIT_SECONDS", 120)
        with patch(f"{W}.submit") as submit:
            assert archive_followup.run(work, sleep=lambda s: None) is None
        assert not submit.called
        assert work.exists()  # left for a later attempt

    def test_a_job_an_earlier_pass_left_running_is_asked_about(self, tmp_path):
        older = tmp_path / "slug" / "run_0_20261114_000000_archive_done.json"
        older.parent.mkdir()
        older.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "url": URL,
                            "wayback": {
                                "archive_outcome": wayback.ARCHIVE_PENDING,
                                "submission_job_id": "spn2-old",
                            },
                        },
                        {
                            "url": "https://settled.example/",
                            "wayback": {
                                "archive_outcome": wayback.ARCHIVE_PENDING,
                                "submission_job_id": "spn2-x",
                            },
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        newer = tmp_path / "slug" / "run_1_20261115_000000_archive_done.json"
        newer.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "url": "https://settled.example/",
                            "wayback": {"archive_outcome": wayback.ARCHIVE_ARCHIVED},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pending = archive_followup._prior_pending(tmp_path, exclude=None)
        assert pending == {URL: {"job_id": "spn2-old"}}


class TestSpawn:
    def test_it_starts_a_detached_process_logging_beside_the_work(self, tmp_path):
        work = tmp_path / "run_1_x_archive_work.json"
        work.write_text("{}", encoding="utf-8")
        with patch.object(subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            assert REAL_SPAWN(work) == 4242
        args, kwargs = popen.call_args
        assert args[0][-2:] == ["ci_article_review.archive_followup", str(work)]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert kwargs.get("creationflags") or kwargs.get("start_new_session")
        assert (tmp_path / "run_1_x_archive.log").exists()

    def test_a_failed_start_is_logged_with_the_command(self, tmp_path, caplog):
        work = tmp_path / "run_1_x_archive_work.json"
        with patch.object(subprocess, "Popen", side_effect=OSError("nope")):
            assert REAL_SPAWN(work) is None
        assert "Run it by hand" in caplog.text


class TestARealRunHandsItOn:
    """``run_draft_pipeline`` opens the deferral, says so in the report, and
    starts the pass on what was handed on, after the report is saved."""

    @staticmethod
    def _resolve_and_defer(claims, *a, **kw):
        out = []
        for c in claims:
            entry = _cit()
            entry["claim"] = c["claim"] if isinstance(c, dict) else c
            # What the real _submit_missing_archives does with a deferral open.
            archive_deferral.active().defer_capture(entry)
            out.append(entry)
        return out

    def test_the_report_counts_it_and_the_pass_is_started(
        self, tmp_path, no_background_archiving
    ):
        from .test_pipeline_end_to_end import _stubbed_run

        stub = patch(
            "ci_article_review.adapters.citation.resolver.resolve_citations",
            side_effect=self._resolve_and_defer,
        )
        with _stubbed_run(tmp_path, extra_patches=[stub]) as report:
            pass
        assert report["archive_followup"] == {
            "urls": 1,
            "captures": 1,
            "comparisons": 0,
            "lookups": 0,
        }
        [work] = no_background_archiving
        assert work.name.endswith(archive_followup.WORK_SUFFIX)
        assert json.loads(work.read_text(encoding="utf-8"))["items"][0]["url"] == URL
        assert archive_deferral.active() is None

    def test_offline_opens_no_deferral(self, tmp_path, no_background_archiving):
        from .test_pipeline_end_to_end import _stubbed_run

        seen = []

        def resolve(claims, *a, **kw):
            seen.append(archive_deferral.active())
            return []

        stub = patch(
            "ci_article_review.adapters.citation.resolver.resolve_citations",
            side_effect=resolve,
        )
        with _stubbed_run(tmp_path, extra_patches=[stub], offline=True) as report:
            pass
        assert "archive_followup" not in report
        assert no_background_archiving == []
        assert all(d is None for d in seen)
