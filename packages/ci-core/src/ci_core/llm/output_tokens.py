"""Output-token ceiling for reasoning passes — the ``max_tokens`` they are sent.

    max_tokens = reasoning_tokens[effort] + answer_tokens[domain] × size_mult(chars)

clamped to the model's own output limit. ``size_mult`` is the timeout model's
size table (configs/timeouts.yaml), used as-is, so the ceiling and the wall-clock
budget scale with the draft together. The numbers and the evidence behind them
are in configs/output_tokens.yaml.

Only two providers send a ceiling at all: claude (Anthropic requires one) and
mistral. The other four run to the model's own limit and are left alone here.
Of those two, only reasoning passes get a computed ceiling; the no-effort paths
keep ``client._provider_params``'s defaults. A claude model that thinks with no
effort set is a reasoning pass, not a no-effort one — see ``_EFFORT_WHEN_UNSET``.

The ceiling counts thinking. On Anthropic, "thinking tokens count toward the
max_tokens limit for the turn" (platform.claude.com, extended thinking), and a
grounded call runs a server-side sampling loop in which each iteration gets the
full ceiling while litellm reports the SUM of their output — which is how
claude:fact_check reported 20,215 output tokens against a 16,000 ceiling and
was still cut off. On Mistral, the reasoning chunks count the same way: every
truncated mistral pass on record stopped at exactly the ceiling having written
under a quarter of it as JSON.
"""

import functools
import math
from pathlib import Path

from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml
from ci_core.llm import timeout_model

#: Providers whose request carries a ceiling taken from the model config. Mirrors
#: ``client._provider_params``: claude and mistral read ``cfg["max_tokens"]``;
#: openai's Responses path, grok, gemini and perplexity send none, so computing
#: one for them would change nothing and read as though it did.
CAPPED_PROVIDERS = frozenset({"claude", "mistral"})

#: The config key each capped provider takes its reasoning level from — the same
#: key ``client._provider_params`` reads, so this agrees with what is sent.
_EFFORT_KEY = {"claude": "effort", "mistral": "reasoning_effort"}

#: The effort a model reasons at when its config sets none. Unset is not "none":
#: it is the provider's default, and for these models the default is to think.
#: Anthropic's per-model table lists thinking as "On" (Opus 5, Sonnet 5) or
#: "Always on" (Fable, Mythos) when the request omits it, and every other claude
#: model this repo has run as "Off"; and "setting effort to "high" produces
#: exactly the same behavior as omitting the effort parameter entirely"
#: (platform.claude.com, build-with-claude/thinking-troubleshooting and
#: build-with-claude/effort, read 2026-09-18). With no effort, no
#: ``output_config`` is sent, and ``thinking`` only asks for the thinking's
#: display (client._provider_params; both captured on the wire in
#: tests/test_llm_client.py), so these run exactly as ``effort: high`` does and
#: need the same ceiling. claude-opus-5 at high has spent over 15,000 tokens
#: reasoning on one pass (configs/output_tokens.yaml, a censored floor) — before
#: this list, the no-effort path sent it 8000 for reasoning and answer together.
#:
#: A list rather than litellm's model map, which cannot tell these apart: the one
#: thinking flag it carries on claude-opus-5 and claude-sonnet-5,
#: ``supports_adaptive_thinking``, is just as true of claude-opus-4-8, which does
#: not think unless asked. Newer litellm adds ``thinking_always_on``, but only on
#: Fable and Mythos; Opus 5 and Sonnet 5 can be switched off, so it is false for
#: them. Exact ids, so a model released after 2026-09-18 is not assumed either
#: way — it runs on the no-effort default until it is added here.
_EFFORT_WHEN_UNSET = {
    "claude": {
        "claude-opus-5": "high",
        "claude-sonnet-5": "high",
        "claude-fable-5": "high",
        "claude-fable-5-1": "high",
        "claude-mythos-5": "high",
        "claude-mythos-5-1": "high",
        "claude-mythos-preview": "high",
    },
}

#: Upper bound for a ``--no-timeout`` calibration run. The point of that run is an
#: uncensored measurement, so the ceiling goes as high as the model allows — but
#: not to Mistral's advertised 262,144, which is its whole context window: the
#: API rejects a request whose prompt plus ``max_tokens`` exceeds it.
CALIBRATION_MAX_TOKENS = 128_000


