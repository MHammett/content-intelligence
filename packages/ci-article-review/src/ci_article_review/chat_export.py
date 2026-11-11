"""Read a claude.ai data export for ``ci-docs import scan --chat-export``.

The export is the only place the versions a drafting chat produced between two
submissions survive, so it is a source for ``history_import`` (issue #422). This
module reads it and finds the texts in it that could be a version of the draft;
``history_import`` decides which of them are (by comparing them with the versions
the other sources hold) and merges them into the plan. It makes no network call,
and it never prints or logs a conversation's name or text.

What the real export looks like (field names and types only; the schema sketch is
in docs/GITHUB-DOCUMENTS.md, "Importing an article's history"):

* ``conversations.json`` is a list of conversations: ``uuid``, ``name``,
  ``summary``, ``created_at``, ``updated_at``, ``account`` and ``chat_messages``.
  **A conversation carries no project id**, so a conversation can be tied to an
  article only by what it says.
* a message has ``uuid``, ``parent_message_uuid``, ``sender`` (``human`` or
  ``assistant``), ``created_at`` and ``updated_at`` (UTC, ``...Z``), ``text``,
  ``content``, ``attachments`` and ``files``;
* ``content`` is a list of items with a ``type`` (``text``, ``thinking``,
  ``tool_use``, ``tool_result``, ...), optional ``start_timestamp`` and
  ``stop_timestamp``, and for ``tool_use`` a ``name``, an ``id`` and an ``input``;
  a ``tool_result`` has ``tool_use_id`` and ``is_error``;
* an attachment has ``file_name``, ``file_size``, ``file_type`` and
  ``extracted_content`` (the pasted or uploaded text);
* ``projects/*.json`` is one project each: ``uuid``, ``name``, ``description``,
  ``prompt_template`` and ``docs`` (``filename``, ``content``, ``created_at``).

**The most exact chat-side versions are in the tool calls, not in the message
text**: a ``create_file`` or ``Write`` input holds a whole file, a ``str_replace``
or ``Edit`` input holds an edit as an old and a new string, an ``artifacts``
input holds a whole artifact or an edit of one, and the Google Drive and Dropbox
connectors' ``create_file`` hold a whole file (each tool occurs under several
spellings: ``Google Drive:create_file``, ``mcp__Google_Drive__create_file``).
Replaying a conversation's tool calls in order therefore rebuilds every version
of every file it wrote. Attachments come next (a pasted draft), and a fenced
block or a long human message last.

``load_chat_map`` reads the explicit conversation-to-article map that
``--chat-map`` takes (issue #430): with a map, ``link_conversations`` links by the
map alone, and ``Candidate.base`` lets the scan ask whether the text an edit was
applied to was kept.
"""

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

__all__ = [
    "ChatExportError",
    "Export",
    "Link",
    "Candidate",
    "MIN_TEXT_CHARS",
    "load_export",
    "load_chat_map",
    "link_conversations",
    "unlisted_mapped",
    "extract",
    "project_candidates",
    "recompute",
]

#: A fenced block or a human message shorter than this is not a whole draft.
MIN_TEXT_CHARS = 2000

#: One member of the export is read into memory whole; this is a sanity bound on
#: untrusted input (a real ``conversations.json`` is tens of megabytes).
_MAX_MEMBER_BYTES = 2 * 1024**3

_MAX_DEPTH = 4

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.DOTALL)


