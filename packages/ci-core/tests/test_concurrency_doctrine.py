"""The no-``ThreadPoolExecutor`` rule, enforced instead of merely documented.

:mod:`ci_core.concurrency` has carried a detailed rationale for this since
2026-08-16 — six ``ci-review`` processes found alive, two of them two days after
printing "REVIEW COMPLETE", each holding a file handle on its own log and
thereby breaking ``git worktree remove``. That rationale did not stop three more
``ThreadPoolExecutor`` uses appearing in ci-style-profile and one more in
ci-article-review, because a rule that lives in one module's docstring is only
read by people already editing that module.

So it lives here too, as a test. The allowlist below is the whole escape hatch:
adding an entry is a deliberate act with a written reason next to it, which is
the difference between a considered exception and drift.

Why an AST walk and not a grep: the codebase discusses ``ThreadPoolExecutor`` at
length in comments and docstrings — it has to, to explain why it isn't used.
A grep flags all of that prose. An AST walk sees only code, so the explanation
of the rule can never trip the rule.
"""

import ast
from pathlib import Path

import pytest

#: Repo root, from packages/ci-core/tests/this_file.py
_REPO_ROOT = Path(__file__).resolve().parents[3]

_BANNED_NAMES = frozenset({"ThreadPoolExecutor", "ProcessPoolExecutor"})
_BANNED_MODULE = "concurrent.futures"

#: Paths (repo-relative, forward slashes) permitted to use a pool anyway, each
#: with the reason it is genuinely safe there. A pool whose tasks all carry hard
#: timeouts and cannot outlive the block is a different risk from one fanning
#: out network or model calls — but the difference has to be argued in writing,
#: here, not assumed.
_ALLOWLIST: dict[str, str] = {}


def _iter_source_files():
    """Every shipped .py file in the workspace.

    Only ``packages/*/src`` — test files are deliberately out of scope. Two of
    them (``test_concurrency.py``, ``test_wayback.py``) construct a
    ``ThreadPoolExecutor`` on purpose to prove the hang it causes, which is the
    evidence this rule rests on rather than a violation of it.
    """
    for src in sorted(_REPO_ROOT.glob("packages/*/src")):
        for path in sorted(src.rglob("*.py")):
            yield path


def _violations(path):
    """Executor uses in ``path`` as (lineno, what) pairs. Code only."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # pragma: no cover — a broken file is its own failure
        pytest.fail(f"{path} does not parse: {exc}")

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _BANNED_MODULE or alias.name.startswith(
                    _BANNED_MODULE + "."
                ):
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module == _BANNED_MODULE:
                names = ", ".join(a.name for a in node.names)
                found.append((node.lineno, f"from {_BANNED_MODULE} import {names}"))
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_NAMES:
            found.append((node.lineno, node.attr))
    return found


def test_the_source_tree_uses_no_thread_pool_executor():
    """A pool worker still inside a call at exit holds the interpreter open.

    ``concurrent.futures.thread`` registers ``_python_exit`` via
    ``threading._register_atexit``, and that hook joins every worker with a bare,
    untimed ``t.join()``. Abandoning the future does not help; neither does
    ``shutdown(wait=False)``. The hook is registered by the module, not the
    pool.

    :func:`ci_core.concurrency.run_all_bounded` is the replacement for
    ``ThreadPoolExecutor(max_workers=N)``: every job gets its own daemon thread
    and a semaphore caps how many run at once. A daemon thread cannot hold the
    interpreter open no matter how long its call takes.
    """
    offenders = {}
    for path in _iter_source_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        found = _violations(path)
        if found:
            offenders[rel] = found

    assert not offenders, (
        "concurrent.futures executors are not used in this codebase — see "
        "ci_core.concurrency for the two-day process hang that rule comes from.\n"
        + "\n".join(
            f"  {rel}:{lineno}  {what}"
            for rel, found in sorted(offenders.items())
            for lineno, what in found
        )
        + "\n\nUse ci_core.concurrency.run_all_bounded (the ThreadPoolExecutor("
        "max_workers=N) replacement), run_all_with_timeout, or run_with_timeout.\n"
        "If a pool really is safe here — every task hard-bounded and unable to "
        "outlive the block — add the path to _ALLOWLIST in this file with the "
        "reason, so the exception is argued rather than assumed."
    )


def test_the_allowlist_only_names_files_that_exist():
    """A stale allowlist entry silently re-permits a path that comes back."""
    missing = [rel for rel in _ALLOWLIST if not (_REPO_ROOT / rel).is_file()]
    assert not missing, (
        f"_ALLOWLIST names files that no longer exist: {missing}. "
        "Remove them, or the rule is silently disabled for those paths if "
        "a file is ever recreated at the same path."
    )


def test_the_detector_actually_detects(tmp_path):
    """The guard above is only as good as this.

    An always-empty ``_violations`` would make the rule pass forever, which is
    the failure mode most worth ruling out: it looks exactly like compliance.
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "import concurrent.futures\n"
        "\n"
        "def go():\n"
        "    with ThreadPoolExecutor(max_workers=2) as pool:\n"
        "        pool.submit(print)\n"
        "    concurrent.futures.ProcessPoolExecutor()\n",
        encoding="utf-8",
    )
    found = _violations(sample)
    what = [w for _, w in found]
    assert "from concurrent.futures import ThreadPoolExecutor" in what
    assert "import concurrent.futures" in what
    assert "ThreadPoolExecutor" in what
    assert "ProcessPoolExecutor" in what


def test_prose_about_the_rule_does_not_trip_the_rule():
    """Docstrings and comments must be able to name what they forbid.

    Without this, the fix for a violation would be to stop explaining it.
    """
    sample = ast.parse(
        '"""We do not use ThreadPoolExecutor here; see ci_core.concurrency."""\n'
        "# ThreadPoolExecutor would be joined untimed at exit by\n"
        "# concurrent.futures' atexit hook.\n"
        "X = 'ThreadPoolExecutor'\n"
    )
    found = []
    for node in ast.walk(sample):
        if (
            isinstance(node, (ast.Name, ast.Attribute))
            and getattr(node, "id", getattr(node, "attr", None)) in _BANNED_NAMES
        ):
            found.append(node)
    assert not found


def test_the_shipped_modules_that_fan_out_import_the_shared_helper():
    """The four sites migrated on 2026-09-05, pinned by name.

    Losing the import is how a migration gets quietly reverted: the executor
    test above would still pass if a module simply grew its own bespoke
    ``threading.Thread`` fan-out instead of using the shared helper, and the
    whole point of the exercise was that the mechanism should be shared rather
    than reinvented per package.
    """
    expected = [
        "packages/ci-article-review/src/ci_article_review/analysis/links.py",
        "packages/ci-style-profile/src/ci_style_profile/callers.py",
        "packages/ci-style-profile/src/ci_style_profile/bootstrap.py",
        "packages/ci-style-profile/src/ci_style_profile/collectors/wordpress.py",
    ]
    for rel in expected:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"{rel} moved; update this test"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "ci_core.concurrency"
            for alias in node.names
        }
        assert "run_all_bounded" in imported, (
            f"{rel} no longer imports run_all_bounded from ci_core.concurrency. "
            "If its fan-out was removed entirely that is fine — drop it from "
            "this list. If it grew a bespoke thread fan-out instead, don't."
        )
