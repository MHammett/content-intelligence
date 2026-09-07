#!/usr/bin/env python3
"""
Article Review Pipeline — orchestration engine.

Usage:
  python pipeline.py --draft path/to/handoff.md --publication your_publication_name
  python pipeline.py --publish path/to/publication_handoff.md --publication your_publication_name [--publish-live]
"""

import sys

# Reconfigure stdout/stderr to UTF-8 on Windows (default cp1252 breaks on non-ASCII report content)
from ci_core.console import force_utf8_stdio

force_utf8_stdio()

# Version guard — must run before any other imports
if sys.version_info < (3, 10):
    print(f"ERROR: Python 3.10 or higher is required. You are running {sys.version}.")
    print("Download the latest Python at https://www.python.org/downloads/")
    sys.exit(1)

# Dependency check — catch missing pip packages before any work starts
_REQUIRED_PACKAGES = {
    "requests": "requests",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
}
_missing = []
for _import_name, _pkg_name in _REQUIRED_PACKAGES.items():
    try:
        __import__(_import_name)
    except ImportError:
        _missing.append(_pkg_name)
if _missing:
    print("ERROR: Required packages are not installed. Run:")
    print(f"  pip install {' '.join(_missing)}")
    print("Or install all dependencies at once:")
    print("  pip install -r requirements.txt")
    sys.exit(1)

import argparse
import logging
import re
import time

import requests
from datetime import datetime, timezone
from pathlib import Path

from .config_loader import (
    apply_api_key_overrides,
    apply_wordpress_overrides,
    load_user_config,
    load_publication_config,
    merge_configs,
    parse_api_key_overrides,
    validate_publication_name,
)
from .handoff_parser import (
    parse_draft_submission,
    parse_publication_handoff,
    build_handoff_from_raw_text,
    build_handoff_from_raw_draft_and_metadata,
)
from . import history as hist
from . import consolidation
from ci_core import redact
from ci_core.config_helpers import normalize_model_configs
from ci_core import llm
from . import schemas
from ci_core.concurrency import run_all_with_timeout
from ci_core.llm.model_registry import check_model_currency
from ci_core.llm import timeout_model
from . import live_model_check
from .analysis import readability as readability_analysis
from .analysis import seo as seo_analysis
from .analysis import seo_content
from .analysis import seo_suggest
from ci_core.llm import cost as cost_analysis
from .analysis.webpage import build_handoff_from_url
from . import fact_check_scope
from .adapters.citation import draft_citations
from .adapters.citation import wayback
from . import ensemble_capture

log = logging.getLogger("pipeline")

HISTORY_ROOT = "pipeline_history"

# Module-level prompt cache — files are read once per process lifetime.
_PROMPT_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Ensemble assignment system
# ---------------------------------------------------------------------------

#: Maps domain names to their prompt file.
_DOMAIN_PROMPTS: dict[str, str] = {
    "fact_check": "fact_check.txt",
    "voice_style": "ai_speak.txt",
    "completeness": "completeness.txt",
    "argument_integrity": "argument_integrity.txt",
    "red_team": "red_team.txt",
}

#: Providers this pipeline can call — the six the shared LLM layer routes.
_PROVIDERS: tuple[str, ...] = llm.PROVIDERS

#: Which models run which domains at each thoroughness level.
#: Models are listed in preference order; unconfigured ones are skipped.
_THOROUGHNESS_PRESETS: dict[str, dict[str, list[str]]] = {
    "standard": {
        # One primary model per domain — current baseline behavior.
        "fact_check": ["gemini"],
        "voice_style": ["openai"],
        "completeness": ["openai"],
        "argument_integrity": ["mistral", "claude"],
        "red_team": ["mistral", "grok"],
    },
    "thorough": {
        # Two to three well-suited models per domain.
        # fact_check limited to search-grounded models for quality.
        "fact_check": ["gemini", "perplexity"],
        "voice_style": ["openai", "claude"],
        "completeness": ["openai", "mistral"],
        "argument_integrity": ["mistral", "claude", "openai"],
        "red_team": ["mistral", "grok", "claude"],
    },
    "maximum": {
        # Every configured model runs every domain.
        # Domain-specific weights in consolidation sort the signal from the noise.
        "fact_check": ["gemini", "perplexity", "openai", "mistral", "grok", "claude"],
        "voice_style": ["gemini", "perplexity", "openai", "mistral", "grok", "claude"],
        "completeness": ["gemini", "perplexity", "openai", "mistral", "grok", "claude"],
        "argument_integrity": [
            "gemini",
            "perplexity",
            "openai",
            "mistral",
            "grok",
            "claude",
        ],
        "red_team": ["gemini", "perplexity", "openai", "mistral", "grok", "claude"],
    },
}


def _model_has_credentials(model_name: str, api_keys: dict, model_cfg: dict) -> bool:
    """Return True if the model has credentials to run."""
    if model_name == "gemini":
        if model_cfg.get("provider") == "vertex_ai":
            # Vertex AI uses google-auth; project ID is the required field.
            return bool(model_cfg.get("project"))
        return bool(api_keys.get("gemini", {}).get("api_key"))
    return bool(api_keys.get(model_name, {}).get("api_key"))


#: Domains the drafting model is not allowed to review.
#:
#: voice_style runs ai_speak.txt, which is a list of AI tells — hedging,
#: throat-clearing, vague significance gesturing, the problem→cause→solution
#: skeleton. Asking the model that wrote the draft to find those is asking it
#: to notice its own defaults, and it under-reports them.
#:
#: Deliberately just this one. A model re-reading its own reasoning in
#: argument_integrity has a similar conflict, but a far weaker one: that prompt
#: asks whether the logic holds, not whether the prose carries the model's own
#: fingerprints. Widening this list costs real review coverage, so it should
#: only grow on evidence that a domain is actually compromised.
_DRAFTER_EXCLUDED_DOMAINS: tuple[str, ...] = ("voice_style",)


def _history_key(handoff: dict) -> str:
    """Return the key naming this article's history directory.

    The directory is normally slugged from the title, which makes the title the
    de-facto primary key for an article's history. That breaks the moment the
    title is revised: "…They Have Eight of Them." became "…Ten of Them." and
    then "…Twelve of Them.", and one article ended up spread over three
    directories. Nothing errors — the runs simply stop finding each other, so
    delta comparison loses its baseline and voice_pattern_report counts one
    article as three distinct ones when deciding whether a phrasing habit
    recurs across the body of work.

    ``History key:`` in the handoff pins the directory to something the author
    controls and can keep stable while the title moves. Absent, the title is
    used exactly as before.
    """
    return (handoff.get("history_key") or "").strip() or handoff.get("title", "")


def _drafting_model(handoff: dict, pipeline_cfg: dict) -> str | None:
    """Return the model that drafted the article, or None if undeclared.

    Two sources, because they answer different questions. ``pipeline.
    drafting_model`` in user.yaml is the default for someone who always drafts
    the same way; a ``Drafted with:`` line in the handoff states it for one
    article and wins when both are present, since the drafting tool can change
    between pieces while the config does not.

    An unrecognised name is a no-op with a warning rather than an error: the
    cost of a typo here is a review pass that should have been dropped, and
    failing the whole run over it would be worse.
    """
    declared = (handoff.get("drafted_with") or "").strip() or (
        pipeline_cfg.get("drafting_model") or ""
    ).strip()
    if not declared:
        return None

    name = declared.lower()
    if name not in _PROVIDERS:
        log.warning(
            "Declared drafting model %r is not one of %s — no review pass will "
            "be excluded. Check 'Drafted with:' in the handoff, or "
            "pipeline.drafting_model in user.yaml.",
            declared,
            ", ".join(sorted(_PROVIDERS)),
        )
        return None
    return name


def _drafter_is_excluded(model_name: str, domain: str, drafting_model: str | None):
    """True when this pair is the drafting model judging its own prose."""
    return (
        drafting_model is not None
        and model_name == drafting_model
        and domain in _DRAFTER_EXCLUDED_DOMAINS
    )


def _warn_on_domains_left_unreviewed(assignments, drafting_model: str | None) -> None:
    """Warn when excluding the drafter leaves a domain with no reviewer at all.

    At ``maximum`` thoroughness every model runs every domain, so this cannot
    happen. At ``standard`` it can: voice_style is one model, and if that model
    drafted the article the domain empties. Silently shipping a report whose
    voice section is empty because nobody ran it — indistinguishable, in the
    output, from nobody finding anything — is the failure worth being loud
    about.
    """
    if drafting_model is None:
        return
    covered = {domain for _, domain in assignments}
    for domain in _DRAFTER_EXCLUDED_DOMAINS:
        if domain not in covered:
            log.warning(
                "No model is reviewing '%s': %s drafted this article and is "
                "excluded from it, and no other model was assigned. That "
                "section will be empty because it never ran, not because the "
                "draft is clean. Add another model to %s, or raise "
                "thoroughness.",
                domain,
                drafting_model,
                domain,
            )


def _model_blocked_reason(model_name: str, cfg: dict, api_keys: dict) -> str | None:
    """Return why a model can run no domain at all, or None if it can run.

    Model-level gates only. A ``prompts:`` override narrows which domains a
    model runs but never stops it running, so it is not checked here.
    """
    if not cfg.get("enabled", True):
        return "disabled in config (enabled: false)"
    if not _model_has_credentials(model_name, api_keys, cfg):
        return "no credentials configured"
    return None


def _prompt_override(cfg: dict) -> list[str] | None:
    """Return the domains a model's ``prompts:`` override limits it to.

    ``None`` means no override is set — the model runs whatever the preset
    asks of it. An empty list (``prompts:`` written with no value) means the
    override admits no domains at all.
    """
    if "prompts" not in cfg:
        return None
    return cfg["prompts"] or []


