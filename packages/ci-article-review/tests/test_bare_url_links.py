"""A bare URL in a draft becomes a link, so a Sources list is clickable once published.

``to_blocks`` runs Python-Markdown with ``extra``, which links ``<https://...>``
and ``[text](...)`` and nothing else, and WordPress does not link a URL in post
content (core applies ``make_clickable`` to comments only). A FINAL DRAFT whose
sources are written as bare URLs, which is how citation lists are written,
therefore published with 24 of them and no ``<a>`` among them, and the publish
reported success (issue #316).

These read the block markup ``to_blocks`` returns, which is what WordPress is
sent. The rules are GitHub Flavored Markdown's for extended autolinks, cut to
``http(s)``: what a sentence sets after a URL stays outside the link, a bracket
the URL did not open stays outside it, and a link, code or an attribute is never
linked again.
"""

import html as html_lib
import logging
import random
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import markdown
import pytest

from ci_article_review import handoff_parser
from ci_article_review.adapters.cms import blocks
from ci_article_review.adapters.cms import wordpress as wp
from ci_article_review.adapters.cms.blocks import to_blocks

URL = "https://example.com/page"
WIKI = "https://en.wikipedia.org/wiki/Foo_(bar)"

#: The anchors this pass writes put ``href`` first and carry nothing else.
ANCHOR = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)

EXAMPLES = Path(handoff_parser.__file__).parent / "handoff_templates" / "examples"


def paragraph(inner):
    return f"<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->"


def link(href, text=None):
    """The anchor written for a bare URL: its text is the URL as the author wrote it."""
    return f'<a href="{href}">{text if text is not None else href}</a>'


def hrefs(md):
    """The target of every link ``to_blocks`` writes for ``md``, in order."""
    return [h for h, _ in ANCHOR.findall(to_blocks(md, strip_leading_h1=False))]


def render(md):
    """What Python-Markdown makes of ``md``, before any link is added."""
    return markdown.markdown(md, extensions=list(blocks._MD_EXTENSIONS))


class TestWhatTheIssueNames:
    """The four cases #316 asks to be pinned, as the whole block that is sent."""

    def test_a_url_before_a_full_stop_leaves_the_stop_outside(self):
        assert to_blocks(f"See {URL}.") == paragraph(f"See {link(URL)}.")

    def test_a_url_in_parentheses_leaves_the_bracket_outside(self):
        assert to_blocks(f"(see {URL})") == paragraph(f"(see {link(URL)})")

    def test_a_url_in_a_list_item_is_linked(self):
        out = to_blocks(f"- {URL}/a\n- {URL}/b")
        assert out.count("<!-- wp:list-item -->") == 2
        assert f"<li>{link(URL + '/a')}</li>" in out
        assert f"<li>{link(URL + '/b')}</li>" in out

    def test_a_url_in_a_table_cell_is_linked(self):
        out = to_blocks(f"| Source | URL |\n|---|---|\n| One | {URL} |")
        assert f"<td>{link(URL)}</td>" in out
        assert "<td>One</td>" in out


