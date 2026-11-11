"""``ci-docs ls`` and ``ci-docs put``: an article's rounds, and one file on the open round (#418).

The GitHub side is the real ``ci_core.github_repo.GitHubRepo`` talking to
ci-core's in-process ``FakeGitHub`` (see ``github_fake``), so the listing, the
``replace=`` guard, the secret scan and the fast-forward commit are the real
ones. What is tested here is what this phase owns: which names ``put`` accepts
and refuses, that it commits to the round's branch and never to ``main``, the
replace-or-refuse rules, the commit message, the announcement on the pull
request, and what ``ls`` shows without writing.

The per-round names (``<slug>-close-out-r4-to-chat-v2.md``) and the ``-vM``
rules they came with are retired and have no tests.

Slugs and contents are invented; this repository is public.
"""

from unittest.mock import patch

import pytest
from ci_core.github_repo import GitHubRepo, Signature

from ci_article_review import documents_cli, github_put as gp, github_rounds
from ci_article_review.github_documents import GitHubDocumentsError

from .github_fake import (
    FOLDER,
    REPO,
    SETTINGS,
    SLUG,
    TOKEN,
    FakeGitHub,
    Resp,
    client,
    make_round,
    non_get,
)

KEY = "sk-example-provider-key-0123456789"
API_KEYS = {"openai": {"api_key": KEY}, "languagetool": {"username": "", "api_key": ""}}
NOTE = (
    "CLOSE-OUT HANDOFF\n"
    "Direction: to-chat\n"
    "Article: Example Article\n"
    "Contains: 3 edits, 1 optional, 2 questions, 1 decision, 0 checks, 2 locks\n\n"
    "A placeholder note.\n"
)
BRANCH = f"round/{SLUG}/r2"


@pytest.fixture
def fake():
    stub = FakeGitHub()
    gh = client(stub)
    # Round 1 accepted (merged) and round 2 open: the usual state of a put.
    make_round(stub, gh, 1, merged=True)
    make_round(
        stub,
        gh,
        2,
        files={
            f"{FOLDER}/draft.md": b"# Example\n\nThe draft.\n",
            f"{FOLDER}/metadata.md": b"meta\n",
        },
    )
    # Round 1 is tagged already, so what put writes is only what put writes.
    github_rounds.tag_merged_rounds(gh, SLUG)
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


def refused(fake, path, *, slug=SLUG, **kw):
    """The refusal's message; asserts nothing was written."""
    writes = len(non_get(fake))
    with pytest.raises(GitHubDocumentsError) as err:
        put(fake, path, slug, **kw)
    assert len(non_get(fake)) == writes
    return str(err.value)


def branch_tree(fake, branch=BRANCH):
    return fake.tree_at(branch)


def comments(fake, number):
    return fake.pulls[number].get("comments", [])


def round2(fake):
    return fake.pull_by_head(BRANCH)["number"]


# -- names ----------------------------------------------------------------------


@pytest.mark.parametrize("name", list(gp.PUT_NAMES))
def test_every_allowed_name_commits_to_the_round_branch_and_never_to_main(
    fake, tmp_path, name
):
    main_before = fake.branches["main"]
    main_blob = fake.tree_at("main").get(f"{FOLDER}/{name}")
    path = local(
        tmp_path,
        name,
        NOTE if name.startswith("close-out") else "# Draft\n\nA new draft.\n",
    )
    result, out = put(fake, path)
    assert fake.branches["main"] == main_before
    assert fake.tree_at("main").get(f"{FOLDER}/{name}") == main_blob
    assert f"{FOLDER}/{name}" in branch_tree(fake)
    assert fake.branches[BRANCH] == result.commit_sha
    assert result.written == (f"{FOLDER}/{name}",)
    assert f"on {BRANCH}" in out


def test_put_takes_the_named_file_not_its_path(fake, tmp_path):
    nested = tmp_path / "somewhere" / "else"
    nested.mkdir(parents=True)
    result, _ = put(fake, local(nested, "close-out-to-chat.md"))
    assert result.written == (f"{FOLDER}/close-out-to-chat.md",)


