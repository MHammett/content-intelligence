import difflib
import logging
import re
from pathlib import Path
from dotenv import find_dotenv, load_dotenv

from ci_core.config_helpers import (
    PackagedConfigError,
    load_packaged_yaml,
    load_yaml as _load_yaml,
    normalize_model_configs as _normalize_model_configs,
    resolve_env_recursive as _resolve_env_recursive,
)
from ci_core.env_provenance import (
    effective_env as _effective_env,
    provenance as _env_var_provenance,
    shadowed_mismatches as _shadowed_mismatches,
    snapshot as _snapshot_dotenv,
)
from ci_core.redact import mask_secret

log = logging.getLogger("config_loader")

# Resolved once so the snapshot below and the actual load agree on exactly
# which file (if any) is in play -- see ci_core.env_provenance for why this
# matters: the snapshot has to be taken before load_dotenv() touches the OS
# environment.
_DOTENV_PATH = find_dotenv()
_ENV_SNAPSHOT = _snapshot_dotenv(_DOTENV_PATH)
load_dotenv(_DOTENV_PATH or None)

# What every ``${VAR}`` in user.yaml / a publication config actually resolves
# against: .env's own value overrides a same-named OS environment variable,
# not the reverse (see ci_core.env_provenance.effective_env). This is the
# lowest tier of this project's config precedence — CLI override (--api-key)
# > publication config file > this.
_EFFECTIVE_ENV = _effective_env(_ENV_SNAPSHOT)


def warn_env_shadowing(env_snapshot=None):
    """Print a loud, unprompted notice for every OS env var whose value
    differs from the .env file's — informational, not a warning that
    something is misconfigured, since .env now wins either way (see
    ci_core.env_provenance). Two sources disagreeing is still worth surfacing
    in case the OS-level value was the one actually intended for this shell.

    Called once below against the real environment; tests pass
    ``env_snapshot`` to exercise specific scenarios without touching process
    state.
    """
    snap = _ENV_SNAPSHOT if env_snapshot is None else env_snapshot
    for m in _shadowed_mismatches(snap):
        print(
            f"NOTE: {m['var_name']} is set in both your .env file and the OS "
            "environment, with different values. The .env value is being "
            "used — this project's precedence is CLI override > publication "
            "config > .env > OS environment variable. If you meant the "
            "OS-level value to apply for this run, pass `--api-key "
            "provider=value` on the command line instead of relying on the "
            "shell variable. Run `uv run ci-check --publication <name> "
            "--show-keys` to see exactly which value each provider will use."
        )


warn_env_shadowing()

REQUIRED_USER_KEYS = [
    # LanguageTool is intentionally absent — grammar_pass is optional.
    ("api_keys", "openai", "api_key"),
    ("api_keys", "gemini", "api_key"),
    ("api_keys", "mistral", "api_key"),
]

REQUIRED_PUB_KEYS = [
    ("publication_name",),
    ("wordpress", "site_url"),
    ("wordpress", "username"),
    ("wordpress", "application_password"),
]

# Publication names must be simple identifiers — no path components allowed.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_publication_name(name):
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid publication name {name!r}. "
            "Use only letters, numbers, hyphens, and underscores."
        )


def _get_nested(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def load_user_config(config_dir="configs"):
    path = Path(config_dir) / "user.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"User config not found at {path}.\n"
            "Copy configs/user.example.yaml to configs/user.yaml and fill in your API keys.\n"
            "API keys can also be set as environment variables — see .env.example."
        )
    config = _load_yaml(path)
    if not isinstance(config, dict):
        raise ValueError(f"{path} is empty or not a valid YAML mapping.")
    config = _resolve_env_recursive(config, env=_EFFECTIVE_ENV)
    _validate_user_config(config)
    return config


