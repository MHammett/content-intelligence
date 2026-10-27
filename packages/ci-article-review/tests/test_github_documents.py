"""``--github``: reading one round of an article from a GitHub repository (#416).

The GitHub side is an in-process stub with the calls the module makes (``info``,
``resolve``, ``list_dir``, ``read``, ``list_pulls``, ``get_pull``,
``matching_refs``, ``merge_base``, ``has_branch``); the HTTP behind them is
``ci_core.github_repo``'s and is tested there. The stub has no write method at
all, so a read that tried to write would fail on the attribute. What is tested
here is the part this module owns: which pull request or tag a run reads, which
files make the handoff, that the handoff equals what the matching local-file
flag builds from the same bytes, the history key and run-number inputs, and that
every failure is a plain message raised before any model call.

Slugs and contents are invented; this repository is public.
"""

import json
import logging
from pathlib import Path

import pytest
from ci_core.github_repo import (
    Entry,
    FileAtRef,
    GitHubRepoError,
    PullInfo,
    RepoInfo,
    git_blob_sha,
)

from ci_article_review import github_documents as gd
from ci_article_review.github_documents import GitHubDocumentsError
from ci_article_review.handoff_parser import (
    build_handoff_from_raw_draft_and_metadata,
    build_handoff_from_raw_text,
    parse_draft_submission,
)

BASE = "a" * 40  # main
HEAD = "b" * 40  # the head of round 1
MERGE_BASE = "c" * 40
HEAD2 = "d" * 40  # the head of round 2
TAGGED = "e" * 40  # the commit an accepted round was tagged at
SLUG = "example-article"
FOLDER = f"articles/{SLUG}"
EXAMPLES = Path(gd.__file__).parent / "handoff_templates" / "examples"
# A Windows checkout holds these with CRLF; the repository's bytes are LF.
HANDOFF = (
    (EXAMPLES / "draft_submission.short-example.full-coverage.md")
    .read_bytes()
    .replace(b"\r\n", b"\n")
)
METADATA = (
    (EXAMPLES / "metadata_only.filled-example.md").read_bytes().replace(b"\r\n", b"\n")
)
DRAFT = b"# Example Article\n\nA plain draft with one claim. It has no headers.\n"
SETTINGS = {"repo": "owner/docs", "path_prefix": "articles"}


def branch(n, slug=SLUG):
    return f"round/{slug}/r{n}"


def pull(
    number=1,
    n=1,
    *,
    slug=SLUG,
    sha=HEAD,
    state="open",
    merged=False,
    head_repo="owner/docs",
    title=None,
    head_ref=None,
):
    return PullInfo(
        number=number,
        state=state,
        merged=merged,
        merged_at="2026-10-01T00:00:00Z" if merged else None,
        merge_commit_sha=None,
        head_ref=head_ref or branch(n, slug),
        head_sha=sha,
        head_repo=head_repo,
        base_ref="main",
        base_sha=BASE,
        title=title or f"{slug} r{n}",
        html_url=f"https://github.com/owner/docs/pull/{number}",
    )


class StubRepo:
    """The calls ``github_documents`` makes, over ``{commit sha: {path: bytes}}``."""

    def __init__(
        self,
        trees=None,
        *,
        pulls=(),
        tags=None,
        heads=None,
        private=True,
        can_push=False,
    ):
        self.trees = {
            sha: {
                p: b if isinstance(b, bytes) else b.encode() for p, b in files.items()
            }
            for sha, files in (trees or {}).items()
        }
        self.pulls = list(pulls)
        #: tag name -> the commit it peels to
        self.tags = dict(tags or {})
        self.heads = {"main": BASE, **(heads or {})}
        for p in self.pulls:
            self.heads.setdefault(p.head_ref, p.head_sha)
        self.private, self.can_push = private, can_push
        self.merge_bases = {}
        self.calls = []
        self.closed = False

    def info(self):
        self.calls.append(("info",))
        return RepoInfo(
            default_branch="main", private=self.private, can_push=self.can_push
        )

    def resolve(self, ref):
        self.calls.append(("resolve", ref))
        if ref in self.heads:
            return self.heads[ref]
        if ref in self.tags:
            return self.tags[ref]
        if ref in self.trees:
            return ref
        raise GitHubRepoError("not_found", f"no commit for {ref!r}", status=422)

    def has_branch(self, name):
        self.calls.append(("has_branch", name))
        return name in self.heads

    def list_pulls(self, state="open"):
        self.calls.append(("list_pulls", state))
        return [p for p in self.pulls if state == "all" or p.state == state]

    def get_pull(self, number):
        self.calls.append(("get_pull", number))
        for p in self.pulls:
            if p.number == number:
                return p
        raise GitHubRepoError("not_found", f"no pull request {number}", status=404)

    def matching_refs(self, prefix):
        self.calls.append(("matching_refs", prefix))
        assert prefix.startswith("tags/"), prefix
        return [
            (f"refs/tags/{name}", sha)
            for name, sha in sorted(self.tags.items())
            if f"tags/{name}".startswith(prefix)
        ]

    def merge_base(self, base, head):
        self.calls.append(("merge_base", base, head))
        return self.merge_bases.get((base, head), MERGE_BASE)

    def list_dir(self, path, ref):
        self.calls.append(("list_dir", path, ref))
        files = self.trees.get(ref, {})
        prefix = f"{path}/" if path else ""
        seen = {}
        for p in files:
            if p.startswith(prefix):
                rest = p[len(prefix) :]
                head = rest.split("/")[0]
                seen[head] = "dir" if "/" in rest else "file"
        return [
            Entry(name=n, path=f"{prefix}{n}", type=t, sha="0" * 40)
            for n, t in sorted(seen.items())
        ]

    def read(self, path, ref):
        self.calls.append(("read", path, ref))
        files = self.trees.get(ref, {})
        if path not in files:
            raise GitHubRepoError("not_found", f"{path} does not exist", status=404)
        data = files[path]
        return FileAtRef(data=data, blob_sha=git_blob_sha(data), commit_sha=ref)

    def close(self):
        self.closed = True

    @property
    def reads(self):
        return [c for c in self.calls if c[0] == "read"]


