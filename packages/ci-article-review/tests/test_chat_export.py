"""``ci-docs import scan --chat-export`` (issue #422), on an invented export only.

The export is written under ``tmp_path`` with the invented slug ``example-article``
and made-up text, in the structure the real claude.ai export has (field names
only). Nothing here reads a real export, conversation or article.
"""

import json
import zipfile
from datetime import timezone

import pytest

from ci_article_review import chat_export as ce
from ci_article_review import documents_cli, history_import as hi

from .test_history_import import (
    OLD_KEY,
    SLUG,
    Sources,
    draft_with,
    drafts_of,
    events,
    iso,
    make_draft,
    put,
    types,
    when,
)

UTC = timezone.utc
TITLE = "An Invented Headline About Draft Tooling"
CONV_NAMES = {
    "a": "Invented chat about the lamp post",
    "b": "Invented chat about gardening",
    "c": "Invented chat about the weather",
    "d": "Invented chat about a headline",
}


# ---------------------------------------------------------------------------
# A synthetic export in the real structure
# ---------------------------------------------------------------------------


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def tool_use(name, inputs, moment, tid, stamped=True):
    item = {
        "type": "tool_use",
        "name": name,
        "input": inputs,
        "id": tid,
        "start_timestamp": stamp(moment) if stamped else None,
        "stop_timestamp": None,
    }
    return item


def tool_result(tid, error=False):
    return {"type": "tool_result", "tool_use_id": tid, "name": "x", "is_error": error}


def message(uuid, sender, moment, text="", content=(), attachments=()):
    return {
        "uuid": uuid,
        "parent_message_uuid": "root",
        "sender": sender,
        "created_at": stamp(moment),
        "updated_at": stamp(moment),
        "text": text,
        "content": [{"type": "text", "text": text}, *content],
        "attachments": list(attachments),
        "files": [],
    }


def conversation(uuid, name, messages, created=None):
    return {
        "uuid": uuid,
        "name": name,
        "summary": "",
        "created_at": stamp(created or when(1)),
        "updated_at": stamp(when(30)),
        "account": {"uuid": "acct"},
        "chat_messages": messages,
    }


def outline():
    return "\n".join(
        f"Outline point {i}: heading{i} covers aspect{i} briefly." for i in range(50)
    )


