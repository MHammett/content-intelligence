"""``ci-docs ls`` and ``ci-docs put``: look at an article folder, and add a file to it (#404).

The GitHub side is the real ``ci_core.github_repo.GitHubRepo`` talking to the
in-process ``FakeGitHub`` of ``test_documents_init.py``, so the folder listing,
the never-overwrite rule and the secret scan are the real ones. What is tested
here is what this phase owns: which names are accepted and which refused, the
close-out ``-vM`` rules, the commit message, and that every refusal that can be
decided locally is decided before a request is sent.

Slugs and contents are invented; this repository is public.
"""

from unittest.mock import patch

import pytest
from .test_documents_init import REPO, SLUG, TOKEN, FakeGitHub

from ci_article_review import documents_cli, github_put as gp
from ci_article_review.github_documents import GitHubDocumentsError

FOLDER = f"articles/{SLUG}"
KEY = "sk-example-provider-key-0123456789"
SETTINGS = {
    "repo": REPO,
    "branch": None,
    "path_prefix": "articles",
    "allow_public_repo": False,
}
API_KEYS = {"openai": {"api_key": KEY}, "languagetool": {"username": "", "api_key": ""}}
NOTE = "CLOSE-OUT HANDOFF\nDirection: to-chat\n\nA placeholder note.\n"


def note(round_number=4, direction="chat", version=1):
    return gp.close_out_name(SLUG, round_number, direction, version)


@pytest.fixture
def fake():
    stub = FakeGitHub()
    stub.external_commit(
        "main",
        {
            f"{FOLDER}/{SLUG}-draft-r4.md": b"# Example\n\nA draft.\n",
            f"{FOLDER}/{SLUG}-metadata-r4.md": b"metadata\n",
            f"{FOLDER}/{SLUG}-review-r4.md": b"a review\n",
            f"{FOLDER}/pipeline/{SLUG}-worklist-r4.md": b"worklist\n",
            f"{FOLDER}/pipeline/run_4_20261026_143022_report.json": b"{}\n",
            "articles/other-article/other-article-draft-r1.md": b"x\n",
        },
        "round 4",
    )
    return stub


def local(tmp_path, name, text=NOTE, *, crlf=False):
    path = tmp_path / name
    data = text.replace("\n", "\r\n") if crlf else text
    path.write_bytes(data.encode("utf-8"))
    return path


def put(fake, path, slug=SLUG, **kw):
    lines = []
    kw.setdefault("settings", SETTINGS)
    kw.setdefault("api_keys", API_KEYS)
    result = gp.put_file(
        slug,
        path,
        token_lookup=lambda: TOKEN,
        session=fake,
        out=lines.append,
        **kw,
    )
    return result, "\n".join(lines)


def refused(fake, path, *, slug=SLUG, sends=False, **kw):
    """The refusal's message; asserts nothing was written (and, unless ``sends``, nothing sent)."""
    commits, writes, requests = len(fake.commits), len(fake.writes), len(fake.requests)
    with pytest.raises(GitHubDocumentsError) as err:
        put(fake, path, slug, **kw)
    assert len(fake.commits) == commits
    assert len(fake.writes) == writes
    if not sends:
        assert len(fake.requests) == requests
    return str(err.value)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, kind, round_number, direction, version",
    [
        (f"{SLUG}-draft-r1.md", "draft", 1, None, None),
        (f"{SLUG}-metadata-r12.md", "metadata", 12, None, None),
        (f"{SLUG}-handoff-r3.md", "handoff", 3, None, None),
        (f"{SLUG}-publication-r6.md", "publication", 6, None, None),
        (f"{SLUG}-close-out-r4-to-code.md", "close-out", 4, "code", 1),
        (f"{SLUG}-close-out-r4-to-chat.md", "close-out", 4, "chat", 1),
        (f"{SLUG}-close-out-r4-to-chat-v2.md", "close-out", 4, "chat", 2),
        (f"{SLUG}-close-out-r10-to-code-v10.md", "close-out", 10, "code", 10),
        (f"{SLUG}-review-r4.md", "review", 4, None, None),
        (f"{SLUG}-worklist-r4.md", "worklist", 4, None, None),
        ("run_4_20261026_143022_report.json", "report", 4, None, None),
        ("run_4_20261026_143022_results.json", "results", 4, None, None),
    ],
)
def test_every_name_in_the_convention_parses(
    name, kind, round_number, direction, version
):
    assert gp.parse_name(SLUG, name) == {
        "kind": kind,
        "round": round_number,
        "direction": direction,
        "version": version,
    }


