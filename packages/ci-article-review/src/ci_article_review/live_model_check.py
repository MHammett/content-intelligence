"""Whether a newer model shipped than the one this run actually used.

``ci_core.llm.model_registry`` answers a narrower question: is the configured
model listed in a hand-maintained table of known-superseded IDs. That table is
only ever as current as the last person to edit it, so it cannot tell you that
you ran ``gpt-5.5`` and ``gpt-5.6`` shipped last week — nobody has written
``gpt-5.6`` down yet. The report inherited that blind spot.

``discover.py`` already asks the providers themselves. What it lacked was a way
into the run report: it is a separate command someone has to remember to run.
This module is that way in.

Why the answer is cached
------------------------
A live sweep is six HTTP requests to six providers, any of which can hang,
rate-limit, or answer with something unexpected. It would sit inside a pipeline
that already makes about thirty model calls, and none of this is worth adding
one failure mode to that run. So:

* Sweeps are cached on disk with a TTL. Inside the TTL a run reads a local file
  and makes no network call at all.
* ``ci-discover`` writes the same cache. A manual sweep is therefore reused by
  the next run for free, and that path needs no configuration at all — it is
  what makes the default (below) more than nothing.
* Refreshing the cache *from a run* is opt-in (``live_model_check: true``),
  because refreshing is the only part that adds latency to the run.
* Nothing here raises. Every failure resolves to a status the report states in
  words, because "we could not check" and "you are on the newest model" must
  never render the same way.

What it deliberately does not do
--------------------------------
It does not recommend an upgrade. Newer is not better, and it is often not
cheaper. A model released last week is also the one ``pricing.yaml`` is least
likely to know, and the pipeline would then price it at the conservative
unknown-model fallback rather than its real rate — so the report says whether
the price is known instead of quoting a number it made up. What shipped, when,
and what we know about it; the decision stays with the reader. Same posture as
``analysis/seo_suggest.py``.
"""

import datetime
import json
import logging
from pathlib import Path

from ci_core.llm.cost import known_price

from . import discover

log = logging.getLogger(__name__)

#: Where a sweep is cached. Relative to the working directory, like
#: ``pipeline.HISTORY_ROOT`` — the pipeline is run from the directory holding
#: ``configs/`` and ``pipeline_history/``, and this belongs with those.
CACHE_PATH = Path(".cache") / "model_discovery.json"

#: How long a cached sweep stays usable. Catching an announcement the same hour
#: is not what this feature promises; catching one from last week is, and a day
#: old costs nothing to be sure of.
DEFAULT_MAX_AGE_HOURS = 24

#: Bumped if the cache layout changes. An unrecognized version is discarded
#: rather than parsed hopefully — a stale schema read as a current one would
#: report on models nobody offers.
_CACHE_VERSION = 1


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _encode_models(models):
    """[(id, date|None)] -> [[id, "YYYY-MM-DD"|None]], JSON-safe."""
    encoded = []
    for model_id, date in models or []:
        encoded.append([str(model_id), date.isoformat() if date else None])
    return encoded


def _decode_models(raw):
    """The inverse. Anything malformed is dropped, not guessed at."""
    models = []
    for row in raw or []:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        model_id = str(row[0])
        date = None
        if len(row) > 1 and row[1]:
            try:
                date = datetime.date.fromisoformat(str(row[1]))
            except ValueError:
                date = None
        models.append((model_id, date))
    return models


def save_cache(collected, providers=None, path=None):
    """Persist a sweep. Returns the path written, or None if it could not be.

    Merges rather than replaces: ``ci-discover --provider openai`` sweeps one
    provider, and clobbering the other five with nothing would make a narrow
    manual check *reduce* what the next run can say.

    Each provider carries its own timestamp for the same reason — after a
    partial sweep, one entry is minutes old and the rest are yesterday's, and a
    single file-level timestamp would have to lie about one of them.
    """
    path = Path(path) if path else CACHE_PATH
    stamp = _now().isoformat()

    data = _read_cache_file(path) or {"version": _CACHE_VERSION, "providers": {}}
    entries = data.setdefault("providers", {})

    for provider_key, record in (collected or {}).items():
        if providers is not None and provider_key not in providers:
            continue
        entries[provider_key] = {
            "checked": stamp,
            "status": record.get("status", "error"),
            "reason": record.get("reason", ""),
            "detail": record.get("detail", ""),
            "static": bool(record.get("static")),
            "models": _encode_models(record.get("models")),
        }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        # A read-only or otherwise unwritable working directory is not a reason
        # to fail anything — the sweep still printed, the run still runs.
        log.debug("Could not write the model discovery cache to %s: %s", path, exc)
        return None
    return path


