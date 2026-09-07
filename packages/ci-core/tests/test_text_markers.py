"""Tests for text_markers -- detecting and stripping authorship markers.

The cases that matter here are the ones where a character is a marker in one
position and legitimate in another, because that is where a scanner built from
a denylist gets it wrong in both directions: a variation selector styling an
emoji, a Cyrillic word that is a quotation rather than a substitution, an em
dash an author typed on purpose.
"""

import pytest

from ci_core.text_markers import (
    KIND_ANOMALY,
    KIND_HOMOGLYPH,
    KIND_INVISIBLE,
    KIND_TYPOGRAPHY,
    KIND_WHITESPACE,
    inventory,
    sanitize,
    scan,
)

ZWSP = "\u200b"
BOM = "\ufeff"
NBSP = "\u00a0"
VS16 = "\ufe0f"
VS17 = "\U000e0100"
VS18 = "\U000e0101"
CHECK = "✔"  # category So, a symbol a selector may legitimately restyle
CYRILLIC_A = "а"
MICRO_SIGN = "µ"  # U+00B5, the character a keyboard's µ key produces
GREEK_MU = "μ"  # U+03BC, what Unicode normalisation prefers


def kinds(findings):
    return {f.kind for f in findings}


def by_char(findings):
    return {f.char: f for f in findings}


class TestOrdinaryText:
    def test_plain_ascii_has_no_findings(self):
        assert scan("The permit allows 1,000 hours per year.") == []

    def test_plain_ascii_is_returned_unchanged(self):
        text = "The permit allows 1,000 hours per year."
        assert sanitize(text) == (text, [])

    def test_accented_names_are_not_markers(self):
        assert scan("Benoit Muller and Benoît Müller filed it.") == []

    def test_ordinary_symbols_are_not_markers(self):
        # A degree sign, a currency symbol and an emoji are content. Flagging
        # them would train the author to ignore the report.
        assert scan("It hit 95°F, cost €40, and shipped 🚚 on time.") == []

    def test_newlines_and_tabs_are_not_whitespace_findings(self):
        assert scan("one\ttwo\r\nthree\n") == []


class TestInvisible:
    @pytest.mark.parametrize(
        "ch",
        [
            ZWSP,
            BOM,
            "\u200c",  # zero width non-joiner
            "\u200d",  # zero width joiner
            "\u00ad",  # soft hyphen
            "\u2060",  # word joiner
            "\u200e",  # left-to-right mark
            "\u2062",  # invisible times
            "\u2028",  # line separator
            "\u2029",  # paragraph separator
            "\ue000",  # private use
        ],
    )
    def test_invisible_characters_are_found_and_removed(self, ch):
        out, findings = sanitize(f"The per{ch}mit allows it.")
        assert kinds(findings) == {KIND_INVISIBLE}
        assert out == "The permit allows it."

    def test_tag_characters_are_found_and_removed(self):
        # The invisible ASCII alphabet, the other half of emoji smuggling.
        payload = "\U000e0041\U000e0042\U000e0043"
        out, findings = sanitize(f"Approved{payload} today.")
        assert kinds(findings) == {KIND_INVISIBLE}
        assert out == "Approved today."

    def test_note_names_the_technique_for_tag_characters(self):
        findings = scan("Approved\U000e0041 today.")
        assert "smuggle" in findings[0].note

    def test_many_occurrences_are_counted_beyond_the_position_cap(self):
        findings = scan("a" + (ZWSP + "b") * 50)
        assert findings[0].count == 50
        assert len(findings[0].positions) == 20  # capped, count is not


class TestVariationSelectors:
    """The trap: these are category Mn, not Cf, so a format-character sweep
    misses the supplement entirely -- and the supplement is the live channel."""

    def test_single_selector_styling_an_emoji_is_left_alone(self):
        text = f"Approved {CHECK}{VS16} today."
        assert scan(text) == []
        assert sanitize(text)[0] == text

    def test_selector_after_a_letter_is_a_marker(self):
        out, findings = sanitize(f"Appro{VS16}ved today.")
        assert kinds(findings) == {KIND_INVISIBLE}
        assert out == "Approved today."

    def test_supplement_selectors_are_caught(self):
        # VS17-256 live outside the BMP and are the ones a Cf-only scan misses.
        out, findings = sanitize(f"Appro{VS17}ved today.")
        assert kinds(findings) == {KIND_INVISIBLE}
        assert out == "Approved today."

    def test_run_after_an_emoji_keeps_the_first_and_strips_the_payload(self):
        # One selector styles; a run of them carries a byte apiece. The emoji
        # and its legitimate selector must survive the strip.
        text = f"Approved {CHECK}{VS16}{VS17}{VS18} today."
        out, findings = sanitize(text)
        assert kinds(findings) == {KIND_INVISIBLE}
        assert out == f"Approved {CHECK}{VS16} today."

    def test_selector_at_the_start_of_the_text_is_a_marker(self):
        out, _ = sanitize(f"{VS16}Approved today.")
        assert out == "Approved today."

    def test_same_selector_can_be_styling_in_one_place_and_payload_elsewhere(self):
        # The decision is positional, so a per-character replacement table is
        # not enough -- this is the case that catches getting that wrong.
        text = f"{CHECK}{VS16} and Appro{VS16}ved"
        out, _ = sanitize(text)
        assert out == f"{CHECK}{VS16} and Approved"


