"""Shared YAML/env config helpers.

These three functions are the supported cross-package contract for loading the
YAML config files both ``ci-article-review`` and ``ci-style-profile`` read.
They used to live as private helpers in ``ci_article_review.config_loader``
(``_load_yaml``, ``_resolve_env_recursive``, ``_normalize_model_configs``) and
were imported across the package boundary by their private names; moving them
here gives them a real public API.

The app-specific parts of config loading — required-key validation, publication
configs, cost presets — deliberately stay in ``ci_article_review.config_loader``.
"""

import os
from pathlib import Path

import yaml

# Default provider for each adapter when the models section of user.yaml uses
# the simple string form.
DEFAULT_PROVIDERS = {
    "gemini": "ai_studio",
    "openai": "openai",
    "mistral": "mistral",
    "grok": "grok",
    "claude": "anthropic",
    "perplexity": "perplexity",
}


def resolve_env(value, env=None):
    """Resolve a single ``${ENV_VAR}`` placeholder to its environment value.

    Non-placeholder values pass through untouched. Raises ``ValueError`` when
    the referenced variable is not set, so a missing credential fails loudly at
    config-load time rather than as an opaque 401 mid-run.

    ``env`` is the mapping to look ``ENV_VAR`` up in, defaulting to
    ``os.environ`` for backward compatibility. A caller that wants a ``.env``
    file's value to win over a same-named OS environment variable (see
    ``ci_core.env_provenance.effective_env``) passes that merged mapping
    instead — this function itself has no opinion on precedence, it just
    reads whatever mapping it's given.
    """
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        lookup = env if env is not None else os.environ
        resolved = lookup.get(env_key)
        if resolved is None:
            raise ValueError(
                f"Environment variable {env_key!r} is not set. "
                f"Add it to your .env file or set it in the shell."
            )
        return resolved
    return value


def resolve_env_recursive(obj, env=None):
    """Apply :func:`resolve_env` to every string in a nested dict/list tree.

    ``env`` is passed straight through to :func:`resolve_env` — see there for
    what it's for.
    """
    if isinstance(obj, dict):
        return {k: resolve_env_recursive(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_env_recursive(i, env) for i in obj]
    if isinstance(obj, str):
        return resolve_env(obj, env)
    return obj


def load_yaml(path):
    """Read a YAML file, raising ``ValueError`` with the path on a parse error."""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Config file {path} contains invalid YAML:\n  {e}") from e


def normalize_model_configs(models_raw):
    """Normalize the ``models`` section of user.yaml to always be dicts.

    Two forms are accepted — both are valid, and simple strings remain the
    default so existing user.yaml files need no changes:

    Simple form (backward-compatible)::

        models:
          gemini: gemini-2.5-flash
          openai: gpt-4o

    Extended form (enables provider switching)::

        models:
          gemini:
            provider: vertex_ai
            model: gemini-2.5-flash
            project: my-gcp-project
            location: us-central1
          openai:
            provider: azure
            model: gpt-5.6-terra
            endpoint: https://my-resource.openai.azure.com
            deployment: my-gpt5-deployment
            api_version: 2025-04-01-preview
          mistral:
            provider: azure
            model: mistral-large-latest
            endpoint: https://Mistral-Large-abc.eastus2.inference.ai.azure.com

    After normalization every entry is a dict with at least ``provider`` and
    ``model`` keys.  The rest of the system only has to deal with dicts.
    """
    if not models_raw or not isinstance(models_raw, dict):
        return {}
    result = {}
    for adapter, value in models_raw.items():
        if isinstance(value, str):
            result[adapter] = {
                "provider": DEFAULT_PROVIDERS.get(adapter, adapter),
                "model": value,
            }
        elif isinstance(value, dict):
            normalized = dict(value)
            if "provider" not in normalized:
                normalized["provider"] = DEFAULT_PROVIDERS.get(adapter, adapter)
            result[adapter] = normalized
        # None or unexpected type — omit silently; adapter will use its built-in default.
    return result


def has_credentials(model_name, api_key, model_cfg):
    """Whether ``model_name`` has what it authenticates with.

    An API key, except for gemini on Vertex AI, which authenticates with a
    Google Cloud service account and needs a project instead: its AI Studio key
    may be absent altogether. The review pipeline decides which models run by
    this, and the citation re-ask which models it can hand a claim back to.
    Here rather than in either, because the re-ask is an adapter and adapters
    do not import the pipeline.
    """
    if model_name == "gemini" and (model_cfg or {}).get("provider") == "vertex_ai":
        return bool(model_cfg.get("project"))
    return bool(api_key)


class PackagedConfigError(RuntimeError):
    """A YAML file shipped inside the package is missing or unreadable."""


def load_packaged_yaml(path):
    """Load a config YAML that ships inside the package, or raise.

    These files (`pricing.yaml`, `timeouts.yaml`, `model_registry.yaml`,
    `presets.yaml`) are packaged data resolved relative to their own module, so
    they are present in every working install. Each used to have a duplicate
    hardcoded copy in Python as a fallback, kept in sync by a parity test — four
    pairs, every edit made twice, on the config surface that changes most often
    (provider pricing and model names move constantly).

    The fallbacks guarded a state that cannot occur while the package works, and
    in that state degrading silently is worse than failing: the user gets quietly
    stale pricing instead of a message telling them the install is broken. So
    this raises, and the duplicates are gone.
    """
    path = Path(path)
    if not path.exists():
        raise PackagedConfigError(
            f"Packaged config file is missing: {path}\n"
            "This ships inside the package, so its absence means a broken or "
            "partial install. Re-run `uv sync` (or reinstall the package)."
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        raise PackagedConfigError(
            f"Packaged config file could not be parsed: {path}\n{exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PackagedConfigError(f"Packaged config file is not a YAML mapping: {path}")
    return data
