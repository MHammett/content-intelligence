"""archive.org's breaker reopens before Section 9 once it has been shut a while (#445).

spn-client's breaker stays open for the rest of the process once it trips.
On brockovich-fact-check run 2 it tripped in link validation at 05:47, and
Section 9, 40 minutes later, skipped all 188 lookups and the archive fallback
for pages that refused us. The throttle itself had long since lifted.
"""

import logging

import pytest

from ci_article_review.adapters.citation import resolver, wayback


@pytest.fixture
def breaker(monkeypatch):
    """A controllable breaker: ``state["open"]`` is what spn-client reports."""
    state = {"open": False, "resets": 0}

    def fake_reset():
        state["resets"] += 1
        state["open"] = False

    monkeypatch.setattr(wayback, "rate_limited_out", lambda: state["open"])
    monkeypatch.setattr(wayback, "_spn_reset_rate_limit_state", fake_reset)
    wayback.reset_rate_limit_state()
    state["resets"] = 0
    return state


class TestNoting:
    def test_a_closed_breaker_records_nothing(self, breaker):
        assert wayback.note_breaker_state() is False
        assert wayback.breaker_summary() == {}

    def test_the_first_sighting_is_kept(self, breaker):
        breaker["open"] = True
        assert wayback.note_breaker_state() is True
        first = wayback.breaker_summary()["first_seen_open_utc"]
        wayback.note_breaker_state()
        assert wayback.breaker_summary()["first_seen_open_utc"] == first
        assert wayback.breaker_summary()["open_at_end"] is True

    def test_the_run_start_reset_clears_the_record(self, breaker):
        breaker["open"] = True
        wayback.note_breaker_state()
        breaker["open"] = False
        wayback.reset_rate_limit_state()
        assert wayback.breaker_summary() == {}


class TestReopening:
    def test_not_before_the_cooldown(self, breaker):
        breaker["open"] = True
        wayback.note_breaker_state()
        seen = wayback._breaker["seen_open"]
        assert not wayback.reopen_breaker_if_cooled(cooldown=300, now=seen + 299)
        assert breaker["resets"] == 0

    def test_after_the_cooldown_it_reopens_and_says_so(self, breaker, caplog):
        breaker["open"] = True
        wayback.note_breaker_state()
        seen = wayback._breaker["seen_open"]
        with caplog.at_level(logging.INFO, logger=wayback.log.name):
            assert wayback.reopen_breaker_if_cooled(cooldown=300, now=seen + 2400)
        assert breaker["resets"] == 1
        assert "trying archive lookups again" in caplog.text
        summary = wayback.breaker_summary()
        assert summary["reopened"] == 1
        assert summary["open_at_end"] is False
        assert summary["first_seen_open_utc"]

    def test_a_closed_breaker_is_left_alone(self, breaker):
        assert not wayback.reopen_breaker_if_cooled(cooldown=0)
        assert breaker["resets"] == 0

    def test_at_most_two_reopenings_per_run(self, breaker):
        for _ in range(4):
            breaker["open"] = True
            wayback.note_breaker_state()
            seen = wayback._breaker["seen_open"]
            wayback.reopen_breaker_if_cooled(cooldown=300, now=seen + 600)
        assert breaker["resets"] == 2
        assert wayback.breaker_summary()["reopened"] == 2


class TestTheWarningSaysWhen:
    def test_the_end_of_pass_warning_names_the_first_sighting(self, breaker, caplog):
        breaker["open"] = True
        wayback.note_breaker_state()
        with caplog.at_level(logging.WARNING, logger=resolver.log.name):
            resolver._note_breaker_state()
        assert "first seen open at" in caplog.text
