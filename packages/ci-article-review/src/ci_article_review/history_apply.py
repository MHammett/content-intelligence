"""Write an article's imported history into the document repository: ``ci-docs import apply``.

Step 2 of the history import (design: ``docs/GITHUB-DOCUMENTS.md``, "Importing an
article's history", Decision 7; the spec is issue #421). ``history_import`` turned
an article's scattered private history into a plan the author edited and
``check``ed. This writes that plan as **merged pull requests, one per round, in
order, with the original dates on every commit**, so the repository starts with the
article's whole history and the next round continues from it.

For each round with an included event, in order::

    1. branch    round/<slug>/rN, cut from the base branch's tip
    2. commits   one per event, author and committer dated at the event's time
                   draft: / revise:   draft.md (+ metadata.md), or handoff.md
                   review:            review.md and pipeline/{run.json,report.json,worklist.md}
                   close-out: / publication:
    3. the pull request, its labels, and one review per run (no line comments)
    4. the merge: main moved to a merge commit built with the Git Data API,
       dated at ``accepted_at``; the pull request reads merged
    5. the annotated tag <slug>/rN, dated at ``accepted_at`` (``tag_merged_rounds``)

Every step writes its result into the plan's ``applied`` block and rewrites the
plan file straight away, so a failure resumes where it stopped: a step that is
recorded and still true on GitHub is skipped, one that is recorded and gone is an
error and never a redo, and one that GitHub already has but the plan does not
record (the process died between the write and the record) is adopted when it is
unmistakably this import's own, by the ``Plan-event`` trailer its commit carries.
Nothing here deletes a branch or a tag, moves a tag, or force-updates a ref.

Refused before any write: a plan that ``check`` rejects, a public repository (no
override), a token that cannot push, merge settings other than merge-commits-only,
a main branch that already has ``<folder>/<slug>/`` and any branch
``round/<slug>/...`` or tag ``<slug>/...`` the plan's ``applied`` block does not
account for (the import starts an article's history and never interleaves with
live rounds).

The sources are private. A commit message names where a text came from (a file
name, a Drive id, a conversation and message id, a run) and never what it says.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ci_core.github_repo import GitHubRepoError, Signature, git_blob_sha

from . import documents_init as di
from . import history_import as hi
from .github_documents import (
    DRAFT_FILE,
    HANDOFF_FILE,
    METADATA_FILE,
    RUN_FILE,
    GitHubDocumentsError,
    _folder,
    round_branch,
    round_tag,
    validate_slug,
)
from .github_put import CLOSE_OUT_NAMES, commit_subject
from .github_rounds import CREATED, PRESENT, tag_merged_rounds
from .github_writeback import (
    REPORT_FILE,
    RESULTS_FILE,
    REVIEW_FILE,
    WORKLIST_FILE,
    _lf,
    _refuse_secrets,
    _run_json,
)
from .review_body import MAX_BODY, RunFacts, build_review_body, is_review_of

__all__ = ["ImportApplyError", "apply_plan"]

#: The event kinds that have a place in an article's folder. ``changes``, ``review``
#: and ``unknown`` documents are not part of the layout: exclude them in the plan.
_WRITABLE = ("draft", "metadata", "handoff", "run", "publication", "close-out")

#: Trust order for choosing which of an event's sources supplies the bytes: the
#: exact copy first, a reconstruction last.
_SOURCE_ORDER = {"drive": 0, "file": 1, "run": 2, "chat": 3, "recovered": 4}

_DIRECTION_LINE = re.compile(r"^\s*Direction\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_EVENT_TRAILER = re.compile(r"^Plan-event:\s*(e\d+)\s*$", re.MULTILINE)

#: The trailer a reconstructed version carries, word for word (issue #421).
RECOVERED_TRAILER = (
    "Recovered: reproduced by a chat from its context; not the original bytes"
)

#: Seconds to wait between reads of a pull request whose merged flag has not yet
#: appeared. GitHub showed it within five seconds in the live check (#413) but took
#: about 90 seconds in a later one, so the schedule runs to about five minutes
#: (302 seconds) before it gives up, backing off from 1 to 10 seconds a read.
_MERGE_WAITS = (1, 1, 2, 2, 3, 3, 5, 5, 5, 5) + (10,) * 27

#: A progress line while waiting for the merged flag, every this many seconds.
_MERGE_PROGRESS_EVERY = 30


class ImportApplyError(GitHubDocumentsError):
    """The import stopped, with a message that says what is written and what to do."""


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value):
    return hi._parse_time(value, "time")


def _short(sha):
    return str(sha or "")[:7]


@dataclass
class _Unit:
    """One commit: the plan events it writes, in plan order."""

    events: list
    kind: str  # draft | revise | review | close-out | publication
    at: datetime
    run: dict | None = None

    @property
    def ids(self):
        return [e["id"] for e in self.events]


@dataclass
class _Round:
    number: int
    accepted: datetime
    first: datetime
    units: list = field(default_factory=list)

    @property
    def runs(self):
        return [u for u in self.units if u.kind == "review"]


# ---------------------------------------------------------------------------
# From the plan to commits
# ---------------------------------------------------------------------------


def _fold_partners(events):
    """``{index of a metadata or handoff event: index of the draft it rides with}``.

    A metadata or handoff event is written in its draft's own commit when it is
    attached to that draft and sits right next to it in the round, so a round's
    first commit is ``draft.md`` and ``metadata.md`` together, as the issue says.
    A partner that is not adjacent is its own commit: folding it would date a
    file before an event that happened between.
    """
    by_id = {e["id"]: i for i, e in enumerate(events)}
    taken = {}
    folded = {}
    for i, event in enumerate(events):
        if event["kind"] not in ("metadata", "handoff"):
            continue
        target = by_id.get(event.get("attached_to"))
        if target is None or events[target]["kind"] != "draft":
            continue
        if abs(target - i) != 1 or (target, event["kind"]) in taken:
            continue
        taken[(target, event["kind"])] = i
        folded[i] = target
    return folded


def _build_units(events):
    """The commits of one round, from its included events in plan order."""
    folded = _fold_partners(events)
    partners = {}
    for partner, draft in folded.items():
        partners.setdefault(draft, []).append(partner)
    units = []
    seen_draft = False
    for i, event in enumerate(events):
        if i in folded:
            continue
        group = [event] + [events[p] for p in sorted(partners.get(i, []))]
        group.sort(key=lambda e: events.index(e))
        kind = event["kind"]
        at = max(_when(e["at"]) for e in group)
        if kind == "run":
            units.append(_Unit(group, "review", at, run=event))
        elif kind in ("close-out", "publication"):
            units.append(_Unit(group, kind, at))
        else:
            holds_text = any(e["kind"] in ("draft", "handoff") for e in group)
            label = "draft" if (holds_text and not seen_draft) else "revise"
            seen_draft = seen_draft or holds_text
            units.append(_Unit(group, label, at))
    return units


def _direction(text):
    """``to-chat`` or ``to-code`` from a close-out note's ``Direction:`` line, else ``None``."""
    found = _DIRECTION_LINE.search(text)
    if not found:
        return None
    value = found.group(1).lower().replace(" ", "-")
    chat, code = "to-chat" in value, "to-code" in value
    if chat == code:
        return None
    return "to-chat" if chat else "to-code"


