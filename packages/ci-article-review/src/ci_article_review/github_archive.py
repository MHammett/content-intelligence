"""``ci-review --archive-only --github SLUG``: the archive status of a round, on its branch.

The publication's rule is that every URL an article cites has a Wayback snapshot
before the article publishes. A full review archives what it can, but the status
it leaves lives in ``pipeline/report.json``, which the drafting side is told not
to read, and a revision after the review adds URLs no run has seen. This module
writes the answer where the drafting side can act on it, as two files on the
round's branch, both owned by the pipeline:

``pipeline/archive.md``
    One line per cited URL: its snapshot, or why there is none. Read before a
    merge; never read into a revision.
``pipeline/archive.json``
    The same, for programs, with the capture job ids still running. The next
    archive pass on the round asks archive.org about those first, since local
    history never held them (``prior_pending_jobs``).

Both name the commit they were made from. A status file older than the head of
the branch says nothing about URLs added since; ``docs/GITHUB-DOCUMENTS.md``,
"Archiving before a merge", is the rule that reads it.
"""

import json
import logging
import posixpath
from datetime import datetime, timezone

from ci_core.github_repo import GitHubRepoError, resolve_token

from .github_documents import GitHubDocumentsError, _explain
from .github_rounds import check_can_write, open_client, require_token
from .github_writeback import secret_values
from .adapters.citation import wayback

log = logging.getLogger(__name__)

ARCHIVE_MD = "archive.md"
ARCHIVE_JSON = "archive.json"
SCHEMA = 1

#: Per-URL status words, in the order the summary counts them.
STATUS_ARCHIVED = "archived"
STATUS_STALE = "stale"
STATUS_PENDING = "pending"
STATUS_MISSING = "missing"
STATUS_UNCHECKED = "not checked"


def _folder(handoff):
    """``articles/<slug>``: the folder the round's files were read from."""
    files = (handoff.get("document_source") or {}).get("files") or []
    for f in files:
        if f.get("path", "").endswith("/draft.md"):
            return posixpath.dirname(f["path"])
    if files:
        return posixpath.dirname(files[0]["path"])
    raise GitHubDocumentsError("The round was read without its file list.")


def _status(wb):
    if wb.get("snapshot_url"):
        return STATUS_STALE if wb.get("snapshot_stale") else STATUS_ARCHIVED
    if wb.get("archive_outcome") == wayback.ARCHIVE_PENDING:
        return STATUS_PENDING
    if wb.get("archived") is None and not wb.get("archive_outcome"):
        return STATUS_UNCHECKED
    return STATUS_MISSING


def _reason(wb):
    """Why a URL has no fresh snapshot, in the report's own words."""
    detail = (wb.get("archive_outcome_detail") or "").strip()
    if detail:
        return detail
    if wb.get("error"):
        return str(wb["error"])
    outcome = wb.get("archive_outcome")
    if outcome:
        return wayback.ARCHIVE_OUTCOME_LABELS.get(outcome, outcome)
    return "no snapshot, and no capture was requested"


def status_rows(results):
    """One dict per (marker, URL), the shape ``archive.json`` stores."""
    rows = []
    for entry in results:
        wb = entry.get("wayback") or {}
        status = _status(wb)
        row = {
            "marker": entry.get("marker"),
            "url": entry.get("url"),
            "status": status,
            "snapshot_url": wb.get("snapshot_url"),
            "snapshot_ts": wb.get("snapshot_ts"),
            "snapshot_age_days": wb.get("snapshot_age_days"),
            "archive_outcome": wb.get("archive_outcome"),
            "submission_job_id": wb.get("submission_job_id"),
            "failure_kind": wb.get("submission_failure_kind"),
            "breaker_open_at_failure": wb.get("breaker_open_at_failure"),
        }
        if status != STATUS_ARCHIVED:
            row["reason"] = _reason(wb)
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def counts(rows):
    out = {
        s: 0
        for s in (
            STATUS_ARCHIVED,
            STATUS_STALE,
            STATUS_PENDING,
            STATUS_MISSING,
            STATUS_UNCHECKED,
        )
    }
    for row in rows:
        out[row["status"]] += 1
    return out


