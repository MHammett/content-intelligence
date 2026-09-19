"""The socket guard covers collection, teardown and DNS, and every inifile loads it.

pytest_plugins/socket_guard.py says why pytest-socket needs extending. The
behavioural tests here run real pytest sessions in a subprocess, because what
they check is the order pytest does things in — at startup, and around a test's
teardown — which a test running inside an already-started session cannot
observe. Each child loads only pytest-socket and the guard, so nothing else
installed can change the outcome, and none of them touches the network: see
``DIALS_OUT``, ``FAKE_RESOLVER`` and ``RECORDS_DIALS``.
"""

import json
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

# Stands in for the machine's resolver in every child. It is loaded before
# pytest-socket and the guard, so both take its functions for the real ones: a
# lookup is either refused or answered here, and none reaches a nameserver. An
# address literal answers as itself, so a connect by address goes where it
# says; a name answers with 192.0.2.1, a documentation address nothing dials.
FAKE_RESOLVER = "fake_resolver"
FAKE_RESOLVER_SOURCE = textwrap.dedent(
    """
    import ipaddress
    import socket

    def _address(host):
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return "192.0.2.1"

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_address(host), port))]

    def gethostbyname(host):
        return _address(host)

    def gethostbyname_ex(host):
        return host, [], [_address(host)]

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    """
)