def folder_files(**files):
    return {f"{FOLDER}/{name}": data for name, data in files.items()}


def article(**files):
    """A repository with round 1 of ``example-article`` open, holding ``files``."""
    return StubRepo(
        {HEAD: folder_files(**files), BASE: {}},
        pulls=[pull()],
    )


def load(stub, **kw):
    kw.setdefault("settings", SETTINGS)
    return gd.load_handoff(SLUG, client=stub, **kw)


def source(handoff):
    return handoff["document_source"]


class TestWhatARunReads:
    """Each row of the table in the issue."""

    def two_rounds(self):
        return StubRepo(
            {
                HEAD: folder_files(**{"draft.md": DRAFT}),
                HEAD2: folder_files(**{"draft.md": DRAFT + b"Round two.\n"}),
                BASE: {},
            },
            # GitHub lists the newest first.
            pulls=[pull(2, 2, sha=HEAD2, title="second round"), pull(1, 1)],
        )

    # no flags

    def test_the_one_open_round_is_read_at_its_head(self):
        got = source(load(article(**{"draft.md": DRAFT})))
        assert got["kind"] == "github" and got["repo"] == "owner/docs"
        assert got["ref"] == branch(1) and got["commit_sha"] == HEAD
        assert (got["round"], got["pull_request"], got["base_sha"]) == (
            1,
            1,
            MERGE_BASE,
        )
        assert got["files"] == [
            {"path": f"{FOLDER}/draft.md", "blob_sha": git_blob_sha(DRAFT)}
        ]

    def test_other_articles_and_other_branches_are_not_this_rounds(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls += [
            pull(2, 1, slug="other-article"),
            pull(3, head_ref="feature/something"),
            pull(4, head_ref=f"round/{SLUG}-two/r1"),
            pull(5, 3, state="closed"),
        ]
        assert source(load(stub))["pull_request"] == 1

    def test_no_open_round_lists_the_tags_and_the_ways_forward(self):
        stub = StubRepo({BASE: {}}, tags={f"{SLUG}/r1": TAGGED, f"{SLUG}/r2": TAGGED})
        with pytest.raises(GitHubDocumentsError) as exc:
            load(stub)
        message = str(exc.value)
        assert f"no open round of {SLUG}" in message
        assert f"{SLUG}/r1" in message and f"{SLUG}/r2" in message
        assert "AGENTS.md" in message and "--round" in message
        assert "--github-ref main" in message

    def test_no_open_round_and_no_tag_says_so(self):
        with pytest.raises(GitHubDocumentsError, match="no tags for this article"):
            load(StubRepo({BASE: {}}))

    def test_two_open_rounds_are_listed_and_pr_is_asked_for(self):
        with pytest.raises(GitHubDocumentsError) as exc:
            load(self.two_rounds())
        message = str(exc.value)
        assert "2 open rounds" in message and "--pr" in message
        assert f'#1 {branch(1)} "{SLUG} r1"' in message
        assert f'#2 {branch(2)} "second round"' in message
        assert message.index("#1 ") < message.index("#2 ")

    def test_a_pull_request_from_a_fork_is_refused(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(head_repo="someone/docs")]
        with pytest.raises(GitHubDocumentsError, match="someone/docs"):
            load(stub)
        assert stub.reads == []

    def test_a_pull_request_whose_fork_is_gone_is_refused(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(head_repo="")]
        with pytest.raises(GitHubDocumentsError, match="no longer exists"):
            load(stub)

    def test_the_repository_name_compares_without_regard_to_case(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(head_repo="Owner/Docs")]
        assert source(load(stub))["pull_request"] == 1

    # --pr N

    def test_pr_reads_that_pull_request_though_two_are_open(self):
        got = source(load(self.two_rounds(), pull_request=2))
        assert (got["pull_request"], got["round"], got["commit_sha"]) == (2, 2, HEAD2)
        assert got["ref"] == branch(2)

    def test_pr_must_be_a_round_of_this_article(self):
        stub = self.two_rounds()
        stub.pulls.append(pull(9, 1, slug="other-article"))
        stub.pulls.append(pull(10, head_ref="feature/something"))
        for number in (9, 10):
            with pytest.raises(GitHubDocumentsError, match="not a round of"):
                load(stub, pull_request=number)

    def test_pr_that_does_not_exist(self):
        with pytest.raises(GitHubDocumentsError, match="no pull request #99"):
            load(self.two_rounds(), pull_request=99)

    def test_pr_from_a_fork_is_refused(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(head_repo="someone/docs")]
        with pytest.raises(GitHubDocumentsError, match="someone/docs"):
            load(stub, pull_request=1)

    def test_pr_reads_a_merged_pull_request_at_the_head_it_ended_with(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(state="closed", merged=True)]
        stub.heads[branch(1)] = "f" * 40  # the branch moved on after the merge
        got = source(load(stub, pull_request=1))
        assert got["commit_sha"] == HEAD and got["pull_request"] == 1
        assert ("resolve", branch(1)) not in stub.calls

    def test_pr_with_a_branch_that_is_not_rN_has_no_round_number(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(head_ref=f"round/{SLUG}/second-try")]
        assert source(load(stub, pull_request=1))["round"] is None

    # --round N

    def test_round_reads_the_open_pull_request_of_that_round(self):
        got = source(load(self.two_rounds(), round_number=2))
        assert (got["pull_request"], got["commit_sha"], got["round"]) == (2, HEAD2, 2)

    def test_round_reads_the_tag_when_no_pull_request_is_open(self):
        stub = StubRepo(
            {BASE: {}, TAGGED: folder_files(**{"draft.md": DRAFT})},
            tags={f"{SLUG}/r3": TAGGED},
        )
        got = source(load(stub, round_number=3))
        assert (got["ref"], got["commit_sha"], got["round"]) == (
            f"{SLUG}/r3",
            TAGGED,
            3,
        )
        assert got["pull_request"] is None and got["base_sha"] is None
        assert ("merge_base", BASE, TAGGED) not in stub.calls

    def test_round_prefers_the_open_pull_request_to_a_tag(self):
        stub = article(**{"draft.md": DRAFT})
        stub.tags[f"{SLUG}/r1"] = TAGGED
        assert source(load(stub, round_number=1))["pull_request"] == 1

    def test_a_closed_pull_request_is_not_an_open_round(self):
        stub = StubRepo(
            {BASE: {}, TAGGED: folder_files(**{"draft.md": DRAFT})},
            pulls=[pull(state="closed", merged=True)],
            tags={f"{SLUG}/r1": TAGGED},
        )
        got = source(load(stub, round_number=1))
        assert got["pull_request"] is None and got["commit_sha"] == TAGGED

    def test_round_with_neither_lists_the_rounds_that_exist(self):
        stub = self.two_rounds()
        stub.tags[f"{SLUG}/r7"] = TAGGED
        with pytest.raises(GitHubDocumentsError) as exc:
            load(stub, round_number=5)
        message = str(exc.value)
        assert "no round 5" in message and branch(5) in message
        assert f"{SLUG}/r5" in message
        assert (
            "Rounds that exist: r1 (open pull request #1), "
            "r2 (open pull request #2), r7 (tag)."
        ) in message

    def test_a_tag_that_only_resembles_a_round_is_not_one(self):
        stub = StubRepo(
            {BASE: {}},
            tags={
                f"{SLUG}/r1-old": TAGGED,
                f"{SLUG}/r01": TAGGED,
                f"{SLUG}/v1": TAGGED,
            },
        )
        with pytest.raises(GitHubDocumentsError, match="no rounds of this article"):
            load(stub, round_number=1)

    def test_two_pull_requests_from_one_round_branch_ask_for_pr(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls.append(pull(2, 1))
        with pytest.raises(GitHubDocumentsError, match="Pass --pr"):
            load(stub, round_number=1)

    # --github-ref REF

    def test_ref_reads_a_tag_as_it_is(self):
        stub = StubRepo(
            {BASE: {}, TAGGED: folder_files(**{"draft.md": DRAFT})},
            pulls=[pull(1, 1), pull(2, 2)],
            tags={f"{SLUG}/r3": TAGGED},
        )
        got = source(load(stub, ref=f"{SLUG}/r3"))
        assert (got["ref"], got["commit_sha"]) == (f"{SLUG}/r3", TAGGED)
        assert (got["round"], got["pull_request"], got["base_sha"]) == (
            None,
            None,
            None,
        )
        # No pull request is looked up for it: two are open, and it is no matter.
        assert not [c for c in stub.calls if c[0] in ("list_pulls", "get_pull")]

    def test_ref_reads_main_and_a_commit_sha(self):
        stub = StubRepo(
            {BASE: folder_files(**{"draft.md": DRAFT})}, pulls=[pull(1, 1), pull(2, 2)]
        )
        assert source(load(stub, ref="main"))["commit_sha"] == BASE
        assert source(load(stub, ref=BASE))["ref"] == BASE

    def test_an_unknown_ref(self):
        with pytest.raises(GitHubDocumentsError, match="no branch, tag or commit 'v9'"):
            load(article(**{"draft.md": DRAFT}), ref="v9")

    # exclusivity and write-back

    @pytest.mark.parametrize(
        "kw",
        [
            {"pull_request": 1, "round_number": 1},
            {"pull_request": 1, "ref": "main"},
            {"round_number": 1, "ref": "main"},
        ],
    )
    def test_the_three_ways_to_choose_are_exclusive(self, kw):
        stub = article(**{"draft.md": DRAFT})
        with pytest.raises(GitHubDocumentsError, match="only one"):
            load(stub, **kw)
        assert stub.calls == []

    def test_ref_never_writes_back(self):
        stub = article(**{"draft.md": DRAFT})
        with pytest.raises(GitHubDocumentsError, match="never writes back"):
            load(stub, ref="main", write_back=True)
        assert stub.calls == []

    def test_a_tag_round_cannot_be_written_back_to(self):
        stub = StubRepo(
            {BASE: {}, TAGGED: folder_files(**{"draft.md": DRAFT})},
            tags={f"{SLUG}/r3": TAGGED},
            can_push=True,
        )
        with pytest.raises(GitHubDocumentsError, match="is closed"):
            load(stub, round_number=3, write_back=True)
        assert stub.reads == []

    @pytest.mark.parametrize(
        "state, merged, word",
        [("closed", True, "merged"), ("closed", False, "closed")],
    )
    def test_a_pull_request_that_is_not_open_cannot_be_written_back_to(
        self, state, merged, word
    ):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [pull(state=state, merged=merged)]
        stub.can_push = True
        with pytest.raises(GitHubDocumentsError, match=f"is {word}"):
            load(stub, pull_request=1, write_back=True)
        assert stub.reads == []

    def test_an_open_pull_request_can_be_written_back_to(self):
        stub = article(**{"draft.md": DRAFT})
        stub.can_push = True
        got = source(load(stub, write_back=True))
        assert got["ref"] == branch(1) and got["pull_request"] == 1
        assert ("has_branch", branch(1)) in stub.calls

    def test_write_back_to_a_round_whose_branch_is_gone(self):
        stub = article(**{"draft.md": DRAFT})
        stub.can_push = True
        del stub.heads[branch(1)]
        with pytest.raises(GitHubDocumentsError, match="not a branch"):
            load(stub, write_back=True)


class TestTheHeadMovesOrLags:
    def test_a_lagging_pull_request_pointer_reads_the_branch(self, caplog):
        stub = StubRepo(
            {
                HEAD: folder_files(**{"draft.md": b"old text\n"}),
                HEAD2: folder_files(**{"draft.md": b"# New\n\nnew text\n"}),
                BASE: {},
            },
            pulls=[pull(sha=HEAD)],
            heads={branch(1): HEAD2},
        )
        with caplog.at_level(logging.WARNING):
            handoff = load(stub)
        assert source(handoff)["commit_sha"] == HEAD2
        assert "new text" in handoff["draft"]
        assert "lagging" in caplog.text and HEAD[:12] in caplog.text

    def test_a_pointer_that_agrees_with_the_branch_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING):
            load(article(**{"draft.md": DRAFT}))
        assert "lagging" not in caplog.text

    def test_every_listing_and_read_names_the_pinned_sha(self):
        stub = article(**{"draft.md": DRAFT, "metadata.md": METADATA})
        load(stub)
        later = [c for c in stub.calls if c[0] in ("list_dir", "read")]
        assert later and all(c[-1] in (HEAD, BASE) for c in later)
        assert [c[-1] for c in later if c[1].endswith(("draft.md", "metadata.md"))] == [
            HEAD,
            HEAD,
        ]
        # The ref is resolved once for the base branch and once to check the lag.
        assert [c for c in stub.calls if c[0] == "resolve"] == [
            ("resolve", branch(1)),
            ("resolve", "main"),
        ]


class TestLoadForms:
    """Each form builds the handoff its local-file flag builds from the same bytes."""

    def without_source(self, handoff):
        return {
            k: v
            for k, v in handoff.items()
            if k
            not in (
                "document_source",
                "history_key",
                "run_number",
                "run_number_declared",
                "taken_run_numbers",
            )
        }

    def test_handoff_equals_draft_flag(self):
        stub = article(**{"handoff.md": HANDOFF})
        expected = parse_draft_submission(HANDOFF.decode())
        got = load(stub)
        assert self.without_source(got) == self.without_source(expected)
        # The file's own History key and Pipeline run: line are kept.
        assert got["history_key"] == expected["history_key"]
        assert got["run_number"] == expected["run_number"]
        assert [f["path"] for f in source(got)["files"]] == [f"{FOLDER}/handoff.md"]

    def test_draft_and_metadata_equal_raw_draft_with_metadata(self):
        stub = article(**{"draft.md": DRAFT, "metadata.md": METADATA})
        expected = build_handoff_from_raw_draft_and_metadata(
            DRAFT.decode(), METADATA.decode(), source_name=SLUG
        )
        got = load(stub)
        assert self.without_source(got) == self.without_source(expected)
        assert [f["path"] for f in source(got)["files"]] == [
            f"{FOLDER}/draft.md",
            f"{FOLDER}/metadata.md",
        ]

    def test_draft_alone_equals_raw_draft_and_warns(self, caplog):
        stub = article(**{"draft.md": DRAFT})
        with caplog.at_level(logging.WARNING):
            got = load(stub)
        assert self.without_source(got) == self.without_source(
            build_handoff_from_raw_text(DRAFT.decode(), source_name=SLUG)
        )
        assert "raw-draft mode" in caplog.text

    def test_a_draft_and_a_handoff_read_the_draft_and_warn_the_handoff_is_stale(
        self, caplog
    ):
        stub = article(**{"handoff.md": HANDOFF, "draft.md": DRAFT})
        expected = build_handoff_from_raw_text(DRAFT.decode(), source_name=SLUG)
        with caplog.at_level(logging.WARNING):
            got = load(stub)
        assert self.without_source(got) == self.without_source(expected)
        assert [f["path"] for f in source(got)["files"]] == [f"{FOLDER}/draft.md"]
        assert f"{FOLDER}/handoff.md" not in [c[1] for c in stub.reads]
        stale = [r for r in caplog.records if "handoff.md" in r.getMessage()]
        assert stale and stale[0].levelno == logging.WARNING
        assert "stale" in stale[0].getMessage()

    def test_a_draft_with_a_metadata_file_and_a_stale_handoff(self):
        stub = article(
            **{"handoff.md": HANDOFF, "draft.md": DRAFT, "metadata.md": METADATA}
        )
        expected = build_handoff_from_raw_draft_and_metadata(
            DRAFT.decode(), METADATA.decode(), source_name=SLUG
        )
        assert self.without_source(load(stub)) == self.without_source(expected)

    def test_a_metadata_file_alone_is_nothing_to_review(self):
        stub = article(**{"metadata.md": METADATA, "notes.md": b"x"})
        with pytest.raises(GitHubDocumentsError) as exc:
            load(stub)
        message = str(exc.value)
        assert f"{FOLDER}/" in message and "draft.md" in message
        assert branch(1) in message and HEAD[:12] in message

    def test_a_folder_with_neither_names_the_folder_and_the_ref(self):
        with pytest.raises(GitHubDocumentsError, match="neither draft.md nor handoff"):
            load(article(**{"notes.md": b"x"}))

    def test_the_old_per_round_names_are_not_read(self):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT, f"{SLUG}-handoff-r1.md": b"x"})
        with pytest.raises(GitHubDocumentsError, match="neither draft.md"):
            load(stub)

    def test_crlf_bytes_read_as_a_local_file_would(self, tmp_path):
        crlf = HANDOFF.replace(b"\n", b"\r\n")
        local = tmp_path / "h.md"
        local.write_bytes(crlf)
        stub = article(**{"handoff.md": crlf})
        assert (
            load(stub)["draft"]
            == parse_draft_submission(local.read_text(encoding="utf-8"))["draft"]
        )

    def test_non_utf8_is_an_error(self):
        stub = article(**{"draft.md": b"\xff\xfe\x00bad"})
        with pytest.raises(GitHubDocumentsError, match="not UTF-8"):
            load(stub)

    def test_blob_shas_are_gits(self):
        stub = article(**{"draft.md": DRAFT})
        assert source(load(stub))["files"] == [
            {"path": f"{FOLDER}/draft.md", "blob_sha": git_blob_sha(DRAFT)}
        ]

    def test_a_path_prefix_of_nothing_reads_the_repo_root(self):
        stub = StubRepo({HEAD: {f"{SLUG}/draft.md": DRAFT}, BASE: {}}, pulls=[pull()])
        handoff = load(stub, settings={**SETTINGS, "path_prefix": ""})
        assert source(handoff)["files"][0]["path"] == f"{SLUG}/draft.md"

    def test_a_missing_folder_names_the_articles_that_exist(self):
        stub = StubRepo(
            {HEAD: {"articles/other-one/draft.md": DRAFT}, BASE: {}}, pulls=[pull()]
        )
        with pytest.raises(GitHubDocumentsError) as exc:
            load(stub)
        assert f"{FOLDER}/" in str(exc.value) and "other-one" in str(exc.value)

    def test_the_client_is_closed(self):
        stub = article(**{"draft.md": DRAFT})
        load(stub)
        assert stub.closed


