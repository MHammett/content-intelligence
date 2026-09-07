"""Tests for authorship provenance in the run report.

The feature is one sentence -- record which provider drafted the article and
whether that provider marks its text -- and almost all of the risk is in
overclaiming. A block that reads like a detection result would be worse than no
block at all, so several of these assert on what is *not* said.
"""

from ci_article_review.consolidation import _build_provenance
from ci_article_review.pipeline import _declared_drafter, _drafting_model
from ci_article_review.report_markdown import _render_provenance


def rendered(drafted_with):
    return "\n".join(
        _render_provenance({"provenance": _build_provenance(drafted_with)})
    )


class TestDeclaredDrafter:
    """The handoff line wins over the config default, and the raw string
    survives -- that split is why _declared_drafter exists separately."""

    def test_handoff_wins_over_config(self):
        assert (
            _declared_drafter({"drafted_with": "claude"}, {"drafting_model": "openai"})
            == "claude"
        )

    def test_config_is_the_fallback(self):
        assert _declared_drafter({}, {"drafting_model": "openai"}) == "openai"

    def test_undeclared_is_empty(self):
        assert _declared_drafter({}, {}) == ""

    def test_whitespace_only_declaration_is_undeclared(self):
        assert _declared_drafter({"drafted_with": "   "}, {}) == ""

    def test_an_unrecognised_name_survives_for_reporting(self):
        # _drafting_model must return None so no review pass is wrongly
        # excluded, but the report still has to say what the handoff said.
        handoff, cfg = {"drafted_with": "gpt-4o"}, {}
        assert _drafting_model(handoff, cfg) is None
        assert _declared_drafter(handoff, cfg) == "gpt-4o"

    def test_the_two_agree_on_a_recognised_provider(self):
        handoff, cfg = {"drafted_with": "Claude"}, {}
        assert _drafting_model(handoff, cfg) == "claude"
        assert _declared_drafter(handoff, cfg) == "Claude"


class TestProvenanceBlock:
    def test_a_marking_provider_is_flagged(self):
        prov = _build_provenance("claude")
        assert prov["drafted_with"] == "claude"
        assert prov["marked"] is True

    def test_a_non_marking_provider_is_not_flagged(self):
        assert _build_provenance("openai")["marked"] is False

    def test_an_unrecognised_name_is_recorded_verbatim_as_unknown(self):
        prov = _build_provenance("gpt-4o")
        assert prov["drafted_with"] == "gpt-4o"
        assert prov["status"] == "unknown"
        assert prov["marked"] is False

    def test_undeclared_records_none_rather_than_an_empty_string(self):
        assert _build_provenance("")["drafted_with"] is None

    def test_the_block_carries_its_own_caveat(self):
        assert "not measured" in _build_provenance("claude")["basis"]


class TestRendering:
    def test_a_marking_provider_produces_a_section(self):
        out = rendered("claude")
        assert "## Authorship Provenance" in out
        assert "Drafted with: **claude**" in out

    def test_the_rendered_block_says_it_cannot_detect(self):
        # The line that keeps an author from reading this as a scan result.
        assert "Nothing in this pipeline can detect" in rendered("claude")

    def test_partial_status_says_assume_present(self):
        assert "rather than absent" in rendered("gemini")

    def test_a_non_marking_provider_renders_nothing(self):
        assert rendered("openai") == ""

    def test_an_undeclared_drafter_renders_nothing(self):
        assert rendered("") == ""

    def test_a_report_without_the_block_renders_nothing(self):
        # Reports written before this feature must render exactly as they did.
        assert _render_provenance({}) == []

    def test_a_marking_provider_cites_its_source_in_the_output(self):
        assert "anthropic.com" in rendered("claude")


class TestStaleRegistryWarning:
    def test_a_stale_registry_is_called_out_in_the_report(self, monkeypatch):
        from ci_core.llm import watermarking

        monkeypatch.setattr(
            watermarking, "staleness", lambda today=None: ("warning", 200)
        )
        out = "\n".join(_render_provenance({"provenance": _build_provenance("claude")}))
        assert "200 days ago" in out
        assert "re-check" in out

    def test_a_fresh_registry_adds_no_staleness_line(self):
        assert "days ago" not in rendered("claude")
