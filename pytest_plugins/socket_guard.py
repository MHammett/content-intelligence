"""Extend pytest-socket's network guard to collection, fixture teardown and DNS.

pytest-socket applies its restrictions in ``pytest_runtest_setup`` and lifts
them in ``pytest_runtest_teardown``, so they cover a test's setup and call, and
nothing before the first test. Collection falls outside that window: every
``conftest.py`` and every test module is imported, and the collection hooks
run, with the network open — and a call made there fails nothing.

That has happened here. ci-style-profile's ``test_callers.py`` imports litellm
at module scope, and ``import litellm`` reaches for the network as a side
effect. On a fresh Windows venv, collecting that one module downloaded
tiktoken's ``cl100k_base`` from openaipublic.blob.core.windows.net with the
guard off, while the same import at test time was blocked. The repo-wide run
went green because collection had quietly fetched the file for every test
after it.

This plugin applies the same restrictions, from the same options
(``--disable-socket``, ``--allow-hosts``, ``--allow-unix-socket``), from just
before the first conftest is imported until collection finishes, and then hands
over to pytest-socket's per-test handling. A network call made at import time
now fails as a conftest import error or a collection error, raising
pytest-socket's own exception. There is no test yet to carry
``@pytest.mark.enable_socket``, so ``--force-enable-socket`` is the way through,
as it is for a test; ``-p no:socket_guard`` switches this plugin off alone.

Why ``pytest_load_initial_conftests``: when a package's suite is run on its own,
its conftests are imported before ``pytest_configure`` or ``pytest_collection``
is ever called, so a hook on either would be too late for them. pytest-cov
starts measuring on the same hook for the same reason. pytest does not call it
for conftest files, so this has to be a plugin loaded before any of them —
hence ``-p socket_guard`` in the ``addopts`` of every inifile that enables the
guard, with ``pythonpath`` pointing at this directory. That needs
pytest 8.4, the first release to apply ``pythonpath`` before loading ``-p``
plugins (pytest-dev/pytest#11118). pytest-socket's tracker has no issue or pull
request about collection (checked 2026-09-17), so there was no upstream
approach to adopt.

Fixture teardown falls outside the window too, though pytest-socket lifts its
restrictions in the teardown hook itself: pytest's runner runs the fixture
finalizers from its own ``pytest_runtest_teardown``, after pytest-socket's.
Both are plain hook implementations, which pluggy calls newest-registered
first, and pytest-socket always registers after pytest's built-in runner. So
every yield fixture's teardown and every ``addfinalizer`` callback, at every
scope, ran with the network open, while the same code in the fixture's setup
was blocked (measured 2026-09-18, pytest-socket 0.8.1, pytest 8.4.2 and 9.1.1).

This plugin's ``pytest_runtest_teardown`` wraps both, and holds pytest-socket's
lift back until every finalizer has run — what marking pytest-socket's hook
``trylast`` would do. That one-line fix is miketheman/pytest-socket#537
(UPSTREAM.md entry 7), and this goes when it ships. Holding the lift, rather
than re-applying anything, means
teardown runs under exactly the restrictions pytest-socket chose for the test's
setup — an ``enable_socket`` or ``allow_hosts`` marker, the ``socket_enabled``
or ``socket_disabled`` fixture — with no copy of its rules here to drift from
them, and it works whichever of the two plugins registered first: the repo's
inifiles and this plugin's tests register them in opposite orders. What it
relies on is pytest-socket lifting through ``pytest_socket._remove_restrictions``,
a private name; the teardown tests fail if it stops doing so. Fixtures a run
leaves set up when it stops early — on a teardown error under ``-x`` or
``--maxfail``, say — are torn down later, by pytest's own
``pytest_sessionfinish``, with no test running, so the global restrictions
apply there, as they do during collection.

Name lookups fall outside pytest-socket's guard in every window. Under an
allow-list — how every inifile here runs it, so that loopback stays usable — it
patches ``connect()`` and nothing else, and ``getaddrinfo`` asks the real
resolver.
It blocks lookups only under ``--disable-socket`` with no allow-list (0.8.0,
PR #482, which settled miketheman/pytest-socket#43 for that mode alone). That
failed a run on 2026-09-18: a resolver test reached
``ci_core.http.classify_host``, which looked example.com up for real, and a
network blip left the name unresolved. Measured that day, 23 tests asked the
real resolver about a name, and three of them depended on the answer.

So wherever pytest-socket installs an allow-list, this plugin restricts the
forward lookups — ``getaddrinfo``, ``gethostbyname``, ``gethostbyname_ex`` —
to names that need no nameserver: address literals, which parse locally; the
wildcard, ``None`` or ``""``; ``localhost``, which RFC 6761 reserves to
loopback; and the names on the allow-list itself. Any other name raises
pytest-socket's ``SocketBlockedError``, as it does under ``--disable-socket``.
That is a RuntimeError, not an OSError, so code written to survive a failed
lookup does not absorb it; ``classify_host``, which catches everything,
reads it as "unresolvable" on every run rather than on an unlucky one. And
pytest-socket's errors warn when raised, so the warnings summary names the
host even when the error itself is caught.

The restriction goes on inside ``pytest_socket.socket_allow_hosts`` and comes
off inside ``pytest_socket._remove_restrictions``, both wrapped at import, so
it follows the allow-list exactly: every marker and fixture, collection, the
held teardown and ``pytest_sessionfinish``, with no copy of pytest-socket's
rules. That relies on pytest-socket calling both through its module globals,
as 0.8.1 does; the lookup tests fail if it stops. pytest-socket's tracker has
nothing on lookups under an allow-list (checked 2026-09-18): UPSTREAM.md entry
8 has the change to propose, and this goes when it ships.

Reverse lookups — ``gethostbyaddr``, ``getnameinfo``, ``getfqdn`` — are left
open, as ``--disable-socket`` leaves them. The address is the query there, so
the literal exemption would not hold, and the suite makes one only when
``http.server`` names the loopback address it has bound.

Subprocesses are not covered, deliberately. A guard installed here lives in
this interpreter, and a child starts a fresh one. Reaching into it takes a
startup hook — a ``.pth`` file in the environment or a ``sitecustomize`` on
``PYTHONPATH``, the route pytest-cov took — which runs in every Python process
started while it is in place, not only the ones a test meant to guard, and
still cannot reach a child that is not Python, like the suite's ``git`` calls.
The Python children the suite starts today (2026-09-17) are interpreter-exit
and import-hygiene probes that make no network calls, plus this plugin's own
tests, whose child pytest sessions load the guard themselves; and a child
inherits the environment, so an import-time call switched off by environment
variable stays off in it. Upstream tracks subprocess support as
miketheman/pytest-socket#401, with a ``.pth``-based implementation open as
PR #409; if that ships, upgrading brings it in with no machinery of our own.
Revisit this if a test starts running network-capable code in a child — the
``ci-review`` CLI end to end, say.
"""

