"""Multi-model caller for style synthesis and detection.

Transport is :mod:`ci_core.llm` — the same litellm-backed call layer the review
pipeline uses, so this module inherits its hardening: HTTP error-body capture,
per-provider read-gap timeouts, and truncated-JSON salvage.

What stays here is the *policy* around those calls: which models run
(Perplexity is excluded by default — web grounding adds noise for corpus
analysis), how they fan out (:func:`ci_core.concurrency.run_all_bounded` —
daemon threads, never a ``ThreadPoolExecutor``; see that module's docstring),
the wall-clock backstop, and the cumulative API-call log used for cost
reporting.

Calls go through :func:`ci_core.llm.call_text` rather than
:func:`ci_core.llm.call_provider`: the shared layer parses its response as JSON
and reports a prose reply as a failure, but style synthesis does its own
parsing downstream (``extract_json``) and feeds some model output back into a
later prompt verbatim, so it wants the assembled text.

Import direction: this package depends on ci-core only. Nothing from
ci-article-review is imported here, and nothing imports back up this chain.
"""

from __future__ import annotations

import logging
import time
from ci_core.concurrency import run_all_bounded
from ci_core.config_helpers import normalize_model_configs
from ci_core.llm import timeout_model
from ci_core.llm import call_text
from ci_core.llm.cost import calculate as cost_calculate

log = logging.getLogger(__name__)

# Providers excluded from synthesis/detection by default
_EXCLUDED_PROVIDERS = frozenset({"perplexity"})

# Absolute ceiling for one model's wall-clock budget, mirroring the review
# pipeline's pipeline.task_timeout_seconds. Corpus synthesis prompts are large
# (up to ~120k chars), so this is generous.
_TASK_CEILING_SECONDS = 1200

# api_call_log accumulated across the process lifetime; read by bootstrap.py at run end
_api_call_log: list[dict] = []


def get_api_call_log() -> list[dict]:
    return list(_api_call_log)


def clear_api_call_log() -> None:
    _api_call_log.clear()


def call_one(
    model_name: str,
    model_cfg: dict,
    api_keys: dict,
    system_prompt: str,
    user_prompt: str,
    pass_name: str = "",
) -> dict:
    """Call one model through the shared adapter layer.

    Returns {"content": str, "failed": bool, "tokens": dict, "elapsed": float, "model": str}.
    Failed providers log the error and return {"failed": True, "error": ...}.
    """
    api_key = (api_keys.get(model_name) or {}).get("api_key", "")
    t0 = time.monotonic()

    try:
        result = call_text(
            model_name,
            system_prompt,
            user_prompt,
            api_key,
            provider_config=model_cfg,
        )
    except KeyError:
        # No provider by that name — a config error, not a provider failure.
        log.warning("Unknown model %r (no provider); skipping", model_name)
        return {
            "failed": True,
            "error": f"Unknown model {model_name!r}",
            "model": model_name,
            "tokens": {},
            "elapsed": round(time.monotonic() - t0, 2),
        }

    if result.get("failed"):
        detail = result.get("error", "")
        if result.get("error_body"):
            detail = f"{detail} | {result['error_body']}"
        log.error(
            "%s failed after %.1fs: %s", model_name, result.get("elapsed", 0.0), detail
        )
    else:
        tokens = result.get("tokens") or {}
        log.info(
            "%s: synthesis complete (%d tokens, %.1fs)",
            model_name,
            sum(v for v in tokens.values() if isinstance(v, (int, float))),
            result.get("elapsed", 0.0),
        )

    # Append to global cost log
    _api_call_log.append(
        {
            "pass": pass_name,
            "model": result.get("model", model_cfg.get("model", model_name)),
            "tokens": result.get("tokens", {}),
            "failed": result.get("failed", False),
        }
    )
    return result