def _read_cache_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return None
    if not isinstance(data.get("providers"), dict):
        return None
    return data


def load_cache(path=None):
    """Return ``{provider_key: entry}`` from the cache; ``{}`` if unusable.

    Entries carry ``checked`` as an aware datetime and ``models`` decoded back
    into dates, ready to hand to ``discover.newer_than_configured``.
    """
    data = _read_cache_file(Path(path) if path else CACHE_PATH)
    if not data:
        return {}

    out = {}
    for provider_key, raw in data["providers"].items():
        if not isinstance(raw, dict):
            continue
        try:
            checked = datetime.datetime.fromisoformat(str(raw.get("checked")))
        except (TypeError, ValueError):
            continue
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=datetime.timezone.utc)
        out[str(provider_key)] = {
            "checked": checked,
            "status": raw.get("status", "error"),
            "reason": raw.get("reason", ""),
            "detail": raw.get("detail", ""),
            "static": bool(raw.get("static")),
            "models": _decode_models(raw.get("models")),
        }
    return out


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def models_that_ran(api_call_log):
    """``{provider_key: model_id}`` for providers that completed a review call.

    The report is about what this run *used*, which is not always what the
    config names: a pass that fell back ran a different model, and a provider
    whose every call failed contributed nothing to the report at all. Reading
    the call log rather than the config keeps the currency notice attached to
    the models whose output the reader is actually holding.

    Pass names are ``provider:domain``; the auxiliary passes (``seo_suggestions``
    and citation verification) carry no colon and are skipped, since they are
    not part of the configured review ensemble.
    """
    ran = {}
    for entry in api_call_log or []:
        if entry.get("failed"):
            continue
        pass_name = str(entry.get("pass") or "")
        if ":" not in pass_name:
            continue
        provider = pass_name.split(":", 1)[0]
        # "gpt-5.4 [FALLBACK from gpt-5.5] [grounded]" -> "gpt-5.4", the same
        # first-token rule cost.py applies to this field.
        model_id = str(entry.get("model") or "").split(" ", 1)[0]
        if provider and model_id:
            ran.setdefault(provider, model_id)
    return ran


def _finding(provider, model_id, entry):
    """One provider's answer, as the report needs it."""
    entry = dict(entry)
    entry["configured"] = model_id
    entry["configured_date"] = next(
        (d for mid, d in entry.get("models", []) if mid == model_id), None
    )
    newer = discover.newer_than_configured(entry)

    return {
        "provider": provider,
        "model": model_id,
        "checked": entry["checked"].isoformat() if entry.get("checked") else None,
        "newer": [
            {
                "model": m["model"],
                "released": m["released"].isoformat() if m["released"] else None,
                # Whether the pipeline could cost a run on it — not whether it
                # is worth switching to. pricing.yaml is hand-maintained and a
                # brand-new model is exactly what it has not caught up with.
                "price_known": known_price(m["model"]) is not None,
            }
            for m in newer
        ],
        # A model the provider lists but does not date cannot be compared, and
        # saying nothing would read as "nothing newer exists". Counted so the
        # report can say the comparison was partial.
        "undated_models": sum(
            1 for mid, d in entry.get("models", []) if d is None and mid != model_id
        ),
        "configured_date_known": entry["configured_date"] is not None,
    }


def _sweep_configs(ran, model_configs):
    """The models config ``collect_available_models`` is handed for ``ran``.

    It takes the models config shape, and the model that ran is what is to be
    compared, so that goes in as the configured id. Of the run's own config only
    an Azure route comes too, so the sweep skips that provider as ci-discover
    does: its key is an Azure key, and the sweep would otherwise hand it to
    OpenAI's or Mistral's model listing. Vertex is left out on purpose. Its
    AI Studio key, where there is one, belongs to the listing it would query.
    """
    configs = {}
    for provider, model_id in ran.items():
        entry = {"model": model_id}
        run_cfg = (model_configs or {}).get(provider)
        if isinstance(run_cfg, dict) and run_cfg.get("provider") == "azure":
            entry.update(provider="azure", endpoint=run_cfg.get("endpoint"))
        configs[provider] = entry
    return configs


