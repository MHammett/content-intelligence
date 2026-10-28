"""``ci-docs import scan --chat-map`` (issue #430), on an invented export only.

Every conversation, slug and text here is invented. The export is written under
``tmp_path`` in the structure the real claude.ai export has (field names only),
and the map is a TSV written next to it. Nothing reads a real export, map,
conversation or article.
"""

import json
from datetime import timedelta

import pytest

from ci_article_review import chat_export as ce
from ci_article_review import documents_cli, history_import as hi

from .test_chat_export import (
    chat_sources,
    conversation,
    message,
    tool_result,
    tool_use,
)
from .test_history_import import SLUG, drafts_of, put, put_run, when

ELSEWHERE = "another-article"
NAMES = {
    "m1": "Invented chat about the lamp post",
    "m2": "Invented chat about gardening",
    "m3": "Invented chat about the weather",
}


def prose(seed, lines=60):
    """About 540 words of invented prose, unlike any other seed's."""
    return "\n".join(
        f"Sentence {i} of part {seed} mentions subject{seed}x{i} beside "
        f"plain{seed}y{i} words."
        for i in range(lines)
    )


def create(uuid, moment, path, text, tid):
    return message(
        uuid,
        "assistant",
        moment,
        content=[
            tool_use("create_file", {"path": path, "file_text": text}, moment, tid),
            tool_result(tid),
        ],
    )


def replace(uuid, moment, path, old, new, tid):
    return message(
        uuid,
        "assistant",
        moment,
        content=[
            tool_use(
                "str_replace",
                {"path": path, "old_str": old, "new_str": new},
                moment,
                tid,
            ),
            tool_result(tid),
        ],
    )


class Mapped:
    """An export whose three conversations are mapped, and a map that says where."""

    def __init__(self, base):
        self.base = base
        self.dir = base / "export"
        self.map = base / "chat-map.tsv"
        self.history = base / "history"
        self.history.mkdir()
        self.v1 = prose("a")
        self.v2 = prose("b")
        self.path = "/mnt/user-data/outputs/piece-notes.md"
        self.conversations = [
            # Mapped to the slug, and never names it.
            conversation(
                "m1",
                NAMES["m1"],
                [
                    message("m1a", "human", when(1, 9), "Please write the piece."),
                    create("m1b", when(1, 10), self.path, self.v1, "t1"),
                    replace(
                        "m1c",
                        when(1, 11),
                        self.path,
                        "Sentence 7 of part a",
                        "Sentence 7 of part a, revised,",
                        "t2",
                    ),
                    replace(
                        "m1d",
                        when(1, 12),
                        self.path,
                        "Sentence 9 of part a",
                        "Sentence 9 of part a, also revised,",
                        "t3",
                    ),
                ],
                created=when(1, 9),
            ),
            # Names the slug, but the map sends it elsewhere.
            conversation(
                "m2",
                NAMES["m2"],
                [
                    message("m2a", "human", when(2, 9), f"Notes for {SLUG} please."),
                    create("m2b", when(2, 10), f"/tmp/{SLUG}-other.md", self.v2, "t4"),
                ],
                created=when(2, 9),
            ),
            # Names the slug and is mapped to nothing.
            conversation(
                "m3",
                NAMES["m3"],
                [
                    message("m3a", "human", when(3, 9), f"A word about {SLUG}."),
                    create(
                        "m3b", when(3, 10), f"/tmp/{SLUG}-loose.md", prose("c"), "t5"
                    ),
                ],
                created=when(3, 9),
            ),
        ]
        self.rows = [("m1", SLUG), ("m2", ELSEWHERE)]
        self.write()

    def write(self):
        folder = self.dir / "conversations"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "conversations.json").write_text(
            json.dumps(self.conversations), encoding="utf-8"
        )
        self.write_map(self.rows)

    def write_map(self, rows, header="conversation\tslug\tname\tconfidence"):
        lines = [header]
        lines.extend(f"{uuid}\t{slug}\tA name\tsure" for uuid, slug in rows)
        self.map.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def scan(self, **overrides):
        options = dict(
            history_root=self.history, chat_export=self.dir, chat_map=self.map
        )
        options.update(overrides)
        return hi.scan(SLUG, **options)


@pytest.fixture
def mapped(tmp_path):
    return Mapped(tmp_path)


def chat_only(plan):
    return [e for e in drafts_of(plan) if e.get("corroborated") is False]


def versions_of(mapped, plan):
    created = hi.normalise(mapped.v1)
    sha = hi._sha(created)
    return [e for e in drafts_of(plan) if e["sha256"] == sha]