def _build_assignments(
    thoroughness: str,
    model_configs: dict,
    api_keys: dict,
    drafting_model: str | None = None,
    skips: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Return list of (model_name, domain) pairs to execute.

    ``model_configs`` accepts either form documented in user.example.yaml —
    simple (``openai: gpt-5.5``) or extended (a dict). The pipeline hands over
    configs already normalised by config_loader; normalising again here is what
    lets a direct call with a hand-written config behave the same way.

    Assignment logic:
    1. Start with the thoroughness preset.
    2. Skip models that are disabled (enabled: false).
    3. Skip models that have no credentials.
    4. Respect per-model prompt overrides (models.<name>.prompts:) — if set,
       that model only runs those domains regardless of the preset.
    5. Drop the drafting model from the domains it cannot judge.
    6. Deduplicate (model, domain) pairs.

    Pass a list as ``skips`` to find out what steps 2-5 dropped: every model the
    preset (or an explicit ``prompts:`` entry) asked for but that contributes
    fewer calls than asked appends one line naming it, the reason, and the
    domains it did not run. Without that, an ensemble smaller than the preset
    implies looks identical to one the preset never asked for.
    """
    model_configs = normalize_model_configs(model_configs)
    preset = _THOROUGHNESS_PRESETS.get(thoroughness, _THOROUGHNESS_PRESETS["standard"])
    assignments: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # What each model was asked to run, in preset order — the denominator every
    # skip line below is measured against.
    requested: dict[str, list[str]] = {}
    for domain, model_list in preset.items():
        for model_name in model_list:
            requested.setdefault(model_name, []).append(domain)
    # An explicit prompts: entry can name domains the preset never asked this
    # model for; the second loop below assigns those, so they count as requested.
    for model_name, cfg in model_configs.items():
        for domain in _prompt_override(cfg) or []:
            if domain in _DOMAIN_PROMPTS and domain not in requested.get(
                model_name, []
            ):
                requested.setdefault(model_name, []).append(domain)

    # Model-level verdicts, resolved once per model rather than once per pair,
    # so the assignment loops and the skip report cannot disagree.
    blocked: dict[str, str] = {}
    for model_name in requested:
        reason = _model_blocked_reason(
            model_name, model_configs.get(model_name, {}), api_keys
        )
        if reason:
            blocked[model_name] = reason

    for domain, model_list in preset.items():
        for model_name in model_list:
            if model_name in blocked:
                continue
            # Per-model prompt override: skip this domain if not in the list
            allowed = _prompt_override(model_configs.get(model_name, {}))
            if allowed is not None and domain not in allowed:
                continue
            if _drafter_is_excluded(model_name, domain, drafting_model):
                continue
            pair = (model_name, domain)
            if pair not in seen:
                seen.add(pair)
                assignments.append(pair)

    # Handle explicit per-model prompts for models not covered by the preset
    # (e.g. a model configured with prompts: [fact_check] that isn't in standard preset)
    for model_name, cfg in model_configs.items():
        if model_name in blocked:
            continue
        for domain in _prompt_override(cfg) or []:
            if _drafter_is_excluded(model_name, domain, drafting_model):
                continue
            pair = (model_name, domain)
            if pair not in seen and domain in _DOMAIN_PROMPTS:
                seen.add(pair)
                assignments.append(pair)

    _warn_on_domains_left_unreviewed(assignments, drafting_model)

    if skips is not None:
        for model_name, domains in requested.items():
            reason = blocked.get(model_name)
            if reason:
                skips.append(
                    f"{model_name} — {reason}; {len(domains)} domain(s) not run: "
                    + ", ".join(domains)
                )
                continue
            allowed = _prompt_override(model_configs.get(model_name, {}))
            by_override = [
                d for d in domains if allowed is not None and d not in allowed
            ]
            # Same precedence as the loops above, which apply the override
            # first: a domain it already dropped is not re-attributed here.
            by_drafter = [
                d
                for d in domains
                if d not in by_override
                and _drafter_is_excluded(model_name, d, drafting_model)
            ]
            groups = []
            if by_override:
                groups.append(
                    f"{', '.join(by_override)} "
                    f"(prompts: override limits it to {allowed})"
                )
            if by_drafter:
                groups.append(
                    f"{', '.join(by_drafter)} "
                    f"(drafted this article — it cannot judge its own prose)"
                )
            if groups:
                skips.append(
                    f"{model_name} — {len(by_override) + len(by_drafter)} "
                    "domain(s) not run: " + "; ".join(groups)
                )

    return assignments


def _build_custom_assignments(
    pub_config,
    model_configs,
    api_keys,
    skips: list[str] | None = None,
):
    """Return (assignments, prompts_by_domain) for publication-defined custom domains.

    Each entry in pub_config.custom_domains has:
      prompt:      inline prompt string  (one of these is required)
      prompt_file: path to a .txt file
      models:      list of model names to run for this domain
      weight:      ensemble weight (optional, default 1.0)

    ``model_configs`` accepts either form documented in user.example.yaml —
    simple (``openai: gpt-5.5``) or extended (a dict). The pipeline hands over
    configs already normalised by config_loader; normalising again here is what
    lets a direct call with a hand-written config behave the same way.

    Pass a list as ``skips`` to find out which of the models a custom domain
    named it did not get: every model held back by ``enabled: false`` or by
    missing credentials appends one line naming it, the reason, and the custom
    domains it did not run. Unknown model names and unresolvable prompts already
    warn for themselves; those two gates were the silent ones, so a domain
    configured for three models could come back with one and say nothing.

    Returns:
      assignments       — list of (model_name, domain_name) tuples
      prompts_by_domain — dict[domain_name, prompt_str]
    """
    custom_domains = pub_config.get("custom_domains") or {}
    model_configs = normalize_model_configs(model_configs)
    assignments = []
    prompts_by_domain = {}
    # model -> (reason, custom domains it was asked for but cannot run). The
    # reason is resolved where the drop happens, so the assignment loop and the
    # skip report below cannot drift apart, and domains accumulate across the
    # whole config so a model named by three of them is one line — the same
    # one-line-per-model shape _build_assignments reports.
    blocked: dict[str, tuple[str, list[str]]] = {}

    for domain_name, cfg in custom_domains.items():
        if not isinstance(cfg, dict):
            log.warning("custom_domains.%s must be a mapping — skipping", domain_name)
            continue

        # Resolve prompt text
        if cfg.get("prompt"):
            prompt_str = cfg["prompt"]
        elif cfg.get("prompt_file"):
            p = Path(cfg["prompt_file"])
            if not p.exists():
                log.warning(
                    "Custom domain %r: prompt_file %r not found — skipping",
                    domain_name,
                    str(p),
                )
                continue
            prompt_str = p.read_text(encoding="utf-8")
        else:
            log.warning(
                "Custom domain %r has no prompt or prompt_file — skipping", domain_name
            )
            continue

        prompts_by_domain[domain_name] = prompt_str

        for model_name in cfg.get("models") or []:
            if model_name not in _PROVIDERS:
                log.warning(
                    "Custom domain %r: unknown model %r — skipping",
                    domain_name,
                    model_name,
                )
                continue
            model_cfg = model_configs.get(model_name, {})
            reason = _model_blocked_reason(model_name, model_cfg, api_keys)
            if reason:
                dropped = blocked.setdefault(model_name, (reason, []))[1]
                if domain_name not in dropped:
                    dropped.append(domain_name)
                continue
            pair = (model_name, domain_name)
            if pair not in assignments:
                assignments.append(pair)

    if skips is not None:
        for model_name, (reason, dropped) in blocked.items():
            skips.append(
                f"{model_name} — {reason}; {len(dropped)} custom domain(s) "
                "not run: " + ", ".join(dropped)
            )

    return assignments, prompts_by_domain


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    if name not in _PROMPT_CACHE:
        # Prompts are packaged alongside this module, so resolve relative to the
        # package — not the current working directory (which broke when the code
        # moved into packages/ci-article-review and is invoked as a module).
        path = Path(__file__).parent / "prompts" / name
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        _PROMPT_CACHE[name] = path.read_text(encoding="utf-8")
    return _PROMPT_CACHE[name]


def _render_prompt(template: str, **kwargs) -> str:
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value) if value else "")
    return template


_CACHE_LAYOUT_SYSTEM = (
    "You are a meticulous editorial reviewer. The article and its context come "
    "first; your specific review task is stated at the end of this message. "
    "Follow that task exactly and return only the JSON it specifies."
)


def _cache_friendly_layout(system: str, user: str) -> tuple[str, str, str]:
    """Move the domain instruction after the draft so the prefix is shareable.

    Returns ``(system, user, cacheable_prefix)`` with the per-domain text
    relocated to the end of
    ``user``. The model still receives the entire domain prompt — only its
    position changes — and putting the task after a long document is a normal
    long-context arrangement rather than an exotic one.

    The shared prefix is ``_CACHE_LAYOUT_SYSTEM`` plus the article, which is
    byte-identical across all five domains of a run.
    """
    # The prefix is returned rather than left to be re-derived: whoever
    # marks the cache breakpoint has to split at exactly this point, and
    # two copies of that boundary would drift apart.
    separator = "=" * 60
    prefix = f"{user}\n\n{separator}\nYOUR REVIEW TASK\n{separator}\n"
    return _CACHE_LAYOUT_SYSTEM, prefix + system, prefix


def _web_search_enabled(setting, domain: str) -> bool:
    """Resolve a model's ``web_search`` setting for one domain.

    Accepts either shape:

    ``web_search: true``          — every domain searches (the original meaning,
                                    kept so existing configs are unaffected).
    ``web_search: [fact_check]``  — only the listed domains search.

    Anything else is falsy and disables search, so a typo fails closed rather
    than quietly billing a search on all five domains.
    """
    if isinstance(setting, str):
        # A bare string is almost certainly one domain name rather than a
        # truthy value; treating it as a one-element list is what the author
        # meant, and `bool("fact_check")` being True on every domain is not.
        return domain == setting
    if isinstance(setting, (list, tuple, set)):
        return domain in setting
    return bool(setting)


def _build_user_prompt(draft: str, handoff: dict) -> str:
    parts = [f"ARTICLE TITLE: {handoff['title']}\n"]
    if handoff.get("target_audience"):
        parts.append(f"TARGET AUDIENCE: {handoff['target_audience']}\n")
    if handoff.get("primary_claim"):
        parts.append(f"PRIMARY CLAIM: {handoff['primary_claim']}\n")
    if handoff.get("pre_draft_analysis"):
        parts.append(f"PRE-DRAFT ANALYSIS:\n{handoff['pre_draft_analysis']}\n")
    if handoff.get("sources_cited"):
        parts.append(f"SOURCES ALREADY CITED:\n{handoff['sources_cited']}\n")
    if handoff.get("uncertain_sections"):
        parts.append(
            f"UNCERTAIN SECTIONS (author-flagged — focus scrutiny here):\n"
            f"{handoff['uncertain_sections']}\n"
        )
    if handoff.get("known_gaps"):
        parts.append(
            f"KNOWN GAPS (author is already aware of these — don't just restate "
            f"them, assess whether they're acceptable or need closing):\n"
            f"{handoff['known_gaps']}\n"
        )
    if handoff.get("additional_context"):
        parts.append(f"ADDITIONAL CONTEXT:\n{handoff['additional_context']}\n")
    parts.append(f"\nDRAFT:\n{draft}")
    return "\n".join(parts)


# Matches a URL embedded in a fact-check "source" field, which is often free
# text like "Publisher Name, Article Title, https://example.com/path" rather
# than a bare URL.
_SOURCE_URL_RE = re.compile(r"https?://\S+")

#: Models frequently emit the source as a markdown link rather than a bare URL.
#: A real run produced `[www.cbc.ca](https://www.cbc.ca)`, and the bare-URL
#: regex captured the whole construct — the fetch then failed against a
#: hostname of literally "[www.cbc.ca]". Take the link target when we see one.
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*(https?://[^\s)]+)\s*\)")


def _extract_source_url(source_field: str) -> str | None:
    """Pull an embedded URL out of a fact-check item's "source" field, if any.

    The field is free text — models write "Publisher, Title, https://..." or a
    markdown link, or occasionally both. Markdown is checked first because its
    target is unambiguous, where the bare-URL pattern would swallow the
    surrounding syntax.
    """
    if not source_field:
        return None
    m = _MARKDOWN_LINK_RE.search(source_field)
    if m:
        return m.group(1).rstrip(".,;:\"'")
    m = _SOURCE_URL_RE.search(source_field)
    if not m:
        return None
    return m.group(0).rstrip(".,;:)\"'")


#: Fact-check buckets fed into citation resolution, mapped to the item key that
#: may carry a source URL for that bucket.
#:
#: ``confirmed`` is here because those are the claims that ship *as written*.
#: Leaving it out meant the pipeline verified and archived the claims the author
#: was about to change, and did nothing for the ones about to be published —
#: which inverts the risk. ``primary_source_needed`` names its URL field
#: ``best_candidate_source``, not ``source``; reading the wrong key meant the one
#: bucket that exists to say "here is where to find the primary source" was the
#: one bucket whose candidate was never fetched.
_CITATION_CLAIM_BUCKETS = {
    "confirmed": "source",
    "outdated": "source",
    "contradicted": "source",
    "unverifiable": None,
    "primary_source_needed": "best_candidate_source",
    # Present so a claim classified out of scope but *not* excluded — a category
    # this publication does not honour on a model's say-so — is still resolved,
    # exactly as ``unverifiable`` is. The entries that ARE excluded are filtered
    # out before this loop; see ``_collect_citation_claims``.
    "out_of_scope": None,
}


_CLAIM_STOPWORDS = frozenset(
    "the a an of in to and is are was were that for on at by its with as from".split()
)

#: Token-overlap threshold above which two claims are treated as the same one.
#: Calibrated on the 2026-08-12 run (144 claims): 0.9 collapses 7 and every pair
#: it merges is a genuine restatement; 0.8 collapses 12 but starts reaching for
#: claims that differ in a material number. Set high deliberately — merging two
#: distinct claims silently drops one from verification, which is worse than
#: verifying a near-duplicate twice.
_CLAIM_SIMILARITY = 0.9


def _claim_key(claim: str) -> frozenset:
    """Content words of a claim, for identity comparison."""
    words = re.sub(r"[^a-z0-9 ]", " ", claim.lower()).split()
    return frozenset(w for w in words if w not in _CLAIM_STOPWORDS)


def _is_duplicate_claim(key: frozenset, seen_keys: list) -> bool:
    """True if ``key`` restates a claim already collected.

    Exact match after normalisation catches the common case — the same sentence
    with a trailing period, or a leading "The". The Jaccard pass catches the rest:
    five models independently paraphrasing one fact.
    """
    if not key:
        return False
    for other in seen_keys:
        if key == other:
            return True
        union = len(key | other)
        if union and len(key & other) / union >= _CLAIM_SIMILARITY:
            return True
    return False


def _record_fact_check_degradation(report: dict, results: dict) -> None:
    """Record that a failed fact-check pass left Sections 2 and 9 incomplete.

    A failed pass in one place quietly degrades another, and nothing in the
    output used to connect the two: the run summary said
    "perplexity:fact_check FAILED" in one place and "9 verified" in another,
    and the drop read as an unexplained regression rather than a known
    consequence.

    **The mechanism this describes is not the one it originally described.**
    Until citations resolved against the draft's own sources, a failed
    fact-check pass cost Section 9 its *grounded-search URLs* — that pass was
    their only supplier, and losing it took citation resolution from 48% to 22%
    in the 2026-08-12 run. Grounded URLs are gone; claims are now anchored to
    the draft's citation block, which no model pass supplies. What a failure
    costs now is the *claims themselves*: every claim only that pass would have
    raised is missing from the fact-check results and therefore from citation
    resolution too. Narrower in effect, wider in scope — it degrades Section 2
    as well, which the old warning never said.
    """
    failed = sorted(
        f"{model}:{domain}"
        for (model, domain), r in results.items()
        if domain == "fact_check" and r.get("failed") and not r.get("skipped")
    )
    if not failed:
        return
    total = sum(1 for (_model, domain) in results if domain == "fact_check")
    detail = (
        f"Sections 2 and 9 are working from an incomplete claim list: "
        f"{len(failed)} of {total} fact-check pass(es) failed "
        f"({', '.join(failed)}). Any claim only those passes would have raised "
        f"is missing from the fact-check results, and so was never put through "
        f"citation resolution either. Counts in both sections are lower than a "
        f"clean run would produce — re-run once the provider recovers before "
        f"treating them as final."
    )
    log.warning("Citations: %s", detail)
    report.setdefault("degradations", []).append(
        {
            "section": "SECTION 2: Factual Verification, SECTION 9: Citations",
            "caused_by": failed,
            "detail": detail,
        }
    )


def _collect_citation_claims(fact_check: dict, draft: str) -> list[dict]:
    """Build the Pass 3 claim list from consolidated fact-check output.

    Every bucket contributes its claims.

    **Which URLs a claim is checked against.** The draft's own citation for the
    claim comes first: Section 9 audits the article's citations, so "does the
    source you cited say this" is the question worth answering. See
    ``draft_citations`` for how a claim is traced back to a marker. The URL named
    in the claim's own source field follows as a fallback, for claims the draft
    attaches no citation to.

    What used to sit in that fallback slot was a *response-level* grounded URL —
    the first entry of the provider's search-results list for the whole response,
    which is not about any particular claim. In one real run that stamped a
    single LBNL energy report onto 44 unrelated claims and produced 47 false
    "the source does not mention this" findings around two real ones. A claim
    with no traceable citation now gets no URL, which is both true and useful:
    "the draft cites nothing here" is something an author can act on.

    **Deduplication is on claim *meaning*, not exact text.** It used to be exact
    text, which meant five models paraphrasing one fact produced five claims: the
    2026-08-12 run carried 29 near-duplicate pairs among 144 claims, one differing
    from its twin only by a trailing full stop. Each duplicate bought its own
    resolution fetch, its own verification call, and its own line in Section 9.

    **Claims marked out of scope never reach this list.** Resolving a claim no
    source can settle cannot produce a finding — only a false negative, since
    the answer is always "the page does not say this". They are seeded into the
    dedup set first so a *second* model's verdict on the same claim cannot let
    it back in through another bucket. See
    :mod:`ci_article_review.fact_check_scope`.
    """
    cited = draft_citations.DraftCitations(draft)
    if cited:
        log.info(
            "Citations: draft citation block parsed — %d marker(s) available "
            "for claim anchoring",
            cited.marker_count,
        )
    else:
        log.info(
            "Citations: no citation block found in the draft; claims fall back "
            "to the source URL named by the fact-check model"
        )

    claims: list[dict] = []
    seen_keys: list[frozenset] = []
    anchored = 0

    excluded = [
        item
        for item in fact_check.get("out_of_scope") or []
        if item.get("excluded") and item.get("claim")
    ]
    for item in excluded:
        seen_keys.append(_claim_key(item["claim"]))
    if excluded:
        log.info(
            "Citations: %d claim(s) held out of resolution as out of scope for "
            "verification (%s)",
            len(excluded),
            ", ".join(sorted({item.get("excluded_by", "?") for item in excluded})),
        )

    for bucket, url_key in _CITATION_CLAIM_BUCKETS.items():
        for item in fact_check.get(bucket, []) or []:
            claim = item.get("claim", "")
            if not claim:
                continue
            if bucket == "out_of_scope" and item.get("excluded"):
                continue
            key = _claim_key(claim)
            if _is_duplicate_claim(key, seen_keys):
                continue
            seen_keys.append(key)
            source_field = item.get(url_key, "") if url_key else ""
            known_urls = cited.candidates_for(claim, source_field)
            if known_urls:
                anchored += 1
            # ``source_url`` is a dedicated field the prompt asks for outright;
            # ``source`` is free text a URL has to be dug out of. Prefer the
            # explicit one and fall back to scraping, so a model that answers
            # the new schema is not held to the old one — and one that ignores
            # it still resolves exactly as before.
            own = _extract_source_url(item.get("source_url", "")) or (
                _extract_source_url(source_field) if url_key else None
            )
            if own and own not in known_urls:
                known_urls = known_urls + [own]
            claims.append(
                {
                    "claim": claim,
                    "known_urls": known_urls,
                    "fact_check_bucket": bucket,
                }
            )
    log.info(
        "Citations: %d of %d claim(s) traced to a citation in the draft",
        anchored,
        len(claims),
    )
    return claims


def _read_handoff_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Handoff file not found: {path}\nCheck the path and try again."
        )
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        raise OSError(f"Cannot read handoff file {path}: {e}") from e


# ---------------------------------------------------------------------------
# Generic domain runner
# ---------------------------------------------------------------------------


def _run_domain(
    model_name: str,
    domain: str,
    draft: str,
    handoff: dict,
    pub_config: dict,
    api_keys: dict,
    pipeline_cfg: dict,
    model_configs: dict,
    prompt_str: str | None = None,
    scope=None,
) -> dict:
    """Call one model on one domain and return the adapter result dict.

    The result is tagged with ``_model`` and ``_domain`` so the caller can
    build the keyed results dict without re-parsing the runner name string.
    prompt_str overrides the built-in prompt file for custom publication domains.
    """
    template = (
        prompt_str if prompt_str is not None else _load_prompt(_DOMAIN_PROMPTS[domain])
    )
    style = pub_config.get("style_rules", {})
    system = _render_prompt(
        template,
        publication_description=pub_config.get("publication_description", ""),
        audience=str(pub_config.get("audience", {})),
        # ci-style-profile emits `style_profile`; fall back to the legacy
        # `voice_profile` key so existing publication.yaml files keep working.
        voice_profile=pub_config.get("style_profile")
        or pub_config.get("voice_profile", ""),
        banned_words=", ".join(style.get("banned_words", [])),
        banned_phrases=", ".join(style.get("banned_phrases", [])),
        positive_rules="\n".join(f"- {r}" for r in style.get("positive_rules", [])),
        primary_claim=handoff.get("primary_claim", ""),
        pre_draft_analysis=handoff.get("pre_draft_analysis", ""),
        # Only fact_check's prompt has this placeholder, so every other domain
        # renders unchanged. Telling the model *and* filtering its output is
        # deliberate: the instruction saves the search spend, the filter is what
        # actually holds when a model ignores it.
        out_of_scope_passages=scope.prompt_block() if scope is not None else "",
    )
    user = _build_user_prompt(draft, handoff)

    # Off by default: it relocates the domain instruction from before the
    # article to after it, and this pipeline's output is the product. Turn it
    # on, diff a run against a prior live run, and keep it if the findings hold.
    cache_prefix = None
    if pipeline_cfg.get("prompt_cache_layout", False):
        system, user, cache_prefix = _cache_friendly_layout(system, user)

    api_key = api_keys.get(model_name, {}).get("api_key", "")

    # Live web search is a per-model flag, but only one domain has any use for
    # it: fact_check, where it replaces training recall with a live-fetched
    # source. voice_style matches the draft against a voice profile, and
    # completeness, argument_integrity and red_team all reason about the draft
    # in front of them — none are improved by fetching a page, and every one of
    # them bills per search anyway. Resolving the flag here, where the domain is
    # known, turns five paid search contexts per run into one. A bare
    # `web_search: true` still means every domain, so existing configs keep
    # their behaviour.
    provider_config = dict(model_configs.get(model_name, {}))
    if "web_search" in provider_config:
        provider_config["web_search"] = _web_search_enabled(
            provider_config["web_search"], domain
        )

    result = llm.call_provider(
        model_name,
        system,
        user,
        api_key,
        retry=pipeline_cfg.get("retry_on_failure", True),
        retry_delay=pipeline_cfg.get("retry_delay_seconds", 10),
        provider_config=provider_config,
        # Let the provider enforce the shape the prompt asks for. None for a
        # custom domain, which has a prompt but no declared schema — and for
        # gemini while grounded, which the LLM layer drops on its own.
        response_schema=schemas.for_domain(domain),
        # Where the shared article ends and this domain's task begins.
        # None unless the cache layout is on — without it there is no
        # shared prefix to point at.
        cache_prefix=cache_prefix,
    )
    result["_model"] = model_name
    result["_domain"] = domain
    return result


# ---------------------------------------------------------------------------
# Draft mode
# ---------------------------------------------------------------------------


def _stagger_offsets(runner_names, stagger_seconds):
    """Return ``{runner_name: seconds to wait before starting}``.

    Providers rate-limit per account, not per call, so firing all five of a
    provider's domain calls in the same instant makes them compete for one
    quota. Observed 2026-08-12: two Perplexity calls returned HTTP 429 within a
    second of each other, and the one whose retry also hit the limit failed
    outright — which then cost Section 9 every claim that pass would have
    raised. (It also cost Section 9 its grounded-search URLs at the time;
    citations now resolve against the draft's own sources, so that particular
    consequence no longer applies — see :func:`_collect_citation_claims`.)

    Only calls sharing a provider are spread; calls to *different* providers all
    start at 0, because the parallelism that matters is across providers, not
    within one. The cost is negligible — these calls run 60-400s, so a few
    seconds of offset is noise against the total.

    ``stagger_seconds`` of 0 disables it and every offset is 0.
    """
    offsets = {}
    seen: dict[str, int] = {}
    for name in runner_names:
        provider = name.split(":")[0]
        offsets[name] = stagger_seconds * seen.get(provider, 0)
        seen[provider] = seen.get(provider, 0) + 1
    return offsets


def _delay_start(fn, delay):
    """Wrap ``fn`` so it sleeps ``delay`` seconds before running."""
    if not delay:
        return fn

    def _delayed():
        time.sleep(delay)
        return fn()

    return _delayed


def _global_ceiling(per_task_timeouts, retry_delay):
    """Outer wall-clock bound for the whole parallel batch.

    Must sit comfortably above the slowest task's own timeout so that (a) a task's
    own timeout fires and is collected cleanly rather than being masked as a
    global-ceiling cancellation, and (b) a transient retry (which costs an extra
    retry_delay plus a second attempt) isn't prematurely killed. We allow the
    slowest timeout + one retry_delay + 30s of scheduling/propagation slack.
    """
    if not per_task_timeouts:
        return retry_delay + 30
    return max(per_task_timeouts) + retry_delay + 30


def _run_reviews_in_parallel(runners, pipeline_cfg, model_configs, task_timeout):
    """Fire every (model, domain) review call concurrently and collect results.

    Two budgets bound each call: its own per-model timeout (config key
    ``timeout_seconds``, falling back to the pipeline-level ``task_timeout``),
    and a shared global ceiling sized to comfortably clear the slowest one
    (see :func:`_global_ceiling`). Whichever fires first is recorded as that
    call's outcome; the other side of the race is simply abandoned rather than
    killed — a running thread cannot be killed, and stopping *waiting* on it is
    the whole point of a wall-clock backstop.

    Runs on :func:`ci_core.concurrency.run_all_with_timeout`, i.e. one daemon
    thread per call, not a ``ThreadPoolExecutor``. A ``with ThreadPoolExecutor``
    block's own exit — and the atexit hook ``concurrent.futures.thread``
    registers regardless of how you shut it down — joins every worker with a
    bare, untimed ``t.join()``, so one abandoned call held the interpreter open
    for exactly as long as it kept running: measured 2026-08-16 at ``ci-review``
    processes still alive two days after their report was written. Daemon
    threads can't do that; an abandoned one goes with the interpreter instead
    of holding it open.
    """

    def _per_model_timeout(runner_name):
        m = runner_name.split(":")[0]
        return model_configs.get(m, {}).get("timeout_seconds", task_timeout)

    def _configured_model_id(runner_name):
        m = runner_name.split(":")[0]
        return model_configs.get(m, {}).get("model", m)

    stagger = pipeline_cfg.get("provider_stagger_seconds", 3)
    offsets = _stagger_offsets([name for name, _ in runners], stagger)
    if any(offsets.values()):
        log.info(
            "Staggering same-provider calls by %ss to avoid self-inflicted "
            "rate limiting (max offset %ss)",
            stagger,
            max(offsets.values()),
        )

    def _own_timeout(runner_name):
        # None is a real, reachable value here — an explicit
        # `task_timeout_seconds: null` in user.yaml survives config_loader's
        # 180-default (dict.get only falls back when the key is *absent*, and
        # this key is present with value None), which is exactly the shape of
        # the PR #105 regression. run_all_with_timeout treats a None per-call
        # timeout as "no budget of its own" and falls back to the group's
        # global_ceiling, so this must not raise trying to add an offset to it.
        per = _per_model_timeout(runner_name)
        return per + offsets[runner_name] if per is not None else None

    # The offset is added to the budget, not spent from it: a staggered call
    # still gets the full timeout its model was calibrated for.
    runner_timeouts = [
        (name, _delay_start(fn, offsets[name]), _own_timeout(name))
        for name, fn in runners
    ]
    global_ceiling = _global_ceiling(
        [t for _, _, t in runner_timeouts if t is not None],
        pipeline_cfg.get("retry_delay_seconds", 10),
    )

    raw_results = {}
    for name, (value, error) in run_all_with_timeout(
        runner_timeouts, global_ceiling
    ).items():
        if error is None:
            raw_results[name] = value
            continue
        per_timeout = _per_model_timeout(name)
        timed_out = isinstance(error, TimeoutError)
        hit_global_ceiling = timed_out and "global timeout" in str(error).lower()
        if hit_global_ceiling:
            log.error(
                f"Review pass {name} exceeded global ceiling {global_ceiling}s and was cancelled."
            )
            raw_results[name] = {
                "failed": True,
                "error": f"Exceeded global timeout of {global_ceiling}s",
                "model": _configured_model_id(name),
                "tokens": {},
                # The call was cancelled at the global ceiling, so we don't know
                # the true elapsed time — record the ceiling as a lower bound
                # rather than leaving it None (breaks [CALIBRATION] log parsing).
                "elapsed_seconds": global_ceiling,
                "_model": name.split(":")[0],
                "_domain": name.split(":", 1)[1] if ":" in name else name,
            }
        else:
            log.error(
                f"Review pass {name} {'timed out after ' + str(per_timeout) + 's' if timed_out else 'raised exception: ' + str(error)}"
            )
            raw_results[name] = {
                "failed": True,
                "error": str(error),
                "model": _configured_model_id(name),
                "tokens": {},
                # Best available lower bound on elapsed time — the task ran
                # at least this long before being cancelled. Avoids "elapsed=Nones"
                # in the [CALIBRATION] log line for timed-out passes.
                "elapsed_seconds": per_timeout if timed_out else None,
                "_model": name.split(":")[0],
                "_domain": name.split(":", 1)[1] if ":" in name else name,
            }
    return raw_results


# Mirrors ci_core.llm.client._TERMINAL_QUOTA_MARKERS, but matched against the
# already-redacted `error` string a failed raw_results entry carries, not a
# live exception — a recovery pass never sees the original exception, only
# what _run_domain/_attempt already turned it into. Broader than the client's
# list because raw_results also surfaces auth/config failures (bad key,
# archived project) that never reach litellm as a quota error at all.
# Measured 2026-08-25: an archived OpenAI project failed all 5 domains
# identically on every attempt — retrying it costs money to learn nothing.
_PERMANENT_FAILURE_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "not_authorized",
    "archived",
    "authentication_error",
    "insufficient_quota",
    "no credits remaining",
    "insufficient credit",
    "exceeded your current quota",
    "billing",
    "payment required",
    "account is not active",
)


def _looks_permanent(error_text):
    """True when a retry cannot possibly fix this failure — skip recovery."""
    text = (error_text or "").lower()
    return any(marker in text for marker in _PERMANENT_FAILURE_MARKERS)


def _run_reviews_for_names(names, runners, pipeline_cfg, model_configs, task_timeout):
    """Run only the runners in ``names`` through the normal parallel batch.

    Shared by the recovery pass here and by ``--retry-failed``'s manual
    re-attempt — both need "the same fan-out machinery, restricted to a
    subset of calls" rather than a second implementation of it.
    """
    subset = [(name, fn) for name, fn in runners if name in names]
    return _run_reviews_in_parallel(subset, pipeline_cfg, model_configs, task_timeout)


def _recover_failed_calls(
    raw_results, runners, pipeline_cfg, model_configs, task_timeout
):
    """Re-attempt calls still marked failed, up to ``recovery_passes`` times.

    A run that comes back 28/30 calls costs the same as a clean one, and until
    now the only way to fill the gap was a full re-run. This retries just the
    gaps in place, on a coarser delay than the per-call retry in
    ``ci_core.llm.client`` — a "give the provider a breather" pause between
    whole passes, not a per-socket backoff. Calls ``_looks_permanent`` flags
    are skipped: retrying a dead account identically every pass spends money
    to learn nothing new.
    """
    recovery_passes = pipeline_cfg.get("recovery_passes", 1)
    recovery_delay = pipeline_cfg.get("recovery_delay_seconds", 30)
    originally_failed = {name for name, r in raw_results.items() if r.get("failed")}
    if not originally_failed or recovery_passes <= 0:
        return raw_results

    for pass_num in range(1, recovery_passes + 1):
        names_to_retry = [
            name
            for name, r in raw_results.items()
            if r.get("failed") and not _looks_permanent(r.get("error", ""))
        ]
        if not names_to_retry:
            break
        log.info(
            "Recovery pass %d/%d: retrying %d failed call(s): %s",
            pass_num,
            recovery_passes,
            len(names_to_retry),
            ", ".join(sorted(names_to_retry)),
        )
        time.sleep(recovery_delay)
        raw_results.update(
            _run_reviews_for_names(
                names_to_retry, runners, pipeline_cfg, model_configs, task_timeout
            )
        )

    still_failed = [name for name, r in raw_results.items() if r.get("failed")]
    recovered = len(originally_failed) - len(still_failed)
    log.info(
        "Recovery summary: %d recovered, %d still failing out of %d originally failed.",
        recovered,
        len(still_failed),
        len(originally_failed),
    )
    return raw_results


def _merge_recovered_results(prior_results, retried_results):
    """Combine a ``--retry-failed`` capture with the calls just re-attempted.

    Previously-successful entries pass through unchanged. Previously-failed
    entries are replaced by whatever the new attempt produced — win or lose,
    the fresh result is more useful than the stale failure it replaces.
    """
    merged = dict(prior_results)
    merged.update(retried_results)
    return merged


def run_draft_pipeline(
    handoff_path,
    publication_name,
    config_dir="configs",
    cost_preset=None,
    no_timeout=False,
    only_model=None,
    only_domain=None,
    handoff=None,
    seo_suggestions=None,
    replay_results=None,
    retry_failed_results=None,
    offline=False,
    api_key_overrides=None,
):
    """Run the full draft review pipeline.

    Normally ``handoff_path`` points at a handoff document that is read and
    parsed here. URL mode (and tests) may instead pass a pre-built ``handoff``
    dict — must contain at least ``title`` and ``draft`` — in which case the
    file read/parse step is skipped and the rest of the pipeline is shared.

    ``seo_suggestions=False`` suppresses the SEO suggestion call for this run
    only, overriding the publication config's ``seo_rules.suggestions``. None
    (the default) leaves the decision to the config.

    ``api_key_overrides`` (``{(provider, field): value}``, from ``--api-key``
    on the CLI) is the highest tier of this project's credential precedence —
    CLI > publication config file > user.yaml/.env > OS environment variable
    — and is applied last, after the other three have already been merged.

    ``retry_failed_results`` (a path, from ``--retry-failed``) is the manual
    counterpart to the automatic recovery pass: it loads a prior run's capture,
    makes model calls only for the (model, domain) pairs marked failed in it,
    and merges the new attempts back over everything that already succeeded.
    Unlike ``replay_results`` this still spends money and still writes a new,
    distinct ``run_N`` — it fills gaps, it does not replay a whole run for free.
    """
    t_start = time.monotonic()

    log.info(f"Loading configs (publication={publication_name})")
    user_config = load_user_config(config_dir)
    pub_config_raw = load_publication_config(publication_name, config_dir)
    if cost_preset:
        # CLI override for this run only — does not modify user.yaml on disk.
        user_config.setdefault("pipeline", {})["cost_preset"] = cost_preset
        log.info(f"cost_preset overridden via --cost-preset: {cost_preset}")
    config = merge_configs(user_config, pub_config_raw)
    if api_key_overrides:
        config = apply_api_key_overrides(config, api_key_overrides)
        log.info(
            "api_key overridden via --api-key for: %s",
            ", ".join(f"{p}.{f}" for p, f in sorted(api_key_overrides)),
        )

    # Model currency check — runs against the fully resolved (post-preset) model config.
    currency = check_model_currency(config.get("models", {}))
    for w in currency["warnings"]:
        log.warning(
            f"Model currency: {w['provider']} is using {w['model']!r}, "
            f"which has been superseded by {w['replacement']!r}. "
            + (w["note"] + ". " if w["note"] else "")
            + "Update user.yaml to use the newer model."
        )
    if currency["registry_warning"]:
        log.warning(
            f"Model registry is {currency['registry_age_days']} days old "
            f"(last updated {currency['registry_date']}). "
            "Provider APIs change frequently — re-check available models and pricing."
        )
    elif currency["registry_stale"]:
        log.info(
            f"Model registry last updated {currency['registry_date']} "
            f"({currency['registry_age_days']} days ago). "
            "Consider re-checking for newer models."
        )

    api_keys = config["api_keys"]
    pipeline_cfg = config["pipeline"]
    pub_config = config["publication"]
    delta_cfg = config["delta"]
    ensemble_cfg = config.get("ensemble", {})
    model_configs = config.get("models", {})
    task_timeout = pipeline_cfg.get("task_timeout_seconds", 180)
    thoroughness = pipeline_cfg.get("thoroughness", "standard")

    if handoff is None:
        log.info(f"Parsing draft submission: {handoff_path}")
        handoff_text = _read_handoff_file(handoff_path)
        handoff = parse_draft_submission(handoff_text)
    else:
        log.info(f"Using pre-built handoff: {handoff.get('title', 'Untitled')!r}")

    if not handoff["draft"]:
        log.error(
            "No DRAFT section found in handoff document. "
            "Ensure the document contains a 'DRAFT' header followed by the article text."
        )
        sys.exit(1)

    run_start_ts = datetime.now(timezone.utc)
    # archive.org pacing state is module-level and therefore process-wide. A
    # breaker tripped by an earlier run in this process would otherwise skip
    # every archive lookup here, reporting each citation as "rate limit tripped
    # earlier this run" for a limit that expired before this run began.
    wayback.reset_rate_limit_state()
    # The handoff declares the run number, so re-running without editing it
    # writes a second report with the same number — pipeline_history ended up
    # with two run_16_* files for one article, and the later one is not
    # obviously the later one. Trust the handoff unless history already has that
    # number, and say so rather than silently renumbering.
    run_number = handoff.get("run_number", 1)
    _existing = hist.existing_run_numbers(HISTORY_ROOT, _history_key(handoff))
    if run_number in _existing:
        _next = max(_existing) + 1
        log.warning(
            "Run %d already exists for this article; using %d instead. Update "
            "the handoff's 'Run:' line so the two agree.",
            run_number,
            _next,
        )
        run_number = _next
    article_title = handoff.get("title", "Untitled")
    lt_config = pub_config.get("languagetool", {})

    # Pass 1: LanguageTool grammar correction (optional)
    grammar_enabled = pipeline_cfg.get("grammar_pass", True)
    lt_creds = api_keys.get("languagetool", {})
    # A self-hosted server (server_url set) needs no auth. Otherwise this is
    # the hosted API, which does.
    lt_has_creds = bool(lt_creds.get("server_url")) or bool(
        lt_creds.get("username") and lt_creds.get("api_key")
    )

    if not grammar_enabled:
        log.info("Pass 1: Grammar pass disabled (grammar_pass: false) — skipping.")
        lt_result = {
            "failed": True,
            "skipped": True,
            # Both skip paths set skipped=True, and the summary used to print the
            # credentials message for either — telling an operator with working
            # credentials in .env to go and configure credentials. Record which.
            "skipped_reason": "disabled",
            "change_log": [],
            "flagged_matches": [],
        }
        corrected_draft = handoff["draft"]
    elif not lt_has_creds:
        log.info(
            "Pass 1: No LanguageTool credentials configured — skipping grammar pass."
        )
        lt_result = {
            "failed": True,
            "skipped": True,
            "skipped_reason": "no_credentials",
            "change_log": [],
            "flagged_matches": [],
        }
        corrected_draft = handoff["draft"]
    else:
        log.info("Pass 1: LanguageTool grammar correction")
        from .adapters.grammar import languagetool as lt

        lt_result = lt.run(
            handoff["draft"],
            lt_config,
            lt_creds.get("username"),
            lt_creds.get("api_key"),
            retry=pipeline_cfg.get("retry_on_failure", True),
            retry_delay=pipeline_cfg.get("retry_delay_seconds", 10),
            url=lt_creds.get("server_url") or lt.LANGUAGETOOL_API_URL,
        )
        if lt_result["failed"]:
            log.warning(
                f"LanguageTool failed ({lt_result.get('elapsed_seconds', '?')}s): "
                f"{lt_result.get('error')}. Proceeding with uncorrected draft."
            )
            corrected_draft = handoff["draft"]
        else:
            corrected_draft = lt_result["corrected_text"]
            log.info(
                f"LanguageTool: {len(lt_result['change_log'])} corrections "
                f"in {lt_result.get('elapsed_seconds', '?')}s."
            )

    # Pre-analysis: readability, link validation, SEO (no API calls required)
    log.info("Pre-analysis: readability, links, SEO")
    pre_analysis = {}

    pre_analysis["readability"] = readability_analysis.analyze(corrected_draft)
    log.info(
        "Readability: %s words, FK grade %.1f (%s)",
        pre_analysis["readability"]["word_count"],
        pre_analysis["readability"]["flesch_kincaid_grade"],
        pre_analysis["readability"]["reading_level"],
    )

    # --offline suppresses every pass that reaches the network, so the parts of
    # the pipeline that only transform data the run already has can be exercised
    # with no connection and no spend.
    link_check_enabled = pipeline_cfg.get("link_validation", True) and not offline
    if offline:
        log.info("Offline: skipping link validation, Wayback and citation resolution")
    if link_check_enabled:
        from .analysis import links as links_analysis

        check_wayback_links = pipeline_cfg.get("wayback_link_check", True)
        # Use `is not None` rather than `or` so an explicit 0 isn't coalesced to the default.
        wayback_stale_days = pipeline_cfg.get("wayback_snapshot_stale_days")
        if wayback_stale_days is not None:
            wayback_stale_days = int(wayback_stale_days)
        log.info(
            "Link validation: scanning for URLs%s",
            " + Wayback check" if check_wayback_links else "",
        )
        pre_analysis["links"] = links_analysis.validate_links(
            corrected_draft,
            check_wayback=check_wayback_links,
            wayback_stale_days=wayback_stale_days,
        )
        broken = [r for r in pre_analysis["links"] if not r.get("ok")]
        not_archived = [
            r
            for r in pre_analysis["links"]
            if r.get("wayback", {}).get("archived") is False
        ]
        log.info(
            "Links: %d found, %d broken/error, %d not archived in Wayback",
            len(pre_analysis["links"]),
            len(broken),
            len(not_archived),
        )
    else:
        pre_analysis["links"] = []

    if seo_suggestions is False:
        # CLI override for this run only — does not modify the publication
        # config on disk. Mirrors --cost-preset above. Assigned rather than
        # setdefault'd because a bare `seo_rules:` line in YAML parses to None,
        # not to an empty dict. Covers both SEO model calls: the flag is about
        # not paying for the SEO extras, not about one of the two.
        pub_config["seo_rules"] = {**(pub_config.get("seo_rules") or {})}
        pub_config["seo_rules"]["suggestions"] = False
        pub_config["seo_rules"]["content_review"] = False

    pre_analysis["seo"] = seo_analysis.analyze(
        corrected_draft,
        handoff,
        seo_rules=pub_config.get("seo_rules"),
        mode=seo_analysis.DRAFT_MODE,
        site_url=(pub_config.get("wordpress") or {}).get("site_url"),
    )
    seo_issues = pre_analysis["seo"]["issues"]
    if seo_issues:
        log.info(
            "SEO: %d issue(s): %s",
            len(seo_issues),
            "; ".join(i["type"] for i in seo_issues),
        )
    else:
        log.info("SEO: no issues detected")

    # SEO suggestions — one cheap model call proposing focus keywords, a meta
    # description, and (when the title is over the ceiling) an OG title. Runs
    # here, at draft time, so the output feeds the chat revision round-trip and
    # can be regenerated each pass. Advisory only: nothing is written to any
    # config, handoff, or WordPress metadata. Its cost entry joins api_call_log
    # further down, once that list exists.
    seo_suggestion_result, seo_suggestion_call = seo_suggest.generate(
        corrected_draft,
        handoff=handoff,
        pub_config=pub_config,
        api_keys=api_keys,
        seo_result=pre_analysis["seo"],
    )
    seo_analysis.apply_suggestions(
        pre_analysis["seo"],
        seo_suggestion_result,
        text=corrected_draft,
        title=article_title,
    )

    # SEO content review — a second cheap call judging the article's structure
    # from a search reader's side (heading descriptiveness, whether the opening
    # delivers, title promise vs delivery). Separate from the suggestion call
    # because it answers a different question and is separately disableable;
    # it takes the keyword candidates as the search intent to judge against.
    seo_content_result, seo_content_call = seo_content.review(
        corrected_draft,
        handoff=handoff,
        pub_config=pub_config,
        api_keys=api_keys,
        suggestions=seo_suggestion_result,
    )
    pre_analysis["seo"]["content_review"] = seo_content_result

    # Sliding-scale timeouts: size the per-model timeout from the draft length, the
    # model, and the reasoning effort. A timeout_seconds set explicitly in user.yaml
    # is treated as an override and left untouched.
    #
    # Since the review adapters stream (SSE), this computed value is the per-task
    # thread WALL-CLOCK BACKSTOP enforced by _run_reviews_in_parallel — NOT a socket
    # timeout. Streaming does not make a long generation finish faster (a gpt-5.5
    # xhigh call still emits tokens for ~800s); it changes the socket read timeout,
    # which the adapters now hold at a small constant inter-token gap (see
    # ci_core/llm/streaming.py) instead of the whole-generation budget. So this
    # ceiling must still cover the genuine total generation time; what it no longer
    # has to absorb is "model buffered everything and sent nothing" — a stall is now
    # caught in ~120s by the adapter's read-gap timeout, not here. Writing into
    # model_configs means _per_model_timeout below picks it up with no further
    # wiring; the adapters ignore timeout_seconds for the socket entirely.
    char_count = len(corrected_draft)
    if no_timeout:
        # Calibration aid: remove timeout truncation so true completion times are
        # measured, never cut off. Uses a large finite value (interruptible with Ctrl-C).
        CALIBRATION_TIMEOUT = 3600
        for _prov, _cfg in model_configs.items():
            if isinstance(_cfg, dict) and _cfg.get("enabled", True):
                _cfg["timeout_seconds"] = CALIBRATION_TIMEOUT
        log.info(
            "Timeouts DISABLED for calibration (--no-timeout): all models set to %ds",
            CALIBRATION_TIMEOUT,
        )
    else:
        # Catch a stale explicit timeout_seconds before it silently caps a run —
        # the bug that hit perplexity (fixed 2026-08-16) and claude (fixed
        # 2026-08-18, cost 3 of 5 domains on a real run) the same way: an
        # override left over from a lighter preset undercutting what the
        # model's current effort actually needs, with no signal that it's
        # doing so. Read against the config as the caller passed it in, before
        # the mutation loop below back-fills computed values into it.
        for _prov, _override, _formula in timeout_model.flag_stale_overrides(
            char_count, model_configs, task_timeout
        ):
            log.warning(
                "%s has an explicit timeout_seconds=%ds in configs/user.yaml, but "
                "the sliding-scale formula computes %ds for this run (%d chars). "
                "The override may be truncating calls that would otherwise "
                "succeed — remove timeout_seconds to let it size automatically, "
                "or raise it to at least %ds if you want to keep an explicit "
                "ceiling.",
                _prov,
                _override,
                _formula,
                char_count,
                _formula,
            )

        computed_timeouts = timeout_model.compute_all(
            char_count, model_configs, task_timeout
        )
        for _prov, _t in computed_timeouts.items():
            if isinstance(model_configs.get(_prov), dict):
                model_configs[_prov]["timeout_seconds"] = _t
        if computed_timeouts:
            log.info(
                "Timeouts (%d chars): %s",
                char_count,
                ", ".join(f"{p}={t}s" for p, t in sorted(computed_timeouts.items())),
            )

    # Pre-load all prompt files before spawning threads (warms the cache)
    for prompt_name in _DOMAIN_PROMPTS.values():
        _load_prompt(prompt_name)

    # Pass 2: Ensemble model review — built-in domains
    drafting_model = _drafting_model(handoff, pipeline_cfg)
    if drafting_model:
        log.info(
            "Drafting model: %s — excluded from %s",
            drafting_model,
            ", ".join(_DRAFTER_EXCLUDED_DOMAINS),
        )
    assignment_skips: list[str] = []
    assignments = _build_assignments(
        thoroughness, model_configs, api_keys, drafting_model, assignment_skips
    )

    # Custom publication-defined domains
    custom_skips: list[str] = []
    custom_assignments, custom_prompts = _build_custom_assignments(
        pub_config, model_configs, api_keys, custom_skips
    )
    if custom_assignments:
        log.info("Custom domains: %d assignment(s)", len(custom_assignments))
        for model_name, domain in custom_assignments:
            log.info("  Custom: %s → %s", model_name, domain)
    # Outside the block above on purpose: a custom domain whose every model was
    # dropped contributes no assignment, so that header never prints — and that
    # is the run whose silence is hardest to account for.
    for skip_line in custom_skips:
        log.info("  Custom skipped: %s", skip_line)

    if not assignments:
        # The reasons matter most here — this is the run that produced nothing.
        for skip_line in assignment_skips:
            log.info(f"  Skipped: {skip_line}")
        log.error(
            "No model assignments could be built. "
            "Check that at least one model has credentials and is enabled."
        )
        sys.exit(1)

    # Calibration filters: restrict the run to a subset so a single cell (e.g.
    # one gpt-5.5 xhigh call) can be measured without the full ensemble.
    if only_model:
        assignments = [a for a in assignments if a[0] == only_model]
        custom_assignments = [a for a in custom_assignments if a[0] == only_model]
    if only_domain:
        assignments = [a for a in assignments if a[1] == only_domain]
        custom_assignments = [a for a in custom_assignments if a[1] == only_domain]
    if (only_model or only_domain) and not (assignments or custom_assignments):
        log.error(
            "No assignments match the calibration filters (--only-model=%r --only-domain=%r). "
            "Check spelling against the configured models and domain names.",
            only_model,
            only_domain,
        )
        sys.exit(1)

    all_assignments = assignments + custom_assignments
    log.info(
        f"Pass 2: Ensemble review — {len(all_assignments)} call(s) "
        f"({len(assignments)} built-in, {len(custom_assignments)} custom), "
        f"thoroughness={thoroughness!r}, "
        f"parallel={pipeline_cfg.get('parallel_review_calls', True)}, "
        f"timeout={task_timeout}s"
    )
    for model_name, domain in assignments:
        log.info(f"  Assigned: {model_name} → {domain}")
    for skip_line in assignment_skips:
        log.info(f"  Skipped: {skip_line}")

    # What this run will not try to verify. Built from the draft the models are
    # about to see (markers live in the text, so it must be the corrected one),
    # the handoff, and the publication config. Used twice: in the fact-check
    # prompt below, and again on the merged results in `build_report`.
    scope = fact_check_scope.ScopeRules.from_run(corrected_draft, handoff, pub_config)

    # Build runner list — custom domains pass their prompt string directly
    runners = [
        (
            f"{model_name}:{domain}",
            lambda m=model_name, d=domain, ps=custom_prompts.get(domain): _run_domain(
                m,
                d,
                corrected_draft,
                handoff,
                pub_config,
                api_keys,
                pipeline_cfg,
                model_configs,
                prompt_str=ps,
                scope=scope,
            ),
        )
        for model_name, domain in all_assignments
    ]

    raw_results: dict[str, dict] = {}

    if replay_results:
        # Replay: hand back a previously captured ensemble instead of paying for
        # it again. The capture is keyed exactly like raw_results, so everything
        # downstream — re-keying, the call log, consolidation, citations, the
        # report — runs unchanged and unaware.
        raw_results = ensemble_capture.load(replay_results)
        log.info("Pass 2: REPLAY — %s", ensemble_capture.describe(replay_results))
        log.info("No model calls made; this run costs nothing.")
    elif retry_failed_results:
        # Manual gap-fill: make model calls only for the (model, domain) pairs
        # a prior capture marked failed, then merge onto everything that
        # already succeeded. Unlike replay this does cost money — just not for
        # the calls that already worked.
        prior_results = ensemble_capture.load(retry_failed_results)
        names_to_retry = [name for name, r in prior_results.items() if r.get("failed")]
        log.info(
            "Pass 2: RETRY-FAILED — %s", ensemble_capture.describe(retry_failed_results)
        )
        if names_to_retry:
            log.info(
                "Re-attempting %d failed call(s): %s",
                len(names_to_retry),
                ", ".join(sorted(names_to_retry)),
            )
            retried = _run_reviews_for_names(
                names_to_retry, runners, pipeline_cfg, model_configs, task_timeout
            )
        else:
            log.info("No failed calls in the prior capture — nothing to retry.")
            retried = {}
        raw_results = _merge_recovered_results(prior_results, retried)
        still_failed = [name for name, r in raw_results.items() if r.get("failed")]
        log.info(
            "Retry-failed summary: %d attempted, %d still failing out of %d total.",
            len(names_to_retry),
            len(still_failed),
            len(raw_results),
        )
    elif pipeline_cfg.get("parallel_review_calls", True):
        raw_results = _run_reviews_in_parallel(
            runners, pipeline_cfg, model_configs, task_timeout
        )
    else:
        for name, fn in runners:
            try:
                raw_results[name] = fn()
            except Exception as e:
                log.error(f"Review pass {name} raised exception: {e}")
                raw_results[name] = {
                    "failed": True,
                    "error": str(e),
                    "model": "unknown",
                    "tokens": {},
                    "_model": name.split(":")[0],
                    "_domain": name.split(":", 1)[1] if ":" in name else name,
                }

    # Replay makes no model calls at all, so there is nothing to recover.
    # Applies to the parallel, serial, and retry-failed branches above alike —
    # each of them can strand the same kind of transient failure.
    if not replay_results and pipeline_cfg.get("recovery_passes", 1) > 0:
        raw_results = _recover_failed_calls(
            raw_results, runners, pipeline_cfg, model_configs, task_timeout
        )

    # Hold the raw ensemble output for capture. Written next to the report once
    # the save path is known, so a later run can replay this one for free.
    captured_raw_results = dict(raw_results)

    # Re-key results as {(model_name, domain): result}
    results: dict[tuple[str, str], dict] = {}
    for result in raw_results.values():
        model_name = result.get("_model", "unknown")
        domain = result.get("_domain", "unknown")
        results[(model_name, domain)] = result

    # Build API call log. Each entry also carries the calibration inputs (effort,
    # the timeout budget that was actually applied, and the draft size) so the
    # report is a self-contained record for retuning configs/timeouts.yaml — no
    # cross-referencing other files needed.
    #
    # On a failed call whose adapter captured the raw response text (e.g.
    # "Malformed JSON response"), a redacted/truncated excerpt is kept in the
    # report so the failure is diagnosable after the fact. The full `raw` field
    # is never persisted — only a bounded excerpt, and only on failure.
    def _raw_excerpt(raw):
        return redact.truncate_excerpt(redact.redact_url_keys(raw))

    api_call_log = []
    for (model_name, domain), result in results.items():
        status_ok = not result.get("failed")
        model_tag = result.get("model", model_name)
        if result.get("fallback_from"):
            model_tag = f"{model_tag} [FALLBACK from {result['fallback_from']}]"
        grounding = " [grounded]" if result.get("grounding_available") else ""
        mcfg = (
            model_configs.get(model_name)
            if isinstance(model_configs.get(model_name), dict)
            else {}
        )
        effort = (
            (mcfg or {}).get("reasoning_effort") or (mcfg or {}).get("effort") or "none"
        )
        budget = (mcfg or {}).get("timeout_seconds")
        elapsed = result.get("elapsed_seconds")
        out_tokens = (result.get("tokens") or {}).get("completion")
        timed_out = "timed out" in str(result.get("error", "")).lower()
        truncated = bool(result.get("truncated"))
        status = (
            "ok"
            if status_ok and not truncated
            else "partial"
            if status_ok and truncated
            else ("timeout" if timed_out else "failed")
        )
        headroom = (
            round(budget - elapsed, 1)
            if (budget is not None and elapsed is not None)
            else None
        )
        log_entry = {
            "pass": f"{model_name}:{domain}",
            "model": f"{model_tag}{grounding}",
            "failed": not status_ok,
            "truncated": truncated,
            "tokens": result.get("tokens", {}),
            "elapsed_seconds": elapsed,
            "error": result.get("error") if not status_ok else None,
            # calibration fields
            "effort": effort,
            "timeout_budget_seconds": budget,
            "headroom_seconds": headroom,
            "char_count": char_count,
            "status": status,
        }
        if (not status_ok or truncated) and result.get("raw"):
            log_entry["raw_excerpt"] = _raw_excerpt(result["raw"])
        if not status_ok and result.get("error_body"):
            # Adapters already redact/truncate this via redact.capture_error_body()
            # before returning it, so it's safe to persist as-is.
            log_entry["error_body_excerpt"] = result["error_body"]
        api_call_log.append(log_entry)
        # Machine-facing structured record — one grep-able line per call, persisted
        # to pipeline_history/pipeline_<date>.log for cross-run calibration analysis.
        log.info(
            "[CALIBRATION] model=%s domain=%s effort=%s chars=%s budget=%ss "
            "elapsed=%ss out_tokens=%s status=%s headroom=%ss",
            model_tag.split(" ")[0],
            domain,
            effort,
            char_count,
            budget,
            elapsed,
            out_tokens,
            status,
            headroom,
        )
        if status_ok and not truncated:
            log.info(
                f"  {model_name}:{domain}: OK "
                f"({result.get('elapsed_seconds', '?')}s, {model_tag}{grounding})"
            )
        elif status_ok and truncated:
            log.warning(
                f"  {model_name}:{domain}: PARTIAL — response was truncated "
                f"(output-token ceiling); some findings recovered, some lost"
            )
            if "raw_excerpt" in log_entry:
                log.debug(
                    f"  {model_name}:{domain}: raw response excerpt:\n"
                    f"{log_entry['raw_excerpt']}"
                )
        else:
            log.warning(
                f"  {model_name}:{domain}: FAILED — {result.get('error', 'unknown error')}"
            )
            if "raw_excerpt" in log_entry:
                log.debug(
                    f"  {model_name}:{domain}: raw response excerpt:\n"
                    f"{log_entry['raw_excerpt']}"
                )
            if "error_body_excerpt" in log_entry:
                log.debug(
                    f"  {model_name}:{domain}: error body excerpt:\n"
                    f"{log_entry['error_body_excerpt']}"
                )

    report_baseline_warning = None
    all_failed = all(r.get("failed") for r in results.values())
    if all_failed and pipeline_cfg.get("abort_if_all_provider_calls_fail", False):
        log.error("All review model calls failed. Aborting.")
        sys.exit(1)

    prior_report, prior_report_path = hist.load_prior_report(
        HISTORY_ROOT, _history_key(handoff), before_ts=run_start_ts
    )
    if prior_report is None and run_number > 1:
        log.warning(
            f"No earlier report found for '{article_title}', but the handoff declares "
            f"run {run_number} — continuity tracking (claim/structure delta, "
            "consensus-flag carryover) is being skipped for this run, same as a first "
            "run. If this article was actually reviewed before, check that the "
            "handoff's 'Article:' title matches the prior run exactly — history is "
            "kept per article title."
        )
    elif prior_report is not None:
        log.info(f"Comparing against prior run: {Path(prior_report_path).name}")
        # A baseline that was itself missing model passes makes the delta
        # unreadable. On 2026-08-12 the comparison ran against a run whose five
        # OpenAI passes had all failed on a dead API key, so nine "new consensus
        # flags" were mostly OpenAI voting for the first time — with the draft
        # unchanged at 0.0% word change, and a "re-run after editing"
        # recommendation on top. Name it, so the numbers below are read as a
        # coverage difference rather than as movement in the article.
        prior_failures = prior_report.get("model_failures") or []
        if prior_failures:
            log.warning(
                "Baseline run was incomplete — %d pass(es) failed in it (%s). "
                "Findings that look new may simply be those models voting for "
                "the first time; treat the delta as indicative, not as movement "
                "in the draft.",
                len(prior_failures),
                ", ".join(prior_failures),
            )
            report_baseline_warning = (
                f"Baseline run {Path(prior_report_path).name} was itself missing "
                f"{len(prior_failures)} model pass(es) ({', '.join(prior_failures)}). "
                f"New findings below may be those models voting for the first time "
                f"rather than changes in the draft."
            )
        else:
            report_baseline_warning = None

    # Tag ensemble config with thoroughness for the report
    ensemble_cfg_tagged = {**ensemble_cfg, "thoroughness": thoroughness}

    log.info("Consolidating review report")
    report = consolidation.build_report(
        article_title=article_title,
        publication_name=publication_name,
        run_number=run_number,
        corrected_draft=corrected_draft,
        lt_result=lt_result,
        results=results,
        ensemble_cfg=ensemble_cfg_tagged,
        api_call_log=api_call_log,
        prior_report=prior_report,
        prior_report_path=prior_report_path,
        primary_claim=handoff.get("primary_claim", ""),
        fact_check_scope=scope,
    )

    # Pass 3: Citation resolution — extract factual claims from fact-check results
    #
    # Deliberately NOT gated on citation_sources. The known_url path never reads
    # that list — _resolve_one branches to _resolve_known_url and returns before
    # the adapter loop — so gating the whole pass on it meant a publication with
    # no matching adapter silently lost all citation verification, including the
    # publication-agnostic half. Every shipped adapter is US-specific and most
    # are Illinois/energy-specific, so that is the common case for a second user,
    # not an edge case. The adapter loop itself is still skipped when the list is
    # empty; see resolve_citations.
    citation_sources = pub_config.get("citation_sources", [])
    log.info(
        "Pass 3: Citation resolution (%d source adapter(s) configured)",
        len(citation_sources),
    )
    from .adapters.citation.resolver import resolve_citations

    fact_check = report.get("section_2_fact_check") or {}
    _record_fact_check_degradation(report, results)
    claims = _collect_citation_claims(fact_check, corrected_draft)
    if offline:
        # Claim collection above still runs — it is pure parsing over the draft
        # and the fact-check section, and it is the part that changes. Only the
        # network resolution below is skipped.
        log.info(
            "Offline: %d claim(s) collected, resolution skipped "
            "(Section 9 will be empty)",
            len(claims),
        )
        claims = []
    if claims:
        citation_results = resolve_citations(
            claims,
            citation_sources,
            api_keys,
            verification_call_log=api_call_log,
            history_root=HISTORY_ROOT,
        )
        verified_count = sum(
            1 for r in citation_results if r.get("verification") == "checksum"
        )
        pointer_count = sum(
            1 for r in citation_results if r.get("verification") == "pointer"
        )
        unverifiable_count = sum(
            1 for r in citation_results if r.get("verification") == "unverifiable"
        )
        log.info(
            "Citations: %d claim(s), %d verified, %d pointer-only, "
            "%d could not be verified, %d unresolved",
            len(claims),
            verified_count,
            pointer_count,
            unverifiable_count,
            len(claims) - verified_count - pointer_count - unverifiable_count,
        )
    else:
        citation_results = []
        log.info("Citations: no actionable claims to resolve")
    report["section_9_citations"] = citation_results

    # The SEO calls happened back in pre-analysis, before this list existed.
    # Fold their cost in here so each lands in cost_summary under its own pass
    # name rather than going untracked.
    for seo_call in (seo_suggestion_call, seo_content_call):
        if seo_call is not None:
            api_call_log.append(seo_call)

    # Cost tracking
    cost_summary = cost_analysis.calculate(api_call_log)
    report["cost_summary"] = cost_summary
    log.info(
        "Estimated cost: $%.4f (%s)",
        cost_summary["total_usd"],
        "exact"
        if cost_summary["pricing_known"]
        else "estimated — some model prices unknown",
    )

    # Attach pre-analysis
    report["pre_analysis"] = pre_analysis

    # Fallback warnings
    fallback_warnings = [
        {
            "pass": f"{model}:{domain}",
            "used": r.get("model"),
            "requested": r.get("fallback_from"),
        }
        for (model, domain), r in results.items()
        if r and r.get("fallback_from")
    ]
    if fallback_warnings:
        report["fallback_warnings"] = fallback_warnings

    if report_baseline_warning:
        report["baseline_warning"] = report_baseline_warning

    # Model currency, part two — what the providers themselves say.
    #
    # The registry check at the top of the run knows only what a human last
    # wrote into model_registry.yaml, so by construction it cannot mention a
    # model released after the last audit: "you ran gpt-5.5 and gpt-5.6 shipped
    # last week" is exactly what it is unable to say. This reads the discovery
    # cache — written by `ci-discover`, and refreshed here when
    # live_model_check is on — and reports against the models that actually
    # ran, which is why it happens here rather than beside the registry check.
    #
    # Advisory in every direction: --offline never queries, a check that fails
    # is reported as a check that failed, and nothing about it can fail the run.
    try:
        currency["live"] = live_model_check.check(
            live_model_check.models_that_ran(api_call_log),
            api_keys,
            refresh=pipeline_cfg.get("live_model_check", False) and not offline,
            max_age_hours=pipeline_cfg.get("live_model_check_max_age_hours", 24),
        )
    except Exception as exc:  # noqa: BLE001 — advisory information, never fatal
        log.debug("Live model check failed: %s", exc)
        currency["live"] = {
            "status": "unavailable",
            "newer": [],
            "current": [],
            "unchecked": [],
            "error": str(exc),
        }

    # Attach currency check results so they appear in the saved report JSON
    # and can be rendered by _print_draft_summary.
    report["model_currency"] = currency

    # Mark a replayed run. Its cost figures are the captured run's, carried in
    # the api_call_log — real when they were incurred, but not spent again here.
    # Without this the report reads as though every replay cost money, and any
    # tool summing history would count one run's spend as many.
    if replay_results:
        report["replayed_from"] = str(replay_results)

    # A replay is a code test, not a review of the article. Writing it into the
    # article's own history would give the next real run a replay as its delta
    # baseline, and would count toward the distinct-article totals in
    # ci-voice-patterns. It goes to a sibling tree instead: still exercises the
    # whole save path, still readable, invisible to anything scanning
    # pipeline_history/ for real runs (that scan looks for reports one level
    # down, and finds only the nested _replay/<slug>/ directory).
    history_root = (
        str(Path(HISTORY_ROOT) / "_replay") if replay_results else HISTORY_ROOT
    )
    paths = hist.save_run(
        history_root,
        _history_key(handoff),
        run_number,
        report,
        lt_result.get("change_log", []),
        run_ts=run_start_ts,
    )
    if paths["report_path"]:
        log.info(f"Report saved: {paths['report_path']}")
    if paths.get("markdown_path"):
        log.info(f"Readable review: {paths['markdown_path']}")

    # Capture the raw ensemble beside the report so this run can be replayed.
    # Skipped on a replay: re-writing a capture from a capture adds nothing and
    # would quietly make a copy look like a fresh measurement.
    if paths["report_path"] and not replay_results and captured_raw_results:
        cap_path = ensemble_capture.save(
            ensemble_capture.capture_path_for(paths["report_path"]),
            captured_raw_results,
            article_title=article_title,
            run_number=run_number,
        )
        if cap_path:
            log.info(f"Ensemble captured: {cap_path}")
            log.info(
                f"  Replay it free with:  ci-review --replay {cap_path} "
                f"--draft <handoff> --publication {publication_name} --offline"
            )

    elapsed_total = round(time.monotonic() - t_start, 1)
    _print_draft_summary(
        report, delta_cfg, elapsed_total, markdown_path=paths.get("markdown_path")
    )

    return report


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------


def _keyword_usage_summary(usage):
    """One-line "where this phrase actually appears" summary, or "" if unscanned.

    The absent case is the one worth reading, so it gets said outright rather
    than implied by three "no"s.
    """
    if not usage:
        return ""
    if not usage.get("body_count"):
        return "NOT USED anywhere in the article"

    where = []
    if usage.get("in_title"):
        where.append("title")
    if usage.get("in_opening"):
        where.append("opening")
    headings = usage.get("in_headings") or []
    if headings:
        where.append(f"{len(headings)} heading(s)")
    placement = ", ".join(where) if where else "body only"
    return f"{usage['body_count']}x — {placement}"


def _print_seo_content_review(content_review):
    """Console rendering of the structural findings, under the suggestions."""
    if not content_review:
        return
    if content_review.get("status") != "ok":
        print(
            f"SEO structure review: unavailable — "
            f"{content_review.get('reason', 'unknown reason')}"
        )
        return

    findings = content_review.get("findings") or []
    if not findings:
        print("SEO structure review: nothing flagged")
        return

    print(f"SEO structure review ({len(findings)} finding(s)):")
    for f in findings:
        target = f' "{f["target"]}"' if f.get("target") else ""
        print(f"  [{f['type']}]{target}")
        print(f"    {f['problem']}")
        if f.get("suggestion"):
            print(f"    → {f['suggestion']}")


def _print_seo_suggestions(suggestions):
    """Console rendering of the SEO suggestion block, under the SEO issues.

    Prints the unavailable reason too, rather than nothing: a run whose
    suggestions silently vanished looks identical to one where the pass was
    never wired up.
    """
    if not suggestions:
        return

    status = suggestions.get("status")
    if status != "ok":
        print(
            f"SEO suggestions: unavailable — {suggestions.get('reason', 'unknown reason')}"
        )
        return

    print("SEO suggestions (advisory — none of this is applied automatically):")

    candidates = suggestions.get("keyword_candidates") or []
    if candidates:
        print("  Focus keyword candidates — choose one yourself:")
        for i, c in enumerate(candidates, 1):
            rationale = f" — {c['rationale']}" if c.get("rationale") else ""
            print(f"    {i}. {c['keyword']}{rationale}")
            usage = _keyword_usage_summary(c.get("usage"))
            if usage:
                print(f"       in article: {usage}")

    fields = suggestions.get("fields") or {}
    for name in seo_suggest.FIELD_ORDER:
        field = fields.get(name)
        if not field:
            continue
        label = field.get("label", name)
        if not field.get("value"):
            # Every field reports an outcome; for these two the outcome is
            # which default the WordPress push would apply.
            print(f"  {label}: {field.get('default_note', 'not proposed')}")
            continue

        measured = (
            f" ({field['chars']}/{field['limit']} chars)"
            if field.get("limit") is not None
            else ""
        )
        over = " — OVER LIMIT, trim before use" if field.get("over_limit") else ""
        print(f"  {label}{measured}{over}:")
        print(f"    {field['value']}")
        if field.get("rationale"):
            print(f"      {field['rationale']}")
        if field.get("recognized") is False:
            print("      Unrecognized type — confirm Rank Math accepts it")
        elif field.get("differs_from_default"):
            print(
                f"      Differs from the configured default: "
                f"{field['configured_default']}"
            )


def _print_live_model_check(live):
    """The providers' own answer about newer models, for the terminal summary.

    Prints nothing when there is nothing to say — a run with no discovery data
    at all should not grow a block explaining that. The one thing it will not
    do is let an empty result read as reassurance: providers that could not be
    checked are named, because "nothing newer found" is only true of the
    providers actually asked.
    """
    if not live:
        return

    newer = live.get("newer") or []
    current = live.get("current") or []
    unchecked = live.get("unchecked") or []
    if not (newer or current or unchecked):
        return

    for finding in newer:
        heads = ", ".join(
            f"{m['model']}"
            + (f" ({m['released']})" if m.get("released") else "")
            + ("" if m.get("price_known") else " [not in pricing.yaml]")
            for m in finding["newer"][:3]
        )
        extra = len(finding["newer"]) - 3
        if extra > 0:
            heads += f", +{extra} more"
        print(
            f"\n{finding['provider']}: ran {finding['model']!r}; "
            f"{finding['provider']} now also offers {heads}"
        )

    if newer:
        # Deliberately not a recommendation. See live_model_check's docstring:
        # newer is not better, and a model pricing.yaml has not caught up with
        # would be costed at the unknown-model fallback rather than its rate.
        print(
            "  Newer is not necessarily better or cheaper — this is what the "
            "provider lists, not advice to switch."
        )

    if unchecked and not (newer or current):
        # The default path: live_model_check is off and nobody has run
        # ci-discover, so there is no provider data at all. One line saying the
        # check did not happen, not a roll-call of every provider — this prints
        # on every single run, and the registry line above already covers what
        # is known about model age.
        print(
            "\nNewer-model check: not run — the built-in registry above is the "
            "only source. `uv run ci-discover` asks your providers directly."
        )
    elif unchecked:
        names = ", ".join(f"{u['provider']} ({u['reason']})" for u in unchecked)
        print(f"\nNot checked for newer models: {names}")

    age = live.get("oldest_check_age_hours")
    if (newer or current) and age is not None:
        source = "refreshed this run" if live.get("refreshed") else "from cache"
        print(f"  (model availability data {source}; oldest entry {age}h old)")


def _print_draft_summary(report, delta_cfg, elapsed_total=None, markdown_path=None):
    print("\n" + "=" * 60)
    print(f"REVIEW COMPLETE: {report['article_title']}")
    print(f"Run #{report['run_number']} — {report['generated']}")
    if elapsed_total is not None:
        print(f"Total elapsed: {elapsed_total}s")

    ensemble_meta = report.get("ensemble", {})
    thoroughness = ensemble_meta.get("thoroughness", "standard")
    n_calls = len(ensemble_meta.get("assignments", []))
    print(f"Thoroughness: {thoroughness} ({n_calls} model-domain calls)")
    print("=" * 60)

    if report.get("baseline_warning"):
        print(f"\nWARNING: {report['baseline_warning']}")

    if report.get("model_failures"):
        print(f"\nWARNING: {len(report['model_failures'])} model pass(es) failed:")
        details = report.get("model_failure_details") or []
        if details:
            for detail in details:
                elapsed = detail.get("elapsed_seconds")
                after = (
                    f" after {elapsed:.0f}s"
                    if isinstance(elapsed, (int, float))
                    else ""
                )
                print(f"  - {detail['pass']} failed{after}: {detail['error']}")
                if detail.get("section"):
                    print(f"    {detail['section']} is short one model.")
        else:
            print(f"  {', '.join(report['model_failures'])}")

    # Knock-on effects of those failures. Printed adjacent to the failure list
    # because the two are only useful together: "perplexity:fact_check failed"
    # and "9 claims verified" are separately unremarkable, and it is the link
    # between them that tells a reader which numbers below to distrust.
    for degradation in report.get("degradations") or []:
        print(f"\nWARNING: {degradation['detail']}")

    if report.get("fallback_warnings"):
        print(f"\n{'!' * 60}")
        print(
            "WARNING: One or more passes ran on fallback models due to capacity limits."
        )
        print(
            "Results from fallback models may be less thorough than preferred models."
        )
        for fw in report["fallback_warnings"]:
            print(
                f"  {fw['pass']}: ran on '{fw['used']}' (preferred: '{fw['requested']}')"
            )
        print("Re-run when preferred models are available to confirm findings.")
        print("!" * 60)

    currency = report.get("model_currency")
    if currency:
        if currency.get("warnings"):
            print(f"\n{'!' * 60}")
            print("MODEL CURRENCY: Outdated model(s) detected — update user.yaml")
            for w in currency["warnings"]:
                note = f" ({w['note']})" if w.get("note") else ""
                print(
                    f"  {w['provider']}: {w['model']!r} → replace with {w['replacement']!r}{note}"
                )
            print("!" * 60)
        if currency.get("notices"):
            print("\nModel upgrades available (optional):")
            for n in currency["notices"]:
                print(
                    f"  {n['provider']}: {n['model']!r} — {n.get('note') or 'newer: ' + n['newer']}"
                )
        age = currency.get("registry_age_days", 0)
        reg_date = currency.get("registry_date", "unknown")
        if currency.get("registry_warning"):
            print(
                f"\n{'!' * 60}\n"
                f"MODEL REGISTRY is {age} days old (last updated {reg_date}).\n"
                f"Provider APIs change frequently — re-check models, pricing, and\n"
                f"reasoning flags before your next run.  See docs/PROVIDERS.md.\n"
                f"{'!' * 60}"
            )
        elif currency.get("registry_stale"):
            print(
                f"\nNote: model registry last updated {reg_date} ({age} days ago). "
                "Consider re-checking for newer models."
            )
        else:
            print(
                f"\nModel registry: current (last updated {reg_date}, {age} days ago)"
            )

        _print_live_model_check(currency.get("live") or {})

    if report.get("lt_skipped"):
        # Both skip paths set lt_skipped, and this printed the credentials
        # message for either — telling an operator whose credentials are sitting
        # in .env and working to go and configure credentials, when the actual
        # cause was grammar_pass: false in the config.
        if report.get("lt_skipped_reason") == "disabled":
            why = "grammar_pass is set to false in the pipeline config"
        else:
            why = "no LanguageTool credentials configured"
        print(
            f"\nGrammar pass: skipped ({why} — run a manual Grammarly pass "
            f"before publishing)"
        )
    elif report.get("lt_failed"):
        print("\nWARNING: LanguageTool failed — draft not grammar-corrected")
    else:
        print(
            f"\nLanguageTool: {len(report['lt_corrections_applied'])} corrections applied"
        )

    print(f"\nSection 1 — Consensus flags: {len(report['section_1_consensus'])}")
    fact = report["section_2_fact_check"]
    fact_count = (
        sum(len(v) for v in fact.values() if isinstance(v, list)) if fact else 0
    )
    print(f"Section 2 — Fact check items: {fact_count}")
    # Say it here as well as in the report. This count drops when claims are
    # marked out of scope — several models' verdicts on one claim collapse into
    # a single entry — and a smaller number with no explanation beside it reads
    # as findings having gone missing, which is the failure the whole feature
    # exists to avoid.
    _out_of_scope = (fact or {}).get("out_of_scope") or []
    _held = sum(1 for i in _out_of_scope if i.get("excluded"))
    if _out_of_scope:
        print(
            f"  of which {len(_out_of_scope)} out of scope for verification "
            f"({_held} also held out of Section 9) — see 'Out of scope for "
            f"verification' in the review"
        )
    print(f"Section 3 — Voice flags: {len(report['section_3_voice'])}")
    print(f"Section 4 — Argument flags: {len(report['section_4_argument'])}")
    print(f"Section 5 — Completeness flags: {len(report['section_5_completeness'])}")

    rt = report["section_6_red_team"]
    if not rt:
        _rt_display = "none"
    elif "most_vulnerable_claim" in rt:
        _rt_display = "3 findings (1 source)"
    else:
        n_src = len(rt)
        _rt_display = f"{n_src * 3} findings ({n_src} sources)"
    print(f"Section 6 — Red team: {_rt_display}")
    print(
        f"Section 7 — Low-confidence flags: {len(report['section_7_low_confidence'])}"
    )

    additional = report.get("section_8_additional", [])
    if additional:
        by_cat: dict[str, int] = {}
        for obs in additional:
            cat = obs.get("category", "unknown")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        cat_summary = ", ".join(f"{c}: {n}" for c, n in sorted(by_cat.items()))
        print(
            f"Section 8 — Cross-model observations: {len(additional)} ({cat_summary})"
        )
    else:
        print("Section 8 — Cross-model observations: 0")

    # Pre-analysis block
    pre = report.get("pre_analysis", {})
    if pre:
        r = pre.get("readability", {})
        if r:
            print(
                f"\nReadability: {r['word_count']} words, {r['sentence_count']} sentences, "
                f"FK grade {r['flesch_kincaid_grade']} ({r['reading_level']}), "
                f"avg sentence {r['avg_sentence_length']} words"
            )
        seo = pre.get("seo", {})
        if seo:
            seo_issues = seo.get("issues", [])
            if seo_issues:
                print(f"SEO issues ({len(seo_issues)}):")
                for iss in seo_issues:
                    print(f"  [{iss['type']}] {iss['detail']}")
            else:
                print("SEO: no issues")
            _print_seo_suggestions(seo.get("suggestions"))
            _print_seo_content_review(seo.get("content_review"))
        links = pre.get("links", [])
        if links:
            broken = [lk for lk in links if not lk.get("ok")]
            # A link we couldn't read — the origin refused us (401/403/429) or
            # we never reached it (timeout, DNS/connection failure) — and that
            # had no archive snapshot to fall back on is likely still a live
            # page, unlike a 404/410, which is confirmed dead. Both count toward
            # "broken" above (we couldn't verify the content either way), but a
            # reader has to treat them differently before acting on the report.
            unread = [lk for lk in broken if lk.get("origin_failure")]
            confirmed_dead = [
                lk for lk in broken if lk.get("status_code") in (404, 410)
            ]
            via_archive = [
                lk for lk in links if lk.get("verified_via") == "wayback_fallback"
            ]
            not_archived = [
                lk for lk in links if lk.get("wayback", {}).get("archived") is False
            ]
            stale_archive = [
                lk
                for lk in links
                if lk.get("wayback", {}).get("snapshot_stale") is True
            ]
            print(f"\nLinks: {len(links)} found", end="")
            if broken:
                print(f", {len(broken)} broken/error", end="")
            if via_archive:
                print(f", {len(via_archive)} recovered via archive", end="")
            if not_archived:
                print(f", {len(not_archived)} not archived", end="")
            if stale_archive:
                print(f", {len(stale_archive)} stale archive", end="")
            print()
            if unread:
                detail = ", ".join(
                    sorted(
                        {
                            wayback.FALLBACK_REASON_LABELS.get(
                                lk["origin_failure"], lk["origin_failure"]
                            )
                            for lk in unread
                        }
                    )
                )
                print(
                    f"  Note: {len(unread)} of the broken link(s) could not be read "
                    f"({detail}) with no archive snapshot to fall back on — likely "
                    "still valid; "
                    "verify manually. This is distinct from a 404, which is "
                    "confirmed dead."
                )
            if confirmed_dead:
                print(
                    f"  Note: {len(confirmed_dead)} of the broken link(s) returned "
                    "404/410 — confirmed gone, and deliberately not substituted with "
                    "an archive copy. These need re-sourcing."
                )
            for lk in links:
                if lk.get("verified_via") == "wayback_fallback":
                    # Never just "OK": the content came from archive.org, and
                    # which way the origin failed changes what that means.
                    reason = lk.get("origin_failure")
                    label = wayback.FALLBACK_REASON_LABELS.get(reason, reason)
                    status = (
                        f"OK (via archive: {label})" if label else "OK (via archive)"
                    )
                elif lk.get("ok"):
                    status = "OK"
                else:
                    status = f"BROKEN ({lk.get('status_code') or lk.get('error', '?')})"
                wb = lk.get("wayback", {})
                age = wb.get("snapshot_age_days")
                if wb.get("is_archive_url"):
                    # The cited link is itself a Wayback snapshot; HTTP status (OK/BROKEN
                    # above) is the functional check. Flag staleness from its own timestamp.
                    stale = " STALE" if wb.get("snapshot_stale") else ""
                    wb_str = (
                        f"archive link{stale} ({age}d)"
                        if age is not None
                        else "archive link"
                    )
                elif wb.get("archived") is False:
                    wb_str = "not archived"
                elif wb.get("snapshot_stale"):
                    wb_str = f"stale archive ({age}d)"
                elif wb.get("archived"):
                    wb_str = f"archived ({age}d)"
                elif wb:
                    # archived is None: the lookup never completed. Rendering
                    # this as blank let it read as "nothing to report", which is
                    # the opposite of true — and the rate limiter's circuit
                    # breaker makes it the common case in a throttled run, so a
                    # whole column of blanks would mean "we stopped asking".
                    wb_str = "archive not checked"
                else:
                    wb_str = ""
                extras = f"  [{wb_str}]" if wb_str else ""
                redirect = (
                    f" → {lk['redirected_to']}" if lk.get("redirected_to") else ""
                )
                print(f"  {status:36s} {lk['url'][:60]}{redirect}{extras}")

    if report.get("api_call_log"):
        print("\nAPI call times  (elapsed / budget — headroom shows timeout margin):")
        for entry in report["api_call_log"]:
            status = (
                "PARTIAL"
                if entry.get("truncated")
                else "OK"
                if not entry["failed"]
                else "FAILED"
            )
            elapsed = (
                f"{entry['elapsed_seconds']}s"
                if entry.get("elapsed_seconds") is not None
                else "?"
            )
            budget = entry.get("timeout_budget_seconds")
            head = entry.get("headroom_seconds")
            budget_str = f"/{budget}s" if budget is not None else ""
            head_str = f"(+{head}s)" if head is not None else ""
            effort = entry.get("effort", "")
            tokens = entry.get("tokens", {})
            tok_str = (
                f"{tokens.get('prompt', 0)}+{tokens.get('completion', 0)} tok"
                if tokens
                else ""
            )
            print(
                f"  {entry['pass']:30s} {status:6s} {elapsed:>8s}{budget_str:>7s} {head_str:>10s}  "
                f"{entry['model']}  effort={effort}  {tok_str}"
            )

    delta = report.get("delta")
    if delta:
        print("\nDelta from prior run:")
        compared = (delta.get("compared_against") or {}).get("report")
        if compared:
            print(f"  Compared against: {compared}")
        print(f"  Word change: {delta['word_change_pct']}%")
        print(
            f"  Resolved consensus flags: {delta['resolved_consensus_count']}/{delta['prior_consensus_count']}"
        )
        print(f"  New consensus flags: {delta['new_consensus_count']}")
        if delta.get("claim_changed"):
            print("  Primary claim: CHANGED since prior run")
        if delta.get("structure_changed"):
            print("  Heading structure: CHANGED since prior run")
        if consolidation.rerun_recommended(delta, delta_cfg):
            print(
                "\n  RECOMMENDATION: Re-run after editing (significant changes detected)"
            )
        else:
            print(
                "\n  RECOMMENDATION: Draft appears stable — proceed to Grammarly pass"
            )

    # Cost summary
    cost = report.get("cost_summary")
    if cost and report.get("replayed_from"):
        # Say plainly that this number was not spent here. A replay carries the
        # captured run's token counts, so the figure is real history, not a bill.
        print(
            f"\nCost: $0.0000 — replayed from a capture, no model calls made."
            f"\n  (the capture's own run cost ${cost['total_usd']:.4f})"
        )
    elif cost:
        known_flag = (
            ""
            if cost["pricing_known"]
            else " (estimated — unknown model in pricing table)"
        )
        print(f"\nEstimated cost: ${cost['total_usd']:.4f}{known_flag}")
        if cost.get("by_pass"):
            for entry in cost["by_pass"]:
                if entry["total_usd"] > 0:
                    print(
                        f"  {entry['pass']:30s}  ${entry['total_usd']:.4f}  {entry['model']}"
                    )

    # Contradiction summary
    contradictions = report.get("contradictions", [])
    if contradictions:
        print(f"\n{'!' * 60}")
        print(
            f"FACT-CHECK CONTRADICTIONS: {len(contradictions)} claim(s) confirmed by one model, challenged by another"
        )
        for c in contradictions:
            print(
                f"  [{c['challenge_type'].upper()}] confirmed by {', '.join(c['confirmed_by'])}; "
                f"challenged by {', '.join(c['challenged_by'])}"
            )
            print(f"    Claim: {c['claim'][:100]}")
        print("!" * 60)

    # Citation resolution summary
    citations = report.get("section_9_citations", [])
    if citations:
        resolved = [c for c in citations if c.get("resolved")]
        verified = [c for c in resolved if c.get("verification") == "checksum"]
        pointer = [c for c in resolved if c.get("verification") == "pointer"]
        unverifiable = [c for c in resolved if c.get("verification") == "unverifiable"]
        not_archived = [
            c for c in resolved if c.get("wayback", {}).get("archived") is False
        ]
        submitted = [c for c in not_archived if c.get("wayback", {}).get("submitted")]
        submission_failed = [
            c
            for c in not_archived
            if c.get("wayback", {}).get("submitted") is False
            and c.get("wayback", {}).get("submission_error")
        ]
        stale = [c for c in resolved if c.get("wayback", {}).get("snapshot_stale")]
        print(
            f"\nSection 9 — Citations: {len(citations)} claim(s) — "
            f"{len(verified)} verified, {len(pointer)} pointer-only "
            f"(not independently verified), {len(unverifiable)} could not be verified, "
            f"{len(citations) - len(resolved)} unresolved"
        )
        from_archive = [
            c for c in resolved if c.get("verified_via") == "wayback_fallback"
        ]
        if from_archive:
            # These were checksummed and verified against archive.org's copy,
            # not the live page. Same tier, weaker provenance — say so rather
            # than folding them into the verified count silently.
            detail = ", ".join(
                sorted(
                    {
                        wayback.FALLBACK_REASON_LABELS.get(
                            c.get("origin_failure"), c.get("origin_failure") or "?"
                        )
                        for c in from_archive
                    }
                )
            )
            print(
                f"  {len(from_archive)} resolved from an archive.org snapshot rather "
                f"than the live source ({detail}) — content checked is the archived copy"
            )
        if submitted:
            print(
                f"  {len(submitted)} resolved URL(s) submitted for archiving "
                "(check back later — archive.org processes asynchronously)"
            )
        if submission_failed:
            print(
                f"  {len(submission_failed)} resolved URL(s) failed Wayback submission "
                "— still not archived"
            )
        not_attempted = [
            c
            for c in not_archived
            if not c.get("wayback", {}).get("submitted")
            and not c.get("wayback", {}).get("submission_error")
        ]
        if not_attempted:
            print(
                f"  {len(not_attempted)} resolved URL(s) not archived in Wayback Machine"
            )
        if stale:
            print(
                f"  {len(stale)} resolved URL(s) have a stale Wayback snapshot (>180 days)"
            )
        changed = [c for c in verified if c.get("content_changed_since")]
        if changed:
            print(f"\n{'!' * 60}")
            print(
                f"WARNING: {len(changed)} verified source(s) have changed content "
                "since a prior run checksummed them:"
            )
            for c in changed:
                drift = c["content_changed_since"]
                print(f"  {c.get('url')}")
                print(
                    f"    Last matched in run {drift.get('prior_run')} of "
                    f"'{drift.get('prior_article')}'"
                    + (
                        f" on {drift.get('prior_date')}"
                        if drift.get("prior_date")
                        else ""
                    )
                )
            print(
                "Claims previously verified against these sources may need re-checking."
            )
            print("!" * 60)

    # Derive the directory from the file actually written rather than re-slugging
    # the title. A handoff with a `History key:` saves under that key, so
    # re-slugging the title printed a path that does not exist.
    if markdown_path:
        report_dir = Path(markdown_path).parent
    else:
        report_dir = Path(HISTORY_ROOT) / hist._slug(report.get("article_title", ""))
    print(f"\nFull report: {report_dir}")
    if markdown_path:
        print(f"Readable review (paste into chat): {markdown_path}")
        _print_next_step(markdown_path)
    print("=" * 60)


def _print_next_step(markdown_path):
    """Point at the revision prompt that pairs with the review just written.

    The packaged templates live inside the installed package, so the path is
    neither guessable nor the same as the user's own handoff_templates/
    directory (which holds their articles, not these templates). Resolving it
    here — at the one moment the next step is obviously relevant — beats
    copying the template into the working tree, where it would drift out of
    sync with the shipped version.
    """
    prompt_path = (
        Path(__file__).parent / "handoff_templates" / "revise_after_review_prompt.md"
    )
    if not prompt_path.exists():
        return
    print(
        "\nNext step — revise the draft and reconcile its metadata in one pass:\n"
        f"  1. Open  {prompt_path}\n"
        "  2. Paste that prompt into a chat session, followed by the review above.\n"
        "  3. Save the revised draft + metadata it returns, then re-run."
    )


# ---------------------------------------------------------------------------
# Publish mode
# ---------------------------------------------------------------------------


def _suggest_seo_for_publish(pub_handoff, pub_config, api_keys):
    """Offer SEO suggestions at publish time for fields the handoff left empty.

    Draft time is where the suggestion pass earns its keep — it's free to
    regenerate each round and it feeds the revision loop. This is the safety
    net for a handoff that reached Template C with SEO METADATA still on its
    "derive from primary claim" placeholders (``_parse_seo_block`` drops those,
    so they arrive here as absent rather than as literal text).

    Triggered by the two SEO METADATA fields the push has no fallback for —
    focus keyword and meta description. The other three resolve to sensible
    defaults on their own (OG title to the article title, OG description to
    the meta description, schema type to the configured default), so a blank
    one is not a hole worth paying for a call over. Once the call is made
    though, all five fields report, since they cost nothing extra.
    """
    seo_meta = pub_handoff.get("seo") or {}
    if seo_meta.get("focus_keyword") and seo_meta.get("meta_description"):
        return

    seo_result = seo_analysis.analyze(
        pub_handoff["final_draft"],
        pub_handoff,
        seo_rules=pub_config.get("seo_rules"),
        mode=seo_analysis.PUBLISH_MODE,
    )
    suggestions, _ = seo_suggest.generate(
        pub_handoff["final_draft"],
        handoff=pub_handoff,
        pub_config=pub_config,
        api_keys=api_keys,
        seo_result=seo_result,
    )
    if not suggestions or suggestions.get("status") != "ok":
        return

    missing = [
        label
        for label, key in (
            ("focus keyword", "focus_keyword"),
            ("meta description", "meta_description"),
        )
        if not seo_meta.get(key)
    ]
    print(
        f"\nThis handoff's SEO METADATA has no {' and no '.join(missing)}. "
        "Suggestions follow — they are NOT applied to the post. To use one, "
        "cancel the push, paste it into the handoff's SEO METADATA block, and "
        "re-run."
    )
    _print_seo_suggestions(suggestions)


def run_publish_pipeline(
    handoff_path,
    publication_name,
    publish_live=False,
    config_dir="configs",
    seo_suggestions=None,
    api_key_overrides=None,
    wp_user=None,
    wp_password=None,
):
    log.info(f"Loading configs (publication={publication_name})")
    user_config = load_user_config(config_dir)
    pub_config_raw = load_publication_config(publication_name, config_dir)
    config = merge_configs(user_config, pub_config_raw)
    if api_key_overrides:
        config = apply_api_key_overrides(config, api_key_overrides)
        log.info(
            "api_key overridden via --api-key for: %s",
            ", ".join(f"{p}.{f}" for p, f in sorted(api_key_overrides)),
        )
    if wp_user is not None or wp_password is not None:
        config = apply_wordpress_overrides(
            config, username=wp_user, application_password=wp_password
        )
        log.info(
            "WordPress credentials overridden via %s",
            ", ".join(
                f
                for f, v in [("--wp-user", wp_user), ("--wp-password", wp_password)]
                if v is not None
            ),
        )

    pub_config = config["publication"]
    wp_config = pub_config.get("wordpress", {})
    rank_math_config = pub_config.get("rank_math", {})

    log.info(f"Parsing publication handoff: {handoff_path}")
    handoff_text = _read_handoff_file(handoff_path)
    pub_handoff = parse_publication_handoff(handoff_text)

    if not pub_handoff["final_draft"]:
        log.error(
            "No FINAL DRAFT section found in publication handoff. "
            "Ensure the document contains a 'FINAL DRAFT' header followed by the article text."
        )
        sys.exit(1)

    from .adapters.cms import wordpress as wp

    if seo_suggestions is not False:
        _suggest_seo_for_publish(pub_handoff, pub_config, config["api_keys"])

    confirmed = wp.print_checklist_and_confirm()
    if not confirmed:
        sys.exit(0)

    pub_params = {
        "title": pub_handoff.get("title", ""),
        "wordpress_category": pub_handoff["publication_parameters"].get(
            "wordpress_category"
        ),
        "tags": [
            t.strip()
            for t in pub_handoff["publication_parameters"].get("tags", "").split(",")
            if t.strip()
        ],
        "author": pub_handoff["publication_parameters"].get("author"),
        "seo": pub_handoff.get("seo", {}),
    }

    content = pub_handoff["final_draft"]

    log.info(f"Pushing to WordPress (status={'publish' if publish_live else 'draft'})")
    result = wp.push(
        content, pub_params, wp_config, rank_math_config, publish_live=publish_live
    )

    if result["success"]:
        print("\nWordPress push successful.")
        print(f"Post URL: {result['post_url']}")
        print(f"Post ID:  {result['post_id']}")
        if result.get("unresolved_terms"):
            # Loud, and next to the success line rather than in a log above it.
            # The post exists but is missing metadata the author asked for.
            print(f"\n{'!' * 60}")
            print(
                "WARNING: these categories/tags did not exist in WordPress and "
                "were NOT applied:"
            )
            for term in result["unresolved_terms"]:
                print(f"  - {term}")
            print("Create them in WP admin, then set them on the post.")
            print("!" * 60)
        print(
            f"Status:   {'PUBLISHED' if publish_live else 'DRAFT (pass --publish-live to publish)'}"
        )
    else:
        print(f"\nWordPress push FAILED: {result['error']}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser():
    """Construct the CLI parser.

    Split out of main() so tests can introspect the flags without running the
    pipeline — see tests/test_docs_current.py, which asserts every long-form
    flag is documented in the README.
    """
    parser = argparse.ArgumentParser(
        description="Article Review Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run ci-review --draft handoff.md --publication myblog\n"
            "  uv run ci-review --url https://example.com/post --publication myblog\n"
            "  uv run ci-review --publish pub_handoff.md --publication myblog --publish-live\n"
            "  uv run ci-review --draft handoff.md --publication myblog --verbose\n"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--draft",
        metavar="HANDOFF_PATH",
        help="Path to draft submission handoff document",
    )
    group.add_argument(
        "--url",
        metavar="URL",
        help="Fetch a published web page and run the review on its extracted content",
    )
    group.add_argument(
        "--publish", metavar="HANDOFF_PATH", help="Path to publication handoff document"
    )
    group.add_argument(
        "--raw-draft",
        metavar="DRAFT_PATH",
        help="Path to a plain draft file with no handoff headers (e.g. pasted "
        "straight out of a chat session) — the whole file is used as the "
        "article body. Skips primary_claim/target_audience/etc.; use --draft "
        "with a full handoff document for full review context, or pair with "
        "--metadata to supply that context from a separate file.",
    )
    parser.add_argument(
        "--metadata",
        metavar="METADATA_PATH",
        help="Path to a metadata file (PRIMARY CLAIM, TARGET AUDIENCE, etc., no "
        "DRAFT section) to combine with --raw-draft. Only valid alongside "
        "--raw-draft.",
    )
    parser.add_argument(
        "--publication", required=True, help="Publication config name (without .yaml)"
    )
    parser.add_argument(
        "--publish-live",
        action="store_true",
        help="Publish to live (default: save as draft)",
    )
    parser.add_argument(
        "--config-dir", default="configs", help="Directory containing config files"
    )
    parser.add_argument(
        "--cost-preset",
        choices=["economy", "standard", "balanced", "thorough", "maximum"],
        help="Override cost_preset from user.yaml for this run only (useful for calibration sweeps)",
    )
    parser.add_argument(
        "--api-key",
        metavar="PROVIDER[.FIELD]=VALUE",
        action="append",
        help="Override one credential field for this run only — the highest "
        "tier of this project's credential precedence (CLI > publication "
        "config file > user.yaml/.env > OS environment variable). Repeatable. "
        "PROVIDER=VALUE is shorthand for the common case (api_key) — openai, "
        "gemini, mistral, grok, perplexity, claude. Multi-field credentials "
        "need the explicit field: languagetool.username, languagetool.api_key, "
        "archive_org.access_key, archive_org.secret_key.",
    )
    parser.add_argument(
        "--wp-user",
        metavar="USERNAME",
        help="Override the WordPress username for this run only (--publish "
        "mode). Same precedence idea as --api-key, applied to "
        "publication.wordpress instead of api_keys.",
    )
    parser.add_argument(
        "--wp-password",
        metavar="APPLICATION_PASSWORD",
        help="Override the WordPress application password for this run only "
        "(--publish mode).",
    )
    parser.add_argument(
        "--no-seo-suggestions",
        action="store_true",
        help="Skip the SEO suggestion pass (one cheap model call proposing focus "
        "keyword candidates, a meta description, and an OG title) for this run. "
        "Set seo_rules.suggestions: false in the publication config to disable it "
        "permanently.",
    )
    parser.add_argument(
        "--no-timeout",
        action="store_true",
        help="Calibration: disable timeout truncation so true completion times are measured",
    )
    parser.add_argument(
        "--only-model",
        metavar="PROVIDER",
        help="Calibration: run only this provider (e.g. openai) instead of the full ensemble",
    )
    parser.add_argument(
        "--only-domain",
        metavar="DOMAIN",
        help="Calibration: run only this domain (e.g. fact_check, voice_style, completeness, "
        "argument_integrity, red_team)",
    )
    parser.add_argument(
        "--replay",
        metavar="RESULTS_JSON",
        help="Replay a captured ensemble instead of calling any models. Every run "
        "writes a run_N_<ts>_results.json beside its report; point at one to "
        "re-run consolidation, citations and reporting over it for free.",
    )
    parser.add_argument(
        "--retry-failed",
        metavar="RESULTS_JSON",
        help="Fill in the gaps from a prior run: make model calls only for the "
        "(model, domain) pairs marked failed in a run_N_<ts>_results.json, "
        "merging the new attempts onto everything that already succeeded. "
        "Requires the same draft-loading flags (--draft/--url/--raw-draft) as "
        "the original run, so the same runners and prompts can be rebuilt. "
        "Mutually exclusive with --replay, which makes no model calls at all.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip every pass that reaches the network (link validation, Wayback, "
        "citation resolution). Combine with --replay for a run that makes no "
        "network calls at all.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable DEBUG logging"
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.publish_live and args.draft:
        print(
            "WARNING: --publish-live has no effect in --draft mode and will be ignored."
        )

    if args.metadata and not args.raw_draft:
        parser.error("--metadata requires --raw-draft")

    if args.retry_failed and args.replay:
        parser.error(
            "--retry-failed and --replay are mutually exclusive — --replay "
            "makes no model calls at all, --retry-failed makes calls for the "
            "still-failed subset."
        )
    if args.retry_failed and args.publish:
        parser.error("--retry-failed only applies to draft review runs, not --publish")

    try:
        api_key_overrides = parse_api_key_overrides(args.api_key)
    except ValueError as e:
        parser.error(str(e))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Also write all log output to a persistent file so warnings aren't lost on scroll.
    # Daily rotation: one file per UTC day; same-day runs append to the same file.
    _log_dir = Path(HISTORY_ROOT)
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    _file_handler = logging.FileHandler(
        _log_dir / f"pipeline_{_log_date}.log", encoding="utf-8"
    )
    _file_handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    _file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logging.getLogger().addHandler(_file_handler)

    try:
        validate_publication_name(args.publication)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    try:
        if args.draft:
            run_draft_pipeline(
                args.draft,
                args.publication,
                config_dir=args.config_dir,
                cost_preset=args.cost_preset,
                no_timeout=args.no_timeout,
                only_model=args.only_model,
                only_domain=args.only_domain,
                seo_suggestions=False if args.no_seo_suggestions else None,
                replay_results=args.replay,
                retry_failed_results=args.retry_failed,
                offline=args.offline,
                api_key_overrides=api_key_overrides,
            )
        elif args.url:
            handoff = build_handoff_from_url(args.url)
            run_draft_pipeline(
                None,
                args.publication,
                config_dir=args.config_dir,
                cost_preset=args.cost_preset,
                no_timeout=args.no_timeout,
                only_model=args.only_model,
                only_domain=args.only_domain,
                seo_suggestions=False if args.no_seo_suggestions else None,
                replay_results=args.replay,
                retry_failed_results=args.retry_failed,
                offline=args.offline,
                handoff=handoff,
                api_key_overrides=api_key_overrides,
            )
        elif args.raw_draft:
            raw_text = _read_handoff_file(args.raw_draft)
            if args.metadata:
                metadata_text = _read_handoff_file(args.metadata)
                handoff = build_handoff_from_raw_draft_and_metadata(
                    raw_text, metadata_text, source_name=Path(args.raw_draft).stem
                )
            else:
                handoff = build_handoff_from_raw_text(
                    raw_text, source_name=Path(args.raw_draft).stem
                )
            run_draft_pipeline(
                None,
                args.publication,
                config_dir=args.config_dir,
                cost_preset=args.cost_preset,
                no_timeout=args.no_timeout,
                only_model=args.only_model,
                only_domain=args.only_domain,
                seo_suggestions=False if args.no_seo_suggestions else None,
                replay_results=args.replay,
                retry_failed_results=args.retry_failed,
                offline=args.offline,
                handoff=handoff,
                api_key_overrides=api_key_overrides,
            )
        elif args.publish:
            run_publish_pipeline(
                args.publish,
                args.publication,
                publish_live=args.publish_live,
                config_dir=args.config_dir,
                seo_suggestions=False if args.no_seo_suggestions else None,
                api_key_overrides=api_key_overrides,
                wp_user=args.wp_user,
                wp_password=args.wp_password,
            )
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to fetch URL: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
