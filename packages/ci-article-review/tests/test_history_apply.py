"""``ci-docs import apply`` (issue #421), against ci-core's in-process ``FakeGitHub``.

The GitHub side is the real ``ci_core.github_repo.GitHubRepo`` talking to the fake,
so the Git Data calls, the fast-forward rule, the merge-by-push behaviour and the
review rules are the fake's model of the real API. The plan is built by hand from
invented files under ``tmp_path`` (slug ``example-import``): three rounds, a
handoff round, a two-file round with a revision and two runs, and a round with a
recovered version and a close-out note. A second article exists only in an invented
chat export, to follow a chat-only version through the real scan.

Nothing here reads a real article, plan or export; this repository is public.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ci_article_review import (
    documents_cli,
    documents_init as di,
    history_apply as ha,
    history_import as hi,
)

from .github_fake import REPO, SETTINGS, TOKEN, FakeGitHub, Resp, client, non_get
from .test_chat_export import message, tool_result, tool_use
from .test_chat_map import Mapped
from .test_history_import import handoff_text, make_draft, when

SLUG = "example-import"
FOLDER = f"articles/{SLUG}"
KEY = "sk-example-provider-key-0123456789"
API_KEYS = {"openai": {"api_key": KEY}}
UTC = timezone.utc
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def t(day, hh=9, mm=0, month=6):
    return datetime(2026, month, day, hh, mm, 0, tzinfo=UTC)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text):
    return hashlib.sha256(hi.normalise(text).encode("utf-8")).hexdigest()


class Article:
    """The invented article: three rounds over files under ``tmp_path``."""

    def __init__(self, base):
        self.dir = base / "src"
        self.dir.mkdir(parents=True)
        self.history = self.dir / "history" / SLUG
        self.history.mkdir(parents=True)
        self.plan_path = base / "plan.json"
        self.d1, self.d2, self.d2b, self.d3 = (
            make_draft(m) for m in ("one", "two", "two-b", "three")
        )
        self.metadata = "PRIMARY CLAIM\nAn invented claim.\n"
        self.close_out = (
            "CLOSE-OUT HANDOFF\nDirection: to-code\nContains: nothing real\n"
        )
        self.plan = self._plan()
        self.write()

    # -- files -----------------------------------------------------------------

    def file(self, name, text, newline="\n"):
        path = self.dir / name
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
        return path.as_posix()

    def run(self, number, moment, corrected, extra=""):
        stem = f"run_{number}_{moment.strftime('%Y%m%d_%H%M%S')}"
        report = {
            "generated": iso(moment),
            "corrected_draft": corrected,
            "lt_corrections_applied": [],
            "ensemble": {"cost_preset": "wide"},
            "cost_summary": {"total_usd": 0.5},
        }
        report_path = self.history / f"{stem}_report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        (self.history / f"{stem}_review.md").write_text(
            f"# Review of run {number}\n{extra}", encoding="utf-8", newline="\n"
        )
        (self.history / f"{stem}_worklist.md").write_text(
            f"worklist {number}\n", encoding="utf-8", newline="\n"
        )
        return report_path.as_posix()

    # -- the plan --------------------------------------------------------------

    def _event(self, eid, kind, moment, text, path, **extra):
        return {
            "id": eid,
            "at": iso(moment),
            "kind": kind,
            "sha256": sha(text),
            "sources": [{"type": "file", "path": path, "time": iso(moment)}],
            "include": True,
            **extra,
        }

    def _run_event(self, eid, number, moment, report, draft):
        return {
            "id": eid,
            "at": iso(moment),
            "kind": "run",
            "run": number,
            "history_key": SLUG,
            "report": report,
            "preset": "wide",
            "cost_usd": 0.5,
            "sha256": sha(self.corrected[report]),
            "draft": draft,
            "suspect": None,
            "include": True,
        }

    def _plan(self):
        self.corrected = {}
        handoff = handoff_text(self.d1)
        handoff_path = self.file(f"{SLUG}-v1-handoff.md", handoff, newline="\r\n")
        r1 = self.run(1, t(1, 9, 30), self.d1)
        d2_path = self.file(f"{SLUG}-draft-r2.md", self.d2)
        meta_path = self.file(f"{SLUG}-metadata-r2.md", self.metadata)
        d2b_path = self.file(f"{SLUG}-draft.md", self.d2b)
        r2 = self.run(2, t(2, 9, 30), self.d2)
        r3 = self.run(3, t(2, 11, 30), self.d2b)
        d3_path = self.file(f"{SLUG}-draft-r3.md", self.d3)
        out_path = self.file(f"{SLUG}-close-out-r3.md", self.close_out)
        sup_path = self.file(f"{SLUG}-draft-r2-again.md", self.d2 + "extra\n")
        for path, draft in ((r1, self.d1), (r2, self.d2), (r3, self.d2b)):
            self.corrected[path] = draft
        events_1 = [
            self._event(
                "e1",
                "draft",
                t(1),
                self.d1,
                handoff_path,
                label="v1",
                submitted=True,
            ),
            self._event("e2", "handoff", t(1), handoff, handoff_path, attached_to="e1"),
            self._run_event("e3", 1, t(1, 9, 30), r1, "e1"),
        ]
        events_1[0]["sources"][0]["part"] = "draft"
        events_2 = [
            self._event("e4", "draft", t(2), self.d2, d2_path, submitted=True),
            self._event(
                "e5", "metadata", t(2), self.metadata, meta_path, attached_to="e4"
            ),
            self._run_event("e6", 2, t(2, 9, 30), r2, "e4"),
            self._event("e7", "draft", t(2, 11), self.d2b, d2b_path),
            self._run_event("e8", 3, t(2, 11, 30), r3, "e7"),
            # a reproduction a file's copy superseded: it writes nothing
            {
                **self._event("e9", "draft", t(2), self.d2 + "extra\n", sup_path),
                "include": False,
                "superseded_by": "e4",
                "recovered": True,
            },
        ]
        events_3 = [
            {
                **self._event(
                    "e10",
                    "draft",
                    t(3),
                    self.d3,
                    d3_path,
                    submitted=True,
                    note="LanguageTool-corrected; the submitted bytes were not found",
                ),
                "recovered": True,
                "sources": [{"type": "recovered", "path": d3_path, "time": None}],
            },
            self._event(
                "e11",
                "close-out",
                t(3, 10),
                self.close_out,
                out_path,
                attached_to="e10",
            ),
        ]
        return {
            "version": 1,
            "slug": SLUG,
            "history_keys": [SLUG],
            "rounds": [
                {"round": 1, "accepted_at": iso(t(1, 10)), "events": events_1},
                {"round": 2, "accepted_at": iso(t(2, 12)), "events": events_2},
                {"round": 3, "accepted_at": iso(t(3, 11)), "events": events_3},
            ],
            "excluded": [],
            "near_duplicates": [],
            "applied": {},
        }

    def write(self, plan=None):
        self.plan_path.write_text(hi.plan_text(plan or self.plan), encoding="utf-8")

    def read(self):
        return json.loads(self.plan_path.read_text(encoding="utf-8"))


@pytest.fixture
def article(tmp_path):
    return Article(tmp_path)


@pytest.fixture
def fake():
    f = FakeGitHub()
    f.settings.update(di.DOCUMENT_REPO_SETTINGS)
    return f


def apply(article, fake, **kw):
    """``apply_plan`` over the fake; returns the lines it printed."""
    lines = []
    options = dict(
        repo=REPO,
        client=client(fake),
        settings=SETTINGS,
        api_keys=API_KEYS,
        out=lines.append,
        now=lambda: NOW,
        sleep=lambda seconds: None,
    )
    options.update(kw)
    ha.apply_plan(article.plan_path, **options)
    return lines


def first_parent_chain(fake, ref="main"):
    chain, sha_ = [], fake.branches[ref]
    while sha_:
        chain.append(sha_)
        parents = fake.commits[sha_]["parents"]
        sha_ = parents[0] if parents else None
    return list(reversed(chain))


def import_commits(fake, branch):
    """The import's commits on a round branch, oldest first."""
    found, sha_ = [], fake.branches[branch]
    while "Plan-event:" in fake.commits[sha_]["message"]:
        found.append(sha_)
        sha_ = fake.commits[sha_]["parents"][0]
    return list(reversed(found))


