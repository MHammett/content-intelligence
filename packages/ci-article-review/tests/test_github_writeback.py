"""``--write-back`` and ``ci-docs push-run``: a run's review goes to the round's pull request (#417).

The GitHub side is the real ``ci_core.github_repo.GitHubRepo`` talking to an
in-process fake (``RoundGitHub`` below: the ``FakeGitHub`` of
``test_documents_init.py``, made strict about fast-forwards as GitHub is, and
given pull requests and reviews as ``ci-core``'s fake has them), so the Git Data
calls, the compare-and-swap on the files the pipeline owns and the retry on a
moved branch are the real ones. What is tested here is what this phase owns:
which files go under which fixed names in one commit with LF endings and the
documented message; the one review that follows (event COMMENT, a marker, no line
comments, the Section 1 findings as a numbered checklist, no worklist text); that
both steps can be repeated safely; and that every failure after the review keeps
the local files and names the retry.

Slugs and contents are invented; this repository is public.
"""

import json
import logging
import re
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

from ci_article_review import (
    documents_cli,
    github_writeback as gw,
    pipeline,
    review_body,
)
from ci_article_review.github_documents import GitHubDocumentsError
from ci_article_review.report_markdown import (
    consensus_findings,
    render_report_markdown,
)
from ci_article_review.worklist import build_worklist, render_worklist
from ci_core.github_repo import GitHubRepo

REPO = "example-owner/example-articles"
TOKEN = "ghp_exampleSECRETtoken0123456789"
SLUG = "example-article"
FOLDER = f"articles/{SLUG}"
BRANCH = f"round/{SLUG}/r4"
PR = 12
TS = "20261026_143022"
RUN = f"run_4_{TS}"
KEY = "sk-example-provider-key-0123456789"
SETTINGS = {"include_capture": False, "allow_public_repo": False}
API_KEYS = {"openai": {"api_key": KEY}, "languagetool": {"username": "", "api_key": ""}}

#: Written by the pipeline, every run, under these names.
OWNED = [
    f"{FOLDER}/review.md",
    f"{FOLDER}/pipeline/run.json",
    f"{FOLDER}/pipeline/report.json",
    f"{FOLDER}/pipeline/worklist.md",
]


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


class RoundGitHub(StrictGitHub):
    """Pull requests and their reviews, over the same repository."""

    def __init__(self, **kw):
        super().__init__(**kw)
        #: number -> {"head", "base", "state", "merged", "head_repo"}
        self.pulls = {}
        #: number -> [review dict], oldest first
        self.reviews = {}
        #: every body posted to ``.../reviews``, as the client sent it
        self.review_posts = []
        #: responses to give the next review posts instead of accepting them
        self.review_errors = []

    def _pull_json(self, number):
        pull = self.pulls[number]
        return {
            "number": number,
            "state": pull["state"],
            "merged": pull["merged"],
            "merged_at": "2026-10-27T00:00:00Z" if pull["merged"] else None,
            "merge_commit_sha": None,
            "head": {
                "ref": pull["head"],
                "sha": self.branches[pull["head"]],
                "repo": {"full_name": pull.get("head_repo", REPO)},
            },
            "base": {"ref": pull["base"], "sha": self.branches[pull["base"]]},
            "title": f"{SLUG} r4",
            "html_url": f"https://github.com/{REPO}/pull/{number}",
        }

    def _ancestors(self, sha):
        seen, todo = [], [sha]
        while todo:
            commit = todo.pop()
            if commit not in seen:
                seen.append(commit)
                todo.extend(self.commits[commit]["parents"])
        return seen

    def _compare(self, base, head):
        """``GET /compare/base...head`` as GitHub answers it, for linear histories."""
        if base == head:
            return {"status": "identical", "ahead_by": 0, "behind_by": 0}
        if base in self._ancestors(head):
            return {"status": "ahead", "ahead_by": 1, "behind_by": 0}
        if head in self._ancestors(base):
            return {"status": "behind", "ahead_by": 0, "behind_by": 1}
        return {"status": "diverged", "ahead_by": 1, "behind_by": 1}

    def _route(self, method, path, headers, params, body):
        if (method, path) == ("GET", "/pulls"):
            # The listing the automatic tagging step (#418) reads before a
            # push-run writes; a merged pull request is "closed" there.
            wanted = (params or {}).get("state", "open")
            return Resp(
                200,
                [
                    self._pull_json(n)
                    for n, p in sorted(self.pulls.items())
                    if wanted == "all" or (p["state"] == "open") == (wanted == "open")
                ],
            )
        if method == "GET" and path.startswith("/git/matching-refs/"):
            return Resp(200, [])  # no tags: nothing to tell from a tagged round
        compared = re.fullmatch(r"/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)", path)
        if method == "GET" and compared:
            return Resp(200, self._compare(*compared.groups()))
        found = re.fullmatch(r"/pulls/(\d+)(/reviews)?", path)
        if not found:
            return super()._route(method, path, headers, params, body)
        number = int(found.group(1))
        if number not in self.pulls:
            return Resp(404, {"message": "Not Found"})
        if not found.group(2):
            return Resp(200, self._pull_json(number))
        if method == "GET":
            return Resp(200, list(self.reviews.get(number, [])))
        self.review_posts.append(body)
        if self.review_errors:
            return self.review_errors.pop(0)
        assert set(body) == {"commit_id", "body", "event"}, body  # no line comments
        reviews = self.reviews.setdefault(number, [])
        review = {
            "id": 1000 + len(reviews),
            "body": body["body"],
            "commit_id": body["commit_id"],
            "state": "COMMENTED",
            "html_url": f"https://github.com/{REPO}/pull/{number}#pullrequestreview-{1000 + len(reviews)}",
        }
        reviews.append(review)
        return Resp(200, review)

    def posted(self, number=PR):
        return self.reviews.get(number, [])


