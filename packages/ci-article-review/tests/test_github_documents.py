"""``--github``: reading one round of an article from a GitHub repository (#402).

The GitHub side is an in-process stub with the four calls the module makes
(``info``, ``resolve``, ``list_dir``, ``read``); the HTTP behind them is
``ci_core.github_repo``'s and is tested there. What is tested here is the part
this module owns: which round is picked, which files make the handoff, that the
handoff equals what the matching local-file flag builds from the same bytes, the
history key and run number rules, and that every failure is a plain message
raised before any model call.

Slugs and contents are invented; this repository is public.
"""

import logging
from pathlib import Path

import pytest
from ci_core.github_repo import (
    Entry,
    FileAtRef,
    GitHubRepoError,
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

COMMIT = "a" * 40
SLUG = "example-article"
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


class StubRepo:
    """The four calls ``github_documents`` makes, over a dict of path -> bytes."""

    def __init__(self, files, *, private=True, branches=("main",)):
        self.files = {
            p: b if isinstance(b, bytes) else b.encode() for p, b in files.items()
        }
        self.private = private
        self.branches = set(branches)
        self.calls = []
        self.closed = False

    def info(self):
        self.calls.append(("info",))
        return RepoInfo(default_branch="main", private=self.private, can_push=False)

    def resolve(self, ref):
        self.calls.append(("resolve", ref))
        if ref in self.branches:
            return COMMIT
        raise GitHubRepoError("not_found", f"no commit for {ref!r}", status=422)

    def list_dir(self, path, ref):
        self.calls.append(("list_dir", path, ref))
        prefix = f"{path}/" if path else ""
        seen = {}
        for p in self.files:
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
        if path not in self.files:
            raise GitHubRepoError("not_found", f"{path} does not exist", status=404)
        data = self.files[path]
        return FileAtRef(data=data, blob_sha=git_blob_sha(data), commit_sha=COMMIT)

    def close(self):
        self.closed = True


def article(**files):
    """A stub repo holding ``articles/example-article/<name>`` files."""
    return StubRepo({f"articles/{SLUG}/{name}": data for name, data in files.items()})


def load(stub, **kw):
    kw.setdefault("settings", SETTINGS)
    return gd.load_handoff(SLUG, client=stub, **kw)


class TestFindRounds:
    def names(self, *names):
        return gd.find_rounds(list(names), SLUG)

    def test_collects_kinds_per_round(self):
        rounds = self.names(
            f"{SLUG}-draft-r1.md",
            f"{SLUG}-metadata-r1.md",
            f"{SLUG}-handoff-r2.md",
        )
        assert rounds == {
            1: {"draft": f"{SLUG}-draft-r1.md", "metadata": f"{SLUG}-metadata-r1.md"},
            2: {"handoff": f"{SLUG}-handoff-r2.md"},
        }

    @pytest.mark.parametrize(
        "name",
        [
            f"{SLUG}-draft-r4-old.md",  # only resembles the pattern
            f"{SLUG}-draft-r04.md",
            f"{SLUG}-draft-r0.md",
            f"{SLUG}-draft-r4.md.bak",
            f"{SLUG}-publication-r4.md",
            f"{SLUG}-review-r4.md",
            "other-slug-draft-r4.md",
            f"{SLUG}x-draft-r4.md",
        ],
    )
    def test_names_that_only_resemble_the_pattern_are_ignored(self, name):
        assert self.names(name) == {}

    def test_a_lone_metadata_file_is_not_a_round(self):
        assert self.names(f"{SLUG}-metadata-r3.md") == {}

    def test_the_slug_is_matched_literally(self):
        # '.' in a slug is not a regex wildcard.
        assert gd.find_rounds(["aXb-draft-r1.md"], "a.b") == {}
        assert gd.find_rounds(["a.b-draft-r1.md"], "a.b") == {
            1: {"draft": "a.b-draft-r1.md"}
        }


class TestRoundSelection:
    def stub(self):
        return article(**{f"{SLUG}-draft-r{n}.md": DRAFT for n in (1, 2, 10)})

    def test_highest_round_by_default_numerically(self):
        # r10 sorts before r2 as text; it is the highest as a number.
        assert load(self.stub())["document_source"]["round"] == 10

    def test_an_explicit_round(self):
        handoff = load(self.stub(), round_number=2)
        assert handoff["document_source"]["round"] == 2
        assert [f["path"] for f in handoff["document_source"]["files"]] == [
            f"articles/{SLUG}/{SLUG}-draft-r2.md"
        ]

    def test_a_missing_round_lists_the_rounds_that_exist(self):
        with pytest.raises(GitHubDocumentsError) as exc:
            load(self.stub(), round_number=3)
        assert "no round 3" in str(exc.value)
        assert "r1, r2, r10" in str(exc.value)

    def test_a_name_that_only_resembles_a_round_does_not_count(self):
        stub = article(
            **{f"{SLUG}-draft-r1.md": DRAFT, f"{SLUG}-draft-r4-old.md": DRAFT}
        )
        assert load(stub)["document_source"]["round"] == 1

    def test_a_folder_with_no_round_files(self):
        stub = article(**{f"{SLUG}-notes.md": b"x"})
        with pytest.raises(GitHubDocumentsError, match="to review"):
            load(stub)


class TestLoadForms:
    """Each form builds the handoff its local-file flag builds from the same bytes."""

    def without_source(self, handoff):
        return {
            k: v
            for k, v in handoff.items()
            if k not in ("document_source", "history_key", "run_number")
        }

    def test_handoff_equals_draft_flag(self):
        stub = article(**{f"{SLUG}-handoff-r1.md": HANDOFF})
        expected = parse_draft_submission(HANDOFF.decode())
        got = load(stub)
        assert self.without_source(got) == self.without_source(expected)
        # The file's own History key and Pipeline run: line are kept.
        assert got["history_key"] == expected["history_key"]
        assert got["run_number"] == expected["run_number"]

    def test_draft_and_metadata_equal_raw_draft_with_metadata(self):
        stub = article(
            **{f"{SLUG}-draft-r1.md": DRAFT, f"{SLUG}-metadata-r1.md": METADATA}
        )
        expected = build_handoff_from_raw_draft_and_metadata(
            DRAFT.decode(), METADATA.decode(), source_name=SLUG
        )
        got = load(stub)
        assert self.without_source(got) == self.without_source(expected)
        assert [f["path"] for f in got["document_source"]["files"]] == [
            f"articles/{SLUG}/{SLUG}-draft-r1.md",
            f"articles/{SLUG}/{SLUG}-metadata-r1.md",
        ]

    def test_draft_alone_equals_raw_draft_and_warns(self, caplog):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        with caplog.at_level(logging.WARNING):
            got = load(stub)
        assert self.without_source(got) == self.without_source(
            build_handoff_from_raw_text(DRAFT.decode(), source_name=SLUG)
        )
        assert "raw-draft mode" in caplog.text

    def test_a_handoff_beats_a_draft_in_the_same_round(self):
        stub = article(
            **{f"{SLUG}-handoff-r1.md": HANDOFF, f"{SLUG}-draft-r1.md": DRAFT}
        )
        assert [f["path"] for f in load(stub)["document_source"]["files"]] == [
            f"articles/{SLUG}/{SLUG}-handoff-r1.md"
        ]

    def test_crlf_bytes_read_as_a_local_file_would(self, tmp_path):
        crlf = HANDOFF.replace(b"\n", b"\r\n")
        local = tmp_path / "h.md"
        local.write_bytes(crlf)
        stub = article(**{f"{SLUG}-handoff-r1.md": crlf})
        assert (
            load(stub)["draft"]
            == parse_draft_submission(local.read_text(encoding="utf-8"))["draft"]
        )

    def test_non_utf8_is_an_error(self):
        stub = article(**{f"{SLUG}-draft-r1.md": b"\xff\xfe\x00bad"})
        with pytest.raises(GitHubDocumentsError, match="not UTF-8"):
            load(stub)


class TestPinnedToOneCommit:
    def test_every_listing_and_read_names_the_resolved_sha(self):
        stub = article(
            **{f"{SLUG}-draft-r1.md": DRAFT, f"{SLUG}-metadata-r1.md": METADATA}
        )
        handoff = load(stub, ref="main")
        later = [c for c in stub.calls if c[0] in ("list_dir", "read")]
        assert later and all(c[-1] == COMMIT for c in later)
        assert [c for c in stub.calls if c[0] == "resolve"] == [("resolve", "main")]
        src = handoff["document_source"]
        assert (src["kind"], src["repo"], src["ref"], src["commit_sha"]) == (
            "github",
            "owner/docs",
            "main",
            COMMIT,
        )

    def test_blob_shas_are_gits(self):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        files = load(stub)["document_source"]["files"]
        assert files == [
            {
                "path": f"articles/{SLUG}/{SLUG}-draft-r1.md",
                "blob_sha": git_blob_sha(DRAFT),
            }
        ]

    def test_the_ref_defaults_to_the_configured_branch_then_the_repo_default(self):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        stub.branches = {"main", "drafts"}
        assert (
            load(stub, settings={**SETTINGS, "branch": "drafts"})["document_source"][
                "ref"
            ]
            == "drafts"
        )
        assert load(stub)["document_source"]["ref"] == "main"
        assert (
            load(stub, ref="main", settings={**SETTINGS, "branch": "drafts"})[
                "document_source"
            ]["ref"]
            == "main"
        )

    def test_a_path_prefix_of_nothing_reads_the_repo_root(self):
        stub = StubRepo({f"{SLUG}/{SLUG}-draft-r1.md": DRAFT})
        handoff = load(stub, settings={**SETTINGS, "path_prefix": ""})
        assert handoff["document_source"]["files"][0]["path"] == (
            f"{SLUG}/{SLUG}-draft-r1.md"
        )

    def test_the_client_is_closed(self):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        load(stub)
        assert stub.closed


class TestHistoryKeyAndRunNumber:
    def test_history_key_is_the_slug_when_the_handoff_has_none(self):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        assert load(stub)["history_key"] == SLUG

    def test_a_handoff_without_a_history_key_gets_the_slug(self):
        text = HANDOFF.decode().replace("History key: short-example-smoke-test\n", "")
        assert "History key:" not in text
        stub = article(**{f"{SLUG}-handoff-r1.md": text})
        assert load(stub)["history_key"] == SLUG

    def test_a_matching_history_key_is_kept_without_a_warning(self, caplog):
        text = HANDOFF.decode().replace("short-example-smoke-test", SLUG)
        stub = article(**{f"{SLUG}-handoff-r1.md": text})
        with caplog.at_level(logging.WARNING):
            assert load(stub)["history_key"] == SLUG
        assert "History key" not in caplog.text

    def test_a_mismatched_history_key_warns_naming_both_and_is_kept(self, caplog):
        stub = article(**{f"{SLUG}-handoff-r1.md": HANDOFF})
        with caplog.at_level(logging.WARNING):
            handoff = load(stub)
        assert handoff["history_key"] == "short-example-smoke-test"
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        text = " ".join(r.getMessage() for r in warned)
        assert "short-example-smoke-test" in text and SLUG in text

    def test_run_number_defaults_to_the_round_when_absent(self):
        stub = article(**{f"{SLUG}-draft-r4.md": DRAFT})
        assert load(stub)["run_number"] == 4

    def test_run_number_defaults_to_the_round_for_a_handoff_without_the_line(self):
        text = HANDOFF.decode().replace("Pipeline run: 1\n", "")
        stub = article(**{f"{SLUG}-handoff-r4.md": text})
        assert load(stub)["run_number"] == 4

    def test_an_explicit_run_one_is_not_mistaken_for_absent(self):
        # Round 4 of a handoff that says "Pipeline run: 1" reviews as run 1.
        stub = article(**{f"{SLUG}-handoff-r4.md": HANDOFF})
        assert "Pipeline run: 1" in HANDOFF.decode()
        assert load(stub)["run_number"] == 1

    def test_an_explicit_run_in_the_metadata_wins(self):
        meta = METADATA.decode()
        assert "Pipeline run:" in meta, "fixture must declare a run"
        declared = int(
            [ln for ln in meta.splitlines() if ln.startswith("Pipeline run:")][0].split(
                ":"
            )[1]
        )
        stub = article(
            **{f"{SLUG}-draft-r7.md": DRAFT, f"{SLUG}-metadata-r7.md": METADATA}
        )
        assert load(stub)["run_number"] == declared

    def test_a_metadata_file_without_the_line_takes_the_round(self):
        meta = METADATA.decode().replace("Pipeline run:", "Note:")
        stub = article(**{f"{SLUG}-draft-r7.md": DRAFT, f"{SLUG}-metadata-r7.md": meta})
        assert load(stub)["run_number"] == 7

    def test_a_slug_history_would_rename_warns(self, caplog):
        stub = StubRepo({"articles/Example.Article/Example.Article-draft-r1.md": DRAFT})
        with caplog.at_level(logging.WARNING):
            gd.load_handoff("Example.Article", settings=SETTINGS, client=stub)
        assert "history directory" in caplog.text


class TestFailuresAreRaisedWithAMessage:
    def test_missing_folder_names_the_articles_that_exist(self):
        stub = StubRepo({"articles/other-one/other-one-draft-r1.md": DRAFT})
        with pytest.raises(GitHubDocumentsError) as exc:
            load(stub)
        assert f"articles/{SLUG}/" in str(exc.value)
        assert "other-one" in str(exc.value)

    def test_empty_repo_setting(self):
        for bad in ("", "   "):
            with pytest.raises(GitHubDocumentsError, match="github_documents.repo"):
                gd.load_handoff(
                    SLUG, settings={**SETTINGS, "repo": bad}, client=StubRepo({})
                )

    def test_a_bad_repo_name(self):
        with pytest.raises(GitHubDocumentsError, match="owner/name"):
            gd.load_handoff(
                SLUG,
                settings={**SETTINGS, "repo": "not a repo"},
                token_lookup=lambda: "tok",
            )

    @pytest.mark.parametrize(
        "slug", ["", "a/b", "..", "a b", ".hidden", "a..b", "a\\b"]
    )
    def test_a_slug_that_is_not_a_folder_name(self, slug):
        with pytest.raises(GitHubDocumentsError, match="slug"):
            gd.load_handoff(slug, settings=SETTINGS, client=StubRepo({}))

    def test_an_unknown_ref(self):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        with pytest.raises(GitHubDocumentsError, match="no branch, tag or commit 'v9'"):
            load(stub, ref="v9")

    def test_a_public_repository_warns_once_and_continues(self, caplog):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        stub.private = False
        with caplog.at_level(logging.WARNING):
            handoff = load(stub)
        assert handoff["document_source"]["round"] == 1
        public = [r for r in caplog.records if "public repository" in r.getMessage()]
        assert len(public) == 1

    def test_a_private_repository_does_not_warn(self, caplog):
        stub = article(**{f"{SLUG}-draft-r1.md": DRAFT})
        with caplog.at_level(logging.WARNING):
            load(stub)
        assert "public repository" not in caplog.text


class _Raises(StubRepo):
    def __init__(self, error, where="info"):
        super().__init__({})
        self._error, self._where = error, where

    def info(self):
        if self._where == "info":
            raise self._error
        return super().info()

    def resolve(self, ref):
        if self._where == "resolve":
            raise self._error
        return super().resolve(ref)


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
                super().__init__({f"articles/{SLUG}/{SLUG}-draft-r1.md": DRAFT})
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
