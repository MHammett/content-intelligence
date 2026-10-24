"""The pre-publication checklist does not name a backup plugin the site may lack.

Its last item used to read "UpdraftPlus backup is current" for every publication.
The one site it was run against has Rank Math and Wordfence and no UpdraftPlus,
and an agent relayed the line to the author as a to-do without checking (issue
#327). The item is now ``wordpress.backup_check`` in the publication config, with
a default that names no plugin, and it is dropped when the setting is empty.

The last test goes through ``run_publish_pipeline`` and reads what it prints,
because the setting is only worth anything if the pipeline hands it over.
"""

from unittest.mock import patch

import pytest

from ci_article_review.adapters.cms import wordpress as wp

from .test_wordpress_images import (
    RANK_MATH,
    WP_CONFIG,
    FakeWordPress,
    _handoff_text,
)


def _items(text):
    return [line for line in text.splitlines() if line.startswith("[ ]")]


class TestChecklistText:
    def test_the_default_names_no_plugin(self):
        text = wp.checklist_text({})
        assert _items(text)[-1] == "[ ] Site backup is current (host or plugin)"
        assert "UpdraftPlus" not in text

    def test_no_config_is_the_default(self):
        assert wp.checklist_text() == wp.checklist_text({})
        assert wp.CHECKLIST == wp.checklist_text()

    def test_the_default_is_a_checklist_item_like_the_rest(self):
        """Last in PUBLICATION, as the plugin line was, with the body unchanged."""
        text = wp.checklist_text({})
        assert text.endswith(wp.DEFAULT_BACKUP_CHECK + "\n")
        assert text.startswith(wp._CHECKLIST_BODY)

    def test_a_publication_names_its_own_backup_item(self):
        text = wp.checklist_text({"backup_check": "UpdraftPlus backup is current"})
        assert _items(text)[-1] == "[ ] UpdraftPlus backup is current"
        assert "host or plugin" not in text

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_an_empty_setting_drops_the_item(self, empty):
        """An explicit empty is different from an absent key: it is a choice."""
        text = wp.checklist_text({"backup_check": empty})
        assert "backup" not in text.lower()
        assert text == wp._CHECKLIST_BODY
        assert _items(text)[-1].startswith("[ ] Status is draft")

    def test_a_wrapped_value_is_one_line(self):
        text = wp.checklist_text({"backup_check": "Host backup\n  is current"})
        assert _items(text)[-1] == "[ ] Host backup is current"

    def test_the_other_items_are_untouched(self):
        for config in ({}, {"backup_check": ""}, {"backup_check": "x"}):
            text = wp.checklist_text(config)
            assert "[ ] WordPress category correct" in text
            assert "[ ] Status is draft (default)" in text


class TestPrintChecklist:
    def _run(self, wp_config, capsys):
        with patch("builtins.input", return_value="yes"):
            confirmed = wp.print_checklist_and_confirm(wp_config)
        return confirmed, capsys.readouterr().out

    def test_it_prints_the_publications_item(self, capsys):
        confirmed, out = self._run({"backup_check": "Host snapshot taken"}, capsys)
        assert confirmed is True
        assert "[ ] Host snapshot taken" in out
        assert "UpdraftPlus" not in out

    def test_it_prints_the_default_when_given_nothing(self, capsys):
        with patch("builtins.input", return_value="yes"):
            wp.print_checklist_and_confirm()
        assert "Site backup is current (host or plugin)" in capsys.readouterr().out

    def test_an_empty_setting_prints_no_backup_item(self, capsys):
        _, out = self._run({"backup_check": ""}, capsys)
        assert "backup" not in out.lower()


class TestPipelinePassesTheSetting:
    def _publish(self, tmp_path, capsys, **wordpress):
        from ci_article_review.pipeline import run_publish_pipeline

        path = tmp_path / "grid-publication.md"
        path.write_text(_handoff_text("", post_type="page"), encoding="utf-8")
        config = {
            "publication": {
                "wordpress": {**WP_CONFIG, **wordpress},
                "rank_math": dict(RANK_MATH),
            },
            "api_keys": {},
        }
        fake = FakeWordPress()
        module = "ci_article_review.adapters.cms.wordpress"
        with (
            patch("ci_article_review.pipeline.load_user_config", return_value={}),
            patch(
                "ci_article_review.pipeline.load_publication_config", return_value={}
            ),
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            patch("ci_article_review.pipeline._suggest_seo_for_publish"),
            patch(f"{module}.requests.post", fake.post),
            patch(f"{module}.requests.get", fake.get),
            patch("builtins.input", return_value="yes"),
        ):
            run_publish_pipeline(str(path), "testpub")
        return capsys.readouterr().out

    def test_the_default_reaches_the_printed_checklist(self, tmp_path, capsys):
        out = self._publish(tmp_path, capsys)
        assert "[ ] Site backup is current (host or plugin)" in out
        assert "UpdraftPlus" not in out

    def test_the_publications_setting_reaches_it(self, tmp_path, capsys):
        out = self._publish(tmp_path, capsys, backup_check="Host snapshot taken")
        assert "[ ] Host snapshot taken" in out
        assert "Site backup is current" not in out

    def test_an_empty_setting_leaves_the_item_off(self, tmp_path, capsys):
        out = self._publish(tmp_path, capsys, backup_check="")
        assert "PRE-PUBLICATION CHECKLIST" in out
        assert "backup" not in out.lower()