@pytest.mark.parametrize(
    "name",
    [
        f"{SLUG}-draft-r0.md",
        f"{SLUG}-draft-r04.md",
        f"{SLUG}-draft-r4-old.md",
        f"{SLUG}-draft-r4.txt",
        f"{SLUG}-draft-4.md",
        f"{SLUG}-notes-r4.md",
        f"{SLUG}-close-out-r4.md",
        f"{SLUG}-close-out-r4-to-everyone.md",
        f"{SLUG}-close-out-r4-to-chat-v1.md",
        f"{SLUG}-close-out-r4-to-chat-v02.md",
        f"{SLUG}-close-out-r4-to-chat-v2-old.md",
        f"{SLUG}-close-out-r04-to-chat.md",
        "other-article-draft-r4.md",
        f"x{SLUG}-draft-r4.md",
        "draft-r4.md",
    ],
)
def test_names_outside_the_convention_do_not_parse(name):
    assert gp.parse_name(SLUG, name) is None


def test_the_slug_is_matched_literally_not_as_a_pattern():
    assert gp.parse_name("a.b", "a.b-draft-r1.md")
    assert gp.parse_name("a.b", "axb-draft-r1.md") is None


@pytest.mark.parametrize(
    "name, message",
    [
        (f"{SLUG}-draft-r1.md", "draft: example-article r1"),
        (f"{SLUG}-metadata-r2.md", "metadata: example-article r2"),
        (f"{SLUG}-handoff-r3.md", "handoff: example-article r3"),
        (f"{SLUG}-publication-r6.md", "publication: example-article r6"),
        (f"{SLUG}-close-out-r4-to-chat.md", "close-out: example-article r4 to-chat"),
        (f"{SLUG}-close-out-r4-to-code.md", "close-out: example-article r4 to-code"),
    ],
)
def test_the_commit_message_is_the_documented_one(fake, tmp_path, name, message):
    result, _ = put(fake, local(tmp_path, name))
    assert fake.messages()[-1] == message + "\n"
    assert result.written == (f"{FOLDER}/{name}",)


def test_a_rewrite_names_its_version_in_the_message(fake, tmp_path):
    put(fake, local(tmp_path, note(4, "chat", 1)))
    put(fake, local(tmp_path, note(4, "chat", 2), NOTE + "Rewritten.\n"))
    assert fake.messages()[-1] == "close-out: example-article r4 to-chat v2\n"


# ---------------------------------------------------------------------------
# Refusals decided before anything is sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "other-article-close-out-r4-to-chat.md",  # a wrong slug
        f"{SLUG}-close-out-r4-to-everyone.md",
        f"{SLUG}-notes-r4.md",  # an unknown kind
        f"{SLUG}-draft-r04.md",
        f"{SLUG}-close-out-r4-to-chat-v1.md",
        "NOTES.md",
    ],
)
def test_a_name_outside_the_convention_is_refused_with_the_patterns(
    fake, tmp_path, name
):
    message = refused(fake, local(tmp_path, name))
    assert "close-out-rN-to-(code|chat)[-vM].md" in message
    assert f"{SLUG}-(draft|metadata|handoff|publication)-rN.md" in message


@pytest.mark.parametrize("kind", ["review", "worklist"])
def test_the_pipelines_own_files_are_not_put_by_hand(fake, tmp_path, kind):
    message = refused(fake, local(tmp_path, f"{SLUG}-{kind}-r5.md"))
    assert f"is a {kind} file, which the pipeline writes" in message


def test_the_slug_argument_is_checked_like_ci_review_checks_it(fake, tmp_path):
    message = refused(fake, local(tmp_path, "x.md"), slug="../x")
    assert "article slug" in message


def test_an_empty_or_binary_file_is_refused(fake, tmp_path):
    name = f"{SLUG}-draft-r5.md"
    assert "is empty" in refused(fake, local(tmp_path, name, " \n"))
    (tmp_path / name).write_bytes(b"\xff\xfe\x00bad")
    assert "not UTF-8" in refused(fake, tmp_path / name)