class TestWhereTheLinkEnds:
    @pytest.mark.parametrize(
        "tail",
        [
            ".",
            ",",
            ";",
            ":",
            "!",
            "?",
            "?!",
            "...",
            ")",
            "]",
            ").",
            ".)",
            ")).",
            "'",
            "*",
            "_",
            ">",
            "\N{RIGHT SINGLE QUOTATION MARK}",
            "\N{RIGHT DOUBLE QUOTATION MARK}",
            "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}",
            "\N{HORIZONTAL ELLIPSIS}",
        ],
    )
    def test_what_a_sentence_puts_after_a_url_stays_outside(self, tail):
        """The issue's list (. , ; :), GFM's (? ! * _), a bracket nothing opened,
        and the quotes and ellipsis that prose uses."""
        expected = paragraph(f"See {link(URL)}{html_lib.escape(tail, quote=False)}")
        assert to_blocks(f"See {URL}{tail}") == expected

    def test_a_url_in_quotes_is_linked_without_them(self):
        assert to_blocks(f'He wrote "{URL}" there') == paragraph(
            f'He wrote "{link(URL)}" there'
        )

    def test_a_bracket_the_url_opened_stays_inside_the_link(self):
        """A Wikipedia title with a bracket in it: the first thing a naive
        pattern breaks, by ending the link before the ``)``."""
        assert to_blocks(WIKI) == paragraph(link(WIKI))

    def test_a_bracketed_url_keeps_its_own_bracket_and_not_the_outer_one(self):
        assert to_blocks(f"({WIKI})") == paragraph(f"({link(WIKI)})")

    def test_a_bracketed_url_before_a_full_stop(self):
        assert to_blocks(f"See {WIKI}.") == paragraph(f"See {link(WIKI)}.")

    def test_square_brackets_balance_the_same_way(self):
        url = "https://example.com/a[0]"
        assert to_blocks(f"[see {url}]") == paragraph(f"[see {link(url)}]")

    def test_an_entity_after_a_url_is_not_part_of_it(self):
        """``&amp;`` is how HTML writes a character. Its ``;`` is not punctuation
        to strip on its own, which would leave ``&amp`` in the link: the whole
        entity stays outside."""
        assert to_blocks(f"{URL}&amp;") == paragraph(f"{link(URL)}&amp;")

    def test_an_ampersand_in_a_query_is_escaped_in_the_link_and_the_text(self):
        escaped = "https://example.com/?a=1&amp;b=2&amp;c=3"
        out = to_blocks("https://example.com/?a=1&b=2&c=3")
        assert out == paragraph(link(escaped))

    def test_a_url_ends_at_a_line_break(self):
        assert hrefs(f"{URL}/1\n{URL}/2") == [f"{URL}/1", f"{URL}/2"]

    def test_a_url_ends_at_a_hard_break(self):
        out = to_blocks(f"{URL}/1  \n{URL}/2")
        assert out == paragraph(f"{link(URL + '/1')}<br>\n{link(URL + '/2')}")

    def test_two_urls_in_one_sentence(self):
        md = f"Compare {URL}/a and {URL}/b."
        assert to_blocks(md) == paragraph(
            f"Compare {link(URL + '/a')} and {link(URL + '/b')}."
        )


