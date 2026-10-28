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
(claude.ai      in-chat versions: a tool call's whole file, an edit   the tool call's timestamp, else the
export)         replayed on the file it edited, an attachment, a      message's ``created_at`` (server time)
                fenced block or a long human message
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

from . import chat_export as chat_export_module
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
    "DEFAULT_MIN_CHAT_WORDS",
]

PLAN_VERSION = 1

#: Built from its code point, not typed: this repository bans the invisible
#: character itself in source (test_markers.py).
BYTE_ORDER_MARK = chr(0xFEFF)

#: Directories under a history root that are never read: experiments, replays
#: and nested runs are not the article's own history.
_NEVER_READ = ("_experiments", "_replay", "_nested")

#: Lower is more trusted. Used to choose between source times that are close.
#: A chat message's time is the server's, like a run's ``generated``: the text
#: existed then.
_TRUST = {"run": 0, "chat": 0, "drive": 1, "file": 2, "recovered": 3}

#: Two timestamps this close are the same moment for trust purposes: the version's
#: time is then the more trusted source's, not merely the earlier one's.
_CLOSE = timedelta(minutes=5)

#: Runs on one draft this close together are probably one person tuning a setting.
_EXPERIMENT_WINDOW = timedelta(minutes=30)

#: How like a reproduction a version must be (Jaccard over content tokens) before
#: its label is allowed to supersede the reproduction. A label shared by several
#: versions (every run numbered 1) must not swallow a reproduction of another.
_MIN_CLOSENESS = 0.6

#: A chat text is a version of the draft when at least this share of *its* content
#: tokens is in some known version of the draft (issue #422)...
_CHAT_SHARE_OF_CANDIDATE = 0.6

#: ...and it carries at least this share of *that version's* tokens. The first
#: rule alone passes a two-thousand-character excerpt of a long draft, which is a
#: fragment and not a version.
_CHAT_SHARE_OF_VERSION = 0.5

#: A version only a chat holds (issue #430) must also be a version *of this
#: article* (PR #435). A linked conversation holds the article's drafts and
#: also its side documents (a verification log, a research batch, a source
#: excerpt), and a whole document of enough words passes the rule above either
#: way. The test is 8-word shingle containment: the share of a document's
#: 8-word windows that appear in the article's other versions. Measured on three
#: real articles, every side document scored 0.00-0.03 and the genuine drafts
#: scored 0.14 or more against the others (lowest 0.14, 0.35, 0.47, most far
#: higher). 0.10 is over three times the highest side document and below the
#: lowest genuine draft.
_SHINGLE_WORDS = 8
_RELATED_SHARE = 0.10

#: A text that sits inside a version it is a small part of is a fragment, not a
#: version: a section cut from a 51 KB draft (about 6 KB, 0.87 of it inside the
#: draft) covers 12% of the draft, and the body-only versions of a draft cover
#: 76% or more. A text is a fragment when at least ``_FRAGMENT_INSIDE`` of it is
#: inside a version written before it (or at the same time) and it covers less
#: than ``_COVERS_SHARE`` of every other version. A smaller text that a later
#: version expanded is an earlier draft, not a fragment. Half is also what ``_CHAT_SHARE_OF_VERSION`` already asks of a text
#: that resembles a version.
_FRAGMENT_INSIDE = 0.5
_COVERS_SHARE = 0.5

#: How far from a reproduction's claimed time an export version may be and still
#: stand in for it (the label must match too).
_REPRODUCTION_WINDOW = timedelta(days=3)

#: Where a chat text came from when the author, not the chat, named the file.
_AUTHORS_FILES = ("attachment", "project_doc")

#: A conversation the author linked (``--chat-map`` or ``--conversation``) keeps a
#: whole document even when no other source holds a similar version (issue #430),
#: if it has at least this many words. A shorter text is a note, not a draft.
DEFAULT_MIN_CHAT_WORDS = 300

#: Where a text that is a whole file comes from: a file-writing tool call's full
#: content, an attachment's text, a project knowledge file. A fenced block or a
#: long message is not on the list: a chat discusses many texts.
_WHOLE_FILE_VIAS = frozenset(
    {
        "create_file",
        "write",
        "artifacts",
        "drive_create_file",
        "drive_update_file",
        "dropbox_create_file",
        "attachment",
        "project_doc",
    }
)

#: Of those, the ones a chat's own tool call writes. A chat that writes a file again
#: with a few words changed has not made a new version, and one that does it in
#: every turn would bury the others; an attachment or a project file is the
#: author's own and is never a repeat.
_TOOL_WRITTEN_VIAS = _WHOLE_FILE_VIAS - {"attachment", "project_doc"}

#: File names with these endings are code, data or sources, not the article.
_NOT_PROSE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".ts", ".json", ".csv", ".tsv", ".html", ".htm", ".css",
        ".sh", ".yaml", ".yml", ".xml", ".sql", ".toml", ".ini", ".log",
        ".ipynb", ".svg", ".pdf",
    }
)  # fmt: skip

#: With no run and no labelled file to say where a round ends, a chat-only
#: article starts a new round after a silence longer than this.
_ROUND_GAP = timedelta(hours=48)

_DOCUMENT_SUFFIXES = (".md", ".markdown")

#: Notes, data and checks the article was written from live in a ``research/``
#: directory next to it (on disk, in Drive, mirrored in a project), and so do the
#: superseded drafts and metadata an author moves out of the way. A file there is
#: read exactly as it would be in the folder root when its name follows the
#: handoff naming for a draft or metadata (``-draft-r1-superseded-<date>``,
#: ``-draft-v4-prerun``, ``-metadata-r1-superseded``: the scan's own kind
#: detection); any other file there is never a version of the article, whatever
#: text it shares with it.
_RESEARCH_DIR = "research"
_RESEARCH_WHY = (
    "under a research/ directory and not named as a draft or metadata: research "
    "notes and data are not versions of the article"
)


