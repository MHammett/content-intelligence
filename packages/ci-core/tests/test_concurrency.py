"""Tests for ci_core.concurrency.run_with_timeout.

The important one here is `test_an_abandoned_call_does_not_hold_the_process_open`,
and it has to spawn a subprocess: the defect it guards is about *interpreter
exit*, which nothing running inside the interpreter can observe. Every
in-process assertion passed against the broken version — the pipeline ran, the
report was written, the summary printed, and only the shell never came back.
"""

import subprocess
import sys
import textwrap
import threading
import time

import pytest

from ci_core.concurrency import (
    batch_ceiling,
    run_all_bounded,
    run_all_with_timeout,
    run_with_timeout,
)


class TestContract:
    def test_returns_the_value(self):
        assert run_with_timeout(lambda: 21 * 2, timeout=5) == 42

    def test_reraises_what_the_call_raised(self):
        """Callers must see provider failures exactly as if called inline."""

        def _boom():
            raise ValueError("provider said no")

        with pytest.raises(ValueError, match="provider said no"):
            run_with_timeout(_boom, timeout=5)

    def test_raises_timeout_error_on_expiry(self):
        with pytest.raises(TimeoutError, match="Timed out after"):
            run_with_timeout(lambda: time.sleep(5), timeout=0.1)

    def test_expiry_returns_promptly_rather_than_waiting_out_the_call(self):
        """The point of a backstop: stop waiting now, not when the call ends."""
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            run_with_timeout(lambda: time.sleep(10), timeout=0.2)
        assert time.monotonic() - t0 < 3

    def test_a_none_timeout_waits_for_the_call(self):
        """compute_all can hand back None for a model with no budget."""
        assert run_with_timeout(lambda: "done", timeout=None) == "done"

    def test_the_worker_is_a_daemon(self):
        """The whole fix in one assertion."""
        seen = {}

        def _capture():
            seen["daemon"] = threading.current_thread().daemon

        run_with_timeout(_capture, timeout=5)
        assert seen["daemon"] is True


def _run_exit_probe(body, abandoned_seconds):
    """Run ``body`` in a fresh interpreter and time how long it takes to exit.

    Shared by the process-exit classes below. The measurement only means
    anything in a subprocess: what is being tested is interpreter *exit*, which
    nothing running inside the interpreter can observe.

    ``{abandoned}`` in ``body`` is filled with ``abandoned_seconds``.
    """
    script = textwrap.dedent(body).format(abandoned=abandoned_seconds)
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=abandoned_seconds + 30,
    )
    return proc, time.monotonic() - t0


