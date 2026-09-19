"""Tests for the --raw-draft / --metadata CLI wiring in pipeline.main().

Added alongside commit 803440c but previously untested. Follows the
patch-and-assert pattern used in test_webpage.py's TestUrlModeFlowsIntoReview.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.usefixtures("tmp_history_root")
class TestRawDraftArgparse:
    def test_raw_draft_mutually_exclusive_with_draft(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--draft",
            "handoff.md",
            "--raw-draft",
            "draft.md",
            "--publication",
            "myblog",
        ]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit):
                pipeline.main()

    def test_raw_draft_mutually_exclusive_with_publish(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--publish",
            "handoff.md",
            "--raw-draft",
            "draft.md",
            "--publication",
            "myblog",
        ]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit):
                pipeline.main()

    def test_metadata_without_raw_draft_errors(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--metadata",
            "meta.md",
            "--publication",
            "myblog",
        ]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit):
                pipeline.main()


@pytest.mark.usefixtures("tmp_history_root")
class TestRetryFailedArgparse:
    def test_retry_failed_mutually_exclusive_with_replay(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--draft",
            "handoff.md",
            "--publication",
            "myblog",
            "--replay",
            "run_1_results.json",
            "--retry-failed",
            "run_1_results.json",
        ]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit):
                pipeline.main()

    def test_retry_failed_rejected_with_publish(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--publish",
            "handoff.md",
            "--publication",
            "myblog",
            "--retry-failed",
            "run_1_results.json",
        ]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit):
                pipeline.main()


@pytest.mark.usefixtures("tmp_history_root")
class TestRetryFailedFlowsIntoReview:
    """--retry-failed must reach run_draft_pipeline alongside normal draft-loading."""

    def test_retry_failed_path_reaches_run_draft_pipeline(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--draft",
            "handoff.md",
            "--publication",
            "myblog",
            "--retry-failed",
            "run_1_results.json",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["retry_failed_results"] == "run_1_results.json"
        # Normal draft loading is unaffected — the handoff path still flows through.
        assert mock_run.call_args.args[0] == "handoff.md"

    def test_absent_flag_defaults_to_none(self):
        import ci_article_review.pipeline as pipeline

        argv = ["pipeline.py", "--draft", "handoff.md", "--publication", "myblog"]
        with (
            patch.object(sys, "argv", argv),
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()

        assert mock_run.call_args.kwargs["retry_failed_results"] is None


@pytest.mark.usefixtures("tmp_history_root")
class TestRawDraftModeFlowsIntoReview:
    """--raw-draft alone must build a raw-text handoff and reach run_draft_pipeline."""

    def test_raw_draft_alone_builds_handoff(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--raw-draft",
            "draft.md",
            "--publication",
            "myblog",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch(
                "ci_article_review.pipeline._read_handoff_file",
                return_value="# My Draft Title\n\nSome article body text.",
            ) as mock_read,
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()

        mock_read.assert_called_once_with("draft.md")
        mock_run.assert_called_once()
        handoff = mock_run.call_args.kwargs["handoff"]
        assert handoff["title"] == "My Draft Title"
        assert "Some article body text." in handoff["draft"]
        assert handoff["run_number"] == 1
        # File-path argument is None — the pipeline uses the pre-built handoff.
        assert mock_run.call_args.args[0] is None

    def test_raw_draft_falls_back_to_filename_stem_for_title(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--raw-draft",
            "/tmp/my-cool-draft.md",
            "--publication",
            "myblog",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch(
                "ci_article_review.pipeline._read_handoff_file",
                return_value="Body text with no H1 heading.",
            ),
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()

        handoff = mock_run.call_args.kwargs["handoff"]
        assert handoff["title"] == "my-cool-draft"


@pytest.mark.usefixtures("tmp_history_root")
class TestRawDraftWithMetadataFlowsIntoReview:
    """--raw-draft + --metadata must combine both files into one handoff."""

    def test_combines_draft_and_metadata_files(self):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--raw-draft",
            "draft.md",
            "--metadata",
            "meta.md",
            "--publication",
            "myblog",
        ]
        draft_text = "Plain article body, no headers."
        metadata_text = (
            "Article: Metadata-Supplied Title\n\n"
            "PRIMARY CLAIM\nThe claim from metadata.\n\n"
            "TARGET AUDIENCE\nEveryone.\n"
        )

        def fake_read(path):
            return {"draft.md": draft_text, "meta.md": metadata_text}[path]

        with (
            patch.object(sys, "argv", argv),
            patch(
                "ci_article_review.pipeline._read_handoff_file",
                side_effect=fake_read,
            ) as mock_read,
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()

        assert mock_read.call_count == 2
        handoff = mock_run.call_args.kwargs["handoff"]
        assert handoff["title"] == "Metadata-Supplied Title"
        assert handoff["draft"] == draft_text
        assert handoff["primary_claim"] == "The claim from metadata."
        assert handoff["target_audience"] == "Everyone."
        assert mock_run.call_args.args[0] is None


class TestBuildUserPromptForwardsMetadataFields:
    """_build_user_prompt must include target_audience/sources_cited/uncertain_sections/known_gaps
    when present, rather than silently dropping them (fixed alongside 803440c)."""

    def test_all_optional_fields_forwarded(self):
        from ci_article_review.pipeline import _build_user_prompt

        handoff = {
            "title": "A Title",
            "target_audience": "Municipal officials.",
            "primary_claim": "The claim.",
            "sources_cited": "FCC broadband map.",
            "uncertain_sections": "Paragraph 4 figures.",
            "known_gaps": "No satellite discussion.",
        }
        prompt = _build_user_prompt("draft body", handoff)
        assert "TARGET AUDIENCE: Municipal officials." in prompt
        assert "SOURCES ALREADY CITED:\nFCC broadband map." in prompt
        assert "UNCERTAIN SECTIONS" in prompt and "Paragraph 4 figures." in prompt
        assert "KNOWN GAPS" in prompt and "No satellite discussion." in prompt

    def test_missing_optional_fields_omitted(self):
        from ci_article_review.pipeline import _build_user_prompt

        handoff = {"title": "A Title"}
        prompt = _build_user_prompt("draft body", handoff)
        assert "TARGET AUDIENCE" not in prompt
        assert "SOURCES ALREADY CITED" not in prompt
        assert "UNCERTAIN SECTIONS" not in prompt
        assert "KNOWN GAPS" not in prompt


@pytest.mark.usefixtures("tmp_history_root")
class TestNoSeoSuggestionsFlag:
    """--no-seo-suggestions must reach run_draft_pipeline; its absence must not."""

    def _run_main(self, extra_argv):
        import ci_article_review.pipeline as pipeline

        argv = [
            "pipeline.py",
            "--draft",
            "handoff.md",
            "--publication",
            "myblog",
            *extra_argv,
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()
        return mock_run

    def test_flag_disables_the_pass(self):
        mock_run = self._run_main(["--no-seo-suggestions"])
        assert mock_run.call_args.kwargs["seo_suggestions"] is False

    def test_absent_flag_defers_to_the_publication_config(self):
        # None, not True — the config decides when the CLI says nothing.
        mock_run = self._run_main([])
        assert mock_run.call_args.kwargs["seo_suggestions"] is None


class TestSeoSuggestionPassReachedFromDraftRun:
    """The pass has to actually be invoked by run_draft_pipeline, with the
    material the pipeline already has, and the CLI override has to reach it."""

    _CURRENCY = {
        "warnings": [],
        "registry_warning": False,
        "registry_stale": False,
        "registry_date": "",
        "registry_age_days": 0,
    }
    _HANDOFF = {
        "title": "A Title That Is Comfortably Long Enough",
        "draft": "# A Title That Is Comfortably Long Enough\n\n## Section\n\nBody.",
        "primary_claim": "The claim.",
        "run_number": 1,
    }

    def _run(self, seo_suggestions=None, seo_rules=None):
        """Drive a draft run up to the point it exits for having no models.

        The suggestion pass runs during pre-analysis, well before that exit, so
        this reaches it without standing up an ensemble. Assignments are forced
        empty rather than left to fall out of the config: an api_keys entry is
        needed to prove the key reaches the pass, and that same entry is enough
        for _build_assignments to schedule real provider calls.
        """
        from contextlib import ExitStack

        import ci_article_review.pipeline as pipeline

        config = {
            "api_keys": {"mistral": {"api_key": "k"}},
            # link_validation off so pre-analysis makes no network call.
            "pipeline": {"link_validation": False, "grammar_pass": False},
            "publication": {"seo_rules": seo_rules} if seo_rules else {},
            "delta": {},
            "ensemble": {},
            "models": {},
        }
        with ExitStack() as stack:
            for target, kwargs in (
                ("load_user_config", {"return_value": {"pipeline": {}}}),
                ("load_publication_config", {"return_value": {}}),
                ("merge_configs", {"return_value": config}),
                ("check_model_currency", {"return_value": self._CURRENCY}),
                ("_build_assignments", {"return_value": []}),
                ("_build_custom_assignments", {"return_value": ([], {})}),
            ):
                stack.enter_context(
                    patch(f"ci_article_review.pipeline.{target}", **kwargs)
                )
            mock_generate = stack.enter_context(
                patch(
                    "ci_article_review.pipeline.seo_suggest.generate",
                    return_value=({"status": "skipped", "reason": "test"}, None),
                )
            )
            # The content-review pass runs right behind the suggestion pass and
            # is not what these tests are about. Left real, it took the
            # api_keys entry above at its word and made a live Mistral call on
            # every run — 10s of retry and backoff against a key that reads
            # "k". `test_content_review_is_invoked_too` already stubs it the
            # same way and is the test that asserts on it.
            stack.enter_context(
                patch(
                    "ci_article_review.pipeline.seo_content.review",
                    return_value=({"status": "ok", "findings": []}, None),
                )
            )
            with pytest.raises(SystemExit):
                pipeline.run_draft_pipeline(
                    None,
                    "myblog",
                    handoff=dict(self._HANDOFF),
                    seo_suggestions=seo_suggestions,
                )
        return mock_generate

    def test_content_review_is_invoked_too(self):
        from contextlib import ExitStack

        import ci_article_review.pipeline as pipeline

        config = {
            "api_keys": {"mistral": {"api_key": "k"}},
            "pipeline": {"link_validation": False, "grammar_pass": False},
            "publication": {},
            "delta": {},
            "ensemble": {},
            "models": {},
        }
        with ExitStack() as stack:
            for target, kwargs in (
                ("load_user_config", {"return_value": {"pipeline": {}}}),
                ("load_publication_config", {"return_value": {}}),
                ("merge_configs", {"return_value": config}),
                ("check_model_currency", {"return_value": self._CURRENCY}),
                ("_build_assignments", {"return_value": []}),
                ("_build_custom_assignments", {"return_value": ([], {})}),
            ):
                stack.enter_context(
                    patch(f"ci_article_review.pipeline.{target}", **kwargs)
                )
            suggestions = {"status": "ok", "keyword_candidates": [], "fields": {}}
            stack.enter_context(
                patch(
                    "ci_article_review.pipeline.seo_suggest.generate",
                    return_value=(suggestions, None),
                )
            )
            mock_review = stack.enter_context(
                patch(
                    "ci_article_review.pipeline.seo_content.review",
                    return_value=({"status": "ok", "findings": []}, None),
                )
            )
            with pytest.raises(SystemExit):
                pipeline.run_draft_pipeline(None, "myblog", handoff=dict(self._HANDOFF))

        mock_review.assert_called_once()
        # The suggestion output feeds it the search intent to judge against.
        assert mock_review.call_args.kwargs["suggestions"] is suggestions

    def test_cli_override_disables_both_seo_calls(self):
        # --no-seo-suggestions is about not paying for the SEO extras, not
        # about one of the two.
        kwargs = self._run(seo_suggestions=False).call_args.kwargs
        assert kwargs["pub_config"]["seo_rules"]["suggestions"] is False
        assert kwargs["pub_config"]["seo_rules"]["content_review"] is False

    def test_pass_is_invoked_with_the_pipeline_context(self):
        mock_generate = self._run()
        mock_generate.assert_called_once()

        kwargs = mock_generate.call_args.kwargs
        assert mock_generate.call_args.args[0] == self._HANDOFF["draft"]
        assert kwargs["handoff"]["primary_claim"] == "The claim."
        assert kwargs["api_keys"] == {"mistral": {"api_key": "k"}}
        # The SEO analysis result goes along, so the pass knows whether to ask
        # for an OG title.
        assert kwargs["seo_result"]["title"] == self._HANDOFF["title"]

    def test_cli_override_disables_it_through_the_config(self):
        kwargs = self._run(seo_suggestions=False).call_args.kwargs
        assert kwargs["pub_config"]["seo_rules"]["suggestions"] is False

    def test_cli_override_preserves_other_seo_rules(self):
        kwargs = self._run(
            seo_suggestions=False, seo_rules={"title_max_chars": 55}
        ).call_args.kwargs
        assert kwargs["pub_config"]["seo_rules"]["title_max_chars"] == 55
        assert kwargs["pub_config"]["seo_rules"]["suggestions"] is False

    def test_bare_seo_rules_key_does_not_crash_the_override(self):
        # `seo_rules:` with nothing under it parses as None, not {}.
        kwargs = self._run(seo_suggestions=False, seo_rules=None).call_args.kwargs
        assert kwargs["pub_config"]["seo_rules"]["suggestions"] is False


class TestSeoSuggestionsAtPublishTime:
    """Publish mode offers suggestions only for fields the handoff left empty,
    and never applies them to the post."""

    _SUGGESTIONS = {
        "status": "ok",
        "keyword_candidates": [{"keyword": "a phrase", "rationale": "why"}],
        "fields": {
            "meta_description": {
                "label": "Meta description",
                "value": "A drafted description.",
                "rationale": "",
                "chars": 22,
                "limit": 155,
                "over_limit": False,
                "default_note": "",
            }
        },
    }

    def _handoff(self, seo):
        return {
            "title": "A Title",
            "seo": seo,
            "final_draft": "# A Title\n\nBody text.",
            "publication_parameters": {},
        }

    def _run(self, seo, suggestions=None):
        from ci_article_review.pipeline import _suggest_seo_for_publish

        with patch(
            "ci_article_review.pipeline.seo_suggest.generate",
            return_value=(suggestions or self._SUGGESTIONS, None),
        ) as mock_generate:
            _suggest_seo_for_publish(self._handoff(seo), {}, {})
        return mock_generate

    def test_no_call_when_the_handoff_supplied_both_fields(self, capsys):
        mock_generate = self._run(
            {"focus_keyword": "chosen", "meta_description": "written"}
        )
        mock_generate.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_suggests_when_the_meta_description_is_missing(self, capsys):
        self._run({"focus_keyword": "chosen"})
        out = capsys.readouterr().out

        assert "no meta description" in out
        assert "focus keyword" not in out.split("Suggestions follow")[0]
        assert "A drafted description." in out

    def test_placeholder_only_handoff_gets_both(self, capsys):
        # _parse_seo_block drops "derive from ..." placeholders, so a handoff
        # left on the template defaults arrives here with nothing at all.
        self._run({})
        out = capsys.readouterr().out
        assert "no focus keyword and no meta description" in out

    def test_says_plainly_that_nothing_is_applied(self, capsys):
        self._run({})
        assert "NOT applied to the post" in capsys.readouterr().out

    def test_unavailable_suggestions_print_nothing_extra(self, capsys):
        self._run({}, suggestions={"status": "failed", "reason": "call failed"})
        # Publish mode is a confirmation prompt, not a report — a failed
        # backstop call stays out of the way rather than adding noise before
        # the checklist.
        assert capsys.readouterr().out == ""

    def test_analysis_runs_in_publish_mode(self):
        mock_generate = self._run({})
        seo_result = mock_generate.call_args.kwargs["seo_result"]
        assert seo_result["mode"] == "publish"


class TestPublishReportsAnUnappliedSchemaType:
    """A leftover `Schema type:` line is reported next to the post, not dropped.

    The template no longer offers the line, because nothing sets schema from a
    handoff. Older copies of it do, and so does at least one real handoff. Run
    through the real publish path with only the network mocked, so the check
    is on what actually leaves the process.
    """

    _HANDOFF = (
        "PUBLICATION HANDOFF\n"
        "Article: About\n"
        "Publication: testpub\n\n"
        "PUBLICATION PARAMETERS\n"
        "Status: draft\n"
        "Post type: page\n\n"
        "SEO METADATA\n"
        "Focus keyword: mike hammett\n"
        "Schema type: other — AboutPage\n\n"
        "FINAL DRAFT\n"
        "# About\n\nBody.\n"
    )

    def _publish(self, tmp_path, handoff_text):
        from ci_article_review.pipeline import run_publish_pipeline

        path = tmp_path / "about-publication.md"
        path.write_text(handoff_text, encoding="utf-8")
        config = {
            "publication": {
                "wordpress": {
                    "site_url": "https://example.com",
                    "username": "editor",
                    "application_password": "pass word here",
                },
                "rank_math": {"auto_set_og_tags": True},
            },
            "api_keys": {},
        }
        created = MagicMock()
        created.json.return_value = {"id": 7, "link": "https://example.com/about/"}
        wp_module = "ci_article_review.adapters.cms.wordpress"
        with (
            patch("ci_article_review.pipeline.load_user_config", return_value={}),
            patch(
                "ci_article_review.pipeline.load_publication_config", return_value={}
            ),
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            patch(f"{wp_module}.print_checklist_and_confirm", return_value=True),
            patch(
                f"{wp_module}.requests.post", side_effect=[created, MagicMock()]
            ) as mock_post,
        ):
            run_publish_pipeline(str(path), "testpub", seo_suggestions=False)
        return [c.kwargs["json"] for c in mock_post.call_args_list]

    def test_the_author_is_told_it_was_not_applied(self, tmp_path, capsys):
        sent = self._publish(tmp_path, self._HANDOFF)
        out = capsys.readouterr().out

        assert "WordPress push successful." in out
        assert "(other — AboutPage) was NOT applied" in out
        assert "Schema tab" in out
        # The page and its Rank Math fields both went out; the schema did not.
        assert len(sent) == 2
        assert sent[1]["meta"]["rank_math_focus_keyword"] == "mike hammett"
        assert "AboutPage" not in repr(sent)

    def test_no_note_without_the_line(self, tmp_path, capsys):
        self._publish(
            tmp_path, self._HANDOFF.replace("Schema type: other — AboutPage\n", "")
        )
        out = capsys.readouterr().out
        assert "WordPress push successful." in out
        assert "schema" not in out.lower()


class TestPublishRefusesAnUntitledPost:
    """No title, no post: the handoff is refused before anything is sent.

    A blank ``Article:`` created an untitled WordPress post without a word, and
    one left on publication.md's ``[title]`` published that as the title. The
    push also drops the draft's leading "# " heading, because the theme renders
    the title itself, so an untitled post carried no title anywhere.
    """

    _HANDOFF = (
        "PUBLICATION HANDOFF\n"
        "{article}"
        "Publication: testpub\n\n"
        "PUBLICATION PARAMETERS\n"
        "Status: draft\n"
        "Post type: page\n\n"
        "SEO METADATA\n"
        "Focus keyword: mike hammett\n\n"
        "FINAL DRAFT\n"
        "# About\n\nBody.\n"
    )

    def _publish(self, tmp_path, article_line):
        """Exit code (None if it ran to the end), and every outward step."""
        from ci_article_review.pipeline import run_publish_pipeline

        path = tmp_path / "about-publication.md"
        path.write_text(self._HANDOFF.format(article=article_line), encoding="utf-8")
        config = {
            "publication": {
                "wordpress": {
                    "site_url": "https://example.com",
                    "username": "editor",
                    "application_password": "pass word here",
                },
                "rank_math": {"auto_set_og_tags": True},
            },
            "api_keys": {},
        }
        wp_module = "ci_article_review.adapters.cms.wordpress"
        with (
            patch("ci_article_review.pipeline.load_user_config", return_value={}),
            patch(
                "ci_article_review.pipeline.load_publication_config", return_value={}
            ),
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            patch("ci_article_review.pipeline._suggest_seo_for_publish") as suggest,
            patch(
                f"{wp_module}.print_checklist_and_confirm", return_value=True
            ) as confirm,
            patch(f"{wp_module}.requests.post") as post,
            patch(f"{wp_module}.requests.get") as get,
        ):
            try:
                run_publish_pipeline(str(path), "testpub")
            except SystemExit as e:
                code = e.code
            else:
                code = None
        return code, {"suggest": suggest, "confirm": confirm, "post": post, "get": get}

    @pytest.mark.parametrize(
        "article_line",
        ["Article: [title]\n", "Article:\n", ""],
        ids=["placeholder", "blank", "absent"],
    )
    def test_nothing_is_sent_without_a_title(self, tmp_path, caplog, article_line):
        """Refused before the SEO suggestion call is paid for, and before the
        checklist asks for a yes that would come to nothing."""
        with caplog.at_level("ERROR"):
            code, steps = self._publish(tmp_path, article_line)
        assert code == 1
        assert [name for name, mock in steps.items() if mock.called] == []
        assert "has no title" in caplog.text

    def test_the_refusal_offers_the_draft_heading(self, tmp_path, caplog):
        """The heading the push would have dropped is the likely title, so
        the refusal gives it as the line to paste."""
        with caplog.at_level("ERROR"):
            self._publish(tmp_path, "Article: [title]\n")
        assert "the line is: Article: About" in caplog.text

    def test_a_titled_handoff_still_publishes(self, tmp_path):
        """The control: the same handoff with its title goes out, so the
        refusal above is the title check and not the harness."""
        code, steps = self._publish(tmp_path, "Article: About\n")
        assert code is None
        assert steps["post"].call_args_list[0].kwargs["json"]["title"] == "About"


class TestSeoSuggestionConsoleOutput:
    """The suggestion has to reach the terminal, next to the SEO issues it answers."""

    _SUGGESTIONS = {
        "status": "ok",
        "model": "mistral-small-latest",
        "keyword_candidates": [
            {"keyword": "interconnection queue", "rationale": "what officials search"}
        ],
        "fields": {
            "meta_description": {
                "label": "Meta description",
                "value": "Queues, not generation, decide the timeline.",
                "rationale": "",
                "chars": 44,
                "limit": 155,
                "over_limit": False,
                "default_note": "",
            },
            "og_title": {
                "label": "OG title",
                "value": "",
                "rationale": "",
                "chars": None,
                "limit": None,
                "over_limit": False,
                "default_note": "The article title is used as-is.",
            },
            "og_description": {
                "label": "OG description",
                "value": "",
                "rationale": "",
                "chars": None,
                "limit": None,
                "over_limit": False,
                "default_note": "The meta description is used.",
            },
            "schema_type": {
                "label": "Schema type",
                "value": "NewsArticle",
                "rationale": "reporting tied to a pending vote",
                "chars": 11,
                "limit": None,
                "over_limit": False,
                "default_note": "",
                "recognized": True,
                "configured_default": "BlogPosting",
                "differs_from_default": True,
            },
        },
    }

    def _summary(self, suggestions):
        from ci_article_review.analysis import seo as seo_analysis
        from ci_article_review.pipeline import _print_draft_summary

        seo = seo_analysis.analyze(
            "# A Title That Is Comfortably Long Enough\n\n" + " ".join(["word"] * 400),
            {"title": "A Title That Is Comfortably Long Enough", "seo": {}},
        )
        seo_analysis.apply_suggestions(seo, suggestions)
        report = {
            "article_title": "A Title That Is Comfortably Long Enough",
            "run_number": 1,
            "generated": "2026-08-09T00:00:00+00:00",
            "section_1_consensus": [],
            "section_2_fact_check": {},
            "section_3_voice": [],
            "section_4_argument": [],
            "section_5_completeness": [],
            "section_6_red_team": {},
            "section_7_low_confidence": [],
            "lt_corrections_applied": [],
            "pre_analysis": {"seo": seo},
        }
        return _print_draft_summary(report, {}) or None

    def test_draft_mode_meta_warning_is_not_bare_and_unactionable(self, capsys):
        self._summary(self._SUGGESTIONS)
        out = capsys.readouterr().out

        # The old text told the author to fill in a section their draft
        # template does not have.
        assert "No meta description in handoff SEO METADATA section" not in out
        assert "[no_meta_description]" in out
        # Paired with a concrete draft to edit, right below it.
        assert "SEO suggestions" in out
        assert "Queues, not generation, decide the timeline." in out
        assert "44/155 chars" in out
        assert "interconnection queue" in out

    def test_every_metadata_field_reaches_the_console(self, capsys):
        self._summary(self._SUGGESTIONS)
        out = capsys.readouterr().out

        # Proposed values and applied defaults both report.
        assert "Meta description" in out
        assert "OG title: The article title is used as-is." in out
        assert "OG description: The meta description is used." in out
        assert "Schema type" in out and "NewsArticle" in out
        assert "reporting tied to a pending vote" in out
        assert "Differs from the configured default: BlogPosting" in out
        assert "Schema tab" in out

    def test_unavailable_suggestions_still_leave_a_finding_that_makes_sense(
        self, capsys
    ):
        self._summary({"status": "skipped", "reason": "no mistral API key configured"})
        out = capsys.readouterr().out

        assert "No meta description in handoff SEO METADATA section" not in out
        assert "Template C" in out
        assert "no mistral API key configured" in out


class TestReportedDirectoryMatchesWhatWasWritten:
    """The summary must name the directory the run actually saved into.

    It rebuilt the path by re-slugging the article title while the save uses the
    history key, so a handoff with a `History key:` line printed a directory that
    does not exist. Every existing test used a handoff without one, where the two
    derivations agree and the bug is invisible — this one pins the case where
    they diverge.
    """

    _REPORT = {
        "article_title": "Data Centers Don't Have an Environmental Record. Twelve.",
        "run_number": 16,
        "generated": "2026-08-15T00:00:00+00:00",
        "section_1_consensus": [],
        "section_2_fact_check": {},
        "section_3_voice": [],
        "section_4_argument": [],
        "section_5_completeness": [],
        "section_6_red_team": {},
        "section_7_low_confidence": [],
        "lt_corrections_applied": [],
    }

    def _run(self, markdown_path, capsys):
        from ci_article_review.pipeline import _print_draft_summary

        _print_draft_summary(dict(self._REPORT), {}, markdown_path=markdown_path)
        return capsys.readouterr().out

    def test_history_key_directory_is_reported_not_the_title_slug(self, capsys):
        out = self._run("pipeline_history/dc-environment/run_16_x_review.md", capsys)
        assert "dc-environment" in out
        assert "data-centers-dont-have" not in out

    def test_falls_back_to_the_title_slug_when_nothing_was_written(self, capsys):
        """No markdown path (e.g. the save failed) still prints a useful guess."""
        out = self._run(None, capsys)
        assert "data-centers-dont-have" in out


class TestTheHardExitStaysOutOfMain:
    """`os._exit` must sit in `cli()`, never in `main()`.

    `main()` is called in-process by this suite. An unconditional hard exit
    inside it killed pytest 36% of the way through a run, and because
    `os._exit(0)` sets a success code the truncated run reported green — a
    suite that stops early while claiming to pass being considerably worse
    than the shutdown hang the exit exists to fix.
    """

    def _source(self, name):
        import inspect

        from ci_article_review import pipeline

        return inspect.getsource(getattr(pipeline, name))

    def test_main_does_not_hard_exit(self):
        body = self._source("main")
        assert "exit_without_waiting_for_foreign_threads" not in body
        assert "os._exit" not in body

    def test_cli_does(self):
        assert "exit_without_waiting_for_foreign_threads" in self._source("cli")

    @pytest.mark.usefixtures("tmp_history_root")
    def test_main_returns_rather_than_ending_the_process(self, monkeypatch):
        """A bad invocation must raise SystemExit for a caller to handle, not
        take the interpreter with it."""
        import pytest

        from ci_article_review import pipeline

        monkeypatch.setattr("sys.argv", ["ci-review"])
        with pytest.raises(SystemExit):
            pipeline.main()

    def test_the_console_script_points_at_cli(self):
        from pathlib import Path

        import tomllib

        from ci_article_review import pipeline

        root = Path(pipeline.__file__).parents[2] / "pyproject.toml"
        scripts = tomllib.loads(root.read_text(encoding="utf-8"))["project"]["scripts"]
        assert scripts["ci-review"].endswith(":cli"), (
            "ci-review must enter through cli(), or a real run gets main()'s "
            "normal shutdown and can hang on a foreign thread pool"
        )


class TestReviewContextReachesTheModels:
    """What the pipeline measured before the ensemble, given to the ensemble.

    Link validation, readability and the prior run were all computed and then
    withheld. Six models were asked to fact-check claims whose sources this
    process already knew were dead, and run 20 of an article opened exactly as
    cold as run 1 — the prior report was not even loaded until after Pass 2 had
    finished.
    """

    _PRE = {
        "links": [
            {"url": "https://ok.example/", "ok": True},
            {"url": "https://gone.example/", "ok": False, "status": "404"},
        ],
        "readability": {
            "word_count": 385,
            "flesch_kincaid_grade": 6.4,
            "reading_level": "Fairly Easy",
            "avg_sentence_length": 12.0,
            "longest_paragraph_words": 111,
        },
    }
    _PRIOR = {
        "run_number": 19,
        "section_1_consensus": [
            {"passage": "A flagged passage.", "models": ["a:red_team", "b:red_team"]}
        ],
    }

    def _ctx(self, pre=None, prior=None):
        from ci_article_review.pipeline import _build_review_context

        return _build_review_context(pre if pre is not None else self._PRE, prior)

    def test_dead_links_are_named(self):
        ctx = self._ctx()
        assert "https://gone.example/" in ctx
        assert "404" in ctx

    def test_working_links_are_not_listed(self):
        assert "https://ok.example/" not in self._ctx()

    def test_readability_is_included(self):
        ctx = self._ctx()
        assert "Flesch-Kincaid grade 6.4" in ctx
        assert "longest paragraph 111 words" in ctx

    def test_prior_consensus_is_replayed_with_its_model_count(self):
        ctx = self._ctx(prior=self._PRIOR)
        assert "run 19" in ctx
        assert "A flagged passage." in ctx
        assert "flagged by 2 model(s)" in ctx

    def test_it_is_labelled_as_measurement_not_as_a_verdict(self):
        """A model treating these as another reviewer's findings would be
        double-counting them into consensus."""
        assert "measured by this pipeline" in self._ctx()

    def test_nothing_measured_yields_nothing(self):
        """No empty scaffolding on a run with links off and no prior."""
        assert self._ctx(pre={}, prior=None) == ""

    def test_the_prior_findings_list_is_capped(self):
        from ci_article_review.pipeline import _PRIOR_FINDINGS_SHOWN

        prior = {
            "run_number": 2,
            "section_1_consensus": [
                {"passage": f"passage {i}", "models": ["a:x"]} for i in range(40)
            ],
        }
        ctx = self._ctx(prior=prior)
        assert ctx.count("flagged by") == _PRIOR_FINDINGS_SHOWN

    def test_it_lands_before_the_draft_in_the_prompt(self):
        """After the metadata, before the article — so the task is framed
        before the model starts reading."""
        from ci_article_review.pipeline import _build_user_prompt

        prompt = _build_user_prompt("BODY", {"title": "T"}, self._ctx())
        assert prompt.index("PIPELINE OBSERVATIONS") < prompt.index("BODY")

    def test_no_context_leaves_the_prompt_unchanged(self):
        from ci_article_review.pipeline import _build_user_prompt

        assert "PIPELINE OBSERVATIONS" not in _build_user_prompt("BODY", {"title": "T"})

    def test_links_that_were_never_checked_add_nothing(self):
        """None is not a list of dead links, and not an error either."""
        pre = {**self._PRE, "links": None, "links_skipped_reason": "offline"}
        ctx = self._ctx(pre=pre)
        assert "Link check" not in ctx
        assert "Flesch-Kincaid grade 6.4" in ctx


class TestTheSummarySaysWhenLinksWereNotChecked:
    """A run that skipped link validation printed nothing about links, which is
    also what a draft with no links prints."""

    _REPORT = {
        "article_title": "A Title",
        "run_number": 1,
        "generated": "2026-09-18T00:00:00+00:00",
        "section_1_consensus": [],
        "section_2_fact_check": {},
        "section_3_voice": [],
        "section_4_argument": [],
        "section_5_completeness": [],
        "section_6_red_team": {},
        "section_7_low_confidence": [],
        "lt_corrections_applied": [],
    }

    def _out(self, pre, capsys):
        from ci_article_review.pipeline import _print_draft_summary

        _print_draft_summary({**self._REPORT, "pre_analysis": pre}, {})
        return capsys.readouterr().out

    def test_offline(self, capsys):
        out = self._out({"links": None, "links_skipped_reason": "offline"}, capsys)
        assert "Links: not checked (--offline)" in out

    def test_switched_off_in_config(self, capsys):
        out = self._out({"links": None, "links_skipped_reason": "disabled"}, capsys)
        assert (
            "Links: not checked (link_validation is set to false in the pipeline "
            "config)" in out
        )

    def test_a_checked_draft_with_no_links_is_not_called_unchecked(self, capsys):
        assert "Links:" not in self._out({"links": []}, capsys)

    def test_checked_links_still_print_as_before(self, capsys):
        links = [
            {"url": "https://ok.example/", "ok": True, "status_code": 200},
            {"url": "https://gone.example/", "ok": False, "status_code": 404},
        ]
        out = self._out({"links": links}, capsys)
        assert "Links: 2 found, 1 broken/error" in out
        assert "not checked" not in out
