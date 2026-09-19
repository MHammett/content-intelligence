"""Shared fixtures for the ci-article-review suite."""

import contextlib
import functools
import hashlib
import ssl
from pathlib import Path

import pytest
import requests
import urllib3

import spn_client.client as _spn_client_engine

from ci_article_review import (
    history,
    history_analytics,
    live_model_check,
    pipeline,
    reproducibility,
)
from ci_article_review.adapters.citation import resolver, wayback
from ci_article_review.analysis import links

#: The ``pipeline_history/`` a real run would write to. ``HISTORY_ROOT`` is
#: relative to the working directory, which for this suite is wherever pytest
#: was started: the repo root, for ``make test`` and for CI. Resolved at import,
#: before any fixture replaces ``HISTORY_ROOT`` or a test changes directory.
_CWD_HISTORY = Path(pipeline.HISTORY_ROOT).resolve()

#: Every name a history reader is bound to, as ``(module, name)``.
#: ``reproducibility`` imports ``_existing_run_dir`` by value, so patching
#: ``history``'s alone would leave its lookups unwatched.
#: ``test_cwd_history_guard.py`` fails if a third module does the same.
_HISTORY_READERS = (
    (history, "_existing_run_dir"),
    (reproducibility, "_existing_run_dir"),
    (history_analytics, "iter_reports"),
)


def _in_cwd_history(root):
    """Whether ``root`` is the working directory's ``pipeline_history/``, or inside it.

    Inside counts: a ``--replay`` run reads ``pipeline_history/_replay/``.
    """
    try:
        return Path(root).resolve().is_relative_to(_CWD_HISTORY)
    except (OSError, ValueError):
        return False


def _watch(reader, reads):
    """``reader``, noting each call whose first argument is in the cwd's history."""

    @functools.wraps(reader)
    def watched(history_root, *args, **kwargs):
        if _in_cwd_history(history_root):
            reads.append(f"{reader.__name__}({str(history_root)!r})")
        return reader(history_root, *args, **kwargs)

    return watched


@pytest.fixture(autouse=True)
def cwd_history_reads(monkeypatch):
    """The lookups this test makes in the working directory's ``pipeline_history/``.

    ``cwd_history_untouched`` fails the test if there are any. Every history
    reader takes the directory to search as its first argument, so a lookup in
    the working directory is a call whose argument resolves there.

    That is not visible from outside. The readers ``stat`` the directory first
    and stop if it is missing, which it is in CI, so nothing is opened, listed
    or created for a file-system hook to see, and the directory's existence
    proves nothing either. Measured 2026-09-19: nine tests looked in it, and a
    PEP 578 audit hook on open, listdir and scandir reported none of them.
    """
    reads = []
    for module, name in _HISTORY_READERS:
        monkeypatch.setattr(module, name, _watch(getattr(module, name), reads))
    return reads


@pytest.fixture(autouse=True)
def cwd_history_untouched(cwd_history_reads):
    """Fail any test that creates or reads ``pipeline_history/`` in the working directory.

    Creating: this checks only whether the directory exists. Where it already
    does, as in a checkout that has run a review, a live run in that checkout
    can write to it while the suite runs, so its contents cannot be blamed on a
    test. CI starts without one, so there this fails every test that creates it.

    If the test left the directory empty, it is removed again. The tests that
    follow are then still checked, rather than passing because it is already
    there.

    Reading: ``run_draft_pipeline`` asks history for the article's earlier runs
    early on, so where the directory has that article's slug, a test that calls
    it takes a different path from the one CI does. A prior run there renumbers
    this one, becomes its delta baseline and is measured against for
    reproducibility. Nothing fails when that happens, which is why this check
    exists.
    """
    existed = _CWD_HISTORY.exists()
    yield
    created = not existed and _CWD_HISTORY.exists()
    if created:
        with contextlib.suppress(OSError):
            _CWD_HISTORY.rmdir()
    problems = []
    if created:
        problems.append(
            f"created {_CWD_HISTORY}; pipeline.main() does that unless the "
            "test uses the tmp_history_root fixture"
        )
    if cwd_history_reads:
        problems.append(
            f"looked for history in {_CWD_HISTORY}: "
            f"{', '.join(sorted(set(cwd_history_reads)))}. "
            "run_draft_pipeline does that unless the test uses the "
            "tmp_history_root fixture; any other reader needs a root under "
            "tmp_path"
        )
    assert not problems, "test " + "; and ".join(problems)


@pytest.fixture
def tmp_history_root(tmp_path, monkeypatch):
    """Point ``pipeline.HISTORY_ROOT`` into ``tmp_path``.

    For every test that calls ``main()``, which creates ``HISTORY_ROOT`` before
    it opens its log file there. Those tests patch ``logging.FileHandler``,
    which stops the file but not the ``mkdir``, so each one that got that far
    left an empty ``pipeline_history/`` in the working directory: eight of
    them, measured from a checkout that had none.

    That includes the tests that expect ``main()`` to stop at argument
    parsing. Whether an invocation reaches the ``mkdir`` depends on the order
    of ``main()``, which is not what those tests are about.

    And for every test that calls ``run_draft_pipeline`` itself, which looks in
    ``HISTORY_ROOT`` for the article's earlier runs, and saves its report there
    unless ``save_run`` is stubbed. Nine of them looked, measured from a
    checkout that had none of their slugs.
    """
    root = tmp_path / "pipeline_history"
    monkeypatch.setattr(pipeline, "HISTORY_ROOT", str(root))
    return root


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

    ``_MIN_INTERVAL_SECONDS``/``_BACKOFF_BASE_SECONDS`` live in ``spn_client``'s
    engine now, not in this package's ``wayback`` module — that module only
    re-exports the public functions/constants, not these private pacing knobs
    — so they're patched on ``spn_client.client`` directly. ``reset_rate_limit_state``
    is still called through ``wayback`` (it's one of the re-exported names) since
    it's the same process-wide state either way.
    """
    monkeypatch.setattr(_spn_client_engine, "_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(_spn_client_engine, "_BACKOFF_BASE_SECONDS", 0.0)
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


@pytest.fixture
def certificate_failure():
    """Build what ``requests`` raises when a server's certificate fails verification.

    The shape is the real one, as a loopback handshake produces it (see
    ``TestRealHandshakes`` in ``test_wayback.py``): ``requests``' ``SSLError``
    wraps urllib3's ``MaxRetryError``, whose ``reason`` wraps the
    ``ssl.SSLCertVerificationError``. The outer exception is also a
    ``requests.exceptions.ConnectionError``, which is how a certificate failure
    came to be reported as an unreachable origin. ``verify_message`` is set the
    way ``ssl`` and truststore set it; it is the part a report quotes.

    Nothing here opens a socket — constructing a connection pool does not
    connect.
    """

    def make(reason="unable to get local issuer certificate", host="www.ntia.gov"):
        err = ssl.SSLCertVerificationError(
            1,
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            f"{reason} (_ssl.c:1032)",
        )
        err.verify_message = reason
        return requests.exceptions.SSLError(
            urllib3.exceptions.MaxRetryError(
                urllib3.HTTPSConnectionPool(host, 443),
                "/page",
                reason=urllib3.exceptions.SSLError(err),
            )
        )

    return make