from __future__ import annotations

import functools
import ipaddress
import socket
from argparse import Namespace
from collections.abc import Callable, Generator
from typing import Any

import pytest
import pytest_socket

_APPLIED = pytest.StashKey[bool]()


def _apply(config: pytest.Config, options: Namespace) -> None:
    """Restrict sockets the way pytest-socket does for a test with no markers.

    Mirrors the global branch of ``pytest_socket.pytest_runtest_setup``: an
    allow-list restricts ``connect()`` (and, through the wrapper below, name
    lookups), ``--disable-socket`` without one blocks sockets and name
    resolution outright, and ``--force-enable-socket`` overrides both. The
    options are pytest-socket's, so with pytest-socket disabled they are absent
    and this does nothing.
    """
    if config.stash.get(_APPLIED, False) or getattr(
        options, "force_enable_socket", False
    ):
        return
    hosts = getattr(options, "allow_hosts", None)
    allow_unix_socket = getattr(options, "allow_unix_socket", False)
    if hosts:
        pytest_socket.socket_allow_hosts(hosts, allow_unix_socket=allow_unix_socket)
    elif getattr(options, "disable_socket", False):
        pytest_socket.disable_socket(allow_unix_socket=allow_unix_socket)
    else:
        return
    config.stash[_APPLIED] = True


def _lift(config: pytest.Config) -> None:
    if config.stash.get(_APPLIED, False):
        # Private, but it is what pytest-socket's own pytest_runtest_teardown
        # calls: it undoes exactly what the two public functions above install,
        # and keeps doing so if pytest-socket starts patching more.
        pytest_socket._remove_restrictions()
        config.stash[_APPLIED] = False


#: The forward lookups an allow-list leaves open, as they are before anything
#: restricts them. Each is put back only while this plugin's guard is the one in
#: place, the way pytest-socket restores its own.
_LOOKUPS = {
    name: getattr(socket, name)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex")
}