class ChatExportError(Exception):
    """An export that cannot be read, with a message the author can act on."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class Export:
    path: str  # resolved, posix
    conversations: list = field(default_factory=list)
    projects: list = field(default_factory=list)
    #: counts of things that were not the expected shape and were skipped
    skipped: dict = field(default_factory=dict)

    def conversation(self, uuid):
        for conv in self.conversations:
            if conv.get("uuid") == uuid:
                return conv
        return None


def _skip(export, what, count=1):
    export.skipped[what] = export.skipped.get(what, 0) + count


def _members(root, depth=0):
    """``(name, reader, inside_zip)`` for each JSON file under a directory, ZIP or file."""
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if len(path.relative_to(root).parts) > _MAX_DEPTH or not path.is_file():
                continue
            suffix = path.suffix.lower()
            relative = path.relative_to(root).as_posix()
            if suffix == ".json":
                yield relative, path.read_bytes, False
            elif suffix == ".zip" and depth < 1:
                yield from _members(path, depth + 1)
        return
    suffix = root.suffix.lower()
    if suffix == ".zip":
        try:
            archive = zipfile.ZipFile(root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ChatExportError(
                f"{root.as_posix()} is not a readable ZIP file ({exc.__class__.__name__})"
            ) from exc
        yield from _zip_members(archive, depth)
    else:
        # A file named on the command line is the conversations unless it says
        # it is the projects.
        named = (
            "projects.json"
            if root.stem.lower().startswith("projects")
            else "conversations.json"
        )
        yield named, root.read_bytes, False


def _zip_members(archive, depth):
    for info in sorted(archive.infolist(), key=lambda i: i.filename):
        if info.is_dir() or info.file_size > _MAX_MEMBER_BYTES:
            continue
        lowered = info.filename.lower()
        if lowered.endswith(".json"):
            yield info.filename, (lambda i=info: archive.read(i)), True
        elif lowered.endswith(".zip") and depth < 1:
            try:
                inner = zipfile.ZipFile(io.BytesIO(archive.read(info)))
            except zipfile.BadZipFile:
                continue
            yield from _zip_members(inner, depth + 1)


def _classify(name):
    parts = [p.lower() for p in PurePosixPath(name).parts]
    stem = PurePosixPath(parts[-1]).stem
    if stem.startswith("conversations"):
        return "conversations"
    if stem.startswith("projects") or "projects" in parts[:-1]:
        return "projects"
    return None


def load_export(path):
    """Read ``conversations.json`` (and the project files) from a ZIP, directory or file.

    Everything unexpected is skipped and counted in ``export.skipped``; only a path
    that cannot be read, or an export with no conversations, is an error.
    """
    root = Path(path)
    if not root.exists():
        raise ChatExportError(f"--chat-export {path} does not exist")
    export = Export(path=root.resolve().as_posix())
    seen = set()
    try:
        members = list(_members(root))
    except OSError as exc:
        raise ChatExportError(
            f"cannot read --chat-export {path} ({exc.__class__.__name__})"
        ) from exc
    # A download is often kept both zipped and extracted; the extracted copy is
    # read and the zip is not decompressed again.
    unzipped = {_classify(name) for name, _, inside in members if not inside}
    for name, reader, inside_zip in members:
        kind = _classify(name)
        if kind is None or (inside_zip and kind in unzipped):
            continue
        try:
            data = json.loads(reader().decode("utf-8-sig"))
        except (OSError, ValueError, KeyError):
            _skip(export, "unreadable files")
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if kind == "conversations":
                uuid = item.get("uuid") if isinstance(item, dict) else None
                if not isinstance(uuid, str) or not isinstance(
                    item.get("chat_messages"), list
                ):
                    _skip(export, "conversations of an unknown shape")
                    continue
                if uuid in seen:
                    _skip(export, "conversations listed twice")
                    continue
                seen.add(uuid)
                export.conversations.append(item)
            else:
                if isinstance(item, dict) and isinstance(item.get("uuid"), str):
                    export.projects.append(item)
                else:
                    _skip(export, "projects of an unknown shape")
    if not export.conversations:
        raise ChatExportError(
            f"no conversations found in --chat-export {path}: expected "
            "conversations.json (a list of conversations) in a ZIP or a directory"
        )
    return export


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _time(value):
    """An aware UTC datetime from an export timestamp, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _s(value):
    return value if isinstance(value, str) else ""


def _basename(path):
    return PurePosixPath(_s(path).replace("\\", "/")).name


def _messages(conversation):
    """The conversation's messages in time order (list order breaks ties)."""
    found = [
        (index, m)
        for index, m in enumerate(conversation.get("chat_messages") or [])
        if isinstance(m, dict) and isinstance(m.get("uuid"), str)
    ]
    far = datetime.max.replace(tzinfo=timezone.utc)
    found.sort(key=lambda p: (_time(p[1].get("created_at")) or far, p[0]))
    return [m for _, m in found]


