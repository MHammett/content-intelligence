"""``--github`` wired into ``ci-review`` (#402): the config block, the command
line, and the report.

The reading itself is ``test_github_documents.py``. These tests cover what
connects it: ``github_documents`` surviving ``merge_configs`` (a top-level key
the merge does not name is dropped silently), every usage error, that a run
without ``--github`` never reaches GitHub, and that the provenance lands in the
saved report under a history directory named for the slug.
"""

import json
import logging
import subprocess
import sys
from unittest.mock import patch

import pytest

from ci_article_review import config_loader, pipeline
from ci_article_review.config_loader import (
    DEFAULT_GITHUB_DOCUMENTS_REPO,
    GITHUB_DOCUMENTS_DEFAULTS,
    merge_configs,
    normalize_github_documents,
)

SLUG = "example-article"
SOURCE = {
    "kind": "github",
    "repo": "owner/docs",
    "ref": "main",
    "commit_sha": "a" * 40,
    "round": 3,
    "pull_request": 7,
    "base_sha": "c" * 40,
    "files": [{"path": f"articles/{SLUG}/draft.md", "blob_sha": "b" * 40}],
}


class TestConfigBlock:
    BLOCK = {
        "repo": "owner/docs",
        "branch": "drafts",
        "path_prefix": "docs/articles",
        "write_back": True,
        "include_capture": True,
        "allow_public_repo": True,
    }

    def test_absent_block_merges_to_the_defaults(self):
        assert merge_configs({}, {})["github_documents"] == GITHUB_DOCUMENTS_DEFAULTS

    def test_the_defaults_are_the_documented_ones(self):
        assert GITHUB_DOCUMENTS_DEFAULTS == {
            "repo": DEFAULT_GITHUB_DOCUMENTS_REPO,
            "branch": None,
            "path_prefix": "articles",
            "write_back": False,
            "include_capture": False,
            "allow_public_repo": False,
        }
        assert DEFAULT_GITHUB_DOCUMENTS_REPO == "MHammett/mikehammett.net-articles"

    def test_the_block_survives_the_merge(self):
        merged = merge_configs({"github_documents": dict(self.BLOCK)}, {})
        assert merged["github_documents"] == self.BLOCK

    @pytest.mark.parametrize("preset", ["wide", "thorough", "maximum"])
    def test_the_block_survives_a_cost_preset(self, preset):
        # The presets rewrite ``models:`` and ``pipeline:``. They must not take
        # the document repository with them.
        user = {
            "github_documents": dict(self.BLOCK),
            "pipeline": {"cost_preset": preset},
            "models": {"openai": {"model": "gpt-5.4"}},
        }
        merged = merge_configs(user, {})
        assert merged["pipeline"]["cost_preset"] == preset
        assert merged["github_documents"] == self.BLOCK

    def test_a_partial_block_keeps_the_defaults_it_did_not_mention(self):
        merged = merge_configs({"github_documents": {"branch": "drafts"}}, {})
        assert merged["github_documents"] == {
            **GITHUB_DOCUMENTS_DEFAULTS,
            "branch": "drafts",
        }

    def test_a_key_left_empty_in_yaml_is_unset_but_an_empty_string_is_kept(self):
        block = normalize_github_documents(
            {"repo": None, "branch": None, "path_prefix": ""}
        )
        assert block["repo"] == DEFAULT_GITHUB_DOCUMENTS_REPO
        assert block["branch"] is None
        assert block["path_prefix"] == ""
        assert normalize_github_documents({"repo": ""})["repo"] == ""

    @pytest.mark.parametrize("bad", ["x", 3, ["a"], None])
    def test_a_block_that_is_not_a_mapping_never_stops_a_run(self, bad):
        assert (
            merge_configs({"github_documents": bad}, {})["github_documents"]
            == GITHUB_DOCUMENTS_DEFAULTS
        )

    def test_the_input_is_not_changed(self):
        user = {"github_documents": {"branch": "drafts"}}
        merge_configs(user, {})
        assert user == {"github_documents": {"branch": "drafts"}}

    def test_the_example_config_carries_the_block_commented_out(self):
        text = (config_loader._PACKAGED_CONFIGS / "user.example.yaml").read_text(
            encoding="utf-8"
        )
        assert "# github_documents:" in text
        assert "\ngithub_documents:" not in text
        # No token key, and no ${VAR}: an unset one stops config loading.
        lines = text[text.index("# github_documents:") :].splitlines()[1:]
        keys = []
        for line in lines:
            if not line.startswith("#   "):
                break
            assert "${" not in line
            keys.append(line[4:].split(":")[0].strip())
        assert keys == list(GITHUB_DOCUMENTS_DEFAULTS)