class TestProcessExit:
    """The regression that cost two days of a process sitting on a log file."""

    # Longer than the assert below, so a process that waits for the abandoned
    # call cannot pass by finishing it.
    #
    # Sized from measurement, not guesswork. The two exit times these tests
    # separate do not scale together:
    #
    #   * the fixed (daemon-thread) form exits in ~0.57s — interpreter start
    #     plus importing ci_core — *independently* of how long the abandoned
    #     call still has to run;
    #   * the broken (executor) form exits at ~abandoned + 0.1s, because the
    #     atexit join waits the call out.
    #
    # So the only job of ABANDONED_SECONDS is to put daylight between those
    # two, and MUST_EXIT_WITHIN is the line drawn between them. At 5s/3s the
    # margin is >5x on both sides (0.57s vs the 3s line, 3s vs 5.1s), which is
    # as decisive as the 20s/10s this used to spend and 15s cheaper per run.
    ABANDONED_SECONDS = 5
    MUST_EXIT_WITHIN = 3

    def _run(self, body):
        return _run_exit_probe(body, self.ABANDONED_SECONDS)

    def test_an_abandoned_call_does_not_hold_the_process_open(self):
        """A timed-out call keeps running; it must not keep the interpreter alive.

        Against the single-worker-executor version this took exactly as long as
        the abandoned call, because concurrent.futures' atexit hook joins worker
        threads with a bare `t.join()` and no timeout. In production that meant
        `ci-review` processes alive two days after writing their report.
        """
        proc, elapsed = self._run(
            """
            import time
            from ci_core.concurrency import run_with_timeout

            try:
                run_with_timeout(lambda: time.sleep({abandoned}), timeout=0.5)
            except TimeoutError:
                print("backstop fired")
            print("exiting")
            """
        )

        assert proc.returncode == 0, proc.stderr
        assert "backstop fired" in proc.stdout
        assert "exiting" in proc.stdout
        assert elapsed < self.MUST_EXIT_WITHIN, (
            f"process took {elapsed:.1f}s to exit with a {self.ABANDONED_SECONDS}s "
            f"abandoned call still running — the abandoned worker is holding the "
            f"interpreter open, which is the bug this guards"
        )

    @pytest.mark.slow
    def test_the_executor_form_really_did_hang(self):
        """Pins the diagnosis, so the fix cannot be argued away later.

        If this ever stops hanging, CPython changed its atexit behaviour and the
        comment in ci_core.concurrency explaining the fix needs revisiting.

        Marked ``slow`` because proving a hang means waiting one out: this is
        the only test in the suite whose cost is irreducible rather than
        accidental. It still runs by default; ``-m "not slow"`` deselects it for
        a fast inner loop.
        """
        proc, elapsed = self._run(
            """
            import concurrent.futures, time

            inner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            fut = inner.submit(lambda: time.sleep({abandoned}))
            try:
                fut.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                print("backstop fired")
            finally:
                inner.shutdown(wait=False, cancel_futures=True)
            print("exiting")
            """
        )

        assert "exiting" in proc.stdout
        assert elapsed >= self.ABANDONED_SECONDS - 2, (
            f"the executor form exited in {elapsed:.1f}s — it used to be held "
            f"open for the full {self.ABANDONED_SECONDS}s by the atexit join"
        )