# ---------------------------------------------------------------------------
# The map file
# ---------------------------------------------------------------------------


def test_a_map_is_read_by_column_name_and_extra_columns_are_ignored(tmp_path):
    path = tmp_path / "map.tsv"
    path.write_text(
        'confidence\tslug\tname\tconversation\tnote\nsure\tOne-Slug\t"quoted\tc-1\tx\n'
        "guess\t\tblank slug\tc-2\n"
        "sure\tone-slug\tagain\tc-1\n"
        "\n",
        encoding="utf-8",
    )
    assert ce.load_chat_map(path) == {"c-1": "one-slug"}


def test_a_map_that_sends_one_conversation_to_two_slugs_is_an_error(tmp_path):
    path = tmp_path / "map.tsv"
    path.write_text(
        "conversation\tslug\nc-1\tone-slug\nc-1\ttwo-slug\n", encoding="utf-8"
    )
    with pytest.raises(ce.ChatExportError, match="c-1"):
        ce.load_chat_map(path)


@pytest.mark.parametrize(
    "text, match",
    [
        ("conversation\tname\nc-1\tx\n", "slug"),
        ("slug\tname\none\tx\n", "conversation"),
        ("", "header"),
    ],
)
def test_a_map_without_its_two_columns_is_an_error_that_says_which(
    tmp_path, text, match
):
    path = tmp_path / "map.tsv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ce.ChatExportError, match=match):
        ce.load_chat_map(path)


def test_a_missing_map_is_an_error(tmp_path):
    with pytest.raises(ce.ChatExportError, match="does not exist"):
        ce.load_chat_map(tmp_path / "nope.tsv")


# ---------------------------------------------------------------------------
# Linking by the map
# ---------------------------------------------------------------------------


def test_the_map_decides_which_conversations_are_linked_and_nothing_else_does(mapped):
    plan, stats = mapped.scan()
    linked = {x["conversation"]: x for x in plan["chat"]["linked"]}
    # m1 never names the slug and is linked; m2 names it and is mapped elsewhere;
    # m3 names it and is in no row.
    assert set(linked) == {"m1"}
    assert linked["m1"]["linked_by"] == ["chat-map"]
    assert linked["m1"]["strong"] is True
    assert "m2" not in json.dumps(plan) and "m3" not in json.dumps(plan).replace(
        "m3a", ""
    )
    assert stats["chat"]["chat_map"] is True


def test_without_the_map_a_conversation_that_names_the_slug_is_still_linked(mapped):
    put_run(mapped.history, SLUG, 1, when(5, 9), prose("z"))
    plan, _ = mapped.scan(chat_map=None)
    linked = {x["conversation"]: x["linked_by"] for x in plan["chat"]["linked"]}
    assert set(linked) == {"m2", "m3"}
    assert all("chat-map" not in reasons for reasons in linked.values())
    # Nothing from the chat is kept: it resembles no other source's draft.
    assert not any(chat_sources(e) for e in drafts_of(plan))


def test_a_listed_conversation_is_linked_in_addition_to_the_map(mapped):
    plan, _ = mapped.scan(conversations=["m2"])
    linked = {x["conversation"]: x["linked_by"] for x in plan["chat"]["linked"]}
    assert linked == {"m1": ["chat-map"], "m2": ["listed"]}
    # And one the map also names has both reasons.
    plan, _ = mapped.scan(conversations=["m1"])
    assert plan["chat"]["linked"][0]["linked_by"] == ["chat-map", "listed"]


def test_a_conversation_the_map_names_but_the_export_lacks_is_a_warning(mapped):
    mapped.write_map([*mapped.rows, ("gone-1", SLUG), ("gone-2", SLUG)])
    plan, stats = mapped.scan()
    assert any("2 conversation(s)" in w and "gone-1" in w for w in plan["warnings"])
    assert any("gone-2" in w for w in stats["warnings"])


def test_a_map_that_names_no_conversation_for_the_slug_says_so(mapped):
    mapped.write_map([("m2", ELSEWHERE)])
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        mapped.scan()
    # With another source the scan goes on, with the reason in the plan.
    put_run(mapped.history, SLUG, 1, when(5, 9), prose("z"))
    plan, _ = mapped.scan()
    assert plan["chat"]["linked"] == []
    assert any("--chat-map" in w for w in plan["warnings"])


def test_a_map_needs_an_export(mapped):
    with pytest.raises(hi.ImportScanError, match="--chat-map needs --chat-export"):
        mapped.scan(chat_export=None)


