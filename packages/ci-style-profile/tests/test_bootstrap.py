"""Integration tests for bootstrap.py CLI."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_doc(text: str = "This is test content. " * 60, source: str = "wordpress"):
    from ci_style_profile.collectors.base import Document

    doc = Document.from_text(
        text=text,
        source=source,
        register="long_form",
        date="2024-01-15",
        url_or_id="http://ex.com/1",
    )
    doc.metrics = {
        "avg_sentence_words": 15.0,
        "hedging_ratio": 0.05,
        "first_person_ratio": 0.2,
    }
    return doc


_MOCK_DOCS = [_make_doc(f"Article content {i}. " * 80) for i in range(10)]

_CANONICAL_RESULT = {
    "style_profile": "Clear analytical style.",
    "audience_primary": "Professionals",
    "audience_secondary": None,
    "banned_words": ["utilize"],
    "banned_phrases": [],
    "positive_rules": ["Be direct"],
}


def _run_bootstrap(*args):
    from ci_style_profile.bootstrap import main

    return main(list(args))


def _make_mock_registry(*source_names):
    """Return a REGISTRY dict with no-op mock collectors for the given source names."""
    from ci_style_profile.collectors.base import Collector

    registry = {}
    for name in source_names:
        _name = name

        class _MockCollector(Collector):
            SOURCE_NAME = _name

            @classmethod
            def validate_config(cls, config):
                pass

            def fetch(self, since=None):
                return iter([])

        _MockCollector.__name__ = f"MockCollector_{name}"
        registry[name] = _MockCollector
    return registry


class TestDryRun:
    def test_dry_run_exits_before_synthesis(self):
        """--dry-run exits before synthesis; no output file written."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.yaml"

            with (
                patch("ci_style_profile.bootstrap._load_sources_yaml", return_value={}),
                patch(
                    "ci_style_profile.bootstrap._load_user_config_lenient",
                    return_value={},
                ),
                patch(
                    "ci_style_profile.collectors.REGISTRY",
                    _make_mock_registry("wordpress"),
                ),
                patch(
                    "ci_style_profile.bootstrap._collect_source",
                    return_value=_MOCK_DOCS,
                ),
            ):
                rc = _run_bootstrap(
                    "--output-yaml",
                    str(output_path),
                    "--sources",
                    "wordpress",
                    "--style",
                    "canonical",
                    "--dry-run",
                )

            assert rc == 0
            assert not output_path.exists()