@pytest.fixture
def run_main(tmp_history_root, capsys):
    """Run ``pipeline.main()`` with ``run_draft_pipeline`` stubbed.

    Returns ``(call, run_mock)``; ``call(*argv)`` returns the exit code (0 when
    ``main`` returns) and the stubbed pipeline records how it was called.
    """

    def call(*argv, load=None, settings=None):
        patches = [
            patch.object(sys, "argv", ["ci-review", *argv, "--publication", "myblog"]),
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch(
                "ci_article_review.pipeline.load_user_config",
                return_value={"github_documents": settings or {"repo": "owner/docs"}},
            ),
        ]
        if load is not None:
            patches.append(
                patch("ci_article_review.github_documents.load_handoff", load)
            )
        with patch("ci_article_review.pipeline.run_draft_pipeline") as run:
            from contextlib import ExitStack

            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                try:
                    pipeline.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
        call.run = run
        return code

    return call


def fake_handoff(**extra):
    return {
        "title": "An Example Article Title",
        "draft": "Body text.",
        "run_number": 3,
        "history_key": SLUG,
        "document_source": SOURCE,
        **extra,
    }


class TestUsageErrors:
    @pytest.mark.parametrize(
        "argv, mentions",
        [
            (["--raw-draft", "d.md", "--round", "2"], "--round requires --github"),
            (["--raw-draft", "d.md", "--pr", "7"], "--pr requires --github"),
            (["--draft", "h.md", "--pr", "7"], "--pr requires --github"),
            (
                ["--raw-draft", "d.md", "--github-ref", "x"],
                "--github-ref requires --github",
            ),
            (
                ["--raw-draft", "d.md", "--github-repo", "a/b"],
                "--github-repo requires --github",
            ),
            (["--draft", "h.md", "--round", "2"], "--round requires --github"),
            (["--github", SLUG, "--round", "0"], "--round must be 1 or more"),
            (["--github", SLUG, "--round", "-2"], "--round must be 1 or more"),
            (["--github", SLUG, "--pr", "0"], "--pr must be 1 or more"),
            (["--github", SLUG, "--pr", "x"], "invalid int value"),
            (["--github", SLUG, "--pr", "7", "--round", "2"], "not allowed with"),
            (["--github", SLUG, "--round", "2", "--pr", "7"], "not allowed with"),
            (
                ["--github", SLUG, "--pr", "7", "--github-ref", "main"],
                "not allowed with",
            ),
            (
                ["--github", SLUG, "--round", "2", "--github-ref", "main"],
                "not allowed with",
            ),
            (
                ["--github", SLUG, "--github-ref", "main", "--write-back"],
                "--write-back does not combine with --github-ref",
            ),
            (["--github", SLUG, "--metadata", "m.md"], "--metadata"),
            (["--github", SLUG, "--archive-only"], "--archive-only"),
            (["--github", SLUG, "--publish", "p.md"], "not allowed with"),
            (["--github", SLUG, "--draft", "h.md"], "not allowed with"),
            (["--github", SLUG, "--raw-draft", "d.md"], "not allowed with"),
            (["--github", SLUG, "--url", "https://example.org/x"], "not allowed with"),
            (["--github", SLUG, "--round", "two"], "invalid int value"),
        ],
    )
    def test_each_is_a_usage_error_before_any_work(
        self, run_main, capsys, argv, mentions
    ):
        sentinel = pytest.fail  # a GitHub read here would fail the test
        assert run_main(*argv, load=sentinel) == 2
        assert mentions in capsys.readouterr().err
        run_main.run.assert_not_called()

    def test_github_alone_is_a_complete_source(self, run_main):
        assert run_main("--github", SLUG, load=lambda *a, **k: fake_handoff()) == 0


