"""``--write-back`` and ``ci-docs push-run``: the review goes back to GitHub (#403).

The GitHub side is the real ``ci_core.github_repo.GitHubRepo`` talking to the
in-process ``FakeGitHub`` of ``test_documents_init.py`` (made strict here: a ref
update that is not a fast forward is refused, as GitHub refuses it), so the
Git Data calls, the never-clobber rule and the retry on a moved branch are the
real ones. What is tested here is what this phase owns: which files go under
which names, in one commit, with LF endings and the documented message; that a
replay is never pushed; and that a failure after the review keeps the local
files and names the retry.

Slugs and contents are invented; this repository is public.
"""

import json
import logging
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from .test_documents_init import FakeGitHub, Resp
from .test_pipeline_end_to_end import (
    _CONFIG,
    _CURRENCY,
    _HANDOFF,
    _fake_resolve_citations,
    _fake_run_domain,
)

from ci_article_review import documents_cli, github_writeback as gw, pipeline
from ci_article_review.github_documents import GitHubDocumentsError
from ci_core.github_repo import GitHubRepo

REPO = "example-owner/example-articles"
TOKEN = "ghp_exampleSECRETtoken0123456789"
SLUG = "example-article"
FOLDER = f"articles/{SLUG}"
TS = "20261026_143022"
RUN = f"run_4_{TS}"
KEY = "sk-example-provider-key-0123456789"
DRAFT_BLOB = "d" * 40
SOURCE = {
    "kind": "github",
    "repo": REPO,
    "ref": "main",
    "commit_sha": "3f2a91c" + "0" * 33,
    "round": 4,
    "files": [
        {"path": f"{FOLDER}/{SLUG}-draft-r4.md", "blob_sha": DRAFT_BLOB},
        {"path": f"{FOLDER}/{SLUG}-metadata-r4.md", "blob_sha": "e" * 40},
    ],
}
SETTINGS = {"include_capture": False, "allow_public_repo": False}
API_KEYS = {"openai": {"api_key": KEY}, "languagetool": {"username": "", "api_key": ""}}


class StrictGitHub(FakeGitHub):
    """``FakeGitHub`` that refuses a non-fast-forward ref update, and can race."""

    def __init__(self, **kw):
        super().__init__(**kw)
        #: Called once, just before the next ref update (a writer who got in first).
        self.before_ref_update = []

    def _route(self, method, path, headers, params, body):
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            if self.before_ref_update:
                self.before_ref_update.pop(0)()
            branch = path[len("/git/refs/heads/") :]
            if self.commits[body["sha"]]["parents"] != [self.branches[branch]]:
                return Resp(422, {"message": "Update is not a fast forward"})
        return super()._route(method, path, headers, params, body)

    def ref_updates(self):
        return [r for r in self.requests if r[0] == "PATCH"]


@pytest.fixture
def fake():
    stub = StrictGitHub()
    stub.external_commit(
        "main",
        {
            f"{FOLDER}/{SLUG}-draft-r4.md": b"# Example\n\nA draft.\n",
            f"{FOLDER}/{SLUG}-metadata-r4.md": b"metadata\n",
        },
        "round 4",
    )
    return stub


@pytest.fixture
def client(fake):
    """Every ``GitHubRepo`` the write-back builds talks to ``fake``."""

    def build(repo, token, session=None):
        assert repo == REPO
        return GitHubRepo(repo, token, session=fake)

    with (
        patch.object(gw, "GitHubRepo", build),
        patch.object(gw, "resolve_token", return_value=TOKEN),
    ):
        yield fake


def saved_run(tmp_path, *, source=SOURCE, run=4, ts=TS, capture=True, worklist=True):
    """A run on disk as ``hist.save_run`` leaves it on Windows: CRLF text files."""
    history = tmp_path / "pipeline_history" / SLUG
    history.mkdir(parents=True)
    report = {
        "run_number": run,
        "publication": "testpub",
        "ensemble": {"cost_preset": "wide"},
        "cost_summary": {"total_usd": 0.7276},
        "document_source": source,
    }
    path = history / f"run_{run}_{ts}_report.json"
    path.write_bytes(json.dumps(report, indent=2).replace("\n", "\r\n").encode())
    (history / f"run_{run}_{ts}_review.md").write_bytes(
        b"# Review\r\n\r\nFindings.\r\n"
    )
    if worklist:
        (history / f"run_{run}_{ts}_worklist.md").write_bytes(
            b"# Worklist\r\n- one\r\n"
        )
    if capture:
        (history / f"run_{run}_{ts}_results.json").write_bytes(b'{"results": []}\r\n')
    return path


