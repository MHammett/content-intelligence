"""Claims no external source can settle, and how they stay out of verification.

Fact-checking and citation resolution both answer one question: *does a document
somewhere say this?* For most of a draft that is the right question. For some of
it there is no document and there never will be, and asking anyway produces one
of two bad outcomes rather than a finding:

* **A false negative.** Measured 2026-09-05: ``"I have a side job."`` was
  resolved against ``fd-ix.com/about/team/`` — a page about a different person
  entirely — and came back ``not_addressed``. Every provider then advised
  WITHDRAWING a true first-person statement, because "the cited source does not
  support this" is what the pipeline had to say about it.
* **False confidence.** ``"Divide by seven and you get 1,024. Exactly."`` and two
  sibling sums came back ``confirmed`` with the source ``"Manual Calculation"``.
  The arithmetic is right. Nothing checked it, and ``confirmed`` is the word in
  Section 2 that sounds finished.

So this module is about *scope*, not verdicts. A claim that is out of scope was
never a candidate for external verification, and saying so is a different
statement from any of the five buckets the fact-check pass already has.

Three things decide it, and they do not have equal authority:

1. **The author marks a passage.** Absolute. Only the author knows whether "I
   have a side job" is true, and their say-so is not a hypothesis for a model to
   test. Two ways to say it: an inline ``<!-- ci:no-verify -->`` block in the
   draft, or an ``OUT OF SCOPE FOR FACT-CHECK`` section in the handoff.
2. **The publication config names a passage or narrows the categories.**
   ``fact_check_scope.exclude_passages`` is a standing author marking;
   ``exclude_types`` bounds what a model may rule out on its own.
3. **A fact-check model classifies the claim.** The models already do this
   unprompted — one described a claim as "an author's hypothesis about the
   internal logic, not [verifiable]" while filing it as a finding anyway — so
   this asks for it in the output shape instead of losing it in a reason string.

**Why the author's categories bound the model's discretion.** A model that can
declare anything out of scope can quietly decline to check a claim a source
would have settled, which is the same class of error in the other direction.
``exclude_types`` is the author's pre-agreement about which categories are
genuinely unresolvable; ``other`` is deliberately not in the default set, so a
model reaching for the catch-all is *reported* rather than *obeyed*. A claim
classified but not honoured stays in verification and is visible as a
disagreement.

**Nothing is ever dropped.** Exclusion is a move into an ``out_of_scope`` bucket
the report renders in full, carrying the reason, who decided it, and — when a
model had already reached a verdict the exclusion overrides — that verdict too.
An entry saying "the author marked this out of scope, and gemini had it as
confirmed against <url>" is information. A claim that silently vanishes is not.

**Which passes this touches.** Fact-check verdicts and citation resolution, and
nothing else. ``red_team``, ``argument_integrity``, ``voice_style`` and
``completeness`` see the whole draft exactly as before: a first-person claim can
still be an argumentative weakness or a credibility risk, and hiding it from
every reviewer to spare it from one would cost more findings than it saves.

**``--raw-draft`` carries no handoff**, so it gets the two sources that do not
need one: inline markers, which live in the draft text itself, and model
classification, which needs no author action at all. That is the intended
default — a raw draft is protected without configuring anything.
"""

from __future__ import annotations

import logging
import re

from . import passage_match

log = logging.getLogger(__name__)


#: Categories a fact-check model may use to put a claim out of scope. Closed, so
#: the response schema can enforce it and ``exclude_types`` can filter on it.
#:
#: ``other`` exists because a model with no fitting category will otherwise
#: shoehorn one, and a wrong category is harder to read than an honest catch-all.
#: It is not in :data:`DEFAULT_HONOURED_TYPES` for exactly that reason: the
#: catch-all is where an unjustified exclusion would arrive.
CLAIM_TYPES = (
    "first_person",
    "author_hypothesis",
    "internal_arithmetic",
    "subjective_judgment",
    "future_prediction",
    "other",
)

#: Honoured when the publication config says nothing — every specific category,
#: but not ``other``. See the module docstring on bounding model discretion.
DEFAULT_HONOURED_TYPES = frozenset(CLAIM_TYPES) - {"other"}

