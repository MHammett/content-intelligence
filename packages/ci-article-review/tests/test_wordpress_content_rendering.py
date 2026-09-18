"""Markdown becomes block markup, and Rank Math fields actually get set.

Both of these shipped broken and reported success. The adapter sent the
handoff's Markdown verbatim, so a published page's headings were literal ``##``
characters; and it set Rank Math's fields through ``payload["meta"]``, which
core WordPress drops because Rank Math never registers those keys with the
REST API — a live page came back with ``meta == ["footnotes"]`` and no SEO
fields, from a publish that returned 200.

So these assert the two things a green publish did not: what the block markup
contains, and that the Rank Math values leave the process at all.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ci_article_review import handoff_parser
from ci_article_review.adapters.cms import wordpress as wp
from ci_article_review.adapters.cms.blocks import looks_like_block_markup, to_blocks
from ci_article_review.handoff_parser import parse_publication_handoff

WP_CONFIG = {
    "site_url": "https://example.com",
    "username": "editor",
    "application_password": "pass word here",
    "rest_api_endpoint": "/wp-json/wp/v2",
}
RANK_MATH = {"auto_set_og_tags": True, "default_schema_type": "Article"}

SAMPLE_MD = """# About

An opening paragraph.

## Standards

Before anything publishes, I run four passes:

- The strongest version.
- The weakest link.