# Looks a name up at import time: refused, it raises; let through,
# FAKE_RESOLVER answers it.
LOOKS_UP = textwrap.dedent(
    """
    import socket

    LOOKED_UP = socket.getaddrinfo("dns.example", 443)
    """
)


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _pytest(project, *args, plugins=("pytest_socket", PLUGIN)):
    """Run pytest in ``project`` with the guard configured as this repo does.

    ``plugins`` is the order the two are registered in, which decides the order
    pluggy calls their plain hook implementations in. ``FAKE_RESOLVER`` goes
    ahead of both.
    """
    load = " ".join(f"-p {plugin}" for plugin in (FAKE_RESOLVER, *plugins))
    _write(
        project,
        {
            f"{FAKE_RESOLVER}.py": FAKE_RESOLVER_SOURCE,
            "pytest.ini": textwrap.dedent(
                f"""\
                [pytest]
                pythonpath = {shlex.quote(PLUGIN_DIR.as_posix())} .
                addopts = {load} --disable-socket --allow-hosts=127.0.0.1,::1
                """
            ),
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

    def test_a_name_lookup_during_collection_fails_it(self, tmp_path):
        _write(
            tmp_path,
            {"test_looks_up.py": LOOKS_UP + "\ndef test_never_runs():\n    pass\n"},
        )
        result = _pytest(tmp_path)

        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.INTERRUPTED, output
        assert "1 error during collection" in output, output
        blocked = "pytest_socket.SocketBlockedError"
        assert f"ERROR test_looks_up.py - {blocked}" in output, output
        assert 'socket.getaddrinfo() with host "dns.example"' in output, output


class TestTheGuardGetsOutOfTheWay:
    def test_it_is_lifted_when_collection_ends(self, tmp_path):
        """Left in place, it would override the marker: pytest-socket's
        ``enable_socket`` restores ``socket.socket`` but neither the
        ``connect`` that an allow-list patches nor the lookups this plugin
        restricts with it. This has to be the session's first test —
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
                    def test_it_can_dial_out_and_look_up():
                        with socket.socket() as sock:
                            with pytest.raises(OSError):
                                sock.connect(("0.0.0.0", 0))
                        assert socket.getaddrinfo("dns.example", 443)
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
# Fixture teardown
# ---------------------------------------------------------------------------
#
# The children below record what a connect to each of two hosts ran into:
# 0.0.0.0, which the allow-list blocks, and 127.0.0.1, which it lets through —
# and what looking up a name ran into, which the guard refuses alongside the
# allow-list. So every set of restrictions leaves its own triple — the
# allow-list (BLOCKED, REFUSED, SOCKET_BLOCKED), none at all (REFUSED, REFUSED,
# RESOLVED) — and a teardown can be checked against the restrictions it should
# have run under, not just for being guarded. REFUSED is the OSError the
# operating system answers port 0 with, as for DIALS_OUT: the connect really
# ran, and nothing left the machine. Its subclass differs by platform —
# ConnectionRefusedError on Linux, a bare OSError on Windows — so it is recorded
# as OSError on both. pytest-socket's own errors are RuntimeErrors, never
# OSErrors, so they keep their names. RESOLVED is FAKE_RESOLVER's answer.

BLOCKED = "SocketConnectBlockedError"  # connect() refused by the allow-list
SOCKET_BLOCKED = "SocketBlockedError"  # the socket, or the lookup, refused
REFUSED = "OSError"  # connect() really ran
RESOLVED = "resolved"  # the lookup reached the resolver

RECORDS_DIALS = textwrap.dedent(
    """
    import json
    import socket
    from pathlib import Path

    import pytest

    LOG = Path(__file__).with_name("dials.jsonl")

    def _dial(host):
        try:
            with socket.socket() as sock:
                sock.connect((host, 0))
        except OSError:
            return "OSError"
        except RuntimeError as exc:
            return type(exc).__name__
        return "connected"

    def _look_up(name):
        try:
            socket.getaddrinfo(name, 443)
        except RuntimeError as exc:
            return type(exc).__name__
        return "resolved"

    def record(where):
        with LOG.open("a", encoding="utf-8") as log:
            for host in ("0.0.0.0", "127.0.0.1"):
                log.write(json.dumps([where, host, _dial(host)]) + "\\n")
            name = "dns.example"
            log.write(json.dumps([where, name, _look_up(name)]) + "\\n")

    @pytest.fixture
    def probe(request):
        name = request.node.name
        request.addfinalizer(lambda: record(f"{name} finalizer"))
        yield
        record(f"{name} teardown")
    """
)


def _dials(project):
    """What each recorded place saw: {where: (0.0.0.0's result, 127.0.0.1's,
    the lookup's)}."""
    seen = {}
    for line in (project / "dials.jsonl").read_text(encoding="utf-8").splitlines():
        where, host, result = json.loads(line)
        seen.setdefault(where, {})[host] = result
    return {
        where: (hosts["0.0.0.0"], hosts["127.0.0.1"], hosts["dns.example"])
        for where, hosts in seen.items()
    }


class TestFixtureTeardownIsGuarded:
    @pytest.mark.parametrize(
        "plugins",
        [("pytest_socket", PLUGIN), (PLUGIN, "pytest_socket")],
        ids=["pytest-socket first", "socket_guard first"],
    )
    def test_at_every_scope(self, tmp_path, plugins):
        """Everything pytest-socket's own teardown hook left open, by lifting
        the guard before pytest ran any of it. In both registration orders:
        the repo's inifiles register this plugin first, these tests
        pytest-socket first, and pluggy calls plain hooks newest first."""
        _write(
            tmp_path,
            {
                "conftest.py": RECORDS_DIALS
                + textwrap.dedent(
                    """
                    @pytest.fixture(scope="session")
                    def session_fixture():
                        yield
                        record("session fixture teardown")

                    def pytest_unconfigure(config):
                        record("after the session")
                    """
                ),
                "test_first.py": textwrap.dedent(
                    """
                    import pytest
                    from conftest import record

                    @pytest.fixture(scope="module")
                    def module_fixture():
                        yield
                        record("module fixture teardown")

                    class TestInAClass:
                        @pytest.fixture(scope="class")
                        def class_fixture(self):
                            yield
                            record("class fixture teardown")

                        def test_one(self, session_fixture, module_fixture, class_fixture, probe):
                            record("test_one call")

                    def test_two(module_fixture, probe):
                        pass
                    """
                ),
                "test_second.py": textwrap.dedent(
                    """
                    from conftest import record

                    def teardown_module():
                        record("xunit teardown_module")

                    def test_three(session_fixture, probe):
                        pass
                    """
                ),
            },
        )
        result = _pytest(tmp_path, plugins=plugins)

        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
        guarded = (BLOCKED, REFUSED, SOCKET_BLOCKED)
        assert _dials(tmp_path) == {
            "test_one call": guarded,
            "test_one teardown": guarded,
            "test_one finalizer": guarded,
            "class fixture teardown": guarded,
            "test_two teardown": guarded,
            "test_two finalizer": guarded,
            "module fixture teardown": guarded,
            "test_three teardown": guarded,
            "test_three finalizer": guarded,
            "xunit teardown_module": guarded,
            "session fixture teardown": guarded,
            # And lifted once there is nothing left to tear down.
            "after the session": (REFUSED, REFUSED, RESOLVED),
        }

    def test_under_the_restrictions_its_test_ran_with(self, tmp_path):
        """Held, not re-derived: whatever pytest-socket chose at setup is what
        the test's teardown runs under. ``allow_hosts(["0.0.0.0"])`` inverts
        the allow-list, so a teardown run under the global restrictions instead
        would show. And each lift is checked by the test after it:
        ``enable_socket`` does not undo an allow-list's ``connect()``, and an
        unmarked test's allow-list does not undo ``disable_socket``."""
        _write(
            tmp_path,
            {
                "conftest.py": RECORDS_DIALS,
                "test_markers.py": textwrap.dedent(
                    """
                    import pytest
                    from conftest import record

                    def test_unmarked(probe):
                        record("test_unmarked call")

                    @pytest.mark.enable_socket
                    def test_enable_socket_marker(probe):
                        record("test_enable_socket_marker call")

                    @pytest.mark.allow_hosts(["0.0.0.0"])
                    def test_allow_hosts_marker(probe):
                        record("test_allow_hosts_marker call")

                    def test_socket_enabled_fixture(socket_enabled, probe):
                        record("test_socket_enabled_fixture call")

                    @pytest.mark.disable_socket
                    def test_disable_socket_marker(probe):
                        record("test_disable_socket_marker call")

                    def test_socket_disabled_fixture(socket_disabled, probe):
                        record("test_socket_disabled_fixture call")

                    def test_unmarked_again(probe):
                        record("test_unmarked_again call")
                    """
                ),
            },
        )
        result = _pytest(tmp_path)

        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
        blocked_outright = (SOCKET_BLOCKED,) * 3
        ran_with = {
            "test_unmarked": (BLOCKED, REFUSED, SOCKET_BLOCKED),
            "test_enable_socket_marker": (REFUSED, REFUSED, RESOLVED),
            "test_allow_hosts_marker": (REFUSED, BLOCKED, SOCKET_BLOCKED),
            "test_socket_enabled_fixture": (REFUSED, REFUSED, RESOLVED),
            "test_disable_socket_marker": blocked_outright,
            "test_socket_disabled_fixture": blocked_outright,
            "test_unmarked_again": (BLOCKED, REFUSED, SOCKET_BLOCKED),
        }
        assert _dials(tmp_path) == {
            f"{test} {phase}": seen
            for test, seen in ran_with.items()
            for phase in ("call", "teardown", "finalizer")
        }

    def test_left_over_for_the_end_of_the_session(self, tmp_path):
        """A teardown error under -x stops the run only once that teardown is
        over. By then pytest has kept the module fixture for test_two, and it
        is torn down in pytest's own pytest_sessionfinish, outside any test."""
        _write(
            tmp_path,
            {
                "conftest.py": RECORDS_DIALS,
                "test_stops_early.py": textwrap.dedent(
                    """
                    import pytest
                    from conftest import record

                    @pytest.fixture(scope="module")
                    def module_fixture():
                        yield
                        record("module fixture teardown")

                    @pytest.fixture
                    def fails_at_teardown():
                        yield
                        raise ValueError("a teardown error, which -x stops on")

                    def test_one(module_fixture, fails_at_teardown):
                        pass

                    def test_two(module_fixture):
                        pass
                    """
                ),
            },
        )
        result = _pytest(tmp_path, "-x")

        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
        assert "ERROR at teardown of test_one" in output, output
        assert _dials(tmp_path) == {
            "module fixture teardown": (BLOCKED, REFUSED, SOCKET_BLOCKED)
        }


# ---------------------------------------------------------------------------
# Name lookups
# ---------------------------------------------------------------------------
#
# The windows above already carry a lookup in each record. What is left is what
# the lookup guard lets through, and what it refuses.


class TestNameLookupsAreGuarded:
    def test_only_names_that_need_no_nameserver_resolve(self, tmp_path):
        """Address literals, which parse locally; the wildcard; localhost; and
        the names on the allow-list. Any other name is refused, by each of the
        three forward lookups, with an error that code handling a failed lookup
        does not catch — so a test that needed the answer fails every time,
        not only when the resolver does."""
        _write(
            tmp_path,
            {
                "test_lookups.py": textwrap.dedent(
                    """
                    import re
                    import socket

                    import pytest
                    from pytest_socket import SocketBlockedError

                    @pytest.mark.parametrize(
                        "host",
                        ["127.0.0.1", "10.1.8.25", "::1", None, "", "localhost", "LocalHost"],
                    )
                    def test_let_through(host):
                        assert socket.getaddrinfo(host, 443)
                        if host is not None:
                            assert socket.gethostbyname(host)
                            assert socket.gethostbyname_ex(host)

                    @pytest.mark.parametrize(
                        ("lookup", "args"),
                        [
                            ("getaddrinfo", ("dns.example", 443)),
                            ("gethostbyname", ("dns.example",)),
                            ("gethostbyname_ex", ("dns.example",)),
                            ("getaddrinfo", ("localhost.localdomain", 443)),
                        ],
                    )
                    def test_refused(lookup, args):
                        message = f'socket.{lookup}() with host "{args[0]}"'
                        with pytest.raises(SocketBlockedError, match=re.escape(message)):
                            getattr(socket, lookup)(*args)

                    def test_not_as_a_failed_lookup():
                        with pytest.raises(SocketBlockedError):
                            try:
                                socket.getaddrinfo("dns.example", 443)
                            except OSError:
                                pass

                    @pytest.mark.allow_hosts(["127.0.0.1", "DNS.example"])
                    def test_a_name_on_the_allow_list_resolves():
                        assert socket.getaddrinfo("dns.example", 443)
                        with pytest.raises(SocketBlockedError):
                            socket.getaddrinfo("other.example", 443)
                    """
                )
            },
        )
        result = _pytest(tmp_path)

        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.OK, output
        assert "13 passed" in output, output


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
        "and test modules are imported there, and fixtures torn down, with the "
        "network open."
    )
    pythonpath = [path.parent / entry for entry in _as_args(ini.get("pythonpath", []))]
    assert any((entry / f"{PLUGIN}.py").is_file() for entry in pythonpath), (
        f"No `pythonpath` entry in {path} contains {PLUGIN}.py, so `-p {PLUGIN}` "
        "cannot be imported. pythonpath is relative to the inifile's directory."
    )