class TestModelCurrencyLog:
    """The currency check logs what it found, not everything it returned.

    check_model_currency returns a dict: the warnings and notices, and the
    registry's date, age and two staleness flags. main() logged every truthy
    value in it at WARNING, so each run said "Model currency: 2026-08-18" and
    "Model currency: 32", and a superseded model came out as a raw list of
    dicts. The wording is ci-review's, from pipeline.run_draft_pipeline.
    """

    CURRENT = {
        "warnings": [],
        "notices": [],
        "registry_date": "2026-08-18",
        "registry_age_days": 32,
        "registry_stale": False,
        "registry_warning": False,
    }

    def _logged(self, caplog, currency=None):
        """(level, message) of every registry line a --dry-run logs.

        ``currency`` stands in for check_model_currency's answer; without it the
        real registry answers, for an empty models block.
        """
        import logging
        from contextlib import ExitStack

        with tempfile.TemporaryDirectory() as tmpdir, ExitStack() as stack:
            stack.enter_context(caplog.at_level(logging.INFO))
            # main() replaces the root handlers, caplog's among them.
            stack.enter_context(
                patch("ci_style_profile.logging_config.configure_logging")
            )
            stack.enter_context(
                patch("ci_style_profile.bootstrap._load_sources_yaml", return_value={})
            )
            stack.enter_context(
                patch(
                    "ci_style_profile.bootstrap._load_user_config_lenient",
                    return_value={},
                )
            )
            stack.enter_context(
                patch(
                    "ci_style_profile.collectors.REGISTRY",
                    _make_mock_registry("wordpress"),
                )
            )
            stack.enter_context(
                patch(
                    "ci_style_profile.bootstrap._collect_source",
                    return_value=_MOCK_DOCS,
                )
            )
            if currency is not None:
                stack.enter_context(
                    patch(
                        "ci_core.llm.model_registry.check_model_currency",
                        return_value=currency,
                    )
                )
            rc = _run_bootstrap(
                "--output-yaml",
                str(Path(tmpdir) / "out.yaml"),
                "--sources",
                "wordpress",
                "--style",
                "canonical",
                "--dry-run",
            )
        assert rc == 0
        return [
            (r.levelname, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith(
                ("Model currency", "Model registry", "Model upgrade")
            )
        ]

    def test_the_real_registrys_date_and_age_are_not_logged(self, caplog):
        """No models, so nothing is superseded. Whatever the registry holds, the
        only line it may earn is about its own age, which starts "Model registry"."""
        lines = self._logged(caplog)
        assert not [m for _, m in lines if m.startswith("Model currency")]

    def test_a_superseded_model_is_named_with_what_replaces_it(self, caplog):
        warning = {
            "provider": "openai",
            "model": "gpt-5.4",
            "replacement": "gpt-5.6-terra",
            "note": "gpt-5.6 family launched at matching price tiers",
        }
        lines = self._logged(caplog, {**self.CURRENT, "warnings": [warning]})
        assert lines == [
            (
                "WARNING",
                "Model currency: openai is using 'gpt-5.4', which has been "
                "superseded by 'gpt-5.6-terra'. gpt-5.6 family launched at "
                "matching price tiers. Update user.yaml to use the newer model.",
            )
        ]

    def test_a_superseded_model_with_no_note_has_no_stray_punctuation(self, caplog):
        warning = {
            "provider": "openai",
            "model": "gpt-5.4",
            "replacement": "gpt-5.6-terra",
            "note": "",
        }
        lines = self._logged(caplog, {**self.CURRENT, "warnings": [warning]})
        assert lines == [
            (
                "WARNING",
                "Model currency: openai is using 'gpt-5.4', which has been "
                "superseded by 'gpt-5.6-terra'. Update user.yaml to use the "
                "newer model.",
            )
        ]

    def test_a_newer_model_is_an_optional_notice_not_a_warning(self, caplog):
        notice = {
            "provider": "claude",
            "model": "claude-haiku-4-5-20251001",
            "newer": "claude-sonnet-5",
            "note": "claude-sonnet-5 available; haiku remains better value",
        }
        lines = self._logged(caplog, {**self.CURRENT, "notices": [notice]})
        assert lines == [
            (
                "INFO",
                "Model upgrade available (optional): claude is using "
                "'claude-haiku-4-5-20251001' — claude-sonnet-5 available; "
                "haiku remains better value",
            )
        ]

    def test_a_notice_with_no_note_names_the_newer_model(self, caplog):
        notice = {
            "provider": "claude",
            "model": "claude-haiku-4-5-20251001",
            "newer": "claude-sonnet-5",
            "note": "",
        }
        lines = self._logged(caplog, {**self.CURRENT, "notices": [notice]})
        assert lines == [
            (
                "INFO",
                "Model upgrade available (optional): claude is using "
                "'claude-haiku-4-5-20251001' — newer: claude-sonnet-5",
            )
        ]

    def test_an_old_registry_warns_once_with_its_age(self, caplog):
        old = {
            **self.CURRENT,
            "registry_age_days": 130,
            "registry_stale": True,
            "registry_warning": True,
        }
        assert self._logged(caplog, old) == [
            (
                "WARNING",
                "Model registry is 130 days old (last updated 2026-08-18). "
                "Provider APIs change frequently — re-check available models "
                "and pricing.",
            )
        ]

    def test_a_stale_registry_is_only_a_note(self, caplog):
        stale = {**self.CURRENT, "registry_age_days": 70, "registry_stale": True}
        assert self._logged(caplog, stale) == [
            (
                "INFO",
                "Model registry last updated 2026-08-18 (70 days ago). "
                "Consider re-checking for newer models.",
            )
        ]

    def test_a_current_registry_and_models_log_nothing(self, caplog):
        assert self._logged(caplog, self.CURRENT) == []


class TestEffortNoneIsWarned:
    """`effort: none` in user.yaml does not stop claude-sonnet-5 thinking.

    main() merges the preset over user.yaml's model config key by key, keeping
    what the preset leaves unset, so a claude `effort: none` rides into any
    preset whose claude sets no effort. litellm drops the none and the model
    thinks at high. The warning is raised before collection, and --dry-run
    makes no calls. The presets are this test's own, so the merge is exercised
    whatever the shipped presets.yaml holds or whether it is found.
    """

    PRESETS = {
        "balanced": {"models": {"claude": {"model": "claude-sonnet-5"}}},
        "maximum": {"models": {"claude": {"model": "claude-opus-5", "effort": "high"}}},
    }

    def _run(self, caplog, preset, claude):
        import logging

        user_config = {"models": {"claude": dict(claude)}}
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                caplog.at_level(logging.WARNING),
                # main() replaces the root handlers, caplog's among them.
                patch("ci_style_profile.logging_config.configure_logging"),
                patch("ci_style_profile.bootstrap._load_sources_yaml", return_value={}),
                patch(
                    "ci_style_profile.bootstrap._load_presets",
                    return_value=self.PRESETS,
                ),
                patch(
                    "ci_style_profile.bootstrap._load_user_config_lenient",
                    return_value=user_config,
                ),
                patch(
                    "ci_style_profile.collectors.REGISTRY",
                    _make_mock_registry("wordpress"),
                ),
                patch(
                    "ci_style_profile.bootstrap._collect_source",
                    return_value=_MOCK_DOCS,
                ),
            ):
                rc = _run_bootstrap(
                    "--output-yaml",
                    str(Path(tmpdir) / "out.yaml"),
                    "--sources",
                    "wordpress",
                    "--style",
                    "canonical",
                    "--preset",
                    preset,
                    "--dry-run",
                )
        assert rc == 0
        return caplog.text

    def test_a_none_that_rides_into_the_presets_model_is_warned(self, caplog):
        text = self._run(
            caplog, "balanced", {"model": "claude-haiku-4-5-20251001", "effort": "none"}
        )
        assert "claude model claude-sonnet-5 is set to effort: none" in text
        assert "effort: low or medium" in text

    def test_a_preset_that_sets_the_effort_replaces_it_and_is_quiet(self, caplog):
        text = self._run(
            caplog, "maximum", {"model": "claude-sonnet-5", "effort": "none"}
        )
        assert "effort: none" not in text