class TestRead:
    """A read without ``--write-back`` stays read-only; the tagging hook sits on the write path."""

    def test_a_plain_read_never_reaches_the_tagging_hook(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            gd, "_tag_accepted_rounds", lambda *a, **k: called.append(1)
        )
        load(article(**{"draft.md": DRAFT}))
        assert called == []

    def test_a_read_makes_only_reads(self):
        # The stub has no write method, so any attempt would be an AttributeError;
        # this names what it did call.
        stub = article(**{"draft.md": DRAFT})
        load(stub)
        assert {c[0] for c in stub.calls} <= {
            "info",
            "resolve",
            "list_pulls",
            "list_dir",
            "read",
            "merge_base",
        }

    def test_write_back_reaches_the_hook_after_the_permission_check(self, monkeypatch):
        stub = article(**{"draft.md": DRAFT})
        stub.can_push = True
        seen = []
        monkeypatch.setattr(
            gd,
            "_tag_accepted_rounds",
            lambda client, slug, **kw: seen.append((slug, kw, list(stub.calls))),
        )
        load(stub, write_back=True)
        [(slug, kw, calls_then)] = seen
        assert slug == SLUG and kw == {"repo_name": "owner/docs"}
        # Only the repository was asked about before it: no pull request, no file.
        assert calls_then == [("info",)]

    def test_a_refused_write_back_never_reaches_the_hook(self, monkeypatch):
        stub = article(**{"draft.md": DRAFT})  # the token cannot push
        monkeypatch.setattr(gd, "_tag_accepted_rounds", pytest.fail)
        with pytest.raises(GitHubDocumentsError, match="cannot push"):
            load(stub, write_back=True)


