"""ci-core's in-process ``FakeGitHub``, loaded for the round and tagging tests.

``packages/ci-core/tests/test_github_repo.py`` holds the one fake that models
pull requests, merges, reviews, annotated tags, a path's history and the compare
endpoint with the real API's strictness. Test directories are not packages here
(see the repo-root ``conftest.py``), so another package cannot ``import`` it; this
loads that file by path under a name of its own instead of copying 800 lines that
would drift from it. Only the fake and its constants are used; the file's tests
are not collected from here.

Slugs and contents are invented; this repository is public.
"""

import importlib.util
import sys
from pathlib import Path

from ci_core.github_repo import GitHubRepo

_PATH = (
    Path(__file__).resolve().parents[2] / "ci-core" / "tests" / "test_github_repo.py"
)
_NAME = "ci_core_tests_github_repo_fake"


def _load():
    module = sys.modules.get(_NAME)
    if module is None:
        spec = importlib.util.spec_from_file_location(_NAME, _PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_NAME] = module
        spec.loader.exec_module(module)
    return module


_module = _load()
FakeGitHub = _module.FakeGitHub
Resp = _module.Resp
REPO = _module.REPO
TOKEN = _module.TOKEN

SLUG = "example-article"
FOLDER = f"articles/{SLUG}"
SETTINGS = {
    "repo": REPO,
    "branch": None,
    "path_prefix": "articles",
    "allow_public_repo": False,
}


def client(fake):
    return GitHubRepo(REPO, TOKEN, session=fake)


def tip(fake, branch):
    return fake.branches[branch]


def make_round(
    fake,
    gh,
    n,
    *,
    slug=SLUG,
    files=None,
    merged=False,
    runs=(),
    review_commit=None,
):
    """Round ``n`` of ``slug``: its branch cut from ``main``, its pull request, its reviews.

    ``runs`` posts one marked review per run on the branch's tip (or on
    ``review_commit``); ``merged`` merges the pull request with a merge commit.
    Returns the pull request as ``get_pull`` reports it afterwards.
    """
    branch = f"round/{slug}/r{n}"
    fake.branches[branch] = fake.branches["main"]
    head = fake.external_commit(
        branch,
        files or {f"articles/{slug}/draft.md": f"draft of round {n}\n".encode()},
        f"draft: {slug} r{n}",
    )
    pull = gh.create_pull(branch, "main", f"{slug} r{n}", f"round {n}")
    for run in runs:
        gh.create_review(
            pull.number,
            commit_id=review_commit or head,
            body=f"<!-- ci-review run={run} commit={(review_commit or head)[:7]} -->\n"
            f"Run {run} summary.",
        )
    if merged:
        gh.merge_pull(pull.number, sha=head, merge_method="merge")
    return gh.get_pull(pull.number)


def non_get(fake):
    """Every request that could write."""
    return [r for r in fake.requests if r[0] != "GET"]
