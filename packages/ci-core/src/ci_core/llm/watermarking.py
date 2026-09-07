"""Provider text-watermarking registry.

Answers one question: does the provider that drafted this article mark its text
output? That is bookkeeping, not detection, and the distinction is the whole
point of the module.

Nothing here inspects text. Nothing here *can*. A statistical watermark is
recovered by re-running a keyed pseudorandom function over the token stream,
and without the provider's secret key watermarked text is statistically
indistinguishable from unmarked text — the scheme is designed to guarantee
exactly that. So a local detector is not a hard problem, it is a precluded one,
and a heuristic pretending otherwise would be unfalsifiable: wrong in ways its
author could never measure.

What is knowable is provenance. The author already declares the drafting model
in the handoff (``Drafted with:``), where the pipeline uses it to keep a model
from reviewing its own prose. This turns that same declaration into a reported
fact — "you drafted with a provider that marks its output, so the published
piece carries a mark you cannot verify locally" — and says plainly that it is
declared rather than measured.

Registry data lives in configs/watermarking.yaml so a provider changing its
mind does not need a code change, and carries a ``registry_date`` because these
facts move quickly: Article 50 came into application on 2026-08-02 and every
signatory to the EU Code of Practice is still shipping changes. Staleness is
surfaced rather than silently assumed away, the same way model_registry does it.

To update: edit configs/watermarking.yaml, bump ``registry_date``. No code change.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml

#: A provider we have no entry for is unknown, never "no". The registry records
#: absence of evidence as absence of evidence.
UNKNOWN = "unknown"

_VALID_STATUSES = frozenset({"yes", "no", "partial", UNKNOWN})

#: Statuses under which a published article should be assumed to carry a mark.
#: ``partial`` is included deliberately: an unresolved API path is a reason to
#: assume the mark is present, not a reason to assume it is absent.
_ASSUME_MARKED = frozenset({"yes", "partial"})


def _load_registry():
    """Load configs/watermarking.yaml, raising rather than falling back.

    No hardcoded duplicate of the table lives in this module. A second copy
    would drift, and drift here means telling an author their article is
    unmarked when it is not — the one error this module exists to prevent.
    """
    yaml_path = Path(__file__).parent.parent / "configs" / "watermarking.yaml"
    data = load_packaged_yaml(yaml_path)

    raw = data.get("providers")
    if not isinstance(raw, dict):
        raise PackagedConfigError(f"{yaml_path}: 'providers' must be a mapping")

    providers = {}
    for name, info in raw.items():
        if not isinstance(info, dict):
            raise PackagedConfigError(
                f"{yaml_path}: provider {name!r} must be a mapping"
            )
        status = str(info.get("status", UNKNOWN))
        if status not in _VALID_STATUSES:
            # A typo here silently downgrades to "unknown" under a plain .get(),
            # which reads as "we checked and could not tell" rather than "this
            # file is malformed". Fail instead.
            raise PackagedConfigError(
                f"{yaml_path}: provider {name!r} has status {status!r}; "
                f"expected one of {', '.join(sorted(_VALID_STATUSES))}"
            )
        providers[str(name).lower()] = {**info, "status": status}

    return providers, data.get("registry_date"), data


PROVIDERS, REGISTRY_DATE, _RAW = _load_registry()
STALE_NOTICE_DAYS = _RAW.get("stale_notice_days", 60)
STALE_WARNING_DAYS = _RAW.get("stale_warning_days", 120)


def registry_age_days(today=None):
    """Days since the registry was last verified, or None if undated."""
    if not REGISTRY_DATE:
        return None
    if isinstance(REGISTRY_DATE, datetime.date):
        stamped = REGISTRY_DATE
    else:
        try:
            stamped = datetime.date.fromisoformat(str(REGISTRY_DATE))
        except ValueError:
            return None
    return ((today or datetime.date.today()) - stamped).days


def staleness(today=None):
    """``("ok"|"notice"|"warning", age_days)`` for the registry as a whole.

    Reported alongside every lookup rather than logged once, because the answer
    a caller gets is only as good as the day the table was checked, and that
    caveat should travel with the answer.
    """
    age = registry_age_days(today)
    if age is None:
        return UNKNOWN, None
    if age >= STALE_WARNING_DAYS:
        return "warning", age
    if age >= STALE_NOTICE_DAYS:
        return "notice", age
    return "ok", age


def status_for(provider, today=None):
    """Return what is known about ``provider``'s text watermarking.

    ``provider`` is a provider key ("claude", "gemini", ...), matched
    case-insensitively; None or an empty string means undeclared. An unlisted
    provider comes back ``unknown`` with ``marked`` False — which asserts only
    that nothing is known, not that the text is clean.
    """
    key = (provider or "").strip().lower()
    entry = PROVIDERS.get(key)
    state, age = staleness(today)

    if not key:
        status = UNKNOWN
        note = "No drafting model declared, so nothing can be said either way."
    elif entry is None:
        status = UNKNOWN
        note = f"Provider {provider!r} is not in the watermarking registry."
    else:
        status = entry["status"]
        note = (entry.get("note") or "").strip()

    return {
        "provider": key or None,
        "status": status,
        "marked": status in _ASSUME_MARKED,
        "scope": (entry or {}).get("scope"),
        "since": (entry or {}).get("since"),
        "method": (entry or {}).get("method"),
        "note": note,
        "source": (entry or {}).get("source"),
        "registry_date": str(REGISTRY_DATE) if REGISTRY_DATE else None,
        "registry_staleness": state,
        "registry_age_days": age,
        # Restated on every lookup because it is the caveat most likely to be
        # dropped when a caller renders only the parts it finds interesting.
        "basis": "declared in the handoff, not measured from the text",
    }
