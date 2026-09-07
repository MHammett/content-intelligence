"""Tests for analysis.webpage — URL fetch (SSRF-guarded), extraction, handoff synthesis.

All network access is mocked; no real HTTP requests are made.
"""

import sys
import types

from unittest.mock import patch, MagicMock

import pytest

from ci_core.http import USER_AGENT, UnsafeURLError

from ci_article_review.analysis import webpage


# A page with realistic boilerplate around the real article content.
SAMPLE_HTML = """
<html>
  <head><title>How Fiber Reaches Rural Towns</title></head>
  <body>
    <nav><a href="/">Home</a> <a href="/about">About</a></nav>
    <header>Site banner that is not content</header>
    <script>var tracking = 1;</script>
    <style>.x { color: red; }</style>
    <article>
      <h1>How Fiber Reaches Rural Towns</h1>
      <p>Rural broadband has lagged for decades because of cost per mile.</p>
      <h2>The economics</h2>
      <p>Subsidies changed the math in 2021 and again in 2024.</p>
      <h3>Who pays</h3>
      <p>Cooperatives and municipalities increasingly foot the bill.</p>
    </article>
    <aside>Related links you should ignore</aside>
    <footer>Copyright 2026 — do not extract this</footer>
  </body>
</html>
"""


class TestExtractArticle:
    def test_title_from_title_tag(self):
        title, _ = webpage.extract_article(SAMPLE_HTML)
        assert title == "How Fiber Reaches Rural Towns"

    def test_body_extracted(self):
        _, body = webpage.extract_article(SAMPLE_HTML)
        assert "Rural broadband has lagged for decades" in body
        assert "Cooperatives and municipalities" in body

    def test_boilerplate_stripped(self):
        _, body = webpage.extract_article(SAMPLE_HTML)
        assert "Home" not in body
        assert "Site banner" not in body
        assert "Related links" not in body
        assert "do not extract this" not in body
        assert "tracking" not in body
        assert "color: red" not in body

    def test_headings_preserved_as_markdown(self):
        _, body = webpage.extract_article(SAMPLE_HTML)
        assert "## The economics" in body
        assert "### Who pays" in body

    def test_title_falls_back_to_first_h1(self):
        html = "<html><body><article><h1>Just an H1</h1><p>Body text here.</p></article></body></html>"
        title, _ = webpage.extract_article(html)
        assert title == "Just an H1"


class TestTrafilaturaPaths:
    #: Long enough to clear the landslide guard for this sample page, whose
    #: heuristic body is 245 characters. On a real article trafilatura's output
    #: is comparable to the heuristic's rather than a fraction of it — measured
    #: 1.09x to 1.45x across four live pages — so a realistic stand-in must be
    #: too, or it trips a guard meant for pages that are not articles at all.
    _TRAFILATURA_BODY = "# From Trafilatura\n\nClean extracted body. " + ("word " * 30)

    def test_uses_trafilatura_when_present(self):
        fake = types.ModuleType("trafilatura")
        fake.extract = MagicMock(return_value=self._TRAFILATURA_BODY)
        with patch.dict(sys.modules, {"trafilatura": fake}):
            _, body = webpage.extract_article(SAMPLE_HTML)
        assert body == self._TRAFILATURA_BODY.strip()
        fake.extract.assert_called_once()

    def test_the_heuristic_wins_when_trafilatura_keeps_almost_nothing(self):
        """A page whose content is repeated blocks rather than one article:
        trafilatura finds a single block and calls the rest boilerplate.

        Measured 2026-09-04 on a six-person team page — 439 characters kept of
        36,717, covering one colleague — while the page had been cited for two
        claims about a different person entirely.
        """
        fake = types.ModuleType("trafilatura")
        fake.extract = MagicMock(return_value="One short block.")
        with patch.dict(sys.modules, {"trafilatura": fake}):
            _, body = webpage.extract_article(SAMPLE_HTML)
        assert "Rural broadband has lagged for decades" in body
        assert body != "One short block."

    def test_falls_back_when_trafilatura_absent(self):
        # Force `import trafilatura` to raise ImportError.
        with patch.dict(sys.modules, {"trafilatura": None}):
            _, body = webpage.extract_article(SAMPLE_HTML)
        # Built-in heuristic ran instead.
        assert "Rural broadband has lagged for decades" in body
        assert "## The economics" in body

    def test_falls_back_when_trafilatura_returns_none(self):
        fake = types.ModuleType("trafilatura")
        fake.extract = MagicMock(return_value=None)  # e.g. couldn't find main content
        with patch.dict(sys.modules, {"trafilatura": fake}):
            _, body = webpage.extract_article(SAMPLE_HTML)
        assert "Rural broadband has lagged for decades" in body