def subject(fake, sha_):
    return fake.commits[sha_]["message"].split("\n", 1)[0]


# ---------------------------------------------------------------------------
# What a full apply writes
# ---------------------------------------------------------------------------


def test_apply_writes_each_round_as_a_merged_pull_request_with_the_original_dates(
    article, fake
):
    apply(article, fake)

    pulls = {p["head"]: p for p in fake.pulls.values()}
    assert sorted(pulls) == [f"round/{SLUG}/r{n}" for n in (1, 2, 3)]
    assert all(p["merged"] and p["state"] == "closed" for p in pulls.values())

    # main: the seed, then the three merges in order, each dated accepted_at
    chain = first_parent_chain(fake)
    merges = chain[1:]
    assert [subject(fake, m) for m in merges] == [
        f"{SLUG} r{n} (#{n})" for n in (1, 2, 3)
    ]
    for merge, accepted in zip(merges, (t(1, 10), t(2, 12), t(3, 11))):
        commit = fake.commits[merge]
        assert commit["author"]["date"] == iso(accepted).replace("Z", "+00:00")
        assert commit["committer"]["date"] == commit["author"]["date"]
        assert len(commit["parents"]) == 2

    # each tag is annotated, on its round's merge commit, dated accepted_at
    for n, merge, accepted in zip((1, 2, 3), merges, (t(1, 10), t(2, 12), t(3, 11))):
        name = f"{SLUG}/r{n}"
        obj = fake.tag_objects[fake.tags[name]]
        assert obj["object"] == merge
        assert obj["tagger"]["date"] == iso(accepted).replace("Z", "+00:00")
        assert obj["message"].startswith(f"{SLUG} r{n} (imported): PR #{n}")