#: The fact-check buckets an author marking sweeps. All five: a first-person
#: claim filed as ``unverifiable`` still reaches citation resolution and still
#: comes back "the source does not support this", which is the finding this
#: exists to stop.
SWEPT_BUCKETS = (
    "confirmed",
    "outdated",
    "contradicted",
    "unverifiable",
    "primary_source_needed",
)

#: Where an exclusion came from, in the words the report uses.
ORIGIN_INLINE = "author marker in the draft"
ORIGIN_HANDOFF = "author's OUT OF SCOPE FOR FACT-CHECK section"
ORIGIN_CONFIG = "publication config (fact_check_scope.exclude_passages)"
ORIGIN_MODEL = "fact-check model classification"

#: Origins that are the author speaking. These outrank a model classification
#: and are honoured whatever ``exclude_types`` says.
AUTHOR_ORIGINS = frozenset({ORIGIN_INLINE, ORIGIN_HANDOFF, ORIGIN_CONFIG})


# ---------------------------------------------------------------------------
# Inline markers
# ---------------------------------------------------------------------------
#
# HTML comments, so the marking survives into WordPress without a reader ever
# seeing it, and so the draft the author edits is the draft the models are shown
# — stripping the markers first would shift every offset and leave each pass
# quoting text that does not appear in the file.

_OPEN_MARKER = re.compile(
    r"<!--\s*ci:no-verify\s*(?::\s*(?P<reason>.*?))?\s*-->",
    re.IGNORECASE | re.DOTALL,
)
_CLOSE_MARKER = re.compile(r"<!--\s*/\s*ci:no-verify\s*-->", re.IGNORECASE)

#: Either marker, for stripping them out of text before it is normalised or
#: quoted back at the author.
_ANY_MARKER = re.compile(
    r"<!--\s*/?\s*ci:no-verify\s*(?::.*?)?\s*-->", re.IGNORECASE | re.DOTALL
)


def strip_markers(text):
    """The text with any ``ci:no-verify`` marker removed."""
    return _ANY_MARKER.sub(" ", str(text or ""))


def parse_inline_markers(draft):
    """Passages the draft itself marks out of scope.

    ``<!-- ci:no-verify -->`` opens a block and ``<!-- /ci:no-verify -->`` closes
    it; ``<!-- ci:no-verify: only I can confirm this -->`` carries a reason.

    **A marker that is never closed is warned about and ignored.** The two ways
    to get this wrong are not symmetric: excluding more than intended is
    invisible in the report, while excluding nothing is obvious the moment the
    claim shows up fact-checked. So an ambiguous marker fails toward checking.
    """
    text = str(draft or "")
    events = []
    for m in _OPEN_MARKER.finditer(text):
        events.append((m.start(), "open", m))
    for m in _CLOSE_MARKER.finditer(text):
        events.append((m.start(), "close", m))
    events.sort(key=lambda e: e[0])

    found = []
    open_match = None
    for _pos, kind, match in events:
        if kind == "open":
            if open_match is not None:
                log.warning(
                    "Draft has a nested <!-- ci:no-verify --> marker at offset %d; "
                    "the enclosing block already covers it, so it is ignored.",
                    match.start(),
                )
                continue
            open_match = match
            continue
        if open_match is None:
            log.warning(
                "Draft has a closing <!-- /ci:no-verify --> at offset %d with no "
                "opening marker before it; ignored.",
                match.start(),
            )
            continue
        passage = text[open_match.end() : match.start()]
        reason = (open_match.groupdict().get("reason") or "").strip()
        if passage.strip():
            found.append(_exclusion(passage, reason, ORIGIN_INLINE))
        else:
            log.warning(
                "Draft has an empty <!-- ci:no-verify --> block at offset %d; "
                "nothing to exclude.",
                open_match.start(),
            )
        open_match = None

    if open_match is not None:
        log.warning(
            "Draft has an unclosed <!-- ci:no-verify --> marker at offset %d — "
            "add <!-- /ci:no-verify --> where the passage ends. Ignoring it, so "
            "that passage IS fact-checked this run.",
            open_match.start(),
        )
    if found:
        log.info(
            "Fact-check scope: %d passage(s) marked out of scope inline in the draft",
            len(found),
        )
    return found