def read_source(fake, commit=None):
    """``document_source`` as a read of the round at ``commit`` (default: ``fake.read_at``) leaves it."""
    commit = commit or fake.read_at
    tree = fake.trees[fake.commits[commit]["tree"]]
    return {
        "kind": "github",
        "repo": REPO,
        "ref": BRANCH,
        "commit_sha": commit,
        "round": 4,
        "pull_request": PR,
        "base_sha": fake.branches["main"],
        "files": [
            {"path": p, "blob_sha": tree[p]}
            for p in (f"{FOLDER}/draft.md", f"{FOLDER}/metadata.md")
        ],
    }


@pytest.fixture
def fake():
    stub = RoundGitHub()
    stub.branches[BRANCH] = stub.branches["main"]
    stub.read_at = stub.external_commit(
        BRANCH,
        {
            f"{FOLDER}/draft.md": b"# Example\n\nA draft.\n",
            f"{FOLDER}/metadata.md": b"metadata\n",
        },
        "round 4",
    )
    stub.pulls[PR] = {"head": BRANCH, "base": "main", "state": "open", "merged": False}
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


def finding(passage, problem, *, models=3, seen=None):
    entry = {
        "passage": passage,
        "models": [f"m{i}:voice_style" for i in range(models)],
        "weight_sum": float(models),
        "flags": [
            {
                "passage": passage,
                "problem": problem,
                "domain": "voice_style",
                "source_model": "m0",
            }
        ],
    }
    if seen is not None:
        entry.update(reproduced_in=seen, reproduced_of=3)
    return entry


#: Listed new / strong / partial, so Section 1's bands (strong, partial, new)
#: number them 2, 3, 1 -- the F numbers are not the list positions.
FINDINGS = [
    finding("A passage that is new this run.", "It overclaims.", seen=0),
    finding("A passage raised in every earlier run.", "It is filler.", seen=3),
    finding("A passage raised in one earlier run.", "It does not follow.", seen=1),
]

#: A citation nobody could read: it puts one action on the worklist.
CITATION = {
    "claim": "A claim only the author can settle",
    "url": "https://example.org/page",
    "resolved": True,
    "content_kind": "html",
    "content_summary": "",
    "verification": "failed",
    "source_name": "Example",
}


def make_report(source, *, run=4, findings=FINDINGS, **extra):
    return {
        "run_number": run,
        "generated": "2026-10-26",
        "article_title": "Example",
        "publication": "testpub",
        "ensemble": {"cost_preset": "wide"},
        "cost_summary": {"total_usd": 0.7276},
        "section_1_consensus": findings,
        "section_9_citations": [CITATION],
        "reproducibility": {"comparable_run_count": 3},
        "document_source": source,
        **extra,
    }


def saved_run(tmp_path, source, *, run=4, ts=TS, capture=True, worklist=True, **extra):
    """A run on disk as ``hist.save_run`` leaves it on Windows: CRLF text files."""
    history = tmp_path / "pipeline_history" / SLUG
    history.mkdir(parents=True, exist_ok=True)
    report = make_report(source, run=run, **extra)
    path = history / f"run_{run}_{ts}_report.json"
    path.write_bytes(json.dumps(report, indent=2).replace("\n", "\r\n").encode())
    crlf = lambda text: text.replace("\n", "\r\n").encode()  # noqa: E731
    (history / f"run_{run}_{ts}_review.md").write_bytes(
        crlf(render_report_markdown(report))
    )
    if worklist:
        (history / f"run_{run}_{ts}_worklist.md").write_bytes(
            crlf("\n".join(render_worklist(build_worklist(report))) + "\n")
        )
    if capture:
        (history / f"run_{run}_{ts}_results.json").write_bytes(b'{"results": []}\r\n')
    return path


def push(path, *, settings=SETTINGS, api_keys=API_KEYS, **kw):
    lines = []
    result = gw.push_saved_run(
        path, settings=settings, api_keys=api_keys, out=lines.append, **kw
    )
    return result, lines


def numbered(review_md):
    """``{number: passage}`` as ``#### N. "passage"`` headings in a review.md."""
    return {
        int(n): passage
        for n, passage in re.findall(r'^#### (\d+)\. "(.*)"\s*$', review_md, re.M)
    }


def checklist(body):
    """``[(number, text after the F)]`` for each task item in a review body."""
    return [
        (int(n), rest)
        for n, rest in re.findall(r"^- \[ \] \*\*F(\d+)\*\* (.*)$", body, re.M)
    ]