def run_json(number):
    return json.dumps({"run_number": number}).encode()


class TestHistoryKeyAndRunNumberInputs:
    def test_history_key_is_the_slug_when_the_handoff_has_none(self):
        assert load(article(**{"draft.md": DRAFT}))["history_key"] == SLUG

    def test_a_handoff_without_a_history_key_gets_the_slug(self):
        text = HANDOFF.decode().replace("History key: short-example-smoke-test\n", "")
        assert "History key:" not in text
        assert load(article(**{"handoff.md": text}))["history_key"] == SLUG

    def test_a_matching_history_key_is_kept_without_a_warning(self, caplog):
        text = HANDOFF.decode().replace("short-example-smoke-test", SLUG)
        with caplog.at_level(logging.WARNING):
            assert load(article(**{"handoff.md": text}))["history_key"] == SLUG
        assert "History key" not in caplog.text

    def test_a_mismatched_history_key_warns_naming_both_and_is_kept(self, caplog):
        with caplog.at_level(logging.WARNING):
            handoff = load(article(**{"handoff.md": HANDOFF}))
        assert handoff["history_key"] == "short-example-smoke-test"
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        text = " ".join(r.getMessage() for r in warned)
        assert "short-example-smoke-test" in text and SLUG in text

    def test_a_slug_history_would_rename_warns(self, caplog):
        slug = "Example.Article"
        stub = StubRepo(
            {HEAD: {f"articles/{slug}/draft.md": DRAFT}, BASE: {}},
            pulls=[pull(slug=slug)],
        )
        with caplog.at_level(logging.WARNING):
            gd.load_handoff(slug, settings=SETTINGS, client=stub)
        assert "history directory" in caplog.text

    def test_the_round_is_not_the_run_number(self):
        stub = StubRepo(
            {HEAD2: folder_files(**{"draft.md": DRAFT}), BASE: {}},
            pulls=[pull(4, 4, sha=HEAD2)],
        )
        handoff = load(stub)
        assert source(handoff)["round"] == 4
        assert handoff["run_number_declared"] is False
        assert handoff["run_number"] == 1  # the parser's default, not the round

    def test_a_declared_run_number_is_used_as_written(self):
        # "Pipeline run: 1" is a declaration, not an absence.
        handoff = load(article(**{"handoff.md": HANDOFF}))
        assert "Pipeline run: 1" in HANDOFF.decode()
        assert (handoff["run_number"], handoff["run_number_declared"]) == (1, True)

    def test_a_handoff_without_the_line_declares_nothing(self):
        text = HANDOFF.decode().replace("Pipeline run: 1\n", "")
        assert "Pipeline run:" not in text
        assert load(article(**{"handoff.md": text}))["run_number_declared"] is False

    def test_a_declared_run_in_the_metadata_wins(self):
        meta = METADATA.decode()
        declared = int(
            [ln for ln in meta.splitlines() if ln.startswith("Pipeline run:")][0].split(
                ":"
            )[1]
        )
        handoff = load(article(**{"draft.md": DRAFT, "metadata.md": METADATA}))
        assert handoff["run_number"] == declared
        assert handoff["run_number_declared"] is True

    def test_a_metadata_file_without_the_line_declares_nothing(self):
        meta = METADATA.decode().replace("Pipeline run:", "Note:")
        handoff = load(article(**{"draft.md": DRAFT, "metadata.md": meta}))
        assert handoff["run_number_declared"] is False