class TestRunAllWithTimeout:
    """The N-way sibling of run_with_timeout, for pipeline.py's parallel
    review-call fan-out and any other caller that used to hand a whole batch
    to a ``with ThreadPoolExecutor() as executor:`` block."""

    def test_returns_every_value_on_success(self):
        jobs = [("a", lambda: 1, 5), ("b", lambda: 2, 5)]
        results = run_all_with_timeout(jobs, global_timeout=5)
        assert results == {"a": (1, None), "b": (2, None)}

    def test_reraises_what_the_call_raised(self):
        def _boom():
            raise ValueError("provider said no")

        results = run_all_with_timeout([("a", _boom, 5)], global_timeout=5)
        value, error = results["a"]
        assert value is None
        assert isinstance(error, ValueError)
        assert "provider said no" in str(error)

    def test_own_timeout_fires_before_the_global_one(self):
        results = run_all_with_timeout(
            [("slow", lambda: time.sleep(5), 0.2)], global_timeout=10
        )
        value, error = results["slow"]
        assert value is None
        assert isinstance(error, TimeoutError)
        assert "Timed out after 0.2s" in str(error)

    def test_global_timeout_fires_before_an_individual_ones(self):
        """A call whose own budget hasn't run out yet still gets cut off once
        the group deadline arrives — the same shape as pipeline.py's global
        ceiling cancelling calls that are individually still within budget."""
        results = run_all_with_timeout(
            [("slow", lambda: time.sleep(5), 30)], global_timeout=0.2
        )
        value, error = results["slow"]
        assert value is None
        assert isinstance(error, TimeoutError)
        assert "Exceeded global timeout of 0.2s" in str(error)

    def test_a_none_own_timeout_still_respects_the_global_one(self):
        """A model with no calibrated per-call budget must not be able to
        stall the whole batch forever — the group deadline is the backstop."""
        results = run_all_with_timeout(
            [("uncapped", lambda: time.sleep(5), None)], global_timeout=0.2
        )
        value, error = results["uncapped"]
        assert value is None
        assert isinstance(error, TimeoutError)
        assert "Exceeded global timeout of 0.2s" in str(error)

    def test_one_slow_job_does_not_hold_up_the_others(self):
        """The whole point: fast jobs are not blocked waiting on a slow one."""
        finished_order = []

        def _slow():
            time.sleep(2)
            finished_order.append("slow")
            return "slow"

        def _fast():
            finished_order.append("fast")
            return "fast"

        started = time.monotonic()
        results = run_all_with_timeout(
            [("slow", _slow, 0.2), ("fast", _fast, 5)], global_timeout=5
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1.5
        assert results["fast"] == ("fast", None)
        assert isinstance(results["slow"][1], TimeoutError)
        assert finished_order == ["fast"]

    def test_a_stragglers_budget_is_not_re_spent_when_the_loop_reaches_it(self):
        """A budget runs from when its own call started, not from when the
        sequential join loop happens to arrive at that call.

        Every test above puts the tight-budget job *first*, where the loop
        reaches it immediately and the distinction cannot show. Production is
        the other shape: the model with the smallest budget sits behind several
        slower ones, so by the time the loop looks at it, its budget expired
        long ago — and it was handed a fresh full one anyway. Measured
        2026-09-03 on a maximum-preset run: four grok calls with a 120s budget
        ran 143s, 233s, 310s and 378s, every one recorded OK, each landing
        within a second of *(the moment its join began) + 120*.

        Five hung calls behind one slow one. Their budgets all expired while
        the first was still being waited on, so each must be abandoned the
        moment the loop looks at it. Re-spending them serially costs
        1.0 + 5 x 0.2s instead.
        """
        jobs = [("slow", lambda: time.sleep(1.0), 5.0)] + [
            (f"hung{i}", lambda: time.sleep(30), 0.2) for i in range(5)
        ]

        started = time.monotonic()
        results = run_all_with_timeout(jobs, global_timeout=20)
        elapsed = time.monotonic() - started

        assert results["slow"][1] is None
        for i in range(5):
            assert isinstance(results[f"hung{i}"][1], TimeoutError)
        assert elapsed < 1.5, (
            f"the group took {elapsed:.2f}s — each straggler was granted a "
            f"fresh budget instead of one measured from its own start"
        )

    def test_a_call_that_overran_but_finished_is_still_a_result(self):
        """The budget bounds how long this function *waits*, not whether an
        answer that already arrived is usable.

        The counterpart to the test above, and the reason the fix cannot simply
        fail anything that outlived its budget: a call the loop was too busy to
        collect on time has still done the work and been paid for. Discarding
        it would throw away findings the run already bought.
        """

        def _late():
            time.sleep(0.3)
            return "late answer"

        results = run_all_with_timeout(
            [("slow", lambda: time.sleep(0.6), 5.0), ("late", _late, 0.05)],
            global_timeout=20,
        )

        assert results["late"] == ("late answer", None)

    def test_jobs_actually_run_concurrently(self):
        """All four jobs must be in flight at once, not run in a sequential loop.

        A barrier rather than a stopwatch. Every job has to arrive before any of
        them is released, so a sequential implementation cannot satisfy this at
        any speed: the first job would sit waiting for three callers that will
        not run until it returns, and the barrier would break. The previous
        form slept 1s in each of four jobs and asserted the total stayed under
        2s — the same claim, argued from wall-clock ratio, for 1s a run.

        `run_all_with_timeout` starts one daemon thread per job with no pool
        bound (see its implementation), so a four-party barrier is safe here.
        """
        gate = threading.Barrier(4, timeout=5)
        jobs = [(str(i), gate.wait, 5) for i in range(4)]

        results = run_all_with_timeout(jobs, global_timeout=5)

        errors = {name: err for name, (_, err) in results.items() if err is not None}
        assert not errors, (
            f"Jobs did not all reach the barrier together, so they were not "
            f"running concurrently: {errors}"
        )
        # Each waiter gets a distinct arrival index, so this also shows all four
        # really passed through rather than one running four times.
        assert sorted(value for value, _ in results.values()) == [0, 1, 2, 3]


class TestRunAllWithTimeoutProcessExit:
    """Same regression as TestProcessExit, for the N-way form: an abandoned
    job in the group must not hold the interpreter open either."""

    ABANDONED_SECONDS = 20
    MUST_EXIT_WITHIN = 10

    def test_an_abandoned_job_does_not_hold_the_process_open(self):
        script = textwrap.dedent(
            """
            import time
            from ci_core.concurrency import run_all_with_timeout

            results = run_all_with_timeout(
                [("stuck", lambda: time.sleep({abandoned}), 0.5)],
                global_timeout=0.5,
            )
            print("backstop fired" if results["stuck"][1] else "no backstop")
            print("exiting")
            """
        ).format(abandoned=self.ABANDONED_SECONDS)

        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=self.ABANDONED_SECONDS + 30,
        )
        elapsed = time.monotonic() - t0

        assert proc.returncode == 0, proc.stderr
        assert "backstop fired" in proc.stdout
        assert "exiting" in proc.stdout
        assert elapsed < self.MUST_EXIT_WITHIN, (
            f"process took {elapsed:.1f}s to exit with a {self.ABANDONED_SECONDS}s "
            f"abandoned job still running in the group"
        )