# ---------------------------------------------------------------------------
# Chat-only versions
# ---------------------------------------------------------------------------


def test_a_mapped_conversations_whole_file_is_kept_with_no_other_source(mapped):
    plan, stats = mapped.scan()
    (created,) = versions_of(mapped, plan)
    assert created["corroborated"] is False
    assert created["include"] is True
    assert chat_sources(created)[0]["via"] == "create_file"
    assert chat_sources(created)[0]["path"] == "chat:m1/m1b"
    kept = plan["chat"]["kept"]
    assert kept["chat_only"] == 3
    assert kept["chat_only_by_via"] == {"create_file": 1, "str_replace": 2}
    assert kept["versions_by_via"] == {"create_file": 1, "str_replace": 2}
    assert stats["chat"]["chat_only"] == 3


def test_an_str_replace_chain_on_a_kept_version_is_rebuilt(mapped):
    plan, _ = mapped.scan()
    first = mapped.v1.replace("Sentence 7 of part a", "Sentence 7 of part a, revised,")
    second = first.replace(
        "Sentence 9 of part a", "Sentence 9 of part a, also revised,"
    )
    by_sha = {e["sha256"]: e for e in drafts_of(plan)}
    for text in (first, second):
        found = by_sha[hi._sha(hi.normalise(text))]
        assert chat_sources(found)[0]["via"] == "str_replace"
        assert found["corroborated"] is False
    assert len(drafts_of(plan)) == 3


def test_an_edit_whose_base_was_not_kept_is_not_kept(mapped):
    short = " ".join(f"short{i}" for i in range(40))
    mapped.conversations[0]["chat_messages"] = [
        create("s1", when(1, 10), "/tmp/note.md", short, "u1"),
        replace("s2", when(1, 11), "/tmp/note.md", "short3", "short3 edited", "u2"),
    ]
    mapped.write()
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        mapped.scan()


def test_a_note_under_the_word_floor_is_not_kept_and_the_floor_can_move(mapped):
    note = "\n".join(f"Line {i} of the note says word{i} plainly." for i in range(40))
    mapped.conversations[0]["chat_messages"] = [
        create("n1", when(1, 10), "/tmp/note.md", note, "u1")
    ]
    mapped.write()
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        mapped.scan()  # about 240 words, floor 300
    plan, _ = mapped.scan(min_chat_words=100)
    assert len(chat_only(plan)) == 1
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        mapped.scan(min_chat_words=1000)


def test_the_floor_counts_words_not_marks_or_numbers(mapped):
    marks = "\n".join("| --- | 123 | --- |" for _ in range(200))
    mapped.conversations[0]["chat_messages"] = [
        create("n1", when(1, 10), "/tmp/table.md", marks, "u1")
    ]
    mapped.write()
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        mapped.scan()


def test_a_fragment_is_not_kept_a_fenced_block_a_long_message_or_a_script(mapped):
    body = prose("d")
    mapped.conversations[0]["chat_messages"] = [
        message("f1", "human", when(1, 9), "Here:\n```\n" + body + "\n```\n"),
        message("f2", "human", when(1, 10), prose("e") * 2),
        create("f3", when(1, 11), "/tmp/build.py", prose("f"), "u1"),
        create("f4", when(1, 12), f"/tmp/{SLUG}-metadata.md", prose("g"), "u2"),
    ]
    mapped.write()
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        mapped.scan()


def test_a_rewrite_that_is_a_near_duplicate_of_a_kept_version_is_not_kept(mapped):
    again = mapped.v1.replace("Sentence 3 of part a", "Sentence 3 of part a again", 1)
    mapped.conversations[0]["chat_messages"] = [
        create("r1", when(1, 10), mapped.path, mapped.v1, "u1"),
        create("r2", when(1, 11), mapped.path, again, "u2"),
        create("r3", when(1, 12), mapped.path, prose("h"), "u3"),
    ]
    mapped.write()
    plan, _ = mapped.scan()
    shas = {e["sha256"] for e in drafts_of(plan)}
    assert hi._sha(hi.normalise(mapped.v1)) in shas
    assert hi._sha(hi.normalise(again)) not in shas
    assert hi._sha(hi.normalise(prose("h"))) in shas