class TestTakenRunNumbers:
    """``pipeline/run.json`` at the head and at the base branch is how another machine is known."""

    def stub(self, at_head=None, at_base=None, **files):
        head = folder_files(**{"draft.md": DRAFT})
        base = {}
        if at_head is not None:
            head[f"{FOLDER}/pipeline/run.json"] = at_head
        if at_base is not None:
            base[f"{FOLDER}/pipeline/run.json"] = at_base
        return StubRepo({HEAD: head, BASE: base}, pulls=[pull()])

    def test_none_when_there_is_no_run_file(self):
        assert load(self.stub())["taken_run_numbers"] == []

    def test_the_number_at_the_head(self):
        assert load(self.stub(at_head=run_json(6)))["taken_run_numbers"] == [6]

    def test_the_numbers_at_the_head_and_at_main(self):
        got = load(self.stub(at_head=run_json(6), at_base=run_json(4)))
        assert got["taken_run_numbers"] == [4, 6]

    def test_the_same_number_twice_is_listed_once(self):
        got = load(self.stub(at_head=run_json(6), at_base=run_json(6)))
        assert got["taken_run_numbers"] == [6]

    def test_a_read_at_main_itself_reads_the_file_once(self):
        stub = StubRepo(
            {
                BASE: {
                    **folder_files(**{"draft.md": DRAFT}),
                    f"{FOLDER}/pipeline/run.json": run_json(2),
                }
            }
        )
        got = load(stub, ref="main")
        assert got["taken_run_numbers"] == [2]
        assert [c for c in stub.reads if c[1].endswith("run.json")] == [
            ("read", f"{FOLDER}/pipeline/run.json", BASE)
        ]

    def test_every_github_read_lists_them_not_only_write_back(self):
        got = load(self.stub(at_head=run_json(6)), write_back=False)
        assert got["taken_run_numbers"] == [6]

    def test_a_tag_and_a_ref_read_them_too(self):
        stub = StubRepo(
            {
                BASE: {},
                TAGGED: {
                    **folder_files(**{"draft.md": DRAFT}),
                    f"{FOLDER}/pipeline/run.json": run_json(9),
                },
            },
            tags={f"{SLUG}/r2": TAGGED},
        )
        assert load(stub, round_number=2)["taken_run_numbers"] == [9]
        assert load(stub, ref=TAGGED)["taken_run_numbers"] == [9]

    @pytest.mark.parametrize(
        "content",
        [
            b"not json at all",
            b"\xff\xfe",
            b"[]",
            b"[6]",
            b"null",
            b"{}",
            b'{"run_number": "6"}',
            b'{"run_number": 6.5}',
            b'{"run_number": true}',
            b'{"run_number": 0}',
            b'{"run_number": -3}',
            b'{"run_number": null}',
        ],
    )
    def test_an_unusable_file_is_ignored_with_a_debug_line(self, content, caplog):
        with caplog.at_level(logging.DEBUG, logger=gd.log.name):
            got = load(self.stub(at_head=content, at_base=run_json(2)))
        assert got["taken_run_numbers"] == [2]
        assert [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "run.json" in r.getMessage()
        ]
        assert not [
            r
            for r in caplog.records
            if r.name == gd.log.name and r.levelno >= logging.WARNING
        ]

    def test_a_base_branch_that_cannot_be_resolved_is_ignored(self):
        stub = self.stub(at_head=run_json(3))
        del stub.heads["main"]
        assert load(stub)["taken_run_numbers"] == [3]

    def test_the_base_branch_is_the_configured_one(self):
        stub = self.stub(at_head=run_json(3))
        stub.heads["drafts"] = "9" * 40
        stub.trees["9" * 40] = {f"{FOLDER}/pipeline/run.json": run_json(8)}
        got = load(stub, settings={**SETTINGS, "branch": "drafts"})
        assert got["taken_run_numbers"] == [3, 8]

    def test_a_read_failure_other_than_absence_is_not_swallowed(self):
        stub = self.stub()

        def read(path, ref):
            if path.endswith("run.json"):
                raise GitHubRepoError("rate_limited", "slow down", status=403)
            return StubRepo.read(stub, path, ref)

        stub.read = read
        with pytest.raises(GitHubDocumentsError, match="rate limit"):
            load(stub)