class TestBatchCeiling:
    """The wave arithmetic that sizes a bounded batch's group deadline.

    Lived as a private ``_batch_ceiling`` in ci-article-review's citation
    resolver until three more call sites in two other packages needed it —
    the same drift, one level down, as a rule about executors living only in
    one module's docstring.
    """

    def test_one_wave_when_everything_fits(self):
        assert batch_ceiling(3, 8, 90, slack=60) == 150

    def test_counts_waves_at_the_parallel_bound(self):
        """10 jobs, 8 at a time, is two waves — not one."""
        assert batch_ceiling(10, 8, 90, slack=60) == 240

    def test_an_exact_multiple_does_not_round_up_a_spare_wave(self):
        assert batch_ceiling(16, 8, 90, slack=60) == 240

    def test_no_jobs_is_just_slack(self):
        assert batch_ceiling(0, 8, 90, slack=60) == 60

    def test_refuses_to_size_a_batch_with_no_per_call_budget(self):
        """Silently returning something would re-create an unbounded group.

        A group deadline is the only backstop a job with no budget of its own
        has; guessing one here would make that guarantee fictional.
        """
        with pytest.raises(ValueError, match="per-call timeout"):
            batch_ceiling(4, 2, None)


class TestRunAllBounded:
    """The ``ThreadPoolExecutor(max_workers=N)`` replacement.

    ``max_workers`` caps how many threads *exist*; this caps how many are
    *working*. That is the whole difference, and it is what makes the
    replacement safe: a thread parked on a semaphore is a daemon that cannot
    outlive the interpreter, where a pool worker parked inside a call is
    joined untimed by the atexit hook ``concurrent.futures`` registers.
    """

    def test_returns_every_value(self):
        jobs = [(str(i), (lambda i=i: i * 2), 5) for i in range(5)]
        results = run_all_bounded(jobs, max_parallel=2)
        assert results == {str(i): (i * 2, None) for i in range(5)}

    def test_no_jobs_is_not_an_error(self):
        assert run_all_bounded([], max_parallel=4) == {}

    def _peak_concurrency(self, job_count, max_parallel, hold=0.12):
        live = 0
        peak = 0
        lock = threading.Lock()

        def _work():
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(hold)
            with lock:
                live -= 1

        jobs = [(str(i), _work, 30) for i in range(job_count)]
        run_all_bounded(jobs, max_parallel=max_parallel)
        return peak

    def test_never_exceeds_max_parallel(self):
        """The bound the semaphore exists to provide.

        Without it the wayback submitter would burst every citation at
        archive.org at once, which is the IP block its low bound avoids.
        """
        assert self._peak_concurrency(12, max_parallel=3) == 3

    def test_zero_means_all_at_once(self):
        """The shape a caller used to write as max_workers=len(jobs)."""
        assert self._peak_concurrency(6, max_parallel=0) == 6

    def test_none_means_all_at_once(self):
        assert self._peak_concurrency(6, max_parallel=None) == 6

    def test_a_bound_above_the_job_count_is_clamped(self):
        assert self._peak_concurrency(4, max_parallel=99) == 4

    def test_a_jobs_own_timeout_still_fires(self):
        results = run_all_bounded(
            [("slow", lambda: time.sleep(10), 0.2)], max_parallel=1
        )
        value, error = results["slow"]
        assert value is None
        assert isinstance(error, TimeoutError)

    def test_reraises_what_a_call_raised(self):
        def _boom():
            raise ValueError("provider said no")

        value, error = run_all_bounded([("a", _boom, 5)], max_parallel=1)["a"]
        assert value is None
        assert isinstance(error, ValueError)

    def test_one_slow_job_does_not_fail_its_neighbours(self):
        jobs = [
            ("fast", lambda: "ok", 5),
            ("slow", lambda: time.sleep(10), 0.2),
        ]
        results = run_all_bounded(jobs, max_parallel=2)
        assert results["fast"] == ("ok", None)
        assert isinstance(results["slow"][1], TimeoutError)

    def test_a_job_with_no_budget_falls_back_to_the_group_deadline(self):
        """A None per-job budget is reachable and must not mean "forever".

        ``timeout_model.compute_all`` omits a model whose ``enabled`` is
        falsy-but-not-False, and ``enabled:`` written with no value at all
        parses as None in YAML.
        """
        results = run_all_bounded(
            [("nobudget", lambda: time.sleep(10), None)],
            max_parallel=1,
            global_timeout=0.3,
        )
        value, error = results["nobudget"]
        assert value is None
        assert "global timeout" in str(error).lower()

    def test_refuses_an_unsizeable_batch_rather_than_running_it_unbounded(self):
        with pytest.raises(ValueError, match="per-call timeout"):
            run_all_bounded([("a", lambda: None, None)], max_parallel=1)


