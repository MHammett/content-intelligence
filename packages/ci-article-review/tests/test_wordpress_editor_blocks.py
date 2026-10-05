"""The converter writes the blocks the WordPress block editor itself writes.

The editor accepts a block only if what is stored equals what that block's
``save()`` writes. Anything else opens as "Block contains unexpected or invalid
content". REST stores any markup and answers ``200``, so a publish cannot tell.
A quote, a table, a code block and a separator were all stored as the Markdown
element came, and all four were rejected (issue #325), from a publish that
reported success.

The expected strings here are not written by hand. ``golden/wordpress_editor/
blocks.json`` holds what a real editor (WordPress 7.1.2) wrote for each
construct, captured by the ``capture.js`` beside it, with the editor's own verdict
on every string. That directory's README says how it was captured and how to
capture it again when WordPress changes. These tests hold the converter to those
strings. They cannot say what an editor makes of a construct nobody captured,
which is why the README also says how to ask one.
"""

import json
import re
from pathlib import Path

import pytest

from ci_article_review.adapters.cms.blocks import to_blocks

GOLDEN = Path(__file__).parent / "golden" / "wordpress_editor"
CORPUS = json.loads((GOLDEN / "blocks.json").read_text(encoding="utf-8"))
CASES = CORPUS["cases"]
NAMES = [case["name"] for case in CASES]

#: A construct that was captured as raw HTML has to reach the editor as that.
HTML_BLOCK = "<!-- wp:html -->\n"