# ---------------------------------------------------------------------------
# Linking a conversation to the article
# ---------------------------------------------------------------------------


@dataclass
class Link:
    conversation: dict
    reasons: tuple

    @property
    def uuid(self):
        return self.conversation["uuid"]

    @property
    def strong(self):
        """Linked by something other than the article's title alone."""
        return bool(set(self.reasons) - {"title"})

    @property
    def explicit(self):
        """Linked by the author (the map or ``--conversation``), not by what it says."""
        return bool(set(self.reasons) & {"chat-map", "listed"})

    @property
    def messages(self):
        return len(self.conversation.get("chat_messages") or [])


def _write_names(message):
    """File names a message's tool calls wrote or read: where a slug shows."""
    for item in message.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            inputs = item.get("input")
            if not isinstance(inputs, dict):
                continue
            for key in ("path", "file_path", "title", "name", "filename"):
                if isinstance(inputs.get(key), str):
                    yield inputs[key]


def _squash(text):
    return " ".join(text.lower().split())


def load_chat_map(path):
    """``{conversation uuid: slug}`` from a ``--chat-map`` TSV (issue #430).

    The header names the columns (``conversation`` and ``slug``; any others, such
    as a name or a confidence, are ignored and never read into the plan). A row
    with no slug is a conversation the author has not placed. Slugs are compared
    in lowercase. One conversation on two rows with different slugs is an error:
    the map is the only thing that links a conversation, so it must not be
    ambiguous.
    """
    source = Path(path)
    if not source.is_file():
        raise ChatExportError(f"--chat-map {path} does not exist")
    try:
        text = source.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ChatExportError(
            f"cannot read --chat-map {path} ({exc.__class__.__name__})"
        ) from exc
    # No quoting: a conversation's name may contain a quote mark, and the name
    # column is not read.
    rows = csv.reader(
        io.StringIO(text, newline=""), delimiter="\t", quoting=csv.QUOTE_NONE
    )
    header = next(rows, None)
    names = [cell.strip().lower() for cell in header or []]
    for needed in ("conversation", "slug"):
        if needed not in names:
            raise ChatExportError(
                f"--chat-map {path}: the header row must name a '{needed}' column "
                "(tab-separated, with a header like conversation<TAB>slug)"
            )
    at_conversation, at_slug = names.index("conversation"), names.index("slug")
    mapping = {}
    for row in rows:
        if len(row) <= max(at_conversation, at_slug):
            continue
        uuid, slug = row[at_conversation].strip(), row[at_slug].strip().lower()
        if not uuid or not slug:
            continue
        if mapping.setdefault(uuid, slug) != slug:
            raise ChatExportError(
                f"--chat-map {path}: conversation {uuid} is mapped to two slugs"
            )
    return mapping


def unlisted_mapped(export, chat_map, slug):
    """The conversations the map gives to ``slug`` that the export does not hold."""
    present = {conv["uuid"] for conv in export.conversations}
    return sorted(
        uuid
        for uuid, mapped in chat_map.items()
        if mapped == slug.lower() and uuid not in present
    )