def _load():
    path = Path(__file__).parent.parent / "configs" / "output_tokens.yaml"
    data = load_packaged_yaml(path)
    for key in ("reasoning_tokens", "answer_tokens", "floor_tokens_per_second"):
        table = data.get(key)
        if not isinstance(table, dict) or "default" not in table:
            raise PackagedConfigError(
                f"{path}: {key!r} must be a mapping with a 'default' entry"
            )
        for name, value in table.items():
            if not isinstance(value, (int, float)) or value <= 0:
                raise PackagedConfigError(
                    f"{path}: {key}.{name} must be a positive number, got {value!r}"
                )
    return data


_CONFIG = _load()


def effort_when_unset(provider, model):
    """The effort ``model`` reasons at when its config sets none, or None.

    Matched on the bare id, so a route pinned in user.yaml
    (``anthropic/claude-opus-5``) counts as the model it routes to.
    """
    if not model:
        return None
    return _EFFORT_WHEN_UNSET.get(provider, {}).get(str(model).rsplit("/", 1)[-1])


def effort_of(provider, cfg):
    """The reasoning level ``provider`` will run at, or None for a plain pass.

    ``"none"`` is a real value mistral-medium-3-5 accepts, and it means no
    reasoning — so it is treated exactly like an absent key. An absent key is
    not always a plain pass, though: a model that thinks by default runs at its
    default (``_EFFORT_WHEN_UNSET``). That holds for a claude ``"none"`` too,
    since litellm drops it before sending, leaving a request with no effort.
    """
    key = _EFFORT_KEY.get(provider)
    effort = (cfg or {}).get(key) if key else None
    if not effort or str(effort).lower() == "none":
        return effort_when_unset(provider, (cfg or {}).get("model"))
    return str(effort).lower()


def effort_none_warnings(model_configs):
    """A warning for each claude config that asks for no thinking and won't get it.

    Whoever writes ``effort: none`` means "do not think", usually to save money,
    and on claude-opus-5 that is not what runs. Anthropic has no effort level
    called none. litellm drops it before sending, so the request carries no
    effort, and a model in ``_EFFORT_WHEN_UNSET`` thinks at its default, high,
    and is billed for it. Nothing fails and nothing truncates (``effort_of``
    sizes it as high), so nothing else says so. YAML reads
    ``effort: off``, ``no`` and ``false`` as false, and a false effort is not
    sent either, with the same result. Both are captured on the wire in
    tests/test_llm_client.py.

    It warns and changes nothing. The other way out is sending ``thinking:
    {"type": "disabled"}``, and for Opus 5 Anthropic documents two failure modes
    of disabled thinking: a tool call written into the visible text, and
    internal tags leaking into the answer. Anthropic recommends a lower effort
    instead, so that is what the warning suggests. Whether to disable thinking
    anyway is the operator's call, not this module's.

    Matched case-insensitively, but litellm matches exactly. It rejects
    ``None``, ``NONE`` or a padded ``" none "`` before sending, so every call
    fails. That is loud already, but only once calls are being made, and the
    warning for it says so rather than promising a high-effort run.
    """
    if not isinstance(model_configs, dict):
        return []
    cfg = model_configs.get("claude")
    if not isinstance(cfg, dict) or cfg.get("enabled", True) is False:
        return []
    if cfg.get("thinking_budget") is not None:
        # client._provider_params sends the budget and never reads the effort.
        return []
    effort = cfg.get(_EFFORT_KEY["claude"])
    if effort is not False and not (
        isinstance(effort, str) and effort.strip().lower() == "none"
    ):
        return []
    from ci_core.llm import client  # lazy, as in model_output_limit

    # The model the request goes to: a config with no `model` calls the client's
    # default, which is what decides whether it thinks.
    model = client._resolve_model("claude", None, cfg)
    default = effort_when_unset("claude", model)
    if default is None:
        return []
    if effort is False:
        said = (
            "effort: false (YAML reads off and no as false too), which does not "
            "turn its thinking off: a false effort is not sent"
        )
    elif effort == "none":
        said = (
            "effort: none, which does not turn its thinking off: Anthropic has "
            'no "none" effort, and litellm drops it before sending'
        )
    else:
        return [
            f"claude model {model} is set to effort: {effort!r}, which litellm "
            f'rejects before sending (it accepts only lowercase "none"), so '
            f'every claude call will fail. Lowercase "none" would not turn '
            f"thinking off either: with no effort, {model} thinks at its default, "
            f"{default}. Set effort: low or medium to spend less on thinking."
        ]
    return [
        f"claude model {model} is set to {said}. With no effort, {model} thinks "
        f"at its default, {default}, and is billed for it. Set effort: low or "
        f"medium to spend less on thinking."
    ]