def test_a_missing_local_file_is_refused(fake, tmp_path):
    assert "Cannot read" in refused(fake, tmp_path / f"{SLUG}-draft-r5.md")


def test_no_token_is_refused_before_a_request(fake, tmp_path):
    with pytest.raises(GitHubDocumentsError, match="No GitHub token"):
        gp.put_file(
            SLUG,
            local(tmp_path, note()),
            settings=SETTINGS,
            api_keys=API_KEYS,
            token_lookup=lambda: None,
            session=fake,
        )
    assert fake.requests == []


# ---------------------------------------------------------------------------
# The close-out rules
# ---------------------------------------------------------------------------


def test_a_first_close_out_note_is_written_with_lf_endings(fake, tmp_path):
    result, out = put(fake, local(tmp_path, note(), crlf=True))
    # Local files are CRLF on Windows; the repository is LF.
    assert fake.file(f"{FOLDER}/{note()}") == NOTE.encode()
    assert "Wrote" in out and result.html_url in out
    assert len(result.written) == 1


def test_only_the_one_file_is_in_the_commit(fake, tmp_path):
    before = dict(fake.tree())
    put(fake, local(tmp_path, note()))
    assert set(fake.tree()) - set(before) == {f"{FOLDER}/{note()}"}
    assert all(fake.tree()[p] == before[p] for p in before)


def test_a_second_note_for_the_same_round_and_direction_is_refused_with_the_next_free_v(
    fake, tmp_path
):
    put(fake, local(tmp_path, note()))
    again = tmp_path / "again"
    again.mkdir()
    message = refused(fake, local(again, note(), NOTE + "Changed.\n"), sends=True)
    assert note(4, "chat", 2) in message
    assert "never overwritten" in message
    assert fake.file(f"{FOLDER}/{note()}") == NOTE.encode()


def test_the_suggestion_moves_past_the_highest_version_there(fake, tmp_path):
    put(fake, local(tmp_path, note(4, "chat", 1)))
    put(fake, local(tmp_path, note(4, "chat", 2), NOTE + "v2\n"))
    other = tmp_path / "other"
    other.mkdir()
    message = refused(fake, local(other, note(4, "chat", 1), NOTE + "x\n"), sends=True)
    assert note(4, "chat", 3) in message
    message = refused(fake, local(other, note(4, "chat", 2), NOTE + "y\n"), sends=True)
    assert note(4, "chat", 3) in message


def test_a_plain_note_is_refused_when_only_later_versions_are_there(fake, tmp_path):
    fake.external_commit("main", {f"{FOLDER}/{note(4, 'chat', 2)}": b"v2 text"})
    message = refused(fake, local(tmp_path, note(4, "chat", 1)), sends=True)
    assert note(4, "chat", 3) in message


def test_an_identical_re_put_is_a_no_op(fake, tmp_path):
    put(fake, local(tmp_path, note()))
    commits = len(fake.commits)
    result, out = put(fake, local(tmp_path, note(), crlf=True))
    assert result.written == () and "Nothing new to write" in out
    assert len(fake.commits) == commits


def test_a_v2_whose_predecessor_is_there_succeeds(fake, tmp_path):
    put(fake, local(tmp_path, note()))
    result, _ = put(fake, local(tmp_path, note(4, "chat", 2), NOTE + "v2\n"))
    assert result.written == (f"{FOLDER}/{note(4, 'chat', 2)}",)
    assert fake.file(f"{FOLDER}/{note()}") == NOTE.encode()  # v1 untouched


def test_a_v3_after_a_v2_succeeds(fake, tmp_path):
    put(fake, local(tmp_path, note()))
    put(fake, local(tmp_path, note(4, "chat", 2), NOTE + "v2\n"))
    result, _ = put(fake, local(tmp_path, note(4, "chat", 3), NOTE + "v3\n"))
    assert result.written == (f"{FOLDER}/{note(4, 'chat', 3)}",)


def test_a_v2_with_no_first_note_is_refused_naming_the_missing_one(fake, tmp_path):
    message = refused(fake, local(tmp_path, note(4, "code", 2)), sends=True)
    assert note(4, "code", 1) in message and "has to exist first" in message