@pytest.mark.parametrize(
    "name, expect",
    [
        ("review.md", "written by the pipeline"),
        ("notes.md", "not a name put accepts"),
        (f"{SLUG}-close-out-r4-to-chat.md", "not a name put accepts"),
        (f"{SLUG}-draft-r4.md", "not a name put accepts"),
        ("handoff.md", "not a name put accepts"),
        ("report.json", "not a name put accepts"),
    ],
)
def test_other_names_are_refused_before_anything_is_sent(fake, tmp_path, name, expect):
    before = len(fake.requests)
    message = refused(fake, local(tmp_path, name))
    assert expect in message and "close-out-to-chat.md" in message
    assert len(fake.requests) == before


def test_anything_under_pipeline_is_refused(fake, tmp_path):
    folder = tmp_path / "pipeline"
    folder.mkdir()
    before = len(fake.requests)
    message = refused(fake, local(folder, "run.json", "{}\n"))
    assert "written by the pipeline" in message
    assert len(fake.requests) == before


def test_local_problems_are_refused_before_anything_is_sent(fake, tmp_path):
    before = len(fake.requests)
    assert "Cannot read" in refused(fake, tmp_path / "close-out-to-chat.md")
    assert "is empty" in refused(fake, local(tmp_path, "draft.md", "  \n"))
    bad = tmp_path / "metadata.md"
    bad.write_bytes(b"\xff\xfe not utf8")
    assert "not UTF-8" in refused(fake, bad)
    assert "article slug" in refused(
        fake, local(tmp_path, "draft.md", "x\n"), slug="../x"
    )
    assert len(fake.requests) == before


def test_a_missing_token_stops_before_a_request(fake, tmp_path):
    with pytest.raises(GitHubDocumentsError, match="No GitHub token"):
        gp.put_file(
            SLUG,
            local(tmp_path, "draft.md", "x\n"),
            settings=SETTINGS,
            api_keys=API_KEYS,
            token_lookup=lambda: None,
            session=fake,
            out=lambda line: None,
        )


# -- the commit -----------------------------------------------------------------------


def test_the_message_is_the_subject_and_one_line_from_the_notes_header(fake, tmp_path):
    result, _ = put(fake, local(tmp_path, "close-out-to-chat.md"))
    assert fake.commits[result.commit_sha]["message"] == (
        "close-out: example-article r2 to-chat\n\n"
        "3 edits, 1 optional, 2 questions, 1 decision, 0 checks, 2 locks\n"
    )


def test_a_note_without_a_contains_line_falls_back_to_its_article_line(fake, tmp_path):
    text = "CLOSE-OUT HANDOFF\nArticle: Example Article\n\nbody\n"
    result, _ = put(fake, local(tmp_path, "close-out-to-code.md", text))
    assert fake.commits[result.commit_sha]["message"] == (
        "close-out: example-article r2 to-code\n\nExample Article\n"
    )


def test_an_unfilled_template_line_is_not_a_summary(fake, tmp_path):
    text = "CLOSE-OUT HANDOFF\nContains: [counts, so a truncated paste is caught]\n"
    result, _ = put(fake, local(tmp_path, "close-out-to-chat.md", text))
    assert fake.commits[result.commit_sha]["message"] == (
        "close-out: example-article r2 to-chat\n"
    )


@pytest.mark.parametrize("name", ["draft.md", "metadata.md", "publication.md"])
def test_a_drafting_file_message_is_kind_slug_and_round(fake, tmp_path, name):
    result, _ = put(fake, local(tmp_path, name, "text\n"))
    assert fake.commits[result.commit_sha]["message"] == (
        f"{name[:-3]}: example-article r2\n"
    )


def test_crlf_is_committed_as_lf(fake, tmp_path):
    put(fake, local(tmp_path, "close-out-to-chat.md", crlf=True))
    assert b"\r" not in fake.blobs[branch_tree(fake)[f"{FOLDER}/close-out-to-chat.md"]]


def test_the_file_goes_into_the_article_folder_under_the_path_prefix(fake, tmp_path):
    settings = {**SETTINGS, "path_prefix": "articles"}
    put(fake, local(tmp_path, "draft.md", "x\n"), settings=settings)
    assert f"articles/{SLUG}/draft.md" in branch_tree(fake)


