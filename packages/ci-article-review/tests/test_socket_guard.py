"""The socket guard covers collection, and every inifile loads what extends it.

pytest_plugins/socket_guard.py says why pytest-socket needs extending. The behavioural tests here run real pytest sessions in a subprocess,
because what they check is the order pytest does things in at startup, which a
test running inside an already-started session cannot observe. Each child loads
only pytest-socket and the guard, so nothing else installed can change the
outcome, and none of them touches the network: see ``DIALS_OUT``.
"""

import os
import shlex
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest


def _find_repo_root():
    """Locate the workspace root by walking up from this file.

    Mirrors test_docs_current.py: these tests read repo-root files, so they must
    not depend on pytest's working directory.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(
        "Could not locate the content-intelligence repo root above "
        f"{Path(__file__).resolve()} — expected an ancestor containing both "
        "packages/ and uv.lock."
    )


REPO_ROOT = _find_repo_root()
PLUGIN = "socket_guard"
PLUGIN_DIR = REPO_ROOT / "pytest_plugins"

# Connects at import time to 0.0.0.0, port 0. That is not an allowed host, so
# the guard blocks it; unguarded, the operating system refuses it at once
# (WSAEADDRNOTAVAIL on Windows, ECONNREFUSED over loopback on Linux) without a
# packet leaving the machine. So these tests can tell "blocked" from "went out"
# without going anywhere, even under --force-enable-socket.
DIALS_OUT = textwrap.dedent(
    """
    import socket

    def _dial():
        with socket.socket() as sock:
            try:
                sock.connect(("0.0.0.0", 0))
            except OSError as exc:
                return type(exc).__name__
        return "connected"

    DIALLED = _dial()
    """
)


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _pytest(project, *args):
    """Run pytest in ``project`` with the guard configured as this repo does."""
    _write(
        project,
        {
            "pytest.ini": textwrap.dedent(
                f"""\
                [pytest]
                pythonpath = {shlex.quote(PLUGIN_DIR.as_posix())}
                addopts = -p pytest_socket -p {PLUGIN} --disable-socket --allow-hosts=127.0.0.1,::1
                """
            )
        },
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Summary lines are cut to the terminal width, and the assertions read them.
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=project,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class TestCollectionIsGuarded:
    def test_a_network_call_during_collection_fails_it(self, tmp_path):
        """A test module imported during collection, and a conftest found
        during it — the shape of the repo-wide run, where each package's
        conftest is only reached once collection walks into that package."""
        _write(
            tmp_path,
            {
                "test_dials_out.py": DIALS_OUT + "\ndef test_never_runs():\n    pass\n",
                "pkg/conftest.py": DIALS_OUT,
                "pkg/test_in_pkg.py": "def test_never_runs():\n    pass\n",
            },
        )
        result = _pytest(tmp_path)

        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.INTERRUPTED, output
        assert "2 errors during collection" in output, output
        blocked = "pytest_socket.SocketConnectBlockedError"
        assert f"ERROR test_dials_out.py - {blocked}" in output, output
        assert f"ERROR pkg - {blocked}" in output, output

    def test_a_conftest_that_dials_out_fails_before_collection_starts(self, tmp_path):
        """The rootdir's conftest is imported before pytest_configure — the
        shape of a single package's run, and the case a hook on
        pytest_collection alone would miss."""
        _write(
            tmp_path,
            {
                "conftest.py": DIALS_OUT,
                "test_nothing.py": "def test_nothing():\n    pass\n",
            },
        )
        result = _pytest(tmp_path)

        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
        assert "ImportError while loading conftest" in result.stderr, output
        assert "SocketConnectBlockedError" in result.stderr, output


class TestTheGuardGetsOutOfTheWay:
    def test_it_is_lifted_when_collection_ends(self, tmp_path):
        """Left in place, it would override the marker: pytest-socket's
        ``enable_socket`` restores ``socket.socket`` but not the ``connect``
        that an allow-list patches. This has to be the session's first test —
        pytest-socket lifts everything after each test, which would hide a
        guard left over from collection from any test after it."""
        _write(
            tmp_path,
            {
                "test_opts_back_in.py": textwrap.dedent(
                    """
                    import socket

                    import pytest

                    @pytest.mark.enable_socket
                    def test_it_can_dial_out():
                        with socket.socket() as sock:
                            with pytest.raises(OSError):
                                sock.connect(("0.0.0.0", 0))
                    """
                )
            },
        )
        result = _pytest(tmp_path)

        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr

    def test_force_enable_socket_lets_import_time_calls_through(self, tmp_path):
        """Both windows honour it: the conftest is imported before the
        command line is fully parsed, the test module after."""
        _write(
            tmp_path,
            {
                "conftest.py": DIALS_OUT.replace("DIALLED", "CONFTEST_DIALLED"),
                "test_dials_out.py": DIALS_OUT
                + textwrap.dedent(
                    """
                    import conftest

                    def test_both_calls_went_out():
                        assert conftest.CONFTEST_DIALLED.endswith("Error")
                        assert DIALLED.endswith("Error")
                    """
                ),
            },
        )
        result = _pytest(tmp_path, "--force-enable-socket")

        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Every inifile has to load it
# ---------------------------------------------------------------------------
#
# pytest reads only the inifile nearest the paths it is given: the root
# pyproject.toml for `pytest packages/`, a package's own for anything inside
# that package. So the guard and the plugin that extends it are repeated in
# each, and a package that leaves them out runs unguarded without a word.


def _pytest_inifiles():
    """Every pyproject.toml in the workspace that configures pytest."""
    candidates = [REPO_ROOT / "pyproject.toml"]
    candidates += sorted(REPO_ROOT.glob("packages/*/pyproject.toml"))
    found = []
    for path in candidates:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        ini = config.get("tool", {}).get("pytest", {}).get("ini_options")
        if ini is not None:
            found.append((path, ini))
    return found


INIFILES = _pytest_inifiles()


def _as_args(value):
    """An ini value that may be one string or a list of them, as pytest allows."""
    return shlex.split(value) if isinstance(value, str) else list(value)


def test_the_root_inifile_is_among_those_checked():
    """So the parametrized test below cannot pass by checking nothing."""
    assert REPO_ROOT / "pyproject.toml" in [path for path, _ in INIFILES]


@pytest.mark.parametrize(
    ("path", "ini"),
    INIFILES,
    ids=[path.relative_to(REPO_ROOT).as_posix() for path, _ in INIFILES],
)
def test_every_inifile_guards_collection(path, ini):
    addopts = _as_args(ini.get("addopts", ""))
    assert "--disable-socket" in addopts, (
        f"{path} does not pass --disable-socket in addopts, so a run that uses "
        "it as the inifile reaches the network freely. Copy the socket options "
        "from the root pyproject.toml."
    )
    assert ["-p", PLUGIN] in [addopts[i : i + 2] for i in range(len(addopts))], (
        f"{path} does not load {PLUGIN} (`-p {PLUGIN}` in addopts), so conftests "
        "and test modules are imported there with the network open."
    )
    pythonpath = [path.parent / entry for entry in _as_args(ini.get("pythonpath", []))]
    assert any((entry / f"{PLUGIN}.py").is_file() for entry in pythonpath), (
        f"No `pythonpath` entry in {path} contains {PLUGIN}.py, so `-p {PLUGIN}` "
        "cannot be imported. pythonpath is relative to the inifile's directory."
    )
