"""Automatic round tagging, ``ci-docs tag`` and ``ci-docs accept`` (#418).

The GitHub side is the real ``ci_core.github_repo.GitHubRepo`` talking to
ci-core's in-process ``FakeGitHub`` (see ``github_fake``), so the tag objects,
the ref that is the compare-and-swap, the 409 on a moved head and the merge
commit are the fake's model of the real API. What is tested here is what this
module owns: which rounds get a tag, what the tag says, that a second call or a
racing one changes nothing, that a failure is a warning and not an error, which
commands call the step and which never do.

Slugs and contents are invented; this repository is public.
"""

import logging
from dataclasses import replace
from unittest.mock import patch

import pytest
from ci_core.github_repo import GitHubRepoError

from ci_article_review import (
    documents_cli,
    github_documents as gd,
    github_rounds as gr,
    pipeline,
)
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

MERGED_AT = "2026-10-27T12:00:00Z"  # what the fake stamps on every merge
KEY = "sk-example-provider-key-0123456789"
API_KEYS = {"openai": {"api_key": KEY}}


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def gh(fake):
    return client(fake)


def tag_names(fake):
    return sorted(fake.tags)


def tag_obj(fake, name):
    return fake.tag_objects[fake.tags[name]]


def posts(fake, part):
    return [r for r in fake.requests if r[0] == "POST" and part in r[1]]


# -- the review marker ------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        ("<!-- ci-review run=7 commit=3f2a91c -->\nSummary", (7, "3f2a91c")),
        ("<!-- ci-review run=12 -->\nSummary", (12, None)),
        ("  \n<!--ci-review run=3 commit=ABCDEF1234-->", (3, "abcdef1234")),
        ("<!-- ci-review run=7 commit=3f2a91c... -->", (7, "3f2a91c")),
        ("", None),
        (None, None),
        # Only a marker that opens the body counts: a person's review quoting one,
        # a run number of 0 and a malformed marker are not the pipeline's.
        ("I read this:\n<!-- ci-review run=7 -->", None),
        ("<!-- ci-review run=0 -->", None),
        ("<!-- ci-review run=x -->", None),
        ("<!-- something else run=7 -->", None),
    ],
)
def test_review_marker(body, expected):
    assert gr.review_marker(body) == expected


@pytest.mark.parametrize(
    "runs, text",
    [
        ([], "example-article r3: PR #12"),
        ([6], "example-article r3: PR #12, run 6"),
        ([6, 7], "example-article r3: PR #12, runs 6 and 7"),
        ([5, 6, 7], "example-article r3: PR #12, runs 5, 6 and 7"),
    ],
)
def test_the_tag_message_names_the_runs(runs, text):
    assert gr.tag_message(SLUG, 3, 12, runs) == text


# -- tag_merged_rounds ----------------------------------------------------------