class TestWhitespace:
    @pytest.mark.parametrize("ch", [NBSP, "\u202f", "\u2009", "\u2007", "\u200a"])
    def test_lookalike_spaces_normalise_to_a_plain_space(self, ch):
        out, findings = sanitize(f"1,000{ch}hours")
        assert kinds(findings) == {KIND_WHITESPACE}
        assert out == "1,000 hours"


class TestTypography:
    def test_em_dash_is_reported_but_kept_by_default(self):
        text = "The permit — as written — allows it."
        out, findings = sanitize(text)
        assert kinds(findings) == {KIND_TYPOGRAPHY}
        assert out == text, "default pass must not edit visible punctuation"

    def test_aggressive_flattens_dashes_and_quotes(self):
        out, _ = sanitize("He said “no” — it’s over.", aggressive=True)
        assert out == 'He said "no" -- it\'s over.'

    def test_aggressive_flattens_non_breaking_hyphen(self):
        # 376 of these in the corpus, in text nobody typed them into.
        out, _ = sanitize("The multi‑year permit.", aggressive=True)
        assert out == "The multi-year permit."

    def test_ellipsis_expands(self):
        out, _ = sanitize("Wait…", aggressive=True)
        assert out == "Wait..."


class TestHomoglyphs:
    def test_foreign_letter_inside_a_latin_word_is_flagged(self):
        findings = scan(f"The b{CYRILLIC_A}nk approved it.")
        assert kinds(findings) == {KIND_HOMOGLYPH}
        assert findings[0].char == CYRILLIC_A

    def test_the_note_says_which_scripts_collided(self):
        findings = scan(f"The b{CYRILLIC_A}nk approved it.")
        assert "CYRILLIC" in findings[0].note and "LATIN" in findings[0].note

    def test_a_whole_foreign_word_is_a_quotation_not_a_substitution(self):
        assert scan("He said привет and left.") == []

    def test_greek_letter_in_a_latin_word_is_flagged(self):
        assert kinds(scan("The rate is 5 perceοt.")) == {KIND_HOMOGLYPH}

    def test_aggressive_substitutes_a_known_confusable(self):
        out, _ = sanitize(f"The b{CYRILLIC_A}nk approved it.", aggressive=True)
        assert out == "The bank approved it."

    def test_default_pass_leaves_homoglyphs_alone(self):
        text = f"The b{CYRILLIC_A}nk approved it."
        assert sanitize(text)[0] == text

    def test_curly_apostrophe_does_not_split_a_contraction(self):
        # If it split, "don" and "t" would each be single-script and the
        # mixed-script check would never see the substituted letter.
        findings = scan(f"They d{CYRILLIC_A}n’t agree.")
        assert KIND_HOMOGLYPH in kinds(findings)


class TestMicroPrefixIsNotAHomoglyph:
    """SI notation defeats the mixed-script test by construction.

    Both spellings of the micro prefix read as a foreign letter next to a Latin
    unit symbol, so every correctly written microtesla or microgram was a
    "serious" finding — and this publication measures magnetic fields, so that
    failed the publish gate on drafts whose only sin was correct units.
    """

    @pytest.mark.parametrize(
        "text",
        [
            f"exposure of 200 {MICRO_SIGN}T at the fence line",
            f"exposure of 200 {GREEK_MU}T at the fence line",
            f"PM2.5 measured at 12 {MICRO_SIGN}g/m3",
            f"a switching delay of 40 {MICRO_SIGN}s",
            f"a gap of 5 {MICRO_SIGN}m across",
        ],
    )
    def test_si_units_are_not_flagged(self, text):
        assert [f for f in scan(text) if f.kind == KIND_HOMOGLYPH] == []

    def test_the_exemption_does_not_cover_a_real_word(self):
        """A mu standing in for a Latin "u" is the substitution being hunted."""
        findings = scan(f"{GREEK_MU}nicode is a lookalike")
        assert KIND_HOMOGLYPH in kinds(findings)

    def test_the_exemption_stops_at_the_longest_unit_symbol(self):
        """Bounded at three characters, so it stays notation and not prose."""
        assert [
            f for f in scan(f"12 {MICRO_SIGN}mol") if f.kind == KIND_HOMOGLYPH
        ] == []
        assert KIND_HOMOGLYPH in kinds(scan(f"{MICRO_SIGN}morphic behaviour"))

    def test_a_bare_micro_sign_is_not_flagged(self):
        assert [
            f for f in scan(f"the {MICRO_SIGN} prefix") if f.kind == KIND_HOMOGLYPH
        ] == []

    def test_other_substitutions_still_caught_alongside_units(self):
        findings = scan(f"200 {MICRO_SIGN}T at the b{CYRILLIC_A}nk")
        assert by_char(findings).keys() == {CYRILLIC_A}


