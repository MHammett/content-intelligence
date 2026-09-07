"""Shared fixtures for the ci-article-review suite."""

import hashlib

import pytest

from ci_article_review import live_model_check
from ci_article_review.adapters.citation import resolver, wayback
from ci_article_review.analysis import links


@pytest.fixture(scope="session")
def _model_discovery_cache_dir(tmp_path_factory):
    """One scratch directory per session for ``isolate_model_discovery_cache``."""
    return tmp_path_factory.mktemp("model_discovery_caches")


@pytest.fixture(autouse=True)
def isolate_model_discovery_cache(_model_discovery_cache_dir, request, monkeypatch):
    """Point the model-discovery cache at a scratch path for every test.

    ``live_model_check.CACHE_PATH`` is relative to the working directory, and
    pytest does not chdir — so without this, any test that runs the pipeline
    reads (and ``ci-discover``'s tests write) the developer's real
    ``.cache/model_discovery.json`` in the repo root.

    That is exactly the kind of environment-dependent test this suite has been
    careful to avoid: whether the golden report matches would depend on whether
    whoever ran the suite had happened to run ``ci-discover`` recently, and it
    would pass in CI — which checks out a clean tree — every time.

    What isolates a test here is the *filename*, not a directory of its own.
    This asked for ``tmp_path``, which makes pytest create a fresh directory
    per test — 1,119 of them, 2.6s of the suite's runtime, for a file most of
    those tests never write. A digest of the node id gives the same guarantee
    (unique per test, stable across runs) inside one session directory; and
    ``save_cache`` mkdirs its own parent, so nothing depends on the path
    existing beforehand.
    """
    unique = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:16]
    monkeypatch.setattr(
        live_model_check,
        "CACHE_PATH",
        _model_discovery_cache_dir / f"{unique}.json",
    )


@pytest.fixture(autouse=True)
def neutralise_wayback_pacing(monkeypatch):
    """Take the archive.org pacing clock out of every test's wall-clock cost.

    ``wayback`` paces its calls to archive.org at one every
    ``_MIN_INTERVAL_SECONDS`` (3.0) and backs off from a 429 on a shared clock.
    Both are process-wide by design — ``check()`` runs on a resolver thread
    pool, so per-thread pacing would not pace anything — which means the clock
    also outlives the test that moved it. The cost of that leaked across the
    suite: six tests in ``TestWaybackCheck`` alone spent 27 seconds sitting in
    ``_pace()``, none of them about pacing, several of them waiting out an
    interval a *previous* test's call had started.

    Zeroing the intervals removes no coverage: every test that is about the
    guard already patches these to 0.0 itself, so nothing here ever exercised
    the wait. The wait is now asserted directly, and cheaply, by
    ``TestPacingClock`` in ``test_wayback.py`` — a test that patches the
    interval back up and checks what ``_pace()`` asks to sleep for.

    The state reset on both sides is the same one ``run_draft_pipeline`` does
    per run, for the same reason: a breaker tripped by one test would otherwise
    skip every archive lookup in the next.
    """
    monkeypatch.setattr(wayback, "_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(wayback, "_BACKOFF_BASE_SECONDS", 0.0)
    wayback.reset_rate_limit_state()
    yield
    wayback.reset_rate_limit_state()


@pytest.fixture(autouse=True)
def block_tls_impersonation(monkeypatch):
    """No test reaches the network through the escalation tier unless it says so.

    ``impersonating_get`` is the one fetch in the codebase that is not routed
    through a patched ``safe_get``, so a test that stubs the honest fetch into a
    403 gets a *real* curl_cffi request to whatever URL the fixture named. Two
    of them did: the 403 cases in ``TestKnownUrlWaybackFallback`` called out to
    example.com on every run of the suite, quietly, and passed either way
    because a failed escalation falls through to the archive fallback they were
    actually testing.

    That is the same class of problem ``neutralise_wayback_pacing`` above
    exists for, so it gets the same treatment: off by default, everywhere, with
    the tests that are *about* escalation opting in by patching it themselves.
    A test doing so still wins — ``mock.patch`` sets and restores around this.

    Both import sites are patched, not ``ci_core.http``: each does
    ``from ci_core.http import impersonating_get`` at module load, so rebinding
    the source module would leave the copies they already hold.
    """
    monkeypatch.setattr(resolver, "impersonating_get", lambda url, timeout=30: None)
    monkeypatch.setattr(links, "impersonating_get", lambda url, timeout=30: None)