def _iter_leaf_strings(node, path=()):
    """Yield (field_path_tuple, value) for every leaf under a nested dict."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_leaf_strings(v, path + (k,))
    elif node is not None:
        yield path, node


def describe_api_key_sources(config_dir="configs", env_snapshot=None):
    """Masked, provenance-annotated view of every ``api_keys.*`` entry in
    ``user.yaml`` -- what a run would actually send as credentials, and where
    each value came from.

    Reads the file directly rather than through ``load_user_config()``, so it
    still works when required keys are missing -- the point is diagnosing a
    key that looks wrong *before* validation gets a chance to complain about
    something else. Returns a list of dicts in file order, one per leaf under
    ``api_keys``:

      provider      -- e.g. "openai"
      field         -- e.g. "api_key" ("username" for languagetool, etc.)
      var_name      -- the ``${VAR_NAME}`` this entry references, or None if
                        the value is written literally in user.yaml
      masked_value  -- the resolved value, masked for display (see
                        ci_core.redact.mask_secret)
      source        -- "env" (resolved from the .env file), "env_overrides_os_env"
                        (same, but a same-named OS environment variable with a
                        *different* value also exists — .env still wins, this
                        just flags the disagreement), "os_env" (no .env entry
                        for this variable, so the OS environment is the
                        fallback), "literal" (hardcoded directly in
                        user.yaml, not from an env var), or "unset"
                        (references an env var that isn't set anywhere).
    """
    snap = _ENV_SNAPSHOT if env_snapshot is None else env_snapshot
    path = Path(config_dir) / "user.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"User config not found at {path}.\n"
            "Copy configs/user.example.yaml to configs/user.yaml and fill in your API keys.\n"
            "API keys can also be set as environment variables — see .env.example."
        )
    raw = _load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is empty or not a valid YAML mapping.")

    entries = []
    for field_path, raw_value in _iter_leaf_strings(raw.get("api_keys") or {}):
        provider, field = field_path[0], ".".join(field_path[1:])
        if (
            isinstance(raw_value, str)
            and raw_value.startswith("${")
            and raw_value.endswith("}")
        ):
            var_name = raw_value[2:-1]
            prov = _env_var_provenance(snap, var_name)
            active = prov["active_value"]
            if active is None:
                source = "unset"
            elif prov["in_dotenv_file"]:
                source = "env_overrides_os_env" if prov["mismatched"] else "env"
            else:
                source = "os_env"
            entries.append(
                {
                    "provider": provider,
                    "field": field,
                    "var_name": var_name,
                    "masked_value": mask_secret(active) if active else "(NOT SET)",
                    "source": source,
                }
            )
        else:
            entries.append(
                {
                    "provider": provider,
                    "field": field,
                    "var_name": None,
                    "masked_value": mask_secret(raw_value),
                    "source": "literal",
                }
            )
    return entries


def load_publication_config(publication_name, config_dir="configs"):
    validate_publication_name(publication_name)
    path = Path(config_dir) / f"{publication_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Publication config not found at {path}.\n"
            f"Create configs/{publication_name}.yaml based on configs/publication.example.yaml.\n"
            "Example configs are in configs/examples/."
        )
    config = _load_yaml(path)
    if not isinstance(config, dict):
        raise ValueError(f"{path} is empty or not a valid YAML mapping.")
    config = _resolve_env_recursive(config, env=_EFFECTIVE_ENV)
    _validate_publication_config(config, publication_name)
    _validate_publication_keys(config, publication_name)
    return config


def _validate_user_config(config):
    missing = []
    for key_path in REQUIRED_USER_KEYS:
        # Gemini API key is not required when using Vertex AI (which uses google-auth instead).
        if key_path == ("api_keys", "gemini", "api_key"):
            gemini_model_raw = config.get("models", {}).get("gemini", {})
            if (
                isinstance(gemini_model_raw, dict)
                and gemini_model_raw.get("provider") == "vertex_ai"
            ):
                continue
        if _get_nested(config, *key_path) is None:
            missing.append(" -> ".join(key_path))
    if missing:
        raise ValueError(
            "User config is missing required fields:\n"
            + "\n".join(f"  {m}" for m in missing)
            + "\nSee configs/user.example.yaml for the expected structure."
        )


def _validate_publication_config(config, publication_name):
    missing = []
    for key_path in REQUIRED_PUB_KEYS:
        if _get_nested(config, *key_path) is None:
            missing.append(" -> ".join(key_path))
    if missing:
        raise ValueError(
            f"Publication config '{publication_name}' is missing required fields:\n"
            + "\n".join(f"  {m}" for m in missing)
            + "\nSee configs/publication.example.yaml for the expected structure."
        )


#: Every top-level key a publication config may carry. Authoritative, not
#: advisory: an unrecognised key is now an error, so this list and the keys the
#: pipeline actually reads have to stay in step. ``TestKnownKeysCoverWhatIsRead``
#: fails if code starts reading one that is missing here.
KNOWN_PUB_KEYS = frozenset(
    {
        "api_keys",
        "audience",
        "author_name",
        "citation_sources",
        "custom_domains",
        "languagetool",
        "publication_description",
        "publication_name",
        "rank_math",
        "seo_rules",
        "style_profile",
        "style_rules",
        "voice_profile",
        "wordpress",
    }
)

#: Escape hatch for keys that are deliberately not ours — a note to a
#: colleague, a value some other tool reads out of the same file. Anything
#: under this prefix is passed over without validation.
#:
#: Borrowed from OpenAPI's ``x-`` specification extensions, which exist for
#: exactly this problem: a strict schema that still has to be extensible.
#: Spelled ``x_`` rather than ``x-`` only to match the snake_case these configs
#: already use. (RFC 6648 deprecated ``X-`` for *protocol headers*; that is a
#: different argument about wire formats and does not reach config schemas,
#: where the OpenAPI convention is current.)
_EXTENSION_PREFIX = "x_"

#: How close a spelling has to be before it is called a probable typo rather
#: than an unknown key. ``difflib`` rather than a hand-rolled edit distance:
#: stdlib, and a ratio cutoff is the standard way to spell "close enough to be
#: a typo, far enough not to be a different word".
_TYPO_CUTOFF = 0.85


def _unknown_key_message(key, publication_name):
    """The error text for one unrecognised key, typo-aware."""
    close = difflib.get_close_matches(key, KNOWN_PUB_KEYS, n=1, cutoff=_TYPO_CUTOFF)
    if close:
        return (
            f"  {key!r} — did you mean {close[0]!r}? As written, the pipeline "
            f"reads {close[0]!r} as absent and silently falls back to its "
            f"default."
        )
    return f"  {key!r} — not a key this pipeline reads."


def _validate_publication_keys(config, publication_name):
    """Reject top-level keys the pipeline does not read.

    A key that is *almost* right is the expensive one, and it used to cost
    nothing to get wrong: ``authorname`` reads as absent, the pipeline falls
    back to no author, and citation verification loses first-person checking
    with no error, no warning and no missing-field message anywhere. A plain
    unknown key — ``byline`` — is the same failure without even a near spelling
    to catch it.

    **This rejects, so the list above must stay complete.** Two things keep it
    honest: a test cross-checks it against the keys the source actually reads,
    and every shipped example config is validated by the suite. A key that is
    deliberately not the pipeline's belongs under ``x_`` (see
    ``_EXTENSION_PREFIX``), which is never validated.
    """
    unknown = [
        key
        for key in config
        if key not in KNOWN_PUB_KEYS and not str(key).startswith(_EXTENSION_PREFIX)
    ]
    if not unknown:
        return
    listed = "\n".join(
        _unknown_key_message(k, publication_name) for k in sorted(unknown)
    )
    plural = "" if len(unknown) == 1 else "s"
    raise ValueError(
        f"Publication config '{publication_name}' has {len(unknown)} key{plural} "
        f"the pipeline does not read:\n"
        f"{listed}\n"
        f"\nValid keys: {', '.join(sorted(KNOWN_PUB_KEYS))}\n"
        f"\nIf a key is deliberately not this pipeline's, prefix it with "
        f"'{_EXTENSION_PREFIX}' and it will be ignored without complaint."
    )


# ---------------------------------------------------------------------------
# Cost presets
# ---------------------------------------------------------------------------
#
# Presets select model variants, reasoning flags, and thoroughness as a bundle
# so users can dial cost vs coverage with a single pipeline.cost_preset value.
#
# Preset definitions are loaded from configs/presets.yaml at runtime so model
# names can be updated without editing Python. The YAML is the single source of
# truth — there is no duplicate table here (audit finding 14).
#
# Behavior when cost_preset is set:
#   - Sets thoroughness (unless the user also set thoroughness explicitly).
#   - Overrides model name and reasoning flags for each configured provider.
#   - Preserves user's infrastructure settings: provider, project, location,
#     credentials_file, endpoint, deployment, api_version, prompts, web_search.
#   - Skips providers the user has not configured (no API key / no models entry).
#   - Respects enabled: false set by the user.
#
# Keys that identify provider infrastructure or per-model tuning that the user
# controls independently of which cost_preset is active.  These are preserved from
# user config even when cost_preset overrides model names and reasoning flags.
_INFRA_KEYS = frozenset(
    {
        "provider",
        "endpoint",
        "deployment",
        "api_version",
        "project",
        "location",
        "credentials_file",
        "prompts",
        "timeout_seconds",  # user-tuned HTTP timeout; preserved so long articles don't time out
        # Which domains may run a live web search. Same category as `prompts`:
        # the user decides what a model is allowed to do, the preset decides how
        # expensive a variant it runs. Without this the preset rebuilt the model
        # config and dropped it, so `web_search` set alongside any cost_preset —
        # which is every non-default configuration — silently never took effect.
        "web_search",
    }
)


def _load_presets_from_yaml(config_dir=None):
    """Load cost presets from the packaged configs/presets.yaml.

    Resolves the path relative to this module (not the CWD) so presets load
    correctly regardless of where the pipeline is invoked from — matching the
    behavior of ci_core/llm/cost.py and model_registry.py.

    Raises PackagedConfigError if the file is missing or malformed. There is no
    hardcoded fallback: the duplicate it replaced had to be edited in lockstep
    with the YAML, and in the only state it could have fired — a broken install
    — quietly running a stale preset is worse than saying so.
    """

    if config_dir is None:
        config_dir = Path(__file__).parent / "configs"
    yaml_path = Path(config_dir) / "presets.yaml"
    try:
        data = load_packaged_yaml(yaml_path)
        # Validate top-level structure: each key must be a dict with a 'models' sub-dict.
        result = {}
        for preset_name, preset_body in data.items():
            if not isinstance(preset_body, dict):
                continue
            models_raw = preset_body.get("models", {})
            # Normalise: each provider entry must be a dict (YAML may give it as a mapping)
            models_normalised = {}
            for provider, cfg in models_raw.items():
                if isinstance(cfg, dict):
                    models_normalised[provider] = cfg
                elif cfg is None:
                    models_normalised[provider] = {}
            result[preset_name] = {
                "thoroughness": preset_body.get("thoroughness", "standard"),
                "models": models_normalised,
            }
        if not result:
            raise PackagedConfigError(
                f"{yaml_path}: contains no usable preset definitions"
            )
        return result
    except PackagedConfigError:
        # A missing or malformed packaged file is a broken install, not a
        # runtime condition to survive — quietly running a stale preset gives
        # the user wrong models and wrong costs with no indication why.
        raise
    except Exception as exc:
        raise PackagedConfigError(
            f"{yaml_path}: could not be parsed as cost presets ({exc})"
        ) from exc


#: Cost presets that no longer exist, and what now runs in their place.
#:
#: `standard` was retired 2026-09-05 because `wide` dominated it on every axis
#: measured over three isolated runs each: 33% more strongly-corroborated
#: consensus flags, 74% more fact-check claims, findings that survive a rerun
#: 67% of the time against 50%, and all of it at 55% of the cost. Keeping a tier
#: that costs more for less is worse than the churn of retiring it.
#:
#: Mapped rather than deleted. A hard removal turns an existing
#: `cost_preset: standard` into a crash at config-load, and a config that has
#: been working for months is the worst place to discover a naming decision.
#: The substitution is not silent: it is a real behaviour change (six models
#: instead of five, twelve calls instead of seven, roughly half the cost), so it
#: warns every run until the config is updated.
_RETIRED_PRESETS = {"standard": "wide"}


def resolve_preset_name(preset_name):
    """Map a retired preset name to its replacement.

    Returns ``(name, note)`` — ``note`` is None for a live preset, or the
    deprecation message to warn with when the name has been retired.
    """
    replacement = _RETIRED_PRESETS.get(preset_name)
    if not replacement:
        return preset_name, None
    return replacement, (
        f"cost_preset {preset_name!r} has been retired and is running as "
        f"{replacement!r}. That is a real change, not a rename: {replacement!r} "
        f"runs six models over twelve calls where {preset_name!r} ran five over "
        f"seven, and costs roughly half as much. Measured 2026-09-05 — see "
        f"configs/presets.yaml. Update cost_preset to {replacement!r} to silence "
        f"this."
    )


def retired_preset_names():
    """Preset names that still parse but no longer name a tier of their own."""
    return list(_RETIRED_PRESETS)


def preset_names(config_dir=None):
    """Every cost preset defined in the packaged presets.yaml, in file order.

    The CLI's ``--cost-preset`` choices come from here rather than a list
    repeated in the argument parser. That duplicate meant a preset added to
    presets.yaml — the file whose own header says to edit it — loaded, resolved
    and applied correctly, then got rejected by argparse as an invalid choice.
    File order is preserved because it runs cheapest to most expensive, which is
    the order the ``--help`` output should show.
    """
    return list(_load_presets_from_yaml(config_dir))


def _apply_cost_preset(pipeline_cfg, models_raw, user_set=None):
    """Apply a cost_preset to pipeline and models config.

    Called in merge_configs() before model normalization.  Returns updated
    (pipeline_cfg, models_raw) dicts; the originals are not mutated.
    """
    preset_name = pipeline_cfg.get("cost_preset")
    if not preset_name:
        return pipeline_cfg, models_raw

    preset_name, retired_note = resolve_preset_name(preset_name)
    if retired_note:
        log.warning(retired_note)

    presets = _load_presets_from_yaml()
    preset = presets.get(preset_name)
    if not preset:
        valid = ", ".join(f"'{k}'" for k in presets)
        raise ValueError(f"Unknown cost_preset {preset_name!r}. Valid values: {valid}")

    new_pipeline = dict(pipeline_cfg)
    # The name that actually ran, so the report's Ensemble Width block names the
    # preset whose models are in the call log rather than the retired alias.
    new_pipeline["cost_preset"] = preset_name
    # Set thoroughness from the preset unless the user asked for one by name.
    #
    # "Did the user set it" cannot be answered from ``pipeline_cfg``, because by
    # the time this runs the defaults have been merged in and `thoroughness` is
    # always present. Checking the merged dict is what broke `cost_preset`
    # between #105 and here: every preset picked its models correctly and then
    # ran a `standard`-sized ensemble, so `maximum` made 7 calls where it should
    # make 30 — maximum-model prices for a fraction of the ensemble, silently.
    # ``user_set`` is the raw pipeline block, before defaults.
    asked_for = pipeline_cfg if user_set is None else user_set
    if "thoroughness" not in asked_for:
        new_pipeline["thoroughness"] = preset["thoroughness"]

    merged_models = dict(models_raw or {})
    preset_models = preset.get("models", {})

    for provider, preset_cfg in preset_models.items():
        user_val = merged_models.get(provider)
        # Skip providers the user has not configured at all.
        if user_val is None:
            continue

        user_dict = {"model": user_val} if isinstance(user_val, str) else dict(user_val)

        # Respect user's explicit enabled: false.
        if user_dict.get("enabled") is False:
            continue

        # Preset disabling a provider in this cost tier.
        if preset_cfg.get("enabled") is False:
            merged_models[provider] = {**user_dict, "enabled": False}
            continue

        # New config: start from preset values; overlay user's infra settings on top.
        new_cfg = dict(preset_cfg)
        for key in _INFRA_KEYS:
            if key in user_dict:
                new_cfg[key] = user_dict[key]

        merged_models[provider] = new_cfg

    return new_pipeline, merged_models


def _apply_preset_overrides(pipeline_cfg, models_raw):
    """Apply user's selective preset_overrides on top of the already-preset models.

    Called after ``_apply_cost_preset()``.  Lets users tweak individual settings
    from a preset without rebuilding the entire config:

    .. code-block:: yaml

        pipeline:
          cost_preset: balanced
          preset_overrides:
            openai:
              reasoning_effort: high    # bump from preset's "low"
            claude:
              model: claude-opus-4-8    # use opus instead of preset's sonnet
              effort: high

    Rules:
    - Only providers already in ``models_raw`` are affected — overrides for
      unconfigured providers are ignored.
    - Infrastructure keys (provider, project, location, …) can be set here
      but are usually already correct from user.yaml.
    - An override of ``enabled: false`` disables the provider for this run.
    """
    overrides = (pipeline_cfg or {}).get("preset_overrides")
    if not overrides:
        return models_raw

    if not isinstance(overrides, dict):
        raise ValueError(
            "pipeline.preset_overrides must be a mapping of provider names to "
            "their override settings.  See configs/user.example.yaml for examples."
        )

    merged = dict(models_raw or {})
    for provider, override_cfg in overrides.items():
        if provider not in merged:
            continue  # provider not configured; skip silently
        current = (
            dict(merged[provider])
            if isinstance(merged[provider], dict)
            else {"model": merged[provider]}
        )
        current.update(override_cfg)
        merged[provider] = current

    return merged


#: Pipeline defaults, applied key by key rather than as a whole-block fallback.
#:
#: These used to be the second argument to ``user_config.get("pipeline", ...)``,
#: which only applies when the key is absent entirely. Any *partial* block
#: therefore discarded all of them: ``pipeline: {cost_preset: maximum}`` — an
#: entirely reasonable thing to write — left ``task_timeout_seconds`` as ``None``,
#: which is a ``TypeError`` inside ``timeout_model.compute_timeout``, and
#: ``retry_on_failure`` as ``None``, silently disabling retries.
#:
#: ``task_timeout_seconds`` is 180 here to preserve the previous no-config
#: behaviour. Note that ``user.example.yaml`` ships **1100**, and anyone starting
#: from ``ci-setup`` gets that; this value only governs a config that omits the
#: key, where failing fast is the safer default.
PIPELINE_DEFAULTS = {
    "parallel_review_calls": True,
    "retry_on_failure": True,
    "retry_delay_seconds": 10,
    "recovery_passes": 1,
    "recovery_delay_seconds": 30,
    "abort_if_all_provider_calls_fail": False,
    "task_timeout_seconds": 180,
    "thoroughness": "standard",
}


def _merge_api_keys(user_keys, pub_keys):
    """Publication-level api_keys override user.yaml's, per provider/field —
    a partial override (e.g. one provider, one field) leaves everything else
    from user.yaml untouched, same "per-key" philosophy as pipeline config
    merging just below. This is the second tier of this project's precedence
    (CLI override > publication config file > user.yaml/.env > OS
    environment variable); user.yaml is the fallback every publication
    shares, not something each one has to repeat.
    """
    merged = {
        k: dict(v) if isinstance(v, dict) else v for k, v in (user_keys or {}).items()
    }
    for provider, fields in (pub_keys or {}).items():
        if isinstance(fields, dict) and isinstance(merged.get(provider), dict):
            merged[provider] = {**merged[provider], **fields}
        else:
            merged[provider] = fields
    return merged


def merge_configs(user_config, pub_config):
    # Per-key, so a partial block keeps the defaults it did not mention.
    user_pipeline = user_config.get("pipeline") or {}
    pipeline = {**PIPELINE_DEFAULTS, **user_pipeline}
    models_raw = user_config.get("models", {})

    # Apply cost_preset when present — overrides model variants, reasoning flags,
    # and thoroughness while preserving user's provider infrastructure settings.
    pipeline, models_raw = _apply_cost_preset(
        pipeline, models_raw, user_set=user_pipeline
    )

    # Apply selective preset_overrides on top — lets users adjust individual
    # fields of a preset without specifying the full config.
    models_raw = _apply_preset_overrides(pipeline, models_raw)

    return {
        "api_keys": _merge_api_keys(
            user_config.get("api_keys", {}), pub_config.get("api_keys", {})
        ),
        "pipeline": pipeline,
        "delta": user_config.get(
            "delta",
            {
                "word_change_threshold_pct": 15,
                "claim_change_triggers_rerun": True,
                "structure_change_triggers_rerun": True,
            },
        ),
        "ensemble": user_config.get("ensemble", {}),
        "models": _normalize_model_configs(models_raw),
        "publication": pub_config,
    }


#: Every field --api-key can address, per provider. The five review models
#: have one (`api_key`); languagetool needs a username alongside its key, and
#: archive_org splits into an access/secret pair — both still addressable,
#: just via the explicit PROVIDER.FIELD form instead of the PROVIDER
#: shorthand (which always means the common case, `api_key`).
_PROVIDER_FIELDS = {
    "openai": {"api_key"},
    "gemini": {"api_key"},
    "mistral": {"api_key"},
    "grok": {"api_key"},
    "perplexity": {"api_key"},
    "claude": {"api_key"},
    "languagetool": {"username", "api_key"},
    "archive_org": {"access_key", "secret_key"},
}


def parse_api_key_overrides(cli_values):
    """Parse repeated ``--api-key`` arguments into ``{(provider, field): value}``
    — the CLI tier of this project's credential precedence (CLI > publication
    config file > user.yaml/.env > OS environment variable), applied last by
    :func:`apply_api_key_overrides`.

    Two forms:

    - ``PROVIDER=VALUE`` — shorthand for the common case, sets ``api_key``.
    - ``PROVIDER.FIELD=VALUE`` — for a credential with more than one field
      (``languagetool.username``, ``archive_org.secret_key``, etc.).

    Raises ``ValueError`` (not a silent no-op) on a malformed entry or an
    unrecognized provider/field, so a typo fails at startup rather than as a
    confusing 401 partway through a paid run.
    """
    overrides = {}
    for raw in cli_values or []:
        key, sep, value = raw.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"--api-key must be PROVIDER=VALUE or PROVIDER.FIELD=VALUE, got {raw!r}"
            )
        provider, _, field = key.partition(".")
        field = field or "api_key"
        valid_fields = _PROVIDER_FIELDS.get(provider)
        if valid_fields is None:
            raise ValueError(
                f"--api-key does not recognize provider {provider!r} — "
                f"expected one of {', '.join(sorted(_PROVIDER_FIELDS))}."
            )
        if field not in valid_fields:
            raise ValueError(
                f"--api-key does not support {provider}.{field} — "
                f"{provider} accepts {', '.join(sorted(valid_fields))}."
            )
        overrides[(provider, field)] = value
    return overrides


def apply_api_key_overrides(config, overrides):
    """Apply CLI-level ``{(provider, field): value}`` overrides to an
    already-merged config's ``api_keys`` — the last, highest-precedence step.
    Returns a new config dict; ``config`` itself is left unmodified.
    """
    if not overrides:
        return config
    api_keys = {
        k: dict(v) if isinstance(v, dict) else v for k, v in config["api_keys"].items()
    }
    for (provider, field), value in overrides.items():
        api_keys[provider] = {**api_keys.get(provider, {}), field: value}
    return {**config, "api_keys": api_keys}


def apply_wordpress_overrides(config, username=None, application_password=None):
    """CLI-level overrides for WordPress credentials (``--wp-user`` /
    ``--wp-password``) — same precedence principle as ``--api-key``, applied
    to ``publication.wordpress`` since that's where those fields live rather
    than under ``api_keys``. A publication config already *is* the
    "publication tier" for WordPress (there's no user.yaml-level default to
    override), so this adds the one tier above it: a one-off run using
    different WordPress credentials without editing the publication config.

    Returns a new config dict; ``config`` itself is left unmodified. A
    ``None`` argument leaves that field as the publication config set it.
    """
    if username is None and application_password is None:
        return config
    pub_config = config["publication"]
    wordpress = dict(pub_config.get("wordpress", {}))
    if username is not None:
        wordpress["username"] = username
    if application_password is not None:
        wordpress["application_password"] = application_password
    return {**config, "publication": {**pub_config, "wordpress": wordpress}}