def test_the_commits_carry_the_events_dates_the_token_user_and_source_trailers(
    article, fake
):
    apply(article, fake)

    r1 = import_commits(fake, f"round/{SLUG}/r1")
    r2 = import_commits(fake, f"round/{SLUG}/r2")
    r3 = import_commits(fake, f"round/{SLUG}/r3")
    assert [subject(fake, c) for c in r1] == [
        f"draft: {SLUG} r1",
        f"review: {SLUG} r1 run 1",
    ]
    assert [subject(fake, c) for c in r2] == [
        f"draft: {SLUG} r2",
        f"review: {SLUG} r2 run 2",
        f"revise: {SLUG} r2",
        f"review: {SLUG} r2 run 3",
    ]
    assert [subject(fake, c) for c in r3] == [
        f"draft: {SLUG} r3",
        f"close-out: {SLUG} r3 to-code",
    ]

    first = fake.commits[r1[0]]
    assert first["author"] == {
        "name": "Example User",
        "email": "4242+example-user@users.noreply.github.com",
        "date": "2026-06-01T09:00:00+00:00",
    }
    assert first["committer"] == first["author"]
    assert "Imported-from: file example-import-v1-handoff.md" in first["message"]
    assert "Imported-at: 2026-10-27T12:00:00Z" in first["message"]
    assert "Plan-event: e1" in first["message"] and "Plan-event: e2" in first["message"]
    assert fake.commits[r2[1]]["author"]["date"] == "2026-06-02T09:30:00+00:00"
    assert "Imported-from: run run_2_20260602_093000" in fake.commits[r2[1]]["message"]

    # a source's contents never appear in a message
    for commit in fake.commits.values():
        assert "Paragraph" not in commit["message"]


def test_a_reproduction_nothing_else_holds_says_so_and_a_superseded_one_writes_nothing(
    article, fake
):
    apply(article, fake)

    recovered = next(
        c
        for c in import_commits(fake, f"round/{SLUG}/r3")
        if subject(fake, c).startswith("draft:")
    )
    message = fake.commits[recovered]["message"]
    assert (
        "Recovered: reproduced by a chat from its context; not the original bytes"
        in message
    )
    assert (
        "Source: LanguageTool-corrected; the submitted bytes were not found" in message
    )
    assert "Imported-from: file example-import-draft-r3.md" in message
    # e9 was superseded and excluded: no commit names it, and its extra line is nowhere
    assert not any("Plan-event: e9" in c["message"] for c in fake.commits.values())
    assert not any(b.endswith(b"extra\n") for b in fake.blobs.values())
    # the other commits are not flagged
    first = next(c for c in import_commits(fake, f"round/{SLUG}/r1"))
    assert "Recovered:" not in fake.commits[first]["message"]


def test_a_handoff_round_writes_handoff_md_and_the_two_file_round_after_it_deletes_it(
    article, fake
):
    apply(article, fake)

    r1 = import_commits(fake, f"round/{SLUG}/r1")
    files = fake.trees[fake.commits[r1[0]]["tree"]]
    assert f"{FOLDER}/handoff.md" in files and f"{FOLDER}/draft.md" not in files
    handoff_blob = fake.blobs[files[f"{FOLDER}/handoff.md"]]
    assert b"\r" not in handoff_blob  # the repository is LF

    r2 = import_commits(fake, f"round/{SLUG}/r2")
    after_draft = fake.trees[fake.commits[r2[0]]["tree"]]
    assert f"{FOLDER}/handoff.md" not in after_draft
    assert fake.blobs[after_draft[f"{FOLDER}/draft.md"]].decode() == article.d2
    assert fake.blobs[after_draft[f"{FOLDER}/metadata.md"]].decode() == article.metadata
    # the draft and its metadata are one commit
    assert fake.commits[r2[0]]["message"].count("Plan-event:") == 2


def test_a_run_commit_writes_the_fixed_layout_and_a_revision_replaces_the_draft(
    article, fake
):
    apply(article, fake)

    r2 = import_commits(fake, f"round/{SLUG}/r2")
    run_tree = fake.trees[fake.commits[r2[1]]["tree"]]
    for name in (
        "review.md",
        "pipeline/run.json",
        "pipeline/report.json",
        "pipeline/worklist.md",
    ):
        assert f"{FOLDER}/{name}" in run_tree
    assert f"{FOLDER}/pipeline/results.json" not in run_tree
    record = json.loads(fake.blobs[run_tree[f"{FOLDER}/pipeline/run.json"]])
    assert record["run_number"] == 2
    assert record["round"] == 2
    assert record["reviewed_commit"] == r2[0]
    assert record["imported"] is True
    assert fake.blobs[run_tree[f"{FOLDER}/review.md"]].startswith(b"# Review of run 2")

    revised = fake.trees[fake.commits[r2[2]]["tree"]]
    assert fake.blobs[revised[f"{FOLDER}/draft.md"]].decode() == article.d2b
    # the second run replaced the first run's review.md
    last = fake.trees[fake.commits[r2[3]]["tree"]]
    assert fake.blobs[last[f"{FOLDER}/review.md"]].startswith(b"# Review of run 3")
    assert (
        json.loads(fake.blobs[last[f"{FOLDER}/pipeline/run.json"]])["reviewed_commit"]
        == r2[2]
    )

    # the close-out note lands under its fixed name, by its Direction: line
    r3 = import_commits(fake, f"round/{SLUG}/r3")
    out_tree = fake.trees[fake.commits[r3[1]]["tree"]]
    assert f"{FOLDER}/close-out-to-code.md" in out_tree


