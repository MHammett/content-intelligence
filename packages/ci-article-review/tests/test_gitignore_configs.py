"""A user's own ``configs/`` stays out of the repo; the rule stays out of the packages.

``ci-setup`` scaffolds ``configs/`` in the working directory and fills it with
real API keys (``configs/user.yaml``) and a publication profile
(``configs/<name>.yaml``). Nothing under it is tracked — ``git ls-files configs/``
is empty — so the whole directory is ignored, as ``/configs/``.

Two things were wrong with the rule this replaced, and this file holds both:

* ``configs/*.yaml`` reached only the top level of the directory, because ``*``
  does not cross a slash. A second copy of a filled-in config one level down —
  ``configs/backup/user.yaml``, the kind of thing a dated backup leaves behind —
  was not ignored, and was one ``git add -A`` from a commit.

* Seven ``!configs/<name>`` negations sat above it, from when ``pricing.yaml``,
  ``presets.yaml``, ``timeouts.yaml``, ``model_registry.yaml`` and the two
  examples lived in a repo-root ``configs/`` and had to be un-ignored to be
  tracked. The monorepo restructure of 2026-06-24 (3ae33d4) moved them under
  ``packages/*/src/*/configs/``, which an anchored line never reaches, so every
  one of them named a path that nothing in the repo creates: ``ci-setup`` and
  ``docs/CONFIGURATION.md`` both copy an example *and rename it*
  (``user.example.yaml`` to ``configs/user.yaml``). ``!configs/examples/`` did
  not even un-ignore anything, for the same reason ``configs/*.yaml`` missed the
  backup: it negated a pattern that had never matched.

The second test is the one that has to keep passing. Dropping the leading slash
— writing ``configs/`` for ``/configs/``, which looks like the same rule — makes
the line unanchored, and it then matches a directory of that name at any depth.
That is the ``dc-environment*.md`` lesson (see ``test_gitignore_private_drafts``)
running the other way: git would ignore every packaged config, and hatchling,
which matches the repo-root file's lines relative to each *package* directory
(pypa/hatch#304, open), would drop them from the wheel and the sdist. Measured
2026-09-30 by running hatchling's own build over a scratch tree: a bare
``configs/`` empties ``configs/`` in all three wheels and all three sdists, and
git then ignores all 12 tracked files under ``packages/*/src/*/configs/``.

The reader answering is git, not a model of it — same frame as
``test_gitignore_private_drafts.py``. Each check asks ``git check-ignore
--no-index`` in a scratch repository holding a copy of the real ``.gitignore``,
so git's root is whatever directory the frame calls the root. That is what lets
one file ask both questions: a repo-root path for git, and the same path
relative to ``packages/<pkg>`` for hatchling. Nothing in the real tree is read or
written and no path has to exist — ``check-ignore`` matches names.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _find_repo_root():
    """Locate the workspace root by walking up from this file.

    Same idiom as test_gitignore_private_drafts.py: the tests read repo files, so
    they must not depend on pytest's working directory.
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

#: What lands in a user's configs/, and what a careless copy leaves beside it.
#: The first two are what ci-setup writes. The rest are names the seven removed
#: negations used to un-ignore: they are not secret, but they are not something
#: this repo wants committed either, and configs/ is not where they live.
PRIVATE_CONFIGS = (
    "configs/user.yaml",
    "configs/mikehammett.yaml",
    "configs/backup/user.yaml",
    "configs/old/mikehammett.yaml",
    "configs/user.example.yaml",
    "configs/publication.example.yaml",
    "configs/pricing.yaml",
    "configs/model_registry.yaml",
    "configs/presets.yaml",
    "configs/timeouts.yaml",
    "configs/examples/local_news.yaml",
)


def _packaged_configs():
    """Every file under a packaged ``configs/``, as a path from the repo root."""
    found = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.glob("packages/*/src/*/configs/**/*")
        if path.is_file()
    )
    if not found:
        pytest.fail(
            "no files found under packages/*/src/*/configs/. If the packaged "
            "configs moved again, point _packaged_configs at their new home; if "
            "they are gone, the anchoring this test guards protects nothing."
        )
    return found


def _from_package(path):
    """``packages/<pkg>/src/...`` as hatchling sees it: relative to ``packages/<pkg>``."""
    return path.split("/", 2)[2]


def _git(root, *args):
    # Without the caller's GIT_* variables: inside a git hook GIT_DIR and
    # GIT_INDEX_FILE are set, and would point these commands at the real repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def scratch_repo(tmp_path_factory):
    """An empty repository holding a copy of the real ``.gitignore``, and nothing else."""
    root = tmp_path_factory.mktemp("gitignore-configs-frame")
    shutil.copy(REPO_ROOT / ".gitignore", root / ".gitignore")
    init = _git(root, "init", "--quiet")
    assert init.returncode == 0, init.stderr
    return root


def _ignored(root, path):
    """Whether git, with ``root`` as the repository root, would ignore ``path``."""
    # core.excludesFile points at nothing, so a machine-wide ignore file cannot
    # make a path look ignored by *this* .gitignore.
    result = _git(
        root,
        "-c",
        f"core.excludesFile={root / 'no-excludes'}",
        "check-ignore",
        "--no-index",
        "--quiet",
        "--",
        path,
    )
    # 0: ignored, 1: not ignored. Anything else is git failing, not an answer.
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("path", PRIVATE_CONFIGS)
def test_a_users_config_is_ignored_wherever_it_lands(scratch_repo, path):
    assert _ignored(scratch_repo, path), (
        f"{path} is not ignored. A user's configs/ holds real API keys and "
        "nothing in it is tracked, so the whole directory is ignored as "
        "/configs/. A pattern that names files instead, like configs/*.yaml, "
        "reaches only the top level — * does not cross a slash."
    )


def test_the_configs_rule_never_reaches_a_packaged_configs_directory(scratch_repo):
    swallowed = []
    for path in _packaged_configs():
        for reader, candidate in (("git", path), ("hatchling", _from_package(path))):
            if _ignored(scratch_repo, candidate):
                swallowed.append(f"{reader}: {candidate}")

    assert not swallowed, (
        f"the .gitignore ignores a packaged config: {swallowed}. /configs/ has a "
        "leading slash so it is anchored to the repo root and cannot match at "
        "depth; a bare configs/ matches a directory of that name anywhere, which "
        "drops every packaged config from the wheel and the sdist, because "
        "hatchling matches the repo-root .gitignore relative to each package "
        "directory (pypa/hatch#304). Restore the leading slash."
    )