# -- replacing, and not -------------------------------------------------------------


def test_an_existing_file_is_replaced_by_a_second_commit(fake, tmp_path):
    first, _ = put(fake, local(tmp_path, "close-out-to-chat.md"))
    second, out = put(
        fake, local(tmp_path, "close-out-to-chat.md", NOTE + "A second version.\n")
    )
    assert second.commit_sha != first.commit_sha
    assert fake.commits[second.commit_sha]["parents"] == [first.commit_sha]
    assert fake.blobs[branch_tree(fake)[f"{FOLDER}/close-out-to-chat.md"]].endswith(
        b"A second version.\n"
    )
    assert out.startswith("Replaced ")
    # Both versions are in the history: that is the changelog.
    assert fake.commits[first.commit_sha]["message"].startswith("close-out:")


def test_identical_bytes_make_no_commit_and_no_comment(fake, tmp_path):
    first, _ = put(fake, local(tmp_path, "close-out-to-chat.md"))
    head = fake.branches[BRANCH]
    posts = len(non_get(fake))
    again, out = put(fake, local(tmp_path, "close-out-to-chat.md", crlf=True))
    assert again.written == () and "Nothing new to write" in out
    assert fake.branches[BRANCH] == head and len(non_get(fake)) == posts
    assert len(comments(fake, round2(fake))) == 1


def test_put_replaces_only_through_the_blob_it_read(fake, tmp_path, monkeypatch):
    put(fake, local(tmp_path, "draft.md", "# First\n"))
    real = GitHubRepo.read

    def read_then_someone_edits(self, path, ref):
        got = real(self, path, ref)
        fake.external_commit(
            BRANCH, {path: b"# Edited by someone else\n"}, "their edit"
        )
        return got

    monkeypatch.setattr(GitHubRepo, "read", read_then_someone_edits)
    with pytest.raises(GitHubDocumentsError, match="changed by someone else"):
        put(fake, local(tmp_path, "draft.md", "# Mine\n"))
    # The branch is where their edit left it: put made no commit of its own.
    assert fake.commits[fake.branches[BRANCH]]["message"] == "their edit"
    assert (
        fake.blobs[branch_tree(fake)[f"{FOLDER}/draft.md"]]
        == b"# Edited by someone else\n"
    )


def test_a_file_that_appears_after_the_read_is_refused_not_overwritten(
    fake, tmp_path, monkeypatch
):
    real = GitHubRepo.read

    def absent_then_appears(self, path, ref):
        try:
            return real(self, path, ref)
        finally:
            fake.external_commit(BRANCH, {path: b"theirs\n"}, "their file")

    monkeypatch.setattr(GitHubRepo, "read", absent_then_appears)
    with pytest.raises(GitHubDocumentsError, match="changed by someone else"):
        put(fake, local(tmp_path, "publication.md", "mine\n"))
    assert fake.blobs[branch_tree(fake)[f"{FOLDER}/publication.md"]] == b"theirs\n"


# -- secrets ----------------------------------------------------------------------------


def test_a_configured_api_key_in_the_file_refuses_the_commit(fake, tmp_path):
    commits = len(fake.commits)
    with pytest.raises(GitHubDocumentsError, match="configured API key"):
        put(fake, local(tmp_path, "close-out-to-chat.md", NOTE + f"key {KEY}\n"))
    assert len(fake.commits) == commits and len(comments(fake, round2(fake))) == 0


def test_the_token_in_the_file_refuses_the_commit(fake, tmp_path):
    with pytest.raises(GitHubDocumentsError, match="nothing was sent"):
        put(fake, local(tmp_path, "draft.md", f"token {TOKEN}\n"))
    assert f"{FOLDER}/draft.md" in branch_tree(fake)  # still round 2's own draft
    assert (
        fake.blobs[branch_tree(fake)[f"{FOLDER}/draft.md"]]
        == b"# Example\n\nThe draft.\n"
    )


# -- the announcement -------------------------------------------------------------------