def _under_research(name):
    """Whether a relative file name or path has a ``research`` directory in it."""
    parts = str(name).replace("\\", "/").lower().split("/")
    return _RESEARCH_DIR in parts[:-1]


def _left_out_of_research(name, terms):
    """Whether a file under ``research/`` is research, not a draft or metadata by name."""
    if not _under_research(name):
        return False
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    kind, _ = _file_kind_and_label(base, terms, "")
    return kind not in ("draft", "metadata")


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

    source: str  # run | chat | drive | file | recovered
    path: str
    time: object  # aware datetime, or None
    label: object = None
    part: object = None  # "draft" (inside a handoff) | "corrected_draft"
    lt_undone: bool = False
    run: object = None
    claimed: bool = False
    via: object = None  # chat only: the tool or place the text came from
    locator: object = None  # chat only: where in the message

    def record(self):
        entry = {
            "type": self.source,
            "path": self.path,
            "time": _iso(self.time) if self.time else None,
        }
        if self.via:
            entry["via"] = self.via
            entry["locator"] = self.locator
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
    why: object = None  # why an event is left out of the plan for the author to decide
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
    #: kept from a linked conversation although no other source holds a similar version
    uncorroborated: bool = False
    #: left out of the plan as not a version of the article: "not_related" or
    #: "fragment" (``why`` says which, with the numbers); include: true keeps it
    unrelated: object = None

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
        self.chat = None  # the export's candidates, when --chat-export was given
        self.terms = frozenset()


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

    def read_research(directory):
        # A draft or metadata by name is read as in the folder root; the rest is
        # research and each such file is listed.
        for child in sorted(directory.rglob("*"), key=lambda p: p.as_posix().lower()):
            if not child.is_file():
                continue
            if _left_out_of_research(f"{_RESEARCH_DIR}/{child.name}", terms):
                collector.excluded.append({"path": _posix(child), "why": _RESEARCH_WHY})
            else:
                consider(child, None, False)

    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            raise ImportScanError(f"--files {directory} is not a directory")
        if directory.name.lower() == _RESEARCH_DIR:
            read_research(directory)
            continue
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file():
                consider(entry, None, False)
            elif entry.is_dir() and entry.name.lower() == _RESEARCH_DIR:
                read_research(entry)
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
        if _left_out_of_research(name, terms):
            collector.excluded.append({"path": _posix(path), "why": _RESEARCH_WHY})
            continue
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
# The claude.ai export
# ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"^Article:[ \t]*(\S.*)$", re.MULTILINE)

_MIN_DATE = datetime.min.replace(tzinfo=timezone.utc)


def _article_title(collector):
    """The article's title from the newest metadata or handoff document, or ``None``."""
    documents = sorted(
        (f for f in collector.found if f.kind in ("metadata", "handoff")),
        key=lambda f: (f.time or _MIN_DATE, _posix(f.path)),
        reverse=True,
    )
    for document in documents:
        found = _ARTICLE_RE.search(document.text)
        if found and not found.group(1).strip().startswith("["):
            return found.group(1).strip()
    return None


def _prepare_chat(
    collector,
    path,
    listed,
    terms,
    chat_map_path=None,
    slug=None,
    min_words=DEFAULT_MIN_CHAT_WORDS,
):
    """Read the export, link its conversations to the article and list their candidates.

    With a ``chat_map_path`` the map alone links conversations (plus ``listed``);
    otherwise they are linked by what they say.
    """
    try:
        export = chat_export_module.load_export(path)
        chat_map = (
            chat_export_module.load_chat_map(chat_map_path) if chat_map_path else None
        )
        pattern = _term_pattern(terms)
        links = chat_export_module.link_conversations(
            export,
            pattern,
            None if chat_map is not None else _article_title(collector),
            listed,
            chat_map,
            slug,
        )
        absent = (
            chat_export_module.unlisted_mapped(export, chat_map, slug)
            if chat_map is not None
            else []
        )
    except chat_export_module.ChatExportError as exc:
        raise ImportScanError(str(exc)) from exc
    read = {}
    candidates = []
    for link in links:
        found, counts = chat_export_module.extract(link.conversation)
        for key, value in counts.items():
            read[key] = read.get(key, 0) + value
        candidates.extend((c, link.strong) for c in found)
    for doc in chat_export_module.project_candidates(export, pattern):
        if _left_out_of_research(doc.name, terms):
            collector.excluded.append({"path": doc.source, "why": _RESEARCH_WHY})
        else:
            candidates.append((doc, True))
    links.sort(key=lambda link: (link.conversation.get("created_at") or "", link.uuid))
    if absent:
        collector.warnings.append(
            f"--chat-map: {len(absent)} conversation(s) mapped to {slug} are not in "
            f"the export: {', '.join(absent)}"
        )
    if not links:
        if chat_map is not None:
            collector.warnings.append(
                f"--chat-map: no conversation in the map is mapped to {slug} "
                "(and none was passed with --conversation)"
            )
        else:
            collector.warnings.append(
                "--chat-export: no conversation in the export names the article "
                "(its slug, a file name, or its title); pass --conversation UUID"
            )
    return {
        "export": export,
        "links": links,
        "candidates": candidates,
        "read": read,
        "report": {},
        "chat_map": (Path(chat_map_path).resolve().as_posix() if chat_map else None),
        "explicit": {link.uuid for link in links if link.explicit},
        "min_words": min_words,
    }


def _chat_text(candidate, terms):
    """``(normalised text, label, part)`` of a chat candidate.

    The label is the one its file name or artifact title carries (``v27``, ``r3``).
    A Template A handoff holds the draft inside it, and that draft is the text.
    """
    label = None
    if candidate.name:
        _, label = _file_kind_and_label(candidate.name, terms, candidate.text)
    embedded = _embedded_draft(candidate.text)
    if embedded:
        return normalise(embedded), label, "draft"
    return normalise(candidate.text), label, None


