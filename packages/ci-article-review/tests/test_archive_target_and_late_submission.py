"""Which URL the archiving pass archives, and what a late submission records.

Both from the wide review of federal-network-outages, 2026-11-15:

- A gemini citation arrived as a ``grounding-api-redirect`` wrapper that
  resolution followed to en.cibercuba.com. The pass submitted the wrapper;
  archive.org captured cibercuba; ``demote_off_site_snapshots`` then reported
  the publisher's own page as an off-site bot wall, "capture failed".
- Five submissions were logged "abandoned" at the batch's 600s deadline, then
  answered moments later with job ids. The daemon thread wrote those over the
  abandoned record, so the report said "queued" where the log said "abandoned".

Every archive.org call is stubbed at the ``wayback`` boundary.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from ci_article_review.adapters.citation import draft_citations, resolver, wayback
from ci_core.http import HOST_PUBLIC, HOST_UNRESOLVABLE

W = "ci_article_review.adapters.citation.resolver.wayback"

WRAPPER = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEVXcq"
PUBLISHER = "https://en.cibercuba.com/noticias/2026-11-15-paralizo-vuelos"


@pytest.fixture(autouse=True)
def _history_in_tmp(tmp_history_root):
    """Nothing here may touch the working directory's pipeline_history/."""


def _entry(url, final_url=None):
    e = {"resolved": True, "url": url, "wayback": {"archived": False}}
    if final_url:
        e["final_url"] = final_url
    return e


def _run_pass(entries, submit):
    with (
        patch(f"{W}.capture_capacity", return_value={"available": 2}),
        patch(f"{W}.submit", side_effect=submit) as sub,
        patch(f"{W}.check_job_status", return_value={"state": "pending"}),
        patch(f"{W}.system_status", return_value={"ok": True}),
        patch(f"{W}.service_health_note", return_value=None),
        patch(f"{W}.check", return_value={"archived": False}),
        patch.object(resolver, "classify_host", return_value=HOST_PUBLIC),
        patch.object(resolver, "_CAPTURE_POLL_BUDGET_SECONDS", 0.2),
    ):
        resolver._submit_missing_archives(
            entries, {"access_key": "ak", "secret_key": "sk"}
        )
    return sub


def _snapshot_of(url):
    return lambda u, **k: {
        "submitted": True,
        "archived": True,
        "job_id": None,
        "snapshot_url": f"https://web.archive.org/web/20261115181042/{url}",
        "snapshot_ts": "20261115181042",
        "snapshot_age_days": 0,
    }


class TestArchiveTarget:
    def test_a_followed_grounding_redirect_archives_the_publisher(self):
        assert resolver._archive_target(_entry(WRAPPER, PUBLISHER)) == PUBLISHER

    def test_an_unfollowed_grounding_redirect_keeps_the_wrapper(self):
        assert resolver._archive_target(_entry(WRAPPER)) == WRAPPER

    def test_an_ordinary_redirect_keeps_the_cited_url(self):
        """The cited URL is the one a reader clicks; archive.org follows the
        redirect itself, so only the opaque wrapper is swapped out."""
        cited = "https://www.cbsnews.com/amp/news/faa-ground-stop"
        landed = "https://www.cbsnews.com/news/faa-ground-stop/"
        assert resolver._archive_target(_entry(cited, landed)) == cited

    def test_a_redirect_landing_on_an_archive_keeps_the_wrapper(self):
        snap = "https://web.archive.org/web/2024/https://example.com/a"
        assert resolver._archive_target(_entry(WRAPPER, snap)) == WRAPPER

    def test_the_predicate_is_the_one_the_worklist_uses(self):
        from ci_article_review import worklist

        assert worklist._is_grounding_redirect(WRAPPER)
        assert draft_citations.is_grounding_redirect(WRAPPER)
        assert not draft_citations.is_grounding_redirect(PUBLISHER)