def check(
    ran,
    api_keys,
    *,
    refresh=False,
    max_age_hours=DEFAULT_MAX_AGE_HOURS,
    cache_path=None,
    model_configs=None,
):
    """Report on models newer than the ones in ``ran``. Never raises.

    ``ran`` is ``{provider_key: model_id}`` — see ``models_that_ran``.
    ``model_configs`` is the run's models config, read only for which providers
    it sends to Azure: see ``_sweep_configs``.

    ``refresh`` allows this call to query providers whose cached answer is
    older than ``max_age_hours`` (or missing). With ``refresh=False`` the check
    is a local file read: it reports on whatever a previous ``ci-discover`` or
    refreshing run left behind, and reports the rest as unchecked.

    The returned dict separates three states that must not be collapsed::

        newer      — checked, and the provider offers something more recent
        current    — checked, and nothing it offers postdates what ran
        unchecked  — not checked, or checked and the answer was unusable

    ``unchecked`` is the one that matters most: an empty ``newer`` list means
    "no newer model found" only for the providers in ``current``.
    """
    cache_path = Path(cache_path) if cache_path else CACHE_PATH
    ran = ran or {}

    try:
        cached = load_cache(cache_path)
    except Exception as exc:  # noqa: BLE001 — advisory; a bad cache is not a run failure
        log.debug("Model discovery cache unreadable: %s", exc)
        cached = {}

    now = _now()
    cutoff = datetime.timedelta(hours=max_age_hours)

    def _is_fresh(entry):
        return entry is not None and (now - entry["checked"]) <= cutoff

    stale = [p for p in ran if not _is_fresh(cached.get(p))]

    refreshed = False
    if refresh and stale:
        try:
            swept = discover.collect_available_models(
                _sweep_configs(ran, model_configs),
                api_keys or {},
                providers=stale,
            )
        except Exception as exc:  # noqa: BLE001 — the sweep must never fail a run
            log.debug("Live model discovery failed: %s", exc)
            swept = {}
        if swept:
            refreshed = True
            save_cache(swept, providers=stale, path=cache_path)
            stamped = _now()
            for provider_key, record in swept.items():
                record = dict(record)
                record["checked"] = stamped
                cached[provider_key] = record

    newer, current, unchecked = [], [], []
    for provider, model_id in sorted(ran.items()):
        entry = cached.get(provider)
        if entry is None:
            unchecked.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "reason": "never checked",
                }
            )
            continue
        if entry.get("status") != "ok" or not entry.get("models"):
            unchecked.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "reason": _reason_text(entry),
                }
            )
            continue

        finding = _finding(provider, model_id, entry)
        if finding["newer"]:
            newer.append(finding)
        elif not finding["configured_date_known"]:
            # The provider answered, but not about this model — it lists no
            # release date for it, or does not list it at all. Nothing can be
            # compared, so this is not evidence of being current.
            unchecked.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "reason": (
                        f"{provider} lists no release date for {model_id!r}, so "
                        "nothing can be compared against it"
                    ),
                }
            )
        else:
            current.append(finding)

    ages = [
        (now - cached[p]["checked"]).total_seconds() / 3600.0
        for p in ran
        if p in cached
    ]

    return {
        "status": "ok" if (newer or current) else "unavailable",
        "source": "live" if refreshed else "cache",
        "refreshed": refreshed,
        "oldest_check_age_hours": round(max(ages), 1) if ages else None,
        "newer": newer,
        "current": current,
        "unchecked": unchecked,
    }


def _reason_text(entry):
    """Why a cached provider entry cannot be compared, in words."""
    reason = entry.get("reason") or ""
    detail = entry.get("detail") or ""
    if reason == "no_api_key":
        return "no API key configured for a model listing"
    if reason == "disabled":
        return "provider disabled"
    if reason == "vertex_ai":
        base = "configured via Vertex AI, whose model listing needs the gcloud SDK"
        return f"{base} ({detail})" if detail else base
    if reason == "azure":
        return (
            "configured via Azure, where what runs is a deployment rather than "
            "a provider catalogue"
        )
    if reason == "request_failed":
        base = "the models API could not be reached"
        return f"{base} ({detail})" if detail else base
    if reason.startswith("HTTP"):
        return f"the models API returned {reason}"
    if entry.get("status") == "ok":
        return "the provider returned no models"
    return reason or "not checked"
