"""Near-miss labels in a publication handoff are reported, not silently dropped.

Issue #399: a handoff written with ``Category:`` for ``WordPress category:``
(and several more) was parsed without complaint and the fields went missing.
Every handoff text here is invented.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from ci_article_review import handoff_parser
from ci_article_review.handoff_parser import parse_publication_handoff
from ci_article_review.pipeline import run_publish_pipeline

_TEMPLATE = (
    Path(handoff_parser.__file__).parent / "handoff_templates" / "publication.md"
)


def _handoff(params="", seo="", draft="Body of the invented article."):
    return (
        "PUBLICATION HANDOFF\nArticle: An Invented Title\nPublication: demo\n\n"
        f"PUBLICATION PARAMETERS\n{params}\n\nSEO METADATA\n{seo}\n\n"
        f"FINAL DRAFT\n{draft}\n"
    )


def _unread(text):
    return {
        (u["section"], u["label"]): u["suggestion"]
        for u in parse_publication_handoff(text)["unread_labels"]
    }


class TestUnreadLabels:
    def test_the_issues_misspelt_labels_are_all_found(self):
        text = _handoff(
            "Category: demo-cat\nWordPress slug: invented-slug\nStatus: draft",
            "SEO title (Rank Math): A title\nOpen Graph title: A title\n"
            "Open Graph description: same as the meta description",
        )
        assert _unread(text) == {
            ("PUBLICATION PARAMETERS", "Category"): "WordPress category",
            ("PUBLICATION PARAMETERS", "WordPress slug"): "Slug",
            ("SEO METADATA", "SEO title (Rank Math)"): "SEO title",
            ("SEO METADATA", "Open Graph title"): "OG title",
            ("SEO METADATA", "Open Graph description"): "OG description",
        }

    def test_the_accepted_format_is_unchanged(self):
        """A warning only: the misspelt field still reads as absent."""
        result = parse_publication_handoff(_handoff("Category: demo-cat"))
        assert result["publication_parameters"]["wordpress_category"] == ""

    def test_a_wrong_case_label_is_reported(self):
        assert _unread(_handoff("Wordpress category: x")) == {
            ("PUBLICATION PARAMETERS", "Wordpress category"): "WordPress category"
        }

    def test_a_label_with_nothing_close_has_no_suggestion(self):
        assert _unread(_handoff("Zebra crossing: x")) == {
            ("PUBLICATION PARAMETERS", "Zebra crossing"): ""
        }

    def test_every_label_the_parser_reads_is_quiet(self):
        params = "\n".join(
            f"{label} x"
            for label in handoff_parser._PUBLICATION_PARAMETER_LABELS.values()
        )
        seo = "\n".join(f"{k}: x" for k in handoff_parser._SEO_FIELD_MAP)
        assert _unread(_handoff(params, seo + "\nSchema type: other")) == {}

    def test_colons_inside_a_wrapped_placeholder_are_not_labels(self):
        params = (
            "Post type: [post (default) | page -- use page for standing pages such as\n"
            "About or Contact: no date, no category. A page\n"
            "ignores the fields below.]\nTags: a, b"
        )
        assert _unread(_handoff(params)) == {}

    def test_the_shipped_template_is_quiet(self):
        """Positive control for the skip rule: the real template wraps
        placeholders with colons in them (Alt text:, About or Contact:)."""
        result = parse_publication_handoff(_TEMPLATE.read_text("utf-8"))
        assert result["unread_labels"] == []

    def test_the_shipped_template_with_a_misspelt_label_is_reported(self):
        text = _TEMPLATE.read_text("utf-8").replace(
            "WordPress category:", "Category:", 1
        )
        assert _unread(text) == {
            ("PUBLICATION PARAMETERS", "Category"): "WordPress category"
        }

    def test_it_is_logged_as_a_warning(self, caplog):
        with caplog.at_level("WARNING", logger="ci_article_review.handoff_parser"):
            parse_publication_handoff(_handoff("Category: demo-cat"))
        assert any(
            "Category" in r.getMessage() and "WordPress category" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )


class TestPublishPrintsThem:
    def test_the_warning_is_printed_before_a_refusal(self, tmp_path, capsys):
        """No FINAL DRAFT makes run_publish_pipeline exit early, which is the
        scenario where the warning must already have been shown."""
        path = tmp_path / "pub.md"
        path.write_text(_handoff("Category: demo-cat", draft=""), encoding="utf-8")
        config = {"publication": {"wordpress": {}}, "api_keys": {}}
        with (
            patch("ci_article_review.pipeline.load_user_config", return_value={}),
            patch(
                "ci_article_review.pipeline.load_publication_config", return_value={}
            ),
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            pytest.raises(SystemExit),
        ):
            run_publish_pipeline(str(path), "testpub", seo_suggestions=False)
        out = capsys.readouterr().out
        assert "NOT READ" in out
        assert "'Category'" in out and "WordPress category" in out