@functools.lru_cache(maxsize=None)
def model_output_limit(provider, model):
    """The model's own maximum output, from litellm's model map, or None.

    None when the map does not know the model — a pinned route or a model newer
    than the installed litellm. The ceiling is then left unclamped rather than
    guessed: every value this module computes sits far below any current
    model's limit, so the clamp only ever matters for the calibration lift.
    """
    if not model:
        return None
    from ci_core.llm import client  # lazy: importing litellm is not free

    try:
        info = client._litellm().get_model_info(client._qualified(provider, model))
    except Exception:
        return None
    limit = info.get("max_output_tokens") or info.get("max_tokens")
    return int(limit) if isinstance(limit, (int, float)) and limit > 0 else None


def compute_max_tokens(provider, cfg, domain, char_count, config=None):
    """The output ceiling for one reasoning pass, or None when none applies.

    None means "leave the request alone": a provider that sends no ceiling, or a
    pass that does not reason, where the client's own default already fits.
    """
    if provider not in CAPPED_PROVIDERS:
        return None
    effort = effort_of(provider, cfg)
    if effort is None:
        return None
    table = config or _CONFIG
    reasoning = table["reasoning_tokens"]
    answer = table["answer_tokens"]
    size = timeout_model._size_mult(
        char_count, timeout_model._CONFIG["size_multipliers"]
    )
    raw = (
        float(reasoning.get(effort, reasoning["default"]))
        + float(answer.get(domain, answer["default"])) * size
    )
    ceiling = math.ceil(raw)
    limit = model_output_limit(provider, (cfg or {}).get("model"))
    return min(ceiling, limit) if limit else ceiling


def largest_ceiling(provider, cfg, char_count, config=None):
    """The highest ceiling ``provider`` could be sent on any domain, or None.

    The wall-clock budget is per model, not per pass, so it has to cover the
    domain with the most room — fact_check, by the answer table.
    """
    table = config or _CONFIG
    domains = [d for d in table["answer_tokens"] if d != "default"] + ["default"]
    ceilings = [
        compute_max_tokens(provider, cfg, d, char_count, config=table) for d in domains
    ]
    ceilings = [c for c in ceilings if c]
    return max(ceilings) if ceilings else None


def _floor_rate(model_id, table):
    for key in sorted((k for k in table if k != "default"), key=len, reverse=True):
        if (model_id or "").startswith(key):
            return float(table[key])
    return float(table["default"])


def seconds_to_fill(provider, cfg, char_count, config=None):
    """Wall-clock a healthy call needs to reach its largest ceiling, or None.

    Converts at the slowest healthy throughput measured for the model, so a
    budget sized from this lets any healthy call run to its ceiling — and be
    truncated, keeping every complete finding — rather than be killed by the
    backstop first, keeping none.
    """
    table = config or _CONFIG
    ceiling = largest_ceiling(provider, cfg, char_count, config=table)
    if not ceiling:
        return None
    rate = _floor_rate((cfg or {}).get("model"), table["floor_tokens_per_second"])
    return ceiling / rate


def calibration_ceiling(provider, cfg):
    """The ceiling for a ``--no-timeout`` run: as high as the model accepts.

    A calibration run exists to record how long a call really is; a ceiling low
    enough to cut it off would record the ceiling instead — the censoring
    configs/output_tokens.yaml warns about.

    None for a provider that sends no ceiling, and for a model whose limit is
    unknown: guessing high there turns the measurement into a 400.
    """
    if provider not in CAPPED_PROVIDERS:
        return None
    limit = model_output_limit(provider, (cfg or {}).get("model"))
    return min(limit, CALIBRATION_MAX_TOKENS) if limit else None
