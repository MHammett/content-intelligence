"""Every archive.org request this package makes sits on spn-client's clock.

Three fetches used to bypass it: the resolver's archive fallback, the
archive-match comparison (four at a time) and link validation's fallback all
read ``web.archive.org`` through ``safe_get`` directly. spn-client paced and
counted its own calls and never saw these, so a run could keep reading
snapshots through a 429 or a refused-connection window (2026-11-11).

The spn-client functions used here (``pace``, ``note_outcome``,
``breaker_reason``, ``use_shared_state``, ``default_shared_state_path``) came
in 0.5.0. These tests put recording fakes in their place; spn-client's own
suite covers what the real ones do.
"""

from unittest.mock import MagicMock

import pytest
import requests

from ci_article_review.adapters.citation import resolver, wayback
from ci_article_review.analysis import links

SNAPSHOT = "https://web.archive.org/web/20260905121627/https://example.com/"


class _Spn:
    """Recording stand-ins for the spn-client functions the adapter looks up."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.shared = []
        self.reason = None
        monkeypatch.setattr(wayback, "_spn_pace", lambda: self.calls.append("pace"))
        monkeypatch.setattr(
            wayback,
            "_spn_note_outcome",
            lambda **kw: self.calls.append(("outcome", kw)),
        )
        monkeypatch.setattr(wayback, "_spn_breaker_reason", lambda: self.reason)
        monkeypatch.setattr(wayback, "_spn_use_shared_state", self.shared.append)
        monkeypatch.setattr(
            wayback, "_spn_default_shared_state_path", lambda: "DEFAULT.sqlite"
        )


@pytest.fixture
def spn(monkeypatch):
    return _Spn(monkeypatch)


def _response(status=200, headers=None):
    resp = MagicMock(status_code=status, headers=headers or {}, url=SNAPSHOT)
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status}", response=resp
        )
    return resp


class TestPacedFetch:
    def test_it_waits_on_the_clock_then_reports_the_answer(self, spn):
        fetch = MagicMock(return_value=_response(200))
        resp = wayback.paced_fetch(fetch, SNAPSHOT, timeout=7)
        assert resp.status_code == 200
        fetch.assert_called_once_with(SNAPSHOT, timeout=7)
        assert spn.calls == [
            "pace",
            ("outcome", {"status_code": 200, "retry_after": None}),
        ]

    def test_a_429_is_reported_with_its_retry_after(self, spn):
        fetch = MagicMock(return_value=_response(429, {"Retry-After": "17"}))
        wayback.paced_fetch(fetch, SNAPSHOT, timeout=7)
        assert spn.calls[-1] == ("outcome", {"status_code": 429, "retry_after": 17.0})

    def test_an_unreadable_retry_after_is_left_to_spn_client(self, spn):
        fetch = MagicMock(return_value=_response(429, {"Retry-After": "soon"}))
        wayback.paced_fetch(fetch, SNAPSHOT, timeout=7)
        assert spn.calls[-1] == ("outcome", {"status_code": 429, "retry_after": None})

    def test_an_exception_is_reported_and_still_raised(self, spn):
        boom = requests.exceptions.ConnectionError(
            ConnectionRefusedError(10061, "refused")
        )
        with pytest.raises(requests.exceptions.ConnectionError):
            wayback.paced_fetch(MagicMock(side_effect=boom), SNAPSHOT, timeout=7)
        assert spn.calls == ["pace", ("outcome", {"exc": boom})]

    def test_an_open_breaker_sends_nothing(self, spn, monkeypatch):
        monkeypatch.setattr(wayback, "rate_limited_out", lambda: True)
        fetch = MagicMock()
        with pytest.raises(wayback.ArchiveBreakerOpen):
            wayback.paced_fetch(fetch, SNAPSHOT, timeout=7)
        fetch.assert_not_called()
        assert spn.calls == []

    def test_the_breaker_being_open_reads_as_a_sentence(self):
        text = wayback.transport_failure_summary(
            wayback.ArchiveBreakerOpen(), "for the snapshot"
        )
        assert "breaker" in text and "not fetched" in text


class TestTheThreeCallSites:
    """Each direct snapshot read goes through ``paced_fetch``."""

    @pytest.fixture
    def seen(self, monkeypatch):
        seen = []

        def paced(fetch, url, timeout):
            seen.append(url)
            return _response(503)

        monkeypatch.setattr(wayback, "paced_fetch", paced)
        return seen

    def test_the_resolvers_archive_fallback(self, seen, monkeypatch):
        monkeypatch.setattr(
            resolver,
            "_wayback_check",
            lambda *a, **k: {"archived": True, "snapshot_url": SNAPSHOT},
        )
        content, wb = resolver._wayback_fallback_content("https://example.com/", 5)
        assert seen == [SNAPSHOT]
        assert content is None and wb["snapshot_url"] == SNAPSHOT

    def test_link_validations_fallback(self, seen, monkeypatch):
        monkeypatch.setattr(
            links,
            "wayback_check",
            lambda *a, **k: {"archived": True, "snapshot_url": SNAPSHOT},
        )
        assert links._wayback_fallback("https://example.com/", 5) is None
        assert seen == [SNAPSHOT]

    def test_the_archive_match_comparison(self, seen):
        entry = {
            "checksum": "x",
            "checksum_basis": "extracted_text",
            "wayback": {"snapshot_url": SNAPSHOT},
        }
        resolver._verify_archive_matches([entry])
        assert seen == [wayback.snapshot_raw_url(SNAPSHOT)]
        assert entry["archive_match"] == resolver.ARCHIVE_MATCH_UNCHECKED


class TestSharedRateLimit:
    def test_on_by_default_at_spn_clients_per_user_path(self, spn, monkeypatch):
        monkeypatch.delenv("SPN_CLIENT_SHARED_STATE", raising=False)
        assert wayback.enable_shared_rate_limit() == "DEFAULT.sqlite"
        assert spn.shared == ["DEFAULT.sqlite"]

    def test_the_environment_variable_wins(self, spn, monkeypatch):
        monkeypatch.setenv("SPN_CLIENT_SHARED_STATE", "ELSEWHERE.sqlite")
        assert wayback.enable_shared_rate_limit() == "ELSEWHERE.sqlite"
        assert spn.shared == ["ELSEWHERE.sqlite"]

    def test_switched_off_in_config_turns_it_off(self, spn, monkeypatch):
        monkeypatch.setenv("SPN_CLIENT_SHARED_STATE", "ELSEWHERE.sqlite")
        assert wayback.enable_shared_rate_limit(False) is None
        assert spn.shared == [None]


class TestReopeningLeavesARefusalAlone:
    """The five-minute reopen (#445) is for a 429 throttle. A refused
    connection is archive.org closing the address, and a machine-wide trip
    cannot be closed from here: neither is reset."""

    def _open(self, monkeypatch):
        wayback.reset_rate_limit_state()
        monkeypatch.setattr(wayback, "rate_limited_out", lambda: True)
        resets = []
        monkeypatch.setattr(
            wayback, "_spn_reset_rate_limit_state", lambda: resets.append(1)
        )
        wayback.note_breaker_state()
        return resets, wayback._breaker["seen_open"]

    @pytest.mark.parametrize("reason", ["connection_refused", "machine_wide"])
    def test_it_is_not_reopened(self, spn, monkeypatch, reason):
        resets, seen = self._open(monkeypatch)
        spn.reason = reason
        assert not wayback.reopen_breaker_if_cooled(cooldown=300, now=seen + 2400)
        assert resets == []
        assert wayback.breaker_summary()["reason"] == reason

    def test_a_429_trip_still_is(self, spn, monkeypatch):
        resets, seen = self._open(monkeypatch)
        spn.reason = "rate_limited"
        assert wayback.reopen_breaker_if_cooled(cooldown=300, now=seen + 2400)
        assert resets == [1]