class TestContinueOnError:
    def test_continue_on_error_skips_failed_source(self):
        """--continue-on-error: one collector raises CollectorError; run completes."""
        from ci_style_profile.collectors.base import CollectorError

        def _fail_collect(source, *a, **kw):
            if source == "gmail":
                raise CollectorError("gmail", "Auth failed")
            return _MOCK_DOCS

        _RECONCILE = """{
          "canonical": {"style_profile": "test", "audience_primary": "test", "banned_words": [], "banned_phrases": [], "positive_rules": [], "confidence": "high"},
          "detected_styles": {}, "synthesis_notes": ""
        }"""

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "out.yaml"
            with (
                patch("ci_style_profile.bootstrap._load_sources_yaml", return_value={}),
                patch(
                    "ci_style_profile.bootstrap._load_user_config_lenient",
                    return_value={
                        "models": {
                            "claude": {
                                "provider": "anthropic",
                                "model": "claude-sonnet-4-6",
                            }
                        },
                        "api_keys": {"claude": {"api_key": "test"}},
                    },
                ),
                patch(
                    "ci_style_profile.collectors.REGISTRY",
                    _make_mock_registry("wordpress", "gmail"),
                ),
                patch(
                    "ci_style_profile.bootstrap._collect_source",
                    side_effect=_fail_collect,
                ),
                patch(
                    "ci_style_profile.synthesize.call_all",
                    return_value={
                        "claude": {
                            "content": '{"style_profile": "t", "audience_primary": "t", "banned_words": [], "banned_phrases": [], "positive_rules": []}',
                            "failed": False,
                            "tokens": {},
                            "elapsed": 1.0,
                            "_parsed": {
                                "style_profile": "t",
                                "audience_primary": "t",
                                "banned_words": [],
                                "banned_phrases": [],
                                "positive_rules": [],
                            },
                        },
                    },
                ),
                patch(
                    "ci_style_profile.synthesize.call_one",
                    return_value={
                        "content": _RECONCILE,
                        "failed": False,
                        "tokens": {},
                        "elapsed": 1.0,
                    },
                ),
            ):
                rc = _run_bootstrap(
                    "--output-yaml",
                    str(output_path),
                    "--sources",
                    "wordpress,gmail",
                    "--style",
                    "canonical",
                    "--continue-on-error",
                    "--overwrite",
                )

            # Should complete even though gmail failed
            assert rc == 0

    def test_stop_on_error_without_flag(self):
        """Without --continue-on-error: collector error causes exit code 1."""
        from ci_style_profile.collectors.base import CollectorError

        with (
            patch("ci_style_profile.bootstrap._load_sources_yaml", return_value={}),
            patch(
                "ci_style_profile.bootstrap._load_user_config_lenient", return_value={}
            ),
            patch(
                "ci_style_profile.collectors.REGISTRY", _make_mock_registry("wordpress")
            ),
            patch(
                "ci_style_profile.bootstrap._collect_source",
                side_effect=CollectorError("wordpress", "Server error"),
            ),
        ):
            rc = _run_bootstrap(
                "--output-yaml",
                "/tmp/out.yaml",
                "--sources",
                "wordpress",
                "--style",
                "canonical",
                "--dry-run",  # dry-run but error happens at collection
            )

        assert rc == 1