def test_the_pull_request_is_labelled_and_each_run_gets_one_marked_review_without_comments(
    article, fake
):
    apply(article, fake)

    pull = fake.pull_by_head(f"round/{SLUG}/r2")
    assert pull["title"] == f"{SLUG} r2"
    assert pull["labels"] == ["round", f"article:{SLUG}"]
    assert pull["body"].startswith(
        f"Imported history: round 2 of {SLUG}, 2026-06-02T09:00:00Z to 2026-06-02T12:00:00Z."
    )
    assert "Only the commits carry the original dates" in pull["body"]
    assert "round" in fake.labels and f"article:{SLUG}".lower() in fake.labels

    reviews = fake.reviews[pull["number"]]
    r2 = import_commits(fake, f"round/{SLUG}/r2")
    assert [r["commit_id"] for r in reviews] == [r2[0], r2[2]]
    assert reviews[0]["body"].startswith(f"<!-- ci-review run=2 commit={r2[0]} -->")
    assert reviews[1]["body"].startswith(f"<!-- ci-review run=3 commit={r2[2]} -->")
    posted = [
        body
        for method, path, _, body in fake.requests
        if method == "POST" and path.endswith("/reviews")
    ]
    assert len(posted) == 3  # rounds 1 and 2 have runs: 1 + 2
    assert all(set(b) == {"commit_id", "body", "event"} for b in posted)
    assert all(b["event"] == "COMMENT" for b in posted)
    # the tag message reads the runs from the markers
    assert fake.tag_objects[fake.tags[f"{SLUG}/r2"]]["message"] == (
        f"{SLUG} r2 (imported): PR #{pull['number']}, runs 2 and 3"
    )


def test_the_exact_sequence_of_writes(article, fake):
    apply(article, fake)

    def kind(request):
        method, path, _, body = request
        if path.endswith("/git/refs") and method == "POST":
            return "tag-ref" if body["ref"].startswith("refs/tags/") else "branch"
        if path.endswith("/git/commits"):
            return "commit"
        if method == "PATCH" and "/git/refs/heads/" in path:
            return "ref:" + path.rsplit("/", 1)[-1]
        if path.endswith("/pulls") and method == "POST":
            return "pull"
        if path.endswith("/reviews"):
            return "review"
        if path.endswith("/labels") and "/issues/" in path:
            return "label-pull"
        if path.endswith("/git/tags"):
            return "tag"
        return None

    steps = [k for k in map(kind, non_get(fake)) if k]
    expected = []
    for n, commits, reviews in ((1, 2, 1), (2, 4, 2), (3, 2, 0)):
        branch = f"round/{SLUG}/r{n}"
        expected += ["branch"] + ["commit", f"ref:{n}"][:0]
        for _ in range(commits):
            expected += ["commit", f"ref:{branch.split('/', 1)[1]}"]
        expected += ["pull", "label-pull"] + ["review"] * reviews
        expected += ["commit", "ref:main", "tag", "tag-ref"]
    # the ref names inside "ref:" differ only in how the path is cut: compare shapes
    shape = lambda seq: [s.split(":")[0] for s in seq]  # noqa: E731
    assert shape(steps) == shape(expected)
    # branches are updated only by fast-forward commits, never forced
    for method, path, _, body in non_get(fake):
        if method == "PATCH":
            assert body["force"] is False
    # nothing is deleted
    assert not [r for r in fake.requests if r[0] == "DELETE"]


def test_apply_records_every_step_in_the_plan(article, fake):
    apply(article, fake)

    applied = article.read()["applied"]
    assert sorted(applied) == ["1", "2", "3"]
    two = applied["2"]
    assert two["branch"] == f"round/{SLUG}/r2"
    assert [c["events"] for c in two["commits"]] == [
        ["e4", "e5"],
        ["e6"],
        ["e7"],
        ["e8"],
    ]
    assert two["pull_request"] == fake.pull_by_head(two["branch"])["number"]
    assert [r["event"] for r in two["reviews"]] == ["e6", "e8"]
    assert two["merge"]["path"] == "ref-move"
    assert two["tag"]["name"] == f"{SLUG}/r2"
    assert two["tag"]["commit"] == two["merge"]["sha"]
    assert two["labels"] == ["round", f"article:{SLUG}"]


def test_running_it_again_changes_nothing(article, fake):
    apply(article, fake)
    before = len(fake.requests)
    snapshot = (dict(fake.branches), dict(fake.tags), len(fake.commits))

    lines = apply(article, fake)

    assert [r for r in fake.requests[before:] if r[0] != "GET"] == []
    assert (dict(fake.branches), dict(fake.tags), len(fake.commits)) == snapshot
    assert lines[-1].startswith("Done:")