class TestGithubReachesThePipeline:
    def test_flags_reach_load_handoff_and_the_handoff_reaches_the_run(self, run_main):
        seen = {}

        def load(slug, **kw):
            seen.update(slug=slug, **kw)
            return fake_handoff()

        code = run_main(
            "--github",
            SLUG,
            "--round",
            "3",
            "--github-repo",
            "owner/scratch",
            "--cost-preset",
            "wide",
            load=load,
            settings={"repo": "owner/docs", "path_prefix": "docs"},
        )
        assert code == 0
        assert seen["slug"] == SLUG
        assert (seen["round_number"], seen["ref"], seen["repo"]) == (
            3,
            None,
            "owner/scratch",
        )
        assert seen["pull_request"] is None
        assert seen["settings"]["path_prefix"] == "docs"
        # Defaults the block did not mention are filled in.
        assert seen["settings"]["write_back"] is False
        run = run_main.run
        run.assert_called_once()
        assert run.call_args.args[0] is None
        assert run.call_args.kwargs["handoff"]["document_source"] == SOURCE
        assert run.call_args.kwargs["cost_preset"] == "wide"

    def test_without_the_optional_flags_they_are_none(self, run_main):
        seen = {}
        run_main(
            "--github",
            SLUG,
            load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
        )
        assert (seen["round_number"], seen["ref"], seen["repo"]) == (None, None, None)
        assert seen["pull_request"] is None

    def test_pr_and_ref_reach_load_handoff_each_on_its_own(self, run_main):
        seen = {}

        def load(slug, **kw):
            seen.update(kw)
            return fake_handoff()

        run_main("--github", SLUG, "--pr", "7", load=load)
        assert (seen["pull_request"], seen["round_number"], seen["ref"]) == (
            7,
            None,
            None,
        )
        run_main("--github", SLUG, "--github-ref", "main", load=load)
        assert (seen["pull_request"], seen["round_number"], seen["ref"]) == (
            None,
            None,
            "main",
        )

    @pytest.mark.parametrize(
        "extra, kwarg, value",
        [
            (["--replay", "r.json"], "replay_results", "r.json"),
            (["--retry-failed", "r.json"], "retry_failed_results", "r.json"),
            (["--offline"], "offline", True),
        ],
    )
    def test_replay_retry_failed_and_offline_work_as_with_raw_draft(
        self, run_main, extra, kwarg, value
    ):
        # --offline still reads the input from GitHub, as --url fetches its page.
        reads = []
        run_main(
            "--github",
            SLUG,
            *extra,
            load=lambda slug, **kw: reads.append(slug) or fake_handoff(),
        )
        assert reads == [SLUG]
        assert run_main.run.call_args.kwargs[kwarg] == value

    def test_a_failed_read_exits_one_before_the_pipeline(self, run_main, caplog):
        def load(slug, **kw):
            raise pipeline_error(
                "example/missing is missing, or not visible to this token"
            )

        from ci_article_review.github_documents import (
            GitHubDocumentsError as pipeline_error,
        )

        with caplog.at_level(logging.ERROR):
            assert run_main("--github", SLUG, load=load) == 1
        assert "not visible to this token" in caplog.text
        run_main.run.assert_not_called()

    def test_an_empty_repo_setting_exits_one_without_a_token_lookup(
        self, run_main, caplog
    ):
        with (
            patch(
                "ci_core.github_repo.resolve_token",
                side_effect=AssertionError("looked"),
            ),
            patch(
                "ci_article_review.github_documents.resolve_token",
                side_effect=AssertionError("looked"),
            ),
            caplog.at_level(logging.ERROR),
        ):
            assert run_main("--github", SLUG, settings={"repo": ""}) == 1
        assert "github_documents.repo is empty" in caplog.text
        run_main.run.assert_not_called()