def test_a_close_out_note_is_announced_on_the_pull_request_with_a_link(fake, tmp_path):
    result, out = put(fake, local(tmp_path, "close-out-to-chat.md"))
    [text] = comments(fake, round2(fake))
    assert text == (
        f"Close-out note to chat: https://github.com/{REPO}/blob/{result.commit_sha}"
        f"/{FOLDER}/close-out-to-chat.md"
    )
    assert "Commented on pull request" in out


def test_a_note_to_code_says_so(fake, tmp_path):
    put(fake, local(tmp_path, "close-out-to-code.md"))
    assert comments(fake, round2(fake))[0].startswith("Close-out note to code: ")


@pytest.mark.parametrize("name", ["draft.md", "metadata.md", "publication.md"])
def test_a_drafting_file_is_not_announced(fake, tmp_path, name):
    put(fake, local(tmp_path, name, "text\n"))
    assert comments(fake, round2(fake)) == []


def test_a_comment_that_fails_leaves_the_commit_and_warns(fake, tmp_path):
    fake.inject("POST", "/issues/", Resp(500, {"message": "boom"}))
    result, out = put(fake, local(tmp_path, "close-out-to-chat.md"))
    assert fake.branches[BRANCH] == result.commit_sha
    assert "warning: the note is committed but the comment" in out


# -- which round --------------------------------------------------------------------------


def test_two_open_rounds_ask_for_pr_and_pr_chooses(fake, tmp_path):
    gh = client(fake)
    third = make_round(fake, gh, 3)
    message = refused(fake, local(tmp_path, "draft.md", "x\n"))
    assert "--pr" in message and BRANCH in message
    result, out = put(fake, local(tmp_path, "draft.md", "x\n"), pr=third.number)
    assert "on round/example-article/r3" in out
    assert fake.tree_at("round/example-article/r3")[f"{FOLDER}/draft.md"]
    assert fake.branches[BRANCH] != result.commit_sha


def test_no_open_round_is_refused_with_how_a_round_starts(fake, tmp_path):
    gh = client(fake)
    gh.merge_pull(round2(fake), sha=fake.branches[BRANCH])
    github_rounds.tag_merged_rounds(
        gh, SLUG
    )  # put would, first; keep that out of the count
    message = refused(fake, local(tmp_path, "draft.md", "x\n"))
    assert "no open round" in message


def test_a_merged_or_foreign_pull_request_is_refused(fake, tmp_path):
    gh = client(fake)
    merged = fake.pull_by_head("round/example-article/r1")["number"]
    assert "is merged" in refused(fake, local(tmp_path, "draft.md", "x\n"), pr=merged)
    other = make_round(fake, gh, 1, slug="other-article")
    assert "not a round of" in refused(
        fake, local(tmp_path, "draft.md", "x\n"), pr=other.number
    )


def test_a_close_out_note_for_an_article_that_is_not_there_is_refused(fake, tmp_path):
    gh = client(fake)
    # A round whose branch has no folder for the slug.
    fake.branches["round/example-article/r9"] = fake.branches["main"]
    fake.external_commit("round/example-article/r9", {"elsewhere/x.md": b"x"}, "x")
    gh.merge_pull(round2(fake), sha=fake.branches[BRANCH])
    github_rounds.tag_merged_rounds(gh, SLUG)
    nine = gh.create_pull("round/example-article/r9", "main", "r9", "b")
    # Remove the folder from r9's tree by cutting it from a commit without it.
    fake.trees[fake.commits[fake.branches["round/example-article/r9"]]["tree"]].pop(
        f"{FOLDER}/draft.md", None
    )
    message = refused(fake, local(tmp_path, "close-out-to-chat.md"), pr=nine.number)
    assert "no folder" in message and "check the slug" in message


# -- the repository's own rules -------------------------------------------------------------


def test_a_public_repository_is_refused_unless_allowed(fake, tmp_path):
    orig = fake._repo_json
    fake._repo_json = lambda: {**orig(), "private": False}
    assert "public repository" in refused(fake, local(tmp_path, "draft.md", "x\n"))
    put(
        fake,
        local(tmp_path, "draft.md", "x\n"),
        settings={**SETTINGS, "allow_public_repo": True},
    )