class TestWhatIsLeftAlone:
    """What the author linked, coded or put in an attribute is never linked again."""

    def test_a_markdown_link_keeps_its_label_and_target(self):
        out = to_blocks("[label](https://example.com/x)")
        assert out == paragraph(link("https://example.com/x", "label"))

    def test_a_link_labelled_with_its_own_url_is_not_nested(self):
        md = "[https://example.com/x](https://example.com/x)"
        assert to_blocks(md) == paragraph(link("https://example.com/x"))

    def test_an_angle_bracket_link_is_one_link(self):
        assert to_blocks("<https://example.com/x>") == paragraph(
            link("https://example.com/x")
        )

    def test_a_reference_link_is_one_link_and_its_definition_is_not_text(self):
        md = "[https://example.com/ref][1]\n\n[1]: https://example.com/ref"
        assert to_blocks(md) == paragraph(link("https://example.com/ref"))

    def test_a_link_title_is_not_linked(self):
        out = to_blocks('[a](https://example.com/x "https://example.com/t")')
        assert out.count("<a ") == 1
        assert 'title="https://example.com/t"' in out

    def test_an_image_source_and_alt_are_not_linked(self):
        out = to_blocks("![see https://example.com/alt](https://example.com/i.png)")
        assert "<a " not in out
        assert "https://example.com/i.png" in out

    @pytest.mark.parametrize(
        "md",
        [
            "Use `https://example.com/code` here",
            "```\nhttps://example.com/fenced\n```",
            "para\n\n    https://example.com/indented\n",
            "<code>https://example.com/raw</code>",
            "<kbd>https://example.com/raw</kbd>",
            "<pre>https://example.com/raw</pre>",
            '<span title="https://example.com/attr">here</span>',
            '<span title="a > https://example.com/attr">here</span>',
            "<span title='https://example.com/attr'>here</span>",
            "<span title='a > https://example.com/attr'>here</span>",
        ],
    )
    def test_code_and_attributes_are_not_linked(self, md):
        assert "<a " not in to_blocks(md)

    def test_a_tag_with_an_unclosed_quote_is_still_a_tag(self):
        """Nothing that looks like a tag is linked inside, however broken: a URL
        in the attribute would otherwise get an anchor written into the tag."""
        html = f'<p class="{URL}>text</p>'
        assert blocks._link_bare_urls(html) == html

    def test_a_link_written_in_html_is_one_link_and_not_linked_again(self):
        md = 'Read <a href="https://example.com/raw">https://example.com/raw</a> now'
        out = to_blocks(md)
        assert out == paragraph(
            'Read <a href="https://example.com/raw">https://example.com/raw</a> now'
        )

    def test_html_tags_are_matched_whatever_their_case(self):
        md = 'Read <A HREF="https://example.com/raw">https://example.com/raw</A> now'
        assert to_blocks(md).lower().count("<a ") == 1

    def test_text_after_a_tag_that_is_never_closed_is_not_linked(self):
        """Linking it would put one link inside another, so nothing after is linked."""
        out = to_blocks(f'<a href="{URL}">text and {URL}/after')
        assert out.lower().count("<a ") == 1

    def test_text_after_a_closed_element_is_linked_again(self):
        assert hrefs(f"<code>{URL}/a</code> then {URL}/b") == [f"{URL}/b"]

    def test_a_neighbouring_url_is_linked_beside_a_link_the_author_wrote(self):
        md = f"[a]({URL}/a) and {URL}/b"
        assert to_blocks(md) == paragraph(
            f"{link(URL + '/a', 'a')} and {link(URL + '/b')}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Visit www.example.com for more",
            "Get ftp://example.com/file now",
            "Mail someone@example.com please",
            "Use mailto:someone@example.com here",
            "javascript:alert(1) is not a URL",
            "A bare scheme, http://, is not one either",
            "Nor is https://. or http://,",
        ],
    )
    def test_only_http_and_https_urls_are_linked(self, text):
        """The issue asks for ``http(s)``. ``www.``, ``ftp://`` and an email
        address are other decisions, which ``pymdownx.magiclink`` makes for you
        and cannot be told not to."""
        assert to_blocks(text) == paragraph(text)

    def test_the_case_of_the_scheme_is_kept(self):
        assert to_blocks("HTTPS://EXAMPLE.COM/X") == paragraph(
            link("HTTPS://EXAMPLE.COM/X")
        )

    def test_block_markup_the_author_wrote_by_hand_is_not_touched(self):
        existing = f"<!-- wp:paragraph -->\n<p>{URL}</p>\n<!-- /wp:paragraph -->"
        assert to_blocks(existing) == existing


#: Constructs that hold a URL where linking would be wrong, with no bare URL in
#: running text anywhere, so the pass has nothing to do and must change nothing.
PROTECTED = [
    "[label](https://example.com/x)",
    "[https://example.com/x](https://example.com/x)",
    "<https://example.com/x>",
    "[a][1]\n\n[1]: https://example.com/ref",
    '[a](https://example.com/x "https://example.com/t")',
    "![alt](https://example.com/i.png)",
    "![a](https://example.com/i.png)\n\n[b](https://example.com/b)",
    "`https://example.com/code`",
    "```\nhttps://example.com/fenced\n```",
    "    https://example.com/indented",
    '<a href="https://example.com/raw">raw</a>',
    '<div title="https://example.com/attr">x</div>',
    "<!-- https://example.com/comment -->",
    "<!-- a note\nhttps://example.com/over-two-lines\n-->",
    "<!-- a -> b: https://example.com/after-an-arrow -->",
    "<!-- a ->\nhttps://example.com/over-two-lines-after-an-arrow\n-->",
    "text[^1]\n\n[^1]: a note with [a link](https://example.com/n)",
    "| a |\n|---|\n| [b](https://example.com/c) |",
]

#: The pieces real drafts are made of, for a shuffle: tags, quotes, entities,
#: brackets, punctuation and URLs. ``<a`` is left out, so every anchor in the
#: output is one this pass wrote.
PIECES = [
    URL,
    WIKI,
    "http://x.org/b_c",
    "HTTPS://Y.NET/Z?q=1&amp;r=2",
    "https://",
    "http://.",
    *". , ; : ! ? ( ) [ ] ' \" * _ & < > x text".split(),
    *"&amp; &gt; &#160; <p> </p> <em> </em> <strong> <code> </code> <pre> </pre>".split(),
    "<br />",
    "<br>",
    "<!-- c -->",
    '<img src="https://example.com/i.png" alt="x"/>',
    "<span title='https://example.com/t'>",
    "</span>",
    " ",
    "\n",
]