class TestPushSavedRun:
    def test_the_files_go_in_one_commit_under_fixed_names_with_lf_endings(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))
        before = len(client.commits)
        result, lines = push(path)
        assert len(client.commits) == before + 1
        assert client.branches[BRANCH] == result.commit.commit_sha
        assert client.branches["main"] != result.commit.commit_sha
        assert set(result.written) == set(OWNED)
        for owned in OWNED:
            assert b"\r" not in client.file(owned, BRANCH), owned
        local = path.with_name(f"{RUN}_review.md").read_bytes().replace(b"\r\n", b"\n")
        assert client.file(f"{FOLDER}/review.md", BRANCH) == local
        assert (
            json.loads(client.file(f"{FOLDER}/pipeline/report.json", BRANCH))[
                "run_number"
            ]
            == 4
        )
        # The worklist is committed, in pipeline/ only.
        assert client.file(f"{FOLDER}/pipeline/worklist.md", BRANCH).startswith(
            b"## WORKLIST"
        )
        # The capture is not committed unless asked for.
        assert f"{FOLDER}/pipeline/results.json" not in client.tree(BRANCH)
        # The commit URL is printed, and the review URL is the last line.
        assert lines[0].startswith("Wrote 4 file(s)") and "/commit/" in lines[0]
        assert "pullrequestreview-" in lines[-1]

    def test_include_capture_adds_results_json(self, client, tmp_path):
        result, _ = push(
            saved_run(tmp_path, read_source(client)),
            settings={**SETTINGS, "include_capture": True},
        )
        assert f"{FOLDER}/pipeline/results.json" in result.written
        assert len(result.written) == 5
        assert (
            client.file(f"{FOLDER}/pipeline/results.json", BRANCH)
            == b'{"results": []}\n'
        )

    def test_run_json_is_what_a_later_run_numbers_itself_from(self, client, tmp_path):
        push(saved_run(tmp_path, read_source(client)))
        raw = client.file(f"{FOLDER}/pipeline/run.json", BRANCH)
        assert raw.endswith(b"}\n") and b"\r" not in raw
        assert json.loads(raw) == {
            "cost_reported_usd": 0.7276,
            "history_key": SLUG,
            "history_name": RUN,
            "preset": "wide",
            "pull_request": PR,
            "reviewed_commit": client.read_at,
            "round": 4,
            "run_number": 4,
        }
        # Sorted keys, and no review id: the review is posted after this commit.
        assert list(json.loads(raw)) == sorted(json.loads(raw))
        assert not {"review", "review_id", "review_url"} & set(json.loads(raw))

    def test_the_commit_message_is_the_documented_one(self, client, tmp_path):
        result, _ = push(saved_run(tmp_path, read_source(client)))
        assert client.commits[result.commit.commit_sha]["message"] == (
            f"review: {SLUG} r4 run 4\n"
            "\n"
            f"Reviewed: draft.md, metadata.md at {client.read_at[:7]} (PR #{PR})\n"
            f"Run: {RUN} (preset wide, $0.73 reported)\n"
            "Files: review.md, pipeline/run.json, pipeline/report.json, "
            "pipeline/worklist.md\n"
        )

    def test_a_second_push_of_the_same_run_does_nothing(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        first, _ = push(path)
        commits, posts = len(client.commits), len(client.review_posts)
        second, lines = push(path)
        assert second.written == () and set(second.unchanged) == set(first.written)
        assert len(client.commits) == commits
        assert client.branches[BRANCH] == first.commit.commit_sha
        # No second review: it finds the first one by its marker.
        assert len(client.review_posts) == posts == 1
        assert second.review_posted is False
        assert second.review.html_url == first.review.html_url
        assert "Nothing new to write" in lines[0]
        assert "already on pull request" in lines[-1]
        assert lines[-1].endswith(first.review.html_url)

    def test_a_review_that_failed_to_post_is_the_only_thing_a_retry_posts(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))
        client.review_errors.append(
            Resp(422, {"message": "Validation Failed", "errors": ["a reason"]})
        )
        with pytest.raises(GitHubDocumentsError) as err:
            push(path)
        # The commit landed; the message says so, and says why the review did not.
        assert "committed" in str(err.value) and "a reason" in str(err.value)
        head = client.branches[BRANCH]
        assert set(OWNED) <= set(client.tree(BRANCH)) and client.posted() == []
        result, _ = push(path)
        assert client.branches[BRANCH] == head  # no new commit
        assert result.review_posted is True and len(client.posted()) == 1
        again, _ = push(path)
        assert again.review_posted is False and len(client.posted()) == 1

    def test_the_review_is_of_the_commit_that_was_read(self, client, tmp_path):
        push(saved_run(tmp_path, read_source(client)))
        (post,) = client.review_posts
        assert post["commit_id"] == client.read_at
        assert post["event"] == "COMMENT"
        assert "comments" not in post
        body = post["body"]
        assert body.startswith(f"<!-- ci-review run=4 commit={client.read_at} -->\n")
        assert review_body.parse_marker(body) == (4, client.read_at)
        assert len(body) <= review_body.MAX_BODY

    def test_the_body_says_the_run_and_where_the_whole_review_is(
        self, client, tmp_path
    ):
        result, _ = push(saved_run(tmp_path, read_source(client)))
        body = client.posted()[0]["body"]
        assert f"`{RUN}`, preset wide, $0.73 reported" in body
        assert f"draft.md, metadata.md at `{client.read_at[:7]}` (PR #{PR})" in body
        assert "One run is not a measurement" in body
        assert "- Section 1, consensus flags: 3" in body
        assert "- Section 9, citations: 1" in body
        commit = result.commit.commit_sha
        assert f"https://github.com/{REPO}/blob/{commit}/{FOLDER}/review.md" in body
        assert (
            f"`get_file_contents` with `path` `{FOLDER}/review.md` and "
            f"`ref` `refs/pull/{PR}/head`" in body
        )

    def test_every_section_1_finding_is_a_task_numbered_as_in_review_md(
        self, client, tmp_path
    ):
        push(saved_run(tmp_path, read_source(client)))
        review_md = client.file(f"{FOLDER}/review.md", BRANCH).decode()
        body = client.posted()[0]["body"]
        in_review = numbered(review_md)
        assert sorted(in_review) == [1, 2, 3]
        tasks = checklist(body)
        assert [k for k, _ in tasks] == [1, 2, 3]
        # Section 1 puts the finding raised in every earlier run first, so F1 is
        # not the first finding in the report's list: the numbers are review.md's.
        assert in_review[1] == "A passage raised in every earlier run."
        for k, rest in tasks:
            assert rest.startswith(f'"{in_review[k]}": ')
        assert tasks[0][1] == '"A passage raised in every earlier run.": It is filler.'
        assert [k for k, _ in consensus_findings(make_report(None))] == [1, 2, 3]

    def test_no_worklist_text_reaches_the_review(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        report = json.loads(path.read_text(encoding="utf-8"))
        worklist = build_worklist(report)
        rendered = "\n".join(render_worklist(worklist))
        assert "Open by hand, or find another copy" in rendered  # there is an item
        push(path)
        body = client.posted()[0]["body"]
        for text in (
            "Open by hand, or find another copy",
            "WORKLIST",
            "A claim only the author can settle",
            "https://example.org/page",
            "tool gap",
        ):
            assert text not in body, text
        # The worklist is still delivered, where the chat is told not to read it.
        assert b"find another copy" in client.file(
            f"{FOLDER}/pipeline/worklist.md", BRANCH
        )

    def test_a_missing_worklist_is_skipped_but_a_missing_review_stops_it(
        self, client, tmp_path, caplog
    ):
        path = saved_run(tmp_path, read_source(client), worklist=False, capture=False)
        with caplog.at_level(logging.WARNING):
            result, _ = push(path)
        assert len(result.written) == 3 and "worklist.md was not saved" in caplog.text
        (path.parent / f"{RUN}_review.md").unlink()
        with pytest.raises(GitHubDocumentsError, match="_review.md is missing"):
            push(path)

    def test_a_second_run_of_the_round_replaces_the_first_runs_files(
        self, client, tmp_path
    ):
        first, _ = push(saved_run(tmp_path, read_source(client)))
        # The next run reads the branch as the first left it.
        second_source = read_source(client, first.commit.commit_sha)
        second, _ = push(
            saved_run(tmp_path, second_source, run=5, ts="20261027_090000"),
        )
        # The worklist is the same text both runs, so it is left alone.
        assert set(second.written) == set(OWNED) - {f"{FOLDER}/pipeline/worklist.md"}
        assert second.unchanged == (f"{FOLDER}/pipeline/worklist.md",)
        assert (
            json.loads(client.file(f"{FOLDER}/pipeline/run.json", BRANCH))["run_number"]
            == 5
        )
        assert [r["commit_id"] for r in client.posted()] == [
            client.read_at,
            first.commit.commit_sha,
        ]
        assert [review_body.parse_marker(r["body"])[0] for r in client.posted()] == [
            4,
            5,
        ]


class TestTheBranchMovedSinceTheRead:
    def test_a_draft_edited_since_it_was_read_is_said_so_in_both_places(
        self, client, tmp_path
    ):
        source = read_source(client)
        chat = client.external_commit(
            BRANCH, {f"{FOLDER}/draft.md": b"# Example\n\nA revised draft.\n"}, "chat"
        )
        result, _ = push(saved_run(tmp_path, source))
        commit = client.commits[result.commit.commit_sha]
        assert commit["parents"] == [chat]  # landed on top of the chat's commit
        note = "Note: draft.md changed at its own path since it was read"
        assert note in commit["message"]
        assert "metadata.md changed" not in commit["message"]
        # The review is still of the commit that was read, and says so.
        assert client.posted()[0]["commit_id"] == client.read_at
        body = client.posted()[0]["body"]
        assert note in body and f"This review is of `{client.read_at[:7]}`" in body
        # The chat's edit survived.
        assert client.file(f"{FOLDER}/draft.md", BRANCH).endswith(b"A revised draft.\n")

    def test_a_branch_that_moves_during_the_commit_is_built_on(self, client, tmp_path):
        client.before_ref_update.append(
            lambda: client.external_commit(
                BRANCH, {f"{FOLDER}/notes.md": b"notes\n"}, "drafting side"
            )
        )
        result, _ = push(saved_run(tmp_path, read_source(client)))
        assert len(client.ref_updates()) == 2  # refused once, then accepted
        parent = client.commits[result.commit.commit_sha]["parents"][0]
        assert client.commits[parent]["message"] == "drafting side"
        assert f"{FOLDER}/notes.md" in client.tree(BRANCH)  # the other writer survived
        assert set(OWNED) <= set(client.tree(BRANCH))

    def test_a_pipeline_file_changed_since_the_read_is_not_overwritten(
        self, client, tmp_path
    ):
        # An earlier run's review.md was there when this run read the round ...
        earlier = client.external_commit(
            BRANCH, {f"{FOLDER}/review.md": b"an earlier review\n"}, "run 3"
        )
        source = read_source(client, earlier)
        path = saved_run(tmp_path, source)
        # ... and someone changed it afterwards.
        client.external_commit(
            BRANCH, {f"{FOLDER}/review.md": b"edited by hand\n"}, "an edit"
        )
        head = client.branches[BRANCH]
        with pytest.raises(GitHubDocumentsError, match="changed by someone else"):
            push(path)
        assert client.branches[BRANCH] == head and client.posted() == []
        assert client.file(f"{FOLDER}/review.md", BRANCH) == b"edited by hand\n"

    def test_a_pipeline_file_that_appeared_since_the_read_is_not_overwritten(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))  # no review.md at the read
        client.external_commit(
            BRANCH, {f"{FOLDER}/pipeline/run.json": b'{"run_number": 9}\n'}, "other run"
        )
        head = client.branches[BRANCH]
        with pytest.raises(GitHubDocumentsError, match="already exists with different"):
            push(path)
        assert client.branches[BRANCH] == head and client.posted() == []
        assert f"{FOLDER}/review.md" not in client.tree(BRANCH)


