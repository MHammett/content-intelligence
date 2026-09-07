"""Tests for callers.py — multi-model routing, SSE accumulation, error handling."""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import litellm
import pytest


# ---------------------------------------------------------------------------
# litellm-shaped stream mocks
# ---------------------------------------------------------------------------
# These patch the litellm call itself rather than the HTTP transport. The old
# versions mocked requests.Session, which after the litellm migration no longer
# intercepts anything — the calls went out to the real providers, and the
# failure-path tests "passed" because a real call fails too.


def _completion_stream(text: str):
    """A litellm.completion(stream=True) response carrying ``text`` and usage."""

    def chunk(content=None, finish_reason=None, usage=None):
        choice = SimpleNamespace(
            delta=SimpleNamespace(content=content), finish_reason=finish_reason
        )
        return SimpleNamespace(
            choices=[choice] if (content or finish_reason) else [],
            usage=usage,
            citations=None,
            search_results=None,
            vertex_ai_grounding_metadata=None,
        )

    return [
        chunk(content=text),
        chunk(finish_reason="stop"),
        chunk(
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                prompt_tokens_details=None,
                cache_read_input_tokens=None,
            )
        ),
    ]


def _responses_stream(text: str):
    """A litellm.responses(stream=True) event stream — the OpenAI surface."""
    return [
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=100,
                    completion_tokens=20,
                    prompt_tokens_details=None,
                    cache_read_input_tokens=None,
                ),
                status="completed",
                incomplete_details=None,
            ),
        ),
    ]


_MOCK_USER_CONFIG = {
    "models": {
        "claude": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "openai": {"provider": "openai", "model": "gpt-5.4"},
        "gemini": {"provider": "ai_studio", "model": "gemini-2.5-flash"},
    },
    "api_keys": {
        "claude": {"api_key": "test-key-claude"},
        "openai": {"api_key": "test-key-openai"},
        "gemini": {"api_key": "test-key-gemini"},
    },
}


class TestCallOneAnthropic:
    def test_anthropic_success(self):
        from ci_style_profile.callers import call_one, clear_api_call_log

        clear_api_call_log()

        with patch.object(
            litellm,
            "completion",
            return_value=_completion_stream('{"style_profile": "test"}'),
        ):
            result = call_one(
                "claude",
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                {"claude": {"api_key": "sk-test"}},
                "system",
                "user",
            )

        assert not result.get("failed"), result.get("error")
        assert result["tokens"]["prompt"] == 100
        assert result["tokens"]["completion"] == 20
        assert '{"style_profile": "test"}' in result["content"]

    def test_anthropic_http_error(self):
        """call_one returns failed=True on a transport error; does not raise."""
        from ci_style_profile.callers import call_one, clear_api_call_log

        clear_api_call_log()

        with patch.object(
            litellm, "completion", side_effect=Exception("Connection refused")
        ):
            result = call_one(
                "claude",
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                {"claude": {"api_key": "sk-test"}},
                "system",
                "user",
            )

        assert result["failed"] is True
        assert "error" in result


class TestCallOneOpenAI:
    def test_openai_success(self):
        """OpenAI goes through responses(), not completion() — see ci_core.llm.client."""
        from ci_style_profile.callers import call_one, clear_api_call_log

        clear_api_call_log()

        with patch.object(
            litellm,
            "responses",
            return_value=_responses_stream('{"style_profile": "openai result"}'),
        ):
            result = call_one(
                "openai",
                {"provider": "openai", "model": "gpt-5.4"},
                {"openai": {"api_key": "sk-test"}},
                "system",
                "user",
            )

        assert not result.get("failed")
        assert "openai result" in result.get("content", "")

    def test_openai_server_error_returns_failed(self):
        """call_one returns failed=True on 5xx; does not raise."""
        from ci_style_profile.callers import call_one, clear_api_call_log

        clear_api_call_log()

        with patch.object(
            litellm, "responses", side_effect=Exception("503 Server Error")
        ):
            result = call_one(
                "openai",
                {"provider": "openai", "model": "gpt-5.4"},
                {"openai": {"api_key": "sk-test"}},
                "system",
                "user",
            )

        assert result["failed"] is True