class TestBaseSha:
    def test_it_is_the_merge_base_of_the_pull_requests_base_and_its_head(self):
        stub = article(**{"draft.md": DRAFT})
        stub.merge_bases[(BASE, HEAD)] = "f" * 40
        assert source(load(stub))["base_sha"] == "f" * 40
        assert ("merge_base", BASE, HEAD) in stub.calls

    def test_a_merged_pull_request_uses_the_base_it_had(self):
        stub = article(**{"draft.md": DRAFT})
        stub.pulls = [
            PullInfo(
                **{**pull(state="closed", merged=True).__dict__, "base_sha": "9" * 40}
            )
        ]
        stub.merge_bases[("9" * 40, HEAD)] = "8" * 40
        assert source(load(stub, pull_request=1))["base_sha"] == "8" * 40


class TestFailuresAreRaisedWithAMessage:
    def test_empty_repo_setting(self):
        for bad in ("", "   "):
            with pytest.raises(GitHubDocumentsError, match="github_documents.repo"):
                gd.load_handoff(
                    SLUG, settings={**SETTINGS, "repo": bad}, client=StubRepo()
                )

    def test_a_bad_repo_name(self):
        with pytest.raises(GitHubDocumentsError, match="owner/name"):
            gd.load_handoff(
                SLUG,
                settings={**SETTINGS, "repo": "not a repo"},
                token_lookup=lambda: "tok",
            )

    @pytest.mark.parametrize(
        "slug", ["", "a/b", "..", "a b", ".hidden", "a..b", "a\\b", "round/x/r1"]
    )
    def test_a_slug_that_is_not_a_folder_name(self, slug):
        stub = StubRepo()
        with pytest.raises(GitHubDocumentsError, match="slug"):
            gd.load_handoff(slug, settings=SETTINGS, client=stub)
        assert stub.calls == []

    def test_a_public_repository_warns_once_and_continues(self, caplog):
        stub = article(**{"draft.md": DRAFT})
        stub.private = False
        with caplog.at_level(logging.WARNING):
            handoff = load(stub)
        assert source(handoff)["round"] == 1
        public = [r for r in caplog.records if "public repository" in r.getMessage()]
        assert len(public) == 1

    def test_a_private_repository_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            load(article(**{"draft.md": DRAFT}))
        assert "public repository" not in caplog.text


