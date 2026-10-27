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
    "files": [{"path": f"articles/{SLUG}/{SLUG}-draft-r3.md", "blob_sha": "b" * 40}],
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
            "--github-ref",
            "drafts",
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
            "drafts",
            "owner/scratch",
        )
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