def test_a_token_that_cannot_push_is_refused(fake, tmp_path):
    orig = fake._repo_json
    fake._repo_json = lambda: {**orig(), "permissions": {"push": False}}
    assert "cannot push" in refused(fake, local(tmp_path, "draft.md", "x\n"))


def test_a_rate_limit_is_explained(fake, tmp_path):
    limited = Resp(
        403,
        {"message": "rate"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"},
    )
    # Two: the tagging step lists the closed pull requests first, and a failure
    # there is only a warning; the listing of the open ones is the put's own.
    fake.inject("GET", "/pulls", limited, times=2)
    assert "rate limit" in refused(fake, local(tmp_path, "draft.md", "x\n")).lower()


# -- ls -------------------------------------------------------------------------------------


def run_cli(fake, *argv):
    lines = []
    with (
        patch(
            "ci_article_review.config_loader.load_user_config",
            return_value={"api_keys": API_KEYS},
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


def tagger(day):
    from datetime import datetime, timezone

    return Signature(
        "Example User",
        "4242+example-user@users.noreply.github.com",
        datetime(2026, 10, day, 9, 30, tzinfo=timezone.utc),
    )


def ls_state():
    """Two tagged rounds, one open, with run files and a note on the open one."""
    fake = FakeGitHub()
    gh = client(fake)
    one = make_round(fake, gh, 1, runs=(2,), merged=True)
    two = make_round(fake, gh, 2, runs=(4, 5), merged=True)
    gh.create_tag(
        "example-article/r1",
        "example-article r1: PR #1, run 2",
        one.merge_commit_sha,
        tagger=tagger(20),
    )
    gh.create_tag(
        "example-article/r2",
        "example-article r2: PR #2, runs 4 and 5",
        two.merge_commit_sha,
        tagger=tagger(24),
    )
    three = make_round(
        fake,
        gh,
        3,
        runs=(6,),
        files={
            f"{FOLDER}/draft.md": b"draft three\n",
            f"{FOLDER}/review.md": b"review\n",
            f"{FOLDER}/pipeline/run.json": b"{}\n",
            f"{FOLDER}/close-out-to-chat.md": NOTE.encode(),
        },
    )
    return fake, gh, three


def test_ls_shows_two_tags_and_one_open_round_and_the_files_of_the_open_branch():
    fake, gh, three = ls_state()
    before = len(fake.requests)
    code, out = run_cli(fake, "ls", SLUG, "--github-repo", REPO)
    assert code == 0
    assert [r for r in fake.requests[before:] if r[0] != "GET"] == []
    lines = out.splitlines()
    assert lines[0] == f"{REPO}  {FOLDER}/"
    assert any(
        line.startswith("  r1")
        and "accepted" in line
        and "tag example-article/r1 2026-10-20" in line
        for line in lines
    )
    assert any(
        line.startswith("  r2") and "accepted" in line and "2026-10-24" in line
        for line in lines
    )
    r3 = next(line for line in lines if line.startswith("  r3"))
    assert "open" in r3
    assert any(
        f"#{three.number} open" in line
        and "round/example-article/r3" in line
        and f"head {fake.branches[three.head_ref][:7]}" in line
        and "last review: run 6" in line
        for line in lines
    )
    assert any("#1 merged" in line and "last review: run 2" in line for line in lines)
    assert f"Files at {three.head_ref} ({fake.branches[three.head_ref][:7]})" in out
    for name in ("draft.md", "review.md", "close-out-to-chat.md", "run.json"):
        assert name in out
    assert "draft: example-article r3" in out  # the file's last commit subject
    assert "  pipeline/" in out


def test_ls_with_ref_lists_that_ref_instead():
    fake, gh, three = ls_state()
    code, out = run_cli(fake, "ls", SLUG, "--github-repo", REPO, "--ref", "main")
    assert code == 0 and "Files at main (" in out
    assert "review.md" not in out  # only on round 3's branch
    code, tagged = run_cli(
        fake, "ls", SLUG, "--github-repo", REPO, "--ref", "example-article/r1"
    )
    assert code == 0 and "Files at example-article/r1 (" in tagged


def test_ls_with_no_open_round_reads_main():
    fake = FakeGitHub()
    gh = client(fake)
    make_round(fake, gh, 1, merged=True)
    code, out = run_cli(fake, "ls", SLUG, "--github-repo", REPO)
    assert code == 0 and "Files at main (" in out
    assert "merged, not yet tagged" in out


def test_ls_with_two_open_rounds_says_which_branch_the_files_are_from():
    fake = FakeGitHub()
    gh = client(fake)
    make_round(fake, gh, 1)
    make_round(fake, gh, 2)
    code, out = run_cli(fake, "ls", SLUG, "--github-repo", REPO)
    assert (
        code == 0
        and "2 rounds are open" in out
        and "--ref round/example-article/rN" in out
    )
    assert "is not at main (" in out  # nothing is merged yet, so main has no folder


def test_ls_of_an_unknown_article_names_the_ones_that_exist(capsys):
    fake = FakeGitHub()
    make_round(fake, client(fake), 1, slug="other-article", merged=True)
    code, _ = run_cli(fake, "ls", SLUG, "--github-repo", REPO)
    assert code == 1 and "other-article" in capsys.readouterr().err


def test_ls_needs_no_token_for_a_repository_that_allows_it():
    fake = FakeGitHub(token=None)
    make_round(fake, GitHubRepo(REPO, None, session=fake), 1, merged=True)
    lines = []
    code = documents_cli.main(
        ["ls", SLUG, "--github-repo", REPO],
        environ={},
        run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(),
        session=fake,
        out=lines.append,
    )
    assert code == 0


def test_the_commands_are_subcommands_of_the_one_console_script():
    parser = documents_cli.build_parser()
    assert parser.parse_args(["ls", "a"]).command == "ls"
    assert parser.parse_args(["ls", "a", "--ref", "main"]).ref == "main"
    put_args = parser.parse_args(["put", "a", "draft.md", "--pr", "7"])
    assert (put_args.command, put_args.pr) == ("put", 7)
    assert parser.parse_args(["tag", "a", "--dry-run"]).dry_run is True
    accept = parser.parse_args(["accept", "a", "--pr", "3", "--yes"])
    assert (accept.command, accept.pr, accept.yes) == ("accept", 3, True)
    with pytest.raises(SystemExit):
        parser.parse_args(["put", "a", "draft.md", "--branch", "main"])  # retired
    assert not parser.parse_args(["accept", "a"]).yes


def test_put_command_end_to_end(fake, tmp_path):
    path = local(tmp_path, "close-out-to-chat.md")
    code, out = run_cli(fake, "put", SLUG, path, "--github-repo", REPO)
    assert code == 0 and f"Added {FOLDER}/close-out-to-chat.md on {BRANCH}" in out
    assert fake.tree_at(BRANCH)[f"{FOLDER}/close-out-to-chat.md"]


def test_put_command_without_a_readable_config_stops_before_a_request(
    fake, tmp_path, capsys
):
    path = local(tmp_path, "draft.md", "x\n")
    before = len(fake.requests)
    with patch(
        "ci_article_review.config_loader.load_user_config",
        side_effect=ValueError("unset ${OPENAI_API_KEY}"),
    ):
        code = documents_cli.main(
            ["put", SLUG, str(path)], environ={"GITHUB_TOKEN": TOKEN}, session=fake
        )
    assert code == 1
    assert "check the file for API keys" in capsys.readouterr().err
    assert len(fake.requests) == before


def test_put_command_scans_for_the_publications_keys_too(fake, tmp_path):
    path = local(tmp_path, "draft.md", "pub-key-0123456789\n")
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
    assert code == 1
    assert (
        fake.blobs[branch_tree(fake)[f"{FOLDER}/draft.md"]]
        == b"# Example\n\nThe draft.\n"
    )


def test_put_command_exits_one_on_a_refusal(fake, tmp_path, capsys):
    code, _ = run_cli(
        fake, "put", SLUG, local(tmp_path, "review.md"), "--github-repo", REPO
    )
    assert code == 1 and "written by the pipeline" in capsys.readouterr().err