def test_artifacts_and_the_connectors_files_are_whole_documents_too(mapped):
    mapped.conversations[0]["chat_messages"] = [
        message(
            "w1",
            "assistant",
            when(1, 10),
            content=[
                tool_use(
                    "artifacts",
                    {
                        "id": "a1",
                        "command": "create",
                        "title": "Piece",
                        "content": prose("i"),
                    },
                    when(1, 10),
                    "u1",
                ),
                tool_use(
                    "mcp__Dropbox__create_file",
                    {"path": "/Apps/piece.md", "content": prose("j")},
                    when(1, 11),
                    "u2",
                ),
                tool_use(
                    "Google Drive:create_file",
                    {"title": "Piece two", "textContent": prose("k")},
                    when(1, 12),
                    "u3",
                ),
                tool_use(
                    "Write",
                    {"file_path": "C:\\w\\piece.md", "content": prose("l")},
                    when(1, 13),
                    "u4",
                ),
            ],
        ),
        message(
            "w2",
            "human",
            when(1, 14),
            "Pasted.",
            attachments=[
                {
                    "file_name": "paste.txt",
                    "file_size": 1,
                    "file_type": "txt",
                    "extracted_content": prose("m"),
                }
            ],
        ),
    ]
    mapped.write()
    plan, _ = mapped.scan()
    by_via = plan["chat"]["kept"]["chat_only_by_via"]
    assert by_via == {
        "artifacts": 1,
        "attachment": 1,
        "drive_create_file": 1,
        "dropbox_create_file": 1,
        "write": 1,
    }


def test_a_version_another_source_holds_keeps_todays_behaviour(mapped):
    put(
        mapped.base / "files",
        f"{SLUG}-draft-v1.md",
        mapped.v1.replace("Sentence 5 of part a", "Sentence 5 of part a, filed,"),
        when(1, 8),
    )
    plan, _ = mapped.scan(files=[mapped.base / "files"])
    created = versions_of(mapped, plan)[0]
    # The chat's text resembles the file's, so it is kept as before: no flag.
    assert "corroborated" not in created
    assert "chat" in [s["type"] for s in created["sources"]]
    assert plan["chat"]["kept"]["chat_only"] == 0


def test_the_timeline_marks_a_chat_only_version(mapped):
    plan, _ = mapped.scan()
    marked = [line for line in hi.format_timeline(plan) if "chat-only" in line]
    assert len(marked) == 3


def test_a_chat_only_version_is_checked_against_the_export(mapped):
    plan, _ = mapped.scan()
    assert hi.check_plan(plan) == []


def test_a_plan_with_chat_only_versions_is_byte_identical_across_two_scans(
    mapped, tmp_path
):
    one, two = tmp_path / "o" / "one.json", tmp_path / "o" / "two.json"
    for path in (one, two):
        plan, _ = mapped.scan()
        hi.write_plan(plan, path)
    assert one.read_bytes() == two.read_bytes()


# ---------------------------------------------------------------------------
# Without --chat-map
# ---------------------------------------------------------------------------


def test_without_the_map_a_content_linked_conversation_keeps_only_what_resembles(
    mapped,
):
    put_run(mapped.history, SLUG, 1, when(5, 9), prose("z"))
    plan, _ = mapped.scan(chat_map=None)
    assert chat_only(plan) == []
    assert not any(chat_sources(e) for e in drafts_of(plan))
    assert "chat_only" not in plan["chat"]["kept"]


def test_a_listed_conversation_gets_the_chat_only_rule_without_a_map(mapped):
    plan, _ = mapped.scan(chat_map=None, conversations=["m1"])
    assert len(chat_only(plan)) == 3
    assert {x["conversation"]: x["linked_by"] for x in plan["chat"]["linked"]}[
        "m1"
    ] == ["listed"]


# ---------------------------------------------------------------------------
# Rounds for a chat-only article
# ---------------------------------------------------------------------------


def spaced(mapped, gaps):
    """A conversation that writes a new whole file after each gap (in hours)."""
    moment = when(1, 10)
    messages = []
    for number, gap in enumerate(gaps):
        moment = moment + timedelta(hours=gap)
        messages.append(
            create(
                f"g{number}",
                moment,
                f"/tmp/piece-{number}.md",
                prose(f"p{number}"),
                f"u{number}",
            )
        )
    mapped.conversations[0]["chat_messages"] = messages
    mapped.write()


def test_chat_only_versions_split_into_a_round_at_each_gap_over_48_hours(mapped):
    spaced(mapped, [0, 5, 47, 49, 3, 120])
    plan, _ = mapped.scan()
    sizes = [len(r["events"]) for r in plan["rounds"]]
    assert sizes == [3, 2, 1]
    assert [r["round"] for r in plan["rounds"]] == [1, 2, 3]
    # accepted_at stays just before the next round's first event.
    second_first = plan["rounds"][1]["events"][0]["at"]
    assert plan["rounds"][0]["accepted_at"] < second_first