def _cell(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(handoff, rows, *, generated, breaker=None):
    source = handoff.get("document_source") or {}
    commit = source.get("commit_sha") or ""
    c = counts(rows)
    unique = len({r["url"] for r in rows})
    lines = [
        "# Archive status",
        "",
        f"Written by `ci-review` (a review's write-back, or `--archive-only "
        f"--github`) on {generated}. Do not edit: the next pass replaces it.",
        "",
        f"- Draft: `draft.md` at {commit[:12] or 'an unknown commit'} "
        f"(branch `{source.get('ref', '')}`). A commit after that one is not "
        "covered: run the pass again on the head.",
        f"- Cited URLs: {len(rows)} ({unique} unique). Fresh snapshot "
        f"{c[STATUS_ARCHIVED]}, stale snapshot only {c[STATUS_STALE]}, capture "
        f"still running {c[STATUS_PENDING]}, no snapshot {c[STATUS_MISSING]}, "
        f"not checked {c[STATUS_UNCHECKED]}.",
    ]
    if breaker:
        lines.append(
            "- archive.org's rate-limit breaker tripped during this pass"
            + (" and was still open at the end." if breaker.get("open_at_end") else ".")
        )
    else:
        lines.append("- archive.org's rate-limit breaker did not trip.")
    gaps = [r for r in rows if r["status"] != STATUS_ARCHIVED]
    lines += ["", "## Without a fresh snapshot", ""]
    if not gaps:
        lines.append("None. Every cited URL has a fresh snapshot.")
    else:
        lines += ["| Ref | URL | Status | Why |", "| --- | --- | --- | --- |"]
        for r in gaps:
            why = r.get("reason", "")
            if r["status"] == STATUS_STALE and r.get("snapshot_url"):
                why = f"only {r['snapshot_url']} ({r.get('snapshot_age_days')} days old). {why}"
            lines.append(
                f"| {_cell(r.get('marker'))} | {_cell(r['url'])} | {r['status']} | {_cell(why)} |"
            )
    lines += [
        "",
        "## Every cited URL",
        "",
        "| Ref | URL | Snapshot |",
        "| --- | --- | --- |",
    ]
    for r in rows:
        snap = r.get("snapshot_url") or r["status"]
        lines.append(
            f"| {_cell(r.get('marker'))} | {_cell(r['url'])} | {_cell(snap)} |"
        )
    return "\n".join(lines) + "\n"


def render_json(handoff, rows, *, generated, breaker=None, lookups=None):
    source = handoff.get("document_source") or {}
    draft_blob = next(
        (
            f.get("blob_sha")
            for f in source.get("files") or []
            if f.get("path", "").endswith("/draft.md")
        ),
        None,
    )
    doc = {
        "schema": SCHEMA,
        "generated": generated,
        "commit_sha": source.get("commit_sha"),
        "branch": source.get("ref"),
        "draft_blob_sha": draft_blob,
        "counts": counts(rows),
        "breaker": breaker or {},
        "lookups": lookups or {},
        "urls": rows,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def prior_pending_jobs(
    handoff, user_config, *, client=None, token_lookup=resolve_token
):
    """``url -> {"job_id": ...}`` for captures the last archive pass left running.

    Read from ``pipeline/archive.json`` at the commit the round was read at.
    Never raises: without the file, or without a token, the pass simply submits
    again, which is what it did before this file existed.
    """
    source = handoff.get("document_source") or {}
    try:
        path = f"{_folder(handoff)}/pipeline/{ARCHIVE_JSON}"
        own = client is None
        if own:
            client = open_client(source["repo"], token_lookup(), None)
        try:
            got = client.read(path, source["commit_sha"])
        finally:
            if own:
                client.close()
        doc = json.loads(got.data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a missing file is the normal case
        log.debug("No prior archive status to reconcile: %s", exc)
        return {}
    out = {}
    for row in doc.get("urls") or []:
        if row.get("status") == STATUS_PENDING and row.get("submission_job_id"):
            out[row["url"]] = {
                "job_id": row["submission_job_id"],
                "run_number": None,
                "generated": doc.get("generated"),
            }
    if out:
        log.info(
            "Archive status at %s left %d capture(s) running; asking archive.org "
            "about those first",
            (source.get("commit_sha") or "")[:12],
            len(out),
        )
    return out


def write_archive_status(
    handoff,
    results,
    *,
    settings,
    api_keys,
    breaker=None,
    lookups=None,
    client=None,
    token_lookup=resolve_token,
    now=None,
    out=print,
):
    """Commit ``pipeline/archive.md`` and ``pipeline/archive.json`` to the round's branch.

    One commit, fast-forward only. The two files belong to the pipeline, so a
    copy already on the branch is replaced, but only if it is still the blob this
    function read: someone else's push in between is refused, not overwritten.
    Returns the ``CommitResult``.
    """
    from .config_loader import normalize_github_documents

    settings = normalize_github_documents(settings)
    source = handoff.get("document_source") or {}
    branch = source.get("ref")
    if not source.get("pull_request") or not branch:
        raise GitHubDocumentsError(
            "The archive status is written to an open round's branch; this run "
            "read something else."
        )
    repo = source["repo"]
    folder = _folder(handoff)
    generated = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = status_rows(results)
    files = {
        f"{folder}/pipeline/{ARCHIVE_MD}": render_markdown(
            handoff, rows, generated=generated, breaker=breaker
        ).encode("utf-8"),
        f"{folder}/pipeline/{ARCHIVE_JSON}": render_json(
            handoff, rows, generated=generated, breaker=breaker, lookups=lookups
        ).encode("utf-8"),
    }
    c = counts(rows)
    slug = posixpath.basename(folder)
    round_number = source.get("round")
    message = (
        f"archive: {slug} r{round_number} at {(source.get('commit_sha') or '')[:12]}\n\n"
        f"{len(rows)} cited URL(s): {c[STATUS_ARCHIVED]} with a fresh snapshot, "
        f"{c[STATUS_STALE]} stale only, {c[STATUS_PENDING]} pending, "
        f"{c[STATUS_MISSING]} without, {c[STATUS_UNCHECKED]} not checked.\n"
    )
    own = client is None
    if own:
        client = open_client(repo, require_token(token_lookup), None)
    try:
        check_can_write(client.info(), repo, settings, "ci-review --archive-only")
        replace = {}
        for path in files:
            try:
                replace[path] = client.read(path, branch).blob_sha
            except GitHubRepoError as exc:
                if exc.kind != "not_found":
                    raise
        result = client.commit_files(
            branch,
            files,
            message,
            replace=replace,
            refuse_if_contains=secret_values(api_keys),
        )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(_explain(exc, repo, has_token=True)) from exc
    finally:
        if own:
            client.close()
    if result.written:
        out(f"Archive status committed to {branch}: {result.html_url}")
    else:
        out(f"Archive status on {branch} is unchanged. {result.html_url}".rstrip())
    return result
