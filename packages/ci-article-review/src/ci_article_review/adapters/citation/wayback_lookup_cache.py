"""A memo for archive.org availability lookups: one per URL per run, and
answers kept across runs under a time limit.

Why this exists. A review asks archive.org about every URL twice — once in
link validation, before the ensemble, and again in Section 9 when the citation
resolves — and asks again on every run, although most of a draft's URLs do not
change between rounds. Measured on four ``maximum`` runs of one article
(2026-10-25 to 2026-10-28): 162 lookups a run, 56 URLs asked in both passes,
73 of 88 link URLs identical to the previous run. archive.org throttles the
availability API at IP level and spn-client's circuit breaker trips after five
refusals, so the link pass used up the run's allowance and Section 9 got
``archived: null`` for every citation in three of the four runs. Nothing was
archived on those runs, because nothing is submitted on the strength of a null.
Fewer lookups is what makes the archiving pass work at all, besides being less
load on a service that asks for it.

What is kept, and for how long. Only archive.org's *answer* is stored —
whether a snapshot exists and which one — never a lookup that did not complete
(``archived: None`` means the breaker tripped or the request failed, and
remembering that would make a bad minute last a day). A positive answer is
reused for :data:`ARCHIVED_TTL_SECONDS` (seven days): a snapshot that exists
keeps existing, and a newer one changes nothing a reader acts on. A negative
answer is reused for :data:`MISSING_TTL_SECONDS` (one day): a page can be
captured at any time, not least by this pipeline, which forgets the entry for
any URL it submits and remembers a capture that completes as the new answer
(the availability index lags a fresh capture by hours, and a live lookup in
that window would capture the page again). The snapshot's age and staleness are recomputed on
every read against the caller's own threshold, so a cached answer reads exactly
as a live one would on the day it is read.

``cdx_fallback`` callers (``--archive-only``) are the one case a cached answer
can be the wrong kind of answer: the availability API never lists a revisit
record, which is the whole reason that mode consults the CDX index. So a cached
negative or stale answer satisfies a CDX caller only if CDX was consulted when
it was stored; a fresh positive satisfies anyone.

Where it lives. In memory always, so the two passes of one run share each
answer. On disk, when the pipeline opens one, as a SQLite file under the
history root — the place this repository already keeps what one run learns for
the next, and the one directory shared across worktrees when
``CI_HISTORY_ROOT`` is set. Any failure of the file (locked, corrupt, read-only
disk) is logged at debug level and the lookup goes live; the store is a
convenience and must never be a reason a run has fewer answers than before.

Existing solutions, considered 2026-10-28: ``requests-cache`` caches whole HTTP
responses by request, and cannot give a negative answer a shorter life than a
positive one, nor let a CDX caller refuse an availability-only answer, which is
the policy that matters here; ``diskcache`` would fit the storage but is a
dependency for one table of one kind of row. The standard library's ``sqlite3``
does it in the ~120 lines below, with WAL and a busy timeout for the sessions
that share a history root.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from spn_client import DEFAULT_STALE_DAYS

log = logging.getLogger(__name__)

#: How long a "snapshot exists" answer is reused.
ARCHIVED_TTL_SECONDS = 7 * 24 * 3600
#: How long a "no snapshot" answer is reused. Short because a capture can happen
#: at any time, and this pipeline is one of the things that makes them happen.
MISSING_TTL_SECONDS = 24 * 3600

#: Name of the store under the history root.
STORE_FILENAME = "wayback_lookups.sqlite"

#: The fields of a ``check()`` result that are archive.org's answer about the
#: URL. ``snapshot_age_days`` and ``snapshot_stale`` are derived from
#: ``snapshot_ts`` and the caller's threshold, so they are recomputed on read
#: rather than stored, and ``url`` is the key. ``error``/``cdx_error`` describe
#: a lookup, not a page, and are not stored (a result carrying ``error`` has
#: ``archived: None`` and is never stored anyway).
_ANSWER_FIELDS = (
    "archived",
    "snapshot_url",
    "snapshot_ts",
    "snapshot_status",
    "snapshot_is_error_capture",
    "found_via",
    "snapshot_is_revisit",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    url TEXT PRIMARY KEY,
    checked_at REAL NOT NULL,
    archived INTEGER NOT NULL,
    cdx_consulted INTEGER NOT NULL,
    answer TEXT NOT NULL
)
"""


