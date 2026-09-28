"""Where a Gemini model is served on Vertex AI (ci_core.llm.vertex).

The facts these pin are Google's, read 2026-09-19: the 3.x models are served at
`global` and the `us` and `eu` multi-regions and at no regional endpoint, the 2.5
models at every US region and `global`. What reaches the wire is pinned in
test_llm_client.py; these cover the choice.
"""

import logging

import pytest

from ci_core.llm import vertex

# Every US region Google's endpoint table lists, none of which serves a 3.x model.
US_REGIONS = [
    "us-west1",
    "us-west4",
    "us-central1",
    "us-east1",
    "us-east4",
    "us-east5",
    "us-south1",
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No inherited location, and the once-only warning starts unsaid."""
    monkeypatch.delenv("VERTEXAI_LOCATION", raising=False)
    monkeypatch.setattr(vertex, "_said", set())


class TestGeneration:
    @pytest.mark.parametrize(
        "model,generation",
        [
            ("gemini-2.5-pro", 2),
            ("gemini-2.5-flash-lite", 2),
            ("gemini-3.5-flash", 3),
            ("gemini-3.5-flash-lite", 3),
            ("gemini-3.1-flash-lite", 3),
            ("gemini-3-flash-preview", 3),
            ("gemini-3.8-flash", 3),
            ("gemini-10-flash", 10),
            ("vertex_ai/gemini-3.5-flash", 3),
            ("gemini/gemini-2.5-pro", 2),
            ("models/gemini-3.5-flash", 3),
            ("GEMINI-3.5-FLASH", 3),
        ],
    )
    def test_it_is_read_off_the_id(self, model, generation):
        assert vertex.generation(model) == generation

    @pytest.mark.parametrize(
        "model",
        [None, "", "gpt-5.6-sol", "gemini-pro", "gemini-live-2.5-flash", "gemma-4"],
    )
    def test_an_id_that_is_not_gemini_then_a_number_has_none(self, model):
        assert vertex.generation(model) is None
        assert vertex.is_modern(model) is False

    @pytest.mark.parametrize(
        "model,modern",
        [
            ("gemini-2.5-pro", False),
            ("gemini-3-flash-preview", True),
            ("gemini-3.5-flash-lite", True),
            ("gemini-10-flash", True),
        ],
    )
    def test_three_and_later_are_modern(self, model, modern):
        assert vertex.is_modern(model) is modern


class TestALocationForAThreeXModel:
    MODEL = "gemini-3.5-flash"

    def test_nothing_set_is_the_us_multi_region(self):
        assert vertex.location_for(self.MODEL) == "us"
        assert vertex.location_for(self.MODEL, "") == "us"
        assert vertex.location_for(self.MODEL, None) == "us"
        assert vertex.DEFAULT_LOCATION == "us"

    @pytest.mark.parametrize("location", ["global", "us", "eu"])
    def test_the_locations_it_is_served_at_are_used_as_written(self, location):
        assert vertex.location_for(self.MODEL, location) == location

    def test_it_is_lowercased_and_trimmed_because_litellm_refuses_otherwise(self):
        assert vertex.location_for(self.MODEL, " US ") == "us"
        assert vertex.location_for(self.MODEL, "Global") == "global"

    @pytest.mark.parametrize("region", US_REGIONS)
    def test_a_us_region_is_not_served_and_becomes_the_multi_region(self, region):
        assert vertex.location_for(self.MODEL, region) == "us"

    @pytest.mark.parametrize(
        "region", ["europe-west4", "asia-northeast1", "northamerica-northeast1"]
    )
    def test_a_region_outside_the_us_is_used_as_written(self, region):
        """Google lists some for these models, and whoever names one meant it."""
        assert vertex.location_for(self.MODEL, region) == region

    def test_litellms_variable_stands_in_for_an_unset_location(self, monkeypatch):
        monkeypatch.setenv("VERTEXAI_LOCATION", "global")
        assert vertex.location_for(self.MODEL) == "global"

    def test_the_config_wins_over_the_variable(self, monkeypatch):
        monkeypatch.setenv("VERTEXAI_LOCATION", "global")
        assert vertex.location_for(self.MODEL, "eu") == "eu"

    def test_the_variable_is_corrected_like_the_config(self, monkeypatch):
        monkeypatch.setenv("VERTEXAI_LOCATION", "us-central1")
        assert vertex.location_for(self.MODEL) == "us"

    def test_a_route_prefix_does_not_hide_the_generation(self):
        assert vertex.location_for("vertex_ai/gemini-3.5-flash", "us-east4") == "us"


class TestALocationForAnOlderModel:
    MODEL = "gemini-2.5-pro"

    def test_nothing_set_is_left_to_litellm(self):
        assert vertex.location_for(self.MODEL) is None
        assert vertex.location_for(self.MODEL, "") is None

    def test_us_means_us_central1_because_2_5_has_no_multi_region(self):
        assert vertex.location_for(self.MODEL, "us") == "us-central1"

    @pytest.mark.parametrize("location", ["global", "eu", *US_REGIONS, "europe-west4"])
    def test_everything_else_is_used_as_written(self, location):
        assert vertex.location_for(self.MODEL, location) == location

    def test_litellms_variable_counts_here_too(self, monkeypatch):
        monkeypatch.setenv("VERTEXAI_LOCATION", "us")
        assert vertex.location_for(self.MODEL) == "us-central1"

    def test_an_unknown_model_is_sent_where_it_is_told(self):
        assert vertex.location_for(None, "us-east4") == "us-east4"


class TestTheCorrectionIsSaidOnce:
    def test_once_per_model_and_location(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ci_core.llm.vertex"):
            for _ in range(3):
                vertex.location_for("gemini-3.5-flash", "us-central1")
            vertex.location_for("gemini-3.5-flash", "us-east4")
            vertex.location_for("gemini-3.5-flash-lite", "us-central1")
        assert len(caplog.records) == 3

    def test_it_names_the_model_the_location_and_the_way_out(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ci_core.llm.vertex"):
            vertex.location_for("gemini-3.5-flash", "us-central1")
        (record,) = caplog.records
        assert "'us-central1'" in record.message
        assert "gemini-3.5-flash" in record.message
        assert "location: us" in record.message
        assert "location: global" in record.message

    def test_nothing_is_said_for_a_location_that_needed_no_correction(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ci_core.llm.vertex"):
            vertex.location_for("gemini-3.5-flash", "us")
            vertex.location_for("gemini-3.5-flash")
            vertex.location_for("gemini-2.5-pro", "us-central1")
        assert caplog.records == []


class TestHost:
    @pytest.mark.parametrize(
        "location,host",
        [
            ("global", "aiplatform.googleapis.com"),
            ("us", "aiplatform.us.rep.googleapis.com"),
            ("eu", "aiplatform.eu.rep.googleapis.com"),
            ("us-central1", "us-central1-aiplatform.googleapis.com"),
            ("europe-west4", "europe-west4-aiplatform.googleapis.com"),
        ],
    )
    def test_the_three_shapes_google_documents(self, location, host):
        assert vertex.host(location) == host

    @pytest.mark.parametrize("location", ["global", "us", "eu", "us-central1"])
    def test_it_is_the_host_litellm_builds(self, location):
        """ci-check builds its own request, so it must agree with the client's."""
        from ci_core.llm import client

        client._litellm()
        from litellm.llms.vertex_ai.common_utils import get_vertex_base_url

        assert f"https://{vertex.host(location)}" == get_vertex_base_url(location)


class TestLocationWarnings:
    @staticmethod
    def _cfg(**overrides):
        return {
            "provider": "vertex_ai",
            "model": "gemini-3.5-flash",
            "project": "p",
            "location": "us-central1",
            **overrides,
        }

    def test_a_us_region_on_a_3x_model_is_warned_by_name(self):
        (warning,) = vertex.location_warnings({"gemini": self._cfg()})
        assert "models.gemini.location is 'us-central1'" in warning
        assert "gemini-3.5-flash" in warning

    def test_the_default_model_counts_when_the_config_names_none(self):
        cfg = self._cfg()
        del cfg["model"]
        assert len(vertex.location_warnings({"gemini": cfg})) == 1

    @pytest.mark.parametrize("region", US_REGIONS)
    def test_every_us_region_is_warned(self, region):
        assert (
            len(vertex.location_warnings({"gemini": self._cfg(location=region)})) == 1
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"location": "us"},
            {"location": "global"},
            {"location": "eu"},
            {"location": "europe-west4"},
            {"location": None},
            {"model": "gemini-2.5-pro"},
            {"provider": "ai_studio"},
            {"enabled": False},
        ],
    )
    def test_a_config_that_runs_as_written_is_quiet(self, overrides):
        assert vertex.location_warnings({"gemini": self._cfg(**overrides)}) == []

    @pytest.mark.parametrize(
        "configs", [None, {}, {"gemini": "gemini-3.5-flash"}, {"openai": {"a": 1}}]
    )
    def test_a_config_it_cannot_judge_is_not_an_error(self, configs):
        assert vertex.location_warnings(configs) == []

    def test_the_warning_says_what_the_request_will_do(self):
        """The same correction location_for makes; the two must not disagree."""
        cfg = self._cfg()
        (warning,) = vertex.location_warnings({"gemini": cfg})
        assert vertex.location_for(cfg["model"], cfg["location"]) == "us"
        assert "Sending it to 'us'" in warning