def test_a_v3_with_no_v2_is_refused(fake, tmp_path):
    put(fake, local(tmp_path, note()))
    message = refused(fake, local(tmp_path, note(4, "chat", 3)), sends=True)
    assert note(4, "chat", 2) in message


def test_another_direction_or_round_has_its_own_notes(fake, tmp_path):
    put(fake, local(tmp_path, note(4, "chat", 1)))
    put(fake, local(tmp_path, note(4, "code", 1), "to code\n"))
    put(fake, local(tmp_path, note(5, "chat", 1), "round five\n"))
    # A v2 needs the predecessor of its own round and direction.
    refused(fake, local(tmp_path, note(5, "code", 2)), sends=True)


def test_a_note_for_an_article_that_is_not_there_is_refused(fake, tmp_path):
    name = gp.close_out_name("misspelt-article", 1, "chat", 1)
    message = refused(fake, local(tmp_path, name), slug="misspelt-article", sends=True)
    assert "no folder articles/misspelt-article/" in message
    assert "other-article" in message and SLUG in message


# ---------------------------------------------------------------------------
# Drafting files, and the rule shared by every kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["draft", "metadata", "handoff", "publication"])
def test_each_drafting_kind_is_added_to_the_folder(fake, tmp_path, kind):
    name = f"{SLUG}-{kind}-r5.md"
    put(fake, local(tmp_path, name, f"{kind} text\n"))
    assert fake.file(f"{FOLDER}/{name}") == f"{kind} text\n".encode()


def test_a_drafting_file_that_exists_with_other_bytes_is_never_overwritten(
    fake, tmp_path
):
    message = refused(
        fake, local(tmp_path, f"{SLUG}-draft-r4.md", "edited\n"), sends=True
    )
    assert "different content" in message and "Nothing was overwritten" in message
    assert fake.file(f"{FOLDER}/{SLUG}-draft-r4.md") == b"# Example\n\nA draft.\n"


def test_the_same_drafting_file_again_is_a_no_op(fake, tmp_path):
    result, out = put(
        fake, local(tmp_path, f"{SLUG}-draft-r4.md", "# Example\n\nA draft.\n")
    )
    assert result.written == () and "Nothing new" in out


def test_a_first_draft_may_create_the_folder_and_says_so(fake, tmp_path):
    slug = "new-article"
    _, out = put(fake, local(tmp_path, f"{slug}-draft-r1.md", "# New\n"), slug)
    assert "articles/new-article/ is not in" in out and "creates it" in out
    assert fake.file(f"articles/{slug}/{slug}-draft-r1.md") == b"# New\n"


def test_the_path_prefix_decides_where_the_folder_is(fake, tmp_path):
    fake.external_commit("main", {f"notes/{SLUG}/{SLUG}-draft-r1.md": b"x\n"})
    settings = {**SETTINGS, "path_prefix": "notes"}
    put(fake, local(tmp_path, note(1)), settings=settings)
    assert f"notes/{SLUG}/{note(1)}" in fake.tree()


def test_a_branch_is_chosen_with_the_branch_argument(fake, tmp_path):
    fake.external_commit("drafts", {f"{FOLDER}/{SLUG}-draft-r4.md": b"d\n"})
    put(fake, local(tmp_path, note()), branch="drafts")
    assert f"{FOLDER}/{note()}" in fake.tree("drafts")
    assert f"{FOLDER}/{note()}" not in fake.tree("main")


def test_a_name_that_is_not_a_branch_is_refused(fake, tmp_path):
    message = refused(fake, local(tmp_path, note()), sends=True, branch="nope")
    assert "has no branch 'nope'" in message


# ---------------------------------------------------------------------------
# Secrets, and where it will write
# ---------------------------------------------------------------------------


def test_a_configured_api_key_in_the_file_stops_it(fake, tmp_path):
    message = refused(fake, local(tmp_path, note(), NOTE + f"key: {KEY}\n"), sends=True)
    assert "must not be committed" in message and "nothing was sent" in message
    assert KEY not in message


