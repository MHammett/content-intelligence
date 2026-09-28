"""`ci-discover` — the optional third step of onboarding.

Audit finding 17, the remaining half. This module was at 0% coverage: the
README offers it as the way to find out a newer model exists "without reading
every provider's changelog", and nothing verified that it parses any provider's
response shape or draws the right conclusion from one.

What it gets wrong is quiet by nature. A date parsed as ``None`` silently turns
into "no date" and the newer-than-configured comparison stops firing — the tool
keeps printing a tidy list and simply never tells you about the upgrade it
exists to find.

All HTTP is stubbed; no network, no credentials.
"""

import datetime
from unittest.mock import MagicMock, patch

import pytest

from ci_article_review import discover


def _resp(json_data):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


class TestDateParsing:
    """Every "is this newer?" decision rests on this."""

    def test_unix_timestamp(self):
        assert discover._iso(1735689600) == datetime.date(2025, 1, 1)

    def test_iso_datetime_with_z(self):
        assert discover._iso("2026-03-04T05:06:07Z") == datetime.date(2026, 3, 4)

    def test_plain_iso_date(self):
        assert discover._iso("2026-03-04") == datetime.date(2026, 3, 4)

    @pytest.mark.parametrize("bad", [None, "", "   ", "not-a-date", {}, []])
    def test_unparseable_input_returns_none_rather_than_raising(self, bad):
        """One odd value from one provider must not abort discovery.

        The empty-string case was a real defect: strptime("", "") returns
        1900-01-01 rather than raising, so a model with no date was reported
        as dated 1900 and fed a fabricated value into the newer-than-configured
        comparison. No date is not a very old date.
        """
        assert discover._iso(bad) is None

    def test_an_absurd_timestamp_does_not_raise(self):
        assert discover._iso(10**18) is None


class TestDaysAgo:
    @pytest.mark.parametrize(
        "delta,expected",
        [(0, "today"), (5, "5d ago"), (60, "2mo ago"), (400, "1.1yr ago")],
    )
    def test_human_readable_ages(self, delta, expected):
        d = datetime.date.today() - datetime.timedelta(days=delta)
        assert discover._days_ago(d) == expected

    def test_missing_date_is_labelled_not_crashed(self):
        assert discover._days_ago(None) == "unknown date"


class TestModelRowMarkers:
    """The markers are the entire output — the wrong one is a wrong answer."""

    _OLD = datetime.date(2025, 1, 1)
    _NEW = datetime.date(2026, 6, 1)

    def test_the_configured_model_is_marked_configured(self, capsys):
        discover._print_model_row("gpt-5.5", self._OLD, "gpt-5.5", self._OLD)
        assert "configured" in capsys.readouterr().out

    def test_a_newer_model_is_flagged_as_newer(self, capsys):
        """The whole reason the command exists."""
        discover._print_model_row("gpt-6", self._NEW, "gpt-5.5", self._OLD)
        assert "newer than configured" in capsys.readouterr().out

    def test_an_older_model_is_not_flagged_as_newer(self, capsys):
        discover._print_model_row("gpt-4", self._OLD, "gpt-5.5", self._NEW)
        assert "newer than configured" not in capsys.readouterr().out

    def test_the_configured_model_is_never_also_called_newer(self, capsys):
        discover._print_model_row("gpt-5.5", self._NEW, "gpt-5.5", self._OLD)
        out = capsys.readouterr().out
        assert "configured" in out
        assert "newer than configured" not in out

    def test_a_missing_date_degrades_visibly(self, capsys):
        """Silence here is the failure mode: no date, no comparison, no notice."""
        discover._print_model_row("gpt-6", None, "gpt-5.5", self._OLD)
        assert "(no date)" in capsys.readouterr().out


