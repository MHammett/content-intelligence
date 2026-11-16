"""Lookups archive.org refused are handed to the pass after the review (#497).

On 2026-11-15 a review of government-911-outages started with archive.org's
breaker already open. It skipped 154 of its 167 lookups, and since a URL with
no lookup answer is neither captured nor compared, the pass after it got only
4 items: those 154 URLs were dropped. A refused lookup is now handed on too,
and the pass looks it up before it captures and compares.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from ci_article_review import archive_followup
from ci_article_review.report_markdown import render_report_markdown
from ci_article_review.adapters.citation import archive_deferral, resolver, wayback
from ci_core.http import HOST_PUBLIC

from .test_archive_followup import (  # noqa: F401  (fixtures)
    SNAP,
    TEXT,
    URL,
    W,
    _cit,
    _work,
    no_config,
    restore_pacing,
)
from .test_report_markdown import _base_report

SKIP = {
    "url": URL,
    "archived": None,
    "error": "skipped: archive.org rate limit tripped earlier this run",
}
REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbCdEf123"


def _refused(url=URL):
    return {**SKIP, "url": url}


class TestTheLookupIsHandedOn:
    def test_a_refused_lookup_is_recorded(self):
        deferral = archive_deferral.begin()
        with patch.object(wayback, "_spn_check", return_value=dict(SKIP)):
            wayback.check(URL)
        [item] = deferral.items()
        assert item["url"] == URL and item["lookup"] is True
        assert not item.get("citation")

    def test_a_429_is_recorded_too(self):
        deferral = archive_deferral.begin()
        refused = {
            "url": URL,
            "archived": None,
            "error": "429 Client Error: Too Many Requests for url: x",
        }
        with patch.object(wayback, "_spn_check", return_value=refused):
            wayback.check(URL)
        assert [i["lookup"] for i in deferral.items()] == [True]

    @pytest.mark.parametrize(
        "answer",
        [
            {"url": URL, "archived": False},
            {"url": URL, "archived": None, "error": "timed out"},
        ],
    )
    def test_an_answer_or_another_failure_is_not(self, answer):
        deferral = archive_deferral.begin()
        with patch.object(wayback, "_spn_check", return_value=answer):
            wayback.check(URL)
        assert len(deferral) == 0

    def test_nothing_is_recorded_without_a_deferral(self):
        archive_deferral.end()
        with patch.object(wayback, "_spn_check", return_value=dict(SKIP)):
            assert wayback.check(URL)["archived"] is None

    def test_a_citation_carries_what_the_pass_needs_to_capture_and_compare(self):
        deferral = archive_deferral.begin()
        entry = _cit()
        entry["wayback"] = dict(SKIP)
        with (
            patch(f"{W}.submit") as submit,
            patch.object(resolver, "classify_host", return_value=HOST_PUBLIC),
        ):
            resolver._submit_missing_archives([entry])
        assert not submit.called
        [item] = deferral.items()
        assert item["lookup"] is True and item["citation"] is True
        assert item["checksum"] == entry["checksum"]
        # Still no answer, and the report must not read that as "not archived".
        assert entry["wayback"]["archived"] is None
        assert "archive_outcome" not in entry["wayback"]
        assert entry["wayback"]["lookup_deferred"] is True

    def test_a_followed_redirect_is_one_item_under_the_publisher_url(self):
        deferral = archive_deferral.begin()
        # The lookup was made on where the redirect landed...
        with patch.object(wayback, "_spn_check", return_value=_refused()):
            wayback.check(URL)
        # ...and the citation cites the redirect.
        entry = _cit(REDIRECT)
        entry["final_url"] = URL
        entry["wayback"] = _refused()
        resolver._submit_missing_archives([entry])
        [item] = deferral.items()
        assert item["citation"] is True and item["final_url"] == URL


class TestTheReportSaysItWillBeRetried:
    def _lines(self, wb):
        citation = {
            "claim": "c",
            "resolved": True,
            "verification": "checksum",
            "url": URL,
            "wayback": wb,
        }
        md = render_report_markdown(_base_report(section_9_citations=[citation]))
        [line] = [x for x in md.splitlines() if "Archive status is unknown" in x]
        return line

    def test_deferred_lookups_point_at_the_pass(self):
        text = self._lines({**SKIP, "lookup_deferred": True})
        assert "archiving pass that runs after this review" in text
        assert "--archive-only" not in text

    def test_otherwise_it_still_points_at_archive_only(self):
        assert "--archive-only" in self._lines(dict(SKIP))


@pytest.mark.usefixtures("restore_pacing", "no_config")
class TestThePassLooksUpFirst:
    def _item(self, url=URL, **flags):
        c = _cit(url)
        return {
            **{k: c[k] for k in ("url", "checksum", "checksum_basis")},
            "capture": False,
            "compare": False,
            "lookup": True,
            "wayback": _refused(url),
            **flags,
        }

    def _run(self, tmp_path, items, check):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        work = _work(tmp_path, items)
        with (
            patch(f"{W}._spn_check", side_effect=check) as looked_up,
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
            patch.object(resolver, "classify_host", return_value=HOST_PUBLIC),
            patch.object(resolver, "safe_get", return_value=resp),
            patch.object(resolver, "_extract_fetched", return_value=(TEXT, "html")),
        ):
            done = archive_followup.run(work)
        rows = json.loads(done.read_text(encoding="utf-8"))["items"]
        return rows, looked_up, submit

    def test_a_citation_found_missing_is_captured(self, tmp_path):
        rows, looked_up, submit = self._run(
            tmp_path,
            [self._item(citation=True)],
            lambda url, **kw: {"url": url, "archived": False},
        )
        assert looked_up.call_args.args[0] == URL
        assert submit.call_args.args[0] == URL
        assert rows[0]["wayback"]["archive_outcome"] == wayback.ARCHIVE_ARCHIVED

    def test_a_citation_found_archived_is_compared(self, tmp_path):
        rows, _, submit = self._run(
            tmp_path,
            [self._item(citation=True)],
            lambda url, **kw: {
                "url": url,
                "archived": True,
                "snapshot_url": SNAP,
                "snapshot_ts": "20261115181042",
            },
        )
        assert not submit.called
        assert rows[0]["archive_match"] == resolver.ARCHIVE_MATCH_IDENTICAL

    def test_a_link_only_url_is_looked_up_and_never_captured(self, tmp_path):
        rows, looked_up, submit = self._run(
            tmp_path,
            [self._item()],
            lambda url, **kw: {"url": url, "archived": False},
        )
        assert looked_up.called and not submit.called
        # The answer is in the shared store for the next review.
        wayback.open_lookup_cache(tmp_path)
        assert wayback.check(URL)["lookup_cached"] is True

    def test_refused_again_it_is_left_unanswered(self, tmp_path):
        rows, _, submit = self._run(
            tmp_path, [self._item(citation=True)], lambda url, **kw: _refused(url)
        )
        assert not submit.called
        assert rows[0]["wayback"]["archived"] is None