class TestTheSubmissionPass:
    def test_the_publisher_is_submitted_not_the_wrapper(self):
        entry = _entry(WRAPPER, PUBLISHER)
        sub = _run_pass([entry], _snapshot_of(PUBLISHER))
        assert sub.call_args.args[0] == PUBLISHER

    def test_the_publishers_snapshot_is_not_called_off_site(self):
        """The defect itself: the capture of the right page was demoted."""
        entry = _entry(WRAPPER, PUBLISHER)
        _run_pass([entry], _snapshot_of(PUBLISHER))
        wb = entry["wayback"]
        assert wb["archive_outcome"] == wayback.ARCHIVE_ARCHIVED
        assert not wb.get("capture_redirected_off_site")
        assert PUBLISHER in wb["snapshot_url"]

    def test_a_real_off_site_capture_is_still_demoted(self):
        """The guard still works for a page that redirects the crawler away."""
        cited = "https://www.ecfr.gov/current/title-47/part-4"
        entry = _entry(cited)
        _run_pass([entry], _snapshot_of("https://unblock.federalregister.gov/"))
        assert entry["wayback"]["archive_outcome"] == wayback.ARCHIVE_CAPTURE_FAILED

    def test_two_wrappers_for_one_page_share_one_submission(self):
        a = _entry(WRAPPER + "a", PUBLISHER)
        b = _entry(WRAPPER + "b", PUBLISHER)
        sub = _run_pass([a, b], _snapshot_of(PUBLISHER))
        assert sub.call_count == 1
        assert a["wayback"]["archive_outcome"] == wayback.ARCHIVE_ARCHIVED
        assert b["wayback"]["archive_outcome"] == wayback.ARCHIVE_ARCHIVED

    def test_submit_is_given_an_explicit_http_timeout(self):
        """spn-client defaults to 120s, far above this module's 45s budget."""
        sub = _run_pass(
            [_entry("https://example.com/a")], _snapshot_of("https://example.com/a")
        )
        assert sub.call_args.kwargs["timeout"] == resolver._SUBMIT_HTTP_TIMEOUT_SECONDS
        assert resolver._SUBMIT_HTTP_TIMEOUT_SECONDS < resolver._SUBMIT_TIMEOUT_SECONDS


class TestTheLookup:
    """The direct fetch asks archive.org about the page it read."""

    def _fetch(self, final_url):
        resp = MagicMock()
        resp.url = final_url
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/html"}
        resp.text = (
            "<html><body><article><p>" + ("word " * 80) + "</p></article></body></html>"
        )
        resp.content = resp.text.encode()
        resp.encoding = "utf-8"
        resp.apparent_encoding = "utf-8"
        resp.raise_for_status = MagicMock()
        return resp

    def _looked_up(self, cited, landed):
        check = MagicMock(return_value={"archived": False})
        with (
            patch.object(resolver, "safe_get", return_value=self._fetch(landed)),
            patch(f"{W}.check", check),
            patch.object(resolver, "classify_host", return_value=HOST_UNRESOLVABLE),
            patch.object(
                resolver,
                "_verify_relevance",
                return_value=({"checked": False, "reason": "no key"}, None),
            ),
        ):
            resolver.resolve_citations([{"claim": "c", "known_urls": [cited]}], [])
        return [c.args[0] for c in check.call_args_list]

    def test_a_grounding_redirect_is_looked_up_at_the_publisher(self):
        assert self._looked_up(WRAPPER, PUBLISHER) == [PUBLISHER]

    def test_an_ordinary_redirect_is_looked_up_as_cited(self):
        cited = "https://www.cbsnews.com/amp/news/faa-ground-stop"
        assert self._looked_up(cited, "https://www.cbsnews.com/news/x/") == [cited]


class TestALateAnswer:
    def _run_with_deadline_passed(self, submit):
        """Run the pass as if the batch gave up on every job at its deadline,
        then let each job finish afterwards, the way a daemon thread does."""
        held = []

        def fake_bounded(jobs, max_parallel):
            held.extend(jobs)
            return {
                name: (None, TimeoutError("Timed out after 45s and still running"))
                for name, _, _ in jobs
            }

        entry = _entry("https://www.ntsb.gov/SIR9601.pdf")
        with patch.object(resolver, "run_all_bounded", side_effect=fake_bounded):
            _run_pass([entry], submit)
        assert entry["wayback"]["archive_outcome"] == wayback.ARCHIVE_SUBMITTED
        assert entry["wayback"]["submission_abandoned"] is True
        with patch(f"{W}.submit", side_effect=submit):
            for _, fn, _ in held:
                fn()
        return entry["wayback"]

    def test_the_late_answer_is_recorded_and_flagged(self, caplog):
        caplog.set_level(logging.INFO, logger=resolver.log.name)
        wb = self._run_with_deadline_passed(
            lambda u, **k: {"submitted": True, "archived": False, "job_id": "spn2-x"}
        )
        assert wb["submission_job_id"] == "spn2-x"
        assert wb["archive_outcome"] == wayback.ARCHIVE_PENDING
        assert wb["submission_finished_late"] is True
        assert "submission_abandoned" not in wb
        assert "answered after the batch had given up" in caplog.text

    def test_an_answer_in_time_is_not_flagged(self):
        entry = _entry("https://example.com/a")
        _run_pass(
            [entry],
            lambda u, **k: {"submitted": True, "archived": False, "job_id": "spn2-y"},
        )
        assert "submission_finished_late" not in entry["wayback"]
        assert "submission_abandoned" not in entry["wayback"]
