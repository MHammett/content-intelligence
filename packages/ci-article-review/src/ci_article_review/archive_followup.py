"""The archiving pass that runs after a review, slowly, in a process of its own.

Issue #497. A review hands its capture requests and snapshot comparisons to
``adapters.citation.archive_deferral`` instead of making them. When the review
has saved its report, ``spawn`` writes that work to
``run_N_<ts>_archive_work.json`` beside it and starts this module on it,
detached, so ``ci-review`` exits as it always did.

This process then:

1. takes ``archive_followup.lock`` under the history root, so two of these
   never talk to archive.org at once (a second one waits its turn);
2. waits while archive.org's machine-wide breaker is open, since a refused
   connection closes the address for about an hour;
3. looks up the URLs the review could not, because archive.org was refusing
   requests when it asked, so those are captured and compared like the rest;
4. submits and compares at one request every ``REQUEST_INTERVAL_SECONDS``
   instead of the review's three, polling capture jobs far longer than a
   review can afford to;
5. writes ``run_N_<ts>_archive_done.json`` beside the work file.

The next review does not need to read that file. A completed capture is kept
in the shared lookup store as the URL's new answer and a comparison in its
``snapshot_matches`` table, so the next review reads both without asking
archive.org. A capture still running when this pass gives up is listed in the
done file, and the next follow-up asks archive.org about its job instead of
submitting the page again.

Run by hand: ``python -m ci_article_review.archive_followup <work file>``.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from .adapters.citation import resolver, wayback

log = logging.getLogger("archive_followup")

WORK_SUFFIX = "_archive_work.json"
DONE_SUFFIX = "_archive_done.json"
LOG_SUFFIX = "_archive.log"
LOCK_FILENAME = "archive_followup.lock"

#: Gap between archive.org requests in this pass, against the review's 3s.
#: Nothing waits on this process, so its pace can be as gentle as archive.org
#: wants; 20s is three a minute, the anonymous capture ceiling. Not a measured
#: threshold: archive.org does not publish what closes an address.
REQUEST_INTERVAL_SECONDS = 20.0
#: How long the pass polls capture jobs it submitted. A review polls for 45s.
CAPTURE_POLL_SECONDS = 30 * 60
#: How long to wait for archive.org's breaker to close before giving up.
MAX_BREAKER_WAIT_SECONDS = 4 * 3600
#: How long to wait for another follow-up to finish before giving up.
MAX_LOCK_WAIT_SECONDS = 6 * 3600
_POLL_SLEEP_SECONDS = 60


def work_path_for(report_path):
    """``run_N_<ts>_archive_work.json`` beside ``run_N_<ts>_report.json``."""
    p = Path(report_path)
    return p.with_name(p.name.replace("_report.json", WORK_SUFFIX))


def write_work(report_path, items, *, history_root, config_dir, publication):
    """Write a run's deferred archive.org work beside its report. Returns the path."""
    path = work_path_for(report_path)
    payload = {
        "written_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report": Path(report_path).name,
        "history_root": str(Path(history_root).resolve()),
        "config_dir": str(Path(config_dir).resolve()),
        "publication": publication,
        "items": items,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def spawn(work_path):
    """Start this module on ``work_path`` in a detached process. Returns its pid,
    or None if it could not be started (logged; the work file stays, and the
    command to run it by hand is in the log)."""
    work_path = Path(work_path)
    log_path = work_path.with_name(work_path.name.replace(WORK_SUFFIX, LOG_SUFFIX))
    cmd = [sys.executable, "-m", "ci_article_review.archive_followup", str(work_path)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        with open(log_path, "ab") as out:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
                **kwargs,
            )
    except OSError as exc:
        log.warning(
            "Could not start the archiving pass (%s). Run it by hand: %s",
            exc,
            subprocess.list2cmdline(cmd),
        )
        return None
    return proc.pid


_ITEM_FLAGS = ("capture", "compare", "lookup", "citation")


def _entry(item):
    """A citation-shaped dict the resolver's passes accept, from a work item."""
    entry = {k: v for k, v in item.items() if k not in _ITEM_FLAGS}
    entry["resolved"] = True
    entry["wayback"] = dict(item.get("wayback") or {})
    return entry


def _prior_pending(history_root, exclude):
    """url -> ``{"job_id"}`` for captures an earlier follow-up left running."""
    pending = {}
    for path in sorted(Path(history_root).glob(f"*/*{DONE_SUFFIX}")):
        if path == exclude:
            continue
        try:
            done = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in done.get("items") or []:
            wb = row.get("wayback") or {}
            if wb.get("archive_outcome") in (
                wayback.ARCHIVE_PENDING,
                wayback.ARCHIVE_SUBMITTED,
            ) and wb.get("submission_job_id"):
                pending[resolver._archive_target(row)] = {
                    "job_id": wb["submission_job_id"]
                }
            else:
                # A later outcome for the URL settles an earlier pending one.
                pending.pop(resolver._archive_target(row), None)
    return pending


def _slow_down():
    """Pace this process at ``REQUEST_INTERVAL_SECONDS`` and poll for longer.

    ``_MIN_INTERVAL_SECONDS`` is spn-client's private base interval; it has no
    public setter, and this process is the only thing it affects. The shared
    machine-wide clock still applies on top, so a review starting meanwhile
    and this pass never send at once."""
    import spn_client.client as engine

    # A capture passes its own interval instead of the base one, so both of
    # its intervals are raised too.
    for name in (
        "_MIN_INTERVAL_SECONDS",
        "_AUTHENTICATED_SUBMIT_INTERVAL_SECONDS",
        "_ANONYMOUS_SUBMIT_INTERVAL_SECONDS",
    ):
        setattr(engine, name, max(getattr(engine, name), REQUEST_INTERVAL_SECONDS))
    resolver._MAX_SUBMIT_PARALLEL = 1
    resolver._MAX_MATCH_PARALLEL = 1
    resolver._MATCH_READ_INTERVAL_SECONDS = REQUEST_INTERVAL_SECONDS
    resolver._MATCH_TIMEOUT_SECONDS = 3 * REQUEST_INTERVAL_SECONDS
    resolver._SUBMIT_TIMEOUT_SECONDS = max(
        resolver._SUBMIT_TIMEOUT_SECONDS, 3 * REQUEST_INTERVAL_SECONDS
    )
    resolver._CAPTURE_POLL_BUDGET_SECONDS = CAPTURE_POLL_SECONDS


def _look_up(entries, stale_days):
    """Ask archive.org about each entry the review could not, in place.

    The answer replaces the entry's ``wayback``, as the review's own lookup
    would have, and lands in the shared lookup store for the next review. A
    lookup refused again leaves the entry with no answer, so nothing is
    captured or compared for it this time."""
    for entry in entries:
        entry["wayback"] = resolver._wayback_check(
            resolver._archive_target(entry), stale_days
        )


def _wait_for_breaker(max_wait, sleep=time.sleep):
    """Wait while archive.org's breaker is open. False if it never closed."""
    waited = 0.0
    while wayback.rate_limited_out():
        if waited >= max_wait:
            return False
        if waited == 0:
            log.info("archive.org's breaker is open; waiting for it to close")
        sleep(_POLL_SLEEP_SECONDS)
        waited += _POLL_SLEEP_SECONDS
        wayback.reopen_breaker_if_cooled()
    return True


def _load_creds(work):
    from .config_loader import load_publication_config, load_user_config, merge_configs

    config_dir = work["config_dir"]
    config = merge_configs(
        load_user_config(config_dir),
        load_publication_config(work["publication"], config_dir),
    )
    return config["api_keys"].get("archive_org"), config["pipeline"]


def run(work_path, *, sleep=time.sleep):
    """Do the deferred work in ``work_path``. Returns the done file's path, or
    None when the work could not start (another pass held the lock, or the
    breaker never closed); the work file is left for a later attempt."""
    work_path = Path(work_path)
    work = json.loads(work_path.read_text(encoding="utf-8"))
    history_root = Path(work["history_root"])
    done_path = work_path.with_name(work_path.name.replace(WORK_SUFFIX, DONE_SUFFIX))
    creds, pipeline_cfg = _load_creds(work)

    wayback.reset_rate_limit_state()
    wayback.enable_shared_rate_limit(
        pipeline_cfg.get("wayback_shared_rate_limit", True)
    )
    wayback.open_lookup_cache(
        history_root if pipeline_cfg.get("wayback_lookup_cache", True) else None
    )
    lock = FileLock(str(history_root / LOCK_FILENAME))
    try:
        lock.acquire(timeout=MAX_LOCK_WAIT_SECONDS)
    except Timeout:
        log.warning("Another archiving pass held the lock too long; giving up")
        return None
    try:
        if not _wait_for_breaker(MAX_BREAKER_WAIT_SECONDS, sleep=sleep):
            log.warning("archive.org's breaker never closed; giving up for now")
            return None
        _slow_down()
        items = work["items"]
        entries = [_entry(item) for item in items]
        lookups = [e for e, i in zip(entries, items) if i.get("lookup")]
        log.info(
            "Archiving pass: %d lookup(s), %d capture(s), %d comparison(s) at one "
            "request every %ss",
            len(lookups),
            sum(1 for i in items if i.get("capture")),
            sum(1 for i in items if i.get("compare")),
            REQUEST_INTERVAL_SECONDS,
        )
        stale_days = pipeline_cfg.get("wayback_snapshot_stale_days")
        _look_up(lookups, None if stale_days is None else int(stale_days))
        # A citation looked up here is offered for capture too; the resolver
        # submits only the ones the lookup found missing or stale. A URL only
        # the link check cited is never captured, as in a review.
        captures = [
            e
            for e, i in zip(entries, items)
            if i.get("capture") or (i.get("lookup") and i.get("citation"))
        ]
        resolver._submit_missing_archives(
            captures,
            creds,
            history_root,
            pipeline_cfg.get("wayback_capture"),
            prior_pending=_prior_pending(history_root, exclude=done_path),
        )
        resolver._verify_archive_matches(entries)
        done = {
            "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "work": work_path.name,
            "breaker": wayback.breaker_summary(),
            "archive_org_requests": wayback.request_summary(),
            "items": entries,
        }
        done_path.write_text(json.dumps(done, indent=2), encoding="utf-8")
        log.info("Archiving pass done: %s", done_path)
        return done_path
    finally:
        lock.release()
        wayback.close_lookup_cache()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ci_article_review.archive_followup"
    )
    parser.add_argument("work", help="a run_N_<ts>_archive_work.json")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return 0 if run(args.work) else 1


if __name__ == "__main__":
    sys.exit(main())