class TestFetchSsrfGuard:
    """URL mode fetches through the shared guard in ``ci_core.http``.

    The check used to live in ``analysis/links.py`` and be called separately
    here. It moved into ``safe_get`` so ``adapters/citation/`` could reach it
    without an import cycle — which is why model-supplied URLs went unguarded
    (audit finding 2) — and so that redirects are re-validated per hop rather
    than only the URL the caller passed (finding 15).
    """

    def test_a_refused_host_surfaces_as_a_valueerror(self):
        """Callers catch ValueError; UnsafeURLError is a subclass of it."""
        with patch(
            "ci_article_review.analysis.webpage.safe_get",
            side_effect=UnsafeURLError("Refusing to fetch ... (SSRF guard): x"),
        ):
            with pytest.raises(ValueError, match="SSRF guard"):
                webpage.fetch_url("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8080/internal",
            "http://127.0.0.1/",
        ],
    )
    def test_private_targets_are_rejected_by_the_real_guard(self, url):
        """No patching of the guard itself — this exercises the real predicate."""
        with pytest.raises(ValueError):
            webpage.fetch_url(url)

    def test_fetches_public_host(self):
        resp = MagicMock()
        resp.text = SAMPLE_HTML
        resp.raise_for_status = MagicMock()
        with patch(
            "ci_article_review.analysis.webpage.safe_get", return_value=resp
        ) as mock_get:
            html = webpage.fetch_url("https://example.com/post")
        assert html == SAMPLE_HTML
        assert mock_get.call_args.args[0] == "https://example.com/post"

    def test_the_platform_user_agent_is_still_sent(self):
        """safe_get defaults to DEFAULT_HEADERS, which carries the platform UA."""
        from ci_core.http import DEFAULT_HEADERS

        assert DEFAULT_HEADERS["User-Agent"] == USER_AGENT


class TestBuildHandoffFromUrl:
    def _patch_fetch(self, html):
        return patch("ci_article_review.analysis.webpage.fetch_url", return_value=html)

    def test_synthesizes_handoff_with_title_and_draft(self):
        with self._patch_fetch(SAMPLE_HTML):
            handoff = webpage.build_handoff_from_url("https://example.com/post")
        assert handoff["title"] == "How Fiber Reaches Rural Towns"
        assert "Rural broadband has lagged" in handoff["draft"]
        assert handoff["run_number"] == 1

    def test_warns_on_thin_extraction(self, caplog):
        thin = "<html><head><title>Paywall</title></head><body><article><p>Subscribe to read.</p></article></body></html>"
        with self._patch_fetch(thin):
            with caplog.at_level("WARNING"):
                handoff = webpage.build_handoff_from_url(
                    "https://example.com/paywalled"
                )
        assert handoff["title"] == "Paywall"
        assert any(
            "paywall" in r.message.lower() or "limited content" in r.message.lower()
            for r in caplog.records
        )


class TestUrlModeFlowsIntoReview:
    """The synthesized handoff must reach run_draft_pipeline via the CLI dispatch."""

    def test_main_url_mode_passes_handoff_to_pipeline(self):
        import ci_article_review.pipeline as pipeline

        resp = MagicMock()
        resp.text = SAMPLE_HTML
        resp.raise_for_status = MagicMock()

        argv = [
            "pipeline.py",
            "--url",
            "https://example.com/post",
            "--publication",
            "myblog",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("ci_article_review.analysis.webpage.safe_get", return_value=True),
            patch("ci_article_review.analysis.webpage.safe_get", return_value=resp),
            patch("ci_article_review.pipeline.logging.FileHandler"),
            patch("logging.Logger.addHandler"),
            patch("ci_article_review.pipeline.run_draft_pipeline") as mock_run,
        ):
            pipeline.main()

        mock_run.assert_called_once()
        handoff = mock_run.call_args.kwargs["handoff"]
        assert handoff["title"] == "How Fiber Reaches Rural Towns"
        assert "Rural broadband has lagged" in handoff["draft"]
        # File-path argument is None in URL mode.
        assert mock_run.call_args.args[0] is None


class TestRunDraftPipelineAcceptsHandoff:
    """run_draft_pipeline must use a pre-built handoff and never read a file."""

    _MIN_CONFIG = {
        "api_keys": {},
        "pipeline": {},
        "publication": {},
        "delta": {},
        "ensemble": {},
        "models": {},
    }
    _CURRENCY = {
        "warnings": [],
        "registry_warning": False,
        "registry_stale": False,
        "registry_date": "",
        "registry_age_days": 0,
    }

    def _patches(self):
        return [
            patch(
                "ci_article_review.pipeline._read_handoff_file",
                side_effect=AssertionError("read a file"),
            ),
            patch(
                "ci_article_review.pipeline.parse_draft_submission",
                side_effect=AssertionError("parsed a file"),
            ),
            patch(
                "ci_article_review.pipeline.load_user_config",
                return_value={"pipeline": {}},
            ),
            patch(
                "ci_article_review.pipeline.load_publication_config", return_value={}
            ),
            patch(
                "ci_article_review.pipeline.merge_configs",
                return_value=self._MIN_CONFIG,
            ),
            patch(
                "ci_article_review.pipeline.check_model_currency",
                return_value=self._CURRENCY,
            ),
        ]

    def test_prebuilt_handoff_used_without_file_read(self):
        import ci_article_review.pipeline as pipeline
        from contextlib import ExitStack

        # An empty-draft handoff makes the pipeline log + sys.exit(1) right after
        # the handoff branch. Reaching that exit (instead of the AssertionError on
        # _read_handoff_file) proves the pre-built handoff was consumed directly.
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            with pytest.raises(SystemExit):
                pipeline.run_draft_pipeline(
                    None,
                    "myblog",
                    handoff={"title": "T", "draft": "", "run_number": 1},
                )