def _plan_rounds(plan):
    """``[_Round]`` for the rounds that have an included event, in plan order."""
    rounds = []
    for group in plan["rounds"]:
        events = [e for e in group.get("events", []) if e.get("include")]
        if not events:
            continue
        rnd = _Round(
            number=group["round"],
            accepted=_when(group["accepted_at"]),
            first=min(_when(e["at"]) for e in events),
        )
        rnd.units = _build_units(events)
        rounds.append(rnd)
    return rounds


# ---------------------------------------------------------------------------
# The applier
# ---------------------------------------------------------------------------


class _Applier:
    def __init__(
        self,
        plan,
        plan_path,
        client,
        *,
        repo,
        settings,
        secrets,
        dry_run,
        out,
        now,
        sleep,
        merge_waits,
    ):
        self.plan = plan
        self.plan_path = Path(plan_path)
        self.client = client
        self.repo = repo
        self.slug = plan["slug"]
        self.folder = _folder(
            (settings.get("path_prefix") or "").strip().strip("/"), self.slug
        )
        self.secrets = secrets
        self.dry = dry_run
        self.out = out
        self._now = now
        self.sleep = sleep
        self.merge_waits = merge_waits
        self.rounds = _plan_rounds(plan)
        #: ``github_documents.branch``; ``None`` means the repository's default branch.
        self.plan_branch = None
        self.base_branch = None
        self.user = None
        self.texts = {}
        self.manifests = {}
        # The folder's files as the last round's commits left them, and that round's
        # branch. A dry run carries them from round to round, because the commits it
        # describes do not exist; a real run reads GitHub for the state it needs.
        self._carry = None
        self._carry_round = None
        self.context = {
            "export_path": (plan.get("chat") or {}).get("export"),
            "terms": {
                str(t).lower()
                for t in [plan.get("slug"), *(plan.get("history_keys") or [])]
                if t
            },
        }
        self.applied = plan.setdefault("applied", {})
        if not isinstance(self.applied, dict):
            raise ImportApplyError("the plan's applied block is not an object")

    # -- small things ----------------------------------------------------------

    def say(self, text):
        self.out(text)

    def would(self, text):
        self.out(f"  would {text}")

    def save(self):
        """Rewrite the plan file now (atomically); a dry run writes nothing."""
        if self.dry:
            return
        text = hi.plan_text(self.plan)
        temp = self.plan_path.with_name(f".{self.plan_path.name}.{os.getpid()}.tmp")
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(temp, self.plan_path)
        finally:
            if temp.exists():
                temp.unlink()

    def signature(self, moment):
        return Signature(
            self.user.name or self.user.login,
            f"{self.user.id}+{self.user.login}@users.noreply.github.com",
            moment,
        )

    # -- the bytes of an event --------------------------------------------------

    def _source_text(self, source):
        """``(text, None)`` of one source with its original bytes where it has them."""
        if source.get("type") == "chat":
            return hi._load_source(source, self.context)  # a rebuilt text, normalised
        path = Path(source.get("path", ""))
        if not path.is_file():
            return None, f"{path.name} is missing"
        try:
            raw = hi._read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"{path.name} cannot be read ({exc.__class__.__name__})"
        if source.get("type") == "run" or source.get("part") == "corrected_draft":
            try:
                report = json.loads(raw)
                corrected = report["corrected_draft"]
            except (ValueError, KeyError, TypeError):
                return None, f"{path.name} no longer holds a corrected_draft"
            if source.get("lt_undone"):
                corrected = hi.undo_lt_corrections(
                    corrected, report.get("lt_corrections_applied") or []
                )
                if corrected is None:
                    return None, f"{path.name}: its corrections no longer undo"
            return corrected, None
        if source.get("part") == "draft":
            draft = hi._embedded_draft(raw)
            if draft is None:
                return None, f"{path.name} no longer holds a draft submission"
            return draft, None
        return raw, None

    def text_of(self, event):
        """The LF text an event writes, checked against the hash the plan recorded."""
        if event["id"] in self.texts:
            return self.texts[event["id"]]
        sources = sorted(
            event.get("sources") or [],
            key=lambda s: _SOURCE_ORDER.get(s.get("type"), 9),
        )
        why = []
        for source in sources:
            text, problem = self._source_text(source)
            if problem:
                why.append(problem)
                continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if hi._sha(hi.normalise(text)) != event.get("sha256"):
                why.append(
                    f"{Path(source.get('path', '')).name} has changed since the scan"
                )
                continue
            if not text.endswith("\n"):
                text += "\n"
            self.texts[event["id"]] = text
            return text
        raise ImportApplyError(
            f"event {event['id']}: no source holds the recorded bytes ({'; '.join(why)}). "
            "Run ci-docs import check."
        )

    def run_paths(self, event):
        report = Path(event["report"])
        name = report.name
        run_name = (
            name[: -len("_report.json")]
            if name.endswith("_report.json")
            else report.stem
        )
        return {
            "report": report,
            "review": report.with_name(f"{run_name}_review.md"),
            "worklist": report.with_name(f"{run_name}_worklist.md"),
            "results": report.with_name(f"{run_name}_results.json"),
            "name": run_name,
        }

    def include_capture(self, event):
        return bool(event.get("include_capture", self.plan.get("include_capture")))

    # -- messages ---------------------------------------------------------------

    def _manifest_id(self, path):
        """A Drive file's id, from the ``drive-manifest.json`` beside it, else its name."""
        path = Path(path)
        manifest = path.parent / "drive-manifest.json"
        if manifest not in self.manifests:
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = []
            self.manifests[manifest] = {
                item.get("name"): item.get("id")
                for item in (data if isinstance(data, list) else data.get("files", []))
                if isinstance(item, dict)
            }
        return self.manifests[manifest].get(path.name) or path.name

    def trailers(self, unit):
        """The trailer lines naming where the unit's texts came from, never what they say."""
        lines = []
        for event in unit.events:
            if event["kind"] == "run":
                report = Path(event["report"]).name
                name = (
                    report[: -len("_report.json")]
                    if report.endswith("_report.json")
                    else report
                )
                lines.append(f"Imported-from: run {name}")
                continue
            for source in sorted(
                event.get("sources") or [],
                key=lambda s: _SOURCE_ORDER.get(s.get("type"), 9),
            ):
                kind, path = source.get("type"), source.get("path", "")
                if kind == "chat":
                    lines.append("Imported-from: chat " + path.removeprefix("chat:"))
                elif kind == "drive":
                    lines.append(f"Imported-from: drive {self._manifest_id(path)}")
                elif kind == "run":
                    name = Path(path).name
                    name = (
                        name[: -len("_report.json")]
                        if name.endswith("_report.json")
                        else name
                    )
                    lines.append(f"Imported-from: run {name}")
                else:
                    lines.append(f"Imported-from: file {Path(path).name}")
        lines = list(dict.fromkeys(lines))
        lines.append(f"Imported-at: {_iso(self._now())}")
        for event in unit.events:
            if event.get("recovered"):
                lines.append(RECOVERED_TRAILER)
            if event.get("corroborated") is False:
                lines.append("Corroborated: no, only a chat held this version")
            if event.get("note"):
                lines.append(f"Source: {event['note']}")
        for event in unit.events:
            lines.append(f"Plan-event: {event['id']}")
        return list(dict.fromkeys(lines))

    def subject(self, rnd, unit, names):
        n = rnd.number
        if unit.kind == "review":
            return f"review: {self.slug} r{n} run {unit.run['run']}"
        if unit.kind in ("draft", "revise"):
            return f"{unit.kind}: {self.slug} r{n}"
        return commit_subject(self.slug, n, names[0])

    def message(self, rnd, unit, names):
        lines = [
            self.subject(rnd, unit, names),
            "",
            f"Files: {', '.join(names)}",
            "",
            *self.trailers(unit),
        ]
        return "\n".join(lines) + "\n"

    # -- what a commit writes ----------------------------------------------------

    def unit_files(self, rnd, rec, unit, state):
        """``(files, deletes)``: ``{path: bytes}`` and ``{path: expected blob}`` for one commit."""
        folder = self.folder
        files = {}
        has_handoff = any(e["kind"] == "handoff" for e in unit.events)
        for event in unit.events:
            kind = event["kind"]
            if kind == "draft" and not has_handoff:
                files[f"{folder}/{DRAFT_FILE}"] = self.text_of(event).encode("utf-8")
            elif kind == "draft":
                continue  # its text is inside the handoff
            elif kind == "metadata":
                files[f"{folder}/{METADATA_FILE}"] = self.text_of(event).encode("utf-8")
            elif kind == "handoff":
                files[f"{folder}/{HANDOFF_FILE}"] = self.text_of(event).encode("utf-8")
            elif kind == "publication":
                files[f"{folder}/publication.md"] = self.text_of(event).encode("utf-8")
            elif kind == "close-out":
                text = self.text_of(event)
                direction = _direction(text)
                name = next(n for n, d in CLOSE_OUT_NAMES.items() if d == direction)
                files[f"{folder}/{name}"] = text.encode("utf-8")
            elif kind == "run":
                files.update(self.run_files(rnd, rec, event))
        deletes = {}
        written = {
            p.rsplit("/", 1)[-1] for p in files if p.count("/") == folder.count("/") + 1
        }
        stale = []
        if DRAFT_FILE in written:
            stale.append(HANDOFF_FILE)  # the two-file form replaces a handoff
        if HANDOFF_FILE in written:
            stale += [DRAFT_FILE, METADATA_FILE]  # a handoff replaces the two-file form
        for name in stale:
            path = f"{folder}/{name}"
            if path in state and path not in files:
                deletes[path] = state[path]
        return files, deletes

    def reviewed_commit(self, rec, event):
        """The commit a run reviewed: the one that wrote its version, else the round's latest."""
        by_event = {eid: c["sha"] for c in rec["commits"] for eid in c["events"]}
        if event.get("draft") in by_event:
            return by_event[event["draft"]]
        if rec["commits"]:
            return rec["commits"][-1]["sha"]
        return rec["base"]

    def run_files(self, rnd, rec, event):
        paths = self.run_paths(event)
        folder = self.folder
        try:
            report = json.loads(paths["report"].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ImportApplyError(
                f"event {event['id']}: cannot read the run's report ({exc})"
            )
        if not isinstance(report, dict):
            raise ImportApplyError(
                f"event {event['id']}: the run's report is not a report"
            )
        if not paths["review"].is_file():
            raise ImportApplyError(
                f"event {event['id']}: the run has no {paths['review'].name} beside its "
                "report. Exclude the run in the plan or restore the file."
            )
        reviewed = self.reviewed_commit(rec, event)
        source = {"commit_sha": reviewed, "round": rnd.number, "pull_request": None}
        record = json.loads(
            _run_json(report, paths["report"], event["run"], source, paths["name"])
        )
        record["imported"] = True
        files = {
            f"{folder}/{REVIEW_FILE}": _lf(paths["review"].read_bytes()),
            f"{folder}/{RUN_FILE}": (
                json.dumps(record, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
            f"{folder}/{REPORT_FILE}": _lf(paths["report"].read_bytes()),
        }
        if paths["worklist"].is_file():
            files[f"{folder}/{WORKLIST_FILE}"] = _lf(paths["worklist"].read_bytes())
        if self.include_capture(event) and paths["results"].is_file():
            files[f"{folder}/{RESULTS_FILE}"] = _lf(paths["results"].read_bytes())
        return files

    # -- before any write ---------------------------------------------------------

    def problems_in_plan(self):
        """What the plan cannot be applied with, as strings; reads local files only."""
        problems = []
        plan = self.plan
        if plan.get("repo") not in (None, "", self.repo):
            problems.append(
                f"the plan names the repository {plan['repo']!r}, not {self.repo!r}"
            )
        if not self.rounds:
            problems.append("the plan has no included event")
        for rnd in self.rounds:
            for unit in rnd.units:
                for event in unit.events:
                    name = f"round {rnd.number} event {event['id']}"
                    if event["kind"] not in _WRITABLE:
                        problems.append(
                            f"{name} is a {event['kind']!r} document, which has no place "
                            "in an article's folder: set include to false"
                        )
                        continue
                    if event["kind"] == "run":
                        problems.extend(self._run_problems(name, event))
                        continue
                    try:
                        text = self.text_of(event)
                    except ImportApplyError as exc:
                        problems.append(str(exc))
                        continue
                    if event["kind"] == "close-out" and _direction(text) is None:
                        problems.append(
                            f"{name}: the close-out note has no usable Direction: line "
                            "(to-chat or to-code), so its file name cannot be chosen"
                        )
        return problems

    def _run_problems(self, name, event):
        problems = []
        paths = self.run_paths(event)
        if not isinstance(event.get("run"), int):
            problems.append(f"{name}: the run event has no run number")
        if not paths["report"].is_file():
            problems.append(f"{name}: the run's report is missing")
        elif not paths["review"].is_file():
            problems.append(
                f"{name}: the run has no {paths['review'].name} beside its report"
            )
        return problems

    def scan_for_secrets(self):
        """Refuse before any write if a file about to be sent holds a configured key."""
        if not self.secrets:
            return
        needles = [s.encode("utf-8") for s in self.secrets]
        for rnd in self.rounds:
            for unit in rnd.units:
                for event in unit.events:
                    if event["kind"] == "run":
                        paths = self.run_paths(event)
                        blobs = [
                            paths[k].read_bytes()
                            for k in ("report", "review", "worklist", "results")
                            if paths[k].is_file()
                        ]
                    else:
                        blobs = [self.text_of(event).encode("utf-8")]
                    if any(n in b for b in blobs for n in needles):
                        raise ImportApplyError(
                            f"round {rnd.number} event {event['id']} holds a configured "
                            "API key; nothing was sent. Find and remove it first."
                        )

    def check_repository(self):
        """The refusals that need GitHub: privacy, access, merge settings and live rounds."""
        client = self.client
        info = client.info()
        if not info.private:
            raise ImportApplyError(
                f"{self.repo} is a public repository. An import commits unpublished "
                "drafts, so it will not write to one and there is no override. Make "
                "it private first."
            )
        if not info.can_push:
            raise ImportApplyError(
                f"This token cannot push to {self.repo} (no write permission). Use a "
                "token with Contents, Pull requests and Issues read and write."
            )
        changes = di.settings_changes(info)
        if changes:
            raise ImportApplyError(
                f"{self.repo} is not set to merge commits only ({di.format_settings(changes)}). "
                "A squash or rebase merge would rewrite the imported history. Run "
                "`ci-docs init` to set it, then apply again."
            )
        self.base_branch = self.plan_branch or info.default_branch
        try:
            self.user = client.current_user()
        except GitHubRepoError as exc:
            raise ImportApplyError(
                f"cannot tell who this token is ({exc}), so commits cannot be "
                "attributed. Use a personal access token, not an app installation token."
            ) from exc

        slug = self.slug
        first_open = next(
            (
                r
                for r in self.rounds
                if "tag" not in self.applied.get(str(r.number), {})
            ),
            None,
        )
        own_branch = round_branch(slug, first_open.number) if first_open else None
        own_tag = round_tag(slug, first_open.number) if first_open else None
        recorded_branches = {
            r.get("branch") for r in self.applied.values() if isinstance(r, dict)
        }
        recorded_tags = {
            (r.get("tag") or {}).get("name")
            for r in self.applied.values()
            if isinstance(r, dict)
        }
        stray = []
        for ref, _ in client.matching_refs(f"heads/round/{slug}/"):
            name = ref.removeprefix("refs/heads/")
            if name not in recorded_branches and name != own_branch:
                stray.append(f"branch {name}")
        for ref, _ in client.matching_refs(f"tags/{slug}/"):
            name = ref.removeprefix("refs/tags/")
            if name not in recorded_tags and name != own_tag:
                stray.append(f"tag {name}")
        if stray:
            raise ImportApplyError(
                f"{self.repo} already has {', '.join(sorted(stray))} that the plan's "
                "applied block does not account for. The import starts an article's "
                "history and never interleaves with live rounds."
            )
        present = bool(client.list_dir(self.folder, self.base_branch))
        merged = any("merge" in r for r in self.applied.values() if isinstance(r, dict))
        if present and not self.applied:
            raise ImportApplyError(
                f"{self.repo} already has {self.folder}/ on {self.base_branch}. The "
                "import starts an article's history and will not add to one that exists."
            )
        if merged and not present:
            raise ImportApplyError(
                f"the plan records a merged round, but {self.folder}/ is not on "
                f"{self.base_branch}. Nothing is repeated over a record that no longer "
                "holds; find out what happened to the repository first."
            )

    # -- the run ------------------------------------------------------------------

    def run(self):
        problems = self.problems_in_plan()
        if problems:
            raise ImportApplyError(
                "the plan cannot be applied:\n  " + "\n  ".join(problems)
            )
        self.scan_for_secrets()
        known = {str(r.number) for r in self.rounds}
        for key in self.applied:
            if key not in known:
                raise ImportApplyError(
                    f"the plan's applied block records round {key}, which the plan no "
                    "longer has an included event for. Nothing is repeated over a "
                    "record that does not match; restore the round or ask for help."
                )
        self.check_repository()
        total = len(self.rounds)
        mode = "Dry run: " if self.dry else ""
        self.say(
            f"{mode}importing {total} round(s) of {self.slug} into {self.repo} "
            f"({self.base_branch})"
        )
        for rnd in self.rounds:
            self.apply_round(rnd)
        done = sum(len(r.get("commits", [])) for r in self.applied.values())
        if self.dry:
            self.say("Dry run: nothing was written.")
        else:
            self.say(
                f"Done: {total} round(s), {done} commit(s). Every round is merged and tagged."
            )

    def apply_round(self, rnd):
        n = rnd.number
        rec = self.applied.setdefault(str(n), {})
        self.say(
            f"Round {n}: {len(rnd.units)} commit(s), {_iso(rnd.first)} to {_iso(rnd.accepted)}"
        )
        self.verify_units(rnd, rec)
        self.ensure_branch(rnd, rec)
        self.commit_units(rnd, rec)
        self.ensure_pull(rnd, rec)
        self.ensure_reviews(rnd, rec)
        self.ensure_merge(rnd, rec)
        self.ensure_tag(rnd, rec)

    def verify_units(self, rnd, rec):
        """A recorded commit must be for the unit the plan now has at that place."""
        for index, entry in enumerate(rec.get("commits", [])):
            if index >= len(rnd.units) or entry.get("events") != rnd.units[index].ids:
                raise ImportApplyError(
                    f"round {rnd.number}: the plan has changed since commit {index + 1} "
                    "was written (its events no longer match the record). A recorded "
                    "step is never redone; restore the plan or start a new one."
                )

    # -- branch ---------------------------------------------------------------------

    def ensure_branch(self, rnd, rec):
        name = round_branch(self.slug, rnd.number)
        client = self.client
        if rec.get("branch"):
            if rec["branch"] != name:
                raise ImportApplyError(
                    f"round {rnd.number}: the record names branch {rec['branch']!r}, "
                    f"not {name!r}"
                )
            if self._recorded_is_real(rec) and not client.has_branch(name):
                raise ImportApplyError(
                    f"round {rnd.number}: branch {name} is recorded but not in "
                    f"{self.repo}. Nothing was redone."
                )
            return
        if self.dry and self._carry is not None:
            base = "<previous merge>"
        else:
            base = client.resolve(self.base_branch)
        if not self.dry and client.has_branch(name):
            self._adopt_branch(rnd, rec, name)
            return
        if self.dry:
            self.would(
                f"create branch {name} at {_short(base) if len(base) == 40 else base}"
            )
            rec.update(branch=name, base=base, commits=[], _dry=True)
            return
        client.create_ref(f"refs/heads/{name}", base)
        rec.update(branch=name, base=base, commits=[])
        self.save()
        self.say(f"  created branch {name} at {_short(base)}")

    def _recorded_is_real(self, rec):
        return not rec.get("_dry")

    def _adopt_branch(self, rnd, rec, name):
        """A branch the plan does not record: this import's own, or a refusal."""
        client = self.client
        tip = client.resolve(name)
        chain, base = self._walk(tip, None, len(rnd.units))
        main = client.resolve(self.base_branch)
        if not chain:
            if tip != main:
                raise ImportApplyError(
                    f"branch {name} exists but is not at {self.base_branch}'s tip and "
                    "holds no import commit. It was not made by this import; nothing "
                    "was written."
                )
        else:
            if client.compare(base, self.base_branch).status not in (
                "ahead",
                "identical",
            ):
                raise ImportApplyError(
                    f"branch {name} does not start from {self.base_branch}'s history; "
                    "nothing was written."
                )
        rec.update(branch=name, base=base, commits=[])
        self._adopt_commits(rnd, rec, chain)
        self.save()
        self.say(f"  adopted branch {name} with {len(chain)} commit(s) already on it")

    def _walk(self, tip, stop_at, limit):
        """``(chain, base)``: this import's commits from ``tip`` back, oldest first, and the commit under them."""
        chain = []
        current = tip
        for _ in range(limit + 1):
            if stop_at and current == stop_at:
                return list(reversed(chain)), current
            commit = self.client.get_commit(current)
            ids = _EVENT_TRAILER.findall(commit.message)
            if not ids:
                if stop_at:
                    raise ImportApplyError(
                        f"the branch does not contain the recorded commit "
                        f"{_short(stop_at)}; it moved by something other than this import"
                    )
                return list(reversed(chain)), current
            chain.append((current, ids))
            current = commit.parents[0]
        raise ImportApplyError(
            "the branch holds more import commits than the plan has events for"
        )

    def _adopt_commits(self, rnd, rec, chain):
        start = len(rec["commits"])
        for offset, (sha, ids) in enumerate(chain):
            if start + offset >= len(rnd.units) or rnd.units[start + offset].ids != ids:
                raise ImportApplyError(
                    f"round {rnd.number}: a commit on the branch ({_short(sha)}) is for "
                    f"{', '.join(ids)}, which is not the next event in the plan. Nothing "
                    "was written."
                )
            unit = rnd.units[start + offset]
            rec["commits"].append(
                {"events": ids, "sha": sha, "at": _iso(unit.at), "adopted": True}
            )

    # -- commits ----------------------------------------------------------------------

    def commit_units(self, rnd, rec):
        client = self.client
        branch = rec["branch"]
        real = self._recorded_is_real(rec) and not self.dry
        if real or (self.dry and self._recorded_is_real(rec)):
            anchor = rec["commits"][-1]["sha"] if rec["commits"] else rec["base"]
            tip = client.resolve(branch)
            if tip != anchor:
                chain, _ = self._walk(tip, anchor, len(rnd.units))
                self._adopt_commits(rnd, rec, chain)
                if not self.dry:
                    self.save()
        done = len(rec["commits"])
        if done == len(rnd.units):
            return
        if self.dry and self._carry is not None and rec.get("_dry"):
            state = dict(self._carry)
        else:
            anchor = rec["commits"][-1]["sha"] if rec["commits"] else rec["base"]
            state = self._read_state(anchor)
        for unit in rnd.units[done:]:
            files, deletes = self.unit_files(rnd, rec, unit, state)
            names = [p[len(self.folder) + 1 :] for p in files]
            message = self.message(rnd, unit, names)
            first = message.split("\n", 1)[0]
            if self.dry:
                shown = ", ".join(names) + "".join(
                    f"; delete {p[len(self.folder) + 1 :]}" for p in deletes
                )
                self.would(f"commit {first!r} dated {_iso(unit.at)}: {shown}")
                sha = f"dry-{len(rec['commits']) + 1}"
                entry = {"events": unit.ids, "sha": sha, "at": _iso(unit.at)}
                if unit.kind == "review":
                    entry["reviewed"] = self.reviewed_commit(rec, unit.run)
                rec["commits"].append(entry)
                self._apply_to_state(state, files, deletes)
                continue
            replace = {p: state[p] for p in files if p in state}
            signature = self.signature(unit.at)
            # Build on the commit this import made last rather than on a fresh read of
            # the branch: github.com has answered that read with the head from before
            # the import's own previous commit (read-after-write lag, undocumented),
            # which the replace check then rightly refuses as "changed by someone else".
            # If someone really did move the branch, the update is refused as not a
            # fast forward and commit_files re-reads, so a real change still refuses.
            parent = rec["commits"][-1]["sha"] if rec["commits"] else rec["base"]
            result = client.commit_files(
                branch,
                files,
                message,
                replace=replace,
                deletes=deletes,
                author=signature,
                committer=signature,
                refuse_if_contains=self.secrets,
                parent=parent,
            )
            entry = {"events": unit.ids, "sha": result.commit_sha, "at": _iso(unit.at)}
            if unit.kind == "review":
                entry["reviewed"] = self.reviewed_commit(rec, unit.run)
            if not result.written and not result.deleted:
                entry["unchanged"] = True
                self.say(f"  nothing new to write for {first}; no commit made")
            else:
                self.say(f"  {_short(result.commit_sha)} {first}")
            rec["commits"].append(entry)
            self._apply_to_state(state, files, deletes)
            self.save()
        self._carry = state
        self._carry_round = branch

    def _apply_to_state(self, state, files, deletes):
        for path, data in files.items():
            state[path] = git_blob_sha(data)
        for path in deletes:
            state.pop(path, None)

    def _read_state(self, ref):
        state = {}
        for directory in (self.folder, f"{self.folder}/pipeline"):
            for entry in self.client.list_dir(directory, ref):
                if entry.type == "file":
                    state[entry.path] = entry.sha
        return state

    # -- the pull request ----------------------------------------------------------

    def pull_body(self, rnd, rec):
        rows = []
        for unit, entry in zip(rnd.units, rec["commits"]):
            what = {
                "draft": "draft",
                "revise": "revision",
                "review": f"run {unit.run['run']}" if unit.run else "run",
                "close-out": "close-out note",
                "publication": "publication",
            }[unit.kind]
            kinds = "+".join(dict.fromkeys(e["kind"] for e in unit.events))
            flags = []
            if any(e.get("recovered") for e in unit.events):
                flags.append("recovered")
            if any(e.get("corroborated") is False for e in unit.events):
                flags.append("chat only")
            note = f" ({', '.join(flags)})" if flags else ""
            sha = "`" + _short(entry["sha"]) + "`" if entry.get("sha") else ""
            files = f" ({kinds})" if unit.kind != "review" else ""
            rows.append(f"| {_iso(unit.at)} | {what}{files}{note} | {sha} |")
        lines = [
            f"Imported history: round {rnd.number} of {self.slug}, "
            f"{_iso(rnd.first)} to {_iso(rnd.accepted)}.",
            "",
            "GitHub sets a pull request's own timestamps to the time of the import and "
            "does not let anyone change them. Only the commits carry the original dates.",
            "",
            "| When (UTC) | Event | Commit |",
            "|---|---|---|",
            *rows,
            "",
        ]
        return "\n".join(lines)

    def ensure_pull(self, rnd, rec):
        client = self.client
        branch = rec["branch"]
        if rec.get("pull_request") and not rec.get("_dry"):
            try:
                pull = client.get_pull(rec["pull_request"])
            except GitHubRepoError as exc:
                raise ImportApplyError(
                    f"round {rnd.number}: pull request #{rec['pull_request']} is "
                    f"recorded but cannot be read ({exc}). Nothing was redone."
                ) from exc
            if pull.head_ref != branch:
                raise ImportApplyError(
                    f"round {rnd.number}: pull request #{pull.number} is from "
                    f"{pull.head_ref!r}, not {branch!r}. Nothing was redone."
                )
            self.ensure_labels(rnd, rec)  # the step after the pull request's record
            return
        title = f"{self.slug} r{rnd.number}"
        body = self.pull_body(rnd, rec)
        _refuse_secrets(body, self.secrets)
        if self.dry:
            self.would(
                f"open pull request {title!r} from {branch} into {self.base_branch}"
            )
            rec["pull_request"] = 0
            rec["_dry"] = True
            return
        existing = next(
            (
                p
                for p in client.list_pulls("all")
                if p.head_ref == branch and p.head_repo.lower() == self.repo.lower()
            ),
            None,
        )
        if existing is not None:
            pull = existing
            self.say(f"  adopted pull request #{pull.number}")
        else:
            pull = client.create_pull(branch, self.base_branch, title, body)
            self.say(f"  opened pull request #{pull.number}: {pull.html_url}")
        rec["pull_request"] = pull.number
        rec["pull_url"] = pull.html_url
        self.save()
        self.ensure_labels(rnd, rec)

    def ensure_labels(self, rnd, rec):
        names = ["round", f"article:{self.slug}"]
        if rec.get("labels") == names:
            return
        if self.dry:
            self.would(f"label the pull request {', '.join(names)}")
            return
        round_label = next(label for label in di.LABELS if label.name == "round")
        self.client.ensure_label(
            round_label.name, round_label.color, round_label.description
        )
        self.client.ensure_label(
            names[1], "c5def5", f"Pull requests and issues about {self.slug}"[:100]
        )
        self.client.add_labels(rec["pull_request"], names)
        rec["labels"] = names
        self.save()

    # -- reviews ----------------------------------------------------------------------

    def ensure_reviews(self, rnd, rec):
        runs = rnd.runs
        if not runs:
            return
        number = rec["pull_request"]
        existing = (
            [] if (self.dry and rec.get("_dry")) else self.client.list_reviews(number)
        )
        have = rec.setdefault("reviews", [])
        for unit in runs:
            event = unit.run
            recorded = next((r for r in have if r["event"] == event["id"]), None)
            commit_entry = next(c for c in rec["commits"] if event["id"] in c["events"])
            reviewed = commit_entry.get("reviewed") or self.reviewed_commit(rec, event)
            # A review must be of a commit that is in the pull request. A run that is
            # the first thing in its round reviewed something on the base branch; it
            # is posted against its own commit instead.
            commit_id = commit_entry["sha"] if reviewed == rec["base"] else reviewed
            if recorded:
                if existing and recorded["id"] not in {r.id for r in existing}:
                    raise ImportApplyError(
                        f"round {rnd.number}: the review of run {event['run']} is "
                        f"recorded (id {recorded['id']}) but not on pull request "
                        f"#{number}. Nothing was redone."
                    )
                continue
            found = next(
                (r for r in existing if is_review_of(r.body, event["run"], commit_id)),
                None,
            )
            if found is not None:
                have.append(self._review_record(event, found, commit_id))
                self.say(f"  adopted the review of run {event['run']}")
                self.save()
                continue
            body = self.review_body(rnd, rec, unit, commit_id, commit_entry["sha"])
            if self.dry:
                self.would(
                    f"post the review of run {event['run']} on pull request "
                    f"#{number or '?'} (commit {_short(commit_id)}), no line comments"
                )
                have.append({"event": event["id"], "id": 0, "run": event["run"]})
                continue
            review = self.client.create_review(
                number, commit_id=commit_id, body=body, event="COMMENT"
            )
            have.append(self._review_record(event, review, commit_id))
            self.say(f"  posted the review of run {event['run']}: {review.html_url}")
            self.save()

    def _review_record(self, event, review, commit_id):
        return {
            "event": event["id"],
            "id": review.id,
            "run": event["run"],
            "commit": commit_id,
            "url": review.html_url,
        }

    def review_body(self, rnd, rec, unit, commit_id, review_commit):
        event = unit.run
        paths = self.run_paths(event)
        report = json.loads(paths["report"].read_text(encoding="utf-8-sig"))
        cost = (report.get("cost_summary") or {}).get("total_usd")
        present = [
            name
            for name in (DRAFT_FILE, METADATA_FILE, HANDOFF_FILE)
            if self._has_file(rec, name)
        ]
        facts = RunFacts(
            slug=self.slug,
            run_number=event["run"],
            run_name=paths["name"],
            reviewed_commit=commit_id,
            pull_number=rec["pull_request"],
            repo=self.repo,
            review_path=f"{self.folder}/{REVIEW_FILE}",
            review_commit=review_commit,
            round_number=rnd.number,
            preset=(report.get("ensemble") or {}).get("cost_preset") or "unknown",
            cost_usd=cost if isinstance(cost, (int, float)) else None,
            files_read=present,
        )
        body = build_review_body(report, facts, limit=MAX_BODY)
        _refuse_secrets(body, self.secrets)
        return body

    def _has_file(self, rec, name):
        """Whether the folder holds ``name`` as the round's commits left it."""
        if self._carry is None or self._carry_round != rec["branch"]:
            anchor = rec["commits"][-1]["sha"]
            self._carry = self._read_state(anchor)
            self._carry_round = rec["branch"]
        return f"{self.folder}/{name}" in self._carry

    # -- merge ------------------------------------------------------------------------

    def ensure_merge(self, rnd, rec):
        if rec.get("merge") and not rec.get("_dry"):
            pull = self.client.get_pull(rec["pull_request"])
            if not pull.merged:
                raise ImportApplyError(
                    f"round {rnd.number}: the merge is recorded but pull request "
                    f"#{pull.number} is not merged. Nothing was redone."
                )
            return
        client = self.client
        number = rec["pull_request"]
        tip = rec["commits"][-1]["sha"]
        message = f"{self.slug} r{rnd.number} (#{number or '?'})"
        if self.dry:
            self.would(
                f"merge: move {self.base_branch} to a merge commit {message!r} dated "
                f"{_iso(rnd.accepted)}"
            )
            rec["merge"] = {"sha": "<merge>", "path": "ref-move"}
            return
        pull = client.get_pull(number)
        path = "ref-move"
        merge_sha = None
        if not pull.merged:
            contained = client.compare(tip, self.base_branch).status in (
                "ahead",
                "identical",
            )
            signature = self.signature(rnd.accepted)
            if not contained:
                # The branch was cut from the base branch's head and this import is
                # its only writer, so that head is rec["base"]; building on it avoids
                # a ref read that may lag the previous round's merge (see commit_units).
                result = client.merge_commit(
                    self.base_branch,
                    tip,
                    message,
                    author=signature,
                    committer=signature,
                    parent=self._merge_parent(rec),
                )
                merge_sha = result.commit_sha
                self.say(
                    f"  moved {self.base_branch} to merge commit {_short(merge_sha)} "
                    f"dated {_iso(rnd.accepted)}"
                )
            # Here the base branch holds the round's head (it did already, or the
            # move just above put it there), and GitHub marks a pull request merged
            # on its own once that is so, but only after a lag that was measured at
            # about 90 seconds. Asking the API to merge it meanwhile is wrong: it
            # refuses ("not mergeable", the pull request still reads CONFLICTING)
            # and the only correct action is to wait.
            pull = self._wait_for_merged(number)
            if not pull.merged:
                held = (
                    "already holds the round"
                    if contained
                    else f"was moved to {_short(merge_sha)}, which holds the round"
                )
                raise ImportApplyError(
                    f"round {rnd.number}: {self.base_branch} {held}, but GitHub has "
                    f"not marked pull request #{number} merged yet (waited "
                    f"{sum(self.merge_waits)} seconds). GitHub catches up on its "
                    "own; run the same command again later and it resumes from here."
                )
        rec["merge"] = {
            "sha": pull.merge_commit_sha or merge_sha,
            "path": path,
            "merged_at": pull.merged_at,
            "accepted_at": _iso(rnd.accepted),
        }
        self.save()
        self.say(
            f"  pull request #{number} merged (merge commit {_short(rec['merge']['sha'])})"
        )

    def _merge_parent(self, rec):
        """The base branch's head as this import last left it, or None if unknown.

        Round N's branch is cut from the base branch right after round N-1's
        merge, so for a branch cut by this run that is ``rec["base"]``. A branch
        adopted from an earlier run may have been cut from an older head; then
        the ordinary read decides, and a merge onto the wrong parent cannot slip
        through (the update is refused as not a fast forward).
        """
        base = rec.get("base")
        return base if isinstance(base, str) and len(base) == 40 else None

    def _wait_for_merged(self, number):
        pull = self.client.get_pull(number)
        waited = 0
        next_report = 0
        for delay in self.merge_waits:
            if pull.merged:
                break
            if waited >= next_report:
                self.say(
                    f"  waiting for GitHub to mark pull request #{number} merged "
                    f"({waited}s so far; it can take a couple of minutes)"
                )
                next_report = waited + _MERGE_PROGRESS_EVERY
            self.sleep(delay)
            waited += delay
            pull = self.client.get_pull(number)
        return pull

    # -- tag --------------------------------------------------------------------------

    def ensure_tag(self, rnd, rec):
        name = round_tag(self.slug, rnd.number)
        if rec.get("tag") and not rec.get("_dry"):
            found = self.client.get_tag(name)
            if found is None or found.commit_sha != rec["merge"]["sha"]:
                raise ImportApplyError(
                    f"round {rnd.number}: tag {name} is recorded but "
                    + (
                        "is not in the repository"
                        if found is None
                        else "points elsewhere"
                    )
                    + ". A tag is never moved or recreated; nothing was redone."
                )
            return
        if self.dry:
            self.would(
                f"tag {name} (annotated) on the merge commit, dated {_iso(rnd.accepted)}"
            )
            rec["tag"] = {"name": name}
            return
        results = tag_merged_rounds(
            self.client,
            self.slug,
            repo=self.repo,
            imported={rnd.number: rnd.accepted},
        )
        mine = [r for r in results if r.round == rnd.number]
        if mine and mine[0].status not in (CREATED, PRESENT):
            raise ImportApplyError(
                f"round {rnd.number}: {mine[0].tag} not tagged: {mine[0].detail}. "
                "Run the same command again once that is fixed."
            )
        found = self.client.get_tag(name)
        if found is None or found.commit_sha != rec["merge"]["sha"]:
            raise ImportApplyError(
                f"round {rnd.number}: tag {name} is "
                + (
                    "missing"
                    if found is None
                    else f"on {_short(found.commit_sha)}, not on the merge commit"
                )
                + ". It was left alone; a tag is never moved."
            )
        rec["tag"] = {
            "name": name,
            "commit": found.commit_sha,
            "tag_sha": found.tag_sha,
            "tagged_at": found.tagged_at,
            "message": found.message,
        }
        self.save()
        self.say(f"  tagged {name} at {_short(found.commit_sha)} ({found.tagged_at})")


def apply_plan(
    plan_path,
    *,
    repo,
    client,
    settings,
    api_keys=None,
    dry_run=False,
    out=print,
    now=None,
    sleep=time.sleep,
    merge_waits=_MERGE_WAITS,
):
    """Apply the plan at ``plan_path`` to ``repo`` through ``client``; returns the plan.

    ``settings`` is the normalised ``github_documents`` block (``path_prefix`` and
    ``branch`` are read); ``api_keys`` is the merged key table, none of whose
    values may appear in what is sent. The plan file is rewritten after every
    step, so run the same command again after any failure. ``dry_run`` reads from
    GitHub and prints every write it would make; it changes nothing, not even the
    plan file.
    """
    hi.refuse_out_inside_checkout(plan_path)
    try:
        with open(plan_path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ImportApplyError(f"cannot read the plan {plan_path}: {exc}") from exc
    problems = hi.check_plan(plan)
    if problems:
        raise ImportApplyError(
            "ci-docs import check rejects the plan:\n  " + "\n  ".join(problems)
        )
    validate_slug(plan.get("slug"))
    if not isinstance(repo, str) or not repo.strip():
        raise ImportApplyError(
            "--repo is required: the import never defaults to a repository"
        )
    from .github_writeback import secret_values

    applier = _Applier(
        plan,
        plan_path,
        client,
        repo=repo.strip(),
        settings=settings,
        secrets=secret_values(api_keys or {}),
        dry_run=dry_run,
        out=out,
        now=now or (lambda: datetime.now(timezone.utc)),
        sleep=sleep,
        merge_waits=merge_waits,
    )
    applier.plan_branch = (settings.get("branch") or "").strip() or None
    try:
        applier.run()
    except GitHubRepoError as exc:
        if exc.kind == "rate_limited" and exc.reset_at:
            when = time.strftime("%H:%M:%S", time.localtime(exc.reset_at))
            exc = f"{exc} The limit lifts at {when}."
        raise ImportApplyError(
            f"{exc}\nWhat was written is recorded in the plan; run the same command "
            "again to resume."
        ) from None
    return plan