class TestNothingGithubWithoutTheFlag:
    @pytest.mark.parametrize(
        "argv",
        [["--raw-draft", "d.md"], ["--draft", "h.md"]],
    )
    def test_no_token_is_looked_up_and_nothing_is_read(self, run_main, argv):
        def fail(*a, **k):
            pytest.fail("GitHub was reached without --github")

        with (
            patch("ci_core.github_repo.resolve_token", fail),
            patch("ci_article_review.github_documents.resolve_token", fail),
            patch("ci_core.github_repo.GitHubRepo", fail),
            patch("ci_article_review.github_documents.GitHubRepo", fail),
            patch("ci_article_review.github_documents.load_handoff", fail),
            patch(
                "ci_article_review.pipeline._read_handoff_file",
                return_value="# A Draft Title\n\nBody.",
            ),
        ):
            assert run_main(*argv) == 0
        run_main.run.assert_called_once()

    def test_the_client_is_not_even_imported(self):
        # In a fresh interpreter, because this process has imported it by now.
        code = (
            "import sys, ci_article_review.pipeline as p;"
            "p.build_parser();"
            "bad = [m for m in ('ci_core.github_repo', 'ci_article_review.github_documents') if m in sys.modules];"
            "print(bad); sys.exit(1 if bad else 0)"
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert done.returncode == 0, done.stdout + done.stderr


class TestDocumentSourceInTheReport:
    def test_it_lands_in_the_saved_report_under_a_directory_named_for_the_slug(
        self, tmp_path, monkeypatch
    ):
        from .test_pipeline_end_to_end import _HANDOFF, _stubbed_run

        monkeypatch.setitem(_HANDOFF, "history_key", SLUG)
        monkeypatch.setitem(_HANDOFF, "document_source", SOURCE)
        with _stubbed_run(tmp_path) as report:
            assert report["document_source"] == SOURCE
        folder = tmp_path / "history" / SLUG
        assert folder.is_dir(), sorted(p.name for p in (tmp_path / "history").iterdir())
        [saved] = folder.glob("run_*_report.json")
        assert (
            json.loads(saved.read_text(encoding="utf-8"))["document_source"] == SOURCE
        )

    def test_a_run_from_a_file_has_no_document_source(self, tmp_path):
        from .test_pipeline_end_to_end import _stubbed_run

        with _stubbed_run(tmp_path) as report:
            assert "document_source" not in report


class TestRunNumbers:
    """Round and run are separate numbers: the run is one past everything known.

    "Known" is local history plus ``taken_run_numbers`` (``pipeline/run.json`` at
    the round's head and at the base branch), unless the handoff declares its own
    ``Pipeline run:``.
    """

    def run(self, tmp_path, monkeypatch, *, history=(), taken=(), declared=None):
        from .test_pipeline_end_to_end import _HANDOFF, _stubbed_run

        folder = tmp_path / "history" / SLUG
        folder.mkdir(parents=True)
        for n in history:
            (folder / f"run_{n}_20260101_000000_report.json").write_text(
                "{}", encoding="utf-8"
            )
        monkeypatch.setitem(_HANDOFF, "history_key", SLUG)
        monkeypatch.setitem(_HANDOFF, "taken_run_numbers", list(taken))
        monkeypatch.setitem(_HANDOFF, "run_number_declared", declared is not None)
        monkeypatch.setitem(_HANDOFF, "run_number", declared or 1)
        with _stubbed_run(tmp_path) as report:
            return report["run_number"]

    def test_run_json_says_6_and_history_has_1_to_4_the_run_is_7(
        self, tmp_path, monkeypatch
    ):
        assert self.run(tmp_path, monkeypatch, history=[1, 2, 3, 4], taken=[6]) == 7

    def test_a_declared_pipeline_run_is_used_as_written(self, tmp_path, monkeypatch):
        got = self.run(
            tmp_path, monkeypatch, history=[1, 2, 3, 4], taken=[6], declared=9
        )
        assert got == 9

    def test_no_run_json_and_no_history_is_run_1(self, tmp_path, monkeypatch):
        assert self.run(tmp_path, monkeypatch) == 1

    def test_a_taken_number_alone_moves_the_run_past_it(self, tmp_path, monkeypatch):
        # Run 1 is free locally and in the repository's list; the run is still
        # 7, because another machine made 6 and an absent line means "next".
        assert self.run(tmp_path, monkeypatch, taken=[6]) == 7

    def test_local_history_alone_moves_the_run_past_it(self, tmp_path, monkeypatch):
        assert self.run(tmp_path, monkeypatch, history=[2, 5]) == 6

    def test_a_declared_number_that_is_taken_is_bumped_with_a_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        with caplog.at_level(logging.WARNING):
            got = self.run(tmp_path, monkeypatch, taken=[3, 4], declared=3)
        assert got == 5 and "Run 3 already exists" in caplog.text

    def test_an_absent_line_is_not_a_warning(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING):
            self.run(tmp_path, monkeypatch, history=[1, 2], taken=[6])
        assert "already exists" not in caplog.text

    def test_a_handoff_from_a_file_still_bumps_only_on_a_clash(
        self, tmp_path, monkeypatch
    ):
        # Not --github: no run_number_declared key, so run 1 stands while it is free.
        from .test_pipeline_end_to_end import _HANDOFF, _stubbed_run

        monkeypatch.setitem(_HANDOFF, "history_key", SLUG)
        monkeypatch.delitem(_HANDOFF, "run_number_declared", raising=False)
        folder = tmp_path / "history" / SLUG
        folder.mkdir(parents=True)
        (folder / "run_2_20260101_000000_report.json").write_text(
            "{}", encoding="utf-8"
        )
        with _stubbed_run(tmp_path) as report:
            assert report["run_number"] == 1


NOT_A_REVIEW = [
    ("--replay", "r.json"),
    ("--offline",),
    ("--only-model", "openai"),
    ("--only-domain", "fact_check"),
    ("--no-timeout",),
]


class TestWriteBackFlags:
    """``--write-back`` / ``--no-write-back`` (#403): usage errors and the config default."""

    @pytest.mark.parametrize(
        "argv, mentions",
        [
            (["--raw-draft", "d.md", "--write-back"], "--write-back requires --github"),
            (
                ["--raw-draft", "d.md", "--no-write-back"],
                "--no-write-back requires --github",
            ),
            (["--draft", "h.md", "--write-back"], "--write-back requires --github"),
        ]
        + [
            (
                ["--github", SLUG, "--write-back", *flag],
                f"does not combine with {flag[0]}",
            )
            for flag in NOT_A_REVIEW
        ],
    )
    def test_each_usage_error_comes_before_any_work(
        self, run_main, capsys, argv, mentions
    ):
        assert run_main(*argv, load=pytest.fail) == 2
        assert mentions in capsys.readouterr().err
        run_main.run.assert_not_called()

    def test_the_flag_turns_it_on_for_the_read_and_the_run(self, run_main):
        seen = {}
        run_main(
            "--github",
            SLUG,
            "--write-back",
            load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
        )
        assert seen["write_back"] is True
        assert run_main.run.call_args.kwargs["write_back"] is True

    def test_it_is_off_unless_asked_for(self, run_main):
        seen = {}
        run_main(
            "--github",
            SLUG,
            load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
        )
        assert seen["write_back"] is False
        assert run_main.run.call_args.kwargs["write_back"] is False

    def test_config_turns_it_on(self, run_main):
        seen = {}
        run_main(
            "--github",
            SLUG,
            settings={"repo": "owner/docs", "write_back": True},
            load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
        )
        assert seen["write_back"] is True
        assert run_main.run.call_args.kwargs["write_back"] is True

    def test_no_write_back_beats_the_config(self, run_main):
        run_main(
            "--github",
            SLUG,
            "--no-write-back",
            settings={"repo": "owner/docs", "write_back": True},
            load=lambda slug, **kw: fake_handoff(),
        )
        assert run_main.run.call_args.kwargs["write_back"] is False

    @pytest.mark.parametrize("flag", NOT_A_REVIEW, ids=lambda f: f[0])
    def test_config_is_set_aside_with_one_info_line_for_a_run_that_is_no_review(
        self, run_main, caplog, flag
    ):
        seen = {}
        with caplog.at_level(logging.INFO):
            code = run_main(
                "--github",
                SLUG,
                *flag,
                settings={"repo": "owner/docs", "write_back": True},
                load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
            )
        assert code == 0
        assert seen["write_back"] is False
        assert run_main.run.call_args.kwargs["write_back"] is False
        lines = [
            r for r in caplog.records if "write_back is set but not used" in r.message
        ]
        assert len(lines) == 1 and lines[0].levelno == logging.INFO
        assert flag[0] in lines[0].getMessage()

    def test_config_is_set_aside_with_one_info_line_for_a_ref(self, run_main, caplog):
        seen = {}
        with caplog.at_level(logging.INFO):
            code = run_main(
                "--github",
                SLUG,
                "--github-ref",
                "main",
                settings={"repo": "owner/docs", "write_back": True},
                load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
            )
        assert code == 0 and seen["write_back"] is False
        lines = [
            r for r in caplog.records if "write_back is set but not used" in r.message
        ]
        assert len(lines) == 1 and "--github-ref" in lines[0].getMessage()

    def test_a_round_read_by_pr_or_round_still_takes_the_config(self, run_main):
        for argv in (["--pr", "7"], ["--round", "2"], []):
            seen = {}
            run_main(
                "--github",
                SLUG,
                *argv,
                settings={"repo": "owner/docs", "write_back": True},
                load=lambda slug, **kw: seen.update(kw) or fake_handoff(),
            )
            assert seen["write_back"] is True

    def test_no_info_line_when_the_config_does_not_ask(self, run_main, caplog):
        with caplog.at_level(logging.INFO):
            run_main(
                "--github", SLUG, "--offline", load=lambda slug, **kw: fake_handoff()
            )
        assert "write_back is set" not in caplog.text

    def test_the_explicit_off_flag_is_fine_with_a_replay(self, run_main):
        code = run_main(
            "--github",
            SLUG,
            "--no-write-back",
            "--replay",
            "r.json",
            load=lambda slug, **kw: fake_handoff(),
        )
        assert code == 0

    def test_retry_failed_may_write_back(self, run_main):
        code = run_main(
            "--github",
            SLUG,
            "--write-back",
            "--retry-failed",
            "r.json",
            load=lambda slug, **kw: fake_handoff(),
        )
        assert code == 0
        assert run_main.run.call_args.kwargs["write_back"] is True

    def test_nothing_is_looked_up_for_write_back_without_github(self, run_main):
        def fail(*a, **k):
            pytest.fail("write-back code ran without --github")

        with (
            patch("ci_article_review.github_writeback.push_saved_run", fail),
            patch("ci_article_review.github_writeback.write_back_after_run", fail),
            patch("ci_core.github_repo.resolve_token", fail),
            patch(
                "ci_article_review.pipeline._read_handoff_file",
                return_value="# A Draft Title" + "\n" * 2 + "Body.",
            ),
        ):
            assert run_main("--raw-draft", "d.md") == 0
        assert "write_back" not in run_main.run.call_args.kwargs


def _round_fake():
    """The ``ci-docs init`` fake plus the routes a round read adds.

    One open pull request, #1, from ``round/example-article/r2``; ``main`` and
    the round both hold a ``pipeline/run.json``.
    """
    from .test_documents_init import REPO, FakeGitHub, Resp

    class Fake(FakeGitHub):
        pulls = ()

        def _pull(self, number, branch, state="open"):
            return {
                "number": number,
                "state": state,
                "merged_at": None,
                "merge_commit_sha": None,
                "head": {
                    "ref": branch,
                    "sha": self.branches[branch],
                    "repo": {"full_name": REPO},
                },
                "base": {"ref": "main", "sha": self.branches["main"]},
                "title": f"{SLUG} round",
                "html_url": f"https://github.com/{REPO}/pull/{number}",
            }

        def _route(self, method, path, headers, params, body):
            if method == "GET" and path == "/pulls":
                items = [self._pull(n, b) for n, b in self.pulls]
                return Resp(200, items)
            if method == "GET" and path.startswith("/pulls/"):
                number = int(path.rsplit("/", 1)[1])
                for n, b in self.pulls:
                    if n == number:
                        return Resp(200, {**self._pull(n, b), "merged": False})
                return Resp(404, {"message": "Not Found"})
            if method == "GET" and path.startswith("/git/matching-refs/"):
                return Resp(200, [])
            if method == "GET" and path.startswith("/compare/"):
                return Resp(
                    200,
                    {
                        "status": "ahead",
                        "merge_base_commit": {"sha": self.branches["main"]},
                    },
                )
            return super()._route(method, path, headers, params, body)

    fake = Fake()
    fake.external_commit(
        "main", {f"articles/{SLUG}/pipeline/run.json": b'{"run_number": 5}'}, "run 5"
    )
    round_files = {
        f"articles/{SLUG}/draft.md": b"# Example\n\nA draft.\n",
        f"articles/{SLUG}/metadata.md": b"metadata\n",
        f"articles/{SLUG}/pipeline/run.json": b'{"run_number": 3}',
    }
    fake.external_commit(f"round/{SLUG}/r2", round_files, "round 2")
    fake.pulls = [(1, f"round/{SLUG}/r2")]
    return fake, REPO


class TestWriteBackPreflight:
    """Every refusal comes before a model is called, from the real ``load_handoff``."""

    TOKEN = "ghp_exampleSECRETtoken0123456789"

    @pytest.fixture
    def github(self):
        return _round_fake()

    def read(self, run_main, github, *argv, token=TOKEN, settings=None):
        from ci_core.github_repo import GitHubRepo

        fake, repo = github
        seen = {}

        def build(name, tok):
            return GitHubRepo(name, tok, session=fake)

        def spy(*a, **k):
            seen["handoff"] = pipeline_load(*a, token_lookup=lambda: token, **k)
            return seen["handoff"]

        from ci_article_review import github_documents

        pipeline_load = github_documents.load_handoff
        with (
            patch("ci_article_review.github_documents.GitHubRepo", build),
            patch("ci_article_review.pipeline._run_domain", pytest.fail),
        ):
            code = run_main(
                "--github",
                SLUG,
                "--github-repo",
                repo,
                *argv,
                settings=settings or {"repo": repo},
                load=spy,
            )
        return code, seen.get("handoff"), fake

    @pytest.mark.parametrize("argv", [[], ["--write-back"]], ids=["read", "write-back"])
    def test_taken_run_numbers_are_carried_to_the_run_number_bump(
        self, run_main, github, argv
    ):
        code, handoff, _ = self.read(run_main, github, *argv)
        assert code == 0
        # run.json at the round's head (3) and at main (5), with or without
        # --write-back: a run made on another machine is known only there.
        assert handoff["taken_run_numbers"] == [3, 5]
        assert run_main.run.call_args.kwargs["handoff"] is handoff
        source = handoff["document_source"]
        assert (source["ref"], source["round"], source["pull_request"]) == (
            f"round/{SLUG}/r2",
            2,
            1,
        )

    def test_a_read_alone_makes_no_write(self, run_main, github):
        code, _, fake = self.read(run_main, github)
        assert code == 0 and fake.writes == []

    @pytest.mark.parametrize(
        "setup, argv, message",
        [
            (lambda f: setattr(f, "push", False), [], "cannot push"),
            (lambda f: setattr(f, "private", False), [], "public repository"),
            (lambda f: None, ["--round", "9"], "no round 9"),
        ],
        ids=["no-push", "public", "no-such-round"],
    )
    def test_each_refusal_exits_one_before_any_model_call(
        self, run_main, github, caplog, setup, argv, message
    ):
        setup(github[0])
        with caplog.at_level(logging.ERROR):
            code, _, fake = self.read(run_main, github, "--write-back", *argv)
        assert code == 1 and message in caplog.text
        run_main.run.assert_not_called()
        assert fake.writes == []

    def test_a_public_repository_is_fine_when_allowed(self, run_main, github):
        github[0].private = False
        code, _, _ = self.read(
            run_main,
            github,
            "--write-back",
            settings={"repo": github[1], "allow_public_repo": True},
        )
        assert code == 0

    def test_no_token_stops_before_the_repository_is_even_reached(
        self, run_main, github, caplog
    ):
        with caplog.at_level(logging.ERROR):
            code, _, fake = self.read(run_main, github, "--write-back", token=None)
        assert code == 1 and "needs a GitHub token" in caplog.text
        assert fake.requests == []

    def test_a_read_alone_still_works_without_push_permission(self, run_main, github):
        github[0].push = False
        code, _, _ = self.read(run_main, github)
        assert code == 0
