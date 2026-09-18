"""Tests for output.py — formatters, merge, diff, atomic write, versioning."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path


_CANONICAL_PROFILE = {
    "style_profile": "Writes with clarity and precision. Uses evidence-based arguments.",
    "audience_primary": "Business professionals and executives",
    "audience_secondary": None,
    "banned_words": ["utilize", "leverage", "synergy"],
    "banned_phrases": ["at the end of the day", "moving forward"],
    "positive_rules": [
        "Lead with the main claim",
        "Use concrete examples",
        "Avoid hedging language",
    ],
}

_DETECT_PROFILE = {
    "canonical": _CANONICAL_PROFILE,
    "detected_styles": {
        "technical analysis": {
            "style_profile": "Long-form analytical writing with sequential argumentation.",
            "additional_banned_words": ["basically"],
            "additional_positive_rules": ["Use numbered lists for technical steps"],
            "style_notes": "Deployed for in-depth analysis posts.",
            "source_distribution": {"wordpress": 0.85, "textfiles": 0.15},
            "doc_count": 42,
            "confidence": "high",
        },
        "direct editorial": {
            "style_profile": "Short, opinionated. First person. Questions.",
            "additional_banned_words": [],
            "additional_positive_rules": ["One main point per post"],
            "style_notes": "Used for quick takes on current events.",
            "source_distribution": {"wordpress": 0.6, "twitter": 0.4},
            "doc_count": 28,
            "confidence": "medium",
        },
    },
}

_EXISTING_YAML = """\
publication_name: Mike Hammett
wordpress:
  site_url: https://mikehammett.net
  username: admin
  application_password: secret

rank_math:
  auto_seo: true

citation_sources:
  - name: Wikipedia
    weight: 0.5

style_profile: |
  Old style profile text.
style_rules:
  banned_words: [old_word]
  banned_phrases: []
  positive_rules: []
"""


class TestPublicationYamlFormatterCanonical:
    def test_canonical_format_has_required_keys(self):
        """Canonical format produces valid YAML with required keys; no style_profiles block."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()
        result = fmt.format(_CANONICAL_PROFILE, mode="canonical")

        assert "style_profile:" in result
        assert "banned_words:" in result or "banned_words" in result
        assert "positive_rules:" in result or "positive_rules" in result
        assert "style_profiles:" not in result


class TestPublicationYamlFormatterDetect:
    def test_detect_format_has_style_profiles_block(self):
        """detect mode: canonical at top level AND style_profiles: block keyed by model-generated labels."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()
        result = fmt.format(_DETECT_PROFILE, mode="detect")

        assert "style_profile:" in result
        assert "style_profiles:" in result
        assert "technical analysis:" in result
        assert "direct editorial:" in result
        # Internal key 'detected_styles' should NOT appear in YAML
        assert "detected_styles:" not in result


class TestMergeIntoExisting:
    def test_preserves_non_style_sections(self):
        """merge_into_existing preserves wordpress, rank_math, citation_sources sections."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(_EXISTING_YAML)
            tmppath = f.name

        try:
            result = fmt.merge_into_existing(
                tmppath, _CANONICAL_PROFILE, mode="canonical"
            )

            assert "site_url:" in result
            assert "rank_math:" in result
            assert "citation_sources:" in result
            assert "auto_seo:" in result
        finally:
            Path(tmppath).unlink(missing_ok=True)

    def test_replaces_style_sections(self):
        """merge_into_existing replaces old style_profile and style_rules."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(_EXISTING_YAML)
            tmppath = f.name

        try:
            result = fmt.merge_into_existing(
                tmppath, _CANONICAL_PROFILE, mode="canonical"
            )
            assert "Old style profile text" not in result
            assert "old_word" not in result
        finally:
            Path(tmppath).unlink(missing_ok=True)

    def test_detect_mode_adds_style_profiles_block(self):
        """detect mode: adds style_profiles: block; does not touch other sections."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(_EXISTING_YAML)
            tmppath = f.name

        try:
            result = fmt.merge_into_existing(tmppath, _DETECT_PROFILE, mode="detect")
            assert "style_profiles:" in result
            assert "technical analysis:" in result
            assert "rank_math:" in result
        finally:
            Path(tmppath).unlink(missing_ok=True)


class TestDiffStyleSections:
    def test_diff_empty_when_identical(self):
        """diff_style_sections returns empty string when profiles are identical."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()

        new_yaml = fmt.format(_CANONICAL_PROFILE, mode="canonical")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(new_yaml)
            tmppath = f.name

        try:
            diff = fmt.diff_style_sections(tmppath, new_yaml)
            assert diff == ""
        finally:
            Path(tmppath).unlink(missing_ok=True)

    def test_diff_shows_changes(self):
        """diff_style_sections shows changes between old and new style sections."""
        from ci_style_profile.output import PublicationYamlFormatter

        fmt = PublicationYamlFormatter()

        old_yaml = (
            "style_profile: |\n  Old profile text\nstyle_rules:\n  banned_words: []\n"
        )
        new_yaml = fmt.format(_CANONICAL_PROFILE, mode="canonical")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(old_yaml)
            tmppath = f.name

        try:
            diff = fmt.diff_style_sections(tmppath, new_yaml)
            assert diff  # Not empty
        finally:
            Path(tmppath).unlink(missing_ok=True)


class TestAtomicWrite:
    def test_atomic_write_success(self):
        """Atomic write creates file; .tmp cleaned up on success."""
        from ci_style_profile.output import write_atomic

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "profile.yaml"
            write_atomic(output_path, "content: test\n")
            assert output_path.read_text() == "content: test\n"
            assert not (output_path.with_suffix(".tmp")).exists()


class TestMarkdownFormatter:
    def test_markdown_has_style_sections(self):
        """MarkdownReportFormatter produces sections per detected style."""
        from ci_style_profile.output import MarkdownReportFormatter

        fmt = MarkdownReportFormatter()
        result = fmt.format(_DETECT_PROFILE, mode="detect")

        assert "# Style Profile Report" in result
        assert "## Detected Styles" in result
        assert "### technical analysis" in result
        assert "### direct editorial" in result

    def test_markdown_canonical_mode(self):
        """Canonical mode has no detected styles section."""
        from ci_style_profile.output import MarkdownReportFormatter

        fmt = MarkdownReportFormatter()
        result = fmt.format(_CANONICAL_PROFILE, mode="canonical")

        assert "## Detected Styles" not in result
        assert "## Style Profile" in result


class TestProfileVersioning:
    def test_snapshot_saved_to_profiles_dir(self):
        """Snapshot written to profiles/<name>/<ISO8601>.yaml."""
        from ci_style_profile import output

        snap = output.save_versioned_snapshot(
            "content: test\n", publication="test_pub", output_yaml=None
        )

        # _PROFILES_DIR is this test's scratch directory: see tests/conftest.py.
        assert snap.parent == output._PROFILES_DIR / "test_pub"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ\.yaml", snap.name)
        assert snap.read_text(encoding="utf-8") == "content: test\n"