def test_exactly_48_hours_is_not_a_gap(mapped):
    spaced(mapped, [0, 48])
    plan, _ = mapped.scan()
    assert len(plan["rounds"]) == 1


def test_chat_only_versions_with_no_gap_are_one_round(mapped):
    plan, _ = mapped.scan()
    assert len(plan["rounds"]) == 1


def test_a_run_turns_the_gap_rule_off(mapped):
    spaced(mapped, [0, 120])
    put_run(mapped.history, SLUG, 1, when(1, 9), prose("z"))
    plan, _ = mapped.scan()
    # The run's draft is a submitted version; rounds follow it, not the gaps.
    assert len(plan["rounds"]) == 1


def test_a_labelled_file_turns_the_gap_rule_off(mapped):
    spaced(mapped, [0, 120])
    put(mapped.base / "files", f"{SLUG}-draft-v1.md", prose("z"), when(1, 8))
    plan, _ = mapped.scan(files=[mapped.base / "files"])
    assert len(plan["rounds"]) == 1


def test_the_gap_rule_leaves_a_scan_without_chat_only_versions_alone(mapped):
    put(mapped.base / "files", f"{SLUG}-draft.md", prose("z"), when(1, 8))
    put(
        mapped.base / "files",
        f"{SLUG}-metadata.md",
        "PRIMARY CLAIM\nA claim.\n",
        when(20),
    )
    plan, _ = mapped.scan(
        chat_export=None, chat_map=None, files=[mapped.base / "files"]
    )
    assert len(plan["rounds"]) == 1


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run_cli(mapped, tmp_path, *extra):
    lines = []
    code = documents_cli.main(
        [
            "import",
            "scan",
            SLUG,
            "--history-root",
            str(mapped.history),
            "--chat-export",
            str(mapped.dir),
            *extra,
            "--out",
            str(tmp_path / "private" / "plan.json"),
        ],
        out=lines.append,
    )
    return code, "\n".join(lines)


def test_the_command_takes_the_map_and_the_floor_and_prints_counts_only(
    mapped, tmp_path
):
    code, printed = run_cli(
        mapped, tmp_path, "--chat-map", str(mapped.map), "--min-chat-words", "100"
    )
    assert code == 0
    assert "1 linked by --chat-map" in printed
    assert "m1  created 2026-03-01" in printed
    assert "linked by chat-map" in printed
    assert "Chat-only versions (no other source holds them): 3" in printed
    assert "chat-only" in printed
    for name in NAMES.values():
        assert name not in printed
    assert "Sentence" not in printed
    plan = json.loads((tmp_path / "private" / "plan.json").read_text(encoding="utf-8"))
    blob = json.dumps(plan)
    for name in NAMES.values():
        assert name not in blob
    assert "Sentence" not in blob
    assert plan["chat"]["min_chat_words"] == 100


def test_the_command_without_the_map_prints_what_it_always_did(mapped, tmp_path):
    put_run(mapped.history, SLUG, 1, when(5, 9), prose("z"))
    code, printed = run_cli(mapped, tmp_path)
    assert code == 0
    assert "conversations searched by content" in printed
    assert "chat-only" not in printed


def test_the_command_refuses_a_map_without_an_export_and_a_bad_floor(mapped, tmp_path):
    lines = []
    code = documents_cli.main(
        [
            "import",
            "scan",
            SLUG,
            "--history-root",
            str(mapped.history),
            "--chat-map",
            str(mapped.map),
            "--out",
            str(tmp_path / "private" / "plan.json"),
        ],
        out=lines.append,
    )
    assert code != 0
    code, printed = run_cli(
        mapped, tmp_path, "--chat-map", str(mapped.map), "--min-chat-words", "-3"
    )
    assert code != 0


# ---------------------------------------------------------------------------
# Side documents and fragments (PR #435)
# ---------------------------------------------------------------------------

ARTICLE_LINES = 400  # about 3,600 words: a section of 50 lines is 12.5% of it


def attachment(uuid, moment, name, text):
    return message(
        uuid,
        "human",
        moment,
        "Here is a file.",
        attachments=[
            {
                "file_name": name,
                "file_size": 1,
                "file_type": "md",
                "extracted_content": text,
            }
        ],
    )