class TestAnomalies:
    """Damage, not marking. Reported, never removed -- deleting a replacement
    character hides a decode bug instead of fixing it."""

    def test_replacement_character_is_an_anomaly(self):
        findings = scan("Content: %PDF-1.6 �� obj")
        assert kinds(findings) == {KIND_ANOMALY}

    def test_unassigned_code_point_is_an_anomaly(self):
        # U+03A2 is a hole in the Greek block; it cannot occur in text that
        # decoded correctly. This is what the corpus actually contained.
        assert kinds(scan("stream h\u07bcW\u03a2 endstream")) == {KIND_ANOMALY}

    def test_anomalies_survive_the_default_pass(self):
        text = "Content: �� obj"
        assert sanitize(text)[0] == text

    def test_anomalies_survive_the_aggressive_pass(self):
        text = "Content: �� obj"
        assert sanitize(text, aggressive=True)[0] == text

    def test_anomalies_are_not_removable(self):
        assert scan("a�b")[0].removable is False

    def test_note_points_upstream_rather_than_at_deletion(self):
        assert "upstream" in scan("a�b")[0].note


class TestFindingMetadata:
    def test_codepoint_is_formatted_for_lookup(self):
        assert scan(f"a{ZWSP}b")[0].codepoint == "U+200B"

    def test_unnamed_code_points_do_not_raise(self):
        # unicodedata.name() raises for unassigned characters; the scan must
        # survive exactly the input that motivated the anomaly class.
        assert scan("a\u03a2b")[0].name == "<unnamed>"

    def test_positions_locate_the_marker(self):
        assert scan(f"ab{ZWSP}cd")[0].positions == [2]

    def test_invisible_findings_are_reported_before_cosmetic_ones(self):
        findings = scan(f"a — b{ZWSP}c � d")
        assert [f.kind for f in findings][:2] == [KIND_INVISIBLE, KIND_ANOMALY]


class TestSanitizeContract:
    def test_sanitize_is_idempotent(self):
        text = f"The{ZWSP} per{NBSP}mit — as {CHECK}{VS16} written."
        once, _ = sanitize(text)
        assert sanitize(once)[0] == once

    def test_aggressive_is_idempotent(self):
        text = "He said “no” — it’s over."
        once, _ = sanitize(text, aggressive=True)
        assert sanitize(once, aggressive=True)[0] == once

    def test_findings_cover_the_whole_scan_not_just_what_was_removed(self):
        # The default pass leaves typography alone but must still report it,
        # otherwise the report understates what is in the file.
        _, findings = sanitize(f"a{ZWSP}b — c")
        assert kinds(findings) == {KIND_INVISIBLE, KIND_TYPOGRAPHY}

    def test_default_pass_changes_no_visible_character(self):
        text = f"The{ZWSP} per{NBSP}mit — “as written” — allows it.{BOM}"
        out, _ = sanitize(text)

        def visible(s):
            return [c for c in s if c not in (ZWSP, BOM, NBSP, " ")]

        assert visible(out) == visible(text)


class TestInventory:
    def test_ascii_is_not_counted(self):
        assert inventory("Plain ASCII text, 1,000 hours.") == []

    def test_every_non_ascii_code_point_is_counted(self):
        entries = {c.char: c.count for c in inventory("Benoît — Benoît — €")}
        assert entries == {"î": 2, "—": 2, "€": 1}

    def test_scan_verdicts_are_carried_through(self):
        entries = {c.char: c.marker_kind for c in inventory(f"a{ZWSP}b — c €")}
        assert entries[ZWSP] == KIND_INVISIBLE
        assert entries["—"] == KIND_TYPOGRAPHY

    def test_characters_scan_has_no_opinion_on_are_still_counted(self):
        # The whole point: the census does not depend on recognising anything,
        # so a marker built from something nobody has thought of still appears.
        entries = {c.char: c.marker_kind for c in inventory("cost €40")}
        assert entries == {"€": ""}

    def test_sorted_by_frequency(self):
        counts = [c.count for c in inventory("é é é ü ü ñ")]
        assert counts == sorted(counts, reverse=True)