def _resembles(tokens, refs):
    """Whether a text is a whole version of the draft: not an excerpt, outline or talk about it."""
    if not tokens:
        return False
    for ref in refs:
        shared = len(tokens & ref)
        if shared >= _CHAT_SHARE_OF_CANDIDATE * len(
            tokens
        ) and shared >= _CHAT_SHARE_OF_VERSION * len(ref):
            return True
    return False


def _only_chat(version):
    """A version whose only non-reproduction sources are chat messages."""
    real = [h for h in version.holders if h.source != "recovered"]
    return bool(real) and all(h.source == "chat" for h in real)


def _draft_text(text):
    """The normalised text a chat text is hashed from: a handoff's embedded draft, else all."""
    embedded = _embedded_draft(text)
    return normalise(embedded) if embedded else normalise(text)


def _prose_words(text):
    """Words in a text: whitespace-separated, with at least one letter in them."""
    return sum(1 for token in text.split() if any(c.isalpha() for c in token))


def _chat_only_problem(candidate, text, part, kept_shas, terms, min_words):
    """Why a linked conversation's text is not kept on its own, or ``None`` if it is.

    Issue #430. A whole document (a file-writing tool call's full content, an
    attachment, a project knowledge file) with at least ``min_words`` words is a
    version of the article even when no other source holds one (a tool-written
    file that is a near-duplicate of a version kept earlier is dropped before it
    gets here, in ``_ingest_chat``). An edit is kept when the text it edited
    was kept. A fenced block or a long message is neither, so it stays under the
    resemblance rule.
    """
    if candidate.edit:
        base = candidate.base
        if isinstance(base, str) and _sha(_draft_text(base)) in kept_shas:
            return None
        return "edit_base_not_kept"
    if candidate.via not in _WHOLE_FILE_VIAS:
        return "not_a_whole_file"
    if Path(candidate.name or "").suffix.lower() in _NOT_PROSE_SUFFIXES:
        return "not_prose"
    if part is None:
        kind, _ = _file_kind_and_label(candidate.name or "", terms, candidate.text)
        if kind not in ("draft", "unknown"):
            return "not_a_draft"
    if _prose_words(text) < min_words:
        return "short"
    return None


_EXTENSION_RE = re.compile(r"\.[a-z]{1,5}$")
_VERSION_MARKER_RE = re.compile(
    r"[-_ ](?:v\d+|r\d+|final|draft|\d{4}-\d{2}-\d{2}|\d{8}|superseded.*)$"
)


def _name_stem(name):
    """A file name's stem: what stays the same as a document is versioned.

    Lowercase the name, drop its extension, then drop trailing version markers
    one after another: ``-vN``, ``-rN``, ``-final``, ``-draft``, ``-superseded``
    and whatever follows it, and a date (``-YYYY-MM-DD`` or ``-YYYYMMDD``); the
    separator before a marker is ``-``, ``_`` or a space. Two names belong to
    one document only when their whole stems are equal: ``a-draft.md`` and
    ``a-v2.md`` are both ``a``, and ``a-check-r1.md`` is ``a-check``. Nothing
    joins on a prefix. A name with nothing left has no stem (``None``).
    """
    if not isinstance(name, str):
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem = _EXTENSION_RE.sub("", base.strip().lower())
    while True:
        found = _VERSION_MARKER_RE.search(stem)
        if not found:
            break
        stem = stem[: found.start()]
    stem = stem.strip(" -_")
    return stem or None


def _shingles(text):
    """The set of ``_SHINGLE_WORDS``-word windows of a text (hashed)."""
    words = normalise(text).split()
    size = _SHINGLE_WORDS
    if not words:
        return frozenset()
    if len(words) <= size:
        return frozenset([hash(tuple(words))])
    return frozenset(
        hash(tuple(words[i : i + size])) for i in range(len(words) - size + 1)
    )


def _share(part, whole):
    """The share of ``part``'s shingles that ``whole`` holds."""
    return len(part & whole) / len(part) if part else 0.0


def _not_versions(alone, anchors, stems, anchor_stems, bases):
    """Which texts only a linked chat holds are not versions of the article.

    ``alone`` is ``[(sha, text, time)]`` in time order. ``anchors`` are
    ``[(text, time)]`` of the versions another source holds (a run, a file,
    Drive, a reproduction, or a chat text that resembles one). ``stems`` maps a
    sha to the name stems (``_name_stem``) of the files the chat wrote it as,
    ``anchor_stems`` is the set of stems of the other sources' files, and
    ``bases`` maps an edit's sha to the shas of the texts it edited. Returns
    ``(left_out, undecided, by_name)``: ``{sha: (code, share)}``, how many texts
    could not be judged, and the shas kept only because of their name.

    A text is kept while ``_RELATED_SHARE`` of its shingles are in the versions
    kept so far: the anchors first, then the chat's own texts one after another
    until none more joins, so a draft that grew over a conversation is followed
    from version to version. A text that does not relate is still kept when its
    file name has the same stem as a kept version's (a first draft the article
    was later rewritten from), or when it is an edit of a kept text. With no
    anchor the biggest group of texts that share text with one another (by
    words) is the article; if no two share any, nothing says which is the
    article and none is left out. Of the kept, a fragment is left out (see
    ``_FRAGMENT_INSIDE``).
    """
    order = [sha for sha, _, _ in alone]
    shingles = {sha: _shingles(text) for sha, text, _ in alone}
    words = {sha: len(text.split()) for sha, text, _ in alone}
    when = {sha: time for sha, _, time in alone}
    held = [(_shingles(text), time) for text, time in anchors]
    kept = []
    if not held:
        group = {sha: {sha} for sha in order}
        for i, a in enumerate(order):
            for b in order[i + 1 :]:
                related = max(
                    _share(shingles[a], shingles[b]), _share(shingles[b], shingles[a])
                )
                if related >= _RELATED_SHARE:
                    merged = group[a] | group[b]
                    for member in merged:
                        group[member] = merged
        biggest = max(
            (frozenset(g) for g in group.values()),
            key=lambda g: (sum(words[s] for s in g), len(g)),
            default=frozenset(),
        )
        if len(biggest) < 2:
            return {}, len(order), set()
        kept = [sha for sha in order if sha in biggest]
    union = set()
    for part in [*(h for h, _ in held), *(shingles[sha] for sha in kept)]:
        union |= part
    left = [sha for sha in order if sha not in kept]
    by_name = set()
    grew = True
    while grew:
        grew = False
        known = set(anchor_stems)
        for sha in kept:
            known |= stems.get(sha, set())
        for sha in list(left):
            related = _share(shingles[sha], union) >= _RELATED_SHARE
            named = bool(stems.get(sha, set()) & known) or bool(
                bases.get(sha, set()) & set(kept)
            )
            if related or named:
                left.remove(sha)
                kept.append(sha)
                union |= shingles[sha]
                grew = True
                if not related:
                    by_name.add(sha)
                break  # the names known have grown: look again from the top
    out = {sha: ("not_related", _share(shingles[sha], union)) for sha in left}
    everyone = [(h, time) for h, time in held]
    everyone += [(shingles[sha], when[sha]) for sha in kept]
    for sha in kept:
        if sha in by_name:
            continue
        mine, moment = shingles[sha], when[sha]
        others = [(o, t) for o, t in everyone if o is not mine]
        inside = any(
            _share(mine, o) >= _FRAGMENT_INSIDE
            and (t is None or moment is None or t <= moment)
            for o, t in others
        )
        covers = max((_share(o, mine) for o, _ in others), default=0.0)
        if inside and covers < _COVERS_SHARE:
            out[sha] = ("fragment", covers)
    return out, 0, by_name