def test_the_plan_file_is_rewritten_atomically_and_no_temp_file_is_left(article, fake):
    apply(article, fake)

    leftovers = [
        p.name for p in article.plan_path.parent.iterdir() if p.name.startswith(".")
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Resuming
# ---------------------------------------------------------------------------


def _made(path):
    path.mkdir()
    return path


class Interrupt(Exception):
    pass


def fail_once(target, name, after=0):
    """Make ``target.name`` raise ``Interrupt`` on call number ``after`` + 1, once."""
    original = getattr(target, name)
    state = {"calls": 0, "armed": True}

    def wrapper(*args, **kw):
        if state["armed"] and state["calls"] == after:
            state["armed"] = False
            raise Interrupt(name)
        state["calls"] += 1
        return original(*args, **kw)

    return patch.object(target, name, wrapper)


def snapshot(fake):
    return {
        "branches": dict(fake.branches),
        "tags": dict(fake.tags),
        "pulls": {n: (p["state"], p["merged"]) for n, p in fake.pulls.items()},
        "reviews": {n: [r["body"] for r in rs] for n, rs in fake.reviews.items()},
        "commits": len(fake.commits),
    }


def test_a_failure_after_round_twos_pull_request_resumes_at_its_review(article, fake):
    from ci_core.github_repo import GitHubRepo

    reference = FakeGitHub()
    reference.settings.update(di.DOCUMENT_REPO_SETTINGS)

    # round 1 has one review; the second create_review call is round 2's first
    with fail_once(GitHubRepo, "create_review", after=1):
        with pytest.raises(Interrupt):
            apply(article, fake)

    applied = article.read()["applied"]
    assert "tag" in applied["1"]
    two = applied["2"]
    assert two["pull_request"] and not two.get("reviews") and "merge" not in two
    pulls_before = dict(fake.pulls)
    commits_before = len(fake.commits)

    apply(article, fake)

    # nothing was opened or committed twice
    assert set(fake.pulls) == {1, 2, 3}
    assert fake.pulls[2]["head"] == f"round/{SLUG}/r2"
    assert pulls_before[2]["number"] == 2
    assert len(import_commits(fake, f"round/{SLUG}/r2")) == 4
    assert len(fake.commits) > commits_before  # round 3 and the merges were still to do
    assert len(fake.reviews[2]) == 2 and len(fake.reviews[1]) == 1
    done = article.read()["applied"]
    assert all("tag" in done[k] for k in "123")

    # and the repository equals an uninterrupted apply, object for object in shape
    clean = Article(article.plan_path.parent / "other")
    apply(clean, reference)
    assert sorted(fake.tags) == sorted(reference.tags)
    assert [subject(fake, c) for c in first_parent_chain(fake)] == [
        subject(reference, c) for c in first_parent_chain(reference)
    ]


def test_a_step_done_on_github_but_not_recorded_is_adopted_not_repeated(article, fake):
    # die after round 2's second commit reaches GitHub but before it is recorded
    original = ha._Applier.save
    state = {"saves": 0}

    def save(self):
        state["saves"] += 1
        rec = self.applied.get("2", {})
        if len(rec.get("commits", [])) == 2 and state["saves"] > 0:
            state["armed"] = state.get("armed", 0) + 1
            if state["armed"] == 1:
                raise Interrupt("save")
        return original(self)

    with patch.object(ha._Applier, "save", save):
        with pytest.raises(Interrupt):
            apply(article, fake)
    recorded = article.read()["applied"]["2"]["commits"]
    assert len(recorded) == 1
    assert len(import_commits(fake, f"round/{SLUG}/r2")) == 2  # GitHub is ahead

    apply(article, fake)

    assert len(import_commits(fake, f"round/{SLUG}/r2")) == 4
    done = article.read()["applied"]["2"]["commits"]
    assert [c["events"] for c in done] == [["e4", "e5"], ["e6"], ["e7"], ["e8"]]
    assert done[1].get("adopted") is True
    assert len(fake.pulls) == 3


def test_a_pull_request_opened_but_not_labelled_is_labelled_on_the_rerun(article, fake):
    from ci_core.github_repo import GitHubRepo

    with fail_once(GitHubRepo, "add_labels"):
        with pytest.raises(Interrupt):
            apply(article, fake)
    assert "labels" not in article.read()["applied"]["1"]
    assert fake.pulls[1].get("labels") is None

    apply(article, fake)

    assert fake.pulls[1]["labels"] == ["round", f"article:{SLUG}"]
    assert len(fake.pulls) == 3


def test_a_review_names_the_files_it_read_even_on_a_resumed_round(article, fake):
    from ci_core.github_repo import GitHubRepo

    with fail_once(GitHubRepo, "create_review", after=1):
        with pytest.raises(Interrupt):
            apply(article, fake)
    apply(article, fake)

    body = fake.reviews[2][0]["body"]
    assert "**Reviewed:** draft.md, metadata.md at" in body
    assert "**Reviewed:** handoff.md at" in fake.reviews[1][0]["body"]


def test_a_recorded_step_that_is_gone_is_an_error_and_is_not_redone(article, fake):
    apply(article, fake)
    fake.tags.pop(f"{SLUG}/r3")  # someone removed a tag
    before = len(fake.requests)

    with pytest.raises(ha.ImportApplyError, match="tag .*r3.* recorded"):
        apply(article, fake)

    assert [r for r in fake.requests[before:] if r[0] != "GET"] == []


def test_a_recorded_branch_that_is_gone_is_an_error(article, fake):
    from ci_core.github_repo import GitHubRepo

    with fail_once(GitHubRepo, "create_pull", after=1):
        with pytest.raises(Interrupt):
            apply(article, fake)
    del fake.branches[f"round/{SLUG}/r2"]

    with pytest.raises(ha.ImportApplyError, match="recorded but not in"):
        apply(article, fake)


def test_a_plan_changed_after_a_commit_was_recorded_is_refused(article, fake):
    from ci_core.github_repo import GitHubRepo

    with fail_once(GitHubRepo, "create_pull", after=1):
        with pytest.raises(Interrupt):
            apply(article, fake)
    plan = article.read()
    events = plan["rounds"][1]["events"]
    next(e for e in events if e["id"] == "e7")["include"] = False  # a later event
    next(e for e in events if e["id"] == "e5")["include"] = False  # a recorded one
    article.write(plan)

    with pytest.raises(ha.ImportApplyError, match="plan has changed"):
        apply(article, fake)


# ---------------------------------------------------------------------------
# How the merge is made
# ---------------------------------------------------------------------------


def test_the_merged_flag_that_appears_late_is_waited_for(article, fake):
    fake.merge_flag_lag = 3
    waits = []

    apply(article, fake, sleep=waits.append)

    assert all(p["merged"] for p in fake.pulls.values())
    assert waits  # it polled
    assert article.read()["applied"]["1"]["merge"]["path"] == "ref-move"


def test_when_github_does_not_mark_the_pull_request_merged_it_is_merged_through_the_api(
    article, fake
):
    fake.marks_merged_on_ref_move = False

    lines = apply(article, fake)

    assert all(p["merged"] for p in fake.pulls.values())
    merge = article.read()["applied"]["1"]["merge"]
    assert merge["path"] == "merge-pull"
    assert any("did not mark the pull request merged" in line for line in lines)
    puts = [r for r in fake.requests if r[0] == "PUT" and r[1].endswith("/merge")]
    assert len(puts) == 3
    assert puts[0][3]["commit_message"] == "Accepted 2026-06-01T10:00:00Z (imported)"
    assert puts[0][3]["merge_method"] == "merge"
    assert f"{SLUG}/r3" in fake.tags


def test_a_pull_request_that_stays_open_after_the_branch_holds_it_is_an_error(
    article, fake
):
    fake.marks_merged_on_ref_move = False
    fake.inject(
        "PUT", "/merge", Resp(405, {"message": "Pull Request is not mergeable"}), 99
    )

    with pytest.raises(ha.ImportApplyError, match="merging it was refused"):
        apply(article, fake)
    assert "merge" not in article.read()["applied"]["1"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def nothing_written(fake):
    return [r for r in fake.requests if r[0] != "GET"] == []


def test_a_public_repository_is_refused_with_no_override(article, fake):
    fake._repo_json = lambda: {  # noqa: SLF001
        "default_branch": "main",
        "private": False,
        "permissions": {"push": True},
        **fake.settings,
    }

    with pytest.raises(ha.ImportApplyError, match="public repository"):
        apply(article, fake, settings={**SETTINGS, "allow_public_repo": True})
    assert nothing_written(fake)


def test_a_token_that_cannot_push_is_refused(article, fake):
    fake._repo_json = lambda: {  # noqa: SLF001
        "default_branch": "main",
        "private": True,
        "permissions": {"push": False},
        **fake.settings,
    }

    with pytest.raises(ha.ImportApplyError, match="cannot push"):
        apply(article, fake)
    assert nothing_written(fake)


@pytest.mark.parametrize("setting", ["allow_squash_merge", "allow_rebase_merge"])
def test_a_repository_that_is_not_merge_commit_only_is_refused(article, fake, setting):
    fake.settings[setting] = True

    with pytest.raises(ha.ImportApplyError, match="merge commits only"):
        apply(article, fake)
    assert nothing_written(fake)


def test_a_main_branch_that_already_has_the_article_is_refused(article, fake):
    fake.external_commit("main", {f"{FOLDER}/draft.md": b"already here\n"})

    with pytest.raises(
        ha.ImportApplyError, match="already has articles/example-import/"
    ):
        apply(article, fake)
    assert nothing_written(fake)


def test_a_round_branch_or_tag_the_plan_does_not_account_for_is_refused(article, fake):
    fake.branches[f"round/{SLUG}/r9"] = fake.branches["main"]
    fake.tags[f"{SLUG}/r8"] = fake.branches["main"]

    with pytest.raises(ha.ImportApplyError, match="does not account for"):
        apply(article, fake)
    assert nothing_written(fake)


def test_another_articles_branches_and_tags_are_not_in_the_way(article, fake):
    fake.branches["round/another-article/r1"] = fake.branches["main"]
    fake.tags["another-article/r1"] = fake.branches["main"]

    apply(article, fake)

    assert f"{SLUG}/r3" in fake.tags


def test_a_plan_that_check_rejects_is_refused_before_any_request(article, fake):
    plan = article.plan
    plan["rounds"][0]["events"][0]["sha256"] = "0" * 64
    article.write(plan)

    with pytest.raises(ha.ImportApplyError, match="rejects the plan"):
        apply(article, fake)
    assert fake.requests == []


def test_a_document_kind_with_no_place_in_the_folder_is_refused(article, fake):
    plan = article.plan
    plan["rounds"][2]["events"][1]["kind"] = "changes"
    article.write(plan)

    with pytest.raises(ha.ImportApplyError, match="has no place"):
        apply(article, fake)
    assert fake.requests == []


def test_a_close_out_note_with_no_direction_is_refused(article, fake):
    path = Path(article.plan["rounds"][2]["events"][1]["sources"][0]["path"])
    text = "CLOSE-OUT HANDOFF\nDirection: [to-code | to-chat]\n"
    path.write_text(text, encoding="utf-8")
    plan = article.plan
    plan["rounds"][2]["events"][1]["sha256"] = sha(text)
    article.write(plan)

    with pytest.raises(ha.ImportApplyError, match="Direction"):
        apply(article, fake)
    assert fake.requests == []


def test_a_file_that_holds_a_configured_key_is_refused_before_anything_is_sent(
    article, fake
):
    path = Path(article.plan["rounds"][1]["events"][1]["sources"][0]["path"])
    text = article.metadata + f"leaked {KEY}\n"
    path.write_text(text, encoding="utf-8")
    plan = article.plan
    plan["rounds"][1]["events"][1]["sha256"] = sha(text)
    article.write(plan)

    with pytest.raises(ha.ImportApplyError, match="API key"):
        apply(article, fake)
    assert fake.requests == []


def test_a_run_with_no_review_file_is_refused(article, fake):
    report = Path(article.plan["rounds"][0]["events"][2]["report"])
    report.with_name(report.name.replace("_report.json", "_review.md")).unlink()

    with pytest.raises(ha.ImportApplyError, match="beside its report"):
        apply(article, fake)
    assert fake.requests == []


def test_a_plan_inside_the_checkout_is_refused(article, fake, monkeypatch):
    monkeypatch.setattr(hi, "checkout_roots", lambda: [article.plan_path.parent])

    with pytest.raises(hi.ImportScanError, match="inside the content-intelligence"):
        apply(article, fake)


def test_repo_is_required_and_has_no_default(article, fake):
    with pytest.raises(ha.ImportApplyError, match="--repo is required"):
        apply(article, fake, repo="  ")
    with pytest.raises(SystemExit):
        documents_cli.main(["import", "apply", str(article.plan_path)])


def test_a_github_error_names_the_resume(article, fake):
    fake.inject("POST", "/git/blobs", Resp(500, {"message": "boom"}), 99)

    with pytest.raises(ha.ImportApplyError, match="run the same command again"):
        apply(article, fake)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_prints_every_write_and_sends_none(article, fake):
    before = article.plan_path.read_bytes()

    lines = apply(article, fake, dry_run=True)

    assert nothing_written(fake)
    assert article.plan_path.read_bytes() == before
    text = "\n".join(lines)
    assert text.count("would create branch") == 3
    assert text.count("would commit") == 8
    assert text.count("would open pull request") == 3
    assert text.count("would post the review") == 3
    assert text.count("would merge") == 3
    assert text.count("would tag") == 3
    assert (
        f"would commit 'draft: {SLUG} r1' dated 2026-06-01T09:00:00Z: handoff.md"
        in text
    )
    assert "delete handoff.md" in text
    assert text.rstrip().endswith("Dry run: nothing was written.")


def test_dry_run_after_a_partial_apply_shows_only_what_is_left(article, fake):
    from ci_core.github_repo import GitHubRepo

    with fail_once(GitHubRepo, "create_review", after=1):
        with pytest.raises(Interrupt):
            apply(article, fake)
    before_plan = article.plan_path.read_bytes()
    before = len(fake.requests)

    lines = apply(article, fake, dry_run=True)

    assert [r for r in fake.requests[before:] if r[0] != "GET"] == []
    assert article.plan_path.read_bytes() == before_plan
    text = "\n".join(lines)
    assert text.count("would create branch") == 1  # round 3 only
    assert text.count("would post the review") == 2  # round 2's two runs
    assert text.count("would tag") == 2


# ---------------------------------------------------------------------------
# The command line, and a chat-only article through the real scan
# ---------------------------------------------------------------------------


def run_cli(fake, *argv):
    lines = []
    with patch(
        "ci_article_review.config_loader.load_user_config",
        return_value={"api_keys": API_KEYS},
    ):
        code = documents_cli.main(
            [*map(str, argv)],
            environ={"GITHUB_TOKEN": TOKEN},
            session=fake,
            out=lines.append,
        )
    return code, "\n".join(lines)


def test_the_command_applies_a_plan_and_a_dry_run_prints_without_writing(article, fake):
    argv = ["import", "apply", article.plan_path, "--repo", REPO]

    code, out = run_cli(fake, *argv, "--dry-run")
    assert code == 0 and "Dry run: nothing was written." in out
    assert nothing_written(fake)

    code, out = run_cli(fake, *argv)
    assert code == 0
    assert f"{SLUG}/r1" in fake.tags
    assert KEY not in out


def test_the_command_reports_a_refusal_as_one_error_and_exit_1(article, fake, capsys):
    fake.settings["allow_squash_merge"] = True

    code, _ = run_cli(fake, "import", "apply", article.plan_path, "--repo", REPO)

    assert code == 1
    assert "merge commits only" in capsys.readouterr().err


def test_a_chat_only_version_from_the_real_scan_is_written_with_its_chat_trailer(
    tmp_path, fake
):
    mapped = Mapped(_made(tmp_path / "chat"))
    plan, _ = mapped.scan()
    assert any(
        e.get("corroborated") is False for r in plan["rounds"] for e in r["events"]
    )
    plan_path = tmp_path / "chat-plan.json"
    plan_path.write_text(hi.plan_text(plan), encoding="utf-8")
    slug = plan["slug"]

    lines = []
    ha.apply_plan(
        plan_path,
        repo=REPO,
        client=client(fake),
        settings=SETTINGS,
        api_keys=API_KEYS,
        out=lines.append,
        now=lambda: NOW,
        sleep=lambda s: None,
    )

    branch = fake.branches[f"round/{slug}/r1"]
    messages = []
    while "Plan-event:" in fake.commits[branch]["message"]:
        messages.append(fake.commits[branch]["message"])
        branch = fake.commits[branch]["parents"][0]
    assert messages
    joined = "\n".join(messages)
    assert re.search(r"Imported-from: chat m1/m1b", joined)
    assert "Corroborated: no, only a chat held this version" in joined
    # a chat text is written as the version it was, ending in one newline
    tree = fake.trees[fake.commits[fake.branches[f"round/{slug}/r1"]]["tree"]]
    assert fake.blobs[tree[f"articles/{slug}/draft.md"]].endswith(b"\n")
    assert f"{slug}/r1" in fake.tags


def test_one_messages_edits_are_written_in_the_order_they_were_made(tmp_path, fake):
    """A message has one timestamp, so its edits tie; item numbers say the order.

    The items are 9, 11, 111 and 142: as strings ``item:11`` < ``item:111`` <
    ``item:142`` < ``item:9``, and the versions' hashes sort another way again, so
    only the number puts them in the order the edits were made.
    """
    base = tmp_path / "chat"
    base.mkdir()
    mapped = Mapped(base)
    items = [9, 11, 111, 142]
    expected, current = [], hi.normalise(mapped.v1)
    content = [{"type": "text", "text": ""} for _ in range(max(items))]
    for number, index in enumerate(items):
        old = f"Sentence {number + 3} of part a"
        new = f"{old}, revised alpha{number},"
        current = current.replace(old, new)
        expected.append(hi._sha(current))
        # the message's own text block is item 0, so item N sits at N - 1 here
        content[index - 1] = tool_use(
            "str_replace",
            {"path": mapped.path, "old_str": old, "new_str": new},
            when(1, 11),
            f"edit{number}",
            stamped=False,
        )
    content += [tool_result(f"edit{n}") for n in range(len(items))]
    edits = message("m1c", "assistant", when(1, 11), content=content)
    mapped.conversations[0]["chat_messages"][2:] = [edits]  # after the creation
    mapped.write()

    plan, _ = mapped.scan()

    locators = [f"item:{i}" for i in items]
    made = [
        e
        for r in plan["rounds"]
        for e in r["events"]
        if e["kind"] == "draft"
        and any(s.get("locator") in locators for s in e["sources"])
    ]
    assert [e["sha256"] for e in made] == expected
    assert [
        s["locator"] for e in made for s in e["sources"] if s.get("via")
    ] == locators
    # the tie this test is about, and that no other order would have passed
    assert len({e["at"] for e in made}) == 1
    assert sorted(expected) != expected
    assert sorted(locators) != locators

    plan_path = tmp_path / "edits-plan.json"
    plan_path.write_text(hi.plan_text(plan), encoding="utf-8")
    ids = [e["id"] for e in made]
    units = [u.ids for r in ha._plan_rounds(plan) for u in r.units]
    assert [i for unit in units for i in unit if i in ids] == ids

    def run(**kw):
        lines = []
        ha.apply_plan(
            plan_path,
            repo=REPO,
            client=client(fake),
            settings=SETTINGS,
            api_keys=API_KEYS,
            out=lines.append,
            now=lambda: NOW,
            sleep=lambda s: None,
            **kw,
        )
        return "\n".join(lines)

    assert run(dry_run=True).count("would commit") == len(units)
    assert nothing_written(fake)

    run()
    branch, written = fake.branches[f"round/{plan['slug']}/r1"], []
    while "Plan-event:" in fake.commits[branch]["message"]:
        tree = fake.trees[fake.commits[branch]["tree"]]
        data = fake.blobs[tree[f"articles/{plan['slug']}/draft.md"]]
        written.append(hi._sha(hi.normalise(data.decode("utf-8"))))
        branch = fake.commits[branch]["parents"][0]
    written.reverse()
    assert [w for w in written if w in expected] == expected


def test_the_tag_step_dates_an_import_at_accepted_at_and_leaves_other_callers_alone(
    article, fake
):
    from ci_article_review import github_rounds as gr

    assert gr.tag_message(SLUG, 3, 12, [6, 7], imported=True) == (
        f"{SLUG} r3 (imported): PR #12, runs 6 and 7"
    )
    assert gr.tag_message(SLUG, 3, 12, [6]) == f"{SLUG} r3: PR #12, run 6"


def test_a_rate_limit_is_explained_with_the_resume(article, fake):
    fake.inject(
        "POST",
        "/git/blobs",
        Resp(
            403,
            {"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"},
        ),
        99,
    )

    with pytest.raises(ha.ImportApplyError, match="again to resume"):
        apply(article, fake)
