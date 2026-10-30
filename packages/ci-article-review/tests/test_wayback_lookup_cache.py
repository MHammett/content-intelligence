"""Tests for the archive.org lookup memo — ``adapters/citation/wayback_lookup_cache``
and the ``check()`` wrapper in ``adapters/citation/wayback`` that reads it.

spn-client's own ``check`` is replaced with a recording fake at
``wayback._spn_check``, so nothing here reaches the network. The one call into
real spn-client code is for a ``web.archive.org`` URL, which it answers from the
URL's own timestamp without a request.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import spn_client

from ci_article_review.adapters.citation import wayback
from ci_article_review.adapters.citation import wayback_lookup_cache as wlc

URL = "https://example.com/report.pdf"
OTHER = "https://example.org/page"


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y%m%d%H%M%S"
    )


def _archived(url, days_ago=10):
    ts = _ts(days_ago)
    return {
        "url": url,
        "archived": True,
        "snapshot_url": f"https://web.archive.org/web/{ts}/{url}",
        "snapshot_ts": ts,
        "snapshot_age_days": days_ago,
        "snapshot_stale": days_ago > spn_client.DEFAULT_STALE_DAYS,
        "snapshot_status": "200",
        "snapshot_is_error_capture": False,
    }


def _missing(url):
    return {"url": url, "archived": False}


def _unanswered(url):
    return {
        "url": url,
        "archived": None,
        "error": "429 Client Error: Too Many Requests for url: ...",
    }


@pytest.fixture
def live(monkeypatch):
    """A recording stand-in for spn-client's ``check``, and a memory-only memo.

    ``live.answers[url]`` is what archive.org "says" for that URL: a dict, or a
    callable taking the URL. Unset URLs are not archived. ``live.calls`` lists
    every (url, cdx_fallback) that reached it.
    """
    calls = []
    answers = {}

    def fake(url, timeout=10, stale_days=None, cdx_fallback=False, cdx_timeout=60):
        calls.append((url, cdx_fallback))
        answer = answers.get(url)
        if callable(answer):
            answer = answer(url)
        answer = dict(answer or _missing(url))
        if answer.get("archived") and "snapshot_age_days" in answer:
            # As spn-client does: staleness against the caller's threshold.
            threshold = (
                stale_days if stale_days is not None else spn_client.DEFAULT_STALE_DAYS
            )
            answer["snapshot_stale"] = answer["snapshot_age_days"] > threshold
        return answer

    fake.calls = calls
    fake.answers = answers
    monkeypatch.setattr(wayback, "_spn_check", fake)
    wayback.open_lookup_cache(None)
    yield fake
    wayback.open_lookup_cache(None)


class TestOneLookupPerUrlPerRun:
    def test_second_ask_in_a_run_is_answered_from_memory(self, live):
        live.answers[URL] = _archived(URL)
        first = wayback.check(URL)
        second = wayback.check(URL)
        assert [c[0] for c in live.calls] == [URL]
        assert "lookup_cached" not in first
        assert second["lookup_cached"] is True
        assert second["snapshot_url"] == first["snapshot_url"]
        assert second["lookup_checked_at"].endswith("Z")

    def test_each_answer_is_its_own_dict(self, live):
        # Callers fold submission outcomes into the dict they are handed; one
        # citation's bookkeeping must not appear on another's.
        live.answers[URL] = _archived(URL)
        first = wayback.check(URL)
        second = wayback.check(URL)
        second["submitted"] = True
        third = wayback.check(URL)
        assert first is not second
        assert "submitted" not in first
        assert "submitted" not in third

    def test_link_pass_and_section_9_share_one_lookup(self, live):
        # The link pass passes a threshold, Section 9 none (spn-client's
        # default, 30 days). Same URL, one lookup; staleness is judged by each
        # caller's own threshold, as it would be with two live lookups.
        assert spn_client.DEFAULT_STALE_DAYS == 30
        live.answers[URL] = _archived(URL, days_ago=20)
        from_links = wayback.check(URL, timeout=10, stale_days=10)
        from_section_9 = wayback.check(URL)
        assert len(live.calls) == 1
        assert from_links["snapshot_stale"] is True
        assert from_section_9["snapshot_stale"] is False
        assert from_section_9["snapshot_age_days"] == 20

    def test_a_lookup_that_did_not_complete_is_asked_again(self, live):
        # ``archived: None`` is "we never found out", and remembering it would
        # make a bad minute last the rest of the run.
        live.answers[URL] = _unanswered(URL)
        wayback.check(URL)
        wayback.check(URL)
        assert len(live.calls) == 2

    def test_negative_answers_are_shared_within_a_run_too(self, live):
        wayback.check(URL)
        again = wayback.check(URL)
        assert len(live.calls) == 1
        assert again["archived"] is False
        assert again["lookup_cached"] is True

    def test_concurrent_misses_for_one_url_take_turns(self, live):
        # Section 9 resolves claims in parallel and ~1.8 claims cite each page,
        # so the same URL is asked at once from several threads. One goes to
        # archive.org; the rest find its answer.
        def slow(url):
            time.sleep(0.2)
            return _archived(url)

        live.answers[URL] = slow
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(wayback.check(URL)))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(live.calls) == 1
        assert all(r["archived"] for r in results)
        summary = wayback.lookup_cache_summary()
        assert summary["lookups"] == 8
        assert summary["live"] == 1
        assert summary["run_hits"] == 7

    def test_counts_add_up(self, live):
        live.answers[URL] = _archived(URL)
        wayback.check(URL)
        wayback.check(URL)
        wayback.check(OTHER)
        summary = wayback.lookup_cache_summary()
        assert summary["lookups"] == 3
        assert summary["live"] == 2
        assert summary["run_hits"] == 1
        assert summary["store_hits"] == 0
        assert summary["persistent"] is False
        assert summary["run_hits"] + summary["store_hits"] + summary["live"] == 3

    def test_a_non_string_url_reaches_spn_client_untouched(self, monkeypatch):
        # spn-client raises its own ValueError for a non-URL; the memo must not
        # turn that into a KeyError or a cached nothing.
        wayback.open_lookup_cache(None)
        with pytest.raises(ValueError):
            wayback.check(None)

    def test_opening_a_new_run_empties_the_memo(self, live):
        live.answers[URL] = _archived(URL)
        wayback.check(URL)
        wayback.open_lookup_cache(None)
        wayback.check(URL)
        assert len(live.calls) == 2


class TestRememberedAcrossRuns:
    def test_the_next_run_reads_the_answer_from_the_store(self, live, tmp_path):
        live.answers[URL] = _archived(URL)
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        wayback.close_lookup_cache()
        assert (tmp_path / wlc.STORE_FILENAME).exists()

        wayback.open_lookup_cache(tmp_path)
        again = wayback.check(URL)
        assert len(live.calls) == 1
        assert again["lookup_cached"] is True
        assert again["snapshot_url"].startswith("https://web.archive.org/")
        summary = wayback.lookup_cache_summary()
        assert summary["store_hits"] == 1
        assert summary["live"] == 0
        assert summary["persistent"] is True

    def test_positive_answers_live_seven_days_and_negatives_one(
        self, live, tmp_path, monkeypatch
    ):
        live.answers[URL] = _archived(URL)
        now = time.time()
        monkeypatch.setattr(wlc, "_now", lambda: now)
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        wayback.check(OTHER)
        assert len(live.calls) == 2

        # Two days on: the "no snapshot" is asked again, the snapshot is not.
        monkeypatch.setattr(wlc, "_now", lambda: now + 2 * 86400)
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        wayback.check(OTHER)
        assert [c[0] for c in live.calls] == [URL, OTHER, OTHER]

        # Eight days on: the snapshot answer has expired too.
        monkeypatch.setattr(wlc, "_now", lambda: now + 8 * 86400)
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        assert [c[0] for c in live.calls] == [URL, OTHER, OTHER, URL]

    def test_stale_recomputed_at_read_matches_spn_client(self, live, tmp_path):
        # A remembered snapshot ages. Its age and [STALE] must be what a live
        # lookup would say today, by spn-client's own arithmetic — which the
        # archive-URL branch of its check() runs with no request.
        days_ago = spn_client.DEFAULT_STALE_DAYS + 20
        live.answers[URL] = _archived(URL, days_ago=days_ago)
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        wayback.open_lookup_cache(tmp_path)
        remembered = wayback.check(URL)
        assert remembered["lookup_cached"] is True

        reference = spn_client.check(remembered["snapshot_url"])
        assert reference["is_archive_url"] is True
        assert remembered["snapshot_age_days"] == reference["snapshot_age_days"]
        assert remembered["snapshot_stale"] is reference["snapshot_stale"] is True
        # And a looser threshold passed by the caller is honoured on read.
        wayback.open_lookup_cache(tmp_path)
        relaxed = wayback.check(URL, stale_days=days_ago + 5)
        assert relaxed["lookup_cached"] is True
        assert relaxed["snapshot_stale"] is False

    def test_a_cdx_caller_refuses_an_availability_only_negative(self, live, tmp_path):
        # --archive-only asks CDX about negatives because the availability API
        # never lists a revisit; a plain negative is not the answer it wants.
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        assert live.calls == [(URL, False)]
        wayback.check(URL, cdx_fallback=True)
        assert live.calls == [(URL, False), (URL, True)]
        # Now CDX has been consulted: a second CDX caller is satisfied.
        wayback.check(URL, cdx_fallback=True)
        assert len(live.calls) == 2
        # And so is a plain caller.
        wayback.check(URL)
        assert len(live.calls) == 2

    def test_a_failed_cdx_lookup_does_not_count_as_consulted(self, live, tmp_path):
        live.answers[URL] = {**_missing(URL), "cdx_error": "timed out"}
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL, cdx_fallback=True)
        wayback.check(URL, cdx_fallback=True)
        assert len(live.calls) == 2
        remembered = wayback.check(URL)
        assert remembered["lookup_cached"] is True
        assert "cdx_error" not in remembered

    def test_a_cdx_caller_takes_a_fresh_positive_but_not_a_stale_one(
        self, live, tmp_path
    ):
        live.answers[URL] = _archived(URL, days_ago=3)
        live.answers[OTHER] = _archived(
            OTHER, days_ago=spn_client.DEFAULT_STALE_DAYS + 1
        )
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        wayback.check(OTHER)
        wayback.check(URL, cdx_fallback=True)
        wayback.check(OTHER, cdx_fallback=True)
        assert live.calls == [(URL, False), (OTHER, False), (OTHER, True)]

    def test_submitting_a_url_forgets_its_answer(self, live, tmp_path):
        from ci_article_review.adapters.citation import resolver

        wayback.open_lookup_cache(tmp_path)
        entry = {"url": URL, "wayback": wayback.check(URL)}
        resolver._record_submission(entry, {"submitted": True, "job_id": "spn2-1"})
        # Forgotten in memory: the same run asks again.
        wayback.check(URL)
        assert len(live.calls) == 2
        # Forgotten on disk too: the next run asks, where the store would
        # otherwise have answered.
        resolver._record_submission(entry, {"submitted": True, "job_id": "spn2-1"})
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        assert len(live.calls) == 3

    def test_a_capture_that_completes_is_remembered_as_the_answer(self, live, tmp_path):
        # The availability index lags a fresh capture by hours; a live lookup
        # in that window reads "not archived" and captures the page again.
        from ci_article_review.adapters.citation import resolver

        wayback.open_lookup_cache(tmp_path)
        entry = {"url": URL, "wayback": wayback.check(URL)}
        assert entry["wayback"]["archived"] is False
        ts = _ts(0)
        resolver._record_submission(
            entry,
            {
                "submitted": True,
                "archived": True,
                "snapshot_url": f"https://web.archive.org/web/{ts}/{URL}",
                "snapshot_ts": ts,
                "snapshot_age_days": 0,
                "snapshot_stale": False,
            },
        )
        wayback.open_lookup_cache(tmp_path)
        remembered = wayback.check(URL, cdx_fallback=True)
        assert len(live.calls) == 1
        assert remembered["archived"] is True
        assert remembered["lookup_cached"] is True
        assert remembered["snapshot_ts"] == ts
        assert remembered["snapshot_stale"] is False

    def test_a_job_that_reports_success_is_remembered_too(self, live, tmp_path):
        from ci_article_review.adapters.citation import resolver

        wayback.open_lookup_cache(tmp_path)
        entry = {"url": URL, "wayback": wayback.check(URL)}
        ts = _ts(0)
        resolver._record_job_status(
            entry,
            {
                "state": "success",
                "snapshot_url": f"https://web.archive.org/web/{ts}/{URL}",
                "snapshot_ts": ts,
            },
        )
        wayback.open_lookup_cache(tmp_path)
        assert wayback.check(URL)["archived"] is True
        assert len(live.calls) == 1

    def test_a_refused_submission_forgets_nothing(self, live, tmp_path):
        from ci_article_review.adapters.citation import resolver

        wayback.open_lookup_cache(tmp_path)
        entry = {"url": URL, "wayback": wayback.check(URL)}
        resolver._record_submission(entry, {"submitted": False, "error": "nope"})
        wayback.check(URL)
        assert len(live.calls) == 1

    def test_archive_urls_are_not_stored(self, live, tmp_path):
        snapshot = f"https://web.archive.org/web/{_ts(5)}/{URL}"
        live.answers[snapshot] = {
            "url": snapshot,
            "archived": True,
            "is_archive_url": True,
            "snapshot_ts": _ts(5),
        }
        wayback.open_lookup_cache(tmp_path)
        wayback.check(snapshot)
        wayback.close_lookup_cache()
        rows = (
            sqlite3.connect(str(tmp_path / wlc.STORE_FILENAME))
            .execute("SELECT count(*) FROM lookups")
            .fetchone()
        )
        assert rows == (0,)

    def test_unanswered_lookups_are_not_stored(self, live, tmp_path):
        live.answers[URL] = _unanswered(URL)
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL)
        assert wayback.lookup_cache_summary()["stored"] == 0

    def test_two_runs_at_once_share_one_store(self, live, tmp_path):
        # Several sessions with one CI_HISTORY_ROOT: each has its own process
        # and connection. What one learns the other may read.
        first = wlc.LookupCache(tmp_path / wlc.STORE_FILENAME)
        second = wlc.LookupCache(tmp_path / wlc.STORE_FILENAME)
        first.put(URL, _archived(URL))
        hit = second.get(URL)
        assert hit is not None and hit["lookup_cached"] is True
        first.close()
        second.close()

    def test_rows_older_than_the_longest_ttl_are_pruned_on_open(self, tmp_path):
        path = tmp_path / wlc.STORE_FILENAME
        cache = wlc.LookupCache(path)
        cache.put(URL, _archived(URL))
        cache.close()
        conn = sqlite3.connect(str(path))
        conn.execute(
            "UPDATE lookups SET checked_at = ?",
            (time.time() - wlc.ARCHIVED_TTL_SECONDS - 60,),
        )
        conn.commit()
        conn.close()
        wlc.LookupCache(path).close()
        remaining = (
            sqlite3.connect(str(path))
            .execute("SELECT count(*) FROM lookups")
            .fetchone()
        )
        assert remaining == (0,)


class TestTheStoreIsNeverAReasonForFewerAnswers:
    def test_a_store_that_cannot_open_leaves_lookups_live(self, live, tmp_path, caplog):
        live.answers[URL] = _archived(URL)
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        with caplog.at_level("WARNING"):
            wayback.open_lookup_cache(blocker)
        assert "could not be opened" in caplog.text
        assert wayback.lookup_cache_summary()["persistent"] is False
        assert wayback.check(URL)["archived"] is True
        # The in-run memo still works without the file.
        wayback.check(URL)
        assert len(live.calls) == 1

    def test_a_corrupt_row_is_ignored(self, live, tmp_path):
        live.answers[URL] = _archived(URL)
        path = tmp_path / wlc.STORE_FILENAME
        wlc.LookupCache(path).close()
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO lookups VALUES (?, ?, 1, 0, ?)",
            (URL, time.time(), "{not json"),
        )
        conn.commit()
        conn.close()
        wayback.open_lookup_cache(tmp_path)
        assert wayback.check(URL)["archived"] is True
        assert len(live.calls) == 1

    def test_a_store_that_breaks_mid_run_leaves_lookups_live(self, live, tmp_path):
        live.answers[URL] = _archived(URL)
        cache = wayback.open_lookup_cache(tmp_path)
        cache._conn.close()  # the handle is dead; every statement now raises
        assert wayback.check(URL)["archived"] is True
        assert wayback.check(OTHER)["archived"] is False
        assert len(live.calls) == 2

    def test_stored_json_holds_only_the_answer(self, live, tmp_path):
        live.answers[URL] = {
            **_archived(URL),
            "error": "should not be stored",
            "cdx_error": "nor this",
        }
        wayback.open_lookup_cache(tmp_path)
        wayback.check(URL, cdx_fallback=True)
        wayback.close_lookup_cache()
        (answer,) = (
            sqlite3.connect(str(tmp_path / wlc.STORE_FILENAME))
            .execute("SELECT answer FROM lookups")
            .fetchone()
        )
        stored = json.loads(answer)
        assert set(stored) <= set(wlc._ANSWER_FIELDS)
        assert "snapshot_age_days" not in stored
        assert "url" not in stored


class TestOnlyRequestsCountAsSent:
    """``live`` is what archive.org saw. spn-client answers two kinds of
    ``check()`` without a request, and those must not be counted as sent: on
    2026-10-29 the log said every lookup was sent while the breaker had
    skipped 306 of 483. These call the real ``spn_client.check``, so a change
    to how it words a skip fails here; the socket guard fails them if it
    makes a request instead."""

    @pytest.fixture(autouse=True)
    def _memory_only(self):
        wayback.open_lookup_cache(None)
        yield
        wayback.open_lookup_cache(None)

    def test_a_lookup_the_breaker_skipped_was_not_sent(self, monkeypatch):
        monkeypatch.setattr(
            spn_client.client,
            "_rate_limited_lookups",
            spn_client.client._CIRCUIT_TRIP_AFTER,
        )
        result = wayback.check(URL)
        assert result["archived"] is None
        assert "rate limit tripped" in result["error"]
        summary = wayback.lookup_cache_summary()
        assert summary["live"] == 0
        assert summary["skipped"] == 1

    def test_an_archive_link_was_not_sent(self):
        result = wayback.check(f"https://web.archive.org/web/{_ts(3)}/{URL}")
        assert result["is_archive_url"] is True
        summary = wayback.lookup_cache_summary()
        assert summary["live"] == 0
        assert summary["archive_urls"] == 1

    def test_a_refused_lookup_was_sent(self, live):
        # A 429 is a request archive.org saw and refused: it counts toward the
        # throttle, so it counts as sent.
        live.answers[URL] = _unanswered(URL)
        wayback.check(URL)
        summary = wayback.lookup_cache_summary()
        assert summary["live"] == 1
        assert summary["skipped"] == 0

    def test_every_ask_is_counted_once(self, live, monkeypatch):
        live.answers[URL] = _archived(URL)
        wayback.check(URL)
        wayback.check(URL)
        # The rest through the real spn-client, which answers both unasked.
        monkeypatch.setattr(wayback, "_spn_check", spn_client.check)
        wayback.check(f"https://web.archive.org/web/{_ts(3)}/{OTHER}")
        monkeypatch.setattr(
            spn_client.client,
            "_rate_limited_lookups",
            spn_client.client._CIRCUIT_TRIP_AFTER,
        )
        wayback.check(OTHER)
        s = wayback.lookup_cache_summary()
        assert (s["live"], s["run_hits"], s["archive_urls"], s["skipped"]) == (
            1,
            1,
            1,
            1,
        )
        assert s["lookups"] == (
            s["live"]
            + s["run_hits"]
            + s["store_hits"]
            + s["archive_urls"]
            + s["skipped"]
        )