_UNRELATED_WHY = {
    "not_related": (
        "only {share:.0%} of its text is in the article's other versions (at "
        "least {need:.0%} is expected): it looks like a side document of the "
        "conversation, not a draft"
    ),
    "fragment": (
        "a fragment: most of it is inside another version, which it covers only "
        "{share:.0%} of (at least {need:.0%} is expected of a version)"
    ),
}


def _ingest_chat(
    chat, versions, version_for, ref_texts, terms, ref_times=(), ref_names=()
):
    """Turn the export's candidates into draft versions (or sources of existing ones).

    A candidate is kept when it resembles a known version (see ``_resembles``);
    kept ones are added to the references as they come, oldest first, so a draft
    that grew over a conversation is followed one step at a time. The references
    grow only from conversations linked by more than the title.

    A conversation the author linked (``--chat-map`` or ``--conversation``) also
    keeps a whole document that resembles nothing (``_chat_only_problem``); such a
    version is marked uncorroborated unless a version another source holds is
    like it.
    """
    before = set(versions)
    refs = [tokenise(t) for t in ref_texts]
    outside = list(refs)  # what the other sources hold; the chat's own texts excluded
    had_references = bool(refs)
    explicit = chat["explicit"]
    mapped = chat["chat_map"] is not None
    min_words = chat["min_words"]
    verdict, grown, weak_only, first_edit = {}, set(), set(), {}
    kept_shas, kept_by_via, dropped_by_via = set(), {}, {}
    kept_tokens, explicit_kept, skipped = {}, set(), {}
    stems, bases = {}, {}  # sha -> file name stems; edit sha -> the shas it edited
    kept = dropped = 0
    ordered = sorted(
        chat["candidates"],
        key=lambda cs: (
            cs[0].created,
            cs[0].conversation,
            cs[0].message,
            _locator_key(cs[0].locator),
        ),
    )
    for candidate, strong in ordered:
        text, label, part = _chat_text(candidate, terms)
        sha = _sha(text) if text else None
        if sha is None:
            dropped += 1
            dropped_by_via[candidate.via] = dropped_by_via.get(candidate.via, 0) + 1
            continue
        linked = candidate.conversation in explicit or (
            mapped and candidate.via == "project_doc"
        )
        tokens = None
        if (
            linked
            and not candidate.edit
            and candidate.via in _TOOL_WRITTEN_VIAS
            and sha not in kept_shas
        ):
            tokens = tokenise(text)
            if any(same_passage(tokens, other) for other in kept_tokens.values()):
                skipped["near_duplicate"] = skipped.get("near_duplicate", 0) + 1
                dropped += 1
                dropped_by_via[candidate.via] = dropped_by_via.get(candidate.via, 0) + 1
                continue
        if sha not in verdict:
            if tokens is None:
                tokens = tokenise(text)
            verdict[sha] = _resembles(tokens, refs)
        if not verdict[sha] and linked:
            if sha in kept_shas:
                verdict[sha] = True  # the same bytes, kept from another message
            else:
                problem = _chat_only_problem(
                    candidate, text, part, kept_shas, terms, min_words
                )
                if problem is None:
                    verdict[sha] = True
                else:
                    skipped[problem] = skipped.get(problem, 0) + 1
        if verdict[sha] and strong and sha not in grown:
            grown.add(sha)
            refs.append(tokens if tokens is not None else tokenise(text))
        if not verdict[sha]:
            dropped += 1
            dropped_by_via[candidate.via] = dropped_by_via.get(candidate.via, 0) + 1
            continue
        kept += 1
        kept_shas.add(sha)
        kept_tokens.setdefault(sha, tokens if tokens is not None else tokenise(text))
        kept_by_via.setdefault(candidate.via, set()).add(sha)
        if linked:
            explicit_kept.add(sha)
        first_edit.setdefault(sha, candidate.edit)
        if _name_stem(candidate.name):
            stems.setdefault(sha, set()).add(_name_stem(candidate.name))
        if candidate.edit and isinstance(candidate.base, str):
            bases.setdefault(sha, set()).add(_sha(_draft_text(candidate.base)))
        existed = sha in versions
        version = version_for(sha, text)
        version.holders.append(
            Holder(
                source="chat",
                path=candidate.source,
                time=candidate.created,
                label=label,
                part=part,
                via=candidate.via,
                locator=candidate.locator,
            )
        )
        if not existed and not strong:
            version.include = False
            version.why = "the conversation is linked to the article only by its title"
            weak_only.add(sha)
        elif strong and sha in weak_only:
            version.include = True
            version.why = None
            weak_only.discard(sha)
    new = kept_shas - before
    chat["report"].update(
        {
            "candidates": len(ordered),
            "kept": kept,
            "dropped": dropped,
            "dropped_by_via": dict(sorted(dropped_by_via.items())),
            "versions_by_via": {k: len(v) for k, v in sorted(kept_by_via.items())},
            "versions": len(kept_shas),
            "new": len(new),
            "duplicates_of_other_sources": len(kept_shas & before),
            "in_round_edits": sum(1 for sha in new if first_edit.get(sha)),
            "left_out": len(weak_only),
            "no_references": not had_references,
            "confirmed": 0,
            "replaced": 0,
        }
    )
    if explicit or mapped:
        # A version is corroborated when a version another source holds is like it.
        alone = {
            sha for sha in explicit_kept if not _resembles(kept_tokens[sha], outside)
        }
        for sha in alone:
            versions[sha].uncorroborated = True
        corroborated = len(kept_shas) - len(alone)
        times = list(ref_times) + [None] * (len(ref_texts) - len(ref_times))
        held_texts = [
            *zip(ref_texts, times),
            *(
                (versions[sha].text, min(h.time for h in versions[sha].holders))
                for sha in explicit_kept - alone
            ),
        ]
        in_order = [
            (sha, versions[sha].text, min(h.time for h in versions[sha].holders))
            for sha in sorted(
                alone,
                key=lambda sha: (min(h.time for h in versions[sha].holders), sha),
            )
        ]
        anchor_stems = set(ref_names)
        for sha in explicit_kept - alone:
            anchor_stems |= stems.get(sha, set())
        left_out, undecided, by_name = _not_versions(
            in_order, held_texts, stems, anchor_stems, bases
        )
        for sha in by_name:
            versions[
                sha
            ].note = "kept by file name: it has the same name stem as a kept version"
        for sha, (code, share) in left_out.items():
            need = _RELATED_SHARE if code == "not_related" else _COVERS_SHARE
            version = versions[sha]
            version.include = False
            version.unrelated = code
            version.why = _UNRELATED_WHY[code].format(share=share, need=need) + (
                "; set include to true in the plan to keep it"
            )
        alone = alone - set(left_out)
        chat["report"].update(
            {
                "not_versions": {
                    code: sum(1 for c, _ in left_out.values() if c == code)
                    for code in _UNRELATED_WHY
                    if any(c == code for c, _ in left_out.values())
                },
                "kept_by_name": len(by_name),
                "undecided": undecided,
            }
        )
        chat["report"].update(
            {
                "chat_only": len(alone),
                "chat_only_by_via": {
                    k: n
                    for k, n in sorted(
                        (k, len(v & alone)) for k, v in kept_by_via.items()
                    )
                    if n
                },
                "corroborated": corroborated,
                "chat_only_skipped": dict(sorted(skipped.items())),
            }
        )


