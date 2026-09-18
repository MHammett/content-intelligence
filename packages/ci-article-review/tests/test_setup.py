"""First-run scaffolding — the very first command a new user runs.

Audit finding 17. ``setup.py`` had 0% coverage, which is how finding 9 (a bash
line-continuation in a printed command, on a Windows-first tool) survived two
dedicated documentation-cleanup PRs. The inverse of where testing effort had
gone: ``resolver.py`` at 95% and the entire first-run experience at zero.

Everything here runs against ``tmp_path``, which stands in for both the working
directory and the repo root, with ``uv sync`` skipped or stubbed; nothing
touches the network or the checkout the suite runs from. ``workdir`` says why
the repo root has to be redirected as well as the working directory.
"""

import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ci_article_review import setup as setup_mod

#: The checkout ``main()`` would write ``.env`` into if a test let it:
#: ``_repo_root()`` walks up from setup.py's own file, whatever the working
#: directory. Resolved at import, before any fixture replaces it.
_REAL_REPO_ROOT = setup_mod._repo_root()


def _stat(path):
    """``path``'s size and mtime, or None if it does not exist."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


@pytest.fixture(autouse=True)
def checkout_env_untouched():
    """Fail any test here that creates, replaces or deletes the checkout's ``.env``."""
    env = _REAL_REPO_ROOT / ".env"
    before = _stat(env)
    yield
    assert _stat(env) == before, (
        f"a setup test changed {env}; everything here must write under tmp_path"
    )


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run setup as if tmp_path were the user's working directory and repo root.

    ``main()`` writes to both. ``configs/`` and ``handoff_templates/`` go to the
    working directory; ``.env`` goes to ``_repo_root()``, which is also where
    ``uv sync`` runs. ``_repo_root()`` walks up from setup.py's own file, so
    when this fixture moved only the working directory, it still named the real
    checkout, and every run of this file copied ``.env.example`` to ``.env`` in
    any checkout that lacked one. That placeholder shadowed the real keys
    ``find_dotenv()`` would otherwise have found further up, and every model
    call then failed authentication in under five seconds, for $0.00.

    ``.env.example`` is copied in because a real repo root always has one.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_mod, "_repo_root", lambda: tmp_path)
    shutil.copyfile(_REAL_REPO_ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def _run(workdir, publication="myblog", sync=False):
    argv = ["ci-setup", "--publication", publication]
    if not sync:
        argv.append("--skip-sync")
    with patch.object(setup_mod, "_check_uv", return_value=True):
        with patch("sys.argv", argv):
            setup_mod.main()


class TestScaffolding:
    def test_creates_configs_and_both_yaml_files(self, workdir):
        _run(workdir)
        assert (workdir / "configs" / "user.yaml").is_file()
        assert (workdir / "configs" / "myblog.yaml").is_file()

    def test_copies_the_fillable_template_into_the_working_tree(self, workdir):
        """The packaged copy lives in site-packages, which is not editable.

        The README used to name a path the user could neither find nor change.
        """
        _run(workdir)
        templates = workdir / "handoff_templates"
        assert (templates / "draft_submission.template.md").is_file()
        assert (templates / "metadata_only.md").is_file()

    def test_ships_the_worked_example_alongside_the_template(self, workdir):
        _run(workdir)
        example = workdir / "handoff_templates" / "draft_submission.filled-example.md"
        assert example.is_file()
        assert "DRAFT SUBMISSION HANDOFF" in example.read_text(encoding="utf-8")

    def test_the_template_is_a_template_not_someone_elses_article(self, workdir):
        """Finding 8 — the file the quick start named was a finished article."""
        _run(workdir)
        text = (
            workdir / "handoff_templates" / "draft_submission.template.md"
        ).read_text(encoding="utf-8")
        assert "mikehammett" not in text.lower()
        assert "[Your article title" in text
        # Still parseable as a handoff: the headers the parser needs are present.
        from ci_article_review.handoff_parser import DRAFT_HEADERS

        for header in DRAFT_HEADERS:
            assert header in text, f"template is missing the {header!r} section"

    def test_existing_files_are_never_clobbered(self, workdir):
        """Re-running setup must not overwrite a filled-in config."""
        configs = workdir / "configs"
        configs.mkdir()
        (configs / "user.yaml").write_text("my: real config", encoding="utf-8")
        _run(workdir)
        assert (configs / "user.yaml").read_text(encoding="utf-8") == "my: real config"

    def test_a_second_run_is_idempotent(self, workdir):
        _run(workdir)
        before = (workdir / "handoff_templates" / "metadata_only.md").read_text(
            encoding="utf-8"
        )
        _run(workdir)
        after = (workdir / "handoff_templates" / "metadata_only.md").read_text(
            encoding="utf-8"
        )
        assert before == after


class TestRepoRoot:
    """What ``main()`` does at the repo root, which ``workdir`` puts in tmp_path."""

    def test_env_is_copied_from_the_example(self, workdir):
        _run(workdir)
        env = (workdir / ".env").read_bytes()
        assert env == (workdir / ".env.example").read_bytes()

    def test_an_existing_env_is_never_clobbered(self, workdir):
        (workdir / ".env").write_text("OPENAI_API_KEY=a-real-key\n", encoding="utf-8")
        _run(workdir)
        env = (workdir / ".env").read_text(encoding="utf-8")
        assert env == "OPENAI_API_KEY=a-real-key\n"

    def test_uv_sync_runs_in_the_repo_root(self, workdir, monkeypatch):
        """Stubbed: for real, it would sync the checkout the suite runs from.

        The invalid-name test did exactly that, on every run, as the one call to
        ``main()`` here without ``--skip-sync``.
        """
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs.get("cwd")))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(setup_mod, "subprocess", SimpleNamespace(run=run))
        _run(workdir, sync=True)
        assert calls == [(["uv", "sync"], workdir)]