A closing line with a [link](https://example.com/x).
"""


class TestToBlocks:
    def test_headings_become_heading_blocks(self):
        out = to_blocks(SAMPLE_MD)
        assert "<!-- wp:heading -->" in out
        assert "<h2>Standards</h2>" in out

    def test_paragraphs_become_paragraph_blocks(self):
        out = to_blocks(SAMPLE_MD)
        assert out.count("<!-- wp:paragraph -->") >= 3

    def test_list_becomes_list_and_list_item_blocks(self):
        out = to_blocks(SAMPLE_MD)
        assert "<!-- wp:list -->" in out
        assert out.count("<!-- wp:list-item -->") == 2
        assert 'class="wp-block-list"' in out

    def test_markdown_syntax_does_not_survive(self):
        """The whole point: no literal ## or [](), which is what shipped."""
        out = to_blocks(SAMPLE_MD)
        assert "## Standards" not in out
        assert "[link](https://example.com/x)" not in out
        assert '<a href="https://example.com/x">link</a>' in out

    def test_leading_h1_is_dropped(self):
        """WordPress renders the title itself; keeping it publishes it twice."""
        out = to_blocks(SAMPLE_MD)
        assert "<h1>" not in out
        assert "About" not in out.split("<!-- wp:heading -->")[0]

    def test_leading_h1_kept_when_asked(self):
        out = to_blocks(SAMPLE_MD, strip_leading_h1=False)
        assert "<h1>About</h1>" in out

    def test_existing_block_markup_passes_through_untouched(self):
        existing = (
            "<!-- wp:paragraph -->\n<p>Already blocks.</p>\n<!-- /wp:paragraph -->"
        )
        assert to_blocks(existing) == existing

    def test_ordered_list_is_marked_ordered(self):
        out = to_blocks("1. first\n2. second\n")
        assert '<!-- wp:list {"ordered":true} -->' in out
        assert "<ol" in out

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_empty_content_stays_empty(self, empty):
        assert to_blocks(empty) == ""

    def test_looks_like_block_markup(self):
        assert looks_like_block_markup("<!-- wp:paragraph --><p>x</p>")
        assert not looks_like_block_markup("## Just markdown")


PUBLICATION_TEMPLATE = (
    Path(handoff_parser.__file__).parent / "handoff_templates" / "publication.md"
)


def _template_seo_labels():
    """The ``Label:`` lines publication.md's SEO METADATA block offers."""
    text = PUBLICATION_TEMPLATE.read_text(encoding="utf-8")
    block = text.split("\nSEO METADATA\n", 1)[1].split("\nEMBEDS AND", 1)[0]
    return re.findall(r"^([A-Z][A-Za-z ]*):", block, re.MULTILINE)


def _handoff_from_seo_block(seo_lines):
    return parse_publication_handoff(
        "PUBLICATION HANDOFF\nArticle: About\n\n"
        f"SEO METADATA\n{seo_lines}\n\n"
        "FINAL DRAFT\nBody.\n"
    )


def _meta_from_handoff(seo_lines):
    """What leaves for Rank Math, from handoff text rather than a hand-built dict."""
    handoff = _handoff_from_seo_block(seo_lines)
    return wp.rank_math_meta(
        {"title": handoff["title"], "seo": handoff["seo"]}, RANK_MATH
    )


class TestRankMathMeta:
    def test_seo_title_sets_the_title_field(self):
        meta = wp.rank_math_meta(
            {"title": "About", "seo": {"seo_title": "About Mike | Standards"}},
            RANK_MATH,
        )
        assert meta["rank_math_title"] == "About Mike | Standards"

    def test_og_title_is_the_fallback_seo_title(self):
        """A handoff that wrote an OG title meant it to be search-facing too."""
        meta = wp.rank_math_meta(
            {"title": "About", "seo": {"og_title": "About Mike | Standards"}},
            RANK_MATH,
        )
        assert meta["rank_math_title"] == "About Mike | Standards"
        assert meta["rank_math_facebook_title"] == "About Mike | Standards"

    def test_the_template_block_was_found(self):
        """Guards the parametrized test below against running on nothing."""
        assert "Focus keyword" in _template_seo_labels()

    @pytest.mark.parametrize("label", _template_seo_labels())
    def test_every_field_the_template_offers_reaches_rank_math(self, label):
        """The guard the schema field needed and did not have.

        The template kept offering "Schema type:" after the push stopped
        reading it, and nothing failed. Labels are read from the template, and
        each goes through the real parser, so a field offered there but never
        wired through to Rank Math fails here instead of vanishing.
        """
        sentinel = f"sentinel for {label}"
        meta = _meta_from_handoff(f"{label}: {sentinel}")
        assert sentinel in meta.values()

    def test_a_leftover_schema_type_line_is_not_sent(self):
        """Nothing sets schema from a handoff, so the template stopped asking.

        Older copies still carry the line, and a real one was written as
        "other — AboutPage". Its text must reach no Rank Math field — under
        the old parser, the blank line above it made it the OG description —
        and the parser still reports it, so the publish can say so.
        """
        text = (
            "Focus keyword: mike hammett\n"
            "OG description:\n"
            "Schema type: other — AboutPage"
        )
        handoff = _handoff_from_seo_block(text)
        meta = _meta_from_handoff(text)

        assert not [v for v in meta.values() if "AboutPage" in v]
        assert not [k for k in meta if "schema" in k or "rich_snippet" in k]
        assert handoff["ignored_schema_type"] == "other — AboutPage"

    def test_og_fields_omitted_when_auto_set_og_tags_is_off(self):
        meta = wp.rank_math_meta(
            {"title": "About", "seo": {"og_title": "T", "meta_description": "D"}},
            {"auto_set_og_tags": False},
        )
        assert "rank_math_facebook_title" not in meta
        assert meta["rank_math_description"] == "D"

    def test_no_seo_block_yields_only_the_title(self):
        meta = wp.rank_math_meta({"title": "About"}, RANK_MATH)
        assert meta["rank_math_title"] == "About"


def _push(pub_params, content="# T\n\nBody.\n", rank_math_status=200):
    with (
        patch("ci_article_review.adapters.cms.wordpress.requests.post") as mock_post,
        patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get,
    ):
        created = MagicMock(raise_for_status=MagicMock())
        created.json.return_value = {"id": 7, "link": "https://example.com/about/"}
        rm = MagicMock()
        if rank_math_status != 200:
            import requests as _rq

            rm.raise_for_status.side_effect = _rq.HTTPError("boom")
        mock_post.side_effect = [created, rm]
        mock_get.return_value = MagicMock(
            ok=True, json=MagicMock(return_value=[{"id": 3, "slug": "s"}])
        )
        result = wp.push(content, pub_params, WP_CONFIG, RANK_MATH)
    return result, mock_post


class TestPushIntegration:
    def test_content_is_converted_before_sending(self):
        _, mock_post = _push(
            {"title": "About", "post_type": "page", "tags": []},
            content="# About\n\n## Standards\n\nBody.\n",
        )
        sent = mock_post.call_args_list[0][1]["json"]["content"]
        assert "<!-- wp:heading -->" in sent
        assert "## Standards" not in sent

    def test_rank_math_endpoint_is_called_after_creation(self):
        _, mock_post = _push(
            {
                "title": "About",
                "post_type": "page",
                "tags": [],
                "seo": {"focus_keyword": "mike", "seo_title": "About Mike"},
            }
        )
        assert mock_post.call_count == 2
        url = mock_post.call_args_list[1][0][0]
        body = mock_post.call_args_list[1][1]["json"]
        assert url.endswith("/wp-json/rankmath/v1/updateMeta")
        assert body["objectID"] == 7
        assert body["objectType"] == "post"
        assert body["meta"]["rank_math_focus_keyword"] == "mike"

    def test_rank_math_is_not_called_when_there_is_nothing_to_set(self):
        _, mock_post = _push({"title": "", "post_type": "page", "tags": []})
        assert mock_post.call_count == 1

    def test_rank_math_failure_does_not_fail_the_publish(self):
        """The post already exists; losing SEO fields must not raise."""
        result, _ = _push(
            {
                "title": "About",
                "post_type": "page",
                "tags": [],
                "seo": {"focus_keyword": "mike"},
            },
            rank_math_status=500,
        )
        assert result["success"] is True
        assert result["post_id"] == 7
        assert "rank_math_error" in result

    def test_rank_math_keys_are_not_in_the_create_payload(self):
        """Core drops them; sending them is what hid the bug for months."""
        _, mock_post = _push(
            {
                "title": "About",
                "post_type": "page",
                "tags": [],
                "seo": {"focus_keyword": "mike"},
            }
        )
        created_meta = mock_post.call_args_list[0][1]["json"].get("meta", {})
        assert not [k for k in created_meta if k.startswith("rank_math_")]