# ---------------------------------------------------------------------------
# Handoff section
# ---------------------------------------------------------------------------

#: Placeholder answers the templates suggest for a section with nothing in it.
#: Same convention UNCERTAIN SECTIONS and KNOWN GAPS already use.
_NONE_SENTINELS = frozenset(
    {
        "none",
        "none.",
        "n/a",
        "na",
        "-",
        "none provided",
        "none provided.",
        "none identified",
        "none identified.",
        "none identified by author",
        "none identified by author.",
        "nothing",
    }
)

#: A line the author never filled in — the template's own bracketed guidance.
_PLACEHOLDER_LINE = re.compile(r"^\[.*\]$", re.DOTALL)

#: How a reason is separated from its passage on one line. Em dash first, since
#: that is what the templates use; the ASCII forms are for anyone typing it.
_REASON_SPLIT = re.compile(r"\s+(?:—|--|\|)\s+")


def parse_handoff_section(section_text):
    """Passages listed under ``OUT OF SCOPE FOR FACT-CHECK`` in the handoff.

    One entry per bullet, ``- passage — reason``; the reason is optional. A
    section with no bullets is read a paragraph at a time, so an author who
    writes prose still gets the passages they named.
    """
    text = str(section_text or "").strip()
    if not text or text.lower() in _NONE_SENTINELS:
        return []

    bullets = [
        line.strip()[1:].strip()
        for line in text.splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    entries = bullets or [p.strip() for p in re.split(r"\n\s*\n", text)]

    found = []
    for entry in entries:
        if not entry or entry.lower() in _NONE_SENTINELS:
            continue
        if _PLACEHOLDER_LINE.match(entry):
            continue
        passage, reason = _split_reason(entry)
        if passage:
            found.append(_exclusion(passage, reason, ORIGIN_HANDOFF))
    if found:
        log.info(
            "Fact-check scope: %d passage(s) marked out of scope in the handoff",
            len(found),
        )
    return found


def _split_reason(entry):
    parts = _REASON_SPLIT.split(entry, maxsplit=1)
    passage = parts[0].strip().strip("\"'“”")
    reason = parts[1].strip() if len(parts) > 1 else ""
    return passage, reason


# ---------------------------------------------------------------------------
# Matching a claim to a marked passage
# ---------------------------------------------------------------------------

#: Below this many normalised characters, a passage matches half the draft.
#: A marking that short is a mistake worth failing closed on.
_MIN_MATCH_CHARS = 12

#: Token-overlap threshold for a claim that paraphrases a marked passage rather
#: than quoting it. Same value and reasoning as ``pipeline._CLAIM_SIMILARITY``:
#: high on purpose, because a wrong match here silently removes a claim from
#: verification.
_SIMILARITY = 0.9


def _normalise(text):
    """Normalise for matching, via ``passage_match``.

    Not a second normaliser: ``passage_match.normalise`` already folds the four
    spellings one apostrophe arrives in — straight, curly, a mis-decoded C1
    control byte, and the ``&#39;`` entity, all four seen for one sentence in a
    single run — and a private copy here would drift from it. Punctuation is
    deliberately kept, since containment is tested on the string.
    """
    return passage_match.normalise(text)


def _content_words(text):
    return passage_match.tokenise(text)


def _exclusion(passage, reason, origin):
    text = str(passage or "").strip()
    normalised = _normalise(strip_markers(text))
    return {
        "passage": text,
        "reason": reason or "",
        "origin": origin,
        "_normalised": normalised,
        "_words": _content_words(normalised),
    }


def _matches(claim_norm, claim_words, exclusion):
    """Whether a claim falls inside the passage ``exclusion`` covers.

    **This is deliberately not ``passage_match.same_passage``,** and the
    difference is the point rather than an oversight. That function answers "are
    these two quotations of the same claim", and it refuses on purpose to merge
    a sentence into the paragraph containing it — because its caller counts
    matches as agreement between models, and fusing a paragraph's four separate
    assertions into one group manufactured a 27-flag consensus that no two
    models had expressed.

    The question here is the opposite one: "does this claim fall inside a region
    the author marked". An author marks a paragraph and a model quotes one
    sentence of it — that is the ordinary case, and the containment
    ``same_passage`` rejects is exactly what has to succeed. Getting it wrong
    costs nothing like the same: an over-broad marking excludes a claim the
    author chose to exclude, which the report then lists by name.

    Containment is tested in both directions, because the two real cases pull
    opposite ways: an author marks a paragraph and a model quotes a sentence of
    it, or an author marks a sentence and a model quotes the paragraph around
    it. The token-overlap pass then catches a model paraphrasing rather than
    quoting.
    """
    passage_norm = exclusion["_normalised"]
    if not claim_norm or not passage_norm:
        return False
    shorter, longer = sorted((claim_norm, passage_norm), key=len)
    if len(shorter) >= _MIN_MATCH_CHARS and shorter in longer:
        return True
    passage_words = exclusion["_words"]
    if not claim_words or not passage_words:
        return False
    union = len(claim_words | passage_words)
    return bool(union) and len(claim_words & passage_words) / union >= _SIMILARITY


# ---------------------------------------------------------------------------
# The rules for one run
# ---------------------------------------------------------------------------


class ScopeRules:
    """What this run treats as out of scope for external verification.

    Built once per run and used in three places: the fact-check prompt (so the
    models are told rather than only overruled), the merged fact-check section
    (so the claims move buckets), and citation resolution (so they are never
    fetched).
    """

    def __init__(
        self,
        exclusions=(),
        honoured_types=DEFAULT_HONOURED_TYPES,
        trust_model_classification=True,
    ):
        self.exclusions = list(exclusions)
        self.honoured_types = frozenset(honoured_types)
        self.trust_model_classification = bool(trust_model_classification)

    def __bool__(self):
        return bool(self.exclusions) or self.trust_model_classification

    @classmethod
    def from_run(cls, draft, handoff, pub_config):
        """Assemble the rules from the draft, the handoff and the publication config.

        A ``--raw-draft`` run supplies no handoff section, which is why inline
        markers and model classification both exist — see the module docstring.
        """
        cfg = (pub_config or {}).get("fact_check_scope") or {}
        exclusions = (
            parse_inline_markers(draft)
            + parse_handoff_section((handoff or {}).get("out_of_scope", ""))
            + _config_exclusions(cfg)
        )
        return cls(
            exclusions=exclusions,
            honoured_types=_honoured_types(cfg),
            trust_model_classification=cfg.get("trust_model_classification", True),
        )

    def author_exclusion_for(self, claim):
        """The author marking covering ``claim``, or None."""
        normalised = _normalise(claim)
        if not normalised:
            return None
        words = _content_words(normalised)
        for exclusion in self.exclusions:
            if _matches(normalised, words, exclusion):
                return exclusion
        return None

    def honours(self, claim_type):
        """Whether a model classification of ``claim_type`` is acted on."""
        if not self.trust_model_classification:
            return False
        return str(claim_type or "").strip().lower() in self.honoured_types

    def prompt_block(self):
        """The author-marked passages, as a block for the fact-check prompt.

        Empty when the author marked nothing, so the prompt reads normally for
        the common case. Passages are truncated: the model needs enough to
        recognise the passage in a draft it already has in full, not a second
        copy of it.
        """
        if not self.exclusions:
            return ""
        lines = [
            "The author has marked these passages out of scope. Put any claim "
            'drawn from them in "out_of_scope" — do not verify them, and do not '
            "report them in any other bucket, even if a source exists:"
        ]
        for i, exclusion in enumerate(self.exclusions, 1):
            passage = " ".join(strip_markers(exclusion["passage"]).split())
            if len(passage) > 300:
                passage = passage[:300].rstrip() + " […]"
            reason = (
                f" — author's reason: {exclusion['reason']}"
                if exclusion["reason"]
                else ""
            )
            lines.append(f'{i}. "{passage}"{reason}')
        return "\n".join(lines)

    # -- enforcement --------------------------------------------------------

    def apply(self, fact_check):
        """Separate out-of-scope claims from the merged fact-check section.

        Returns a new dict. Author markings pull matching claims out of all five
        verdict buckets; model classifications are honoured or not according to
        ``exclude_types``, and either way stay visible. The verdict an exclusion
        overrides travels with it as ``withdrawn_verdicts`` rather than being
        discarded — that disagreement is the reader's to judge.
        """
        if not fact_check:
            return fact_check

        entries = []
        by_key = {}

        def record(claim, origin, reason, claim_type, model, excluded):
            """Fold one model's line about ``claim`` into a single entry.

            ``model`` is recorded in ``classified_by`` only when it is the model
            that made the scope call. A model whose *verdict* was overridden by
            an author marking said nothing about scope, and listing it as though
            it had would attribute the author's decision to four models that
            never made it — its verdict lands in ``withdrawn_verdicts`` instead.
            """
            key = _normalise(claim)
            entry = by_key.get(key)
            if entry is None:
                entry = {
                    "claim": claim,
                    "excluded": excluded,
                    "excluded_by": origin,
                    "claim_type": claim_type or "",
                    "reason": reason or "",
                    "classified_by": [],
                    "withdrawn_verdicts": [],
                }
                by_key[key] = entry
                entries.append(entry)
            else:
                # The author outranks a model, so an author origin arriving
                # second replaces one already recorded; a second model just
                # joins the list.
                if (
                    origin in AUTHOR_ORIGINS
                    and entry["excluded_by"] not in AUTHOR_ORIGINS
                ):
                    entry["excluded_by"] = origin
                    entry["reason"] = reason or entry["reason"]
                entry["excluded"] = entry["excluded"] or excluded
                if claim_type and not entry["claim_type"]:
                    entry["claim_type"] = claim_type
            if claim_type and model and model not in entry["classified_by"]:
                entry["classified_by"].append(model)
            return entry

        # 1. What the models themselves classified.
        for item in fact_check.get("out_of_scope") or []:
            claim = item.get("claim", "")
            if not claim:
                continue
            claim_type = str(item.get("claim_type", "") or "").strip().lower()
            author = self.author_exclusion_for(claim)
            if author is not None:
                record(
                    claim,
                    author["origin"],
                    author["reason"] or item.get("reason", ""),
                    claim_type,
                    item.get("source_model", ""),
                    True,
                )
                continue
            honoured = self.honours(claim_type)
            reason = item.get("reason", "")
            if not honoured:
                reason = (
                    f"{reason} [Classified {claim_type or 'out of scope'}, which "
                    f"this publication does not exclude on a model's say-so, so "
                    f"the claim stayed in verification.]"
                ).strip()
            record(
                claim,
                ORIGIN_MODEL,
                reason,
                claim_type,
                item.get("source_model", ""),
                honoured,
            )

        # 2. Sweep the verdict buckets.
        #
        # An exclusion has to reach every bucket, not just the one the deciding
        # model wrote in. Models disagree about the same sentence — in a real
        # run "Accuracy is the entire premise of this project" came back
        # `confirmed` from perplexity, `unverifiable` from mistral and
        # out-of-scope from gemini — and leaving those verdicts in place while
        # holding the claim out of Section 9 would print the same claim in two
        # places with nothing connecting them. That is the silent disagreement
        # this is supposed to prevent, so an honoured classification withdraws
        # the other verdicts and records each one against the entry: "gemini
        # ruled this out of scope; perplexity had it confirmed citing <url>" is
        # one legible finding where two contradictory ones used to sit.
        #
        # One dissenting model can therefore pull a claim several models
        # verified. That is the intended direction: an evidence-first rule —
        # "any real source_url overrules a scope call" — was the alternative,
        # and it fails on exactly the case this exists for, because the false
        # `confirmed` for "I have a side job" carried a real, openable URL to a
        # page about somebody else. Nothing is lost either way: the verdict and
        # its source are printed, and `trust_model_classification: false` or a
        # narrower `exclude_types` turns the whole path off.
        result = dict(fact_check)
        honoured = [
            (_exclusion(entry["claim"], entry["reason"], entry["excluded_by"]), entry)
            for entry in entries
            if entry["excluded"] and entry["claim"]
        ]
        for bucket in SWEPT_BUCKETS:
            kept = []
            for item in fact_check.get(bucket) or []:
                claim = item.get("claim", "")
                if not claim:
                    kept.append(item)
                    continue
                author = self.author_exclusion_for(claim)
                entry = None
                if author is not None:
                    entry = record(
                        claim,
                        author["origin"],
                        author["reason"],
                        "",
                        item.get("source_model", ""),
                        True,
                    )
                else:
                    entry = _first_match(claim, honoured)
                if entry is None:
                    kept.append(item)
                    continue
                entry["withdrawn_verdicts"].append(
                    {
                        "model": item.get("source_model", ""),
                        "bucket": bucket,
                        # `checked` last: a confirmation demoted by
                        # `_demote_unsourced_confirmations` arrives here as
                        # `unverifiable` with the source the model originally
                        # offered moved into `checked`. Without this the record
                        # would say a verdict was withdrawn and name nothing —
                        # losing "Manual Calculation", which is the most telling
                        # part of that particular finding.
                        "source": item.get("source", "")
                        or item.get("best_candidate_source", "")
                        or item.get("checked", ""),
                        "source_url": item.get("source_url", "")
                        or item.get("best_candidate_url", "")
                        or "",
                    }
                )
            result[bucket] = kept

        # Unconditionally, even when nothing was recorded: the raw model output
        # is not the shape the report and the citation collector read, so
        # leaving it in place would let a malformed item — one with no claim
        # text, say — reach the report carrying none of the fields those two
        # rely on. Where there are no rules at all, ``_apply_scope`` never gets
        # here and the data is left exactly as it arrived.
        result["out_of_scope"] = entries
        if not entries:
            return result

        excluded = sum(1 for e in entries if e["excluded"])
        log.info(
            "Fact-check scope: %d claim(s) out of scope for verification "
            "(%d excluded from citation resolution, %d reported but still checked)",
            len(entries),
            excluded,
            len(entries) - excluded,
        )
        return result


def _first_match(claim, candidates):
    """The entry whose claim ``claim`` restates, or None.

    ``candidates`` are ``(exclusion, entry)`` pairs so the matcher's precomputed
    normalisation is reused rather than recomputed per bucket item.
    """
    normalised = _normalise(claim)
    if not normalised:
        return None
    words = _content_words(normalised)
    for exclusion, entry in candidates:
        if _matches(normalised, words, exclusion):
            return entry
    return None


def _honoured_types(cfg):
    """``exclude_types`` from the config, or the default set.

    An unrecognised name is warned about rather than ignored: a typo here means
    the category is never honoured, which looks exactly like the feature being
    off.
    """
    declared = cfg.get("exclude_types")
    if declared is None:
        return DEFAULT_HONOURED_TYPES
    if isinstance(declared, str):
        declared = [declared]
    names = {str(n).strip().lower() for n in declared if str(n).strip()}
    unknown = sorted(names - set(CLAIM_TYPES))
    if unknown:
        log.warning(
            "fact_check_scope.exclude_types names %s, which no fact-check model "
            "can return — nothing will ever match. Valid types: %s.",
            ", ".join(repr(u) for u in unknown),
            ", ".join(CLAIM_TYPES),
        )
    return frozenset(names & set(CLAIM_TYPES))


def _config_exclusions(cfg):
    """``exclude_passages`` from the publication config.

    Accepts a bare string or a ``{passage, reason}`` mapping per entry, because
    the short form is what anyone writes first.
    """
    found = []
    for raw in cfg.get("exclude_passages") or []:
        if isinstance(raw, str):
            passage, reason = raw, ""
        elif isinstance(raw, dict):
            passage = raw.get("passage", "") or raw.get("text", "")
            reason = raw.get("reason", "") or ""
        else:
            log.warning(
                "fact_check_scope.exclude_passages entry %r is neither a string "
                "nor a mapping with a 'passage' key; ignored.",
                raw,
            )
            continue
        if str(passage).strip():
            found.append(_exclusion(passage, reason, ORIGIN_CONFIG))
        else:
            log.warning(
                "fact_check_scope.exclude_passages has an entry with no passage "
                "text; ignored."
            )
    if found:
        log.info(
            "Fact-check scope: %d passage(s) marked out of scope by the "
            "publication config",
            len(found),
        )
    return found
