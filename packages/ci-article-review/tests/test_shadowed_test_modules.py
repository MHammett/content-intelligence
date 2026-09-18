"""A session that spans packages must not let one test file run another's code.

conftest.py at the repo root enforces that, and its docstring has the history:
while every packages/<pkg>/tests/ was a package named ``tests``, the repo-wide
run silently ran ci-core's test_import.py in place of ci-style-profile's.

That guard acts only in a session holding more than one package's tests, and CI
runs each package on its own, so these tests are the only thing in CI that
exercises it. Each builds a two-package workspace in tmp_path, with the real
conftest.py at its root, and runs pytest over it in a subprocess: the collision
lives in ``sys.modules``, and this session's own modules must stay out of it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_repo_root():
    """Locate the workspace root by walking up from this file.

    Mirrors test_docs_current.py: must not depend on pytest's working directory.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(
        "Could not locate the content-intelligence repo root above "
        f"{Path(__file__).resolve()} — expected an ancestor containing both "
        "packages/ and uv.lock."
    )


GUARD = _find_repo_root() / "conftest.py"


def _run_pytest(workspace, files):
    """Run pytest in importlib mode over the guard plus `files`, like `pytest packages/`."""
    shutil.copy(GUARD, workspace / "conftest.py")
    (workspace / "pytest.ini").write_text(
        "[pytest]\naddopts = --import-mode=importlib -p no:cacheprovider\n"
    )
    for name, text in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # Nothing of this session's configuration reaches the child: no
    # PYTEST_ADDOPTS, no coverage hand-off, no third-party plugins. The guard
    # needs none of them, and without them the child starts in about half a
    # second.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COV_CORE_"))
    }
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "packages", "-v"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


def _test_named(name):
    return f"def {name}():\n    pass\n"


class TestShadowedTestModules:
    def test_same_named_files_in_two_packages_each_run_their_own_code(self, tmp_path):
        # The layout the repo uses: no __init__.py in either tests/.
        code, out = _run_pytest(
            tmp_path,
            {
                "packages/alpha/tests/test_same.py": _test_named("test_in_alpha"),
                "packages/beta/tests/test_same.py": _test_named("test_in_beta"),
            },
        )

        assert code == pytest.ExitCode.OK, out
        # A node id pairs the file pytest collected with a test name taken from
        # the module it actually loaded, so a shadowed beta would show up here
        # as test_same.py::test_in_alpha.
        assert "packages/alpha/tests/test_same.py::test_in_alpha PASSED" in out
        assert "packages/beta/tests/test_same.py::test_in_beta PASSED" in out

    def test_a_file_that_would_run_another_files_code_stops_the_run(self, tmp_path):
        # The layout before 2026-09-17. Without the guard this passes: beta's
        # file is collected, and alpha's test runs under its name.
        code, out = _run_pytest(
            tmp_path,
            {
                "packages/alpha/tests/__init__.py": "",
                "packages/alpha/tests/test_same.py": _test_named("test_in_alpha"),
                "packages/beta/tests/__init__.py": "",
                "packages/beta/tests/test_same.py": _test_named("test_in_beta"),
            },
        )

        assert code == pytest.ExitCode.USAGE_ERROR, out
        assert (
            "packages/beta/tests/test_same.py ran the code of "
            "packages/alpha/tests/test_same.py (module 'tests.test_same')"
        ) in out
        assert "PASSED" not in out

    def test_two_tests_packages_stop_the_run_before_any_name_collides(self, tmp_path):
        # No two test files share a name yet, but a shared helper module
        # (tests/helpers.py in both) would already resolve to one of them.
        code, out = _run_pytest(
            tmp_path,
            {
                "packages/alpha/tests/__init__.py": "",
                "packages/alpha/tests/test_a.py": _test_named("test_in_alpha"),
                "packages/beta/tests/__init__.py": "",
                "packages/beta/tests/test_b.py": _test_named("test_in_beta"),
            },
        )

        assert code == pytest.ExitCode.USAGE_ERROR, out
        assert (
            "'tests' is the module name of 2 directories: "
            "packages/alpha/tests, packages/beta/tests"
        ) in out
        assert "PASSED" not in out