def call_all(
    system_prompt: str,
    user_prompt: str,
    user_config: dict,
    models: list[str] | None = None,
    max_parallel: int = 0,
    exclude_perplexity: bool = True,
    pass_name: str = "",
) -> dict[str, dict]:
    """Call each model in parallel, bounded by ``max_parallel``.

    Fans out on :func:`ci_core.concurrency.run_all_bounded` — one daemon
    thread per model, semaphore-bounded — rather than a
    ``ThreadPoolExecutor``. See :mod:`ci_core.concurrency` for why: a pool
    worker still inside a call at interpreter exit is joined by
    ``concurrent.futures``' atexit hook with a bare, untimed ``t.join()``,
    which is how finished ``ci-review`` processes were found alive two days
    later. This module's previous form nested ``run_with_timeout`` *inside*
    pool workers, which looks bounded but is not: a model missing from
    ``backstops`` got a None budget, and ``run_with_timeout(fn, None)``
    waits forever — reintroducing the exact hang inside the pool.

    Args:
        system_prompt: System prompt string.
        user_prompt: User prompt string.
        user_config: Full user config (from load_user_config / load_yaml + normalize).
        models: Explicit model subset to use; None = all configured models.
        max_parallel: Max concurrent threads; 0 = all at once.
        exclude_perplexity: If True, skip perplexity (default True).
        pass_name: Label for cost log.

    Returns:
        {model_name: {"content": str, "failed": bool, "tokens": dict, "elapsed": float}}
    """
    models_cfg: dict = user_config.get("models", {})
    api_keys: dict = user_config.get("api_keys", {})

    # Normalize model configs (handle simple string form)
    models_cfg = normalize_model_configs(models_cfg)

    # Filter to requested subset
    if models is not None:
        active = {k: v for k, v in models_cfg.items() if k in models}
    else:
        active = dict(models_cfg)

    # Exclude disabled models
    active = {k: v for k, v in active.items() if v.get("enabled", True) is not False}

    # Exclude perplexity by default
    if exclude_perplexity:
        active = {k: v for k, v in active.items() if k not in _EXCLUDED_PROVIDERS}

    if not active:
        log.warning(
            "call_all: no active models to call (models=%r, exclude_perplexity=%r)",
            models,
            exclude_perplexity,
        )
        return {}

    # Wall-clock backstop per model, sized from prompt length x model x effort.
    # Under streaming the socket timeout is only the inter-token read gap, so a
    # model that dribbles out tokens indefinitely would otherwise never stop.
    backstops = timeout_model.compute_all(
        len(system_prompt) + len(user_prompt),
        active,
        _TASK_CEILING_SECONDS,
    )

    def _budget(name: str) -> float:
        """The wall-clock budget for one model, never None.

        ``compute_all`` skips a model whose ``enabled`` is falsy-but-not-False,
        while the filter above keeps it — ``enabled:`` written with no value at
        all parses as None in YAML, which is exactly that shape. So ``backstops``
        can genuinely lack an entry for a model we are about to call.

        Left as None that model would run unbounded: the old code passed the
        None straight to ``run_with_timeout``, whose documented contract for a
        None timeout is to wait for the call, inside a pool worker that
        ``concurrent.futures`` then joins untimed at exit. A one-character
        config slip was enough to hang the process for as long as the provider
        kept the socket open. Fall back to the task ceiling and say so, so the
        config mistake surfaces as a warning instead of a silent hang.
        """
        budget = backstops.get(name)
        if budget is None:
            log.warning(
                "call_all: %s has no computed backstop; check its 'enabled' "
                "value in user.yaml. Falling back to the %ss task ceiling.",
                name,
                _TASK_CEILING_SECONDS,
            )
            return float(_TASK_CEILING_SECONDS)
        return budget

    budgets = {name: _budget(name) for name in active}

    def _job(name: str, cfg: dict):
        return lambda: call_one(
            name,
            cfg,
            api_keys,
            system_prompt,
            user_prompt,
            pass_name=pass_name,
        )

    jobs = [(name, _job(name, cfg), budgets[name]) for name, cfg in active.items()]

    results: dict[str, dict] = {}
    for name, (value, error) in run_all_bounded(
        jobs, max_parallel=max_parallel
    ).items():
        if error is None:
            results[name] = value
            continue
        if isinstance(error, TimeoutError):
            budget = budgets[name]
            log.error("call_all: %s exceeded its %ss wall-clock backstop", name, budget)
            results[name] = {
                "failed": True,
                "error": f"Timed out after {budget}s (wall-clock backstop)",
                "model": active[name].get("model", name),
                "tokens": {},
                # The call was cut off, so the true elapsed time is unknown —
                # record the budget as a lower bound rather than 0.
                "elapsed": float(budget or 0.0),
            }
        else:
            log.error("call_all: unexpected error from %s: %s", name, error)
            results[name] = {
                "failed": True,
                "error": str(error),
                "tokens": {},
                "elapsed": 0.0,
            }

    return results


def log_cost_summary() -> None:
    """Log spend summary using the accumulated api_call_log."""
    log_entries = get_api_call_log()
    if not log_entries:
        return
    summary = cost_calculate(log_entries)
    log.info(
        "API spend: $%.4f total ($%.4f input + $%.4f output) across %d calls%s",
        summary["total_usd"],
        summary["total_input_usd"],
        summary["total_output_usd"],
        len(log_entries),
        "" if summary["pricing_known"] else " [pricing estimate — some models unknown]",
    )