class TestProviderDiscovery:
    def test_openai_lists_models_from_the_api_shape(self):
        payload = {"data": [{"id": "gpt-5.5", "created": 1735689600}]}
        with patch.object(discover.requests, "get", return_value=_resp(payload)):
            got = discover._discover_openai("k", "gpt-5.5")
        assert ("gpt-5.5", datetime.date(2025, 1, 1)) in got

    def test_mistral_lists_models_from_the_api_shape(self):
        payload = {"data": [{"id": "mistral-large-latest", "created": 1735689600}]}
        with patch.object(discover.requests, "get", return_value=_resp(payload)):
            got = discover._discover_mistral("k", "mistral-large-latest")
        assert any(m == "mistral-large-latest" for m, _ in got)

    def test_gemini_strips_the_models_prefix(self):
        """AI Studio returns "models/gemini-2.5-pro"; user.yaml holds the bare id.

        If the prefix survives, the configured-model comparison never matches
        and every Gemini model is reported as unconfigured.
        """
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.5-pro",
                    "supportedGenerationMethods": ["generateContent"],
                }
            ]
        }
        with patch.object(discover.requests, "get", return_value=_resp(payload)):
            got = discover._discover_gemini_aistudio("k", "gemini-2.5-pro")
        ids = [m for m, _ in got]
        assert "gemini-2.5-pro" in ids
        assert "models/gemini-2.5-pro" not in ids

    def test_gemini_drops_models_that_cannot_generate(self):
        """Embedding-only models would otherwise pad the list with noise."""
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.5-pro",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }
        with patch.object(discover.requests, "get", return_value=_resp(payload)):
            ids = [m for m, _ in discover._discover_gemini_aistudio("k", "x")]
        assert ids == ["gemini-2.5-pro"]

    def test_perplexity_lists_the_agent_apis_models(self):
        """Its Agent API publishes a models endpoint; Sonar never did, so this
        was a list hardcoded in June 2026 until the Sonar retirement. The shape
        is the one the live endpoint returned on 2026-09-28."""
        payload = {
            "object": "list",
            "data": [
                {"id": "perplexity/sonar", "created": 0, "owned_by": "perplexity"},
                {"id": "perplexity/kimi-k3", "created": 0, "owned_by": "perplexity"},
            ],
        }
        with patch.object(discover.requests, "get", return_value=_resp(payload)) as get:
            got = discover._discover_perplexity("k", "perplexity/sonar")
        assert get.call_args.args[0] == "https://api.perplexity.ai/v1/models"
        assert [m for m, _ in got] == ["perplexity/sonar", "perplexity/kimi-k3"]

    def test_perplexity_created_zero_is_no_date(self):
        """Perplexity reports every model as created 0, which is not 1970."""
        payload = {"data": [{"id": "perplexity/sonar", "created": 0}]}
        with patch.object(discover.requests, "get", return_value=_resp(payload)):
            got = discover._discover_perplexity("k", "perplexity/sonar")
        assert got == [("perplexity/sonar", None)]

    def test_every_registered_provider_is_callable(self):
        for name, (fn, label) in discover._PROVIDERS.items():
            assert callable(fn), f"{name} discovery entry is not callable"
            assert label, f"{name} has no display label"


class TestMainExitBehaviour:
    def test_a_missing_config_exits_with_a_readable_message(self, capsys):
        with (
            patch("sys.argv", ["ci-discover"]),
            patch.object(
                discover,
                "load_user_config",
                side_effect=FileNotFoundError("configs/user.yaml not found"),
            ),
        ):
            with pytest.raises(SystemExit):
                discover.main()
        out = capsys.readouterr().out
        assert "Config error" in out and "user.yaml" in out

    def test_provider_filter_restricts_what_runs(self):
        cfg = {
            "api_keys": {"openai": {"api_key": "k"}, "mistral": {"api_key": "k"}},
            "models": {"openai": "gpt-5.5", "mistral": "mistral-large-latest"},
            "pipeline": {},
        }
        called = []

        def _spy(name):
            def _fn(api_key, configured_id):
                called.append(name)

            return _fn

        providers = {
            "openai": (_spy("openai"), "OpenAI"),
            "mistral": (_spy("mistral"), "Mistral"),
        }
        # main() also saves the sweep for the pipeline to read; conftest.py's
        # autouse fixture keeps that out of the working tree.
        with (
            patch("sys.argv", ["ci-discover", "--provider", "openai"]),
            patch.object(discover, "load_user_config", return_value=cfg),
            patch.object(discover, "_PROVIDERS", providers),
        ):
            try:
                discover.main()
            except SystemExit:
                pass
        assert called == ["openai"], (
            f"--provider filter did not scope the run: {called}"
        )
