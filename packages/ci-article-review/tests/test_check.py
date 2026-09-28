"""`ci-check` — the second command in the documented onboarding.

Audit finding 17, the half I missed the first time. The finding named three
modules a new user runs before anything else; `setup.py` got covered in PR #66
and this one did not, staying at 25%.

It matters more than its size suggests: `ci-check` is what tells someone their
API keys work, and its *error text* is the entire diagnostic. A check that fails
silently, or reports a credential problem as a network problem, sends a new user
looking in the wrong place — and leaks a key into the terminal if redaction
breaks.

All HTTP is stubbed; no network, no credentials.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest
import requests

from ci_article_review import check as check_mod


def _resp(status=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_data if json_data is not None else {}

    def _raise():
        if status >= 400:
            err = requests.HTTPError(f"{status} Error")
            err.response = r
            raise err

    r.raise_for_status.side_effect = _raise
    return r


class TestCheckWrapperReporting:
    """`check()` turns an exception into one line a newcomer can act on."""

    def test_success_prints_pass_and_returns_true(self, capsys):
        assert check_mod.check("OpenAI", lambda: "model=gpt-5, replied: 'ok'") is True
        out = capsys.readouterr().out
        assert "PASS" in out and "OpenAI" in out
        assert "replied" in out, "the detail message is the useful half"

    def test_failure_returns_false_rather_than_raising(self, capsys):
        """One dead provider must not abort the whole check run."""
        assert (
            check_mod.check("Grok", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            is False
        )
        assert "FAIL" in capsys.readouterr().out

    def test_http_error_includes_the_response_body(self, capsys):
        """A bare 401 cannot distinguish a bad key from an unfunded account."""
        resp = _resp(401, text='{"error":{"message":"Incorrect API key provided"}}')
        err = requests.HTTPError("401 Client Error")
        err.response = resp

        check_mod.check("OpenAI", lambda: (_ for _ in ()).throw(err))
        out = capsys.readouterr().out
        assert "Incorrect API key provided" in out, (
            "the provider's explanation is what makes a 401 actionable"
        )

    def test_a_key_in_a_url_is_redacted_before_printing(self, capsys):
        """Gemini AI Studio puts the key in the query string.

        A network error embeds the full URL — including the key — in its
        message. Printing that leaks the credential to the terminal and to any
        log the user pastes into a bug report.
        """
        leaky = RuntimeError(
            "HTTPSConnectionPool: /v1beta/models?key=AIzaSyREALSECRET123 failed"
        )
        check_mod.check("Gemini", lambda: (_ for _ in ()).throw(leaky))
        out = capsys.readouterr().out
        assert "AIzaSyREALSECRET123" not in out, "API key leaked into check output"
        assert "REDACTED" in out


class TestProviderChecks:
    def test_openai_reports_the_model_and_the_reply(self):
        resp = _resp(200, {"choices": [{"message": {"content": " ok "}}]})
        with patch.object(check_mod.requests, "post", return_value=resp):
            msg = check_mod.check_openai("k", "gpt-5.5")
        assert "gpt-5.5" in msg and "ok" in msg

    def test_openai_uses_max_completion_tokens_not_max_tokens(self):
        """GPT-5.x rejects max_tokens; sending it makes every check fail."""
        resp = _resp(200, {"choices": [{"message": {"content": "ok"}}]})
        with patch.object(check_mod.requests, "post", return_value=resp) as post:
            check_mod.check_openai("k", "gpt-5.5")
        body = post.call_args.kwargs["json"]
        assert "max_completion_tokens" in body
        assert "max_tokens" not in body

    def test_an_http_error_propagates_so_check_can_report_it(self):
        with patch.object(check_mod.requests, "post", return_value=_resp(401)):
            with pytest.raises(requests.HTTPError):
                check_mod.check_openai("bad", "gpt-5.5")

    @pytest.mark.parametrize(
        "fn,payload",
        [
            ("check_mistral", {"choices": [{"message": {"content": "ok"}}]}),
            ("check_claude", {"content": [{"text": "ok"}]}),
            ("check_perplexity", {"choices": [{"message": {"content": "ok"}}]}),
            ("check_grok", {"choices": [{"message": {"content": "ok"}}]}),
        ],
    )
    def test_each_provider_check_returns_a_message(self, fn, payload):
        with patch.object(check_mod.requests, "post", return_value=_resp(200, payload)):
            msg = getattr(check_mod, fn)("k", "some-model")
        assert isinstance(msg, str) and msg


class TestGeminiChecks:
    """ci-check builds Vertex's request itself, so it has to choose the location
    the way the client does (ci_core.llm.vertex): the 3.x models are served at
    `global` and the `us` and `eu` multi-regions and at no region."""

    @staticmethod
    def _cfg(**overrides):
        return {
            "provider": "vertex_ai",
            "project": "p",
            "model": "gemini-3.5-flash",
            **overrides,
        }

    def test_a_3x_model_with_no_location_is_checked_at_the_us_multi_region(self):
        project, location, model, url = check_mod._vertex_target(self._cfg())
        assert (project, location, model) == ("p", "us", "gemini-3.5-flash")
        assert url == (
            "https://aiplatform.us.rep.googleapis.com/v1/projects/p/locations/us/"
            "publishers/google/models/gemini-3.5-flash:generateContent"
        )

    def test_a_us_region_is_corrected_as_the_client_corrects_it(self):
        _, location, _, url = check_mod._vertex_target(
            self._cfg(location="us-central1")
        )
        assert location == "us"
        assert url.startswith("https://aiplatform.us.rep.googleapis.com/")

    def test_global_has_a_host_of_its_own(self):
        _, location, _, url = check_mod._vertex_target(self._cfg(location="global"))
        assert location == "global"
        assert url.startswith(
            "https://aiplatform.googleapis.com/v1/projects/p/locations/global/"
        )

    def test_a_2_5_model_keeps_its_region(self):
        _, location, _, url = check_mod._vertex_target(
            self._cfg(model="gemini-2.5-flash", location="europe-west4")
        )
        assert location == "europe-west4"
        assert url.startswith("https://europe-west4-aiplatform.googleapis.com/")

    def test_a_2_5_model_with_no_location_is_checked_at_us_central1(self):
        _, location, _, url = check_mod._vertex_target(
            self._cfg(model="gemini-2.5-flash")
        )
        assert location == "us-central1"
        assert url.startswith("https://us-central1-aiplatform.googleapis.com/")

    def test_a_config_with_no_model_checks_the_clients_default(self):
        from ci_core.llm import client

        cfg = self._cfg()
        del cfg["model"]
        _, _, model, _ = check_mod._vertex_target(cfg)
        assert model == client._PROVIDERS["gemini"]["default_model"]

    def test_the_reply_cap_leaves_room_for_a_3x_model_to_think(self):
        """Thinking counts against maxOutputTokens, and a cap it eats leaves no
        text part, which _extract_gemini_text reports as a failure."""
        resp = _resp(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        with patch.object(check_mod.requests, "post", return_value=resp) as post:
            check_mod.check_gemini("k", "gemini-3.5-flash")
        cap = post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"]
        assert cap >= 256


class TestWordPressCheck:
    """The publishing credential — the one with real consequences."""

    _SITE = "https://example.com"

    def test_success_names_the_authenticated_user(self):
        with patch.object(
            check_mod.requests,
            "get",
            side_effect=[_resp(200, {}), _resp(200, {"name": "Mike"})],
        ):
            msg = check_mod.check_wordpress(self._SITE, "u", "p")
        assert "Mike" in msg and self._SITE in msg

    def test_a_non_200_rest_root_points_at_permalinks(self):
        """The actual fix for this failure is a WP admin setting, so say so."""
        with patch.object(check_mod.requests, "get", return_value=_resp(404)):
            with pytest.raises(Exception, match="Permalinks"):
                check_mod.check_wordpress(self._SITE, "u", "p")

    def test_a_401_is_reported_as_credentials_not_as_an_http_error(self):
        with patch.object(
            check_mod.requests, "get", side_effect=[_resp(200, {}), _resp(401)]
        ):
            with pytest.raises(Exception, match="Authentication failed"):
                check_mod.check_wordpress(self._SITE, "u", "p")

    def test_basic_auth_header_is_built_correctly(self):
        with patch.object(
            check_mod.requests,
            "get",
            side_effect=[_resp(200, {}), _resp(200, {"name": "Mike"})],
        ) as get:
            check_mod.check_wordpress(self._SITE, "user", "pass")
        sent = get.call_args_list[1].kwargs["headers"]["Authorization"]
        assert sent == "Basic " + base64.b64encode(b"user:pass").decode()

    def test_a_trailing_slash_does_not_double_up_the_path(self):
        with patch.object(
            check_mod.requests,
            "get",
            side_effect=[_resp(200, {}), _resp(200, {"name": "M"})],
        ) as get:
            check_mod.check_wordpress(self._SITE + "/", "u", "p")
        assert "//wp-json" not in get.call_args_list[0].args[0]


class TestMainExitBehaviour:
    def test_a_missing_config_exits_with_a_readable_message(self, capsys):
        with (
            patch("sys.argv", ["ci-check", "--publication", "nope"]),
            patch.object(
                check_mod,
                "load_user_config",
                side_effect=FileNotFoundError("configs/user.yaml not found"),
            ),
        ):
            with pytest.raises(SystemExit):
                check_mod.main()
        out = capsys.readouterr().out
        assert "Config error" in out and "user.yaml" in out

    def test_publication_name_is_validated_before_any_network_call(self):
        """A path-traversal name must not reach the filesystem."""
        with (
            patch("sys.argv", ["ci-check", "--publication", "../etc/passwd"]),
            patch.object(check_mod.requests, "post") as post,
        ):
            with pytest.raises(SystemExit):
                check_mod.main()
        post.assert_not_called()