class Export:
    """The invented export: four conversations that matter and some that do not."""

    def __init__(self, base, sources):
        self.base = base
        self.sources = sources
        self.dir = base / "export"
        self.drafts = {n: make_draft(n) for n in range(60, 80)}
        self.v6 = f"/mnt/user-data/outputs/{SLUG}-draft-v6.md"
        self.conversations = self._conversations()
        self.write()

    def _conversations(self):
        d = self.drafts
        a = conversation(
            "conv-a",
            CONV_NAMES["a"],
            [
                message(
                    "a1",
                    "human",
                    when(9, 20),
                    f"Please keep working on {SLUG}.",
                ),
                # The Drive copy of v2, written a day before the file and the
                # Drive manifest say it existed.
                message(
                    "a2",
                    "assistant",
                    when(9, 20, 5),
                    content=[
                        tool_use(
                            "mcp__Google_Drive__create_file",
                            {
                                "title": f"{SLUG}-draft-v2.md",
                                "textContent": self.sources.drafts[2],
                                "contentMimeType": "text/markdown",
                            },
                            when(9, 20, 5),
                            "t-drive",
                        ),
                        tool_result("t-drive"),
                    ],
                ),
                message(
                    "a3",
                    "assistant",
                    when(12, 10),
                    content=[
                        tool_use(
                            "create_file",
                            {"path": self.v6, "file_text": d[60]},
                            when(12, 10),
                            "t-create",
                        ),
                        tool_result("t-create"),
                    ],
                ),
                # An edit of the file just written: a second version of it.
                message(
                    "a4",
                    "assistant",
                    when(12, 11),
                    content=[
                        tool_use(
                            "str_replace",
                            {
                                "path": self.v6,
                                "old_str": "Paragraph 7 discusses topic7",
                                "new_str": "Paragraph 7 now discusses topic7",
                            },
                            when(12, 11),
                            "t-edit",
                        ),
                        tool_result("t-edit"),
                        # An edit that failed in the chat changes nothing.
                        tool_use(
                            "str_replace",
                            {
                                "path": self.v6,
                                "old_str": "Paragraph 9 discusses topic9",
                                "new_str": "Paragraph 9 never discusses topic9",
                            },
                            when(12, 12),
                            "t-failed",
                        ),
                        tool_result("t-failed", error=True),
                        # An edit of a file the chat never wrote, and one whose
                        # old text is not unique: neither can be rebuilt.
                        tool_use(
                            "str_replace",
                            {
                                "path": "/tmp/never-written.md",
                                "old_str": "Paragraph 3 discusses topic3",
                                "new_str": "x",
                            },
                            when(12, 13),
                            "t-unanchored",
                        ),
                        tool_use(
                            "str_replace",
                            {
                                "path": self.v6,
                                "old_str": "Paragraph",
                                "new_str": "Para",
                            },
                            when(12, 14),
                            "t-ambiguous",
                        ),
                    ],
                ),
                message(
                    "a5",
                    "assistant",
                    when(13, 9),
                    content=[
                        tool_use(
                            "artifacts",
                            {
                                "id": "art-1",
                                "command": "create",
                                "type": "text/markdown",
                                "title": "Draft",
                                "content": d[61],
                                "version_uuid": "va",
                            },
                            when(13, 9),
                            "t-art1",
                        ),
                        tool_use(
                            "artifacts",
                            {
                                "id": "art-1",
                                "command": "update",
                                "old_str": "Paragraph 11 discusses topic11",
                                "new_str": "Paragraph 11 revisits topic11",
                                "version_uuid": "vb",
                            },
                            when(13, 10),
                            "t-art2",
                        ),
                    ],
                ),
                message(
                    "a6",
                    "human",
                    when(14, 9),
                    "Here is my edited version:\n" + d[62],
                ),
                message(
                    "a7",
                    "assistant",
                    when(14, 10),
                    "An outline:\n```\n" + outline() + "\n```\nThat is the plan.",
                ),
                message(
                    "a8",
                    "assistant",
                    when(16, 10),
                    content=[
                        tool_use(
                            "Google Drive:create_file",
                            {"title": f"{SLUG}-draft.md", "textContent": d[64]},
                            when(16, 10),
                            "t-drive2",
                        )
                    ],
                ),
                message(
                    "a9",
                    "human",
                    when(17, 9),
                    "Pasted.",
                    attachments=[
                        {
                            "file_name": f"{SLUG}-paste.md",
                            "file_size": 1,
                            "file_type": "md",
                            "extracted_content": d[65],
                        }
                    ],
                ),
                message(
                    "a10",
                    "assistant",
                    when(18, 9),
                    content=[
                        tool_use(
                            "Write",
                            {
                                "file_path": f"C:\\work\\{SLUG}-draft-r7.md",
                                "content": d[66],
                            },
                            when(18, 9),
                            "t-write",
                        ),
                        tool_use(
                            "Edit",
                            {
                                "file_path": f"C:\\work\\{SLUG}-draft-r7.md",
                                "old_string": "Paragraph 12 discusses topic12",
                                "new_string": "Paragraph 12 dwells on topic12",
                                "replace_all": False,
                            },
                            when(18, 10),
                            "t-edit2",
                        ),
                    ],
                ),
                message(
                    "a11",
                    "assistant",
                    when(27, 12),
                    content=[
                        tool_use(
                            "mcp__Dropbox__create_file",
                            {"path": f"/Apps/{SLUG}-draft-v9.md", "content": d[63]},
                            when(27, 12),
                            "t-dropbox",
                        )
                    ],
                ),
            ],
            created=when(9, 20),
        )
        b = conversation(
            "conv-b",
            CONV_NAMES["b"],
            [
                message("b1", "human", when(11), "Tell me about roses."),
                message("b2", "human", when(11, 1), self.drafts[70]),
            ],
        )
        c = conversation(
            "conv-c",
            CONV_NAMES["c"],
            [message("c1", "human", when(19, 9), "Edited:\n" + self.drafts[71])],
            created=when(19, 9),
        )
        d = conversation(
            "conv-d",
            CONV_NAMES["d"],
            [
                message("d1", "human", when(21, 9), f"I am writing: {TITLE.lower()}."),
                message("d2", "human", when(21, 10), self.drafts[72]),
            ],
            created=when(21, 9),
        )
        return [a, b, c, d]

    def write(self):
        folder = self.dir / "conversations"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "conversations.json").write_text(
            json.dumps(self.conversations), encoding="utf-8"
        )
        projects = self.dir / "projects" / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "project-1.json").write_text(
            json.dumps(
                {
                    "uuid": "proj-1",
                    "name": "Invented project",
                    "description": "",
                    "prompt_template": "Be brief.",
                    "docs": [
                        {
                            "filename": f"{SLUG}-draft-v3-knowledge.md",
                            "content": self.drafts[73],
                            "created_at": stamp(when(15, 12)),
                        },
                        {
                            "filename": "style-guide.md",
                            "content": self.drafts[74],
                            "created_at": stamp(when(1)),
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def sources(tmp_path):
    src = Sources(tmp_path)
    # The newest metadata names the article's title.
    put(
        src.files,
        f"{SLUG}-v8-metadata.md",
        f"Article: {TITLE}\nPRIMARY CLAIM\nAn invented claim.\n",
        when(25),
    )
    return src


@pytest.fixture
def export(tmp_path, sources):
    return Export(tmp_path, sources)


def scan(sources, export, **overrides):
    options = dict(chat_export=export.dir)
    options.update(overrides)
    return sources.scan(**options)


def chat_sources(event):
    return [s for s in event["sources"] if s["type"] == "chat"]


def chat_versions(plan):
    return [e for e in drafts_of(plan) if chat_sources(e)]


def edited(text, old, new):
    assert text.count(old) == 1
    return text.replace(old, new)


# ---------------------------------------------------------------------------
# Reading the export
# ---------------------------------------------------------------------------


def test_a_directory_a_zip_and_a_bare_file_read_alike(export, tmp_path):
    from_dir = ce.load_export(export.dir)
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "conversations.json",
            (export.dir / "conversations" / "conversations.json").read_text(
                encoding="utf-8"
            ),
        )
    from_zip = ce.load_export(archive)
    bare = ce.load_export(export.dir / "conversations" / "conversations.json")
    nested = tmp_path / "outer.zip"
    with zipfile.ZipFile(nested, "w") as zf:
        zf.write(archive, "conversations-000.zip")
    from_nested = ce.load_export(nested)
    uuids = [c["uuid"] for c in from_dir.conversations]
    assert uuids == ["conv-a", "conv-b", "conv-c", "conv-d"]
    for other in (from_zip, bare, from_nested):
        assert [c["uuid"] for c in other.conversations] == uuids
    assert len(from_dir.projects) == 1 and not bare.projects


def test_a_missing_or_empty_export_is_an_error_with_a_message(tmp_path):
    with pytest.raises(ce.ChatExportError, match="does not exist"):
        ce.load_export(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "conversations.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ce.ChatExportError, match="no conversations"):
        ce.load_export(empty)


def test_a_shape_the_parser_does_not_know_is_skipped_and_counted(
    sources, export, tmp_path
):
    data = json.loads(
        (export.dir / "conversations" / "conversations.json").read_text(
            encoding="utf-8"
        )
    )
    data.append({"uuid": "conv-x"})  # no chat_messages
    data.append("not a conversation")
    data[0]["chat_messages"].append("not a message")
    data[0]["chat_messages"].append(
        {"uuid": "odd", "created_at": "never", "content": "a string", "text": 5}
    )
    data[0]["chat_messages"][1]["content"].append(
        tool_use("create_file", {"path": 5, "file_text": None}, when(9, 21), "t-odd")
    )
    (export.dir / "conversations" / "conversations.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    plan, stats = scan(sources, export)
    assert plan["chat"]["skipped"] == {"conversations of an unknown shape": 2}
    assert plan["chat"]["read"]["shapes_skipped"] >= 1
    # Everything the parser does know is unaffected.
    assert len(chat_versions(plan)) >= 8


def test_a_download_kept_zipped_and_extracted_is_read_once(export, tmp_path):
    folder = tmp_path / "both"
    (folder / "conversations").mkdir(parents=True)
    source = export.dir / "conversations" / "conversations.json"
    (folder / "conversations" / "conversations.json").write_bytes(source.read_bytes())
    with zipfile.ZipFile(folder / "conversations-000.zip", "w") as zf:
        zf.writestr("conversations.json", source.read_text(encoding="utf-8"))
    loaded = ce.load_export(folder)
    assert [c["uuid"] for c in loaded.conversations] == [
        "conv-a",
        "conv-b",
        "conv-c",
        "conv-d",
    ]
    assert loaded.skipped == {}  # the zip was not read again, so nothing was "twice"


def test_a_conversation_listed_twice_is_kept_once_and_counted(export, tmp_path):
    data = json.loads(
        (export.dir / "conversations" / "conversations.json").read_text(
            encoding="utf-8"
        )
    )
    path = tmp_path / "twice.json"
    path.write_text(json.dumps([*data, data[0]]), encoding="utf-8")
    loaded = ce.load_export(path)
    assert len(loaded.conversations) == 4
    assert loaded.skipped == {"conversations listed twice": 1}


def test_conversations_are_linked_by_what_they_say_and_why(export):
    loaded = ce.load_export(export.dir)
    pattern = hi._term_pattern({SLUG})
    links = ce.link_conversations(loaded, pattern, TITLE)
    reasons = {link.uuid: link.reasons for link in links}
    assert set(reasons) == {"conv-a", "conv-d"}
    assert "text" in reasons["conv-a"] and "tool file name" in reasons["conv-a"]
    assert reasons["conv-d"] == ("title",)


# ---------------------------------------------------------------------------
# Linking a conversation to the article
# ---------------------------------------------------------------------------


def test_an_unrelated_conversation_does_not_link(sources, export):
    plan, _ = scan(sources, export)
    linked = {link["conversation"] for link in plan["chat"]["linked"]}
    assert "conv-b" not in linked
    # Its draft-like text is in no event.
    blob = json.dumps(plan)
    assert "conv-b" not in blob


def test_a_conversation_listed_by_uuid_is_linked_though_it_names_nothing(
    sources, export
):
    plan, _ = scan(sources, export)
    assert "conv-c" not in {x["conversation"] for x in plan["chat"]["linked"]}
    plan, _ = scan(sources, export, conversations=["conv-c"])
    link = next(x for x in plan["chat"]["linked"] if x["conversation"] == "conv-c")
    assert link["linked_by"] == ["listed"] and link["strong"] is True
    version = draft_with(plan, "Edited:\n" + export.drafts[71])
    assert version["include"] is True
    assert chat_sources(version)[0]["path"] == "chat:conv-c/c1"


def test_a_listed_uuid_the_export_lacks_is_an_error(sources, export):
    with pytest.raises(hi.ImportScanError, match="conv-zzz"):
        scan(sources, export, conversations=["conv-zzz"])


def test_a_listed_conversation_needs_an_export(sources):
    with pytest.raises(hi.ImportScanError, match="needs --chat-export"):
        sources.scan(conversations=["conv-c"])


def test_a_conversation_linked_only_by_title_is_left_out_with_a_reason(sources, export):
    plan, stats = scan(sources, export)
    link = next(x for x in plan["chat"]["linked"] if x["conversation"] == "conv-d")
    assert link["linked_by"] == ["title"] and link["strong"] is False
    version = draft_with(plan, export.drafts[72])
    assert version["include"] is False
    assert "only by its title" in version["why"]
    assert stats["chat"]["left_out"] == 1


def test_the_title_comes_from_the_newest_metadata(sources, export, tmp_path):
    collector = hi._Collector()
    hi._scan_files(collector, [sources.files], {SLUG, OLD_KEY}, [])
    assert hi._article_title(collector) == TITLE


# ---------------------------------------------------------------------------
# Versions rebuilt from tool calls, attachments and messages
# ---------------------------------------------------------------------------


def test_a_create_file_and_the_str_replace_after_it_are_two_versions(sources, export):
    plan, _ = scan(sources, export)
    created = draft_with(plan, export.drafts[60])
    after_edit = draft_with(
        plan,
        edited(
            export.drafts[60],
            "Paragraph 7 discusses topic7",
            "Paragraph 7 now discusses topic7",
        ),
    )
    assert chat_sources(created)[0]["via"] == "create_file"
    assert chat_sources(after_edit)[0]["via"] == "str_replace"
    assert chat_sources(created)[0]["path"] == "chat:conv-a/a3"
    assert chat_sources(after_edit)[0]["path"] == "chat:conv-a/a4"
    assert chat_sources(created)[0]["locator"] == "item:1"
    # Times are the tool call's own, in UTC.
    assert created["at"] == iso(when(12, 10))
    assert after_edit["at"] == iso(when(12, 11))


def test_an_edit_that_failed_or_cannot_be_rebuilt_is_counted_not_guessed(
    sources, export
):
    plan, _ = scan(sources, export)
    read = plan["chat"]["read"]
    assert read["failed_calls"] == 1
    assert read["edits_unanchored"] == 1
    assert read["edits_unapplied"] == 1
    # Neither failed nor unrebuilt edit produced a version.
    never = export.drafts[60].replace(
        "Paragraph 9 discusses topic9", "Paragraph 9 never discusses topic9"
    )
    sha = hi._sha(hi.normalise(never))
    assert all(e["sha256"] != sha for e in drafts_of(plan))


def test_an_artifact_and_its_update_are_two_versions(sources, export):
    plan, _ = scan(sources, export)
    first = draft_with(plan, export.drafts[61])
    second = draft_with(
        plan,
        edited(
            export.drafts[61],
            "Paragraph 11 discusses topic11",
            "Paragraph 11 revisits topic11",
        ),
    )
    assert chat_sources(first)[0]["via"] == "artifacts"
    assert chat_sources(second)[0]["via"] == "artifacts"
    assert second["at"] == iso(when(13, 10))


def test_the_connectors_and_claude_codes_tools_are_read_under_every_spelling(
    sources, export
):
    plan, _ = scan(sources, export)
    via = {
        text: chat_sources(draft_with(plan, text))[0]["via"]
        for text in (
            export.drafts[64],
            export.drafts[63],
            export.drafts[66],
        )
    }
    assert via == {
        export.drafts[64]: "drive_create_file",  # "Google Drive:create_file"
        export.drafts[63]: "dropbox_create_file",  # "mcp__Dropbox__create_file"
        export.drafts[66]: "write",
    }
    # Claude Code's Edit rebuilt the edit of the file it had just written.
    draft_with(
        plan,
        edited(
            export.drafts[66],
            "Paragraph 12 discusses topic12",
            "Paragraph 12 dwells on topic12",
        ),
    )


def test_attachments_and_a_long_human_message_are_candidates_after_the_tools(
    sources, export
):
    plan, _ = scan(sources, export)
    pasted = draft_with(plan, export.drafts[65])
    assert chat_sources(pasted)[0]["via"] == "attachment"
    assert chat_sources(pasted)[0]["locator"] == "attachment:0"
    message_text = hi.normalise("Here is my edited version:\n" + export.drafts[62])
    found = [e for e in drafts_of(plan) if e["sha256"] == hi._sha(message_text)]
    assert len(found) == 1
    assert chat_sources(found[0])[0]["via"] == "message_text"


def test_a_project_knowledge_file_named_for_the_article_is_a_candidate(sources, export):
    plan, _ = scan(sources, export)
    doc = draft_with(plan, export.drafts[73])
    assert chat_sources(doc)[0]["via"] == "project_doc"
    assert chat_sources(doc)[0]["path"].startswith("chat:project-proj-1/")
    # A knowledge file that does not name the article is not read.
    sha = hi._sha(hi.normalise(export.drafts[74]))
    assert all(e["sha256"] != sha for e in drafts_of(plan))


def test_an_outline_is_dropped_and_counted(sources, export):
    plan, stats = scan(sources, export)
    sha = hi._sha(hi.normalise(outline()))
    assert all(e["sha256"] != sha for e in events(plan))
    assert stats["chat"]["dropped"] >= 1
    assert plan["chat"]["kept"]["dropped_by_via"].get("fenced_block") == 1


def test_a_whole_draft_is_told_from_an_excerpt_an_outline_and_talk():
    ref = frozenset(f"word{i}" for i in range(100))
    assert hi._resembles(frozenset(f"word{i}" for i in range(95)), [ref])
    # An excerpt: every token is in the draft, but it is under half of it.
    assert not hi._resembles(frozenset(f"word{i}" for i in range(40)), [ref])
    # An outline shares too few of its tokens.
    assert not hi._resembles(
        frozenset(f"word{i}" for i in range(40))
        | frozenset(f"o{i}" for i in range(90)),
        [ref],
    )
    assert not hi._resembles(frozenset(), [ref])
    assert not hi._resembles(ref, [])


def test_the_counts_say_what_was_found_per_tool_and_what_was_new(sources, export):
    plan, stats = scan(sources, export)
    chat = stats["chat"]
    kept = plan["chat"]["kept"]
    assert chat["conversations"] == 4 and len(chat["linked"]) == 2
    assert kept["versions_by_via"] == {
        "artifacts": 2,
        "attachment": 1,
        "create_file": 1,
        "drive_create_file": 2,
        "dropbox_create_file": 1,
        "edit": 1,
        "message_text": 2,  # a human paste; and the title-only conversation's
        "project_doc": 1,
        "str_replace": 1,
        "write": 1,
    }
    # The Drive copy of v2 is the one text another source already held.
    assert kept["duplicates_of_other_sources"] == 1
    assert kept["new"] == kept["versions"] - 1
    # The edits in this chat: str_replace, the artifact update and Claude Code's Edit.
    assert kept["in_round_edits"] == 3
    assert kept["left_out"] == 1


# ---------------------------------------------------------------------------
# Merging with the other sources
# ---------------------------------------------------------------------------


def test_a_chat_copy_of_a_known_version_adds_its_source_and_an_earlier_time(
    sources, export
):
    before, _ = sources.scan()
    plan, _ = scan(sources, export)
    d2_before = draft_with(before, sources.drafts[2])
    d2 = draft_with(plan, sources.drafts[2])
    assert types(d2_before) == ["drive", "file", "run", "run"]
    assert types(d2) == ["chat", "drive", "file", "run", "run"]
    assert d2["sha256"] == d2_before["sha256"] and d2["id"] is not None
    assert d2_before["at"] == "2026-03-10T08:30:00Z"
    assert d2["at"] == iso(when(9, 20, 5))  # the chat held the text first
    assert d2["label"] == "v2"


def test_chat_edits_are_revisions_inside_a_round_not_rounds_of_their_own(
    sources, export
):
    before, _ = sources.scan()
    plan, _ = scan(sources, export)
    created = draft_with(plan, export.drafts[60])
    assert (
        created["submitted"] is False
    )  # its file name says v6, but it is a chat's file
    assert all(
        draft_with(plan, t)["submitted"] is False
        for t in (export.drafts[61], export.drafts[64], export.drafts[65])
    )
    # A file the author put in the project, named v3, is a submission: it starts
    # a round. Nothing a chat's own tool calls wrote does.
    knowledge = draft_with(plan, export.drafts[73])
    assert knowledge["submitted"] is True and knowledge["label"] == "v3"
    assert len(plan["rounds"]) == len(before["rounds"]) + 1


def test_chat_edit_chains_are_not_listed_as_near_duplicates(sources, export):
    plan, _ = scan(sources, export)
    chat_only = {e["id"] for e in drafts_of(plan) if types(e) == ["chat"]}
    assert chat_only
    for a, b in plan["near_duplicates"]:
        assert not (a in chat_only and b in chat_only)


def test_the_summary_prints_ids_dates_and_counts_never_names_or_text(
    sources, export, tmp_path
):
    lines = []
    code = documents_cli.main(
        [
            "import",
            "scan",
            SLUG,
            "--history-key",
            SLUG,
            "--history-key",
            OLD_KEY,
            "--history-root",
            str(sources.history),
            "--files",
            str(sources.files),
            "--drive-dir",
            str(sources.drive),
            "--chat-export",
            str(export.dir),
            "--conversation",
            "conv-c",
            "--out",
            str(tmp_path / "private" / "plan.json"),
        ],
        out=lines.append,
    )
    printed = "\n".join(lines)
    assert code == 0
    assert "Chat export: 4 conversations searched by content" in printed
    assert "conv-a  created 2026-03-09  11 messages" in printed
    assert "linked by listed" in printed
    assert "Reproductions: 0 confirmed by the export, 0 replaced by it" in printed
    assert "  chat " in printed  # a row of its own in the per-source table
    for name in CONV_NAMES.values():
        assert name not in printed
    assert "Paragraph" not in printed and "Outline point" not in printed
    plan = json.loads((tmp_path / "private" / "plan.json").read_text(encoding="utf-8"))
    blob = json.dumps(plan)
    for name in CONV_NAMES.values():
        assert name not in blob
    assert "Paragraph" not in blob and "Outline point" not in blob


# ---------------------------------------------------------------------------
# Reproductions (--recovered): confirmed or replaced by the export
# ---------------------------------------------------------------------------


@pytest.fixture
def reproduced(sources, export):
    # v6: exactly the bytes the chat wrote (confirmed).
    put(sources.recovered, f"{SLUG}-draft-v6.md", export.drafts[60], when(30))
    # v9: not the bytes the chat's Dropbox copy has (replaced by it).
    put(
        sources.recovered,
        f"{SLUG}-draft-v9.md",
        edited(
            export.drafts[63], "Paragraph 4 discusses topic4", "Paragraph 4 differs"
        ),
        when(30),
    )
    return sources


def test_an_exact_hash_match_in_the_export_confirms_a_reproduction(reproduced, export):
    plan, stats = scan(reproduced, export, recovered=reproduced.recovered)
    v6 = draft_with(plan, export.drafts[60])
    assert "recovered" not in v6  # the flag is dropped
    assert types(v6) == ["chat", "recovered"]
    assert v6["at"] == iso(when(12, 10))  # the chat's time, not the reproduction's
    assert stats["chat"]["confirmed"] == 1


def test_a_different_export_version_for_the_same_label_replaces_the_reproduction(
    reproduced, export
):
    plan, stats = scan(reproduced, export, recovered=reproduced.recovered)
    v9 = draft_with(plan, export.drafts[63])
    replaced = [e for e in drafts_of(plan) if e.get("superseded_by") == v9["id"]]
    assert len(replaced) == 1
    assert replaced[0]["include"] is False and replaced[0]["recovered"] is True
    assert "recovered" not in v9
    assert stats["chat"]["replaced"] == 1
    lines = hi.format_summary(stats)
    assert any("1 confirmed by the export, 1 replaced by it" in line for line in lines)


def test_a_reproduction_outside_the_exports_time_window_stays_flagged(sources, export):
    # Labelled v6 like the chat's file, but it claims February: the export's copy
    # is a month later, so it cannot be the version the reproduction copied.
    text = edited(
        export.drafts[60], "Paragraph 4 discusses topic4", "Paragraph 4 differs"
    )
    put(sources.recovered, f"{SLUG}-draft-v6.md", text, when(30))
    (sources.recovered / f"{SLUG}-draft-v6.md.json").write_text(
        json.dumps({"claimed_at": "2026-02-01T10:00:00Z"}), encoding="utf-8"
    )
    plan, stats = scan(sources, export, recovered=sources.recovered)
    kept = next(e for e in drafts_of(plan) if e.get("recovered") and e["include"])
    assert kept["label"] == "v6" and kept["at"] == "2026-02-01T10:00:00Z"
    assert stats["chat"]["confirmed"] == 0 and stats["chat"]["replaced"] == 0


def test_a_reproduction_whose_neighbours_put_the_export_out_of_order_stays_flagged(
    sources, export
):
    # Another source dates v5 after the chat's "v6": the chat's file cannot be v6.
    put(sources.files, f"{SLUG}-v5-draft.md", make_draft(5), when(20, 12))
    text = edited(
        export.drafts[60], "Paragraph 4 discusses topic4", "Paragraph 4 differs"
    )
    put(sources.recovered, f"{SLUG}-draft-v6.md", text, when(30))
    plan, stats = scan(sources, export, recovered=sources.recovered)
    kept = next(e for e in drafts_of(plan) if e.get("recovered") and e["include"])
    assert kept["label"] == "v6"
    assert stats["chat"]["replaced"] == 0


# ---------------------------------------------------------------------------
# The plan, and check
# ---------------------------------------------------------------------------


def test_the_plan_is_byte_identical_across_two_scans_of_one_export(
    sources, export, tmp_path
):
    one, two = tmp_path / "o" / "one.json", tmp_path / "o" / "two.json"
    for path in (one, two):
        plan, _ = scan(sources, export, conversations=["conv-c"])
        hi.write_plan(plan, path)
    assert one.read_bytes() == two.read_bytes()


def test_check_rebuilds_a_chat_version_from_the_export(sources, export, tmp_path):
    plan, _ = scan(sources, export)
    assert hi.check_plan(plan) == []
    # An included event that only the export holds is checked against the export.
    only_chat = next(
        e
        for e in drafts_of(plan)
        if types(e) == ["chat"]
        and e["include"]
        and chat_sources(e)[0]["via"] == "str_replace"
    )
    assert only_chat


def test_check_says_so_when_the_export_is_gone_or_changed(sources, export, tmp_path):
    plan, _ = scan(sources, export)
    chat_only = [e for e in drafts_of(plan) if types(e) == ["chat"] and e["include"]]
    assert chat_only
    # Changed: edit a conversation so a version no longer rebuilds the same.
    path = export.dir / "conversations" / "conversations.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data[0]["chat_messages"][3]["content"]:
        if item.get("id") == "t-edit":
            item["input"]["new_str"] = "Paragraph 7 changed topic7"
    path.write_text(json.dumps(data), encoding="utf-8")
    problems = hi.check_plan(plan)
    assert any(
        "no source file is present with the recorded hash" in p for p in problems
    )
    # Gone: the export is moved away.
    path.unlink()
    problems = hi.check_plan(plan)
    assert any("no conversations found" in p or "does not exist" in p for p in problems)


def test_check_does_not_read_the_export_when_a_file_holds_the_version(
    sources, export, monkeypatch
):
    plan, _ = scan(sources, export)
    d2 = draft_with(plan, sources.drafts[2])
    assert "chat" in types(d2) and "file" in types(d2)
    for event in events(plan):
        if event["kind"] == "draft" and types(event) == ["chat"]:
            event["include"] = False  # the author leaves out what only the chat holds

    def refuse(*args, **kwargs):
        raise AssertionError("the export was read")

    monkeypatch.setattr(ce, "load_export", refuse)
    assert hi.check_plan(plan) == []