def article_chat(mapped, *, side=(), extra=()):
    """The mapped conversation: a draft and two edits, then what a test adds."""
    base = prose("a", ARTICLE_LINES)
    first = base.replace("Sentence 7 of part a", "Sentence 7 of part a, revised,")
    second = first.replace(
        "Sentence 9 of part a", "Sentence 9 of part a, also revised,"
    )
    mapped.texts = {"base": base, "first": first, "second": second}
    mapped.conversations[0]["chat_messages"] = [
        create("d1", when(1, 10), mapped.path, base, "t1"),
        replace(
            "d2",
            when(1, 11),
            mapped.path,
            "Sentence 7 of part a",
            "Sentence 7 of part a, revised,",
            "t2",
        ),
        replace(
            "d3",
            when(1, 12),
            mapped.path,
            "Sentence 9 of part a",
            "Sentence 9 of part a, also revised,",
            "t3",
        ),
        *side,
        *extra,
    ]
    mapped.write()


def side_documents():
    return [
        create("s1", when(1, 13), "/tmp/verification-log.md", prose("v", 60), "u1"),
        create("s2", when(1, 14), "/tmp/research-batch.md", prose("w", 90), "u2"),
    ]


def sha_of(text):
    return hi._sha(hi.normalise(text))


def by_sha(plan):
    return {e["sha256"]: e for e in drafts_of(plan)}


def test_a_side_document_of_a_linked_chat_is_left_out_with_its_reason(mapped):
    article_chat(mapped, side=side_documents())
    plan, _ = mapped.scan()
    found = by_sha(plan)
    for text in mapped.texts.values():
        assert found[sha_of(text)]["include"] is True
    for name, words in (("v", 60), ("w", 90)):
        left = found[sha_of(prose(name, words))]
        assert left["include"] is False
        assert left["corroborated"] is False
        assert "side document" in left["why"]
        assert "set include to true" in left["why"]
    assert [e["include"] for e in drafts_of(plan)].count(True) == 3
    assert plan["chat"]["kept"]["not_versions"] == {"not_related": 2}
    assert plan["chat"]["kept"]["chat_only"] == 3


def test_a_body_only_version_is_kept_and_a_cut_out_section_is_not(mapped):
    base = prose("a", ARTICLE_LINES)
    body = "\n".join(base.split("\n")[40:])  # the draft without its first tenth
    cut = "\n".join(base.split("\n")[100:150])  # a section: 12.5% of the draft
    article_chat(
        mapped,
        extra=[
            attachment("b1", when(1, 13), f"{SLUG}-body.md", body),
            attachment("c1", when(1, 14), f"{SLUG}-cut.md", cut),
        ],
    )
    plan, _ = mapped.scan()
    found = by_sha(plan)
    assert found[sha_of(body)]["include"] is True
    left = found[sha_of(cut)]
    assert left["include"] is False
    assert left["why"].startswith("a fragment")
    assert plan["chat"]["kept"]["not_versions"] == {"fragment": 1}
    kept = [e["sha256"] for e in drafts_of(plan) if e["include"]]
    assert len(kept) == 4  # the draft, its two edits and the body-only version


def test_the_edit_chain_of_one_message_is_unaffected(mapped):
    article_chat(mapped, side=side_documents())
    plan, _ = mapped.scan()
    found = by_sha(plan)
    chain = [sha_of(t) for t in mapped.texts.values()]
    assert all(found[sha]["include"] for sha in chain)
    assert [found[sha]["at"] for sha in chain] == sorted(
        found[sha]["at"] for sha in chain
    )


def test_a_left_out_document_is_in_the_summary_and_the_timeline(mapped):
    article_chat(mapped, side=side_documents())
    plan, stats = mapped.scan()
    lines = "\n".join(hi.format_summary(stats))
    assert "2 side documents" in lines
    assert "chat:m1/s1" in lines and "chat:m1/s2" in lines
    assert "set include to true" in lines
    assert "Sentence" not in lines
    timeline = hi.format_timeline(plan)
    assert sum("excluded" in line for line in timeline) == 2


def test_the_author_can_put_a_left_out_document_back_by_setting_include(mapped):
    article_chat(mapped, side=side_documents())
    plan, _ = mapped.scan()
    by_sha(plan)[sha_of(prose("v", 60))]["include"] = True
    assert hi.check_plan(plan) == []


def test_a_document_another_source_holds_anchors_the_comparison(mapped):
    base = prose("a", ARTICLE_LINES)
    put(
        mapped.base / "files",
        f"{SLUG}-draft-v1.md",
        base.replace("Sentence 5 of part a", "Sentence 5 of part a, filed,"),
        when(1, 8),
    )
    article_chat(mapped, side=side_documents())
    plan, _ = mapped.scan(files=[mapped.base / "files"])
    found = by_sha(plan)
    # The chat's draft resembles the file's: kept, and not chat-only.
    assert found[sha_of(base)]["include"] is True
    assert "corroborated" not in found[sha_of(base)]
    assert found[sha_of(prose("v", 60))]["include"] is False
    assert found[sha_of(prose("w", 90))]["include"] is False


