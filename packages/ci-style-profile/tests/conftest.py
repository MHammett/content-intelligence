"""Shared fixtures for the ci-style-profile suite."""

import hashlib
import os
from pathlib import Path

import pytest

from ci_style_profile import bootstrap, output

#: The installed package's own directory: with an editable install,
#: ``src/ci_style_profile/`` of the checkout the suite runs from. bootstrap
#: keeps its staging corpus and watermarks here and output its profile
#: snapshots, each resolved from its module's ``__file__``.
_PACKAGE_DIR = Path(bootstrap.__file__).resolve().parent


def _tree(root):
    """Every path under ``root`` but bytecode caches, with each file's size and mtime."""
    entries = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        entries.update((os.path.join(dirpath, d), None) for d in dirnames)
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            entries.add((path, (st.st_size, st.st_mtime_ns)))
    return entries


@pytest.fixture(scope="session")
def _package_state_dir(tmp_path_factory):
    """One scratch directory per session for ``isolate_package_state``."""
    return tmp_path_factory.mktemp("package_state")


@pytest.fixture(autouse=True)
def isolate_package_state(_package_state_dir, request, monkeypatch):
    """Send bootstrap's staging and watermarks, and profile snapshots, to scratch.

    All three live in ``_PACKAGE_DIR``, gitignored, so nothing showed when a
    test wrote there. The one test that runs ``bootstrap.main()`` to the end
    left a new snapshot in ``profiles/_output/out/`` on every run, and replaced
    ``staging/.watermarks.json``, the file a real bootstrap reads to decide how
    far back to collect. On 2026-09-18 the main checkout's copy held that
    test's ``{"wordpress": "2024-01-15"}``, so a real run there without
    ``--refresh`` would have fetched WordPress posts from that date on only.

    The per-test path is a digest of the node id, as for ci-article-review's
    model-discovery cache: nothing is created unless a test writes, and no test
    reads another's watermarks. The package directory is compared before and
    after as well, so a test that writes there by any other route fails too.
    """
    before = _tree(_PACKAGE_DIR)
    unique = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:16]
    scratch = _package_state_dir / unique
    monkeypatch.setattr(bootstrap, "_DEFAULT_STAGING_DIR", scratch / "staging")
    monkeypatch.setattr(
        bootstrap, "_WATERMARKS_FILE", scratch / "staging" / ".watermarks.json"
    )
    monkeypatch.setattr(output, "_PROFILES_DIR", scratch / "profiles")
    yield
    changed = sorted({path for path, _ in _tree(_PACKAGE_DIR) ^ before})
    assert not changed, f"test wrote inside the installed package: {changed}"
