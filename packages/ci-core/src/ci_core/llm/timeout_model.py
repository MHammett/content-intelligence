"""Sliding-scale timeout model.

Computes a per-model timeout from the draft size, the model, and the reasoning
effort, so timeouts track real call latency instead of being hand-tuned per preset.
Calibrated from runs on 2026-06-22; see configs/timeouts.yaml.

Since the review adapters stream (SSE), the value computed here is the per-task
thread WALL-CLOCK BACKSTOP (enforced by pipeline._run_reviews_in_parallel), not the HTTP
socket timeout. The socket timeout is a small constant inter-token read gap held by
the adapters (ci_core/llm/streaming.py). This module is unchanged by streaming —
only the *meaning* of its output shifted from "socket read timeout" to "wall-clock
backstop"; both are sized the same way (total generation time still bounds it).

    effective = clamp( base × size_mult × model_mult × effort_mult, floor, ceiling )

and, for a model sent an output-token ceiling, at least the time a healthy call
needs to reach that ceiling (``compute_budget``; see ci_core/llm/output_tokens.py).

Config is loaded from configs/timeouts.yaml at import time; the hardcoded
_FALLBACK is used only when the YAML is missing or unreadable.
"""

import logging
import math
from pathlib import Path

import yaml as _yaml  # noqa: F401

from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml

log = logging.getLogger(__name__)


def _load():
    """Load the sliding-scale timeout model from the packaged timeouts.yaml.

    Raises PackagedConfigError rather than falling back to a duplicate table in
    Python — see ci_core.config_helpers.load_packaged_yaml for why. The shape
    checks below are real validation, which is something the old parity test
    could not provide: it only proved two copies matched, not that either was
    usable.
    """
    path = Path(__file__).parent.parent / "configs" / "timeouts.yaml"
    data = load_packaged_yaml(path)
    for key in (
        "base_seconds",
        "floor_seconds",
        "size_multipliers",
        "model_multipliers",
        "effort_multipliers",
    ):
        if key not in data:
            raise PackagedConfigError(f"{path}: missing required key {key!r}")

    buckets = data["size_multipliers"]
    if not isinstance(buckets, list) or not buckets:
        raise PackagedConfigError(
            f"{path}: 'size_multipliers' must be a non-empty list"
        )
    # Ascending max_chars with exactly one open-ended final bucket, or the
    # lookup silently picks the wrong multiplier for large drafts.
    seen_open = False
    last = -1
    for bucket in buckets:
        limit = bucket.get("max_chars")
        if limit is None:
            seen_open = True
            continue
        if seen_open:
            raise PackagedConfigError(
                f"{path}: size_multipliers has a bucket after the open-ended one"
            )
        if limit <= last:
            raise PackagedConfigError(
                f"{path}: size_multipliers max_chars must ascend (saw {limit} after {last})"
            )
        last = limit
    if not seen_open:
        raise PackagedConfigError(
            f"{path}: size_multipliers needs a final bucket with max_chars: null"
        )
    return data


_CONFIG = _load()


def _size_mult(char_count, buckets):
    for b in buckets:
        cap = b.get("max_chars")
        if cap is None or char_count <= cap:
            return float(b["mult"])
    return float(buckets[-1]["mult"])


def _model_mult(model_id, table):
    if not model_id:
        return float(table.get("default", 1.0))
    # Longest key first so "mistral-medium-3-5" wins over a hypothetical "mistral".
    for key in sorted((k for k in table if k != "default"), key=len, reverse=True):
        if model_id.startswith(key):
            return float(table[key])
    return float(table.get("default", 1.0))