def test_the_token_in_the_file_stops_it(fake, tmp_path):
    message = refused(
        fake, local(tmp_path, note(), NOTE + f"token {TOKEN}\n"), sends=True
    )
    assert "must not be committed" in message
    assert TOKEN not in message


def test_an_empty_api_key_does_not_block_every_file(fake, tmp_path):
    put(fake, local(tmp_path, note()), api_keys={"x": {"api_key": ""}})


def test_a_public_repository_is_refused(tmp_path):
    public = FakeGitHub(private=False)
    public.external_commit("main", {f"{FOLDER}/{SLUG}-draft-r1.md": b"x\n"})
    message = refused(public, local(tmp_path, note(1)), sends=True)
    assert "public repository" in message and "allow_public_repo" in message


def test_allow_public_repo_lets_it_through(tmp_path):
    public = FakeGitHub(private=False)
    public.external_commit("main", {f"{FOLDER}/{SLUG}-draft-r1.md": b"x\n"})
    settings = {**SETTINGS, "allow_public_repo": True}
    put(public, local(tmp_path, note(1)), settings=settings)
    assert f"{FOLDER}/{note(1)}" in public.tree()


def test_a_token_that_cannot_push_is_refused(tmp_path):
    readonly = FakeGitHub(push=False)
    readonly.external_commit("main", {f"{FOLDER}/{SLUG}-draft-r1.md": b"x\n"})
    message = refused(readonly, local(tmp_path, note(1)), sends=True)
    assert "cannot push" in message


def test_a_missing_repository_says_so(tmp_path):
    message = refused(FakeGitHub(exists=False), local(tmp_path, note()), sends=True)
    assert "missing, or not visible" in message


def test_an_unset_repository_is_refused(fake, tmp_path):
    message = refused(fake, local(tmp_path, note()), settings={**SETTINGS, "repo": ""})
    assert "github_documents.repo is empty" in message


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def listing(fake, **kw):
    header, rows = gp.list_article(
        SLUG,
        settings=kw.pop("settings", SETTINGS),
        token_lookup=lambda: TOKEN,
        session=fake,
        **kw,
    )
    return header, rows, gp.format_listing(header, rows)


def test_ls_lists_the_top_level_then_pipeline_with_round_and_kind(fake):
    fake.external_commit(
        "main",
        {
            f"{FOLDER}/{note(4, 'chat', 1)}": b"n\n",
            f"{FOLDER}/{note(4, 'chat', 2)}": b"n2\n",
            f"{FOLDER}/{note(3, 'code', 1)}": b"n3\n",
            f"{FOLDER}/scratch.txt": b"?\n",
        },
    )
    header, rows, lines = listing(fake)
    assert header["repo"] == REPO and header["folder"] == FOLDER
    assert lines[0].startswith(f"{REPO}  {FOLDER}/  main (")
    body = [ln.split() for ln in lines[1:]]
    # Rounds ascend, kinds follow the convention's order, pipeline/ comes last.
    assert body == [
        ["r3", "close-out", "to-code", note(3, "code", 1)],
        ["r4", "draft", f"{SLUG}-draft-r4.md"],
        ["r4", "metadata", f"{SLUG}-metadata-r4.md"],
        ["r4", "close-out", "to-chat", note(4, "chat", 1)],
        ["r4", "close-out", "to-chat", "v2", note(4, "chat", 2)],
        ["r4", "review", f"{SLUG}-review-r4.md"],
        ["-", "other", "scratch.txt"],
        ["pipeline/"],
        ["r4", "worklist", f"{SLUG}-worklist-r4.md"],
        ["r4", "report", "run_4_20261026_143022_report.json"],
    ]
    assert [r["pipeline"] for r in rows].count(True) == 2


def test_ls_does_not_list_the_pipeline_directory_itself_as_a_file(fake):
    _, rows, _ = listing(fake)
    assert "pipeline" not in [r["name"] for r in rows]


def test_ls_sends_only_gets(fake):
    listing(fake)
    assert fake.writes == []


def test_ls_of_a_missing_article_lists_the_ones_that_exist(fake):
    with pytest.raises(GitHubDocumentsError) as err:
        gp.list_article(
            "misspelt-article",
            settings=SETTINGS,
            token_lookup=lambda: TOKEN,
            session=fake,
        )
    assert "no folder articles/misspelt-article/" in str(err.value)
    assert "other-article" in str(err.value)