def _in_reproduction_window(holder, version, versions):
    """Whether an export version could be the one a reproduction claims to copy.

    Its time must be near the reproduction's claimed time, if there is one, and
    between the nearest lower and higher labelled versions that other sources hold.
    """
    at = _best_time(version.holders)
    if at is None:
        return True
    if holder.time and abs(at - holder.time) > _REPRODUCTION_WINDOW:
        return False
    label = holder.label
    if not label:
        return True
    number = int(label[1:])
    lower = higher = None
    for other in versions.values():
        if other is version or _only_chat(other):
            continue
        moment = _best_time(other.holders)
        if moment is None:
            continue
        for found in other.match_labels():
            if found[0] != label[0]:
                continue
            n = int(found[1:])
            if n < number and (lower is None or moment > lower):
                lower = moment
            elif n > number and (higher is None or moment < higher):
                higher = moment
    if lower is not None and at <= lower:
        return False
    return not (higher is not None and at >= higher)


# ---------------------------------------------------------------------------
# Building events
# ---------------------------------------------------------------------------


def _time_tier(holder):
    """0 for a source that dates its text, 1 for a reproduction, 2 for a project document.

    A project document's ``created_at`` only says that the document existed by
    then, in some earlier state: the project syncs from Drive, so its text can be
    a later file's, byte for byte. It dates a version only when nothing else does.
    """
    if holder.source == "recovered":
        return 1
    return 2 if holder.via == "project_doc" else 0


def _best_holder(holders):
    """The holder that defines the time of a version or file, or ``None``.

    The earliest, unless a more trusted source is within 5 minutes of it. A
    reproduction's claimed time counts only when nothing else has a time, and a
    project document's creation time only when not even that exists
    (``_time_tier``).
    """
    timed = []
    for tier in (0, 1, 2):
        timed = [h for h in holders if h.time and _time_tier(h) == tier]
        if timed:
            break
    if not timed:
        return None
    earliest = min(h.time for h in timed)
    near = [h for h in timed if h.time - earliest <= _CLOSE]
    return min(near, key=lambda h: (_TRUST[h.source], h.time))


def _best_time(holders):
    """The time of a version or file (see ``_best_holder``)."""
    holder = _best_holder(holders)
    return holder.time if holder is not None else None


def _locator_key(locator):
    """Sort key for a chat locator (``item:N``, ``fence:N``, ``text``...).

    The number compares as a number: ``item:9`` precedes ``item:11``, which a
    string comparison reverses.
    """
    kind, _, number = str(locator or "").partition(":")
    return (kind, int(number) if number.isdigit() else -1)


