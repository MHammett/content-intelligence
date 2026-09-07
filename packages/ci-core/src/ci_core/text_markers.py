"""Detect and optionally strip machine-authorship markers from article text.

Two different problems wear the same name, and only one of them is real today.

There is no recoverable watermark in the text these tools handle. No provider in
this pipeline embeds a signal in API output that survives the loop, and a
logit-biased scheme could not: the review models never write the article, and
the revise step rewrites whatever the draft model produced. Nothing in
``pipeline_history/`` carries a zero-width character. That was measured, not
assumed.

What is real is *residue*. Model output arrives with typography a keyboard does
not produce -- em dashes at ten times an author's own rate, curly quotes,
non-breaking spaces -- and it rides into the article on every paste-back from
the revise step. Stripping it is cosmetic, not cryptographic, and this module
says so rather than implying the text is afterwards "clean".

The scanner is built for the watermark that does not exist yet. A fixed list of
bad characters can only find what was known when it was written, so detection
here keys on Unicode *classes* instead: format characters, private-use and
unassigned code points, separators, orphaned variation selectors, and scripts
mixed inside a single word. A future marker built from any of those is caught
without a change here.

For a marker built from none of them there is :func:`inventory`, which counts
every distinct non-ASCII code point in a text and judges nothing. That division
is deliberate. An earlier draft of this module had a fifth verdict for
characters it could not account for, which turned out to be empty by
construction -- once the classes above are handled, every remaining assigned
code point is an ordinary letter, mark, number, punctuation mark or symbol, so
the bucket either stayed empty or fired on a check mark in a quoted tweet. A
census carries the same information honestly: diff two drafts, and a code point
that was not there last week is visible without anything having had to guess in
advance that it was worth flagging.

Two things this deliberately does not do. It does not touch prose style; banned
phrasing is voice-profile territory (``banned_phrases`` in the domain config),
and rewording is not a character-level operation. And it does not promise
undetectability: classifiers key on sentence rhythm and word choice far more
than on punctuation, so a clean scan here is not evidence about them.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

# Variation selectors. VS1-16 sit in the BMP, VS17-256 in a supplement, and
# both are category Mn -- *not* Cf, which is the trap: a scanner that strips
# format characters and calls it done misses the supplement entirely, and the
# supplement is the channel in current use for hiding bytes in text. Legitimate
# use is a single selector fixing the presentation of the symbol before it.
_VS_BMP = range(0xFE00, 0xFE10)
_VS_SUPPLEMENT = range(0xE0100, 0xE01F0)

# Tag characters: a full ASCII alphabet with no rendering, originally for
# language tagging, deprecated for it, and now the other half of the
# emoji-smuggling technique. Category Cf, so the class check below already
# covers them; named here because a hit means something quite specific.
_TAGS = range(0xE0000, 0xE0080)

# Invisible by category. Cf is format (zero-width space/joiner, BOM, soft
# hyphen, word joiner, bidi controls, invisible math operators); Co is private
# use, where a vendor may define anything it likes and nothing renders it. Zl
# and Zp are the line and paragraph separators that survive a copy out of a web
# view.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Co", "Zl", "Zp"})

# Damage, not marking, and the distinction is worth a separate verdict because
# the response differs: a marker should be removed, a decode failure should be
# investigated upstream and the bytes recovered. Cn is unassigned and Cs a lone
# surrogate -- neither can arrive in text that decoded correctly -- and U+FFFD
# is a decoder's own note that it gave up on a byte.
#
# This class exists because the corpus had some. A citation fetch pulled down a
# PDF, the resolver stored the raw binary as that source's content summary, and
# it reached the review report as 340 replacement characters and a scatter of
# unassigned code points from unrelated scripts. Grouping those under
# "invisible" would have implied the fix was to delete them, which would have
# destroyed the evidence and left the resolver still doing it.
_ANOMALY_CATEGORIES = frozenset({"Cn", "Cs"})
_REPLACEMENT_CHAR = "\ufffd"  # written as an escape on purpose: a
# literal here is invisible to review and degrades silently through tooling,
# which is the failure this module exists to catch.

# Visible, legitimate, and not a keyboard's default. Tier two only: an em dash
# is punctuation, not evidence, and flattening one is a style decision the
# author gets to make rather than a cleanup.
_TYPOGRAPHY = {
    "—": "--",  # em dash
    "–": "-",  # en dash
    "‒": "-",  # figure dash
    "―": "--",  # horizontal bar
    "‘": "'",  # left single quote
    "’": "'",  # right single quote / curly apostrophe
    "‚": "'",  # single low-9 quote
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "„": '"',  # double low-9 quote
    "…": "...",  # horizontal ellipsis
    "′": "'",  # prime
    "″": '"',  # double prime
    "−": "-",  # minus sign
    # Both render as a hyphen and neither is the one the key produces, which is
    # what makes them worth listing: U+2011 turned up 376 times in the corpus,
    # more than the en dash, in text nobody typed it into.
    "‐": "-",  # hyphen (U+2010, not hyphen-minus)
    "‑": "-",  # non-breaking hyphen
}

# Letters that render as Latin but are not. Detection does not depend on this
# table -- that part is generic, below -- but substitution does, and
# substitution guesses at intent, so it stays explicit and short rather than
# clever.
# fmt: off
_CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p",
    "с": "c", "х": "x", "у": "y", "і": "i",
    "ј": "j", "ѕ": "s",
    "А": "A", "Е": "E", "О": "O", "Р": "P",
    "С": "C", "Х": "X", "У": "Y", "І": "I",
    "Ј": "J", "Ѕ": "S", "В": "B", "Н": "H",
    "К": "K", "М": "M", "Т": "T",
    "ο": "o", "α": "a", "ρ": "p", "υ": "u",
    "χ": "x",
    "Ο": "O", "Α": "A", "Β": "B", "Ε": "E",
    "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ρ": "P", "Τ": "T",
    "Υ": "Y", "Χ": "X",
}
# fmt: on

KIND_INVISIBLE = "invisible"
KIND_ANOMALY = "anomaly"
KIND_WHITESPACE = "whitespace"
KIND_TYPOGRAPHY = "typography"
KIND_HOMOGLYPH = "homoglyph"

# Stripped by the default pass. Everything else is either visible -- and
# changing visible text without being asked is editing rather than cleaning --
# or an anomaly, where removal would hide a bug rather than fix one.
SAFE_KINDS = frozenset({KIND_INVISIBLE, KIND_WHITESPACE})

# Ordering for reports: never-innocent first, then damage, then the judgement
# calls, then the cosmetic ones. Frequency breaks ties within a kind.
_KIND_ORDER = {
    KIND_INVISIBLE: 0,
    KIND_ANOMALY: 1,
    KIND_HOMOGLYPH: 2,
    KIND_WHITESPACE: 3,
    KIND_TYPOGRAPHY: 4,
}

# Positions kept per finding. A heavily-marked file should not produce a report
# longer than the article it describes; the count carries the magnitude.
_MAX_POSITIONS = 20


@dataclass
class CodePoint:
    """One distinct non-ASCII code point and how often it occurs.

    Deliberately verdict-free. ``marker_kind`` reports what :func:`scan` made of
    it, empty when scan had no opinion, and an empty verdict is not a claim that
    the character is fine -- only that nothing here recognised it.
    """

    char: str
    name: str
    category: str
    count: int = 0
    marker_kind: str = ""

    @property
    def codepoint(self) -> str:
        return "U+%04X" % ord(self.char)


@dataclass
class Finding:
    """One code point, with where it occurs and what removing it would cost.

    ``count`` and ``positions`` are kept separately because a marker appearing
    900 times is a different story from one appearing twice, and only the count
    is complete.
    """

    char: str
    kind: str
    name: str
    category: str
    count: int = 0
    positions: list[int] = field(default_factory=list)
    note: str = ""

    @property
    def codepoint(self) -> str:
        return "U+%04X" % ord(self.char)

    @property
    def replacement(self) -> str:
        """What a sanitize pass would put in this character's place.

        Returning the character unchanged is how a finding says "reportable but
        not mine to rewrite": an anomaly, or a homoglyph outside the table.
        """
        if self.kind == KIND_INVISIBLE:
            return ""
        if self.kind == KIND_WHITESPACE:
            return " "
        if self.kind == KIND_TYPOGRAPHY:
            return _TYPOGRAPHY.get(self.char, self.char)
        if self.kind == KIND_HOMOGLYPH:
            return _CONFUSABLES.get(self.char, self.char)
        return self.char

    @property
    def removable(self) -> bool:
        """False for a finding this module can report but not safely rewrite.

        An unexpected code point has no known-good substitute by definition, and
        a homoglyph outside the table is a guess. Both are still worth showing,
        which is the reason to distinguish the two rather than drop them.
        """
        return self.replacement != self.char


def _char_name(ch: str) -> str:
    try:
        return unicodedata.name(ch)
    except ValueError:
        # Unassigned, or a control character. The code point is the identity
        # anyway, and an unnamed one is more suspicious rather than less.
        return "<unnamed>"


def _script_of(ch: str) -> str:
    """First word of the Unicode name, which for a letter is its script.

    Python ships no script property, and this stands in for one: "CYRILLIC
    SMALL LETTER A" and "LATIN SMALL LETTER A" differ in exactly the way the
    mixed-script check needs, using nothing outside the stdlib.
    """
    return _char_name(ch).split(" ", 1)[0]


def _is_orphan_variation_selector(text: str, i: int) -> bool:
    """True when the selector at ``i`` carries data rather than styling.

    A selector is doing its job when it follows a symbol it can restyle, and
    only one can. So the rule is positional rather than a test of the character
    itself: the first selector after a symbol is legitimate, one after a letter
    or a space is not, and the second onward in a run is not regardless of what
    the run follows -- a string of them after a single emoji is the smuggling
    pattern itself, one selector per hidden byte.
    """
    if i == 0:
        return True
    prev = text[i - 1]
    if ord(prev) in _VS_BMP or ord(prev) in _VS_SUPPLEMENT:
        return True  # second or later in a run
    return unicodedata.category(prev) not in ("So", "Sk")


def _classify(text: str, i: int) -> tuple[str, str] | None:
    """Return ``(kind, note)`` for the character at ``i``, or None if ordinary.

    Order matters. The invisible classes come first because they are the ones
    that are never legitimate, and a character that is both invisible and odd
    should be reported as the former.
    """
    ch = text[i]
    cp = ord(ch)

    if ch in ("\n", "\r", "\t", " "):
        return None

    if cp in _VS_BMP or cp in _VS_SUPPLEMENT:
        if _is_orphan_variation_selector(text, i):
            return (
                KIND_INVISIBLE,
                "variation selector not styling a preceding symbol -- the "
                "channel currently used to hide bytes inside text",
            )
        return None

    if cp in _TAGS:
        return (
            KIND_INVISIBLE,
            "tag character: an invisible ASCII alphabet, used to smuggle "
            "text inside other text",
        )

    category = unicodedata.category(ch)

    if category in _INVISIBLE_CATEGORIES:
        return KIND_INVISIBLE, ""

    if category in _ANOMALY_CATEGORIES or ch == _REPLACEMENT_CHAR:
        return (
            KIND_ANOMALY,
            "text that did not decode cleanly -- look upstream at what wrote "
            "it rather than deleting this",
        )

    if category == "Zs":
        # U+0020 returned above already; anything still here is a space that
        # looks like a space without being one.
        return KIND_WHITESPACE, ""

    if ch in _TYPOGRAPHY:
        return KIND_TYPOGRAPHY, ""

    # Everything still here is visible: a letter, a mark, a number, a
    # punctuation mark, a symbol. A visible character cannot be a covert marker,
    # and an accented name, a degree sign or an emoji in a quoted post is
    # ordinary content -- judging any of it would only teach the author to
    # scroll past the report. Letters get one more look in the mixed-script
    # pass, where a whole Cyrillic word reads as a quotation and a single
    # Cyrillic letter inside a Latin word reads as a substitution; that needs
    # the surrounding word, so it cannot happen here. The rest is counted by
    # `inventory` and left alone.
    return None


def _word_spans(text: str):
    """Yield ``(start, end)`` for each run of letters, apostrophes included.

    Apostrophes belong inside the word so that "don't" stays one word --
    otherwise a curly apostrophe would split every contraction and the
    mixed-script check would be looking at fragments.
    """
    start = None
    for i, ch in enumerate(text):
        if unicodedata.category(ch).startswith("L") or ch in ("'", "’"):
            if start is None:
                start = i
        elif start is not None:
            yield start, i
            start = None
    if start is not None:
        yield start, len(text)


#: The SI micro prefix, in both spellings Unicode provides for it.
_MICRO_SIGNS = frozenset({"µ", "μ"})

#: Longest SI unit symbol the micro prefix can lead ("mol").
_MAX_UNIT_SYMBOL = 3


def _is_micro_unit(word: str) -> bool:
    """True for SI notation like ``µT``, ``μg``, ``µs`` -- not a substitution.

    Both spellings of the prefix defeat the mixed-script test by construction:
    ``_script_of`` reads U+00B5 as "MICRO" (the first word of "MICRO SIGN",
    which is not a script at all) and U+03BC as "GREEK", so every correctly
    written microtesla or microgram reads as a Latin word with a foreign letter
    in it. For a publication that measures magnetic fields, that failed the
    publish gate on drafts whose only sin was correct units.

    Deliberately narrow. The exemption needs the prefix in first position and
    at most a unit symbol after it, so a Greek mu standing in for a Latin "u"
    inside a real word is still the substitution this check exists to find.
    """
    if len(word) < 2 or word[0] not in _MICRO_SIGNS:
        return False
    rest = word[1:]
    return len(rest) <= _MAX_UNIT_SYMBOL and all(
        c.isascii() and c.isalpha() for c in rest
    )


def _scan_homoglyphs(text: str, findings: dict[str, Finding]) -> None:
    """Flag letters whose script differs from the rest of their own word.

    Generic on purpose. It does not ask whether a character appears in a table
    of known confusables; it asks whether a word is written in a single
    alphabet, which is the property that makes a substitution work at all. A
    confusable nobody has catalogued yet still fails that test.

    The one exception is SI notation -- see :func:`_is_micro_unit`.
    """
    for start, end in _word_spans(text):
        word = text[start:end]
        if _is_micro_unit(word):
            continue
        scripts: dict[int, str] = {}
        for offset, ch in enumerate(word):
            if unicodedata.category(ch).startswith("L"):
                scripts[offset] = _script_of(ch)
        if len(set(scripts.values())) < 2:
            continue
        present = list(scripts.values())
        majority = max(set(present), key=present.count)
        for offset, script in scripts.items():
            if script == majority:
                continue
            ch = word[offset]
            f = findings.get(ch)
            if f is None:
                f = Finding(
                    char=ch,
                    kind=KIND_HOMOGLYPH,
                    name=_char_name(ch),
                    category=unicodedata.category(ch),
                    note=f"{script} letter inside an otherwise-{majority} word",
                )
                findings[ch] = f
            f.count += 1
            if len(f.positions) < _MAX_POSITIONS:
                f.positions.append(start + offset)


def scan(text: str) -> list[Finding]:
    """Report every authorship marker in ``text`` without changing it."""
    findings: dict[str, Finding] = {}

    for i, ch in enumerate(text):
        result = _classify(text, i)
        if result is None:
            continue
        kind, note = result
        f = findings.get(ch)
        if f is None:
            f = Finding(
                char=ch,
                kind=kind,
                name=_char_name(ch),
                category=unicodedata.category(ch),
                note=note,
            )
            findings[ch] = f
        f.count += 1
        if len(f.positions) < _MAX_POSITIONS:
            f.positions.append(i)

    _scan_homoglyphs(text, findings)

    return sorted(
        findings.values(),
        key=lambda f: (_KIND_ORDER.get(f.kind, 9), -f.count, ord(f.char)),
    )


def sanitize(text: str, aggressive: bool = False) -> tuple[str, list[Finding]]:
    """Strip markers from ``text``; return the result and everything found.

    The default pass changes no glyph: it removes characters that render as
    nothing and normalises the spaces that only pretend to be one. The one
    visible consequence is that turning a non-breaking space into an ordinary
    one lets a line wrap where it previously could not, which is a layout
    change rather than a text change but is not nothing in a table or a figure.

    Anomalies are reported and never touched. A replacement character means
    bytes were lost before this function ever saw them, and deleting the scar
    would leave the wound.

    ``aggressive`` additionally flattens typography to ASCII and substitutes
    known confusables. That does change how the piece reads, which is why it is
    not the default -- an author who uses em dashes is entitled to keep them.

    The findings returned always describe the whole scan, including markers this
    pass chose not to touch. Reporting is not conditional on removal.
    """
    findings = scan(text)
    kinds = set(SAFE_KINDS)
    if aggressive:
        kinds |= {KIND_TYPOGRAPHY, KIND_HOMOGLYPH}

    replacements = {
        f.char: f.replacement
        for f in findings
        if f.kind in kinds and f.replacement != f.char
    }
    if not replacements:
        return text, findings

    out = []
    for i, ch in enumerate(text):
        if ch not in replacements:
            out.append(ch)
            continue
        # A variation selector is a marker only in some positions, so that
        # decision is re-made per occurrence rather than trusted from the
        # character alone -- the same code point can be styling here and a
        # payload three words later.
        cp = ord(ch)
        if (
            cp in _VS_BMP or cp in _VS_SUPPLEMENT
        ) and not _is_orphan_variation_selector(text, i):
            out.append(ch)
            continue
        out.append(replacements[ch])
    return "".join(out), findings


def inventory(text: str) -> list[CodePoint]:
    """Census every distinct non-ASCII code point in ``text``, judging none.

    This is the half of the module that does not need to know what a watermark
    looks like. :func:`scan` recognises the classes a covert marker can be built
    from today; this recognises nothing at all, and is therefore the part that
    still works when someone builds one out of something else.

    Use it as a diff. The interesting output is not one run but two: a code
    point that appears in this week's drafts and not last week's is worth an
    explanation, whatever category it belongs to and whether or not anything
    above thought to flag it.

    Counts are per code point, not per grapheme -- an emoji built from a base,
    a joiner and a modifier is three entries, which is the resolution the
    question needs.
    """
    verdicts = {f.char: f.kind for f in scan(text)}
    counts: dict[str, CodePoint] = {}
    for ch in text:
        if ord(ch) < 128:
            continue
        cp = counts.get(ch)
        if cp is None:
            cp = CodePoint(
                char=ch,
                name=_char_name(ch),
                category=unicodedata.category(ch),
                marker_kind=verdicts.get(ch, ""),
            )
            counts[ch] = cp
        cp.count += 1
    return sorted(counts.values(), key=lambda c: (-c.count, ord(c.char)))