class TestARoundThatCannotTakeTheReview:
    @pytest.mark.parametrize(
        "state, merged, word", [("closed", True, "merged"), ("closed", False, "closed")]
    )
    def test_a_merged_or_closed_pull_request_gets_nothing(
        self, client, tmp_path, state, merged, word
    ):
        path = saved_run(tmp_path, read_source(client))
        client.pulls[PR].update(state=state, merged=merged)
        head = client.branches[BRANCH]
        with pytest.raises(gw.PullRequestClosed, match=f"is {word}"):
            push(path)
        assert client.branches[BRANCH] == head
        assert client.writes == [] and client.review_posts == []

    def test_the_write_back_after_a_run_names_the_saved_report_and_no_retry(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))
        client.pulls[PR]["state"] = "closed"
        with pytest.raises(gw.PullRequestClosed) as err:
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)
        assert str(path) in str(err.value) and "push-run" not in str(err.value)

    def test_a_pull_request_that_is_gone_is_a_plain_message(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        del client.pulls[PR]
        with pytest.raises(GitHubDocumentsError, match=f"no pull request #{PR}"):
            push(path)

    def test_a_pull_request_from_a_fork_is_refused(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        client.pulls[PR]["head_repo"] = "someone/fork"
        with pytest.raises(GitHubDocumentsError, match="fork"):
            push(path)
        assert client.writes == []

    def test_a_report_read_from_a_tag_or_ref_has_no_pull_request(
        self, client, tmp_path
    ):
        source = {**read_source(client), "pull_request": None}
        with pytest.raises(GitHubDocumentsError, match="names no pull request"):
            push(saved_run(tmp_path, source))
        assert client.requests == []

    def test_a_replay_is_never_pushed(self, client, tmp_path):
        replay = tmp_path / "pipeline_history" / "_replay"
        path = saved_run(replay, read_source(client))
        with pytest.raises(GitHubDocumentsError, match="under _replay/"):
            push(path)
        assert client.requests == []

    def test_a_report_copied_out_of_replay_is_pushed(self, client, tmp_path):
        # The copy is an explicit act, and the report still says "replayed_from".
        path = saved_run(tmp_path, read_source(client), replayed_from="x_results.json")
        assert push(path)[0].written

    def test_a_report_not_read_from_github_is_refused(self, client, tmp_path):
        path = saved_run(tmp_path, None)
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
        path = saved_run(tmp_path, read_source(client))
        renamed = path.with_name(name)
        path.rename(renamed)
        with pytest.raises(GitHubDocumentsError, match="not named run_N_<timestamp>"):
            push(renamed)

    def test_a_public_repository_is_refused_unless_allowed(self, client, tmp_path):
        client.private = False
        path = saved_run(tmp_path, read_source(client))
        with pytest.raises(GitHubDocumentsError, match="public repository"):
            push(path)
        assert client.writes == []
        result, _ = push(path, settings={**SETTINGS, "allow_public_repo": True})
        assert result.written

    def test_a_token_that_cannot_push_is_refused(self, client, tmp_path):
        client.push = False
        with pytest.raises(GitHubDocumentsError, match="cannot push"):
            push(saved_run(tmp_path, read_source(client)))
        assert client.writes == []

    def test_no_token_is_a_plain_message(self, fake, tmp_path):
        with (
            patch.object(gw, "resolve_token", return_value=None),
            pytest.raises(GitHubDocumentsError, match="GITHUB_TOKEN"),
        ):
            push(saved_run(tmp_path, read_source(fake)))

    def test_repo_overrides_the_repository_the_report_names(self, fake, tmp_path):
        seen = []

        def build(repo, token, session=None):
            seen.append(repo)
            return GitHubRepo(REPO, token, session=fake)

        with (
            patch.object(gw, "GitHubRepo", build),
            patch.object(gw, "resolve_token", return_value=TOKEN),
        ):
            source = {**read_source(fake), "repo": "someone/else"}
            push(saved_run(tmp_path, source), repo=REPO)
        assert seen == [REPO]

    def test_the_folder_comes_from_the_report_not_from_a_configured_prefix(
        self, client, tmp_path
    ):
        deep = f"docs/a/{SLUG}"
        client.external_commit(
            BRANCH, {f"{deep}/draft.md": b"# Deeper\n\nA draft.\n"}, "elsewhere"
        )
        tree = client.tree(BRANCH)
        source = {
            **read_source(client, client.branches[BRANCH]),
            "files": [
                {"path": f"{deep}/draft.md", "blob_sha": tree[f"{deep}/draft.md"]}
            ],
        }
        result, _ = push(saved_run(tmp_path, source))
        assert f"{deep}/review.md" in result.written
        assert f"{deep}/pipeline/run.json" in result.written


class TestSecrets:
    def test_a_configured_api_key_in_a_file_refuses_the_commit(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        report = json.loads(path.read_text(encoding="utf-8"))
        report["provider_error"] = f"401 for key {KEY}"
        path.write_text(json.dumps(report), encoding="utf-8")
        head = client.branches[BRANCH]
        with pytest.raises(GitHubDocumentsError, match="nothing was sent"):
            push(path)
        assert client.branches[BRANCH] == head
        assert client.writes == [] and client.review_posts == []

    def test_a_secret_that_would_be_in_the_body_is_refused_before_anything_is_sent(
        self, client, tmp_path
    ):
        # "ci-review run=" is only in the body (the marker), never in a file.
        keys = {"openai": {"api_key": "ci-review run="}}
        with pytest.raises(GitHubDocumentsError, match="review body contains"):
            push(saved_run(tmp_path, read_source(client)), api_keys=keys)
        assert client.writes == [] and client.review_posts == []
        assert OWNED[0] not in client.tree(BRANCH)

    def test_the_token_never_reaches_a_commit_a_message_or_a_review(
        self, client, tmp_path
    ):
        result, _ = push(saved_run(tmp_path, read_source(client)))
        everything = [client.commits[result.commit.commit_sha]["message"].encode()]
        everything += [client.file(p, BRANCH) for p in result.written]
        everything += [json.dumps(p).encode() for p in client.review_posts]
        assert not any(TOKEN.encode() in blob for blob in everything)


class TestFailureAfterTheRun:
    """``write_back_after_run``: the review is saved, so a failure says how to retry."""

    def test_the_retry_command_is_named(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        client.push = False
        with pytest.raises(GitHubDocumentsError) as err:
            gw.write_back_after_run(
                path, settings=SETTINGS, api_keys=API_KEYS, config_dir="my configs"
            )
        message = str(err.value)
        assert "saved" in message
        retry = gw.retry_command(path, "my configs")
        assert retry in message
        assert retry.startswith("ci-docs push-run ") and str(path) in retry
        assert retry.endswith('--config-dir "my configs"')

    def test_the_default_config_dir_is_left_off_the_retry(self):
        assert gw.retry_command("a/run_1_x_report.json") == (
            "ci-docs push-run a/run_1_x_report.json"
        )

    def test_a_review_that_would_not_post_names_the_retry(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        client.review_errors.append(Resp(422, {"message": "Unprocessable"}))
        with pytest.raises(GitHubDocumentsError) as err:
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)
        assert "review was not posted" in str(err.value)
        assert gw.retry_command(path) in str(err.value)

    def test_exists_says_the_pipeline_files_changed_and_nothing_was_overwritten(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))
        client.external_commit(BRANCH, {f"{FOLDER}/review.md": b"other\n"})
        with pytest.raises(GitHubDocumentsError, match="Nothing was overwritten"):
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)

    def test_secret_names_the_problem_not_a_retry_that_would_help(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))
        report = json.loads(path.read_text(encoding="utf-8"))
        report["note"] = KEY
        path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(GitHubDocumentsError, match="Find and remove it"):
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)

    def test_conflict_after_the_retries_keeps_the_local_files(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client))
        for n in range(4):  # commit_files tries four times in all
            client.before_ref_update.append(
                lambda n=n: client.external_commit(
                    BRANCH, {f"{FOLDER}/x{n}.md": b"x\n"}, f"racer {n}"
                )
            )
        with pytest.raises(GitHubDocumentsError, match="kept moving") as err:
            gw.write_back_after_run(path, settings=SETTINGS, api_keys=API_KEYS)
        assert "ci-docs push-run" in str(err.value)
        assert path.exists()
        assert OWNED[0] not in client.tree(BRANCH)


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
# The review body on its own
# ---------------------------------------------------------------------------