class TestLinkingOnlyAddsLinks:
    @pytest.mark.parametrize("md", PROTECTED)
    def test_a_draft_with_nothing_to_link_comes_out_exactly_as_it_did(self, md):
        rendered = render(md)
        assert blocks._link_bare_urls(rendered) == rendered

    @pytest.mark.parametrize(
        "tag", ["code", "pre", "kbd", "samp", "script", "style", "textarea"]
    )
    def test_text_inside_each_of_these_elements_is_not_linked(self, tag):
        html = f"<{tag}>{URL}</{tag}> and {URL}/after"
        assert blocks._link_bare_urls(html) == (
            f"<{tag}>{URL}</{tag}> and {link(URL + '/after')}"
        )

    @pytest.mark.parametrize("tag", ["abbr", "aside", "address", "article", "span"])
    def test_other_elements_are_ordinary_text(self, tag):
        """Only the elements named above are skipped. The ones that merely start
        with the same letters, ``<abbr>`` and ``<aside>`` for ``<a>``, are not."""
        html = f"<{tag}>{URL}</{tag}>"
        assert blocks._link_bare_urls(html) == f"<{tag}>{link(URL)}</{tag}>"

    def test_an_element_inside_another_leaves_the_outer_one_open(self):
        """``</code>`` must not end the ``<pre>`` it sits in."""
        html = f"<pre><code>x</code> {URL}</pre> {URL}/after"
        assert blocks._link_bare_urls(html) == (
            f"<pre><code>x</code> {URL}</pre> {link(URL + '/after')}"
        )

    def test_a_closing_tag_nothing_opened_does_not_stop_linking(self):
        """Raw HTML can be unbalanced; the count must not go below nothing open,
        or the next URL would find it already negative and skip."""
        assert blocks._link_bare_urls(f"</code> {URL}") == f"</code> {link(URL)}"

    def test_a_scheme_inside_a_word_is_not_a_url(self):
        html = f"<p>xhttps://example.com ghttp://example.com/a and {URL}</p>"
        assert blocks._link_bare_urls(html) == (
            f"<p>xhttps://example.com ghttp://example.com/a and {link(URL)}</p>"
        )

    def test_a_url_after_a_colon_or_a_bracket_is_a_url(self):
        assert blocks._link_bare_urls(f"<p>see:{URL}</p>") == f"<p>see:{link(URL)}</p>"

    def test_linking_twice_is_linking_once(self):
        html = f"<p>See {URL}. And ({WIKI}). <a href='{URL}'>x</a></p>"
        once = blocks._link_bare_urls(html)
        assert blocks._link_bare_urls(once) == once

    def test_taking_the_new_links_out_gives_back_the_input(self):
        """Seeded, from any order of the pieces above.

        Whatever the input is, the pass may add anchors and must change nothing
        else, never open one anchor inside another, and never raise: it runs after
        the images are already uploaded, so a failure there is the worst place for
        one.
        """
        rng = random.Random(316)
        for _ in range(1500):
            html = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 14)))
            out = blocks._link_bare_urls(html)
            undone = re.sub(r'<a href="[^"]*">(.*?)</a>', r"\1", out, flags=re.DOTALL)
            assert undone == html, f"{html!r} became {out!r}"
            depth = 0
            for tag in re.finditer(r"<(/?)a[\s>]", out):
                depth += -1 if tag.group(1) else 1
                assert depth <= 1, f"{html!r} became {out!r}"

    def test_unclosed_tags_cannot_make_the_pass_quadratic(self):
        """20,000 unclosed ``<a>`` took 24 seconds when the skip was a lazy
        ``.*?</a>``, which scanned to the end of the document from each of them.
        It is a counter now and takes about 0.03 seconds. The bound is 150 times
        that, so this fails on the old shape and not on a slow machine."""
        html = '<a href="x">' * 20_000 + URL
        start = time.perf_counter()
        assert blocks._link_bare_urls(html) == html
        assert time.perf_counter() - start < 5

    @pytest.mark.parametrize(
        ("run", "times"), [(")", 200_000), (";", 200_000), ("&amp;", 20_000)]
    )
    def test_a_long_run_after_a_url_cannot_either(self, run, times):
        """Each character of the run is looked at once. Counting the brackets
        again for every one, or searching back to the start of the URL for the
        ``&`` a ``;`` ends, takes 10 to 25 seconds at these lengths, against 0.1
        for the single pass."""
        html = f"<p>{URL}" + run * times + "</p>"
        start = time.perf_counter()
        assert blocks._link_bare_urls(html) == f"<p>{link(URL)}" + run * times + "</p>"
        assert time.perf_counter() - start < 5


