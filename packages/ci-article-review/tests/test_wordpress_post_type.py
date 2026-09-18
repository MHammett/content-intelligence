"""WordPress ``Post type:`` routing — post (default) vs page.

An About or Contact page is a WordPress *page*, not a post: no date, no
category, not in the blog feed. Before this, the adapter hardcoded ``/posts``,
so publishing a standing page through the pipeline produced a dated blog entry
in the RSS feed — correct-looking output of the wrong content type, which is
the kind of failure a green publish hides.

These assert the invariants (which route, which fields) rather than a payload
snapshot, so adding a field for posts later does not break them spuriously.
"""

from unittest.mock import MagicMock, patch

import pytest

from ci_article_review.adapters.cms.wordpress import (
    POST_TYPE_ROUTES,
    _build_post_payload,
    push,
    resolve_post_type,
)

WP_CONFIG = {
    "site_url": "https://example.com",
    "username": "editor",
    "application_password": "pass word here",
    "rest_api_endpoint": "/wp-json/wp/v2",
}
RANK_MATH = {"auto_set_og_tags": True, "default_schema_type": "Article"}


def _created_response():
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"id": 7, "link": "https://example.com/about/"}
    return mock


def _push(pub_params, **kw):
    """Push with the network stubbed; returns (result, post_mock, get_mock).

    ``push`` issues more than one POST: the create, then Rank Math's
    ``updateMeta``. Read the create with ``_created_call`` rather than
    ``call_args``, which is the *last* call and would silently start asserting
    against the SEO request instead.
    """
    with (
        patch("ci_article_review.adapters.cms.wordpress.requests.post") as mock_post,
        patch("ci_article_review.adapters.cms.wordpress.requests.get") as mock_get,
    ):
        mock_post.return_value = _created_response()
        mock_get.return_value = MagicMock(
            ok=True, json=MagicMock(return_value=[{"id": 3, "slug": "essays"}])
        )
        result = push("<p>body</p>", pub_params, WP_CONFIG, RANK_MATH, **kw)
    return result, mock_post, mock_get


def _created_call(mock_post):
    """The POST that created the post/page — always the first one."""
    return mock_post.call_args_list[0]


class TestResolvePostType:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_means_post(self, raw):
        """Every handoff written before this field existed is an article."""
        assert resolve_post_type(raw) == "post"

    @pytest.mark.parametrize("raw", ["page", "PAGE", "  Page  "])
    def test_page_is_normalised(self, raw):
        assert resolve_post_type(raw) == "page"

    @pytest.mark.parametrize("raw", ["post", "Post ", "  POST"])
    def test_post_is_normalised(self, raw):
        """Hand-typed handoff fields carry stray case and whitespace."""
        assert resolve_post_type(raw) == "post"

    @pytest.mark.parametrize("raw", ["pages", "posts", "attachment", "custom"])
    def test_unknown_type_is_rejected(self, raw):
        """A typo must not reach the REST API as a 404 on an invented route."""
        with pytest.raises(ValueError, match="Unknown post type"):
            resolve_post_type(raw)

    def test_routes_cover_every_supported_type(self):
        assert set(POST_TYPE_ROUTES) == {"post", "page"}


class TestPayload:
    def test_post_carries_taxonomy(self):
        payload = _build_post_payload(
            {"title": "T"},
            WP_CONFIG,
            RANK_MATH,
            "body",
            category_ids=[3],
            tag_ids=[4],
            post_type="post",
        )
        assert payload["categories"] == [3]
        assert payload["tags"] == [4]

    def test_page_omits_taxonomy_entirely(self):
        """Absent, not empty: /pages does not register these fields."""
        payload = _build_post_payload(
            {"title": "About"},
            WP_CONFIG,
            RANK_MATH,
            "body",
            category_ids=[],
            tag_ids=[],
            post_type="page",
        )
        assert "categories" not in payload
        assert "tags" not in payload

    def test_rank_math_keys_are_not_in_the_create_payload(self):
        """This asserted the opposite until the fields were found never to apply.

        Rank Math does not register its meta keys with the core REST API, and
        core silently drops unregistered keys — so sending them here set
        nothing while the publish returned 200. They go to Rank Math's own
        endpoint after creation now; see test_wordpress_content_rendering.py.
        """
        payload = _build_post_payload(
            {
                "title": "About",
                "seo": {"focus_keyword": "mike hammett", "schema_type": "AboutPage"},
            },
            WP_CONFIG,
            RANK_MATH,
            "body",
            category_ids=[],
            tag_ids=[],
            post_type="page",
        )
        assert not [k for k in payload.get("meta", {}) if k.startswith("rank_math_")]

    def test_page_is_still_a_draft_by_default(self):
        payload = _build_post_payload(
            {"title": "About"},
            WP_CONFIG,
            RANK_MATH,
            "body",
            category_ids=[],
            tag_ids=[],
            post_type="page",
        )
        assert payload["status"] == "draft"


class TestPushRouting:
    def test_default_goes_to_posts(self):
        result, mock_post, _ = _push({"title": "Article", "tags": []})
        assert result["success"] is True
        assert _created_call(mock_post)[0][0].endswith("/wp-json/wp/v2/posts")
        assert result["post_type"] == "post"

    def test_page_goes_to_pages(self):
        result, mock_post, _ = _push(
            {"title": "About", "post_type": "page", "tags": []}
        )
        assert result["success"] is True
        assert _created_call(mock_post)[0][0].endswith("/wp-json/wp/v2/pages")
        assert result["post_type"] == "page"

    def test_page_skips_term_lookup_calls(self):
        """Pages are in neither taxonomy — don't spend the two GETs."""
        _, _, mock_get = _push(
            {
                "title": "About",
                "post_type": "page",
                "wordpress_category": "essays",
                "tags": ["a", "b"],
            }
        )
        assert mock_get.call_count == 0

    def test_page_reports_terms_it_could_not_apply(self):
        """Named in the handoff but impossible on a page: say so, don't drop."""
        result, _, _ = _push(
            {
                "title": "About",
                "post_type": "page",
                "wordpress_category": "essays",
                "tags": ["grid"],
            }
        )
        assert result["success"] is True
        assert result["ignored_terms"] == ["essays", "grid"]

    def test_page_without_terms_reports_nothing(self):
        result, _, _ = _push({"title": "About", "post_type": "page", "tags": []})
        assert "ignored_terms" not in result

    def test_bad_type_fails_before_any_request(self):
        result, mock_post, mock_get = _push(
            {"title": "About", "post_type": "pages", "tags": []}
        )
        assert result["success"] is False
        assert "Unknown post type" in result["error"]
        assert mock_post.call_count == 0
        assert mock_get.call_count == 0

    def test_publish_live_still_applies_to_pages(self):
        _, mock_post, _ = _push(
            {"title": "About", "post_type": "page", "tags": []}, publish_live=True
        )
        assert _created_call(mock_post)[1]["json"]["status"] == "publish"
