"""Snapshot comparisons are remembered, and the reads are paced wider.

From issue #497: a wide review on 2026-11-15 was refused four connections in a
row by archive.org while ``_verify_archive_matches`` read snapshots, about 20 a
minute. A snapshot never changes and the checksum is the live page's, so one
comparison answers every later run that cites the same page.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from ci_article_review import pipeline
from ci_article_review.adapters.citation import resolver, wayback
from ci_article_review.adapters.citation.wayback_lookup_cache import LookupCache

SNAP = "https://web.archive.org/web/20261115181042/https://example.org/a"
TEXT = "y" * 400

#: The shipped values, read before the autouse fixture zeroes the interval.
SHIPPED_INTERVAL = resolver._MATCH_READ_INTERVAL_SECONDS
SHIPPED_BUDGET = resolver._MATCH_TIMEOUT_SECONDS


def _cit(checksum):
    return {
        "claim": "c",
        "url": "https://example.org/a",
        "resolved": True,
        "checksum": checksum,
        "checksum_basis": "extracted_text",
        "wayback": {"archived": True, "snapshot_url": SNAP},
    }


def _verify(citations, text=TEXT, side_effect=None):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    with (
        patch.object(
            resolver, "safe_get", return_value=resp, side_effect=side_effect
        ) as get,
        patch.object(resolver, "_extract_fetched", return_value=(text, "html")),
    ):
        resolver._verify_archive_matches(citations)
    return get


class TestTheComparisonIsRemembered:
    def test_a_second_run_does_not_read_the_snapshot(self, tmp_path):
        wayback.open_lookup_cache(tmp_path)
        first = _cit(resolver.sha256_checksum(TEXT))
        assert _verify([first]).call_count == 1
        assert first["archive_match"] == resolver.ARCHIVE_MATCH_IDENTICAL

        wayback.open_lookup_cache(tmp_path)  # the next run, same store
        second = _cit(resolver.sha256_checksum(TEXT))
        assert _verify([second]).call_count == 0
        assert second["archive_match"] == resolver.ARCHIVE_MATCH_IDENTICAL
        assert second["archive_match_detail"] == first["archive_match_detail"]
        assert second["archive_match_cached"] is True

    def test_differs_is_remembered_too(self, tmp_path):
        wayback.open_lookup_cache(tmp_path)
        _verify([_cit("deadbeef")])
        wayback.open_lookup_cache(tmp_path)
        again = _cit("deadbeef")
        assert _verify([again]).call_count == 0
        assert again["archive_match"] == resolver.ARCHIVE_MATCH_DIFFERS

    def test_a_changed_live_page_is_compared_again(self, tmp_path):
        """The key includes the live page's checksum: a revised page is a new
        question even against the same snapshot."""
        wayback.open_lookup_cache(tmp_path)
        _verify([_cit(resolver.sha256_checksum(TEXT))])
        wayback.open_lookup_cache(tmp_path)
        changed = _cit("a-different-live-page")
        assert _verify([changed]).call_count == 1
        assert changed["archive_match"] == resolver.ARCHIVE_MATCH_DIFFERS

    @pytest.mark.parametrize(
        "how",
        [
            # Not a refused connection: that backs the real clock off a
            # minute, and the point here is only that the read failed.
            {"side_effect": OSError("the snapshot read failed")},
            {"text": "too short"},
        ],
        ids=["unreadable", "no-article-text"],
    )
    def test_an_unchecked_snapshot_is_not_remembered(self, tmp_path, how):
        """Failing to read is a fact about this run; the next run must try."""
        wayback.open_lookup_cache(tmp_path)
        first = _cit("abc")
        _verify([first], **how)
        assert first["archive_match"] == resolver.ARCHIVE_MATCH_UNCHECKED
        wayback.open_lookup_cache(tmp_path)
        assert _verify([_cit("abc")]).call_count == 1

    def test_an_older_store_gains_the_table_on_open(self, tmp_path):
        path = tmp_path / "wayback_lookups.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE lookups (url TEXT PRIMARY KEY, checked_at REAL NOT NULL,"
            " archived INTEGER NOT NULL, cdx_consulted INTEGER NOT NULL,"
            " answer TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        cache = LookupCache(path)
        cache.match_put(SNAP, "abc", "identical", "d")
        cache.close()
        reopened = LookupCache(path)
        assert reopened.match_get(SNAP, "abc") == {
            "verdict": "identical",
            "detail": "d",
        }
        reopened.close()


class TestTheReadIsPacedWider:
    def test_each_read_asks_for_the_wider_gap(self, monkeypatch):
        asked = []
        monkeypatch.setattr(resolver, "_MATCH_READ_INTERVAL_SECONDS", 10.0)
        monkeypatch.setattr(wayback, "_spn_pace", lambda m=None: asked.append(m))
        _verify([_cit("abc")])
        assert asked == [10.0]

    def test_the_shipped_budget_covers_the_wait(self):
        assert SHIPPED_INTERVAL > 3.0
        assert SHIPPED_BUDGET > 2 * SHIPPED_INTERVAL


class TestTheUnsentNote:
    @staticmethod
    def _unsent(url):
        return {
            "url": url,
            "wayback": {"submitted": False, "breaker_open_at_failure": True},
        }

    def test_nothing_unsent_says_nothing(self):
        sent = {"url": "u", "wayback": {"submitted": True}}
        assert pipeline._unsent_archive_note([sent], [], "d.md", "pub") is None

    def test_unsent_urls_get_the_command_and_a_time(self, caplog, monkeypatch):
        monkeypatch.setattr(
            wayback,
            "breaker_summary",
            lambda: {"first_seen_open_utc": "2026-11-15T18:28:56+00:00"},
        )
        note = pipeline._unsent_archive_note(
            [self._unsent("https://a")],
            [self._unsent("https://a"), self._unsent("https://b")],
            "drafts/x.md",
            "mikehammett",
        )
        assert note == {
            "count": 2,
            "retry_after_utc": "2026-11-15 19:28 UTC",
            "command": (
                'ci-review --archive-only --draft "drafts/x.md" '
                "--publication mikehammett"
            ),
        }
        assert "2 URL(s) were never sent" in caplog.text

    def test_a_run_without_a_draft_path_names_no_command(self):
        note = pipeline._unsent_archive_note([self._unsent("u")], None, None, "p")
        assert note["command"] is None