class TestTagMergedRounds:
    def test_tags_only_merged_round_pull_requests_without_a_tag(self, fake, gh):
        merged = make_round(fake, gh, 1, merged=True)
        tagged = make_round(fake, gh, 2, merged=True)
        gh.create_tag(
            "example-article/r2", "earlier", tagged.merge_commit_sha, tagger=_sig(gh)
        )
        make_round(fake, gh, 3)  # open
        closed = make_round(fake, gh, 4)
        fake.pulls[closed.number]["state"] = "closed"  # closed without a merge
        make_round(fake, gh, 1, slug="other-article", merged=True)
        # A merged pull request from a branch that is not a round of this article.
        fake.branches["feature/typo"] = fake.branches["main"]
        fake.external_commit("feature/typo", {"x.md": b"x"}, "typo")
        gh.merge_pull(
            gh.create_pull("feature/typo", "main", "typo", "b").number,
            sha=fake.branches["feature/typo"],
        )

        results = gr.tag_merged_rounds(gh, SLUG)

        assert [(r.round, r.tag, r.status) for r in results] == [
            (1, "example-article/r1", gr.CREATED)
        ]
        # r2 had its tag, r3 is open, r4 was closed unmerged, and another
        # article's round and a plain merged branch are not this article's rounds.
        assert sorted(fake.tags) == ["example-article/r1", "example-article/r2"]
        obj = tag_obj(fake, "example-article/r1")
        assert obj["type"] == "commit" and obj["object"] == merged.merge_commit_sha
        assert results[0].commit == merged.merge_commit_sha

    def test_the_tagger_is_the_tokens_user_dated_at_the_merge(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        gr.tag_merged_rounds(gh, SLUG)
        assert tag_obj(fake, "example-article/r1")["tagger"] == {
            "name": "Example User",
            "email": "4242+example-user@users.noreply.github.com",
            "date": "2026-10-27T12:00:00+00:00",
        }

    def test_a_user_without_a_profile_name_is_tagged_by_login(self, fake, gh):
        fake.user = {"login": "example-user", "id": 4242, "name": None}
        make_round(fake, gh, 1, merged=True)
        gr.tag_merged_rounds(gh, SLUG)
        assert tag_obj(fake, "example-article/r1")["tagger"]["name"] == "example-user"

    def test_the_message_names_the_pull_request_and_the_runs_in_the_markers(
        self, fake, gh
    ):
        pull = make_round(fake, gh, 1, runs=(6, 7))
        # A person's review, one that only quotes a marker, and a repeat of a run.
        gh.create_review(
            pull.number, commit_id=fake.branches[pull.head_ref], body="Looks fine."
        )
        gh.create_review(
            pull.number,
            commit_id=fake.branches[pull.head_ref],
            body="Compare\n<!-- ci-review run=99 -->",
        )
        gh.create_review(
            pull.number,
            commit_id=fake.branches[pull.head_ref],
            body="<!-- ci-review run=7 commit=abc1234 -->\nagain",
        )
        gh.merge_pull(pull.number, sha=fake.branches[pull.head_ref])
        [result] = gr.tag_merged_rounds(gh, SLUG)
        message = f"example-article r1: PR #{pull.number}, runs 6 and 7"
        assert tag_obj(fake, "example-article/r1")["message"] == message
        assert result.detail == message

    def test_a_round_with_no_review_has_no_runs_in_its_message(self, fake, gh):
        pull = make_round(fake, gh, 1, merged=True)
        gr.tag_merged_rounds(gh, SLUG)
        assert (
            tag_obj(fake, "example-article/r1")["message"]
            == f"example-article r1: PR #{pull.number}"
        )

    def test_a_second_call_makes_no_write(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2, merged=True)
        assert len(gr.tag_merged_rounds(gh, SLUG)) == 2
        before = len(fake.requests)
        assert gr.tag_merged_rounds(gh, SLUG) == []
        assert [r for r in fake.requests[before:] if r[0] != "GET"] == []

    def test_dry_run_reads_and_writes_nothing(self, fake, gh):
        make_round(fake, gh, 1, merged=True, runs=(4,))
        before = len(non_get(fake))
        [result] = gr.tag_merged_rounds(gh, SLUG, dry_run=True)
        assert result.status == gr.WOULD_CREATE and result.detail.endswith("run 4")
        assert len(non_get(fake)) == before and fake.tags == {}
        # No /user lookup either: a dry run needs no push rights.
        assert not [r for r in fake.requests if r[1] == "/user"]

    def test_a_pull_request_from_a_fork_is_not_tagged(self, fake, gh, caplog):
        make_round(fake, gh, 1, merged=True)
        real = gh.list_pulls

        def forked(state="open"):
            return [replace(p, head_repo="someone/fork") for p in real(state)]

        gh.list_pulls = forked
        with caplog.at_level(logging.WARNING):
            assert gr.tag_merged_rounds(gh, SLUG, repo=REPO) == []
        assert fake.tags == {} and "not from a branch of" in caplog.text

    def test_two_merged_pull_requests_for_one_round_tag_the_earlier(self, fake, gh):
        first = make_round(fake, gh, 1, merged=True)
        again = gh.create_pull("round/example-article/r1", "main", "again", "b")
        gh.merge_pull(again.number, sha=fake.branches["round/example-article/r1"])
        [result] = gr.tag_merged_rounds(gh, SLUG)
        assert result.pull == first.number
        assert result.commit == first.merge_commit_sha

    def test_a_slug_is_checked(self, gh):
        with pytest.raises(GitHubDocumentsError):
            gr.tag_merged_rounds(gh, "../x")


def _sig(gh):
    from datetime import datetime, timezone

    from ci_core.github_repo import Signature

    return Signature(
        "Someone", "someone@example.invalid", datetime(2026, 1, 1, tzinfo=timezone.utc)
    )


class TestRaces:
    """Creating the ref is the compare-and-swap; the loser reads the tag back."""

    def test_a_tag_that_appears_between_the_listing_and_the_create_is_success(
        self, fake, gh, monkeypatch
    ):
        pull = make_round(fake, gh, 1, merged=True)
        real = gh.matching_refs
        winner = {}

        def late(prefix):
            listed = real(prefix)  # the listing saw no tag ...
            winner["sha"] = gh.create_tag(
                "example-article/r1",
                "by the other command",
                pull.merge_commit_sha,
                tagger=_sig(gh),
            )  # ... and the other command made it a moment later
            return listed

        monkeypatch.setattr(gh, "matching_refs", late)
        [result] = gr.tag_merged_rounds(gh, SLUG)
        assert result.status == gr.PRESENT and not result.problem
        assert fake.tags["example-article/r1"] == winner["sha"]  # not moved

    def test_a_tag_that_wins_the_ref_after_our_tag_object_exists_is_success(
        self, fake, gh
    ):
        pull = make_round(fake, gh, 1, merged=True)
        theirs = {}

        def other_command_gets_there_first(f, body):
            # Our tag object is made; theirs takes the ref before our POST /git/refs.
            f.before_create_ref = None
            sha = "9" * 40
            f.tag_objects[sha] = {
                "tag": "example-article/r1",
                "message": "theirs",
                "object": pull.merge_commit_sha,
                "type": "commit",
                "tagger": {
                    "name": "n",
                    "email": "e",
                    "date": "2026-10-27T11:00:00+00:00",
                },
            }
            f.tags["example-article/r1"] = sha
            theirs["sha"] = sha

        fake.before_create_ref = other_command_gets_there_first
        [result] = gr.tag_merged_rounds(gh, SLUG)
        assert result.status == gr.PRESENT and not result.problem
        assert fake.tags["example-article/r1"] == theirs["sha"]

    def test_a_tag_pointing_elsewhere_is_a_warning_and_left_alone(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        elsewhere = fake.branches["main"]

        def squatter(f, body):
            f.before_create_ref = None
            f.tags["example-article/r1"] = f.commits[elsewhere]["parents"][0]

        fake.before_create_ref = squatter
        [result] = gr.tag_merged_rounds(gh, SLUG)
        assert result.status == gr.CONFLICT and result.problem
        assert "never moved" in result.detail or "left alone" in result.detail
        assert fake.tags["example-article/r1"] == fake.commits[elsewhere]["parents"][0]

    def test_a_lightweight_tag_at_the_same_commit_counts_as_tagged(self, fake, gh):
        pull = make_round(fake, gh, 1, merged=True)
        fake.tags["example-article/r1"] = pull.merge_commit_sha
        # It is in the listing, so the round is simply tagged already.
        assert gr.tag_merged_rounds(gh, SLUG) == []

    def test_two_commands_tagging_at_once_make_one_tag(self, fake, gh):
        pull = make_round(fake, gh, 1, merged=True)
        other = client(fake)
        # The other command lists first, then ours runs to the end, then theirs creates.
        real = other.matching_refs
        outcomes = []

        def lists_then_waits(prefix):
            listed = real(prefix)
            outcomes.extend(gr.tag_merged_rounds(gh, SLUG))
            return listed

        other.matching_refs = lists_then_waits
        theirs = gr.tag_merged_rounds(other, SLUG)
        assert [r.status for r in outcomes] == [gr.CREATED]
        assert [r.status for r in theirs] == [gr.PRESENT]
        assert list(fake.tags) == ["example-article/r1"]
        assert len(fake.tag_objects) == 1 or all(
            o["object"] == pull.merge_commit_sha for o in fake.tag_objects.values()
        )


class TestFailuresAreWarnings:
    def test_a_network_error_on_the_tag_is_a_failed_result_not_an_exception(
        self, fake, gh
    ):
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2, merged=True)
        fake.inject("POST", "/git/tags", Resp(500, {"message": "boom"}))
        results = gr.tag_merged_rounds(gh, SLUG)
        assert [(r.round, r.status) for r in results] == [
            (1, gr.FAILED),
            (2, gr.CREATED),
        ]
        assert "500" in results[0].detail and results[0].problem
        assert list(fake.tags) == ["example-article/r2"]

    def test_a_rate_limit_stops_the_loop(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2, merged=True)
        fake.inject(
            "POST",
            "/git/tags",
            Resp(403, {"message": "rate"}, headers={"x-ratelimit-remaining": "0"}),
        )
        results = gr.tag_merged_rounds(gh, SLUG)
        assert [(r.round, r.status) for r in results] == [(1, gr.FAILED)]
        assert fake.tags == {}

    def test_a_failure_to_list_is_one_failed_result(self, fake, gh):
        fake.inject("GET", "/pulls", Resp(500, {"message": "boom"}))
        [result] = gr.tag_merged_rounds(gh, SLUG)
        assert result.status == gr.FAILED and "could not list" in result.detail

    def test_a_token_without_a_user_fails_the_round_not_the_run(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        fake.inject("GET", "/user", Resp(403, {"message": "Resource not accessible"}))
        [result] = gr.tag_merged_rounds(gh, SLUG)
        assert result.status == gr.FAILED and fake.tags == {}

    def test_tag_before_write_turns_any_exception_into_a_warning(self, fake, gh):
        warned = []

        def boom(*a, **k):
            raise RuntimeError("a bug")

        with patch.object(gr, "tag_merged_rounds", boom):
            assert gr.tag_before_write(gh, SLUG, say=print, warn=warned.append) == []
        assert "RuntimeError: a bug" in warned[0]

    def test_tag_before_write_says_what_it_did_and_warns_about_problems(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2, merged=True)
        fake.inject("POST", "/git/tags", Resp(500, {"message": "boom"}))
        said, warned = [], []
        gr.tag_before_write(gh, SLUG, say=said.append, warn=warned.append)
        assert len(said) == 1 and said[0].startswith("Tagged example-article/r2 at ")
        assert len(warned) == 1 and warned[0].startswith(
            "example-article/r1 not tagged"
        )


# -- wiring: ci-review --github --write-back ------------------------------------------


class TestReviewWiring:
    def load(self, fake, **kw):
        return gd.load_handoff(SLUG, settings=SETTINGS, client=client(fake), **kw)

    def two_rounds(self, fake, gh):
        merged = make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2)
        return merged

    def test_write_back_tags_a_merged_round_before_its_own_work(self, fake, gh):
        merged = self.two_rounds(fake, gh)
        handoff = self.load(fake, write_back=True)
        assert tag_obj(fake, "example-article/r1")["object"] == merged.merge_commit_sha
        assert handoff["document_source"]["round"] == 2
        first_tag = next(
            i
            for i, r in enumerate(fake.requests)
            if r[0] == "POST" and "/git/tags" in r[1]
        )
        first_read = next(
            i for i, r in enumerate(fake.requests) if "/contents/" in r[1]
        )
        assert first_tag < first_read  # the tag came before the round's files were read

    def test_a_failure_to_tag_does_not_stop_the_run(self, fake, gh, caplog):
        self.two_rounds(fake, gh)
        fake.inject("POST", "/git/tags", Resp(500, {"message": "boom"}))
        with caplog.at_level(logging.WARNING):
            handoff = self.load(fake, write_back=True)
        assert handoff["document_source"]["round"] == 2
        assert "example-article/r1 not tagged" in caplog.text

    def test_a_github_run_without_write_back_makes_no_write(self, fake, gh):
        self.two_rounds(fake, gh)
        before = len(non_get(fake))
        handoff = self.load(fake)
        assert handoff["document_source"]["round"] == 2
        assert len(non_get(fake)) == before and fake.tags == {}

    @pytest.mark.parametrize("extra", [["--replay", "x_results.json"], ["--offline"]])
    def test_replay_and_offline_never_write_back_or_tag(
        self, fake, gh, extra, monkeypatch
    ):
        self.two_rounds(fake, gh)
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        monkeypatch.setattr(gd, "GitHubRepo", lambda name, token: client(fake))
        args = pipeline.build_parser().parse_args(
            ["--github", SLUG, "--github-repo", REPO, "--publication", "p", *extra]
        )
        settings = {**SETTINGS, "write_back": True}
        # write_back: true in config is set aside for these runs ...
        write_back = pipeline._decide_write_back(args, settings)
        assert write_back is False
        before = len(non_get(fake))
        pipeline._read_github_handoff(args, settings, write_back)
        # ... so the read, which is all they do, writes nothing.
        assert len(non_get(fake)) == before and fake.tags == {}


# -- wiring: ci-docs ----------------------------------------------------------------


def run_cli(fake, *argv, config=None):
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


def cli(fake, command, *rest):
    return run_cli(fake, command, SLUG, *rest, "--github-repo", REPO)


class Spy:
    """Counts calls to ``github_rounds.tag_before_write`` without changing what it does."""

    def __init__(self, monkeypatch):
        self.calls = []
        real = gr.tag_before_write

        def spy(client, slug, **kw):
            self.calls.append(slug)
            return real(client, slug, **kw)

        monkeypatch.setattr(gr, "tag_before_write", spy)
        # github_put imported the name; point it at the spy too.
        from ci_article_review import github_put

        monkeypatch.setattr(github_put, "tag_before_write", spy)


class TestCommandWiring:
    def test_ls_never_tags_or_writes(self, fake, gh, monkeypatch):
        spy = Spy(monkeypatch)
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2)
        before = len(non_get(fake))
        code, out = cli(fake, "ls")
        assert code == 0 and spy.calls == []
        assert len(non_get(fake)) == before and fake.tags == {}
        assert "merged, not yet tagged" in out

    def test_put_tags_once_before_its_own_commit(self, fake, gh, monkeypatch, tmp_path):
        spy = Spy(monkeypatch)
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2)
        note = tmp_path / "close-out-to-chat.md"
        note.write_text("CLOSE-OUT HANDOFF\nContains: 1 edit\n", encoding="utf-8")
        code, out = cli(fake, "put", note)
        assert code == 0 and spy.calls == [SLUG]
        assert "Tagged example-article/r1" in out
        tag_i = next(
            i
            for i, r in enumerate(fake.requests)
            if "/git/tags" in r[1] and r[0] == "POST"
        )
        commit_i = next(
            i
            for i, r in enumerate(fake.requests)
            if r[0] == "POST" and r[1].endswith("/git/commits")
        )
        assert tag_i < commit_i

    def test_accept_without_yes_writes_and_tags_nothing(self, fake, gh, monkeypatch):
        spy = Spy(monkeypatch)
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2)
        before = len(non_get(fake))
        code, _ = cli(fake, "accept")
        assert code == 0 and spy.calls == []
        assert len(non_get(fake)) == before and fake.tags == {}

    def test_accept_yes_tags_before_and_after_the_merge(self, fake, gh, monkeypatch):
        spy = Spy(monkeypatch)
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2)
        code, _ = cli(fake, "accept", "--yes")
        assert code == 0 and spy.calls == [SLUG, SLUG]
        assert tag_names(fake) == ["example-article/r1", "example-article/r2"]

    def test_tag_runs_the_step_alone(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2, merged=True)
        code, out = cli(fake, "tag")
        assert code == 0 and tag_names(fake) == [
            "example-article/r1",
            "example-article/r2",
        ]
        assert out.count("Tagged ") == 2
        again_code, again = cli(fake, "tag")
        assert again_code == 0 and "Nothing to tag" in again

    def test_tag_dry_run_writes_nothing(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        before = len(non_get(fake))
        code, out = cli(fake, "tag", "--dry-run")
        assert code == 0 and "Would tag example-article/r1" in out
        assert len(non_get(fake)) == before and fake.tags == {}

    def test_tag_exits_1_when_a_tag_could_not_be_made(self, fake, gh):
        make_round(fake, gh, 1, merged=True)
        fake.inject("POST", "/git/tags", Resp(500, {"message": "boom"}))
        code, out = cli(fake, "tag")
        assert code == 1 and "warning: example-article/r1 not tagged" in out

    def test_tag_will_not_write_to_a_repository_the_token_cannot_push_to(
        self, fake, gh
    ):
        make_round(fake, gh, 1, merged=True)
        orig = fake._repo_json
        fake._repo_json = lambda: {**orig(), "permissions": {"push": False}}
        code, _ = cli(fake, "tag")
        assert code == 1 and fake.tags == {}

    def push_run_report(self, tmp_path, *, replay=False, kind="github"):
        import json

        folder = tmp_path / ("_replay" if replay else "hist")
        folder.mkdir()
        report = folder / "run_4_20261026_143022_report.json"
        source = {
            "kind": kind,
            "repo": REPO,
            "ref": "round/example-article/r2",
            "files": [{"path": f"{FOLDER}/draft.md", "blob_sha": "0" * 40}],
        }
        report.write_text(json.dumps({"document_source": source}), encoding="utf-8")
        return report

    def test_push_run_tags_once_for_the_article_the_report_names(
        self, fake, gh, tmp_path, monkeypatch
    ):
        make_round(fake, gh, 1, merged=True)
        make_round(fake, gh, 2)
        report = self.push_run_report(tmp_path)
        from ci_article_review import github_writeback

        pushed = []
        monkeypatch.setattr(
            github_writeback, "push_saved_run", lambda *a, **k: pushed.append(a)
        )
        code, out = run_cli(fake, "push-run", report)
        assert code == 0 and len(pushed) == 1
        assert tag_names(fake) == ["example-article/r1"] and "Tagged " in out

    @pytest.mark.parametrize("variant", ["replay", "not-github"])
    def test_push_run_of_a_report_it_will_refuse_tags_nothing(
        self, fake, gh, tmp_path, monkeypatch, variant
    ):
        make_round(fake, gh, 1, merged=True)
        report = self.push_run_report(
            tmp_path,
            replay=variant == "replay",
            kind="local" if variant == "not-github" else "github",
        )
        before = len(fake.requests)
        results = gr.tag_for_saved_run(
            report,
            settings=SETTINGS,
            token_lookup=lambda: TOKEN,
            session=fake,
            out=lambda line: None,
        )
        assert results == [] and fake.tags == {} and len(fake.requests) == before

    def test_push_run_without_a_token_or_push_rights_is_silent(
        self, fake, gh, tmp_path
    ):
        make_round(fake, gh, 1, merged=True)
        report = self.push_run_report(tmp_path)
        lines = []
        assert (
            gr.tag_for_saved_run(
                report,
                settings=SETTINGS,
                token_lookup=lambda: None,
                session=fake,
                out=lines.append,
            )
            == []
        )
        orig = fake._repo_json
        fake._repo_json = lambda: {**orig(), "permissions": {"push": False}}
        assert (
            gr.tag_for_saved_run(
                report,
                settings=SETTINGS,
                token_lookup=lambda: TOKEN,
                session=fake,
                out=lines.append,
            )
            == []
        )
        assert lines == [] and fake.tags == {}


# -- ci-docs accept -----------------------------------------------------------------


class TestAccept:
    def test_without_yes_it_shows_the_round_and_merges_nothing(self, fake, gh):
        pull = make_round(fake, gh, 1, runs=(3,))
        head = fake.branches[pull.head_ref]
        before = len(non_get(fake))
        code, out = cli(fake, "accept")
        assert code == 0 and len(non_get(fake)) == before
        assert f"#{pull.number}" in out and head[:7] in out
        assert "run 3, of this head" in out and "no commit has landed since" in out
        assert "Not merged" in out and "--yes" in out
        assert fake.pulls[pull.number]["state"] == "open"

    def test_it_says_when_commits_landed_since_the_review(self, fake, gh):
        pull = make_round(fake, gh, 1, runs=(3,))
        fake.external_commit(
            pull.head_ref, {f"{FOLDER}/draft.md": b"later\n"}, "revise"
        )
        fake.external_commit(
            pull.head_ref, {f"{FOLDER}/draft.md": b"later 2\n"}, "revise 2"
        )
        _, out = cli(fake, "accept")
        assert "2 commits landed since" in out and "does not cover the head" in out

    def test_it_says_when_there_is_no_review(self, fake, gh):
        make_round(fake, gh, 1)
        _, out = cli(fake, "accept")
        assert "no ci-review review on this pull request yet" in out

    def test_it_reads_the_newest_review_with_a_marker(self, fake, gh):
        pull = make_round(fake, gh, 1, runs=(3,))
        gh.create_review(
            pull.number, commit_id=fake.branches[pull.head_ref], body="A person."
        )
        gh.create_review(
            pull.number,
            commit_id=fake.branches[pull.head_ref],
            body="<!-- ci-review run=8 -->\nlater",
        )
        _, out = cli(fake, "accept")
        assert "run 8," in out

    def test_yes_merges_the_head_it_showed_and_tags(self, fake, gh):
        pull = make_round(fake, gh, 2, runs=(5,))
        head = fake.branches[pull.head_ref]
        code, out = cli(fake, "accept", "--yes")
        assert code == 0
        [merge] = [
            r for r in fake.requests if r[0] == "PUT" and r[1].endswith("/merge")
        ]
        assert merge[3] == {
            "sha": head,
            "merge_method": "merge",
            "commit_title": f"example-article r2 (#{pull.number})",
        }
        done = fake.pulls[pull.number]
        assert done["merged"] and done["merge_method"] == "merge"
        assert tag_obj(fake, "example-article/r2")["object"] == done["merge_commit_sha"]
        assert (
            f"Merged pull request #{pull.number}" in out
            and "Tagged example-article/r2" in out
        )

    def test_a_head_that_moved_since_it_was_shown_merges_nothing(
        self, fake, gh, monkeypatch
    ):
        pull = make_round(fake, gh, 1, runs=(5,))
        from ci_core.github_repo import GitHubRepo

        real = GitHubRepo.merge_pull

        def moved(self, number, **kw):
            fake.external_commit(
                pull.head_ref, {f"{FOLDER}/draft.md": b"pushed\n"}, "late"
            )
            return real(self, number, **kw)

        monkeypatch.setattr(GitHubRepo, "merge_pull", moved)
        code, out = cli(fake, "accept", "--yes")
        assert code == 1
        assert fake.pulls[pull.number]["state"] == "open" and fake.tags == {}

    def test_the_conflict_message_says_nothing_was_merged(
        self, fake, gh, monkeypatch, capsys
    ):
        pull = make_round(fake, gh, 1)
        fake.external_commit(pull.head_ref, {f"{FOLDER}/draft.md": b"x\n"}, "late")
        from ci_core.github_repo import GitHubRepo

        def stale(self, number, *, sha, **kw):
            raise GitHubRepoError(
                "conflict",
                "pull request 1 was not merged: Head branch was modified",
                status=409,
            )

        monkeypatch.setattr(GitHubRepo, "merge_pull", stale)
        code, _ = cli(fake, "accept", "--yes")
        assert code == 1
        assert "Nothing was merged" in capsys.readouterr().err

    def test_two_open_rounds_ask_for_pr(self, fake, gh, capsys):
        make_round(fake, gh, 1)
        second = make_round(fake, gh, 2)
        code, _ = cli(fake, "accept", "--yes")
        err = capsys.readouterr().err
        assert code == 1 and "--pr" in err and "round/example-article/r1" in err
        assert [r for r in fake.requests if r[0] == "PUT"] == []
        code, out = cli(fake, "accept", "--yes", "--pr", second.number)
        assert code == 0 and fake.pulls[second.number]["merged"]

    def test_no_open_round_is_an_error(self, fake, gh, capsys):
        make_round(fake, gh, 1, merged=True)
        code, _ = cli(fake, "accept")
        assert code == 1 and "no open round" in capsys.readouterr().err

    def test_a_merged_pull_request_cannot_be_accepted_again(self, fake, gh, capsys):
        pull = make_round(fake, gh, 1, merged=True)
        code, _ = cli(fake, "accept", "--pr", pull.number)
        assert code == 1 and "is merged" in capsys.readouterr().err

    def test_a_pull_request_of_another_article_is_refused(self, fake, gh, capsys):
        other = make_round(fake, gh, 1, slug="other-article")
        code, _ = cli(fake, "accept", "--pr", other.number)
        assert code == 1 and "not a round of" in capsys.readouterr().err

    def test_yes_needs_push_rights_but_showing_does_not(self, fake, gh, capsys):
        make_round(fake, gh, 1)
        orig = fake._repo_json
        fake._repo_json = lambda: {**orig(), "permissions": {"push": False}}
        assert cli(fake, "accept")[0] == 0
        assert cli(fake, "accept", "--yes")[0] == 1
        assert "cannot push" in capsys.readouterr().err

    def test_a_public_repository_is_not_merged_into(self, fake, gh, capsys):
        make_round(fake, gh, 1)
        orig = fake._repo_json
        fake._repo_json = lambda: {**orig(), "private": False}
        code, _ = cli(fake, "accept", "--yes")
        assert code == 1 and "public repository" in capsys.readouterr().err
