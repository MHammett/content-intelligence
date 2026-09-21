"""A private draft has to stay out of the repo wherever it lands, in git and in a build.

``.gitignore`` keeps the ``dc-environment*`` article drafts and their handoffs out
of the repo. Until 2026-09-20 it said so as ``handoff_templates/dc-environment*.md``,
which had two flaws that nothing showed:

* A line with a path in it is anchored to the repo root, and the directory it
  named moved: the monorepo restructure of 2026-06-24 (3ae33d4), a day after the
  line was written, put the handoff templates under
  ``packages/ci-article-review/src/ci_article_review/handoff_templates/``. A draft
  saved beside them was not ignored, so it showed up untracked, one ``git add -A``
  from a commit, and was packaged into the wheel and the sdist. The real drafts sit
  in the repo-root ``handoff_templates/`` of a checkout, which the line still
  matched, so the drift stayed invisible.
* Even a corrected full path is only right for git. Hatchling finds the repo-root
  ``.gitignore`` but matches its lines against paths relative to the *package*
  directory (pypa/hatch#304, open), so ``packages/ci-article-review/src/...`` never
  matches ``src/...`` and the file ships anyway. Measured 2026-09-20 with
  hatchling 1.32.4's own build: with that line, all three planted drafts shipped, in
  the wheel and in the sdist.

A line with no leading path means the same thing to both readers, and at any
depth. That is the fix, and what this test holds it to: each draft name has to be
ignored in the repo-root ``handoff_templates/`` (git), in every packaged
``handoff_templates/`` and the ``examples/`` under it (git), and in those packaged
paths as hatchling sees them, relative to their package.

The reader answering is git, not a model of it. Each check asks ``git check-ignore
--no-index`` in a scratch repository that holds a copy of the real ``.gitignore``,
so git's root is whatever directory the frame calls the root. Nothing in the real
tree is read or written and no path has to exist: ``check-ignore`` matches names.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _find_repo_root():
    """Locate the workspace root by walking up from this file.

    Same idiom as test_docs_current.py: the tests read repo files, so they must
    not depend on pytest's working directory.
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

#: Where the real drafts still sit in a checkout: the repo-root directory the
#: templates lived in before they moved into the package.
LEGACY_DIR = "handoff_templates"

#: What a draft and its handoff are called. The real ones are dc-environment-v19
#: to v26; the names here are only shaped like them.
DRAFT_NAMES = ("dc-environment-v26.md", "dc-environment-v26-handoff.md")

#: A name the rule must leave alone: a shipped template. Swallowing those would
#: drop them from the wheel without a word.
TEMPLATE_NAME = "draft_submission.template.md"


def _packaged_dirs():
    """Every packaged handoff_templates/ directory, as a path from the repo root."""
    found = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.glob("packages/*/src/*/handoff_templates")
    )
    if not found:
        pytest.fail(
            "no packages/*/src/*/handoff_templates/ directory found. If the "
            "templates moved again, point _packaged_dirs at the new home; if they "
            "are gone, this test and the dc-environment*.md line in .gitignore "
            "have nothing left to guard."
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
    root = tmp_path_factory.mktemp("gitignore-frame")
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


@pytest.mark.parametrize("name", DRAFT_NAMES)
def test_a_private_draft_is_ignored_wherever_it_lands(scratch_repo, name):
    cases = [("git", f"{LEGACY_DIR}/{name}")]
    for directory in _packaged_dirs():
        package_side = _from_package(directory)
        cases += [
            ("git", f"{directory}/{name}"),
            ("git", f"{directory}/examples/{name}"),
            ("hatchling", f"{package_side}/{name}"),
            ("hatchling", f"{package_side}/examples/{name}"),
        ]

    escaped = [
        f"{reader}: {path}"
        for reader, path in cases
        if not _ignored(scratch_repo, path)
    ]

    assert not escaped, (
        f"a draft named {name} is not ignored at: {escaped}. Git anchors a line "
        "that has a path in it to the repo root, and hatchling matches it "
        "relative to the package directory (pypa/hatch#304), so only a line with "
        "no leading path, like dc-environment*.md, holds for both."
    )


def test_the_draft_rule_leaves_the_shipped_templates_alone(scratch_repo):
    swallowed = []
    for directory in _packaged_dirs():
        for path in (
            f"{directory}/{TEMPLATE_NAME}",
            f"{_from_package(directory)}/{TEMPLATE_NAME}",
        ):
            if _ignored(scratch_repo, path):
                swallowed.append(path)

    assert not swallowed, (
        f"the .gitignore ignores a shipped template: {swallowed}. Git would show it "
        "as untracked-then-ignored, and a build would drop it from the wheel."
    )