class TestPublicationNameValidation:
    @pytest.mark.parametrize("name", ["myblog", "dna-com", "tech_review", "a1"])
    def test_valid_names_accepted(self, name):
        assert setup_mod._validate_publication_name(name) is True

    @pytest.mark.parametrize(
        "name", ["My Blog", "-leading", "", "UPPER", "has space", "ünicode"]
    )
    def test_invalid_names_rejected(self, name):
        assert setup_mod._validate_publication_name(name) is False

    def test_an_invalid_name_exits_rather_than_scaffolding_junk(self, workdir, capsys):
        with pytest.raises(SystemExit) as exited:
            _run(workdir, publication="My Blog")
        # Exited at the name check, not before it: a failed ``uv sync`` exits 1
        # too, and would have passed this test for the wrong reason.
        assert exited.value.code == 1
        assert "'My Blog' is not a valid publication name" in capsys.readouterr().out
        assert not (workdir / "configs" / "My Blog.yaml").exists()


class TestPrintedNextSteps:
    """Finding 9 — these strings are the newcomer's first instruction.

    ``test_docs_current.py`` guards the *form* of printed commands
    mechanically. This checks they are actually present and name the real
    console scripts, which a regex over source cannot confirm.
    """

    def _output(self, workdir, capsys):
        _run(workdir)
        return capsys.readouterr().out

    def test_next_steps_name_the_console_scripts(self, workdir, capsys):
        out = self._output(workdir, capsys)
        assert "uv run ci-check --publication myblog" in out
        assert "uv run ci-review" in out

    def test_no_command_is_split_across_lines(self, workdir, capsys):
        """A trailing backslash breaks the command on Windows cmd.exe."""
        out = self._output(workdir, capsys)
        for line in out.splitlines():
            assert not line.rstrip().endswith("\\"), (
                f"printed command uses a bash line continuation: {line!r}"
            )

    def test_the_run_command_points_at_a_file_setup_actually_created(
        self, workdir, capsys
    ):
        """The printed --draft path must exist after setup finishes."""
        out = self._output(workdir, capsys)
        line = next(ln for ln in out.splitlines() if "ci-review" in ln)
        draft_arg = line.split("--draft", 1)[1].split()[0]
        assert (workdir / draft_arg).is_file(), (
            f"setup tells the user to run --draft {draft_arg}, which it did not create"
        )