def _edit_order(event):
    """Where an event sits among others made at the same instant by one chat message.

    A claude.ai export has one timestamp per message, but a message can edit a
    file many times, and those edits share that timestamp. They were made in item
    order, so the order is the message, then the locator's number. Events with no
    chat holder return an empty key and keep the order the rest of the tie-break
    gives them.
    """
    holder = _best_holder(event.holders)
    if holder is None or holder.source != "chat" or not holder.via:
        return ("", ("", -1))
    return (holder.path, _locator_key(holder.locator))


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

    # The claude.ai export: texts that resemble a known version, whole.
    chat = collector.chat
    if chat is not None:
        _ingest_chat(
            chat,
            versions,
            version_for,
            [
                *(v.text for v in versions.values()),
                *(text for _, _, text in recovered_drafts),
            ],
            collector.terms,
            [
                min((h.time for h in v.holders if h.time), default=None)
                for v in versions.values()
            ]
            + [holder.time for _, holder, _ in recovered_drafts],
            {
                stem
                for stem in (
                    _name_stem(Path(h.path).name)
                    for h in [
                        *(h for v in versions.values() for h in v.holders),
                        *(holder for _, holder, _ in recovered_drafts),
                    ]
                    if h.source in ("file", "drive", "recovered")
                )
                if stem
            },
        )

    if chat is not None and chat["report"].get("undecided"):
        collector.warnings.append(
            f"--chat-export: {chat['report']['undecided']} documents only a linked "
            "conversation holds share no text with one another, so none could be "
            "told from a side document of the conversation; all are kept. Set "
            "include to false in the plan for any that is not the article"
        )

    # Reproductions: any other source holding the version wins.
    for item, holder, text in sorted(
        recovered_drafts, key=lambda r: (r[1].path, r[1].part or "")
    ):
        sha = _sha(text)
        if sha in versions:
            if chat is not None and _only_chat(versions[sha]):
                chat["report"]["confirmed"] += 1  # the export holds these exact bytes
            versions[sha].holders.append(holder)  # the same bytes: it is that version
            continue
        # A version is "held" by a real source unless every holder is a reproduction.
        label_matches = []
        if holder.label:
            for version in versions.values():
                if any(h.source != "recovered" for h in version.holders) and (
                    holder.label in version.match_labels()
                ):
                    if _only_chat(version) and not _in_reproduction_window(
                        holder, version, versions
                    ):
                        continue
                    label_matches.append((_closeness(text, version.text), version))
        best = max(label_matches, key=lambda m: m[0], default=None)
        if best and best[0] >= _MIN_CLOSENESS:
            if chat is not None and _only_chat(best[1]):
                chat["report"]["replaced"] += 1  # the export's text stands in for it
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
        # A file name a chat's tool call chose says which version a draft is, but
        # a chat edits that file many times: those are revisions, not submissions.
        # A file the author put in front of the chat (an attachment, a project
        # file), another source's label, a run or a Drive copy makes it one.
        version.submitted = bool(
            any(
                h.label
                for h in version.holders
                if h.source != "chat" or h.via in _AUTHORS_FILES
            )
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


def _group_rounds(events, gap=None):
    """Rounds: a new one at each included, submitted draft version.

    With a ``gap`` (an article that exists only in chats, issue #430) a new round
    also starts after a silence longer than that between two events.
    """

    def tiebreak(e):
        path = e.run_data["path"] if e.kind == "run" else e.holders[0].path
        return (
            _KIND_ORDER[e.kind],
            e.superseded_by is not None,
            _edit_order(e) if e.kind != "run" else ("", ("", -1)),
            e.sha or "",
            path,
        )

    placed = sorted(
        (e for e in events if e.at is not None), key=lambda e: (e.at, *tiebreak(e))
    )
    unplaced = sorted((e for e in events if e.at is None), key=tiebreak)
    rounds = []
    previous = None
    for event in placed:
        if event.unrelated:
            # Left out as not a version: it joins the round that is open, and it
            # neither starts one nor ends a silence.
            if not rounds:
                rounds.append({"events": [], "started": False})
            rounds[-1]["events"].append(event)
            continue
        starts = event.kind == "draft" and event.submitted and event.include
        silent = gap is not None and previous is not None and event.at - previous > gap
        if not rounds or (starts and rounds[-1]["started"]) or silent:
            rounds.append({"events": [], "started": False})
        previous = event.at
        rounds[-1]["events"].append(event)
        if starts:
            rounds[-1]["started"] = True
    if unplaced:
        if not rounds:
            rounds.append({"events": [], "started": False})
        rounds[-1]["events"].extend(unplaced)
    return rounds


def _metadata_with_its_draft(rounds):
    """Move metadata saved just before its draft into the draft's round.

    An author often saves ``<slug>-metadata-rN.md`` seconds or minutes before
    ``<slug>-draft-rN.md``, and by time it then ends the round before. A metadata
    event whose label is the label of the submitted draft that starts a later
    round moves to the front of that round, but only when no other event of its
    own round comes after it in time: then the move reorders nothing, and the
    round it leaves still ends where it did. Any other case stays where time put
    it and keeps its hint.
    """
    starts = {}  # label -> index of the round that a submitted draft with it starts
    for number, group in enumerate(rounds):
        first = group["events"][0] if group["events"] else None
        if number and first is not None and first.kind == "draft":
            if first.include and first.submitted:
                for label in first.match_labels():
                    starts.setdefault(label, number)
    moves = {}
    for number, group in enumerate(rounds):
        for event in list(group["events"]):
            if event.kind != "metadata" or not event.include or event.at is None:
                continue
            target = starts.get(event.label)
            if target is None or target <= number:
                continue
            if any(e.at is not None and e.at > event.at for e in group["events"]):
                continue
            group["events"].remove(event)
            moves.setdefault(target, []).append(event)
            event.placement = (
                f"with the draft it labels ({event.label}), which starts round "
                f"{target + 1}; saved before it"
            )
    for target, moved in moves.items():
        moved.sort(key=lambda e: (e.at, e.sha or ""))
        rounds[target]["events"][0:0] = moved
    rounds[:] = [group for group in rounds if group["events"]]


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
    chat_map=None,
    min_chat_words=DEFAULT_MIN_CHAT_WORDS,
    environ=None,
):
    """Read the sources and return ``(plan, stats)``. Writes nothing.

    ``plan`` is the JSON-ready dict; ``stats`` holds the counts the summary prints.
    """
    if conversations and not chat_export:
        raise ImportScanError("--conversation needs --chat-export")
    if chat_map and not chat_export:
        raise ImportScanError("--chat-map needs --chat-export")
    if not isinstance(min_chat_words, int) or min_chat_words < 0:
        raise ImportScanError("--min-chat-words must be a whole number, 0 or more")
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
    collector.terms = frozenset(terms)
    _scan_runs(collector, root, keys)
    _scan_files(collector, files, terms, include)
    if drive_dir:
        _scan_drive(collector, drive_dir, terms)
    if recovered:
        _scan_recovered(collector, recovered, terms)
    if chat_export:
        collector.chat = _prepare_chat(
            collector,
            chat_export,
            conversations,
            terms,
            chat_map,
            slug,
            min_chat_words,
        )

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
        dated_by = _best_holder(event.holders)
        if dated_by is not None and dated_by.via == "project_doc":
            event.hint = (
                "time is when a project document holding it was created: it may "
                "have held an earlier state, and no other source dates it"
            )
    for event in others:
        if event.superseded_by is not None:
            event.at = event.superseded_by.at
    claimed_only = [e for e in [*drafts, *others] if e.at is None]
    _place_recovered(claimed_only, drafts)

    _mark_suspect_runs(run_events)
    all_events = [*drafts, *others, *run_events]

    # An article that exists only in chats has no run or labelled file to say where
    # a round ends: a silence of more than 48 hours does.
    chat_only_article = (
        not run_events
        and any(d.uncorroborated and d.include for d in drafts)
        and not any(d.submitted and d.include for d in drafts)
    )
    rounds = _group_rounds(all_events, _ROUND_GAP if chat_only_article else None)
    _metadata_with_its_draft(rounds)

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
        if not (_only_chat(a) and _only_chat(b))  # an edit chain, not a duplicate
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
    if collector.chat is not None:
        plan["chat"] = _chat_record(collector.chat)

    stats = _stats(
        collector, drafts, others, run_events, rounds, root, root_why, keys, near
    )
    return plan, stats