def facts(**kw):
    return review_body.RunFacts(
        **{
            "slug": SLUG,
            "run_number": 4,
            "run_name": RUN,
            "reviewed_commit": "a" * 40,
            "pull_number": PR,
            "repo": REPO,
            "review_path": f"{FOLDER}/review.md",
            "review_commit": "b" * 40,
            "round_number": 4,
            "preset": "wide",
            "cost_usd": 0.7276,
            "files_read": ["draft.md", "metadata.md"],
            **kw,
        }
    )


def many(n, *, passage_chars=120, problem_chars=180):
    return [
        finding(
            f"P{i:04d} " + "x" * (passage_chars - 6),
            "problem " + "y" * (problem_chars - 8),
        )
        for i in range(n)
    ]


class TestTheBody:
    def test_the_marker_round_trips(self):
        line = review_body.marker(7, "c" * 40)
        assert line == f"<!-- ci-review run=7 commit={'c' * 40} -->"
        assert review_body.parse_marker(line + "\nmore") == (7, "c" * 40)
        assert review_body.is_review_of(line, 7, "c" * 40)
        assert not review_body.is_review_of(line, 8, "c" * 40)
        assert not review_body.is_review_of(line, 7, "d" * 40)
        assert review_body.parse_marker("A review written by hand") is None
        assert review_body.parse_marker(None) is None

    def test_a_passage_with_markup_or_newlines_stays_one_line_and_inert(self):
        entry = finding("Line one\nline two <!-- and a comment", "Bad <b>thing</b>\n!")
        item = review_body.checklist_item(3, entry)
        assert "\n" not in item and "<" not in item.replace("&lt;", "")
        assert item.startswith('- [ ] **F3** "Line one line two &lt;!-- and a comment"')

    def test_other_domains_say_what_is_wrong_in_their_own_field(self):
        entry = {
            "passage": "p",
            "models": ["a:x", "b:y"],
            "flags": [
                {"domain": "fact_check", "type": "outdated", "current_value": "38%"}
            ],
        }
        assert review_body.checklist_item(1, entry).endswith(": outdated: 38%")
        bare = {"passage": "p", "models": ["a:x", "b:y"], "flags": []}
        assert review_body.checklist_item(2, bare).endswith(": flagged by 2 pass(es)")

    def test_a_long_passage_and_finding_are_clipped_to_a_line(self):
        item = review_body.checklist_item(1, many(1, passage_chars=900)[0])
        assert len(item) < 400 and item.count("…") >= 1

    def test_no_findings_is_said(self):
        body = review_body.build_review_body(make_report(None, findings=[]), facts())
        assert "No consensus findings" in body and "- [ ]" not in body

    def test_over_the_cap_items_are_cut_from_the_end_with_what_was_cut_said(self):
        report = make_report(None, findings=many(400))
        body = review_body.build_review_body(report, facts())
        assert len(body) <= review_body.MAX_BODY
        tasks = checklist(body)
        kept = len(tasks)
        assert 0 < kept < 400
        assert [k for k, _ in tasks] == list(
            range(1, kept + 1)
        )  # whole items, from the front
        assert body.rstrip().endswith("review.md has all of it._")
        assert f"{400 - kept} of 400 findings (F{kept + 1} to F400)" in body
        assert "Findings by section" not in body  # those went first
        # One more item would not have fit.
        longer = review_body.build_review_body(
            report, facts(), limit=review_body.MAX_BODY + 400
        )
        assert len(checklist(longer)) > kept

    def test_the_section_lines_are_cut_before_any_item(self):
        report = make_report(None, findings=many(3))
        full = review_body.build_review_body(report, facts())
        assert "Findings by section" in full and len(checklist(full)) == 3
        tight = review_body.build_review_body(report, facts(), limit=len(full) - 50)
        assert len(tight) <= len(full) - 50
        assert "Findings by section" not in tight and len(checklist(tight)) == 3
        assert "per-section counts are left out" in tight

    def test_section_counts_read_every_shape_a_report_has(self):
        report = {
            **make_report(None),
            "section_2_fact_check": {
                "confirmed": [{"claim": "a"}, {"claim": "b"}],
                "outdated": [{"claim": "c"}],
                "unverifiable": ["not a record"],
            },
            "section_3_voice": [{"passage": "p"}, "junk"],
            "section_6_red_team": {
                "mistral": {"most_vulnerable_claim": {"passage": "p"}},
                "grok": {"highest_audience_risk": {"passage": "q"}},
            },
            "section_10_expansion": {"sources": [{"title": "t"}], "topics": []},
        }
        lines = review_body.section_lines(report)
        assert "- Section 2, fact-check: 3 claim(s), 1 not confirmed" in lines
        assert "- Section 3, voice and AI-speak: 1" in lines
        assert "- Section 6, red team: 2" in lines
        assert "- Section 10, expansion candidates: 1" in lines

    def test_an_oversized_review_is_posted_under_the_cap(self, client, tmp_path):
        path = saved_run(tmp_path, read_source(client), findings=many(400))
        push(path)
        body = client.posted()[0]["body"]
        assert len(body) <= review_body.MAX_BODY
        assert "review.md has all of it" in body
        # Nothing was cut from review.md itself.
        review_md = client.file(f"{FOLDER}/review.md", BRANCH).decode()
        assert len(numbered(review_md)) == 400