class TestCallOneGemini:
    def test_gemini_success(self):
        from ci_style_profile.callers import call_one, clear_api_call_log

        clear_api_call_log()

        with patch.object(
            litellm,
            "completion",
            return_value=_completion_stream('{"style_profile": "gemini result"}'),
        ):
            result = call_one(
                "gemini",
                {"provider": "ai_studio", "model": "gemini-2.5-flash"},
                {"gemini": {"api_key": "AIza-test"}},
                "system",
                "user",
            )

        assert not result.get("failed")
        assert result["tokens"]["prompt"] == 100
        assert result["tokens"]["completion"] == 20


class TestCallAll:
    def test_call_all_three_models(self):
        """call_all with 3 models: all 3 called in parallel; results keyed by model name."""
        from ci_style_profile.callers import call_all, clear_api_call_log

        clear_api_call_log()

        def _fake_call_one(model_name, model_cfg, api_keys, system, user, pass_name=""):
            return {
                "content": f"{model_name}_result",
                "failed": False,
                "tokens": {},
                "elapsed": 0.1,
                "model": model_name,
            }

        with patch("ci_style_profile.callers.call_one", side_effect=_fake_call_one):
            results = call_all(
                system_prompt="system",
                user_prompt="user",
                user_config=_MOCK_USER_CONFIG,
                pass_name="test",
            )

        assert "claude" in results
        assert "openai" in results
        assert "gemini" in results
        assert results["claude"]["content"] == "claude_result"

    def test_call_all_subset(self):
        """call_all with models=["claude"]: only Claude called."""
        from ci_style_profile.callers import call_all, clear_api_call_log

        clear_api_call_log()

        called = []

        def _fake_call_one(model_name, model_cfg, api_keys, system, user, pass_name=""):
            called.append(model_name)
            return {
                "content": f"{model_name}_result",
                "failed": False,
                "tokens": {},
                "elapsed": 0.1,
                "model": model_name,
            }

        with patch("ci_style_profile.callers.call_one", side_effect=_fake_call_one):
            results = call_all(
                system_prompt="system",
                user_prompt="user",
                user_config=_MOCK_USER_CONFIG,
                models=["claude"],
                pass_name="test",
            )

        assert called == ["claude"]
        assert "openai" not in results
        assert "gemini" not in results

    def test_call_all_applies_wall_clock_backstop(self):
        """A call that overruns its budget is reported as a timeout, not awaited.

        Under streaming the socket timeout is only the inter-token read gap, so
        a model that keeps dribbling tokens needs a wall-clock bound on top.
        """
        import time

        from ci_style_profile.callers import call_all, clear_api_call_log

        clear_api_call_log()

        def _slow_call_one(model_name, *a, **kw):
            time.sleep(2)
            return {
                "content": "too late",
                "failed": False,
                "tokens": {},
                "elapsed": 2.0,
            }

        with (
            patch("ci_style_profile.callers.call_one", side_effect=_slow_call_one),
            patch(
                "ci_style_profile.callers.timeout_model.compute_all",
                return_value={"claude": 0.2},
            ),
        ):
            started = time.monotonic()
            results = call_all(
                system_prompt="s",
                user_prompt="u",
                user_config=_MOCK_USER_CONFIG,
                models=["claude"],
            )
            gave_up_after = time.monotonic() - started

        assert results["claude"]["failed"] is True
        assert "backstop" in results["claude"]["error"]
        assert results["claude"]["elapsed"] == 0.2
        # Reported back well before the call itself finished.
        assert gave_up_after < 1.5

    def test_call_all_uses_per_model_backstops_from_timeout_model(self):
        """Budgets come from the shared sliding-scale model, sized on prompt length."""
        from ci_style_profile.callers import call_all, clear_api_call_log

        clear_api_call_log()
        seen = {}

        def _capture(char_count, model_configs, ceiling, **kw):
            seen["char_count"] = char_count
            seen["models"] = sorted(model_configs)
            seen["ceiling"] = ceiling
            return {name: 30 for name in model_configs}

        def _fake_call_one(model_name, *a, **kw):
            return {"content": "", "failed": False, "tokens": {}, "elapsed": 0.0}

        with (
            patch("ci_style_profile.callers.call_one", side_effect=_fake_call_one),
            patch(
                "ci_style_profile.callers.timeout_model.compute_all",
                side_effect=_capture,
            ),
        ):
            call_all(
                system_prompt="s" * 100,
                user_prompt="u" * 400,
                user_config=_MOCK_USER_CONFIG,
            )

        assert seen["char_count"] == 500
        assert seen["models"] == ["claude", "gemini", "openai"]
        assert seen["ceiling"] > 0

    def test_call_all_excludes_perplexity(self):
        """Perplexity is excluded by default."""
        from ci_style_profile.callers import call_all, clear_api_call_log

        clear_api_call_log()

        config_with_perplexity = {
            **_MOCK_USER_CONFIG,
            "models": {
                **_MOCK_USER_CONFIG["models"],
                "perplexity": {"provider": "perplexity", "model": "sonar"},
            },
            "api_keys": {
                **_MOCK_USER_CONFIG["api_keys"],
                "perplexity": {"api_key": "pplx-test"},
            },
        }

        called = []

        def _fake_call_one(model_name, *a, **kw):
            called.append(model_name)
            return {
                "content": "",
                "failed": False,
                "tokens": {},
                "elapsed": 0.1,
                "model": model_name,
            }

        with patch("ci_style_profile.callers.call_one", side_effect=_fake_call_one):
            call_all(
                system_prompt="s", user_prompt="u", user_config=config_with_perplexity
            )

        assert "perplexity" not in called


