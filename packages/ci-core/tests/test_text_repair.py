"""Tests for ci_core.text_repair -- undoing low-byte narrowed punctuation.

The bug these guard against: Perplexity returned prose in which General
Punctuation (U+2000-U+201F) had been narrowed to its low byte, landing in the
C0 control range. It reached the fact-check ``claim``/``note``/``source``
fields and from there into ``run_N_*_review.md``, the file the documented
workflow tells the author to paste into a chat model.

The two anchor cases below are taken verbatim from
``pipeline_history/.../run_1_20260904_020535_results.json`` and
``run_20_20260818_124145_report.json``; the same runs recorded the undamaged
spelling of the same names elsewhere, which is what pins the mapping down.
"""

import logging

import pytest

from ci_core import text_repair


class TestAnchorCases:
    """Real damaged strings, and the character each one has to come back as."""

    def test_narrowed_right_single_quote_becomes_apostrophe(self):
        assert (
            text_repair.repair_narrowed_punctuation("Lawrence Berkeley Laboratory\x19s")
            == "Lawrence Berkeley Laboratory’s"
        )

    def test_narrowed_em_dash_becomes_em_dash(self):
        assert (
            text_repair.repair_narrowed_punctuation("counterexample\x14a published")
            == "counterexample—a published"
        )

    def test_possessives_from_a_real_run(self):
        for damaged, fixed in [
            ("I\x19ve been building", "I’ve been building"),
            ("there\x19s no way", "there’s no way"),
            ("It\x19s not finished", "It’s not finished"),
            ("Virginia\x19s Joint Legislative", "Virginia’s Joint Legislative"),
        ]:
            assert text_repair.repair_narrowed_punctuation(damaged) == fixed


class TestMapping:
    def test_quotes_and_dashes_restored(self):
        assert (
            text_repair.repair_narrowed_punctuation("\x1cquoted\x1d and \x13dash")
            == "“quoted” and –dash"
        )

    def test_space_variants_collapse_to_one_ascii_space(self):
        """U+2000-U+2008 are all space variants; the word break is what matters."""
        assert text_repair.repair_narrowed_punctuation("a\x07b\x00c") == "a b c"

    def test_zero_width_and_bidi_marks_are_dropped(self):
        """U+200B/C/E/F are invisible, so removing them deletes no content."""
        assert text_repair.repair_narrowed_punctuation("a\x0b\x0c\x0e\x0fb") == "ab"

    def test_real_whitespace_is_untouched(self):
        """Tab, newline and carriage return are not damage."""
        text = "keep\tthis\nand\r\nthis"
        assert text_repair.repair_narrowed_punctuation(text) == text

    def test_clean_text_is_returned_unchanged(self):
        """This runs on every provider response; the common case must be inert."""
        text = "A perfectly ordinary sentence — with real punctuation."
        assert text_repair.repair_narrowed_punctuation(text) is text

    def test_empty_and_none_are_safe(self):
        assert text_repair.repair_narrowed_punctuation("") == ""
        assert text_repair.repair_narrowed_punctuation(None) is None


class TestRepairTree:
    def test_nested_strings_are_repaired(self):
        payload = {
            "content": "Laboratory\x19s report",
            "search_results": [{"snippet": "the site\x19s owner"}],
            "citations": ["https://example.org/a"],
        }
        out = text_repair.repair_tree(payload)
        assert out["content"] == "Laboratory’s report"
        assert out["search_results"][0]["snippet"] == "the site’s owner"
        assert out["citations"] == ["https://example.org/a"]

    def test_non_string_values_pass_through(self):
        """usage objects, numbers and None must survive the walk untouched."""
        sentinel = object()
        out = text_repair.repair_tree({"usage": sentinel, "n": 3, "x": None})
        assert out["usage"] is sentinel
        assert out["n"] == 3 and out["x"] is None

    def test_tuples_keep_their_type(self):
        assert text_repair.repair_tree(("a\x19b",)) == ("a’b",)


#: A backslash built rather than written, so that neither this source file nor
#: any tool that reads it can collapse the escape sequence being tested.
_BS = chr(92)