def source_as_read(fake, stale=()):
    """``SOURCE`` with the blob SHAs the branch holds now; ``stale`` names are changed."""
    files = []
    for entry in SOURCE["files"]:
        blob = fake.tree()[entry["path"]]
        if entry["path"].rsplit("/", 1)[-1] in stale:
            blob = DRAFT_BLOB
        files.append({"path": entry["path"], "blob_sha": blob})
    return {**SOURCE, "files": files}


def push(path, *, settings=SETTINGS, api_keys=API_KEYS, **kw):
    lines = []
    result = gw.push_saved_run(
        path, settings=settings, api_keys=api_keys, out=lines.append, **kw
    )
    return result, lines


NEW_FILES = {
    f"{FOLDER}/{SLUG}-review-r4.md": b"# Review\n\nFindings.\n",
    f"{FOLDER}/pipeline/{SLUG}-worklist-r4.md": b"# Worklist\n- one\n",
}


class TestPushSavedRun:
    def test_three_files_go_in_one_commit_with_lf_endings(self, client, tmp_path):
        before = len(client.commits)
        result, lines = push(saved_run(tmp_path))
        assert len(client.commits) == before + 1
        assert client.branches["main"] == result.commit_sha
        tree = client.tree()
        report_path = f"{FOLDER}/pipeline/{RUN}_report.json"
        assert set(result.written) == {*NEW_FILES, report_path}
        for path, data in NEW_FILES.items():
            assert client.file(path) == data
        for path in result.written:
            assert b"\r" not in client.file(path), path
        assert json.loads(client.file(report_path))["run_number"] == 4
        # The capture is not committed unless asked for.
        assert f"{FOLDER}/pipeline/{RUN}_results.json" not in tree
        assert lines[0].endswith(result.html_url)
        assert {line.strip() for line in lines[1:]} == set(result.written)

    def test_include_capture_adds_the_fourth_file(self, client, tmp_path):
        result, _ = push(
            saved_run(tmp_path), settings={**SETTINGS, "include_capture": True}
        )
        assert f"{FOLDER}/pipeline/{RUN}_results.json" in result.written
        assert len(result.written) == 4
        assert (
            client.file(f"{FOLDER}/pipeline/{RUN}_results.json") == b'{"results": []}\n'
        )

    def test_the_commit_message_is_the_documented_one(self, client, tmp_path):
        result, _ = push(saved_run(tmp_path, source=source_as_read(client)))
        assert client.commits[result.commit_sha]["message"] == (
            f"review: {SLUG} r4\n"
            "\n"
            f"Reviewed: {SLUG}-draft-r4.md, {SLUG}-metadata-r4.md at 3f2a91c\n"
            f"Run: {RUN} (preset wide, $0.73 reported)\n"
            f"Files: {SLUG}-review-r4.md, pipeline/{SLUG}-worklist-r4.md, "
            f"pipeline/{RUN}_report.json\n"
        )

    def test_a_draft_edited_in_place_since_it_was_read_is_said_so(
        self, client, tmp_path
    ):
        stale = source_as_read(client, stale={f"{SLUG}-draft-r4.md"})
        result, _ = push(saved_run(tmp_path, source=stale))
        message = client.commits[result.commit_sha]["message"]
        assert (
            f"Note: {SLUG}-draft-r4.md changed at its own path since it was read"
            in message
        )
        assert f"{SLUG}-metadata-r4.md changed" not in message

    def test_a_second_push_of_the_same_run_is_a_no_op(self, client, tmp_path):
        path = saved_run(tmp_path)
        first, _ = push(path)
        commits = len(client.commits)
        second, lines = push(path)
        assert second.written == () and set(second.unchanged) == set(first.written)
        assert len(client.commits) == commits
        assert client.branches["main"] == first.commit_sha
        assert "Nothing new to write" in lines[0]

    def test_it_only_adds_never_replaces(self, client, tmp_path):
        path = saved_run(tmp_path)
        taken = f"{FOLDER}/{SLUG}-review-r4.md"
        client.external_commit("main", {taken: b"someone else's review\n"})
        head = client.branches["main"]
        with pytest.raises(GitHubDocumentsError, match="already exists with different"):
            push(path)
        assert client.branches["main"] == head
        assert client.file(taken) == b"someone else's review\n"

    def test_a_branch_that_moves_during_the_commit_is_built_on(self, client, tmp_path):
        client.before_ref_update.append(
            lambda: client.external_commit(
                "main", {f"{FOLDER}/{SLUG}-draft-r5.md": b"round 5\n"}, "drafting side"
            )
        )
        result, _ = push(saved_run(tmp_path))
        assert len(client.ref_updates()) == 2  # refused once, then accepted
        parent = client.commits[result.commit_sha]["parents"][0]
        assert client.commits[parent]["message"] == "drafting side"
        tree = client.tree()
        assert f"{FOLDER}/{SLUG}-draft-r5.md" in tree  # the other writer survived
        assert f"{FOLDER}/{SLUG}-review-r4.md" in tree

    def test_a_replay_is_never_pushed(self, client, tmp_path):
        replay = tmp_path / "pipeline_history" / "_replay"
        path = saved_run(replay)
        with pytest.raises(GitHubDocumentsError, match="under _replay/"):
            push(path)
        assert client.requests == []

    def test_a_report_copied_out_of_replay_is_pushed(self, client, tmp_path):
        # The copy is an explicit act, and the report still says "replayed_from".
        path = saved_run(tmp_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(
            json.dumps({**report, "replayed_from": "x_results.json"}), encoding="utf-8"
        )
        assert push(path)[0].written

    def test_a_report_not_read_from_github_is_refused(self, client, tmp_path):
        path = saved_run(tmp_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        del report["document_source"]
        path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(GitHubDocumentsError, match="no GitHub document_source"):
            push(path)
        assert client.requests == []

    @pytest.mark.parametrize(
        "name", ["report.json", "run_4_report.json", "x_report.json"]
    )
    def test_a_report_that_is_not_named_like_a_run_is_refused(
        self, client, tmp_path, name
    ):
        path = saved_run(tmp_path)
        renamed = path.with_name(name)
        path.rename(renamed)
        with pytest.raises(GitHubDocumentsError, match="not named run_N_<timestamp>"):
            push(renamed)

    def test_a_missing_review_stops_it_but_a_missing_worklist_does_not(
        self, client, tmp_path, caplog
    ):
        path = saved_run(tmp_path, worklist=False, capture=False)
        with caplog.at_level(logging.WARNING):
            result, _ = push(path)
        assert len(result.written) == 2 and "worklist.md was not saved" in caplog.text
        (path.parent / f"{RUN}_review.md").unlink()
        with pytest.raises(GitHubDocumentsError, match="_review.md is missing"):
            push(path)

    def test_a_configured_api_key_in_a_file_refuses_the_commit(self, client, tmp_path):
        path = saved_run(tmp_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        report["provider_error"] = f"401 for key {KEY}"
        path.write_text(json.dumps(report), encoding="utf-8")
        head = client.branches["main"]
        with pytest.raises(GitHubDocumentsError, match="nothing was sent"):
            push(path)
        assert client.branches["main"] == head
        assert client.writes == []

    def test_the_token_never_reaches_the_commit_or_the_message(self, client, tmp_path):
        result, _ = push(saved_run(tmp_path))
        everything = [client.commits[result.commit_sha]["message"].encode()]
        everything += [client.file(p) for p in result.written]
        assert not any(TOKEN.encode() in blob for blob in everything)

    def test_a_public_repository_is_refused_unless_allowed(self, client, tmp_path):
        client.private = False
        path = saved_run(tmp_path)
        with pytest.raises(GitHubDocumentsError, match="public repository"):
            push(path)
        assert client.writes == []
        result, _ = push(path, settings={**SETTINGS, "allow_public_repo": True})
        assert result.written

    def test_a_token_that_cannot_push_is_refused(self, client, tmp_path):
        client.push = False
        with pytest.raises(GitHubDocumentsError, match="cannot push"):
            push(saved_run(tmp_path))
        assert client.writes == []

    def test_a_branch_that_no_longer_exists_is_refused(self, client, tmp_path):
        source = {**SOURCE, "ref": "gone"}
        with pytest.raises(GitHubDocumentsError, match="no branch 'gone'"):
            push(saved_run(tmp_path, source=source))

    def test_no_token_is_a_plain_message(self, tmp_path):
        with (
            patch.object(gw, "resolve_token", return_value=None),
            pytest.raises(GitHubDocumentsError, match="GITHUB_TOKEN"),
        ):
            push(saved_run(tmp_path))

    def test_repo_overrides_the_repository_the_report_names(self, fake, tmp_path):
        seen = []

        def build(repo, token, session=None):
            seen.append(repo)
            return GitHubRepo("example-owner/example-articles", token, session=fake)

        with (
            patch.object(gw, "GitHubRepo", build),
            patch.object(gw, "resolve_token", return_value=TOKEN),
        ):
            source = {**SOURCE, "repo": "someone/else"}
            push(saved_run(tmp_path, source=source), repo=REPO)
        assert seen == [REPO]

    def test_names_come_from_the_folder_not_from_a_configured_prefix(
        self, client, tmp_path
    ):
        # A repository that keeps articles under another prefix still works:
        # push-run needs no path_prefix because the report records the paths.
        deep = {
            **SOURCE,
            "files": [
                {"path": f"docs/a/{SLUG}/{SLUG}-draft-r4.md", "blob_sha": DRAFT_BLOB}
            ],
        }
        result, _ = push(saved_run(tmp_path, source=deep))
        assert f"docs/a/{SLUG}/{SLUG}-review-r4.md" in result.written


class TestFailureAfterTheRun:
    """``write_back_after_run``: the review is saved, so a failure says how to retry."""

    def test_the_retry_command_is_named(self, client, tmp_path):
        path = saved_run(tmp_path)
        client.push = False
        with pytest.raises(GitHubDocumentsError) as err:
            gw.write_back_after_run(
                path, settings=SETTINGS, api_keys=API_KEYS, config_dir="my configs"
            )
        message = str(err.value)
        assert "saved" in message
        # Quoting of the path depends on the platform's separators; the line
        # that is printed is the one retry_command builds, and a config
        # directory with a space in it is quoted everywhere.
        retry = gw.retry_command(path, "my configs")
        assert retry in message
        assert retry.startswith("ci-docs push-run ") and str(path) in retry
        assert retry.endswith('--config-dir "my configs"')

    def test_the_default_config_dir_is_left_off_the_retry(self):
        assert gw.retry_command("a/run_1_x_report.json") == (
            "ci-docs push-run a/run_1_x_report.json"
        )

    def test_exists_says_to_take_a_new_run_number(self, client, tmp_path):
        client.external_commit("main", {f"{FOLDER}/{SLUG}-review-r4.md": b"other\n"})
        with pytest.raises(GitHubDocumentsError, match="run the review again"):
            gw.write_back_after_run(
                saved_run(tmp_path), settings=SETTINGS, api_keys=API_KEYS
            )

    def test_secret_names_the_problem_not_a_retry_that_would_help(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        report["note"] = KEY
        path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(GitHubDocumentsError, match="Find and remove it"):
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)

    def test_conflict_after_the_retries_keeps_the_local_files(self, client, tmp_path):
        path = saved_run(tmp_path)
        for n in range(4):  # commit_files tries four times in all
            client.before_ref_update.append(
                lambda n=n: client.external_commit(
                    "main", {f"{FOLDER}/x{n}.md": b"x\n"}, f"racer {n}"
                )
            )
        with pytest.raises(GitHubDocumentsError, match="kept moving") as err:
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)
        assert "ci-docs push-run" in str(err.value)
        assert path.exists()
        assert f"{FOLDER}/{SLUG}-review-r4.md" not in client.tree()


class TestSecretValues:
    def test_every_nested_non_empty_string_counts(self):
        keys = {
            "a": {"api_key": "one", "n": 3, "empty": "", "blank": "  "},
            "b": {"c": {"deep": " two "}},
            "d": None,
        }
        assert gw.secret_values(keys) == ["one", "two"]

    def test_no_keys_means_nothing_to_refuse(self):
        assert gw.secret_values({}) == [] and gw.secret_values(None) == []


# ---------------------------------------------------------------------------
# A real run, through run_draft_pipeline, writing back
# ---------------------------------------------------------------------------


def handoff(**extra):
    return {
        **_HANDOFF,
        "history_key": SLUG,
        "run_number": 1,
        "document_source": SOURCE,
        **extra,
    }


def run_pipeline(tmp_path, client, *, write_back=True, settings=None, **extra):
    """``run_draft_pipeline`` over stubs, writing back to ``client``."""
    config = {
        **_CONFIG,
        "api_keys": {"openai": {"api_key": KEY}},
        "github_documents": {"include_capture": False, **(settings or {})},
    }
    with ExitStack() as stack:
        for target, kw in (
            ("load_user_config", {"return_value": {"pipeline": {}}}),
            ("load_publication_config", {"return_value": {}}),
            ("merge_configs", {"return_value": config}),
            ("check_model_currency", {"return_value": _CURRENCY}),
            ("_run_domain", {"side_effect": _fake_run_domain}),
        ):
            stack.enter_context(patch(f"ci_article_review.pipeline.{target}", **kw))
        stack.enter_context(patch.object(pipeline.time, "sleep"))
        stack.enter_context(
            patch("ci_article_review.pipeline.HISTORY_ROOT", str(tmp_path / "history"))
        )
        for target, kw in (
            (
                "ci_article_review.adapters.citation.resolver.resolve_citations",
                {"side_effect": _fake_resolve_citations},
            ),
            (
                "ci_article_review.analysis.seo_suggest.generate",
                {"return_value": ({"status": "skipped", "reason": "test"}, None)},
            ),
            (
                "ci_article_review.analysis.seo_content.review",
                {"return_value": ({"status": "skipped", "reason": "test"}, None)},
            ),
        ):
            stack.enter_context(patch(target, **kw))
        return pipeline.run_draft_pipeline(
            None,
            "testpub",
            handoff=handoff(**extra),
            write_back=write_back,
        )


def local_runs(tmp_path):
    return sorted(
        p.name for p in (tmp_path / "history" / SLUG).glob("run_*_report.json")
    )


class TestRealRun:
    def test_a_run_writes_its_review_back_in_one_commit(self, client, tmp_path, capsys):
        before = len(client.commits)
        run_pipeline(tmp_path, client)
        assert len(client.commits) == before + 1
        (report,) = local_runs(tmp_path)
        stem = report[: -len("_report.json")]
        tree = client.tree()
        new = sorted(
            set(tree)
            - {
                f"{FOLDER}/{SLUG}-draft-r4.md",
                f"{FOLDER}/{SLUG}-metadata-r4.md",
                "README.md",
            }
        )
        assert new == sorted(
            [
                f"{FOLDER}/{SLUG}-review-r1.md",
                f"{FOLDER}/pipeline/{SLUG}-worklist-r1.md",
                f"{FOLDER}/pipeline/{report}",
            ]
        )
        for path in new:
            assert b"\r" not in client.file(path)
        local = (tmp_path / "history" / SLUG / f"{stem}_review.md").read_bytes()
        assert client.file(f"{FOLDER}/{SLUG}-review-r1.md") == local.replace(
            b"\r\n", b"\n"
        )
        head = client.commits[client.branches["main"]]["message"]
        assert head.startswith(f"review: {SLUG} r1\n\nReviewed: {SLUG}-draft-r4.md")
        assert f"Run: {stem} (preset" in head
        out = capsys.readouterr().out
        assert "Wrote 3 file(s)" in out and client.branches["main"] in out

    def test_include_capture_adds_the_capture_to_the_commit(self, client, tmp_path):
        run_pipeline(tmp_path, client, settings={"include_capture": True})
        (report,) = local_runs(tmp_path)
        capture = report.replace("_report.json", "_results.json")
        assert f"{FOLDER}/pipeline/{capture}" in client.tree()

    def test_without_write_back_nothing_reaches_github(self, tmp_path):
        def fail(*a, **k):
            pytest.fail("GitHub was reached without --write-back")

        with (
            patch.object(gw, "GitHubRepo", fail),
            patch.object(gw, "resolve_token", fail),
        ):
            run_pipeline(tmp_path, None, write_back=False)
        assert local_runs(tmp_path)

    def test_a_taken_review_name_moves_the_run_number_everywhere(
        self, client, tmp_path, caplog
    ):
        client.external_commit(
            "main",
            {
                f"{FOLDER}/{SLUG}-review-r1.md": b"an earlier review\n",
                f"{FOLDER}/{SLUG}-review-r2.md": b"another\n",
            },
        )
        with caplog.at_level(logging.WARNING):
            run_pipeline(tmp_path, client, taken_run_numbers=[1, 2])
        (report,) = local_runs(tmp_path)
        assert report.startswith("run_3_")
        tree = client.tree()
        assert f"{FOLDER}/{SLUG}-review-r3.md" in tree
        assert f"{FOLDER}/pipeline/{SLUG}-worklist-r3.md" in tree
        assert f"{FOLDER}/pipeline/{report}" in tree
        assert client.file(f"{FOLDER}/{SLUG}-review-r1.md") == b"an earlier review\n"
        assert "Run 1 already exists for this article; using 3 instead" in caplog.text

    def test_a_failed_write_back_keeps_the_review_and_names_the_retry(
        self, client, tmp_path, capsys
    ):
        client.push = False
        with pytest.raises(GitHubDocumentsError) as err:
            run_pipeline(tmp_path, client)
        (report,) = local_runs(tmp_path)
        assert (tmp_path / "history" / SLUG / report).exists()
        assert (
            tmp_path / "history" / SLUG / report.replace("_report.json", "_review.md")
        ).exists()
        assert "ci-docs push-run" in str(err.value) and report in str(err.value)
        # The summary was printed before the commit was attempted.
        assert "REVIEW COMPLETE" in capsys.readouterr().out.upper()

    def test_the_retry_after_a_failure_writes_the_same_commit(self, client, tmp_path):
        client.push = False
        with pytest.raises(GitHubDocumentsError):
            run_pipeline(tmp_path, client)
        (report,) = local_runs(tmp_path)
        client.push = True
        result, _ = push(tmp_path / "history" / SLUG / report)
        assert f"{FOLDER}/{SLUG}-review-r1.md" in result.written

    def test_a_secret_in_the_report_refuses_the_commit_and_keeps_the_run(
        self, client, tmp_path
    ):
        # The key is in the draft, so it reaches review.md and report.json.
        leaky = {**_HANDOFF, "draft": _HANDOFF["draft"] + f"\n\nSecret {KEY}\n"}
        with (
            patch.dict(_HANDOFF, leaky),
            pytest.raises(GitHubDocumentsError, match="Find and remove it"),
        ):
            run_pipeline(tmp_path, client)
        assert local_runs(tmp_path)
        assert client.writes == []


# ---------------------------------------------------------------------------
# ci-docs push-run
# ---------------------------------------------------------------------------


def run_cli(client, *argv):
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
            ["push-run", *map(str, argv)],
            environ={"GITHUB_TOKEN": TOKEN},
            session=client,
            out=lines.append,
        )
    return code, "\n".join(lines)


@pytest.fixture
def cli_client(fake):
    """``push-run`` builds its own client, so the session reaches it as ``session=``."""
    with patch.object(gw, "resolve_token", return_value=TOKEN):
        yield fake


class TestPushRunCommand:
    def test_it_writes_the_run_and_a_second_run_is_a_no_op(self, cli_client, tmp_path):
        path = saved_run(tmp_path)
        with patch.object(
            gw,
            "GitHubRepo",
            lambda repo, token, session=None: GitHubRepo(repo, token, session=session),
        ):
            code, out = run_cli(cli_client, path, "--config-dir", "no-such-dir")
            assert code == 0 and "Wrote 3 file(s)" in out
            commits = len(cli_client.commits)
            code, out = run_cli(cli_client, path)
        assert code == 0 and "Nothing new to write" in out
        assert len(cli_client.commits) == commits

    def test_a_replay_report_is_refused_with_exit_one(
        self, cli_client, tmp_path, capsys
    ):
        path = saved_run(tmp_path / "pipeline_history" / "_replay")
        code, _ = run_cli(cli_client, path)
        assert code == 1 and "under _replay/" in capsys.readouterr().err
        assert cli_client.requests == []

    def test_an_unreadable_config_stops_it_before_anything_is_sent(
        self, cli_client, tmp_path, capsys
    ):
        with patch(
            "ci_article_review.config_loader.load_user_config",
            side_effect=ValueError("unset ${OPENAI_API_KEY}"),
        ):
            code = documents_cli.main(
                ["push-run", str(saved_run(tmp_path))],
                environ={"GITHUB_TOKEN": TOKEN},
                session=cli_client,
            )
        assert code == 1
        assert "check the commit for API keys" in capsys.readouterr().err
        assert cli_client.requests == []

    def test_there_is_no_second_console_script(self):
        import tomllib
        from pathlib import Path

        toml = Path(documents_cli.__file__).parents[2] / "pyproject.toml"
        scripts = tomllib.loads(toml.read_text(encoding="utf-8"))["project"]["scripts"]
        assert [k for k in scripts if k.startswith("ci-docs")] == ["ci-docs"]