class TestBudgetStartsOnAcquire:
    """A queued job must not be charged for the waves that ran ahead of it.

    :func:`run_all_with_timeout` stamps each job's deadline when its thread is
    created. That is right for an ungated fan-out, where every thread starts
    work immediately, and it is what commit 58c0aa5 fixed — four grok calls ran
    143-378s against a 120s budget and each recorded ``OK``, because the old
    join loop handed out durations and restarted every straggler's budget from
    wherever the loop had reached.

    Put a semaphore between thread creation and the work, though, and that same
    stamp becomes wrong in the other direction: a job in wave four has spent its
    whole budget queueing and is reported as a timeout the moment it is joined,
    having done nothing. So :func:`run_all_bounded` applies the budget *inside*
    the semaphore and hands the layer below only the group ceiling.

    resolver.py has three batches shaped exactly like this; the archive-match
    one is 4 wide on a 30s budget, which is ten waves for forty targets.
    """

    # Every job finishes in WORK, comfortably inside PER_CALL. But the batch as
    # a whole takes (JOBS / MAX_PARALLEL) * WORK = 5s, which is longer than any
    # single job's budget — the condition that exposes the bug.
    JOBS = 40
    MAX_PARALLEL = 8
    PER_CALL = 3.0
    WORK = 1.0

    def _batch(self):
        def work(i):
            def run():
                time.sleep(self.WORK)
                return i

            return run

        return [(str(i), work(i), self.PER_CALL) for i in range(self.JOBS)]

    def test_no_job_times_out_when_every_job_is_inside_its_budget(self):
        results = run_all_bounded(self._batch(), max_parallel=self.MAX_PARALLEL)

        spurious = sorted(int(k) for k, v in results.items() if v[1] is not None)
        assert not spurious, (
            f"{len(spurious)} of {self.JOBS} jobs timed out despite each doing "
            f"{self.WORK}s of work inside a {self.PER_CALL}s budget: {spurious[:12]}. "
            f"The budget is being charged from thread creation rather than from "
            f"semaphore acquire, so everything past wave "
            f"{int(self.PER_CALL // self.WORK)} is billed for queueing."
        )

    def test_the_naive_form_really_does_fail(self):
        """Pins the diagnosis, so the fix cannot be argued away later.

        This is what ``run_all_bounded`` would do if it simply forwarded each
        job's timeout to ``run_all_with_timeout`` and let that layer stamp the
        deadlines — i.e. the shape three hand-rolled semaphore batches in
        resolver.py had before they moved onto the shared helper.

        If this ever stops failing, ``run_all_with_timeout`` has changed when it
        stamps deadlines and the extra machinery in ``run_all_bounded`` may no
        longer be earning its keep.
        """
        semaphore = threading.Semaphore(self.MAX_PARALLEL)

        def gated(fn):
            def run():
                with semaphore:
                    return fn()

            return run

        jobs = [(name, gated(fn), timeout) for name, fn, timeout in self._batch()]
        results = run_all_with_timeout(jobs, global_timeout=60)

        spurious = [k for k, v in results.items() if v[1] is not None]
        assert spurious, (
            "the naive semaphore-outside-the-budget form no longer produces "
            "spurious timeouts — re-check whether run_all_bounded still needs "
            "to apply budgets itself"
        )

    def test_a_genuine_overrun_is_still_caught(self):
        """The fix must not blunt the backstop it exists to protect.

        Moving the stamp later would be worthless if it also stopped a truly
        wedged call from being abandoned.
        """
        jobs = [
            ("fine", lambda: "ok", 5),
            ("wedged", lambda: time.sleep(30), 0.3),
        ]
        results = run_all_bounded(jobs, max_parallel=2)

        assert results["fine"] == ("ok", None)
        value, error = results["wedged"]
        assert value is None
        assert isinstance(error, TimeoutError)
        assert "0.3" in str(error)

    def test_the_bound_still_holds_while_budgets_are_applied_inside_it(self):
        """Applying the timeout inside the semaphore must not widen the gate."""
        live = 0
        peak = 0
        lock = threading.Lock()

        def work():
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1

        run_all_bounded([(str(i), work, 30) for i in range(20)], max_parallel=4)
        assert peak == 4