def link_conversations(
    export, pattern, title=None, listed=(), chat_map=None, slug=None
):
    """The conversations that belong to the article, with why.

    ``pattern`` finds the slug (or a history key) in lowercase text. A
    conversation is linked when its name or summary, a message, an attachment's
    name or text, or a file name a tool call wrote carries the slug, when the
    author lists its uuid (``listed``), or, weakly, when it quotes the article's
    ``title``. Only a title match is weak: its candidates are kept in the plan
    but left out (``include: false``) for the author to decide.

    With a ``chat_map`` (``{uuid: slug}``, from ``load_chat_map``) nothing is
    linked by what it says: the conversations are those the map gives to ``slug``
    plus the ``listed`` ones, and a conversation the map gives to another slug is
    never linked (unless the author lists it).
    """
    listed = set(listed)
    if chat_map is not None:
        wanted = (slug or "").lower()
        links = []
        for conv in export.conversations:
            reasons = set()
            if chat_map.get(conv["uuid"]) == wanted:
                reasons.add("chat-map")
            if conv["uuid"] in listed:
                reasons.add("listed")
            if reasons:
                links.append(Link(conversation=conv, reasons=tuple(sorted(reasons))))
        _require_listed(listed, links)
        return links
    title = _squash(title) if title and len(title.strip()) >= 15 else None
    links = []
    for conv in export.conversations:
        reasons = set()
        if conv["uuid"] in listed:
            reasons.add("listed")
        name = _s(conv.get("name")).lower()
        summary = _s(conv.get("summary")).lower()
        if pattern.search(name):
            reasons.add("name")
        if pattern.search(summary):
            reasons.add("summary")
        weak_title = bool(
            title and (title in _squash(name) or title in _squash(summary))
        )
        for message in conv["chat_messages"]:
            if not isinstance(message, dict):
                continue
            text = _s(message.get("text")).lower()
            if pattern.search(text):
                reasons.add("text")
            elif title and not weak_title and title in _squash(text):
                weak_title = True
            for attachment in message.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                if pattern.search(_s(attachment.get("file_name")).lower()):
                    reasons.add("attachment name")
                if pattern.search(_s(attachment.get("extracted_content")).lower()):
                    reasons.add("attachment text")
            for entry in message.get("files") or []:
                if isinstance(entry, dict) and pattern.search(
                    _s(entry.get("file_name")).lower()
                ):
                    reasons.add("file name")
            for written in _write_names(message):
                if pattern.search(written.lower()):
                    reasons.add("tool file name")
        if weak_title:
            reasons.add("title")
        if reasons:
            links.append(Link(conversation=conv, reasons=tuple(sorted(reasons))))
    _require_listed(listed, links)
    return links