def _now() -> float:
    return time.time()


def _age_days(ts: str, now: float) -> int | None:
    """Days since a Wayback timestamp (``YYYYMMDD...``), or None if unparseable.

    The same arithmetic as spn-client's ``_snapshot_state`` — whole days from
    the date part only — so a cached answer and a live one agree on the day a
    snapshot turns stale. ``test_stale_recomputation_matches_spn_client``
    checks that against spn-client's own output.
    """
    if len(ts) >= 8:
        try:
            snap = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            return (datetime.fromtimestamp(now, timezone.utc) - snap).days
        except ValueError:
            pass
    return None


class LookupCache:
    """Memo for ``spn_client.check`` answers, in memory and optionally on disk.

    Thread-safe: the resolver runs claims in parallel and the link pass runs
    ten URLs at a time. ``lock_for(url)`` lets a caller make concurrent misses
    for one URL take turns, so the second finds the first's answer in the memo
    instead of paying a paced live call for the same question.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._memo: dict[str, dict] = {}
        self._url_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._conn = None
        self.stats = {
            # Every ask that reached the memo, however it was answered.
            "lookups": 0,
            # Answered by an earlier ask in this run.
            "run_hits": 0,
            # Answered from the on-disk store, i.e. an earlier run.
            "store_hits": 0,
            # Went to archive.org.
            "live": 0,
            # Answers written to the store this run.
            "stored": 0,
        }
        if self.path is not None:
            self._open_store()

    # -- store plumbing -------------------------------------------------------

    def _open_store(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            # Nothing older than the longest TTL can ever be read again.
            conn.execute(
                "DELETE FROM lookups WHERE checked_at < ?",
                (_now() - ARCHIVED_TTL_SECONDS,),
            )
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError) as exc:
            log.debug("Wayback lookup store %s unavailable: %s", self.path, exc)
            self._conn = None

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:  # pragma: no cover - nothing to do about it
                    pass
                self._conn = None

    def _store_read(self, url):
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT checked_at, cdx_consulted, answer FROM lookups WHERE url = ?",
                (url,),
            ).fetchone()
        except sqlite3.Error as exc:
            log.debug("Wayback lookup store read failed for %s: %s", url, exc)
            return None
        if row is None:
            return None
        try:
            answer = json.loads(row[2])
        except ValueError:
            return None
        return {"checked_at": row[0], "cdx_consulted": bool(row[1]), "answer": answer}

    def _store_write(self, url, entry):
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO lookups VALUES (?, ?, ?, ?, ?)",
                (
                    url,
                    entry["checked_at"],
                    int(bool(entry["answer"].get("archived"))),
                    int(entry["cdx_consulted"]),
                    json.dumps(entry["answer"]),
                ),
            )
            self._conn.commit()
            self.stats["stored"] += 1
        except sqlite3.Error as exc:
            log.debug("Wayback lookup store write failed for %s: %s", url, exc)

    def _store_delete(self, url):
        if self._conn is None:
            return
        try:
            self._conn.execute("DELETE FROM lookups WHERE url = ?", (url,))
            self._conn.commit()
        except sqlite3.Error as exc:
            log.debug("Wayback lookup store delete failed for %s: %s", url, exc)

    # -- policy -------------------------------------------------------------

    @staticmethod
    def _usable(entry, *, stale_days, cdx_fallback, now):
        """Whether a stored entry may answer this request."""
        answer = entry["answer"]
        archived = answer.get("archived")
        ttl = ARCHIVED_TTL_SECONDS if archived else MISSING_TTL_SECONDS
        if now - entry["checked_at"] > ttl:
            return False
        if not cdx_fallback or entry["cdx_consulted"]:
            return True
        # A CDX caller takes an availability-only answer only when it is one
        # CDX would not have been asked to second-guess: a fresh snapshot.
        if not archived:
            return False
        threshold = stale_days if stale_days is not None else DEFAULT_STALE_DAYS
        age = _age_days(answer.get("snapshot_ts") or "", now)
        return age is not None and age <= threshold

    @staticmethod
    def _render(url, entry, *, stale_days, now):
        """A fresh result dict from a stored answer, as ``check()`` would return
        it today. Always a new dict: callers fold submission outcomes into the
        dict they are handed, and two citations must never share one."""
        answer = entry["answer"]
        result = {"url": url, **answer}
        if answer.get("archived"):
            threshold = stale_days if stale_days is not None else DEFAULT_STALE_DAYS
            age = _age_days(answer.get("snapshot_ts") or "", now)
            result["snapshot_age_days"] = age
            result["snapshot_stale"] = age is not None and age > threshold
        result["lookup_cached"] = True
        result["lookup_checked_at"] = datetime.fromtimestamp(
            entry["checked_at"], timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    # -- public -------------------------------------------------------------

    def lock_for(self, url):
        """A lock private to ``url``, so concurrent misses for it take turns."""
        with self._lock:
            lock = self._url_locks.get(url)
            if lock is None:
                lock = self._url_locks[url] = threading.Lock()
            return lock

    def note(self, kind):
        """Count one event in ``stats``. The caller that knows what happened
        counts it: ``check()`` asks the memo twice on a miss (before and inside
        the per-URL lock), and only it knows which ask was the lookup."""
        with self._lock:
            self.stats[kind] += 1

    def lookup(self, url, *, stale_days=None, cdx_fallback=False):
        """``(answer, source)``: the remembered answer for ``url`` if one may be
        used, with ``"run"`` or ``"store"`` for where it came from; else
        ``(None, None)``."""
        now = _now()
        with self._lock:
            entry = self._memo.get(url)
            source = "run"
            if entry is None:
                entry = self._store_read(url)
                source = "store"
                if entry is not None:
                    # Promote so the rest of the run reads it from memory.
                    self._memo[url] = entry
            if entry is None or not self._usable(
                entry, stale_days=stale_days, cdx_fallback=cdx_fallback, now=now
            ):
                return None, None
            return self._render(url, entry, stale_days=stale_days, now=now), source

    def get(self, url, *, stale_days=None, cdx_fallback=False):
        """The remembered answer for ``url`` if one may be used, else None."""
        return self.lookup(url, stale_days=stale_days, cdx_fallback=cdx_fallback)[0]

    def put(self, url, result, *, cdx_fallback=False):
        """Remember a live ``check()`` answer. Ignores anything that is not an
        answer: a lookup that did not complete, or an archive URL read off its
        own timestamp (free, and not a fact about archive.org's index)."""
        if not isinstance(result, dict) or result.get("archived") is None:
            return
        if result.get("is_archive_url"):
            return
        answer = {k: result[k] for k in _ANSWER_FIELDS if k in result}
        entry = {
            "checked_at": _now(),
            # CDX counts as consulted only if it answered. A ``cdx_error`` means
            # the availability answer stands alone, as if nobody had asked.
            "cdx_consulted": bool(cdx_fallback) and "cdx_error" not in result,
            "answer": answer,
        }
        with self._lock:
            self._memo[url] = entry
            self._store_write(url, entry)

    def forget(self, url):
        """Drop what is remembered for ``url``, in memory and on disk. Called
        when this run submits the URL for capture: whatever archive.org said
        before the submission is no longer what it would say after."""
        with self._lock:
            self._memo.pop(url, None)
            self._store_delete(url)

    def summary(self):
        """Counts, plus whether answers were kept on disk, for the log and the
        report. Not the path: a report is compared across machines and runs
        (the golden test, ci-history-report), and the path is in the log."""
        return {**self.stats, "persistent": self._conn is not None}