class TestBoundedProcessExit:
    """``run_all_bounded`` must inherit the daemon-thread exit guarantee.

    The bug this module exists for is not about correctness of results — every
    in-process assertion passed against the broken version. It is about whether
    the process can *exit*. A new fan-out helper that got results right and exit
    wrong would look entirely healthy in every other test here.

    Deliberately not a subclass of :class:`TestProcessExit`, despite wanting its
    constants: pytest collects inherited tests too, so subclassing re-ran the
    whole parent class under a second name — including the ``slow`` proof that
    waits out a real hang, doubling its cost for no added coverage.
    """

    ABANDONED_SECONDS = TestProcessExit.ABANDONED_SECONDS
    MUST_EXIT_WITHIN = TestProcessExit.MUST_EXIT_WITHIN

    def _run(self, body):
        return _run_exit_probe(body, self.ABANDONED_SECONDS)

    def test_an_abandoned_bounded_job_does_not_hold_the_process_open(self):
        proc, elapsed = self._run(
            """
            import time
            from ci_core.concurrency import run_all_bounded

            results = run_all_bounded(
                [("stuck", lambda: time.sleep({abandoned}), 0.5)],
                max_parallel=1,
            )
            print("backstop fired" if results["stuck"][1] else "no backstop")
            print("exiting")
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert "backstop fired" in proc.stdout
        assert "exiting" in proc.stdout
        assert elapsed < self.MUST_EXIT_WITHIN, (
            f"process took {elapsed:.1f}s to exit with a {self.ABANDONED_SECONDS}s "
            f"abandoned bounded job still running"
        )

    def test_a_job_still_queued_on_the_semaphore_does_not_hold_it_open_either(self):
        """The case a pool cannot match.

        With max_parallel=1 and two jobs, the second is still parked on the
        semaphore when the group deadline fires — it has not started its work
        and never will. A ``ThreadPoolExecutor`` in the same state holds a
        worker thread that atexit joins untimed; a parked daemon thread just
        goes with the interpreter.
        """
        proc, elapsed = self._run(
            """
            import time
            from ci_core.concurrency import run_all_bounded

            results = run_all_bounded(
                [
                    ("first", lambda: time.sleep({abandoned}), 0.5),
                    ("queued", lambda: time.sleep({abandoned}), 0.5),
                ],
                max_parallel=1,
                global_timeout=1,
            )
            print("both stopped" if all(v[1] for v in results.values()) else "no")
            print("exiting")
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert "both stopped" in proc.stdout
        assert "exiting" in proc.stdout
        assert elapsed < self.MUST_EXIT_WITHIN, (
            f"process took {elapsed:.1f}s to exit with a job parked on the "
            f"semaphore and one abandoned mid-call"
        )


class TestForeignThreadPoolsDoNotHoldTheExitOpen:
    """The hang that started the 2026-09-03 audit, from the other direction.

    Everything else here keeps *our* abandoned work off the shutdown path. It
    cannot help with a pool this codebase never creates, and litellm creates one
    per streaming call for its post-call success logging. Those workers are not
    daemons, so `threading._shutdown()` joins them.

    Measured: a `ci-review` run printed its whole summary, wrote every output
    file, then sat at 0% CPU with 19 threads — 18 blocked in `accept()` inside
    `socket.socketpair()`, asyncio's Windows self-pipe, which had stopped
    returning. The work was done; the process could not leave.
    """

    STUCK_SECONDS = 30
    MUST_EXIT_WITHIN = 15

    def _run(self, body):
        script = textwrap.dedent(body).format(stuck=self.STUCK_SECONDS)
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=self.STUCK_SECONDS + 30,
        )
        return proc, time.monotonic() - t0

    def test_a_stuck_foreign_worker_does_not_hold_the_interpreter(self):
        proc, elapsed = self._run(
            """
            import concurrent.futures, time
            from ci_core.concurrency import exit_without_waiting_for_foreign_threads

            # Stand-in for litellm's logging executor: not ours, never shut
            # down, and its worker outlives the run.
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            pool.submit(lambda: time.sleep({stuck}))
            time.sleep(0.2)
            print("work done")
            exit_without_waiting_for_foreign_threads(0)
            """
        )

        assert proc.returncode == 0, proc.stderr
        assert "work done" in proc.stdout, proc.stderr
        assert elapsed < self.MUST_EXIT_WITHIN, (
            f"exit took {elapsed:.1f}s with a {self.STUCK_SECONDS}s foreign "
            f"worker still running — that is the original hang"
        )

    def test_output_written_before_the_exit_is_not_lost(self):
        """`os._exit` runs no buffers down, so the flush has to be explicit —
        and the report, review and capture are all written before this point."""
        proc, _ = self._run(
            """
            import sys
            from ci_core.concurrency import exit_without_waiting_for_foreign_threads

            print("stdout line")
            print("stderr line", file=sys.stderr)
            exit_without_waiting_for_foreign_threads(0)
            """
        )
        assert "stdout line" in proc.stdout
        assert "stderr line" in proc.stderr

    def test_the_exit_code_is_honoured(self):
        proc, _ = self._run(
            """
            from ci_core.concurrency import exit_without_waiting_for_foreign_threads
            exit_without_waiting_for_foreign_threads(3)
            """
        )
        assert proc.returncode == 3

    @pytest.mark.slow
    def test_without_it_the_process_really_does_hang(self):
        """Pins the diagnosis. A plain exit waits the foreign worker out."""
        proc, elapsed = self._run(
            """
            import concurrent.futures, time

            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            pool.submit(lambda: time.sleep({stuck}))
            time.sleep(0.2)
            print("work done")
            """
        )
        assert "work done" in proc.stdout
        assert elapsed >= self.STUCK_SECONDS - 3, (
            f"exited in {elapsed:.1f}s — CPython no longer joins non-daemon "
            f"pool workers at shutdown, so this fix needs revisiting"
        )
