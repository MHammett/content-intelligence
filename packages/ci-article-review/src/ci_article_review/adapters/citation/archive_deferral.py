"""Archive.org work a review hands on instead of doing (issue #497).

A review used to make every capture request and every snapshot comparison
inline, in the same half hour as everything else it asked archive.org. On
2026-11-15 two reviews of one draft were refused by archive.org mid-run: the
first after 176 requests, the second after only 76, nearly two hours after the
first block had lifted. Fewer requests in the review was not enough; the work
has to happen later, slowly, when archive.org is taking requests again.

While a run has a deferral open, ``_submit_missing_archives`` and
``_verify_archive_matches`` record their work here and return. The pipeline
writes what was recorded to a work file and starts ``archive_followup`` on it.
Nothing here talks to archive.org.

One deferral per process, module-level, for the same reason the lookup memo
is: the claim citations and the source list reach the two choke points through
separate call chains.
"""

import copy
import threading

from . import wayback

#: What the report says about a capture this run handed on.
DEFERRED_CAPTURE_DETAIL = (
    "not requested during the review: queued for the archiving pass that runs "
    "after it, slowly, once archive.org is taking requests"
)
#: What the report says about a comparison this run handed on.
DEFERRED_MATCH_DETAIL = (
    "not compared during the review: queued for the archiving pass that runs after it"
)

#: Citation fields the follow-up needs to submit and compare: the URL archived
#: (``final_url`` for a followed grounding redirect, see ``_archive_target``)
#: and the live page's checksum.
_CARRIED = ("url", "final_url", "checksum", "checksum_basis", "verified_via")


class Deferral:
    """The work one run handed on, one item per cited URL. Thread-safe: the
    comparison pass records from worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def _item(self, entry):
        key = entry.get("url")
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = {k: entry[k] for k in _CARRIED if k in entry}
            item["capture"] = False
            item["compare"] = False
        else:
            # A later citation of the same URL may carry what the first lacked.
            for k in _CARRIED:
                if k in entry and k not in item:
                    item[k] = entry[k]
        return item

    def defer_capture(self, entry):
        """Record a capture this run would have requested, and say so on it."""
        wb = entry.setdefault("wayback", {})
        with self._lock:
            item = self._item(entry)
            item["capture"] = True
            # What the lookup said, before this pass wrote over it: the
            # follow-up submits on the same evidence the review would have.
            item["wayback"] = copy.deepcopy(wb)
        wb["archive_outcome"] = wayback.ARCHIVE_NOT_ATTEMPTED
        wb["archive_outcome_detail"] = DEFERRED_CAPTURE_DETAIL
        wb["archive_deferred"] = True

    def defer_match(self, entry):
        """Record a snapshot comparison this run would have made."""
        with self._lock:
            item = self._item(entry)
            item["compare"] = True
            item.setdefault("wayback", copy.deepcopy(entry.get("wayback") or {}))

    def items(self):
        with self._lock:
            return [copy.deepcopy(v) for v in self._items.values()]

    def __len__(self):
        with self._lock:
            return len(self._items)


_active: Deferral | None = None


def begin():
    """Open a deferral for this run, replacing any left open."""
    global _active
    _active = Deferral()
    return _active


def end():
    """Close the run's deferral. Archive.org work after this is done inline."""
    global _active
    _active = None


def active():
    """The open deferral, or None when archive.org work is done inline."""
    return _active