def test_a_side_document_does_not_move_a_round_boundary(mapped):
    article_chat(
        mapped,
        side=[create("s1", when(2, 18), "/tmp/log.md", prose("v", 60), "u1")],
        extra=[
            replace(
                "d4",
                when(4, 10),
                mapped.path,
                "Sentence 11 of part a",
                "Sentence 11 of part a, much later,",
                "t4",
            )
        ],
    )
    plan, _ = mapped.scan()
    # The edit is 70 hours after the one before it; the side document 30 hours
    # in does not bridge that silence.
    assert len(plan["rounds"]) == 2
    assert [e["include"] for e in plan["rounds"][1]["events"]] == [True]


def test_when_no_two_documents_share_text_none_can_be_told_apart_and_all_stay(
    mapped,
):
    mapped.conversations[0]["chat_messages"] = [
        create("a1", when(1, 10), "/tmp/one.md", prose("p", 60), "u1"),
        create("a2", when(1, 11), "/tmp/two.md", prose("q", 60), "u2"),
    ]
    mapped.write()
    plan, _ = mapped.scan()
    assert [e["include"] for e in drafts_of(plan)] == [True, True]
    assert plan["chat"]["kept"]["undecided"] == 2
    assert any("share no text" in w for w in plan["warnings"])


def test_a_single_document_is_kept_as_before(mapped):
    mapped.conversations[0]["chat_messages"] = [
        create("a1", when(1, 10), "/tmp/one.md", prose("p", 60), "u1")
    ]
    mapped.write()
    plan, _ = mapped.scan()
    assert [e["include"] for e in drafts_of(plan)] == [True]


def test_the_edits_of_a_side_document_are_left_out_with_it(mapped):
    article_chat(
        mapped,
        side=[
            create("s1", when(1, 13), "/tmp/log.md", prose("v", 60), "u1"),
            replace(
                "s2",
                when(1, 14),
                "/tmp/log.md",
                "Sentence 3 of part v",
                "Sentence 3 of part v, amended,",
                "u2",
            ),
        ],
    )
    plan, _ = mapped.scan()
    assert [e["include"] for e in drafts_of(plan)].count(False) == 2
    assert plan["chat"]["kept"]["chat_only"] == 3


def test_the_command_prints_each_left_out_document_with_its_reason(mapped, tmp_path):
    article_chat(mapped, side=side_documents())
    code, printed = run_cli(mapped, tmp_path, "--chat-map", str(mapped.map))
    assert code == 0
    assert "Left out of the draft history, not versions of the article" in printed
    assert "chat:m1/s1" in printed
    assert "Sentence" not in printed


# ---------------------------------------------------------------------------
# Lineage by name, and earlier drafts that a later version expanded (PR #435)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("Piece-Notes.md", "piece-notes"),
        ("piece-notes-v2.md", "piece-notes"),
        ("piece-notes-v4-FINAL.md", "piece-notes"),
        ("piece-notes_draft.md", "piece-notes"),
        ("piece-notes-final-v3.md", "piece-notes"),
        ("piece-notes-2026-03-02.md", "piece-notes"),
        ("piece-notes-20260302.md", "piece-notes"),
        ("piece-notes-superseded-by-v3.md", "piece-notes"),
        ("piece-notes-r2.md", "piece-notes"),
        ("piece-notes-citation-check-r1.md", "piece-notes-citation-check"),
        ("piece-notes-cut.md", "piece-notes-cut"),
        ("piece.md", "piece"),
        ("draft.md", "draft"),
        ("v2.md", "v2"),
        ("My Piece v2", "my piece"),
        (None, None),
        ("", None),
    ],
)
def test_a_name_stem_drops_the_extension_and_trailing_version_markers(name, stem):
    assert hi._name_stem(name) == stem