# ---------------------------------------------------------------------------
# The un-backstopped-model hang
# ---------------------------------------------------------------------------


class TestModelWithNoComputedBackstop:
    """The gap between which models get *called* and which get a *budget*.

    ``call_all`` keeps a model when ``enabled`` is anything other than the
    literal ``False``; ``timeout_model.compute_all`` skips a model when
    ``enabled`` is merely falsy. Three reachable config values fall in that gap
    — ``0``, ``""``, and ``None`` — and ``enabled:`` written with no value at
    all is exactly how YAML produces the third.

    A model in the gap was called with a None budget. The old code passed that
    straight to ``run_with_timeout``, whose documented contract for a None
    timeout is to wait for the call, from inside a ``ThreadPoolExecutor``
    worker that ``concurrent.futures`` then joins untimed at interpreter exit.
    So a one-character config slip reproduced the original two-day hang in full:
    the run finished its work, printed its output, and never exited.
    """

    def _config(self, enabled):
        return {
            "models": {"claude": {"model": "anthropic/x", "enabled": enabled}},
            "api_keys": {},
        }

    @pytest.mark.parametrize("enabled", [0, "", None])
    def test_the_gap_is_real_for_every_falsy_enabled_value(self, enabled):
        """Pins the cause, so the fix can't be argued away as hypothetical."""
        from ci_core.config_helpers import normalize_model_configs
        from ci_core.llm import timeout_model

        cfg = normalize_model_configs(self._config(enabled)["models"])
        active = {k: v for k, v in cfg.items() if v.get("enabled", True) is not False}
        backstops = timeout_model.compute_all(1000, active, 1200)

        assert "claude" in active, "call_all would call this model"
        assert "claude" not in backstops, "but compute_all gives it no budget"

    def test_such_a_model_is_bounded_rather_than_awaited_forever(self):
        from ci_style_profile import callers as C

        never_returns = threading.Event()  # deliberately never set

        with (
            patch.object(
                C, "call_one", side_effect=lambda *a, **kw: never_returns.wait()
            ),
            patch.object(C, "_TASK_CEILING_SECONDS", 2),
        ):
            started = time.monotonic()
            results = C.call_all(
                system_prompt="s", user_prompt="u", user_config=self._config(None)
            )
            elapsed = time.monotonic() - started

        assert results["claude"]["failed"] is True
        assert "backstop" in results["claude"]["error"]
        # Bounded by the task ceiling it fell back to, not by the call.
        assert elapsed < 10, (
            f"call_all waited {elapsed:.1f}s on a model with no computed "
            f"backstop — it is treating 'no budget' as 'no limit'"
        )

    def test_the_missing_budget_is_reported_not_silently_papered_over(self, caplog):
        """The fallback fixes the hang; the warning fixes the config.

        Quietly substituting a ceiling would leave a user whose model was never
        meant to be disabled wondering why it now takes 20 minutes to fail.
        """
        from ci_style_profile import callers as C

        with (
            patch.object(C, "call_one", side_effect=lambda *a, **kw: {"content": "x"}),
            caplog.at_level(logging.WARNING, logger="ci_style_profile.callers"),
        ):
            C.call_all(
                system_prompt="s", user_prompt="u", user_config=self._config(None)
            )

        assert any("no computed backstop" in r.getMessage() for r in caplog.records), (
            f"expected a warning naming the missing backstop, got: {caplog.messages}"
        )


