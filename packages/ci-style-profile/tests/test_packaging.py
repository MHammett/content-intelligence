"""A wheel or sdist built from a checkout must not carry its gitignored runtime state.

ci-style-profile keeps its runtime state inside its own package directory:
``staging/`` (the collected writing corpus, which can hold Gmail, Outlook and
Twitter text, plus ``.watermarks.json``), ``profiles/`` (timestamped profile
snapshots) and ``sources.yaml`` (per-source config). The repo-root ``.gitignore``
ignores all three, which is all git needs. Hatchling did not honour it: on
2026-09-19 a wheel built from a working tree contained
``ci_style_profile/staging/.watermarks.json`` and
``ci_style_profile/profiles/_output/out/*.yaml``, and a file planted in
``staging/`` shipped as well, in the sdist too.

Hatchling does find the repo-root file (it walks up from the project directory
to the first ``.gitignore``), but it matches that file's lines against paths
relative to the *project* directory, not the directory the file sits in
(pypa/hatch#304, open). The anchored line
``packages/ci-style-profile/src/ci_style_profile/staging/`` can therefore never
match ``src/ci_style_profile/staging/...``. A line with no leading path
(``.env``, ``*.pem``, ``credentials*.json``) matches at any depth and never
leaked; only the three anchored entries did. The fix is ``exclude`` under
``[tool.hatch.build]`` in the package's ``pyproject.toml``, which hatchling adds
to the patterns the ``.gitignore`` gives it.

These tests run the real build backend, not a model of it, over a scratch tree
laid out like the repo: the repo's own ``.gitignore`` and ``pyproject.toml``, a
stub package, and planted state files. What is planted comes from the
``.gitignore`` itself, so a new anchored entry for this package fails here until
``exclude`` names it too. Nothing is read from or written to the real package
directory: on a machine that has run a bootstrap it holds real writing.
"""

import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest
from hatchling import build as hatchling_build

PACKAGE = "ci-style-profile"
MODULE = "ci_style_profile"

#: How the repo-root .gitignore spells this package's runtime state.
STATE_PREFIX = f"packages/{PACKAGE}/src/{MODULE}/"

#: Files the package really ships, relative to ``src/ci_style_profile/``: a module,
#: a prompt, and the presets bootstrap loads from ``configs/`` (the root .gitignore
#: has an anchored ``configs/*.yaml`` that must not reach it). The build has to keep
#: all three, or "nothing leaked" would also be true of an empty wheel.
SHIPPED = ("__init__.py", "prompts/detect_styles.txt", "configs/presets.yaml")


def _find_repo_root():
    """Locate the workspace root by walking up from this file.

    Same idiom as ci-article-review's test_docs_current.py: the tests read repo
    files, so they must not depend on pytest's working directory.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError(
        "Could not locate the content-intelligence repo root above "
        f"{Path(__file__).resolve()} — expected an ancestor containing both "
        "packages/ and README.md."
    )


REPO_ROOT = _find_repo_root()


def _state_entries():
    """This package's runtime-state entries in the repo .gitignore, relative to the package."""
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return [
        line.strip()[len(STATE_PREFIX) :]
        for line in lines
        if line.strip().startswith(STATE_PREFIX)
    ]


def _paths_under(entry):
    """Files, relative to the package, that one .gitignore entry covers.

    A directory covers everything below it, so plant a dotfile and a file two
    levels down beside the one at the top: ``staging/.watermarks.json`` and
    ``profiles/_output/out/<timestamp>.yaml`` are the real shapes.
    """
    if any(ch in entry for ch in "*?[!\\"):
        pytest.fail(f"{entry!r} is a glob, not a path: teach _paths_under to plant it")
    if not entry.endswith("/"):
        return [entry]
    return [entry + rel for rel in ("sentinel.txt", ".sentinel", "a/b/sentinel.txt")]


def _write(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SENTINEL: synthetic file, not real data\n", encoding="utf-8")


def _scratch_repo(tmp_path):
    """Lay the repo's packaging inputs, and planted state, out under ``tmp_path``.

    Returns the project directory and the planted paths, relative to the package.
    The real ``.gitignore`` goes at the scratch root, where hatchling finds it by
    walking up from the project. Every top-level file of the real project
    directory comes too: ``pyproject.toml`` is the point, and the rest keeps a
    future ``readme =`` key from breaking the build. The package is a stub
    because the real directory is not ours to read.
    """
    entries = _state_entries()
    assert entries, (
        f"no line in the repo .gitignore starts with {STATE_PREFIX!r}, so nothing "
        "would be planted. If the runtime state moved out of the package directory, "
        "delete this test and the excludes in pyproject.toml; if the package was "
        "renamed, update STATE_PREFIX."
    )
    project = tmp_path / "packages" / PACKAGE
    src = project / "src" / MODULE
    src.mkdir(parents=True)
    shutil.copy(REPO_ROOT / ".gitignore", tmp_path / ".gitignore")
    for path in (REPO_ROOT / "packages" / PACKAGE).iterdir():
        if path.is_file():
            shutil.copy(path, project / path.name)

    planted = [p for entry in entries for p in _paths_under(entry)]
    for rel in (*SHIPPED, *planted):
        _write(src / rel)
    return project, planted


def _build(kind, project, out, monkeypatch):
    """Build ``project`` with the real backend; return what shipped, relative to the package."""
    monkeypatch.chdir(project)  # hatchling's PEP 517 hooks build the working directory
    out.mkdir()
    if kind == "wheel":
        with zipfile.ZipFile(out / hatchling_build.build_wheel(str(out))) as z:
            names = z.namelist()
        prefix = f"{MODULE}/"
    else:
        with tarfile.open(out / hatchling_build.build_sdist(str(out))) as t:
            # Members sit under a "<name>-<version>/" directory.
            names = [n.split("/", 1)[1] for n in t.getnames() if "/" in n]
        prefix = f"src/{MODULE}/"
    return {n[len(prefix) :] for n in names if n.startswith(prefix)}


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_build_leaves_out_gitignored_runtime_state(kind, tmp_path, monkeypatch):
    project, planted = _scratch_repo(tmp_path)

    shipped = _build(kind, project, tmp_path / "dist", monkeypatch)

    leaked = sorted(set(planted) & shipped)
    assert not leaked, (
        f"the {kind} contains gitignored runtime state: {leaked}. Hatchling matches "
        "the repo-root .gitignore against paths relative to the project directory, "
        "so an anchored entry never matches; add it to `exclude` under "
        f"[tool.hatch.build] in packages/{PACKAGE}/pyproject.toml."
    )
    missing = sorted(set(SHIPPED) - shipped)
    assert not missing, f"the {kind} dropped files the package needs: {missing}"