#: The names on the allow-list in force, which a test may still look up.
_allowed_names: frozenset[str] = frozenset()


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _asks_no_nameserver(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        # None is the wildcard. Any other type is the lookup's own to reject.
        return True
    if host == "" or _is_address(host):
        return True
    return host.lower() == "localhost" or host.lower() in _allowed_names


def _guarded(name: str, lookup: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(host: object, *args: Any, **kwargs: Any) -> Any:
        if _asks_no_nameserver(host):
            return lookup(host, *args, **kwargs)
        allowed = ", ".join(["address literals", "localhost", *sorted(_allowed_names)])
        raise pytest_socket.SocketBlockedError(
            f'A test tried to use socket.{name}() with host "{host}" '
            f"(allowed: {allowed})."
        )

    return guarded


_GUARDS = {name: _guarded(name, lookup) for name, lookup in _LOOKUPS.items()}


def _restrict_lookups(allowed: list[str]) -> None:
    global _allowed_names
    # As pytest-socket reads the list: stripped, with networks and addresses
    # set apart from names.
    _allowed_names = frozenset(
        host.strip().lower()
        for host in allowed
        if "/" not in host and not _is_address(host.strip())
    )
    for name, guard in _GUARDS.items():
        # Over the real lookup only. pytest-socket's outright block, from
        # disable_socket, is stricter than this; a test's own stub asks no
        # nameserver either.
        if getattr(socket, name) is _LOOKUPS[name]:
            setattr(socket, name, guard)


def _lift_lookups() -> None:
    for name, guard in _GUARDS.items():
        if getattr(socket, name) is guard:
            setattr(socket, name, _LOOKUPS[name])


_true_socket_allow_hosts = pytest_socket.socket_allow_hosts
_true_remove_restrictions = pytest_socket._remove_restrictions


@functools.wraps(_true_socket_allow_hosts)
def _socket_allow_hosts(
    allowed: str | list[str] | None = None, *args: Any, **kwargs: Any
) -> None:
    _true_socket_allow_hosts(allowed, *args, **kwargs)
    # The same test pytest-socket makes before it patches connect().
    if isinstance(allowed, str):
        allowed = allowed.split(",")
    if isinstance(allowed, list):
        _restrict_lookups(allowed)


@functools.wraps(_true_remove_restrictions)
def _remove_restrictions() -> None:
    _true_remove_restrictions()
    _lift_lookups()


# At import, before any hook runs: the first caller of either is this
# plugin's own pytest_load_initial_conftests. pytest-socket reaches both
# through its module globals, so its setup and teardown hooks call these too.
pytest_socket.socket_allow_hosts = _socket_allow_hosts
pytest_socket._remove_restrictions = _remove_restrictions


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config,
) -> Generator[None, None, None]:
    # Lifted when collection ends. The cleanup is for the runs that never get
    # there: --help, --version, a conftest that failed to import.
    early_config.add_cleanup(lambda: _lift(early_config))
    # The command line is not fully parsed yet. known_args_namespace is what is
    # parsed so far — pytest-socket's options included, since it has already
    # registered them — and is what pytest's own early hooks read here too.
    _apply(early_config, early_config.known_args_namespace)
    return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection(session: pytest.Session) -> Generator[None, object, object]:
    # Normally already applied, since the initial conftests. This covers a run
    # that loaded the plugin too late for that hook.
    _apply(session.config, session.config.option)
    try:
        return (yield)
    finally:
        _lift(session.config)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown() -> Generator[None, object, object]:
    # Innermost of the wrappers, so every plain implementation runs inside this
    # one: pytest-socket's lift, which is noted rather than done, and the
    # runner's finalizers. The lift happens once they have all returned, and
    # only if pytest-socket asked for it: with pytest-socket switched off,
    # nothing here touches the socket module.
    lift = pytest_socket._remove_restrictions
    lift_requested = False

    def hold() -> None:
        nonlocal lift_requested
        lift_requested = True

    pytest_socket._remove_restrictions = hold
    try:
        return (yield)
    finally:
        pytest_socket._remove_restrictions = lift
        if lift_requested:
            lift()


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> Generator[None, object, object]:
    # pytest's own pytest_sessionfinish tears down whatever is still set up:
    # nothing after a full run, whose last test took everything with it, but
    # after an early stop, the fixtures the next test would have shared. No test
    # is running, so the global restrictions apply. Innermost of the wrappers,
    # so the terminal summary is outside it.
    _apply(session.config, session.config.option)
    try:
        return (yield)
    finally:
        _lift(session.config)