class TestCallAllProcessExit:
    """``call_all`` must not hold the interpreter open. Needs a subprocess.

    Every in-process assertion above passes against the broken version too —
    the defect is in interpreter *exit*, which nothing running inside the
    interpreter can observe. This is the only test here that can see it.
    """

    # Measured on this machine, 2026-09-05: the subprocess costs 7.2-8.3s, of
    # which 5.9s is importing ci_style_profile.callers (litellm) before a line
    # of the test runs. An earlier 8s limit was therefore timing the import, and
    # failed in a full-suite run while passing in isolation.
    #
    # There is no tight bound worth drawing here, because the two outcomes this
    # separates are not close: fixed, the process exits at import cost plus the
    # 1s ceiling; broken, it never exits at all (measured at >90s before being
    # killed, and unbounded in principle — the call it is waiting on never
    # returns). 30s is ~3.6x the observed worst case and still decisively
    # short of "forever".
    MUST_EXIT_WITHIN = 30

    @pytest.mark.slow
    def test_a_model_with_no_backstop_does_not_hold_the_process_open(self):
        script = textwrap.dedent(
            """
            import threading, sys
            from unittest.mock import patch
            import ci_style_profile.callers as C

            never = threading.Event()

            cfg = {
                "models": {"claude": {"model": "anthropic/x", "enabled": None}},
                "api_keys": {},
            }

            with patch.object(C, "call_one", side_effect=lambda *a, **kw: never.wait()), \\
                 patch.object(C, "_TASK_CEILING_SECONDS", 1):
                res = C.call_all(system_prompt="s", user_prompt="u", user_config=cfg)

            print("failed" if res["claude"]["failed"] else "ok")
            print("exiting")
            """
        )
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - t0

        assert proc.returncode == 0, proc.stderr
        assert "failed" in proc.stdout
        assert "exiting" in proc.stdout
        assert elapsed < self.MUST_EXIT_WITHIN, (
            f"call_all's process took {elapsed:.1f}s to exit with a call that "
            f"never returns still running. Before the migration this never "
            f"exited at all: the None budget made run_with_timeout wait "
            f"forever inside a pool worker, and concurrent.futures' atexit "
            f"hook joined that worker with a bare, untimed t.join()."
        )