# ---------------------------------------------------------------------------
# A real run, through run_draft_pipeline, writing back
# ---------------------------------------------------------------------------


def handoff(source, **extra):
    return {
        **_HANDOFF,
        "history_key": SLUG,
        "run_number": 1,
        "document_source": source,
        **extra,
    }


def run_pipeline(tmp_path, client, *, write_back=True, settings=None, **extra):
    """``run_draft_pipeline`` over stubs, writing back to ``client``."""
    config = {
        **_CONFIG,
        "api_keys": {"openai": {"api_key": KEY}},
        # One stubbed provider: let two of its passes agree, so Section 1 has
        # findings to number.
        "ensemble": {"consensus_min_models": 1},
        "github_documents": {"include_capture": False, **(settings or {})},
    }
    domain = extra.pop("run_domain", _fake_run_domain)
    with ExitStack() as stack:
        for target, kw in (
            ("load_user_config", {"return_value": {"pipeline": {}}}),
            ("load_publication_config", {"return_value": {}}),
            ("merge_configs", {"return_value": config}),
            ("check_model_currency", {"return_value": _CURRENCY}),
            ("_run_domain", {"side_effect": domain}),
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
            handoff=handoff(read_source(client) if client else None, **extra),
            write_back=write_back,
        )


def local_runs(tmp_path):
    return sorted(
        p.name for p in (tmp_path / "history" / SLUG).glob("run_*_report.json")
    )