class TestPublicationFlag:
    def test_publication_resolves_to_configs(self):
        """--publication mikehammett resolves to configs/mikehammett.yaml."""
        from ci_style_profile.bootstrap import _resolve_output_path

        path = _resolve_output_path("mikehammett", None)
        assert path == Path("configs") / "mikehammett.yaml"

    def test_output_yaml_explicit_path(self):
        """--output-yaml sets explicit output path."""
        from ci_style_profile.bootstrap import _resolve_output_path

        path = _resolve_output_path(None, "/tmp/my_profile.yaml")
        assert path == Path("/tmp/my_profile.yaml")


class TestRefreshFlag:
    def test_refresh_clears_watermarks(self):
        """--refresh: watermarks cleared before collection."""
        with (
            patch("ci_style_profile.bootstrap._load_sources_yaml", return_value={}),
            patch(
                "ci_style_profile.bootstrap._load_user_config_lenient", return_value={}
            ),
            patch(
                "ci_style_profile.collectors.REGISTRY", _make_mock_registry("wordpress")
            ),
            patch(
                "ci_style_profile.bootstrap._load_watermarks",
                return_value={"wordpress": "2024-01-01"},
            ),
            patch(
                "ci_style_profile.bootstrap._collect_source", return_value=_MOCK_DOCS
            ) as mock_collect,
        ):
            _run_bootstrap(
                "--output-yaml",
                "/tmp/out.yaml",
                "--sources",
                "wordpress",
                "--style",
                "canonical",
                "--dry-run",
                "--refresh",
            )

            # When --refresh, watermarks should be {} (cleared) so _collect_source is called
            assert mock_collect.called
            call_kwargs = mock_collect.call_args
            # watermarks kwarg should be {} (cleared by --refresh)
            watermarks_arg = call_kwargs.kwargs.get(
                "watermarks", call_kwargs.args[4] if len(call_kwargs.args) > 4 else None
            )
            if watermarks_arg is not None:
                assert watermarks_arg == {}


class TestCheckDraftNotImplemented:
    def test_check_draft_raises_not_implemented(self):
        """--check-draft raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            _run_bootstrap("--publication", "test", "--check-draft", "/tmp/draft.md")