def compute_timeout(char_count, model_id, effort, task_ceiling_seconds, config=None):
    """Return the effective per-call timeout in seconds for one model.

    char_count:           length of the draft text.
    model_id:             resolved model id (e.g. "gpt-5.5").
    effort:               reasoning_effort / effort value, or None.
    task_ceiling_seconds: absolute upper bound; result is clamped to ceiling − 15.
    """
    cfg = config or _CONFIG
    base = float(cfg["base_seconds"])
    floor = int(cfg["floor_seconds"])
    size = _size_mult(char_count, cfg["size_multipliers"])
    model = _model_mult(model_id, cfg["model_multipliers"])
    eff_table = cfg["effort_multipliers"]
    eff = float(eff_table.get(effort or "default", eff_table.get("default", 1.0)))
    variance = float(
        cfg.get("variance_margin", 1.0)
    )  # default 1.0 keeps old behavior if absent

    raw = base * size * model * eff * variance
    ceiling = max(floor, int(task_ceiling_seconds) - 15)
    return int(min(max(raw, floor), ceiling))


def compute_budget(char_count, provider, cfg, task_ceiling_seconds, config=None):
    """``compute_timeout``, raised if need be to let the model reach its ceiling.

    A model sent an output-token ceiling (see ``output_tokens``) needs enough
    wall-clock to generate it. The formula above sizes for TYPICAL output and
    this for the LONGEST allowed: whichever is larger wins, still clamped to the
    task ceiling. Without it, raising the token ceiling converts truncations —
    which keep every complete finding — into timeouts, which keep none.

    Measured case: claude:fact_check on a 27,113-char draft ran 391.39s of a
    446s budget and was still cut off at the old 16,000 ceiling. A ceiling with
    room to finish needed a budget with room to reach it.
    """
    effort = cfg.get("reasoning_effort") or cfg.get("effort")
    budget = compute_timeout(
        char_count, cfg.get("model", ""), effort, task_ceiling_seconds, config=config
    )
    # Imported here, not at the top: output_tokens sizes its ceiling off this
    # module's size table, so it imports this module first.
    from ci_core.llm import output_tokens

    need = output_tokens.seconds_to_fill(provider, cfg, char_count)
    if need:
        cfg_ = config or _CONFIG
        ceiling = max(int(cfg_["floor_seconds"]), int(task_ceiling_seconds) - 15)
        budget = min(max(budget, math.ceil(need)), ceiling)
    return budget


def compute_all(char_count, model_configs, task_ceiling_seconds, config=None):
    """Compute effective timeouts for every enabled model.

    An explicit ``timeout_seconds`` already present on a model config is treated as
    a user override and returned unchanged. All other models get a computed value.
    Returns ``{provider: timeout_seconds}``.
    """
    out = {}
    for provider, cfg in (model_configs or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        explicit = cfg.get("timeout_seconds")
        if explicit is not None:
            out[provider] = int(explicit)
            continue
        out[provider] = compute_budget(
            char_count, provider, cfg, task_ceiling_seconds, config=config
        )
    return out


def flag_stale_overrides(
    char_count, model_configs, task_ceiling_seconds, config=None, ratio=0.5
):
    """Explicit ``timeout_seconds`` overrides that undercut what the formula
    would compute for the model's *current* effort and this draft's size.

    This is the failure mode that hit perplexity (fixed 2026-08-16, see
    configs/user.example.yaml's note on it) and claude (fixed 2026-08-18,
    losing 3 of 5 domains on a real run): a ``timeout_seconds`` set while a
    provider ran light gets left behind after that provider's preset moves to
    a heavier effort or a grounded/reasoning model, and ``compute_all`` treats
    any explicit value as authoritative — so the formula never gets a chance
    to say the override is now too tight. This does not run automatically; a
    caller logs its findings so an operator sees them.

    Returns ``[(provider, override_seconds, formula_seconds), ...]`` for every
    enabled model whose override sits below ``ratio`` of the value the model
    would otherwise get from ``compute_budget``. Silent about models with no
    override — that is the normal, recommended state.
    """
    out = []
    for provider, cfg in (model_configs or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        explicit = cfg.get("timeout_seconds")
        if explicit is None:
            continue
        formula = compute_budget(
            char_count, provider, cfg, task_ceiling_seconds, config=config
        )
        if explicit < ratio * formula:
            out.append((provider, int(explicit), formula))
    return out