class TestWhenMarkdownCutsTheUrl:
    """``/_foo_/`` and ``__init__.py`` are emphasis to Markdown, which has split the
    URL before the link pass sees it. Linking the first piece is a link to the
    wrong address, so it is left as text, and the author is told how to fix it."""

    CUT = [
        "https://example.com/_foo_/bar",
        "https://github.com/org/repo/blob/main/pkg/__init__.py",
        "https://example.com/a*b*c",
    ]

    @pytest.mark.parametrize("url", CUT + ["https://example.com/a`b`"])
    def test_a_url_markdown_has_cut_is_not_linked(self, url):
        assert "<a " not in to_blocks(f"Source: {url}")

    @pytest.mark.parametrize("url", CUT)
    def test_and_the_warning_names_the_fix(self, url, caplog):
        with caplog.at_level(logging.WARNING, logger=blocks.log.name):
            to_blocks(url)
        (record,) = caplog.records
        assert "<https://...>" in record.getMessage()
        assert url.split("/")[2] in record.getMessage()

    @pytest.mark.parametrize("url", CUT)
    def test_angle_brackets_link_all_of_it(self, url, caplog):
        with caplog.at_level(logging.WARNING, logger=blocks.log.name):
            out = to_blocks(f"<{url}>")
        assert out == paragraph(link(url))
        assert not caplog.records

    def test_the_part_before_a_cut_is_never_linked_on_its_own(self):
        md = "https://example.com/_foo_/bar and https://example.com/ok"
        assert hrefs(md) == ["https://example.com/ok"]

    def test_underscores_inside_words_are_not_a_cut(self):
        url = "https://example.com/a_b_c/d_e"
        assert to_blocks(url) == paragraph(link(url))

    def test_a_url_between_emphasis_marks_is_one_link_inside_them(self):
        assert to_blocks(f"**{URL}**") == paragraph(f"<strong>{link(URL)}</strong>")
        assert to_blocks(f"*{URL}*") == paragraph(f"<em>{link(URL)}</em>")

    def test_a_full_stop_between_a_url_and_emphasis_separates_them(self):
        out = to_blocks(f"{URL}.*Next*")
        assert out == paragraph(f"{link(URL)}.<em>Next</em>")

    @pytest.mark.parametrize("after", ["  \nnext", "[^1]"])
    def test_a_hard_break_or_a_footnote_marker_is_not_a_cut(self, after):
        out = to_blocks(f"{URL}{after}\n\n[^1]: A note.")
        assert (URL, URL) in ANCHOR.findall(out)

    def test_find_images_does_not_repeat_the_warning(self, caplog):
        """``find_images`` asks about images and ``to_blocks`` converts, and both
        render the draft. Only the conversion links, so only it says it did not."""
        md = "https://example.com/_foo_/bar"
        with caplog.at_level(logging.WARNING, logger=blocks.log.name):
            blocks.find_images(md)
        assert not caplog.records
        with caplog.at_level(logging.WARNING, logger=blocks.log.name):
            to_blocks(md)
        assert len(caplog.records) == 1