class TestTheCaptures:
    """What is checked in is what a real editor wrote, and the editor accepted it."""

    def test_the_editor_build_is_recorded(self):
        provenance = CORPUS["provenance"]
        assert re.fullmatch(r"\d+\.\d+(\.\d+)?", provenance["wordpress"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", provenance["captured_on"])
        bundles = provenance["bundles_sha256_12"]
        assert set(bundles) == {
            "blocks.min.js",
            "block-library.min.js",
            "block-editor.min.js",
        }
        assert all(re.fullmatch(r"[0-9a-f]{12}", h) for h in bundles.values())

    def test_every_case_in_the_script_was_captured_and_nothing_else_is_here(self):
        """A case added to capture.js with no capture, or one in blocks.json that
        capture.js does not make, means the corpus was edited by hand."""
        script = (GOLDEN / "capture.js").read_text(encoding="utf-8")
        assert sorted(re.findall(r"name: '([a-z0-9-]+)'", script)) == sorted(NAMES)

    def test_the_four_blocks_the_issue_names_are_all_in_it(self):
        captured = {name for case in CASES for name in case["names"]}
        assert {
            "core/quote",
            "core/table",
            "core/code",
            "core/separator",
        } <= captured

    @pytest.mark.parametrize("case", CASES, ids=NAMES)
    def test_the_editor_accepted_what_it_wrote(self, case):
        """``valid``: every block it read back was valid. ``round_trip``: writing
        the blocks it read back gave the same text again. Without both, the string
        is not something to hold the converter to."""
        assert case["valid"] is True
        assert case["round_trip"] is True
        # Text it could not place becomes a classic or a "missing" block, which is
        # valid and is not what the case set out to capture.
        assert case["names"]
        assert not {"core/freeform", "core/missing"} & set(case["names"])


class TestTheConverterWritesWhatTheEditorWrote:
    @pytest.mark.parametrize("case", CASES, ids=NAMES)
    def test_to_blocks_equals_the_capture(self, case):
        assert to_blocks(case["markdown"]) == case["expected"]


def _table(*cells, section="thead", **attrs):
    attributes = "".join(f' {key}="{value}"' for key, value in attrs.items())
    return (
        f"<table><{section}><tr>"
        + "".join(f"<th{attributes}>{cell}</th>" for cell in cells)
        + f"</tr></{section}><tbody><tr><td>x</td></tr></tbody></table>"
    )


class TestWhatHasNoNativeFormIsSentWhole:
    """Where a block cannot hold what an element carries, the element goes out as
    ``wp:html``, unchanged, and is not trimmed to fit. The editor accepts that as
    it is (the captures have one for each construct); these say which elements
    take that road."""

    @pytest.mark.parametrize(
        "markdown",
        [
            pytest.param(_table("A", colspan="2"), id="a-cell-that-spans-columns"),
            pytest.param(_table("A", rowspan="2"), id="a-cell-that-spans-rows"),
            pytest.param(_table("A", scope="col"), id="a-cell-with-a-scope"),
            pytest.param(_table("A", style="color: red;"), id="a-cell-with-a-style"),
            pytest.param(_table("A", **{"class": "x"}), id="a-cell-with-a-class"),
            pytest.param(_table("A", style="text-align: justify;"), id="justified"),
            pytest.param(
                _table("A", style="text-align: left;", align="right"), id="two-ways"
            ),
            pytest.param(
                "<table><caption>c</caption><thead><tr><th>A</th></tr></thead>"
                "<tbody><tr><td>b</td></tr></tbody></table>",
                id="a-caption",
            ),
            pytest.param(
                "<table><thead><tr><th>A</th></tr></thead>"
                "<tbody><tr><td>b</td></tr></tbody>"
                "<tfoot><tr><td>c</td></tr></tfoot></table>",
                id="a-footer",
            ),
            pytest.param(
                "<table><thead><tr class='x'><th>A</th></tr></thead>"
                "<tbody><tr><td>b</td></tr></tbody></table>",
                id="a-row-with-a-class",
            ),
            pytest.param(
                '<table id="t"><thead><tr><th>A</th></tr></thead>'
                "<tbody><tr><td>b</td></tr></tbody></table>",
                id="a-table-with-an-id",
            ),
            pytest.param("<table><thead></thead><tbody></tbody></table>", id="no-rows"),
        ],
    )
    def test_a_table(self, markdown):
        out = to_blocks(markdown)
        assert out.startswith(HTML_BLOCK + "<table")
        assert "wp:table" not in out

    @pytest.mark.parametrize(
        "markdown",
        [
            pytest.param("> # A heading\n> and text", id="a-heading"),
            pytest.param("> 1. one\n> 2. two", id="an-ordered-list"),
            pytest.param("> Intro.\n>\n>     indented code", id="a-code-block"),
            pytest.param("<blockquote>loose text</blockquote>", id="loose-text"),
            pytest.param("<blockquote></blockquote>", id="nothing"),
            pytest.param('<blockquote class="x"><p>a</p></blockquote>', id="a-class"),
            pytest.param(
                "<blockquote><p>a</p>loose text<p>b</p></blockquote>", id="text-between"
            ),
        ],
    )
    def test_a_quote(self, markdown):
        out = to_blocks(markdown)
        assert out.startswith(HTML_BLOCK + "<blockquote")
        assert "wp:quote" not in out

    @pytest.mark.parametrize(
        "markdown",
        [
            pytest.param("<pre>no code element</pre>", id="no-code-element"),
            pytest.param("<pre><code>a<b>b</b></code></pre>", id="markup-inside"),
            pytest.param('<pre><code id="x">a</code></pre>', id="an-id"),
            pytest.param('<pre><code class="x">a</code></pre>', id="a-class"),
            pytest.param('<pre id="x"><code>a</code></pre>', id="an-id-on-the-pre"),
            pytest.param("<pre><code>a</code><code>b</code></pre>", id="two-code"),
            pytest.param("<pre>text<code>a</code></pre>", id="text-before"),
        ],
    )
    def test_code(self, markdown):
        out = to_blocks(markdown)
        assert out.startswith(HTML_BLOCK + "<pre")
        assert "wp:code" not in out

    def test_a_rule_with_attributes(self):
        assert to_blocks('<hr class="x" />').startswith(HTML_BLOCK)

    def test_a_figure_written_as_html(self):
        """It was wrapped as an image block, and the editor rejected that too: it
        has none of the markup an image block's ``save()`` writes."""
        out = to_blocks(
            '<figure><img src="https://example.com/a.png" alt="A" /></figure>'
        )
        assert out.startswith(HTML_BLOCK + "<figure")
        assert "wp:image" not in out


class TestTableAlignment:
    def test_the_align_attribute_means_what_the_style_does(self):
        """Python-Markdown writes ``style="text-align: right;"``; an extension
        option, or a table written as raw HTML, writes ``align="right"``. The
        table block keeps neither, and both have to come out the same."""
        by_style = to_blocks(_table("A", style="text-align: right;"))
        by_attribute = to_blocks(_table("A", align="right"))
        assert by_style == by_attribute
        assert by_style.startswith("<!-- wp:table -->")
        assert 'class="has-text-align-right" data-align="right"' in by_style

    def test_alignment_is_read_without_regard_to_case_or_spacing(self):
        assert to_blocks(_table("A", style="TEXT-ALIGN:Center")) == to_blocks(
            _table("A", style="text-align: center;")
        )

    def test_a_column_with_no_alignment_gets_no_attributes(self):
        out = to_blocks("| A |\n|---|\n| b |")
        assert "align" not in out


class TestCode:
    def test_the_language_is_dropped_because_the_block_has_nowhere_to_keep_it(self):
        with_language = to_blocks('```python\nprint("hi")\n```')
        without = to_blocks('```\nprint("hi")\n```')
        assert with_language == without
        assert "language" not in with_language

    def test_a_shortcode_in_a_code_sample_is_not_left_to_run(self):
        """WordPress runs shortcodes over post content, ``<pre>`` included, so the
        editor writes every ``[`` in a code block as ``&#91;``. A ``[gallery]`` in
        a tutorial would otherwise be a gallery."""
        out = to_blocks("```\n[gallery ids=1]\n```")
        assert "[gallery" not in out
        assert "&#91;gallery ids=1]" in out

    def test_a_url_alone_on_a_line_is_not_left_to_be_embedded(self):
        out = to_blocks("```\nhttps://example.com/a\n```")
        assert "https:&#47;&#47;example.com/a" in out


class TestListItemText:
    def test_a_less_than_in_a_list_item_is_escaped(self):
        """The item's own text was written out raw, so ``a < b`` reached the markup
        as the start of a tag and the editor rejected the item ("Expected text
        `a < b & c > d`, saw `a `", WordPress 7.1.2, 2026-10-05). The table cells
        share the helper that did it."""
        out = to_blocks("- a < b & c > d")
        assert "<li>a &lt; b &amp; c &gt; d</li>" in out
        assert "a < b" not in out


EVERY_CONSTRUCT = (GOLDEN / "every_construct.md").read_text(encoding="utf-8")


class TestEveryConstructTogether:
    def test_the_sample_comes_out_as_the_blocks_it_means(self):
        """The sample is the document to put in front of an editor (the README says
        how), so it has to keep every construct the converter writes: the blocks in
        the order they are written, nested ones included."""
        out = to_blocks(EVERY_CONSTRUCT)
        assert re.findall(r"<!-- wp:([a-z0-9-]+)", out) == [
            "paragraph",
            "heading",
            "heading",
            "list",
            "list-item",
            "list-item",
            "list",
            "list-item",
            "list-item",
            "quote",
            "paragraph",
            "paragraph",
            "quote",
            "paragraph",
            "paragraph",
            "table",
            "code",
            "separator",
            "image",
            "html",
            "paragraph",
        ]