def lineage_chat(mapped, *, extra=()):
    """A first draft written, then rewritten from scratch under one name stem."""
    rewritten = prose("a", ARTICLE_LINES)
    # v3 expands v2 about 2.2 times, so v2 covers less than half of it
    expanded = prose("a", 900).replace(
        "Sentence 7 of part a", "Sentence 7 of part a, longer,"
    )
    mapped.texts = {
        "first": prose("r", 300),
        "v2": rewritten,
        "v3": expanded,
    }
    mapped.conversations[0]["chat_messages"] = [
        create("l1", when(1, 10), "/tmp/piece.md", mapped.texts["first"], "t1"),
        replace(
            "l2",
            when(1, 10, 5),
            "/tmp/piece.md",
            "Sentence 7 of part r",
            "Sentence 7 of part r, edited,",
            "t2",
        ),
        replace(
            "l3",
            when(1, 10, 9),
            "/tmp/piece.md",
            "Sentence 9 of part r",
            "Sentence 9 of part r, also edited,",
            "t3",
        ),
        create("l4", when(1, 12), "/tmp/piece-v2.md", mapped.texts["v2"], "t4"),
        attachment("l5", when(1, 14), "piece-v3-FINAL.md", mapped.texts["v3"]),
        *extra,
    ]
    mapped.write()


def test_a_heavily_rewritten_first_draft_is_kept_by_its_name_with_its_edits(mapped):
    lineage_chat(mapped)
    plan, _ = mapped.scan()
    found = by_sha(plan)
    first = mapped.texts["first"]
    edited = first.replace("Sentence 7 of part r", "Sentence 7 of part r, edited,")
    edited_again = edited.replace(
        "Sentence 9 of part r", "Sentence 9 of part r, also edited,"
    )
    for text in (first, edited, edited_again):
        assert found[sha_of(text)]["include"] is True
    # The first text is kept by its name; its edits relate to it.
    assert found[sha_of(first)]["note"].startswith("kept by file name")
    for key in ("v2", "v3"):
        assert found[sha_of(mapped.texts[key])]["include"] is True
        assert "note" not in found[sha_of(mapped.texts[key])]
    assert plan["chat"]["kept"]["kept_by_name"] == 1
    assert plan["chat"]["kept"]["not_versions"] == {}
    assert "Kept by file name: 1" in "\n".join(hi.format_summary(mapped.scan()[1]))


def test_a_name_sharing_only_a_prefix_with_the_stem_does_not_keep_a_document(mapped):
    lineage_chat(
        mapped,
        extra=[
            create(
                "p1",
                when(1, 15),
                "/tmp/piece-citation-check-r1.md",
                prose("v", 60),
                "u1",
            ),
            create("p2", when(1, 16), "/tmp/pie.md", prose("w", 60), "u2"),
            create("p3", when(1, 17), "/tmp/piece-pie.md", prose("x", 60), "u3"),
        ],
    )
    plan, _ = mapped.scan()
    found = by_sha(plan)
    for name in ("v", "w", "x"):
        assert found[sha_of(prose(name, 60))]["include"] is False
    assert found[sha_of(mapped.texts["first"])]["include"] is True
    assert plan["chat"]["kept"]["not_versions"] == {"not_related": 3}


def test_a_document_with_the_stem_of_a_file_another_source_holds_is_kept(mapped):
    put(
        mapped.base / "files",
        f"{SLUG}-draft-v1.md",
        prose("a", ARTICLE_LINES),
        when(1, 8),
    )
    mapped.conversations[0]["chat_messages"] = [
        create("n1", when(1, 10), f"/tmp/{SLUG}.md", prose("r", ARTICLE_LINES), "t1"),
        create(
            "n2", when(1, 11), f"/tmp/{SLUG}-citation-check.md", prose("v", 60), "t2"
        ),
    ]
    mapped.write()
    plan, _ = mapped.scan(files=[mapped.base / "files"])
    found = by_sha(plan)
    assert found[sha_of(prose("r", ARTICLE_LINES))]["include"] is True
    assert found[sha_of(prose("v", 60))]["include"] is False


def test_an_earlier_smaller_draft_that_a_later_version_expanded_is_kept(mapped):
    base = prose("a", ARTICLE_LINES)
    small = "\n".join(base.split("\n")[:150])  # 37% of the later draft
    article_chat(mapped, extra=[attachment("e1", when(1, 9), "outline.md", small)])
    plan, _ = mapped.scan()
    found = by_sha(plan)
    assert found[sha_of(small)]["include"] is True
    assert plan["chat"]["kept"]["not_versions"] == {}


def test_a_section_cut_from_an_existing_draft_is_left_out_even_under_its_name(mapped):
    base = prose("a", ARTICLE_LINES)
    cut = "\n".join(base.split("\n")[100:150])
    article_chat(
        mapped,
        extra=[attachment("c1", when(1, 14), "piece-notes-v9.md", cut)],
    )
    plan, _ = mapped.scan()
    left = by_sha(plan)[sha_of(cut)]
    assert left["include"] is False
    assert left["why"].startswith("a fragment")