def _escaped_json(body):
    """JSON *text* carrying a narrowed-punctuation escape, as Perplexity sends it."""
    return body.replace("<ESC>", _BS + "u0019")


class TestRepairHappensAfterTheJsonParse:
    """Where the repair sits matters, and the placement is not the obvious one.

    Perplexity sends the damage as an *escape* inside its JSON text, not as a
    literal control byte. So the response string holds no control character at
    all -- one only exists once ``json`` has decoded the escape. Repairing the
    response text instead of the parsed object silently does nothing, which is
    the bug this class exists to keep fixed.

    A literal control byte is not the case to worry about: ``json.loads``
    rejects one outright, so such a response never parses in the first place.
    """

    def test_the_response_text_itself_holds_no_control_character(self):
        text = _escaped_json('{"claim": "Laboratory<ESC>s report"}')
        assert not any(ord(c) < 0x20 for c in text)
        assert text_repair.repair_narrowed_punctuation(text) == text

    def test_parsing_through_the_shared_helper_repairs_it(self):
        from ci_core.llm.json_utils import extract_json

        parsed = extract_json(_escaped_json('{"claim": "Laboratory<ESC>s", "n": 1}'))
        assert parsed["claim"] == "Laboratory’s"
        assert parsed["n"] == 1

    def test_salvaged_truncated_responses_are_repaired_too(self):
        from ci_core.llm.json_utils import extract_json_with_salvage

        cut = _escaped_json(
            '{"items": [{"claim": "the site<ESC>s owner"}, {"claim": "cut'
        )
        data, truncated = extract_json_with_salvage(cut)
        assert truncated is True
        assert data["items"][0]["claim"] == "the site’s owner"

    def test_a_literal_control_byte_is_rejected_by_json_not_repaired(self):
        import json

        with pytest.raises(json.JSONDecodeError):
            json.loads('{"claim": "Laboratory' + chr(0x19) + 's"}')


class TestTheRepairAnnouncesItself:
    """Repairing silently would trade a visible defect for an invisible one.

    The provider is still corrupting text. If that stops, worsens, or moves to
    a code point the mapping cannot recover, the log line is the only way
    anyone finds out -- so these pin the signal, not just the repair.
    """

    DAMAGED = "Laboratory" + chr(0x19) + "s report" + chr(0x14) + "as filed"

    def test_a_repair_is_logged_at_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ci_core.text_repair"):
            text_repair.repair_tree({"claim": self.DAMAGED})
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "WARNING"

    def test_the_log_names_the_count_and_the_code_points(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ci_core.text_repair"):
            text_repair.repair_tree({"claim": self.DAMAGED})
        msg = caplog.text
        assert "Repaired 2 narrowed punctuation character(s)" in msg
        assert "U+0019 -> U+2019 x1" in msg
        assert "U+0014 -> U+2014 x1" in msg

    def test_clean_data_logs_nothing(self, caplog):
        """This runs on every response; a clean one must stay silent."""
        with caplog.at_level(logging.WARNING, logger="ci_core.text_repair"):
            text_repair.repair_tree({"claim": "ordinary text — real punctuation"})
        assert caplog.records == []

    def test_one_line_per_response_not_per_string(self, caplog):
        """A payload's worth of damage is one provider defect, not twenty."""
        payload = {
            "items": [{"claim": self.DAMAGED} for _ in range(10)],
            "snippets": [self.DAMAGED, self.DAMAGED],
        }
        with caplog.at_level(logging.WARNING, logger="ci_core.text_repair"):
            text_repair.repair_tree(payload)
        assert len(caplog.records) == 1
        assert "Repaired 24 narrowed" in caplog.text

    def test_dropped_and_space_outcomes_are_named_not_shown_as_code_points(self):
        """U+200B/E/F are invisible and U+2000-8 are spaces; say so plainly."""
        import collections

        tally = collections.Counter({chr(0x0F): 3, chr(0x07): 2, chr(0x19): 1})
        described = text_repair._describe(tally)
        assert "U+000F -> dropped x3" in described
        assert "U+0007 -> space x2" in described
        assert "U+0019 -> U+2019 x1" in described
