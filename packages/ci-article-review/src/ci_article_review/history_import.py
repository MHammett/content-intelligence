"""Import an article's local history into an editable plan: ``ci-docs import scan``.

Step 1 of the history import in docs/GITHUB-DOCUMENTS.md ("Importing an
article's history", Decision 7). The repository is meant to hold an article's
complete history, including rounds and edits made before it existed, and that
history is scattered over private sources with inconsistent names. No rule can
sort it unattended, so the import is scan, then edit, then apply:

* ``scan`` (here) reads the sources, de-duplicates by content, orders by the best
  timestamp, groups into rounds, prints a timeline and writes ``plan.json``;
* the author edits the plan (``include: false``, move an event, split or join
  rounds, set ``accepted_at``);
* ``check`` (here) validates the edited plan and prints the timeline again;
* ``apply`` (issue #421) writes the rounds to the document repository.

This module reads local files and writes one local file, the plan. It makes no
network call and no model call, and nothing it does reaches GitHub.

The sources, in the order they are trusted for a version's *time*:

==============  ==================================================  =========
source          what it gives                                        time
==============  ==================================================  =========
saved runs      a run, and the draft it reviewed                     ``generated``
(claude.ai      in-chat versions                                     message time; issue #422, not built)
export)
``--drive-dir`` files exactly as submitted                           Drive ``createdTime``
``--files``     drafts, metadata, handoffs, close-out, changes       file mtime
``--recovered`` versions a chat *reproduced from its context*        the sidecar's ``claimed_at`` only
==============  ==================================================  =========

A reproduction is not the original bytes and may silently differ, so it is the
least trusted source for content and for time: it is never dated from its file's
mtime, it is dropped in favour of any other source holding the same version, and
it is flagged ``recovered: true`` in the plan when nothing else does.

Privacy. The sources are unpublished drafts. The plan holds paths, hashes, times
and counts, never a draft's text, and ``--out`` inside this checkout is refused.
Nothing here prints a file's contents.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import handoff_parser
from .passage_match import same_passage, tokenise

__all__ = [
    "ImportScanError",
    "scan",
    "write_plan",
    "check_plan",
    "format_timeline",
    "default_history_root",
    "checkout_roots",
]

PLAN_VERSION = 1

#: Built from its code point, not typed: this repository bans the invisible
#: character itself in source (test_markers.py).
BYTE_ORDER_MARK = chr(0xFEFF)

#: Directories under a history root that are never read: experiments, replays
#: and nested runs are not the article's own history.
_NEVER_READ = ("_experiments", "_replay", "_nested")

#: Lower is more trusted. Used to choose between source times that are close.
_TRUST = {"run": 0, "drive": 1, "file": 2, "recovered": 3}

#: Two timestamps this close are the same moment for trust purposes: the version's
#: time is then the more trusted source's, not merely the earlier one's.
_CLOSE = timedelta(minutes=5)

#: Runs on one draft this close together are probably one person tuning a setting.
_EXPERIMENT_WINDOW = timedelta(minutes=30)

#: How like a reproduction a version must be (Jaccard over content tokens) before
#: its label is allowed to supersede the reproduction. A label shared by several
#: versions (every run numbered 1) must not swallow a reproduction of another.
_MIN_CLOSENESS = 0.6

_DOCUMENT_SUFFIXES = (".md", ".markdown")

_REPORT_RE = re.compile(r"^run_(?P<run>\d+)_((?P<ts>\d{8}_\d{6})_)?report\.json$")
_LABEL_RE = re.compile(r"(?<![a-z0-9])([vr])(\d+)(?![a-z0-9])")

#: Name patterns, tried in this order (a close-out note's name carries ``r3``, a
#: handoff's carries ``draft``). Matched case-insensitively after the slug is
#: removed from the name, so a slug that contains one of these words cannot
#: misclassify its own files.
_NAME_KINDS = (
    ("close-out", re.compile(r"(?<![a-z0-9])close-?out(?![a-z0-9])")),
    ("publication", re.compile(r"(?<![a-z0-9])publication(?![a-z0-9])")),
    ("handoff", re.compile(r"(?<![a-z0-9])handoff(?![a-z0-9])")),
    ("metadata", re.compile(r"(?<![a-z0-9])metadata(?![a-z0-9])")),
    ("changes", re.compile(r"(?<![a-z0-9])changes?(?![a-z0-9])")),
    ("review", re.compile(r"(?<![a-z0-9])review(?![a-z0-9])")),
    ("draft", re.compile(r"(?<![a-z0-9])draft(?![a-z0-9])")),
)

#: Order of kinds at one instant, so a draft precedes the handoff that carries it.
_KIND_ORDER = {
    "draft": 0,
    "metadata": 1,
    "handoff": 2,
    "run": 3,
    "publication": 4,
    "changes": 5,
    "close-out": 6,
    "review": 7,
    "unknown": 8,
}

_CHAT_NOT_BUILT = (
    "reading a claude.ai export is not built yet (issue #422); run the scan "
    "without --chat-export and --conversation"
)


class ImportScanError(Exception):
    """A scan that must stop, with a message the author can act on."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value, what):
    """An aware UTC datetime from an ISO-8601 string (``Z`` or an offset)."""
    if not isinstance(value, str) or not value.strip():
        raise ImportScanError(f"{what}: no time given")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ImportScanError(f"{what}: {value!r} is not an ISO-8601 time") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _mtime(path):
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.replace(microsecond=0)