class _Raises(StubRepo):
    def __init__(self, error, where="info"):
        super().__init__()
        self._error, self._where = error, where

    def info(self):
        if self._where == "info":
            raise self._error
        return super().info()

    def list_pulls(self, state="open"):
        if self._where == "list_pulls":
            raise self._error
        return super().list_pulls(state)


class TestGitHubErrorKinds:
    def run(self, error, *, has_token=True, where="info"):
        with pytest.raises(GitHubDocumentsError) as exc:
            gd.load_handoff(
                SLUG,
                settings=SETTINGS,
                client=_Raises(error, where),
                has_token=has_token,
            )
        return str(exc.value)

    def test_no_token_and_not_found_says_how_to_get_one(self):
        message = self.run(
            GitHubRepoError("not_found", "x", status=404), has_token=False
        )
        assert "GITHUB_TOKEN" in message and "gh auth login" in message

    def test_not_found_with_a_token_says_missing_or_not_visible(self):
        message = self.run(GitHubRepoError("not_found", "x", status=404))
        assert "missing, or not visible to this token" in message

    def test_401(self):
        message = self.run(GitHubRepoError("auth", "x", status=401))
        assert "rejected the token" in message and "gh auth login" in message

    def test_401_from_the_pull_request_listing(self):
        message = self.run(GitHubRepoError("auth", "x", status=401), where="list_pulls")
        assert "rejected the token" in message

    def test_forbidden(self):
        assert "cannot read owner/docs" in self.run(
            GitHubRepoError("forbidden", "403 body", status=403)
        )

    def test_rate_limited_names_the_reset_time(self):
        message = self.run(
            GitHubRepoError("rate_limited", "x", status=403, reset_at=1_800_000_000)
        )
        assert "rate limit" in message and "UTC" in message

    def test_empty_repository(self):
        assert "no commits" in self.run(GitHubRepoError("empty", "x", status=409))

    def test_other_errors_keep_their_text(self):
        assert "HTTP 500" in self.run(GitHubRepoError("other", "HTTP 500", status=500))

    def test_the_error_is_a_value_error_so_main_exits_one(self):
        assert issubclass(GitHubDocumentsError, ValueError)