def test_ls_can_read_another_branch(fake):
    fake.external_commit("drafts", {f"{FOLDER}/{SLUG}-draft-r9.md": b"d\n"})
    _, rows, _ = listing(fake, branch="drafts")
    assert 9 in {r["round"] for r in rows}


def test_ls_with_a_slug_that_is_not_a_slug_is_refused():
    with pytest.raises(GitHubDocumentsError, match="article slug"):
        gp.list_article("a/b", settings=SETTINGS, session=None)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def run_cli(fake, *argv, keys=API_KEYS):
    lines = []
    with (
        patch(
            "ci_article_review.config_loader.load_user_config",
            return_value={"api_keys": keys},
        ),
        patch(
            "ci_article_review.config_loader.load_publication_config", return_value={}
        ),
    ):
        code = documents_cli.main(
            [*map(str, argv)],
            environ={"GITHUB_TOKEN": TOKEN},
            session=fake,
            out=lines.append,
        )
    return code, "\n".join(lines)


def test_ls_command_prints_the_listing(fake, tmp_path):
    code, out = run_cli(
        fake, "ls", SLUG, "--github-repo", REPO, "--config-dir", tmp_path
    )
    assert code == 0
    assert f"{SLUG}-draft-r4.md" in out and "pipeline/" in out


def test_put_command_adds_the_note_and_prints_the_commit(fake, tmp_path):
    path = local(tmp_path, note())
    code, out = run_cli(
        fake, "put", SLUG, path, "--github-repo", REPO, "--config-dir", tmp_path
    )
    assert code == 0
    assert f"Wrote {FOLDER}/{note()} to {REPO}: https://github.com/" in out
    assert fake.messages()[-1] == "close-out: example-article r4 to-chat\n"


def test_put_command_exits_one_with_the_reason_on_stderr(fake, tmp_path, capsys):
    path = local(tmp_path, f"{SLUG}-notes-r4.md")
    code, _ = run_cli(fake, "put", SLUG, path, "--github-repo", REPO)
    assert code == 1
    assert "not a name put accepts" in capsys.readouterr().err
    assert fake.requests == []


def test_put_command_stops_when_the_configuration_cannot_be_read(
    fake, tmp_path, capsys
):
    path = local(tmp_path, note())
    with patch(
        "ci_article_review.config_loader.load_user_config",
        side_effect=ValueError("unset ${OPENAI_API_KEY}"),
    ):
        code = documents_cli.main(
            ["put", SLUG, str(path)], environ={"GITHUB_TOKEN": TOKEN}, session=fake
        )
    assert code == 1
    assert "check the file for API keys" in capsys.readouterr().err
    assert fake.requests == []


def test_put_command_scans_for_the_publications_keys_too(fake, tmp_path):
    path = local(tmp_path, note(), NOTE + "pub-key-0123456789\n")
    with (
        patch(
            "ci_article_review.config_loader.load_user_config",
            return_value={"api_keys": API_KEYS},
        ),
        patch(
            "ci_article_review.config_loader.load_publication_config",
            return_value={"api_keys": {"openai": {"api_key": "pub-key-0123456789"}}},
        ),
    ):
        code = documents_cli.main(
            ["put", SLUG, str(path), "--github-repo", REPO, "--publication", "p"],
            environ={"GITHUB_TOKEN": TOKEN},
            session=fake,
            out=lambda line: None,
        )
    assert code == 1 and fake.writes == []


def test_put_command_refuses_a_clashing_note_with_exit_one(fake, tmp_path, capsys):
    run_cli(fake, "put", SLUG, local(tmp_path, note()), "--github-repo", REPO)
    other = tmp_path / "other"
    other.mkdir()
    code, _ = run_cli(
        fake,
        "put",
        SLUG,
        local(other, note(), NOTE + "changed\n"),
        "--github-repo",
        REPO,
    )
    assert code == 1
    assert note(4, "chat", 2) in capsys.readouterr().err


def test_the_commands_are_subcommands_of_the_one_console_script():
    parser = documents_cli.build_parser()
    assert parser.parse_args(["ls", "a"]).command == "ls"
    assert parser.parse_args(["put", "a", "f.md"]).command == "put"