def _chat_record(chat):
    """The plan's ``chat`` block: where the export is, and which conversations were read.

    Conversation ids, dates, message counts and why each was linked; never a name
    or any text.
    """
    record = {
        "export": chat["export"].path,
        "linked": [
            {
                "conversation": link.uuid,
                "created_at": link.conversation.get("created_at"),
                "messages": link.messages,
                "linked_by": list(link.reasons),
                "strong": link.strong,
            }
            for link in chat["links"]
        ],
        "read": dict(sorted(chat["read"].items())),
        "kept": chat["report"],
        "skipped": dict(sorted(chat["export"].skipped.items())),
    }
    if chat["chat_map"] is not None:
        record["chat_map"] = chat["chat_map"]
    if chat["chat_map"] is not None or chat["explicit"]:
        record["min_chat_words"] = chat["min_words"]
    return record


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
    if event.uncorroborated:
        record["corroborated"] = False
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
    if event.why:
        record["why"] = event.why
    if event.placement:
        record["placement"] = event.placement
    if event.hint:
        record["hint"] = event.hint
    if event.at is None:
        record["hint"] = "no time: a reproduction is never dated from its file; set at"
    return record


def _stats(collector, drafts, others, run_events, rounds, root, root_why, keys, near):
    types = ("run", "chat", "drive", "file", "recovered")
    per_source = {}
    for source in types:
        if source == "run":
            candidates = len(run_events)
        elif source == "chat":
            candidates = collector.chat["report"]["kept"] if collector.chat else 0
        else:
            candidates = sum(1 for f in collector.found if f.source == source)
        versions = sum(1 for d in drafts if any(h.source == source for h in d.holders))
        per_source[source] = {"candidates": candidates, "draft_versions": versions}
    draft_holders = sum(len(d.holders) for d in drafts)
    document_files = len(collector.found)
    other_events = [e for e in others if e.superseded_by is None]
    other_holders = sum(len(e.holders) for e in others if e.superseded_by is None)
    stats = {
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
    if collector.chat is not None:
        chat = collector.chat
        stats["chat"] = {
            "conversations": len(chat["export"].conversations),
            "linked": [
                {
                    "uuid": link.uuid,
                    "created_at": link.conversation.get("created_at"),
                    "messages": link.messages,
                    "linked_by": list(link.reasons),
                    "strong": link.strong,
                }
                for link in chat["links"]
            ],
            "skipped": dict(chat["export"].skipped),
            "read": dict(chat["read"]),
            "rounds": len(rounds),
            "chat_map": chat["chat_map"] is not None,
            "min_chat_words": chat["min_words"],
            **chat["report"],
            "left_out_documents": [
                {
                    "id": d.id,
                    "source": d.holders[0].path,
                    "via": d.holders[0].via,
                    "words": len(d.text.split()),
                    "why": d.why,
                }
                for d in drafts
                if d.unrelated
            ],
        }
    return stats


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
    names = [Path(s["path"]).name for s in sources if s["type"] not in ("run", "chat")]
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
            if event.get("corroborated") is False:
                marks.append("chat-only")
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
    if "chat" in stats:
        lines.extend(_format_chat(stats["chat"]))
    lines.extend(f"warning: {w}" for w in stats["warnings"])
    return lines


def _format_chat(chat):
    """The export's part of the summary: counts and conversation ids, never names or text."""
    strong = sum(1 for link in chat["linked"] if link["strong"])
    if chat.get("chat_map"):
        by_map = sum(1 for link in chat["linked"] if "chat-map" in link["linked_by"])
        by_hand = sum(1 for link in chat["linked"] if "listed" in link["linked_by"])
        lines = [
            f"Chat export: {chat['conversations']} conversations, none searched by "
            f"content; {by_map} linked by --chat-map"
            + (f", {by_hand} by --conversation" if by_hand else ""),
        ]
    else:
        lines = [
            f"Chat export: {chat['conversations']} conversations searched by content "
            f"(they carry no project id); {len(chat['linked'])} linked ({strong} by "
            f"the slug or a file name, {len(chat['linked']) - strong} by the title only)",
        ]
    for link in chat["linked"]:
        lines.append(
            f"  {link['uuid']}  created {(link['created_at'] or '?')[:10]}  "
            f"{link['messages']} messages  linked by {', '.join(link['linked_by'])}"
        )
    kept_by = ", ".join(f"{k} {n}" for k, n in chat.get("versions_by_via", {}).items())
    lines.append(
        f"  Candidates: {chat.get('candidates', 0)}; kept {chat.get('kept', 0)}, "
        f"dropped {chat.get('dropped', 0)} (an outline, an excerpt, or talk about the draft)"
    )
    lines.append(
        f"  Distinct versions kept: {chat.get('versions', 0)}"
        + (f" ({kept_by})" if kept_by else "")
    )
    lines.append(
        f"  New: {chat.get('new', 0)}; already held by a run, Drive or a file: "
        f"{chat.get('duplicates_of_other_sources', 0)}; in-round edits: "
        f"{chat.get('in_round_edits', 0)}; left out for the author (title only): "
        f"{chat.get('left_out', 0)}"
    )
    if "chat_only" in chat:
        alone_by = ", ".join(f"{k} {n}" for k, n in chat["chat_only_by_via"].items())
        lines.append(
            f"  Chat-only versions (no other source holds them): {chat['chat_only']}"
            + (f" ({alone_by})" if alone_by else "")
            + f"; like a version another source holds: {chat['corroborated']}"
        )
        left = ", ".join(f"{k} {n}" for k, n in chat["chat_only_skipped"].items())
        lines.append(
            f"  Not kept from linked conversations (at least {chat['min_chat_words']} "
            f"words, whole documents only): {left or 'none'}"
        )
    for code, label in (("not_related", "side documents"), ("fragment", "fragments")):
        count = chat.get("not_versions", {}).get(code)
        if count:
            lines.append(
                f"  Left out of the draft history, not versions of the article "
                f"({count} {label}); each is in the plan with include: false and "
                "its reason"
            )
    if chat.get("kept_by_name"):
        lines.append(
            f"  Kept by file name: {chat['kept_by_name']} chat-only document(s) "
            "unlike the other versions but named like one (same name stem)"
        )
    for item in chat.get("left_out_documents", []):
        lines.append(
            f"    {item['id']}  {item['source']}  {item['via']}  {item['words']} "
            f"words: {item['why']}"
        )
    lines.append(
        f"  Reproductions: {chat.get('confirmed', 0)} confirmed by the export, "
        f"{chat.get('replaced', 0)} replaced by it"
    )
    read = chat["read"]
    lines.append(
        f"  Tool calls read: {read.get('tool_calls', 0)}; edits rebuilt "
        f"{read.get('edits_applied', 0)}, not rebuilt {read.get('edits_unanchored', 0)} "
        f"(file never written in the chat) + {read.get('edits_unapplied', 0)} (old text "
        f"not found exactly once); failed calls {read.get('failed_calls', 0)}; "
        f"shapes skipped {read.get('shapes_skipped', 0)}"
    )
    if chat["skipped"]:
        lines.append(
            "  Skipped in the export: "
            + ", ".join(f"{n} {what}" for what, n in sorted(chat["skipped"].items()))
        )
    if chat.get("no_references") and not chat.get("chat_only"):
        lines.append(
            "  warning: no other source holds a draft, so no chat text could be "
            "compared with one"
        )
    return lines


def format_excluded(plan):
    return [f"  {item['path']}: {item['why']}" for item in plan.get("excluded", [])]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _load_chat_source(source, context):
    """``(text, None)`` for a chat source the export still holds, else ``(None, why)``.

    The export is read once per ``check`` and only if an included event has a chat
    source. The text is rebuilt the way the scan built it, so a plan whose export
    was replaced or moved says so instead of passing.
    """
    where = source.get("path", "")
    exported = context.get("export_path")
    if not exported:
        return None, f"{where}: the plan names no chat export"
    if "export" not in context:
        try:
            context["export"] = chat_export_module.load_export(exported)
        except chat_export_module.ChatExportError as exc:
            context["export"] = str(exc)
    export = context["export"]
    if isinstance(export, str):
        return None, export
    conversation, _, message = where.removeprefix("chat:").partition("/")
    found = chat_export_module.recompute(
        export,
        conversation,
        message,
        source.get("locator", ""),
        pattern=_term_pattern(context["terms"]),
    )
    if found is None:
        return None, f"{where} is no longer in the export"
    return _chat_text(found, context["terms"])[0], None


def _load_source(source, context=None):
    """``(text, None)`` for a source file that is present, else ``(None, why)``.

    The text is the normalised text the event's hash was taken from: the whole
    file, a handoff's embedded draft, or a run's ``corrected_draft`` (with its
    LanguageTool corrections undone when the plan says the run held the submitted
    version), or a chat message's text rebuilt from the export.
    """
    if source.get("type") == "chat":
        return _load_chat_source(source, context or {})
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
    context = {
        "export_path": (plan.get("chat") or {}).get("export"),
        "terms": {
            str(t).lower()
            for t in [plan.get("slug"), *(plan.get("history_keys") or [])]
            if t
        },
    }
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
            problems.extend(_check_event_files(event, name, context))
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


def _check_event_files(event, name, context):
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
    # The export is large: read it only if no file holds the version.
    for source in sorted(sources, key=lambda s: s.get("type") == "chat"):
        text, why = _load_source(source, context)
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