class TestTheOtherPlaces:
    def test_a_heading_is_linked(self):
        assert to_blocks(f"## See {URL}", strip_leading_h1=False) == (
            f"<!-- wp:heading -->\n<h2>See {link(URL)}</h2>\n<!-- /wp:heading -->"
        )

    def test_a_quote_is_linked(self):
        assert f"<p>{link(URL)}</p>" in to_blocks(f"> {URL}")

    def test_a_footnote_is_linked_with_its_full_stop_outside(self):
        out = to_blocks(f"Text[^1]\n\n[^1]: See {URL}.")
        assert f"See {link(URL)}." in out

    def test_a_document_that_cannot_be_parsed_still_gets_its_links(self, caplog):
        """A raw ``<br>`` is not XML, so the whole draft goes out as one
        ``wp:html`` block. Linking happens before that parse, or the same Sources
        list would publish unlinked from exactly the drafts that already carry a
        warning."""
        md = f"Intro<br>line\n\n- {URL}/a.\n- ({URL}/b)\n"
        with caplog.at_level(logging.WARNING, logger=blocks.log.name):
            out = to_blocks(md)
        assert out.startswith("<!-- wp:html -->")
        assert out.count("<!-- wp:") == 1
        assert [h for h, _ in ANCHOR.findall(out)] == [f"{URL}/a", f"{URL}/b"]
        assert "could not be parsed" in caplog.text

    def test_an_image_block_and_a_bare_url_share_a_draft(self):
        md = f"![Grid](https://example.com/grid.png)\n\nSee {URL}.\n"
        out = to_blocks(md)
        assert out.count("<!-- wp:image") == 1
        assert f"See {link(URL)}." in out

    def test_find_images_sees_the_same_images_with_and_without_urls(self):
        base = "![One](https://e.com/1.png)\n\nWords here.\n\nLook ![two](2.png) here."
        linked = base.replace("Words here.", f"Words {URL}.")

        def seen(md):
            return [(r.src, r.placement) for r in blocks.find_images(md)]

        assert seen(base) == seen(linked)
        assert len(seen(base)) == 2


class TestTheProjectsOwnDrafts:
    """The drafts shipped as examples write their sources as bare URLs, one to a
    line of a list: the shape that published with no links."""

    DRAFTS = sorted(EXAMPLES.glob("*.md"))

    def test_the_examples_were_found(self):
        """Guards the parametrized test below against running on nothing."""
        urls = [
            u
            for p in self.DRAFTS
            for u in re.findall(r"https?://\S+", p.read_text(encoding="utf-8"))
        ]
        assert len(urls) > 10

    @pytest.mark.parametrize("path", DRAFTS, ids=lambda p: p.name)
    def test_every_url_in_an_example_draft_is_linked_once(self, path):
        text = path.read_text(encoding="utf-8")
        wanted = [u.rstrip(".,;:") for u in re.findall(r"https?://\S+", text)]
        found = [h for h, _ in ANCHOR.findall(to_blocks(text))]
        assert sorted(found) == sorted(wanted)


WP_CONFIG = {
    "site_url": "https://example.com",
    "username": "editor",
    "application_password": "pass word here",
    "rest_api_endpoint": "/wp-json/wp/v2",
}


def _sent_content(md):
    """What ``wp.push`` puts in the body of the POST that creates the post."""
    with (
        patch("ci_article_review.adapters.cms.wordpress.requests.post") as mock_post,
        patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get,
    ):
        created = MagicMock(raise_for_status=MagicMock())
        created.json.return_value = {"id": 7, "link": "https://example.com/p/"}
        mock_post.side_effect = [created, MagicMock()]
        mock_get.return_value = MagicMock(
            ok=True, json=MagicMock(return_value=[{"id": 3, "slug": "s"}])
        )
        result = wp.push(
            md,
            {"title": "Sources", "post_type": "page", "tags": []},
            WP_CONFIG,
            {"auto_set_og_tags": True},
        )
    assert result["success"] is True
    return mock_post.call_args_list[0][1]["json"]["content"]


class TestPublishing:
    def test_a_sources_list_reaches_wordpress_with_its_links(self):
        """The failure as reported: a publish that succeeded and sent no links."""
        urls = [f"https://example.com/source-{n}/" for n in range(1, 25)]
        sources = "\n".join(f"{n}. Org {n}, Title. {u}" for n, u in enumerate(urls, 1))
        sent = _sent_content(f"# Sources\n\nBody.\n\n## Sources\n\n{sources}\n")
        assert [h for h, _ in ANCHOR.findall(sent)] == urls
