"""Deciding whether two quoted passages point at the same place in the draft.

Three separate answers to this question had grown up in the codebase, and none
of them handled the way an ensemble actually duplicates itself:

* ``consolidation._passage_key`` — lowercase, collapse whitespace, truncate to
  250 characters, compare for equality. Described as "fuzzy cross-model
  matching"; it is prefix-exact. Two models quoting the same sentence at
  different lengths never matched, and two genuinely different passages sharing
  a 250-character prefix silently merged.
* ``pipeline._is_duplicate_claim`` — Jaccard over token sets at 0.9.
* the citation resolver's own matching.

Every model is quoting *one* draft, so the duplicates are near-identical
quotations of one claim: the same sentence with different punctuation, a
different apostrophe encoding, or one clause more at either end. Jaccard at 0.9
missed all of those — 36 of the 45 citation claims collected on 2026-09-03 were
wholly contained in another claim and every one was treated as distinct.

What this deliberately does *not* do is merge a sentence into a long paragraph
that contains it. Containment cannot distinguish "a fuller quote of the same
claim" from "a different claim in the same paragraph" — both are substrings —
and the caller treats the merged weight as its strongest signal. An early
version with a permissive ratio fused four separate assertions from one
paragraph ("the engine is still being built", "it's not a plugin I installed",
"releasing it on GitHub") into a single 27-flag group weighted 29.2 and printed
it as the run's top consensus finding. Missing a merge costs a duplicate row;
inventing one manufactures agreement that no two models expressed.

So the test is containment, held to near-identity by three guards:

``_MIN_SHARED_TOKENS``
    A short fragment is contained in almost anything. Below this many content
    tokens, only equality or Jaccard counts. "I have a family." is four tokens;
    it must not merge into every paragraph that happens to contain it.

``_MIN_LENGTH_RATIO``
    The shorter passage must be most of the longer one, which is what keeps
    "same claim, quoted slightly differently" apart from "different claim,
    same paragraph".

Representative-only grouping
    In :func:`group_passages`, membership is decided against the group's
    longest passage and never against another member, so the ratio guard cannot
    be walked around one hop at a time.
"""

import re

__all__ = ["normalise", "tokenise", "same_passage", "group_passages"]

#: Below this many tokens a passage is too short for containment to mean
#: anything — "I have a family." is inside any paragraph that contains it.
_MIN_SHARED_TOKENS = 6

#: The shorter passage must be at least this fraction of the longer one, or two
#: models quoting different sentences of one paragraph would read as agreement.
#:
#: Set high deliberately. Containment cannot tell "the same claim quoted at two
#: lengths" from "two different claims in one paragraph" — both are substrings.
#: At 0.35 the real 2026-09-03 draft collapsed a paragraph's four separate
#: assertions ("the engine is still being built", "it's not a plugin I
#: installed", "releasing it on GitHub") into one 27-flag group weighted 29.2,
#: which is a fabricated consensus of exactly the kind this module's caller
#: treats as its strongest signal. Missing a merge costs a duplicate row;
#: inventing one corrupts Section 1.
_MIN_LENGTH_RATIO = 0.6

#: Fraction of the shorter passage's tokens that must appear in the longer one.
_CONTAINMENT = 0.95

#: Fallback for passages that overlap heavily without either containing the
#: other — paraphrases, or the same sentence with a clause added.
_SIMILARITY = 0.85

_WORD = re.compile(r"[a-z0-9]+")