class TestRealRun:
    def test_a_run_writes_its_files_and_one_review(self, client, tmp_path, capsys):
        before = len(client.commits)
        run_pipeline(tmp_path, client)
        assert len(client.commits) == before + 1
        (report_name,) = local_runs(tmp_path)
        stem = report_name[: -len("_report.json")]
        tree = client.tree(BRANCH)
        assert set(OWNED) <= set(tree)
        assert f"{FOLDER}/pipeline/results.json" not in tree
        for path in OWNED:
            assert b"\r" not in client.file(path, BRANCH)
        local = (tmp_path / "history" / SLUG / f"{stem}_review.md").read_bytes()
        assert client.file(f"{FOLDER}/review.md", BRANCH) == local.replace(
            b"\r\n", b"\n"
        )
        head = client.commits[client.branches[BRANCH]]["message"]
        assert head.startswith(
            f"review: {SLUG} r4 run 1\n\nReviewed: draft.md, metadata.md at "
        )
        assert f"Run: {stem} (preset" in head
        # One review, of the commit that was read, as a comment.
        (post,) = client.review_posts
        assert post["commit_id"] == client.read_at and post["event"] == "COMMENT"
        assert review_body.parse_marker(post["body"]) == (1, client.read_at)
        out = capsys.readouterr().out
        assert "Wrote 4 file(s)" in out
        assert out.strip().splitlines()[-1].endswith(client.posted()[0]["html_url"])

    def test_the_checklist_is_section_1_of_the_review_it_links(self, client, tmp_path):
        run_pipeline(tmp_path, client)
        (report_name,) = local_runs(tmp_path)
        report = json.loads(
            (tmp_path / "history" / SLUG / report_name).read_text(encoding="utf-8")
        )
        expected = consensus_findings(report)
        assert expected, "the stubbed run should produce Section 1 findings"
        in_review = numbered(client.file(f"{FOLDER}/review.md", BRANCH).decode())
        tasks = checklist(client.posted()[0]["body"])
        assert [k for k, _ in tasks] == [k for k, _ in expected]
        for k, rest in tasks:
            assert rest.startswith(f'"{in_review[k][:40]}')

    def test_include_capture_adds_the_capture_to_the_commit(self, client, tmp_path):
        run_pipeline(tmp_path, client, settings={"include_capture": True})
        assert f"{FOLDER}/pipeline/results.json" in client.tree(BRANCH)

    def test_without_write_back_nothing_reaches_github(self, tmp_path):
        def fail(*a, **k):
            pytest.fail("GitHub was reached without --write-back")

        with (
            patch.object(gw, "GitHubRepo", fail),
            patch.object(gw, "resolve_token", fail),
        ):
            run_pipeline(tmp_path, None, write_back=False)
        assert local_runs(tmp_path)

    def test_a_taken_run_number_moves_the_run_number_everywhere(
        self, client, tmp_path, caplog
    ):
        with caplog.at_level(logging.WARNING):
            run_pipeline(tmp_path, client, taken_run_numbers=[1, 2])
        (report_name,) = local_runs(tmp_path)
        assert report_name.startswith("run_3_")
        run = json.loads(client.file(f"{FOLDER}/pipeline/run.json", BRANCH))
        assert run["run_number"] == 3 and run["history_name"] == report_name[:-12]
        assert review_body.parse_marker(client.posted()[0]["body"])[0] == 3
        assert "Run 1 already exists for this article; using 3 instead" in caplog.text

    def test_a_pull_request_merged_during_the_run_gets_nothing(
        self, client, tmp_path, capsys
    ):
        def merge_then_answer(model_name, domain, *a, **kw):
            client.pulls[PR].update(state="closed", merged=True)
            return _fake_run_domain(model_name, domain, *a, **kw)

        with pytest.raises(gw.PullRequestClosed) as err:
            run_pipeline(tmp_path, client, run_domain=merge_then_answer)
        assert "is merged" in str(err.value) and "push-run" not in str(err.value)
        assert client.writes == [] and client.review_posts == []
        (report,) = local_runs(tmp_path)  # the review is in local history
        assert report in str(err.value)
        assert "REVIEW COMPLETE" in capsys.readouterr().out.upper()

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

    def test_the_retry_after_a_failure_writes_the_same_commit_and_review(
        self, client, tmp_path
    ):
        client.push = False
        with pytest.raises(GitHubDocumentsError):
            run_pipeline(tmp_path, client)
        (report,) = local_runs(tmp_path)
        client.push = True
        result, _ = push(tmp_path / "history" / SLUG / report)
        assert f"{FOLDER}/review.md" in result.written
        assert result.review_posted and len(client.posted()) == 1

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
        assert client.writes == [] and client.review_posts == []


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
    def test_it_writes_the_run_and_a_second_run_does_nothing(
        self, cli_client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(cli_client))
        with patch.object(
            gw,
            "GitHubRepo",
            lambda repo, token, session=None: GitHubRepo(repo, token, session=session),
        ):
            code, out = run_cli(cli_client, path, "--config-dir", "no-such-dir")
            assert code == 0 and "Wrote 4 file(s)" in out
            # The commit URL, then the review URL last.
            assert "/commit/" in out.splitlines()[0]
            assert "pullrequestreview-" in out.splitlines()[-1]
            commits = len(cli_client.commits)
            code, out = run_cli(cli_client, path)
        assert code == 0 and "Nothing new to write" in out
        assert len(cli_client.commits) == commits
        assert len(cli_client.posted()) == 1

    def test_a_replay_report_is_refused_with_exit_one(
        self, cli_client, tmp_path, capsys
    ):
        path = saved_run(
            tmp_path / "pipeline_history" / "_replay", read_source(cli_client)
        )
        code, _ = run_cli(cli_client, path)
        assert code == 1 and "under _replay/" in capsys.readouterr().err
        assert cli_client.requests == []

    def test_a_closed_pull_request_is_refused_with_exit_one(
        self, cli_client, tmp_path, capsys
    ):
        path = saved_run(tmp_path, read_source(cli_client))
        cli_client.pulls[PR].update(state="closed", merged=True)
        code, _ = run_cli(cli_client, path)
        assert code == 1 and "is merged" in capsys.readouterr().err
        assert cli_client.writes == []

    def test_an_unreadable_config_stops_it_before_anything_is_sent(
        self, cli_client, tmp_path, capsys
    ):
        with patch(
            "ci_article_review.config_loader.load_user_config",
            side_effect=ValueError("unset ${OPENAI_API_KEY}"),
        ):
            code = documents_cli.main(
                ["push-run", str(saved_run(tmp_path, read_source(cli_client)))],
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


TAG = f"{SLUG}/r4"


def tag_source(fake, commit=None):
    """``document_source`` as ``--round 4`` of an accepted round leaves it: a tag, no pull request."""
    return {**read_source(fake, commit), "ref": TAG, "pull_request": None}


class TestAttachingARunReadFromATag:
    """``push-run --pr N``: a run of an accepted round's tag, written to the next round's pull request."""

    def test_it_is_written_to_the_pull_request_and_says_where_it_was_read(
        self, client, tmp_path
    ):
        reviewed = client.read_at
        # The next round's branch has a revision on top of what was reviewed.
        client.external_commit(
            BRANCH, {f"{FOLDER}/draft.md": b"# Example\n\nRevised.\n"}
        )
        head = client.branches[BRANCH]
        path = saved_run(tmp_path, tag_source(client, reviewed))
        result, lines = push(path, pr=PR)
        assert set(result.written) == set(OWNED)
        run = json.loads(client.file(f"{FOLDER}/pipeline/run.json", BRANCH))
        assert run["reviewed_commit"] == reviewed and run["pull_request"] == PR
        [review] = client.posted()
        # Posted on the head it was checked against; the marker names what it reviewed.
        assert review["commit_id"] == head
        assert review_body.is_review_of(review["body"], 4, reviewed)
        assert f"Read from {TAG} at {reviewed[:7]}" in review["body"]
        assert f"Read from {TAG}" in client.commits[client.branches[BRANCH]]["message"]
        # The report on disk still says it was read from the tag.
        assert (
            json.loads(path.read_text(encoding="utf-8"))["document_source"][
                "pull_request"
            ]
            is None
        )
        # A second push is a no-op in both halves.
        again, _ = push(path, pr=PR)
        assert not again.written and not again.review_posted
        assert len(client.posted()) == 1

    def test_a_commit_that_is_not_an_ancestor_of_the_head_is_refused(
        self, client, tmp_path
    ):
        client.branches["elsewhere"] = client.branches["main"]
        stray = client.external_commit(
            "elsewhere",
            {f"{FOLDER}/draft.md": b"other\n", f"{FOLDER}/metadata.md": b"m\n"},
        )
        path = saved_run(tmp_path, tag_source(client, stray))
        head = client.branches[BRANCH]
        with pytest.raises(GitHubDocumentsError, match="not an ancestor"):
            push(path, pr=PR)
        assert client.branches[BRANCH] == head
        assert client.writes == [] and client.review_posts == []

    def test_a_closed_pull_request_still_gets_nothing(self, client, tmp_path):
        path = saved_run(tmp_path, tag_source(client))
        client.pulls[PR].update(state="closed", merged=True)
        with pytest.raises(gw.PullRequestClosed):
            push(path, pr=PR)
        assert client.writes == []

    def test_a_run_read_from_a_pull_request_goes_only_to_that_one(
        self, client, tmp_path
    ):
        path = saved_run(tmp_path, read_source(client))
        with pytest.raises(
            GitHubDocumentsError, match=f"#{PR}, so it is written there"
        ):
            push(path, pr=PR + 1)
        assert client.requests == []
        # Naming its own pull request is the same as naming none.
        assert push(path, pr=PR)[0].written

    def test_without_pr_the_refusal_says_how_to_attach_it(self, client, tmp_path):
        with pytest.raises(GitHubDocumentsError, match="Pass --pr N"):
            push(saved_run(tmp_path, tag_source(client)))
        assert client.requests == []

    def test_the_command_takes_pr(self, cli_client, tmp_path):
        path = saved_run(tmp_path, tag_source(cli_client))
        with patch.object(
            gw,
            "GitHubRepo",
            lambda repo, token, session=None: GitHubRepo(repo, token, session=session),
        ):
            code, out = run_cli(cli_client, path, "--pr", PR)
        assert code == 0 and "Wrote 4 file(s)" in out
        assert len(cli_client.posted()) == 1