def _require_listed(listed, links):
    missing = listed - {link.uuid for link in links}
    if missing:
        raise ChatExportError(
            f"--conversation {', '.join(sorted(missing))}: not in the export"
        )


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One text in the export that might be a version of the draft."""

    conversation: str
    message: str
    locator: str  # where in the message: item:N, attachment:N, text, fence:N
    via: str  # create_file, str_replace, artifacts, attachment, ...
    name: object  # a file name or title, or None
    edit: bool  # produced by applying an edit to an earlier text
    created: object  # aware UTC datetime
    text: str
    #: for an edit: the text it was applied to (so a scan can ask whether that
    #: text was kept); ``None`` for a whole file
    base: object = None

    @property
    def source(self):
        return f"chat:{self.conversation}/{self.message}"


def _classify_tool(name):
    """``(space, operation, via)`` for a tool that writes a whole file or an edit, else ``None``.

    ``space`` is where the file lives (so an edit finds its file): the chat's
    sandbox or a local disk (``fs``), an artifact, Google Drive, Dropbox. Each tool
    occurs under several spellings.
    """
    raw = name.strip() if isinstance(name, str) else ""
    if raw.startswith("mcp__"):
        server, _, tool = raw[5:].partition("__")
    elif "__" in raw:
        server, _, tool = raw.partition("__")
    elif ":" in raw:
        server, _, tool = raw.partition(":")
    else:
        server, tool = "", raw
    server = re.sub(r"[^a-z0-9]", "", server.lower())
    if not server:
        return {
            "create_file": ("fs", "create", "create_file"),
            "Write": ("fs", "create", "write"),
            "str_replace": ("fs", "edit", "str_replace"),
            "Edit": ("fs", "edit", "edit"),
            "artifacts": ("artifact", "artifact", "artifacts"),
        }.get(tool)
    if server == "googledrive":
        return {
            "create_file": ("drive", "create", "drive_create_file"),
            "update_file": ("drive", "update", "drive_update_file"),
        }.get(tool)
    if server == "dropbox" and tool == "create_file":
        return ("dropbox", "create", "dropbox_create_file")
    return None


def _item_time(item, message_time):
    return (
        _time(item.get("start_timestamp"))
        or _time(item.get("stop_timestamp"))
        or message_time
    )


def extract(conversation):
    """``(candidates, stats)`` for one conversation.

    Replays the conversation's tool calls in order, so each ``str_replace`` /
    ``Edit`` / artifact update is applied to the text it edited and yields the
    version it produced. A tool call the conversation's own results report as
    failed is skipped; an edit whose file the conversation never wrote, or whose
    old string is not in it exactly once, cannot be rebuilt and is counted.
    """
    stats = {
        "tool_calls": 0,
        "failed_calls": 0,
        "edits_applied": 0,
        "edits_unanchored": 0,
        "edits_unapplied": 0,
        "shapes_skipped": 0,
        "no_time": 0,
    }
    messages = _messages(conversation)
    failed = {}
    for message in messages:
        for item in message.get("content") or []:
            if (
                isinstance(item, dict)
                and item.get("type") == "tool_result"
                and isinstance(item.get("tool_use_id"), str)
            ):
                failed[item["tool_use_id"]] = item.get("is_error") is True

    uuid = conversation["uuid"]
    candidates = []
    states = {}  # (space, key) -> text
    names = {}  # (space, key) -> file name
    stamps = {}  # (space, key) -> order of the last write
    counter = 0

    def emit(message, locator, via, name, edit, created, text, base=None):
        if created is None:
            stats["no_time"] += 1
            return
        candidates.append(
            Candidate(
                conversation=uuid,
                message=message["uuid"],
                locator=locator,
                via=via,
                name=name or None,
                edit=edit,
                created=created,
                text=text,
                base=base,
            )
        )

    def store(key, name, text):
        nonlocal counter
        counter += 1
        states[key] = text
        names[key] = name
        stamps[key] = counter

    def base_for(key, name, old):
        if key in states:
            return key
        if key[0] == "artifact":
            return None
        same = [k for k in states if k[0] != "artifact" and names[k] == name and name]
        if same:
            return max(same, key=stamps.get)
        holders = [k for k, t in states.items() if t.count(old) == 1]
        return holders[0] if len(holders) == 1 else None

    for message in messages:
        created = _time(message.get("created_at"))
        for index, item in enumerate(message.get("content") or []):
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            kind = _classify_tool(item.get("name"))
            if kind is None:
                continue
            stats["tool_calls"] += 1
            if failed.get(item.get("id")):
                stats["failed_calls"] += 1
                continue
            inputs = item.get("input")
            if not isinstance(inputs, dict):
                stats["shapes_skipped"] += 1
                continue
            space, operation, via = kind
            at = _item_time(item, created)
            locator = f"item:{index}"
            if operation == "create":
                path = inputs.get("path") or inputs.get("file_path")
                if space == "drive":
                    path = inputs.get("title") or inputs.get("name")
                text = next(
                    (
                        inputs[k]
                        for k in ("file_text", "textContent", "content", "body")
                        if isinstance(inputs.get(k), str)
                    ),
                    None,
                )
                if not isinstance(path, str) or text is None:
                    stats["shapes_skipped"] += 1
                    continue
                key = (space, path)
                store(key, _basename(path), text)
                emit(message, locator, via, _basename(path), False, at, text)
            elif operation == "update":
                text = inputs.get("textContent")
                file_id = inputs.get("fileId")
                if not isinstance(text, str) or not isinstance(file_id, str):
                    stats["shapes_skipped"] += 1
                    continue
                name = _s(inputs.get("title"))
                store((space, file_id), name, text)
                emit(message, locator, via, name, False, at, text)
            elif operation == "artifact":
                command = inputs.get("command")
                ident = inputs.get("id")
                if not isinstance(ident, str):
                    stats["shapes_skipped"] += 1
                    continue
                key = ("artifact", ident)
                title = _s(inputs.get("title")) or names.get(key, "")
                if command in ("create", "rewrite") and isinstance(
                    inputs.get("content"), str
                ):
                    store(key, title, inputs["content"])
                    emit(message, locator, via, title, False, at, inputs["content"])
                elif command == "update":
                    _apply_edit(
                        stats,
                        states,
                        store,
                        base_for,
                        key,
                        title,
                        inputs.get("old_str"),
                        inputs.get("new_str"),
                        False,
                        lambda text, base, t=title: emit(
                            message, locator, via, t, True, at, text, base
                        ),
                    )
                else:
                    stats["shapes_skipped"] += 1
            else:  # edit
                path = inputs.get("path") or inputs.get("file_path")
                old = inputs.get("old_str", inputs.get("old_string"))
                new = inputs.get("new_str", inputs.get("new_string"))
                if not isinstance(path, str):
                    stats["shapes_skipped"] += 1
                    continue
                _apply_edit(
                    stats,
                    states,
                    store,
                    base_for,
                    (space, path),
                    _basename(path),
                    old,
                    new,
                    inputs.get("replace_all") is True,
                    lambda text, base, p=path: emit(
                        message, locator, via, _basename(p), True, at, text, base
                    ),
                )
        _message_candidates(message, created, uuid, candidates)
    return candidates, stats


def _apply_edit(stats, states, store, base_for, key, name, old, new, replace_all, emit):
    if not isinstance(old, str) or not old or not isinstance(new, str):
        stats["shapes_skipped"] += 1
        return
    base_key = base_for(key, name, old)
    if base_key is None:
        stats["edits_unanchored"] += 1
        return
    base = states[base_key]
    present = base.count(old)
    if present == 0 or (present > 1 and not replace_all):
        stats["edits_unapplied"] += 1
        return
    text = base.replace(old, new)
    stats["edits_applied"] += 1
    store(key, name, text)
    if text != base:
        emit(text, base)


def _message_candidates(message, created, uuid, candidates):
    """Attachments, fenced blocks and a long human message of one message."""
    if created is None:
        return
    for index, attachment in enumerate(message.get("attachments") or []):
        if not isinstance(attachment, dict):
            continue
        text = attachment.get("extracted_content")
        if isinstance(text, str) and len(text) >= MIN_TEXT_CHARS:
            candidates.append(
                Candidate(
                    conversation=uuid,
                    message=message["uuid"],
                    locator=f"attachment:{index}",
                    via="attachment",
                    name=_s(attachment.get("file_name")) or None,
                    edit=False,
                    created=created,
                    text=text,
                )
            )
    text = message.get("text")
    if not isinstance(text, str):
        return
    for number, found in enumerate(_FENCE_RE.finditer(text)):
        if len(found.group(1)) >= MIN_TEXT_CHARS:
            candidates.append(
                Candidate(
                    conversation=uuid,
                    message=message["uuid"],
                    locator=f"fence:{number}",
                    via="fenced_block",
                    name=None,
                    edit=False,
                    created=created,
                    text=found.group(1),
                )
            )
    if message.get("sender") == "human" and len(text) >= MIN_TEXT_CHARS:
        candidates.append(
            Candidate(
                conversation=uuid,
                message=message["uuid"],
                locator="text",
                via="message_text",
                name=None,
                edit=False,
                created=created,
                text=text,
            )
        )


def project_candidates(export, pattern):
    """Knowledge files of a project whose file name mentions the article."""
    found = []
    for project in export.projects:
        for doc in project.get("docs") or []:
            if not isinstance(doc, dict):
                continue
            filename, content = doc.get("filename"), doc.get("content")
            created = _time(doc.get("created_at"))
            if (
                isinstance(filename, str)
                and isinstance(content, str)
                and created
                and pattern.search(filename.lower())
            ):
                found.append(
                    Candidate(
                        conversation=f"project-{project['uuid']}",
                        message=filename,
                        locator="doc",
                        via="project_doc",
                        name=filename,
                        edit=False,
                        created=created,
                        text=content,
                    )
                )
    return found


def recompute(export, conversation_uuid, message, locator, pattern=None):
    """The candidate at a recorded place, or ``None`` if the export no longer has it.

    ``ci-docs import check`` uses this to confirm that a chat source still holds
    the text whose hash the plan recorded.
    """
    if conversation_uuid.startswith("project-") and pattern is not None:
        for candidate in project_candidates(export, pattern):
            if (candidate.conversation, candidate.message) == (
                conversation_uuid,
                message,
            ):
                return candidate
        return None
    conversation = export.conversation(conversation_uuid)
    if conversation is None:
        return None
    candidates, _ = extract(conversation)
    for candidate in candidates:
        if (candidate.message, candidate.locator) == (message, locator):
            return candidate
    return None