class TestTokenLookup:
    def test_a_client_is_built_with_the_token_found(self, monkeypatch):
        seen = {}

        class Fake(StubRepo):
            def __init__(self, repo, token):
                super().__init__(
                    {HEAD: folder_files(**{"draft.md": DRAFT}), BASE: {}},
                    pulls=[pull()],
                )
                seen["args"] = (repo, token)

        monkeypatch.setattr(gd, "GitHubRepo", Fake)
        gd.load_handoff(SLUG, settings=SETTINGS, token_lookup=lambda: "tok")
        assert seen["args"] == ("owner/docs", "tok")

    def test_no_token_is_reported_as_no_token(self, monkeypatch):
        class Fake(_Raises):
            def __init__(self, repo, token):
                super().__init__(GitHubRepoError("not_found", "x", status=404))

        monkeypatch.setattr(gd, "GitHubRepo", Fake)
        with pytest.raises(GitHubDocumentsError, match="GITHUB_TOKEN"):
            gd.load_handoff(SLUG, settings=SETTINGS, token_lookup=lambda: None)

    def test_the_token_never_reaches_a_message(self, monkeypatch):
        class Fake(_Raises):
            def __init__(self, repo, token):
                super().__init__(GitHubRepoError("auth", "bad token", status=401))

        monkeypatch.setattr(gd, "GitHubRepo", Fake)
        with pytest.raises(GitHubDocumentsError) as exc:
            gd.load_handoff(
                SLUG, settings=SETTINGS, token_lookup=lambda: "ghp_SECRETVALUE"
            )
        assert "ghp_SECRETVALUE" not in str(exc.value)