#: Drafts reach here having been through a chat client, a handoff file and a
#: JSON round-trip, and the same sentence arrives spelled several ways: a
#: straight apostrophe, a curly one, a stray C1 control byte where a curly quote
#: was mis-decoded, and the HTML entity. All four appeared in the 2026-09-03
#: run for one sentence. Left alone they defeat matching outright, and `&#39;`
#: is worse than that — the tokeniser reads the digits and invents a `39` token
#: that no other spelling of the sentence has.
_ENTITY = re.compile(r"&(?:#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def normalise(passage):
    """Lowercase, strip entities and control bytes, collapse whitespace.

    No truncation. The old 250-character cap is gone: it was the cause of the
    false-merge half of the bug, and nothing downstream needs a bounded key.
    """
    text = _ENTITY.sub(" ", passage or "")
    text = _CONTROL.sub(" ", text)
    return " ".join(text.lower().split())


def tokenise(passage):
    """Content tokens of ``passage`` as a set, for overlap tests."""
    return frozenset(_WORD.findall(normalise(passage)))


def same_passage(a, b):
    """True if ``a`` and ``b`` quote the same place in the draft.

    Accepts either strings or pre-computed token sets, so a caller looping over
    many comparisons can tokenise once.
    """
    ta = a if isinstance(a, frozenset) else tokenise(a)
    tb = b if isinstance(b, frozenset) else tokenise(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True

    shared = len(ta & tb)
    union = len(ta | tb)

    if union and shared / union >= _SIMILARITY:
        return True

    smaller, larger = (len(ta), len(tb)) if len(ta) <= len(tb) else (len(tb), len(ta))
    if smaller < _MIN_SHARED_TOKENS:
        return False
    if smaller / larger < _MIN_LENGTH_RATIO:
        return False
    return shared / smaller >= _CONTAINMENT


def group_passages(items, text_of):
    """Cluster ``items`` so that each group holds one passage's worth of findings.

    ``text_of(item)`` returns the quoted passage for an item. Returns a list of
    ``(representative_text, [items])``, representative being the *longest*
    passage in the group — the most complete quotation of the shared place.

    An item joins a group only if it matches that group's **representative** —
    the longest passage in it — never merely some member.

    That restriction is the whole safety property. Single-link clustering let
    membership chain: A matches B, B matches C, so A and C share a group even
    though A and C fail a direct test. On the real 2026-09-03 draft that chained
    a paragraph's four separate assertions together into one 27-flag group
    weighted 29.2, presented as the run's strongest consensus. Comparing against
    the representative bounds every member to one hop from the same container,
    so the length-ratio guard actually holds instead of being walked around a
    step at a time.

    Sorting longest-first is what makes that work regardless of input order: the
    container is established before the fragments that belong to it, so a
    fragment never misses its group merely because it arrived first. Without the
    sort, a sentence A inside a clause-group B inside a paragraph C gave one
    group for input order ABC and two for ACB. The tie-break on text keeps
    equal-length passages deterministic too.
    """
    prepared = []
    for item in items:
        text = text_of(item)
        toks = tokenise(text)
        if not toks:
            continue
        prepared.append((text, toks, item))

    prepared.sort(key=lambda p: (-len(p[1]), p[0]))

    groups = []  # [{"rep_tokens": frozenset, "items": [...], "texts": [...]}]
    for text, toks, item in prepared:
        target = next((g for g in groups if same_passage(toks, g["rep_tokens"])), None)
        if target is None:
            groups.append({"rep_tokens": toks, "items": [item], "texts": [text]})
        else:
            target["items"].append(item)
            target["texts"].append(text)

    return [(_representative(g["texts"]), g["items"]) for g in groups]


def _representative(texts):
    """Pick the quotation that should stand for a group.

    Cleanest first, then longest. Models corrupt the passages they echo back:
    one sentence of the 2026-09-03 draft came back as ``It's``, ``It’s``,
    ``It\\x19s`` and ``It&#39;s`` from four different models, while the draft
    itself held nothing but plain apostrophes. Ranking on length alone made
    whichever mangled variant happened to be longest the heading a human then
    read in the report. Group members are near-identical by construction, so
    preferring a clean one costs nothing in completeness.
    """
    return max(texts, key=lambda t: (-_artifacts(t), len(t)))


def _artifacts(text):
    """How many entity or control-byte artifacts a quotation carries."""
    return len(_ENTITY.findall(text)) + len(_CONTROL.findall(text))
