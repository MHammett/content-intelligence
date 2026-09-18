"""Refuse a test session in which one test file runs another file's code.

pytest reads this file only when the root ``pyproject.toml`` is the inifile —
in practice the repo-wide ``uv run pytest packages/`` that ``make test`` runs,
the one session that collects more than one package's tests.

That session runs under ``--import-mode=importlib``, which names a test module
after the package it sits in. Until 2026-09-17 every ``packages/<pkg>/tests/``
had an ``__init__.py``, so all three were a package named ``tests``, and
ci-style-profile's ``tests/test_import.py`` became ``tests.test_import`` — a
name ci-core's file of the same name had already put in ``sys.modules``. pytest
returns a cached module by name without checking which file it came from
(``import_path`` in ``_pytest/pathlib.py``: pytest 8.4, unchanged on main), so
ci-style-profile's test ran ci-core's code. Same test name, same count, nothing
on screen. A second ``tests/conftest.py`` broke the run outright, with
``ValueError: Plugin already registered under a different name``, for the same
reason.

The test directories are not packages any more. Without an ``__init__.py``,
importlib mode names a module after its path from the rootdir
(``packages.ci-core.tests.test_import``), which no other file can share — the
case pytest's docs mean by "test module names do not need to be unique". Each
package's own pytest config uses importlib mode too, so relative imports
between test modules resolve the same way in both invocations.

This hook fails the session if that regresses, on either of two conditions:

* a collected test file whose module object was loaded from a different file;
* one top-level module name standing for more than one directory, such as two
  ``tests`` packages. That is the precondition, checked on its own because it
  also shadows modules pytest never collects: two ``tests/helpers.py`` would
  quietly share one module even if no two test files had the same name.

A single-package run needs neither check. It collects one package's tests under
that package's own rootdir, so there is no second tree to collide with.
"""

import os
from pathlib import Path

import pytest


def _shown(path, root):
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # tryfirst: see every collected module before -k/-m deselect any of them.
    root = config.rootpath
    problems = []
    homes = {}
    checked = set()
    for item in items:
        node = item.getparent(pytest.Module)
        if node is None or node.path in checked:
            continue
        checked.add(node.path)
        module = node.obj
        if getattr(module, "__file__", None) is None:
            continue
        loaded = Path(module.__file__)
        if not _same_file(node.path, loaded):
            problems.append(
                f"{_shown(node.path, root)} ran the code of {_shown(loaded, root)}"
                f" (module {module.__name__!r})"
            )
        # The directory the top-level name stands for: .../tests for
        # "tests.test_x", .../packages for "packages.ci-core.tests.test_x".
        parts = module.__name__.split(".")
        depth = len(parts) - 1
        home = loaded.parents[depth - 1] if 0 < depth <= len(loaded.parents) else loaded
        homes.setdefault(parts[0], set()).add(home)
    for name, dirs in sorted(homes.items()):
        if len(dirs) > 1:
            listed = ", ".join(sorted(_shown(d, root) for d in dirs))
            problems.append(
                f"{name!r} is the module name of {len(dirs)} directories: {listed}"
            )
    if problems:
        raise pytest.UsageError(
            "test modules shadow each other, so a test can run another file's code:\n  "
            + "\n  ".join(problems)
            + "\nMost likely an __init__.py in a test directory; see conftest.py at the"
            " repo root."
        )