def normalise(text):
    """The text a version is hashed from.

    CRLF becomes LF, trailing whitespace is stripped from every line, and blank
    lines around the whole text are dropped (a file that ends in a newline and the
    same text without one are one version).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.removeprefix(BYTE_ORDER_MARK)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path):
    return Path(path).read_bytes().decode("utf-8-sig")


def _posix(path):
    return Path(path).resolve().as_posix()


def _term_pattern(terms):
    alternatives = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")


def _has_header(text, header):
    return any(line.strip() == header for line in text.split("\n"))


def _embedded_draft(text):
    """The DRAFT section of a Template A handoff, or ``None``.

    Template A is recognised by ``handoff_parser``'s own banner. The parser logs a
    warning for every missing field, which is noise for a scan of old files, so its
    logger is silenced for the call.
    """
    if not _has_header(text, handoff_parser.DRAFT_HEADERS[0]):
        return None
    logger = logging.getLogger(handoff_parser.__name__)
    previous = logger.disabled
    logger.disabled = True
    try:
        draft = handoff_parser.parse_draft_submission(text).get("draft") or ""
    finally:
        logger.disabled = previous
    return draft or None


def _kind_by_content(text):
    if _has_header(text, handoff_parser.DRAFT_HEADERS[0]):
        return "handoff"
    if _has_header(text, handoff_parser.PUB_HEADERS[0]):
        return "publication"
    if _has_header(text, "PRIMARY CLAIM") and not _has_header(text, "DRAFT"):
        return "metadata"
    return "unknown"


def undo_lt_corrections(corrected, changes):
    """The draft as submitted, from a run's ``corrected_draft`` and its change log.

    ``changes`` is ``lt_corrections_applied``: each entry has the ``original`` text,
    its ``replacement`` and the ``offset`` in the *original* text. Walking them in
    order of offset, each replacement sits at its offset plus what the earlier
    changes added or removed. Returns ``None`` when a replacement is not where the
    log says, because a guess here would put wrong bytes in a history.
    """
    try:
        ordered = sorted(changes, key=lambda c: c["offset"])
        shift = 0
        spans = []
        for change in ordered:
            replacement, original = change["replacement"], change["original"]
            start = change["offset"] + shift
            if corrected[start : start + len(replacement)] != replacement:
                return None
            spans.append((start, len(replacement), original))
            shift += len(replacement) - len(original)
    except (KeyError, TypeError):
        return None
    text = corrected
    for start, length, original in reversed(spans):
        text = text[:start] + original + text[start + length :]
    return text


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------


def checkout_roots():
    """The content-intelligence checkout this code runs from, and its main checkout.

    A worktree's ``.git`` is a file pointing into the main checkout's ``.git``;
    both roots are returned so a plan cannot be written into either. An empty list
    means the code is not running from a checkout (an installed package).
    """
    here = Path(__file__).resolve()
    for root in here.parents:
        marker = root / ".git"
        if marker.is_dir():
            return [root]
        if marker.is_file():
            roots = [root]
            try:
                pointer = marker.read_text(encoding="utf-8").strip()
            except OSError:
                return roots
            if pointer.startswith("gitdir:"):
                gitdir = Path(pointer[len("gitdir:") :].strip())
                if not gitdir.is_absolute():
                    gitdir = (root / gitdir).resolve()
                # <main>/.git/worktrees/<name>
                if gitdir.parent.name == "worktrees":
                    main = gitdir.parent.parent.parent
                    if main.is_dir() and main != root:
                        roots.append(main)
            return roots
    return []


def default_history_root(environ=None):
    """``(path, why)``: ``$CI_HISTORY_ROOT``, else the main checkout's ``pipeline_history``.

    The main checkout is found from this code's location, not the working
    directory, so the answer does not change with where the command is typed.
    """
    environ = os.environ if environ is None else environ
    value = (environ.get("CI_HISTORY_ROOT") or "").strip()
    if value:
        return Path(value), "$CI_HISTORY_ROOT"
    roots = checkout_roots()
    if roots:
        return roots[-1] / "pipeline_history", "the main checkout"
    return Path.cwd() / "pipeline_history", "the working directory (no checkout found)"


def refuse_out_inside_checkout(out_path):
    target = Path(out_path).resolve()
    for root in checkout_roots():
        resolved = root.resolve()
        if target == resolved or resolved in target.parents:
            raise ImportScanError(
                f"--out {out_path} is inside the content-intelligence checkout "
                f"({resolved.as_posix()}). The plan names private files and must "
                "be written outside any git repository."
            )


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass
class Holder:
    """One place a draft text was seen."""

    source: str  # run | drive | file | recovered
    path: str
    time: object  # aware datetime, or None
    label: object = None
    part: object = None  # "draft" (inside a handoff) | "corrected_draft"
    lt_undone: bool = False
    run: object = None
    claimed: bool = False

    def record(self):
        entry = {
            "type": self.source,
            "path": self.path,
            "time": _iso(self.time) if self.time else None,
        }
        if self.part:
            entry["part"] = self.part
        if self.lt_undone:
            entry["lt_undone"] = True
        if self.label:
            entry["label"] = self.label
        return entry


@dataclass
class Event:
    kind: str
    sha: object = None
    text: object = None
    holders: list = field(default_factory=list)
    include: bool = True
    note: object = None
    recovered: bool = False
    superseded_by: object = None
    submitted: bool = False
    hint: object = None
    placement: object = None
    # run events
    run_data: dict = field(default_factory=dict)
    version: object = None  # the draft Event a run reviewed
    at: object = None
    id: object = None
    attached_to: object = None

    @property
    def label(self):
        labelled = sorted(
            (h for h in self.holders if h.label),
            key=lambda h: (
                _TRUST[h.source],
                h.time or datetime.max.replace(tzinfo=timezone.utc),
            ),
        )
        return labelled[0].label if labelled else None

    def other_labels(self):
        found = {h.label for h in self.holders if h.label}
        found.discard(self.label)
        return sorted(found)

    def match_labels(self):
        """Labels a reproduction may claim this version by.

        A run carries no label, but its ``Pipeline run:`` number is the author's
        count of the round, so ``r<N>`` stands in when nothing better is known.
        """
        labels = {h.label for h in self.holders if h.label}
        if not labels:
            labels = {f"r{h.run}" for h in self.holders if h.run is not None}
        return labels


def _file_kind_and_label(name, terms, text, folder_label=None):
    """Kind and version label of a document, by name first and content second."""
    stem = Path(name).stem.lower()
    stripped = _term_pattern(terms).sub(" ", stem)
    kind = "unknown"
    for candidate, pattern in _NAME_KINDS:
        if pattern.search(stripped):
            kind = candidate
            break
    if kind == "unknown":
        kind = _kind_by_content(text)
    found = _LABEL_RE.search(stripped)
    label = f"{found.group(1)}{int(found.group(2))}" if found else folder_label
    return kind, label


def _not_a_document_reason(path):
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "not a document: script"
    if suffix == ".txt":
        return "not a document: text file (a prompt or posting brief)"
    return f"not a document: {suffix or 'no extension'} file"


@dataclass
class _Found:
    """A document file read from disk, before it becomes an event."""

    source: str
    path: Path
    kind: str
    label: object
    time: object
    text: str  # normalised
    embedded: object  # normalised embedded draft, or None
    recovered: bool = False
    claimed: bool = False


def _read_document(path, source, terms, time, folder_label=None):
    raw = _read_text(path)
    kind, label = _file_kind_and_label(path.name, terms, raw, folder_label)
    embedded = None
    if kind in ("handoff",):
        draft = _embedded_draft(raw)
        embedded = normalise(draft) if draft else None
    return _Found(
        source=source,
        path=path,
        kind=kind,
        label=label,
        time=time,
        text=normalise(raw),
        embedded=embedded,
    )


class _Collector:
    """Everything the sources yield, plus what was left out and why."""

    def __init__(self):
        self.found = []  # _Found
        self.runs = []  # dicts, one per report
        self.excluded = []  # {"path", "why"}
        self.warnings = []
        self.skipped_by_name = 0
        self.seen_paths = set()


def _scan_files(collector, directories, terms, include_names):
    pattern = _term_pattern(terms)
    wanted = {n.lower() for n in include_names}

    def consider(path, folder_label, whole_folder):
        resolved = path.resolve()
        if resolved in collector.seen_paths:
            return False
        name = path.name.lower()
        if not (whole_folder or pattern.search(name) or name in wanted):
            collector.skipped_by_name += 1
            return False
        collector.seen_paths.add(resolved)
        if path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            collector.excluded.append(
                {"path": _posix(path), "why": _not_a_document_reason(path)}
            )
            return False
        try:
            collector.found.append(
                _read_document(path, "file", terms, _mtime(path), folder_label)
            )
        except (OSError, UnicodeDecodeError) as exc:
            collector.excluded.append(
                {"path": _posix(path), "why": f"unreadable: {exc.__class__.__name__}"}
            )
            return False
        return True

    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            raise ImportScanError(f"--files {directory} is not a directory")
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file():
                consider(entry, None, False)
            elif entry.is_dir():
                matched = bool(pattern.search(entry.name.lower()))
                folder_label = None
                if matched:
                    stripped = pattern.sub(" ", entry.name.lower())
                    found = _LABEL_RE.search(stripped)
                    if found:
                        folder_label = f"{found.group(1)}{int(found.group(2))}"
                documents = 0
                first_excluded = len(collector.excluded)
                inside = sorted(
                    (p for p in entry.iterdir() if p.is_file()),
                    key=lambda p: p.name.lower(),
                )
                for child in inside:
                    if consider(child, folder_label, matched):
                        documents += 1
                left_out = len(collector.excluded) - first_excluded
                if matched and documents == 0 and left_out:
                    # A folder of images or scripts: say so once, not per file.
                    del collector.excluded[first_excluded:]
                    collector.excluded.append(
                        {
                            "path": _posix(entry),
                            "why": f"not a document: folder of {left_out} non-document file(s)",
                        }
                    )


def _scan_drive(collector, drive_dir, terms):
    drive_dir = Path(drive_dir)
    manifest_path = drive_dir / "drive-manifest.json"
    if not manifest_path.is_file():
        raise ImportScanError(
            f"--drive-dir {drive_dir} has no drive-manifest.json. A Code session "
            "writes it from the Drive connector's get_file_metadata."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ImportScanError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, list):
        raise ImportScanError(f"{manifest_path} must be a list of file entries")

    listed = set()
    for entry in manifest:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ImportScanError(f"{manifest_path} has an entry without a name")
        name = entry["name"]
        listed.add(name)
        path = drive_dir / name
        if not path.is_file():
            collector.excluded.append(
                {
                    "path": _posix(path),
                    "why": "listed in drive-manifest.json but not in the directory",
                }
            )
            continue
        expected = entry.get("md5Checksum")
        if expected:
            actual = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
            if actual.lower() != str(expected).lower():
                raise ImportScanError(
                    f"{path.as_posix()} does not match its md5Checksum in "
                    "drive-manifest.json. It was altered after download (the "
                    "connector's read_file_content rewrites Markdown); download "
                    "the exact bytes with download_file_content."
                )
        created = _parse_time(
            entry.get("createdTime"), f"drive-manifest.json entry {name!r} createdTime"
        )
        collector.seen_paths.add(path.resolve())
        if path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            collector.excluded.append(
                {"path": _posix(path), "why": _not_a_document_reason(path)}
            )
            continue
        collector.found.append(_read_document(path, "drive", terms, created))

    for path in sorted(drive_dir.iterdir(), key=lambda p: p.name.lower()):
        if (
            path.is_file()
            and path.name not in listed
            and path.name != manifest_path.name
        ):
            collector.excluded.append(
                {
                    "path": _posix(path),
                    "why": "in the Drive directory but not in drive-manifest.json",
                }
            )


def _scan_recovered(collector, recovered_dir, terms):
    recovered_dir = Path(recovered_dir)
    if not recovered_dir.is_dir():
        raise ImportScanError(f"--recovered {recovered_dir} is not a directory")
    for path in sorted(recovered_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.name.lower().endswith(tuple(s + ".json" for s in _DOCUMENT_SUFFIXES)):
            continue  # a sidecar, read with its document
        sidecar = path.with_name(path.name + ".json")
        if path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            collector.excluded.append(
                {"path": _posix(path), "why": _not_a_document_reason(path)}
            )
            continue
        claimed = None
        if sidecar.is_file():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ImportScanError(f"{sidecar} is not valid JSON: {exc}") from exc
            if isinstance(data, dict) and data.get("claimed_at"):
                claimed = _parse_time(data["claimed_at"], f"{sidecar} claimed_at")
        item = _read_document(path, "recovered", terms, claimed)
        item.recovered = True
        item.claimed = claimed is not None
        collector.found.append(item)


def _scan_runs(collector, history_root, keys):
    root = Path(history_root)
    for key in keys:
        directory = root / key
        if not directory.is_dir():
            collector.warnings.append(
                f"history key {key!r}: no directory {directory.as_posix()}"
            )
            continue
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            match = _REPORT_RE.match(path.name)
            if not (path.is_file() and match):
                continue
            try:
                report = json.loads(_read_text(path))
                if not isinstance(report, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                collector.excluded.append(
                    {
                        "path": _posix(path),
                        "why": f"unreadable report: {exc.__class__.__name__}",
                    }
                )
                continue
            collector.seen_paths.add(path.resolve())
            moment = None
            generated = report.get("generated")
            if isinstance(generated, str):
                try:
                    moment = _parse_time(generated, "generated")
                except ImportScanError:
                    moment = None
            if moment is None and match.group("ts"):
                moment = datetime.strptime(match.group("ts"), "%Y%m%d_%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            if moment is None:
                moment = _mtime(path)
            prefix = path.name[: -len("report.json")]
            siblings = sorted(
                _posix(p)
                for p in directory.iterdir()
                if p.is_file() and p.name.startswith(prefix) and p != path
            )
            ensemble = (
                report.get("ensemble")
                if isinstance(report.get("ensemble"), dict)
                else {}
            )
            cost = (
                report.get("cost_summary")
                if isinstance(report.get("cost_summary"), dict)
                else {}
            )
            corrected = report.get("corrected_draft")
            changes = report.get("lt_corrections_applied")
            collector.runs.append(
                {
                    "key": key,
                    "path": _posix(path),
                    "run": int(match.group("run")),
                    "time": moment,
                    "preset": ensemble.get("cost_preset")
                    or ensemble.get("thoroughness"),
                    "cost": cost.get("total_usd"),
                    "corrected": corrected
                    if isinstance(corrected, str) and corrected.strip()
                    else None,
                    "changes": changes if isinstance(changes, list) else [],
                    "siblings": siblings,
                }
            )


# ---------------------------------------------------------------------------
# Building events
# ---------------------------------------------------------------------------


def _best_time(holders):
    """The time of a version or file: the earliest, unless a more trusted source is within 5 minutes of it.

    A reproduction's claimed time counts only when nothing else has a time.
    """
    timed = [h for h in holders if h.time and h.source != "recovered"]
    if not timed:
        timed = [h for h in holders if h.time]
    if not timed:
        return None
    earliest = min(h.time for h in timed)
    near = [h for h in timed if h.time - earliest <= _CLOSE]
    return min(near, key=lambda h: (_TRUST[h.source], h.time)).time


def _closeness(a, b):
    ta, tb = tokenise(a), tokenise(b)
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


def _build_events(collector):
    """Draft versions, other documents and runs, as events with no time order yet."""
    versions = {}  # sha -> Event (draft)
    others = {}  # (kind, sha) -> Event

    def version_for(sha, text):
        if sha not in versions:
            versions[sha] = Event(kind="draft", sha=sha, text=text)
        return versions[sha]

    recovered_drafts = []
    for item in collector.found:
        holder = Holder(
            source=item.source,
            path=_posix(item.path),
            time=item.time,
            label=item.label,
            claimed=item.claimed,
        )
        if item.kind == "draft":
            if item.recovered:
                recovered_drafts.append((item, holder, item.text))
            else:
                version_for(_sha(item.text), item.text).holders.append(holder)
            continue
        if item.embedded:
            inner = Holder(
                source=item.source,
                path=_posix(item.path),
                time=item.time,
                label=item.label,
                part="draft",
                claimed=item.claimed,
            )
            if item.recovered:
                recovered_drafts.append((item, inner, item.embedded))
            else:
                version_for(_sha(item.embedded), item.embedded).holders.append(inner)
        key = (item.kind, _sha(item.text), item.recovered)
        event = others.setdefault(
            key,
            Event(
                kind=item.kind,
                sha=_sha(item.text),
                recovered=item.recovered,
                include=item.kind != "unknown",
            ),
        )
        event.holders.append(holder)

    # Runs: the draft each reviewed is the submitted version when it can be found.
    run_events = []
    for run in collector.runs:
        event = Event(kind="run", at=run["time"], run_data=run)
        corrected = run["corrected"]
        if corrected is not None:
            holder = Holder(
                source="run",
                path=run["path"],
                time=run["time"],
                part="corrected_draft",
                run=run["run"],
            )
            submitted_text = (
                undo_lt_corrections(corrected, run["changes"])
                if run["changes"]
                else None
            )
            target = None
            if submitted_text is not None and normalise(submitted_text) in {
                v.text for v in versions.values()
            }:
                target = versions[_sha(normalise(submitted_text))]
                holder.lt_undone = True
            elif _sha(normalise(corrected)) in versions:
                target = versions[_sha(normalise(corrected))]
            if target is None:
                text = normalise(corrected)
                target = version_for(_sha(text), text)
                target.note = (
                    "LanguageTool-corrected; the submitted bytes were not found"
                    if run["changes"]
                    else "from the run's saved draft; no file holds this version"
                )
            target.holders.append(holder)
            event.version = target
            event.sha = _sha(normalise(corrected))
        run_events.append(event)

    # Reproductions: any other source holding the version wins.
    for item, holder, text in sorted(
        recovered_drafts, key=lambda r: (r[1].path, r[1].part or "")
    ):
        sha = _sha(text)
        if sha in versions:
            versions[sha].holders.append(holder)  # the same bytes: it is that version
            continue
        # A version is "held" by a real source unless every holder is a reproduction.
        label_matches = []
        if holder.label:
            for version in versions.values():
                if any(h.source != "recovered" for h in version.holders) and (
                    holder.label in version.match_labels()
                ):
                    label_matches.append((_closeness(text, version.text), version))
        best = max(label_matches, key=lambda m: m[0], default=None)
        if best and best[0] >= _MIN_CLOSENESS:
            event = Event(
                kind="draft",
                sha=sha,
                text=text,
                holders=[holder],
                include=False,
                recovered=True,
                superseded_by=best[1],
            )
            others[("superseded", sha, holder.path, holder.part)] = event
        else:
            version = version_for(sha, text)
            version.holders.append(holder)

    drafts = list(versions.values())
    for version in drafts:
        real = [h for h in version.holders if h.source != "recovered"]
        version.recovered = not real
        version.submitted = bool(
            version.label
            or any(h.source in ("run", "drive") for h in real)
            or (version.recovered and any(h.label for h in version.holders))
        )
    return drafts, list(others.values()), run_events


def _place_recovered(events, drafts):
    """Give a reproduction with no claimed time a place by its label, or none.

    Between the nearest lower and higher labelled versions of the same prefix:
    just after the lower, else just before the higher. With neither it stays
    unplaced; it is never dated from its file.
    """
    anchors = {}
    for version in drafts:
        if version.recovered or version.at is None:
            continue
        for label in version.match_labels():
            anchors.setdefault(label, version.at)

    def number(label):
        return int(label[1:])

    for event in events:
        if event.at is not None or not event.recovered:
            continue
        label = event.label
        if not label:
            continue
        same = [(number(k), t) for k, t in anchors.items() if k[0] == label[0]]
        lower = [x for x in same if x[0] < number(label)]
        higher = [x for x in same if x[0] > number(label)]
        if lower:
            event.at = max(lower)[1] + timedelta(seconds=1)
            event.placement = f"after {label[0]}{max(lower)[0]}, by its label"
        elif higher:
            event.at = min(higher)[1] - timedelta(seconds=1)
            event.placement = f"before {label[0]}{min(higher)[0]}, by its label"


def _mark_suspect_runs(run_events):
    by_version = {}
    for event in run_events:
        by_version.setdefault(id(event.version), []).append(event)
    for event in run_events:
        reasons = []
        version = event.version
        if version is None:
            reasons.append("the report holds no draft")
        elif all(h.source == "run" for h in version.holders):
            reasons.append("its draft matches no other source")
        if version is not None:
            near = [
                other
                for other in by_version[id(version)]
                if other is not event and abs(other.at - event.at) <= _EXPERIMENT_WINDOW
            ]
            if near:
                reasons.append(f"{len(near) + 1} runs on this draft within 30 minutes")
        event.run_data["suspect"] = "experiment?" if reasons else None
        event.run_data["suspect_why"] = "; ".join(reasons) if reasons else None


def _group_rounds(events):
    """Rounds: a new one at each included, submitted draft version."""

    def tiebreak(e):
        path = e.run_data["path"] if e.kind == "run" else e.holders[0].path
        return (_KIND_ORDER[e.kind], e.superseded_by is not None, e.sha or "", path)

    placed = sorted(
        (e for e in events if e.at is not None), key=lambda e: (e.at, *tiebreak(e))
    )
    unplaced = sorted((e for e in events if e.at is None), key=tiebreak)
    rounds = []
    for event in placed:
        starts = event.kind == "draft" and event.submitted and event.include
        if not rounds or (starts and rounds[-1]["started"]):
            rounds.append({"events": [], "started": False})
        rounds[-1]["events"].append(event)
        if starts:
            rounds[-1]["started"] = True
    if unplaced:
        if not rounds:
            rounds.append({"events": [], "started": False})
        rounds[-1]["events"].extend(unplaced)
    return rounds


def _accepted_at(rounds, index):
    these = [e for e in rounds[index]["events"] if e.at is not None]
    if not these:
        return None
    last = max(e.at for e in these)
    if index + 1 < len(rounds):
        following = [e.at for e in rounds[index + 1]["events"] if e.at is not None]
        if following:
            return max(min(following) - timedelta(seconds=1), last)
    return last


def _near_duplicates(drafts):
    """Pairs of included draft versions that ``same_passage`` calls the same."""
    tokens = {id(v): tokenise(v.text) for v in drafts}
    pairs = []
    for i, a in enumerate(drafts):
        for b in drafts[i + 1 :]:
            if same_passage(tokens[id(a)], tokens[id(b)]):
                pairs.append((a, b))
    return pairs


def _source_summary(holders):
    return sorted(
        (h.record() for h in holders),
        key=lambda s: (
            s["time"] or "",
            _TRUST[s["type"]],
            s["path"],
            s.get("part", ""),
        ),
    )


def scan(
    slug,
    *,
    history_keys=None,
    history_root=None,
    files=(),
    include=(),
    drive_dir=None,
    recovered=None,
    chat_export=None,
    conversations=(),
    environ=None,
):
    """Read the sources and return ``(plan, stats)``. Writes nothing.

    ``plan`` is the JSON-ready dict; ``stats`` holds the counts the summary prints.
    """
    if chat_export or conversations:
        raise ImportScanError(_CHAT_NOT_BUILT)
    if not isinstance(slug, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", slug
    ):
        raise ImportScanError(f"{slug!r} is not an article slug")
    keys = list(dict.fromkeys(history_keys or [slug]))
    for key in keys:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", key) or key in _NEVER_READ:
            raise ImportScanError(
                f"history key {key!r} is not a history directory name "
                f"({', '.join(_NEVER_READ)} are never read)"
            )
    if history_root is not None:
        root, root_why = Path(history_root), "--history-root"
    else:
        root, root_why = default_history_root(environ)

    terms = {slug.lower(), *(k.lower() for k in keys)}
    collector = _Collector()
    _scan_runs(collector, root, keys)
    _scan_files(collector, files, terms, include)
    if drive_dir:
        _scan_drive(collector, drive_dir, terms)
    if recovered:
        _scan_recovered(collector, recovered, terms)

    drafts, others, run_events = _build_events(collector)
    if not (drafts or others or run_events):
        raise ImportScanError(
            f"nothing found for {slug!r}: no runs under {root.as_posix()} for "
            f"{', '.join(keys)} and no matching documents. Check --history-key, "
            "--history-root and --files."
        )

    # Times. A version's time is its best source's; a reproduction is placed last.
    for event in [*drafts, *others]:
        event.at = _best_time(event.holders)
        if event.at is None and event.recovered and event.superseded_by is None:
            event.at = None
    for event in others:
        if event.superseded_by is not None:
            event.at = event.superseded_by.at
    claimed_only = [e for e in [*drafts, *others] if e.at is None]
    _place_recovered(claimed_only, drafts)

    _mark_suspect_runs(run_events)
    all_events = [*drafts, *others, *run_events]

    rounds = _group_rounds(all_events)

    # Attach each non-draft document to the nearest included draft in its round.
    for group in rounds:
        group_drafts = [
            e for e in group["events"] if e.kind == "draft" and e.include and e.at
        ]
        for event in group["events"]:
            if event.kind in ("draft", "run") or event.at is None or not group_drafts:
                continue
            event.attached_to = min(
                group_drafts, key=lambda d: (abs(d.at - event.at), d.at)
            )

    # Ids follow the plan's order, so they are stable for the same inputs.
    counter = 0
    for group in rounds:
        for event in group["events"]:
            counter += 1
            event.id = f"e{counter}"

    # A label that sits on a draft in another round is worth a hint.
    drafts_by_label = {}
    round_of = {}
    for number, group in enumerate(rounds, start=1):
        for event in group["events"]:
            round_of[id(event)] = number
            if event.kind == "draft" and event.include:
                for label in event.match_labels():
                    drafts_by_label.setdefault(label, event)
    for event in all_events:
        if event.kind in ("draft", "run") or not event.label:
            continue
        mate = drafts_by_label.get(event.label)
        if mate is not None and round_of[id(mate)] != round_of[id(event)]:
            event.hint = (
                f"label {event.label} is on a draft in round {round_of[id(mate)]}; "
                "placed by time"
            )

    near = [
        sorted([a.id, b.id], key=lambda i: int(i[1:]))
        for a, b in _near_duplicates([d for d in drafts if d.include])
    ]
    near.sort(key=lambda p: (int(p[0][1:]), int(p[1][1:])))

    plan = {
        "version": PLAN_VERSION,
        "slug": slug,
        "history_keys": keys,
        "rounds": [
            {
                "round": number,
                "accepted_at": _iso(_accepted_at(rounds, number - 1))
                if _accepted_at(rounds, number - 1)
                else None,
                "events": [_event_record(e) for e in group["events"]],
            }
            for number, group in enumerate(rounds, start=1)
        ],
        "excluded": sorted(collector.excluded, key=lambda x: (x["path"], x["why"])),
        "near_duplicates": near,
        "applied": {},
    }
    if collector.warnings:
        plan["warnings"] = sorted(collector.warnings)

    stats = _stats(
        collector, drafts, others, run_events, rounds, root, root_why, keys, near
    )
    return plan, stats


def _event_record(event):
    if event.kind == "run":
        data = event.run_data
        record = {
            "id": event.id,
            "at": _iso(event.at),
            "kind": "run",
            "run": data["run"],
            "history_key": data["key"],
            "report": data["path"],
            "preset": data["preset"],
            "cost_usd": data["cost"],
            "sha256": event.sha,
            "draft": event.version.id if event.version else None,
            "files": data["siblings"],
            "suspect": data["suspect"],
            "include": True,
        }
        if data["suspect_why"]:
            record["suspect_why"] = data["suspect_why"]
        return record
    record = {
        "id": event.id,
        "at": _iso(event.at) if event.at else None,
        "kind": event.kind,
        "sha256": event.sha,
        "label": event.label,
        "sources": _source_summary(event.holders),
        "include": event.include,
    }
    if event.kind == "draft":
        record["submitted"] = event.submitted and event.superseded_by is None
    other = event.other_labels()
    if other:
        record["other_labels"] = other
    if event.recovered:
        record["recovered"] = True
    if event.superseded_by is not None:
        record["superseded_by"] = event.superseded_by.id
    if event.attached_to is not None:
        record["attached_to"] = event.attached_to.id
    if event.note:
        record["note"] = event.note
    if event.placement:
        record["placement"] = event.placement
    if event.hint:
        record["hint"] = event.hint
    if event.at is None:
        record["hint"] = "no time: a reproduction is never dated from its file; set at"
    return record


def _stats(collector, drafts, others, run_events, rounds, root, root_why, keys, near):
    types = ("run", "drive", "file", "recovered")
    per_source = {}
    for source in types:
        if source == "run":
            candidates = len(run_events)
        else:
            candidates = sum(1 for f in collector.found if f.source == source)
        versions = sum(1 for d in drafts if any(h.source == source for h in d.holders))
        per_source[source] = {"candidates": candidates, "draft_versions": versions}
    draft_holders = sum(len(d.holders) for d in drafts)
    document_files = len(collector.found)
    other_events = [e for e in others if e.superseded_by is None]
    other_holders = sum(len(e.holders) for e in others if e.superseded_by is None)
    return {
        "history_root": root.as_posix(),
        "history_root_why": root_why,
        "history_keys": keys,
        "per_source": per_source,
        "draft_versions": len(drafts),
        "draft_holders": draft_holders,
        "duplicates_removed": (draft_holders - len(drafts))
        + (other_holders - len(other_events)),
        "other_documents": len(other_events),
        "document_files": document_files,
        "superseded": sum(1 for e in others if e.superseded_by is not None),
        "recovered_kept": sum(1 for d in drafts if d.recovered),
        "runs": len(run_events),
        "suspect_runs": sum(1 for r in run_events if r.run_data["suspect"]),
        "rounds": len(rounds),
        "excluded": len(collector.excluded),
        "skipped_by_name": collector.skipped_by_name,
        "near_duplicate_pairs": len(near),
        "warnings": list(collector.warnings),
    }


# ---------------------------------------------------------------------------
# The plan file, and its timeline
# ---------------------------------------------------------------------------


def plan_text(plan):
    return json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_plan(plan, out_path):
    """Write the plan as UTF-8 with LF line endings, refusing a path inside the checkout."""
    refuse_out_inside_checkout(out_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(plan_text(plan))


def _short_sources(sources):
    if not sources:
        return "-"
    counts = {}
    for source in sources:
        counts[source["type"]] = counts.get(source["type"], 0) + 1
    names = [Path(s["path"]).name for s in sources if s["type"] != "run"]
    kinds = "+".join(f"{k}({n})" if n > 1 else k for k, n in sorted(counts.items()))
    if names:
        extra = f" +{len(names) - 1}" if len(names) > 1 else ""
        return f"{kinds} {names[0]}{extra}"
    return kinds


def format_timeline(plan):
    """One line per event: round, time, kind, label, run, sources, marks."""
    lines = [
        f"{'round':<5} {'at (UTC)':<20} {'id':<5} {'kind':<11} {'label':<6} {'run':<4} sources"
    ]
    for group in plan.get("rounds", []):
        for event in group.get("events", []):
            marks = []
            if not event.get("include", True):
                marks.append("excluded")
            if event.get("recovered"):
                marks.append("recovered")
            if event.get("superseded_by"):
                marks.append(f"superseded by {event['superseded_by']}")
            if event.get("suspect"):
                marks.append("suspect")
            sources = event.get("sources")
            if event.get("kind") == "run":
                sources = [{"type": "run", "path": event.get("report", "")}]
                described = f"run {event.get('history_key', '')}"
            else:
                described = _short_sources(sources)
            at = (event.get("at") or "?").replace("T", " ").rstrip("Z")
            lines.append(
                f"{group.get('round', '?'):<5} {at:<20} {event.get('id', ''):<5} "
                f"{event.get('kind', ''):<11} {event.get('label') or '-':<6} "
                f"{event.get('run') if event.get('run') is not None else '-':<4} "
                f"{described}" + (f"  [{', '.join(marks)}]" if marks else "")
            )
        accepted = group.get("accepted_at") or "?"
        lines.append(f"{'':<5} accepted_at {accepted.replace('T', ' ').rstrip('Z')}")
    return lines


def format_summary(stats):
    lines = [
        f"History root: {stats['history_root']} ({stats['history_root_why']})",
        f"History keys: {', '.join(stats['history_keys'])}",
        "Per source (candidates; draft versions it holds):",
    ]
    for source, numbers in stats["per_source"].items():
        lines.append(
            f"  {source:<10} {numbers['candidates']:>4} candidates, "
            f"{numbers['draft_versions']:>3} draft versions"
        )
    lines.extend(
        [
            f"Draft versions: {stats['draft_versions']} distinct from "
            f"{stats['draft_holders']} copies; duplicates removed (all kinds): "
            f"{stats['duplicates_removed']}",
            f"Other documents: {stats['other_documents']}; runs: {stats['runs']} "
            f"({stats['suspect_runs']} marked suspect); rounds: {stats['rounds']}",
            f"Reproductions: {stats['superseded']} superseded, "
            f"{stats['recovered_kept']} kept and flagged recovered",
            f"Excluded: {stats['excluded']} listed; {stats['skipped_by_name']} files "
            "skipped because their names do not mention the article",
            f"Near-duplicate draft pairs listed: {stats['near_duplicate_pairs']}",
        ]
    )
    lines.extend(f"warning: {w}" for w in stats["warnings"])
    return lines


def format_excluded(plan):
    return [f"  {item['path']}: {item['why']}" for item in plan.get("excluded", [])]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _load_source(source):
    """``(text, None)`` for a source file that is present, else ``(None, why)``.

    The text is the normalised text the event's hash was taken from: the whole
    file, a handoff's embedded draft, or a run's ``corrected_draft`` (with its
    LanguageTool corrections undone when the plan says the run held the submitted
    version).
    """
    path = Path(source.get("path", ""))
    shown = path.as_posix()
    if not path.is_file():
        return None, f"{shown} is missing"
    try:
        raw = _read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{shown} cannot be read ({exc.__class__.__name__})"
    if source.get("type") == "run" or source.get("part") == "corrected_draft":
        try:
            report = json.loads(raw)
        except ValueError:
            return None, f"{shown} is not valid JSON"
        corrected = report.get("corrected_draft") if isinstance(report, dict) else None
        if not isinstance(corrected, str):
            return None, f"{shown} no longer holds a corrected_draft"
        if source.get("lt_undone"):
            undone = undo_lt_corrections(
                corrected, report.get("lt_corrections_applied") or []
            )
            if undone is None:
                return None, f"{shown}: its LanguageTool corrections no longer undo"
            return normalise(undone), None
        return normalise(corrected), None
    if source.get("part") == "draft":
        draft = _embedded_draft(raw)
        if draft is None:
            return None, f"{shown} no longer holds a draft submission"
        return normalise(draft), None
    return normalise(raw), None


def check_plan(plan):
    """Problems with an edited plan, as a list of strings (empty means it is fine)."""
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION:
        return [f"not a version {PLAN_VERSION} plan"]
    rounds = plan.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        return ["the plan has no rounds"]

    problems = []
    ids = {}
    for group in rounds:
        for event in group.get("events", []):
            if event.get("id") in ids:
                problems.append(f"event id {event.get('id')} appears twice")
            ids[event.get("id")] = event

    previous_number = 0
    previous_end = None
    for group in rounds:
        number = group.get("round")
        label = f"round {number}"
        if not isinstance(number, int) or number <= previous_number:
            problems.append(f"{label} is not after round {previous_number}")
        else:
            previous_number = number

        times = []
        for event in group.get("events", []):
            name = f"{label} event {event.get('id')}"
            if not isinstance(event.get("include"), bool):
                problems.append(f"{name}: include must be true or false")
                continue
            if not event["include"]:
                continue
            for field_name in ("superseded_by", "draft", "attached_to"):
                if event.get(field_name) and event[field_name] not in ids:
                    problems.append(f"{name}: {field_name} names no event")
            try:
                at = _parse_time(event.get("at"), name)
            except ImportScanError as exc:
                problems.append(f"{name} is included but has no usable time ({exc})")
                continue
            times.append((at, name))
            problems.extend(_check_event_files(event, name))
        for (earlier, _), (later, name) in zip(times, times[1:]):
            if later < earlier:
                problems.append(f"{name}: earlier than the event before it in {label}")

        accepted = None
        if times or group.get("accepted_at"):
            try:
                accepted = _parse_time(group.get("accepted_at"), f"{label} accepted_at")
            except ImportScanError as exc:
                problems.append(str(exc))
        if accepted and times and accepted < max(t for t, _ in times):
            problems.append(f"{label}: accepted_at is before its last included event")
        if times and previous_end and min(t for t, _ in times) < previous_end:
            problems.append(
                f"{label}: starts before the previous round was accepted "
                f"({_iso(previous_end)})"
            )
        if accepted:
            previous_end = accepted
    return problems


def _check_event_files(event, name):
    """Problems with an included event's files: one source must be present, unchanged."""
    if event.get("kind") == "run":
        sources = [
            {"type": "run", "path": event.get("report", ""), "part": "corrected_draft"}
        ]
    else:
        sources = event.get("sources") or []
    if not sources:
        return [f"{name}: included but names no source file"]
    recorded = event.get("sha256")
    reasons = []
    for source in sources:
        text, why = _load_source(source)
        if why:
            reasons.append(why)
        elif recorded and _sha(text) != recorded:
            reasons.append(
                f"{Path(source['path']).as_posix()} has changed since the scan"
            )
        else:
            return []
    return [
        f"{name}: no source file is present with the recorded hash ({'; '.join(reasons)})"
    ]
