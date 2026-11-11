"""A list inside a quote, and a list straight after a paragraph, stay lists.

Two posts went out with their TL;DR flattened: the handoff had ``> **TL;DR**``
with ``> - point`` lines right under it, Python-Markdown read the lot as one
paragraph (a list there needs a blank line first), and the post got a single
paragraph of "- point" text inside its quote. The editor-side shape these tests
hold the converter to, a quote of a paragraph and a list, was checked valid in
the WordPress 7.1 editor with ``capture.js``'s ``check()``.
"""

import re

import pytest

from ci_article_review.adapters.cms.blocks import (
    _let_lists_interrupt_paragraphs,
    to_blocks,
)

TLDR = "> **TL;DR**\n> - First point.\n> - Second point.\n> - Third point."


def _items(out):
    return re.findall(r"<li>(.*?)</li>", out)


class TestATldrQuote:
    @pytest.mark.parametrize(
        "markdown",
        [
            pytest.param(TLDR, id="no-blank-line"),
            pytest.param(
                TLDR.replace("\n> - First", "\n>\n> - First"), id="blank-line"
            ),
        ],
    )
    def test_is_a_quote_of_a_paragraph_and_a_list(self, markdown):
        out = to_blocks(markdown)
        assert out.startswith('<!-- wp:quote -->\n<blockquote class="wp-block-quote">')
        assert "wp:html" not in out
        assert (
            "<!-- wp:paragraph -->\n<p><strong>TL;DR</strong></p>\n<!-- /wp:paragraph -->"
            in out
        )
        assert out.count("<!-- wp:list -->") == 1
        assert out.count("<!-- wp:list-item -->") == 3
        assert _items(out) == ["First point.", "Second point.", "Third point."]
        assert "- First" not in out

    def test_both_spellings_give_the_same_blocks(self):
        assert to_blocks(TLDR) == to_blocks(
            TLDR.replace("\n> - First", "\n>\n> - First")
        )

    def test_an_ordered_list_in_a_quote_is_a_native_ordered_list(self):
        out = to_blocks("> Steps:\n> 1. one\n> 2. two")
        assert "wp:html" not in out
        assert '<!-- wp:list {"ordered":true} -->' in out
        assert _items(out) == ["one", "two"]

    def test_inline_formatting_in_an_item_survives(self):
        out = to_blocks(
            "> **TL;DR**\n> - **9,683 reports** as of [October](https://example.com/x)."
        )
        assert _items(out) == [
            '<strong>9,683 reports</strong> as of <a href="https://example.com/x">October</a>.'
        ]


class TestAListStraightAfterAParagraph:
    def test_at_top_level_is_a_list(self):
        out = to_blocks("Three things:\n- one\n- two\n- three")
        assert out.count("<!-- wp:paragraph -->") == 1
        assert _items(out) == ["one", "two", "three"]

    def test_a_wrapped_line_starting_with_a_number_stays_prose(self):
        """CommonMark lets only an ordered list that starts at 1 interrupt a paragraph."""
        out = to_blocks("The vote was held on November\n2. That was a Tuesday.")
        assert "wp:list" not in out

    def test_a_rule_after_text_is_not_an_item(self):
        assert _let_lists_interrupt_paragraphs("text\n* * *") == "text\n* * *"

    def test_fenced_code_is_left_alone(self):
        text = "Intro.\n\n```\nsome code\n- not an item\n```\n"
        assert _let_lists_interrupt_paragraphs(text) == text

    def test_continuation_lines_do_not_split_a_list(self):
        text = "Intro.\n- one\n  more of one\n- two"
        assert (
            _let_lists_interrupt_paragraphs(text)
            == "Intro.\n\n- one\n  more of one\n- two"
        )

    def test_the_blank_line_inserted_stays_inside_the_quote(self):
        assert _let_lists_interrupt_paragraphs(TLDR).split("\n")[:3] == [
            "> **TL;DR**",
            ">",
            "> - First point.",
        ]

    def test_text_already_separated_is_unchanged(self):
        text = "Intro.\n\n- one\n- two\n\n> Quote.\n>\n> - a"
        assert _let_lists_interrupt_paragraphs(text) == text
