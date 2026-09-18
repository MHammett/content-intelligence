"""Integrity tests for ci_core's externalized LLM config data files.

  ci_core/configs/pricing.yaml        -> ci_core/llm/cost.py
  ci_core/configs/model_registry.yaml -> ci_core/llm/model_registry.py
  ci_core/configs/timeouts.yaml       -> ci_core/llm/timeout_model.py

Each loader keeps a hardcoded fallback used only when the YAML is missing or
malformed.  These tests assert two things:

1. PARITY — the YAML content matches the hardcoded fallback, so a dropped or
   corrupt YAML silently using stale defaults can never go unnoticed.  If you
   intentionally change one, change the other and these tests confirm they agree.
2. STRUCTURE — each YAML file parses and has the shape the loader expects, so a
   typo is caught at test time instead of during a real (billed) pipeline run.
"""

import os
import datetime

import yaml
import ci_core

_CONFIGS = os.path.join(os.path.dirname(ci_core.__file__), "configs")


def _load(name):
    with open(os.path.join(_CONFIGS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Validity, not parity
# ---------------------------------------------------------------------------
#
# These used to assert that each YAML matched a duplicate hardcoded table in
# Python (audit finding 14). Four such pairs existed, and every pricing or
# timeout edit had to be made twice or CI failed — on the config surface that
# changes most often.
#
# The duplicates are gone: the YAML is the single source of truth and the
# loaders raise PackagedConfigError if it is missing or malformed. A parity test
# only ever proved two copies matched, never that either was *usable*, so these
# check the thing that actually matters instead.


class TestPackagedConfigsAreValid:
    def test_pricing_loads_with_entries(self):
        from ci_core.llm import cost

        assert cost._PRICING, "pricing.yaml produced an empty table"
        for model, pair in cost._PRICING.items():
            assert len(pair) in (2, 3), f"{model} price is not a pair"
            assert all(isinstance(v, float) and v >= 0 for v in pair), (
                f"{model} has a negative or non-numeric price: {pair}"
            )

    def test_unknown_price_is_a_positive_pair(self):
        from ci_core.llm import cost

        assert len(cost._UNKNOWN_PRICE) == 2
        assert all(v > 0 for v in cost._UNKNOWN_PRICE)

    def test_timeout_size_buckets_ascend_and_end_open(self):
        """The bucket order decides which multiplier a draft gets.

        A parity test could not catch a mis-ordered bucket, because it would
        happily match a mis-ordered duplicate.
        """
        import ci_core.llm.timeout_model as tm

        buckets = tm._CONFIG["size_multipliers"]
        limits = [b.get("max_chars") for b in buckets]
        assert limits[-1] is None, "final bucket must be open-ended (max_chars: null)"
        finite = [x for x in limits[:-1]]
        assert all(x is not None for x in finite), "only the last bucket may be open"
        assert finite == sorted(finite), f"size buckets are not ascending: {finite}"

    def test_timeout_multipliers_are_positive(self):
        import ci_core.llm.timeout_model as tm

        for section in ("model_multipliers", "effort_multipliers"):
            for key, value in tm._CONFIG[section].items():
                assert float(value) > 0, f"{section}.{key} must be positive"

    def test_a_missing_packaged_file_raises_rather_than_degrading(self, tmp_path):
        """The state the old fallbacks existed for — now reported, not hidden.

        Silently serving a stale pricing table in a broken install gives the
        user quietly wrong cost numbers instead of a fixable message.
        """
        import pytest

        from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml

        with pytest.raises(PackagedConfigError, match="missing"):
            load_packaged_yaml(tmp_path / "does-not-exist.yaml")

    def test_a_malformed_packaged_file_raises(self, tmp_path):
        import pytest

        from ci_core.config_helpers import PackagedConfigError, load_packaged_yaml

        bad = tmp_path / "bad.yaml"
        bad.write_text("just a string, not a mapping", encoding="utf-8")
        with pytest.raises(PackagedConfigError, match="not a YAML mapping"):
            load_packaged_yaml(bad)


# ---------------------------------------------------------------------------
# Structure: each YAML parses and matches the loader's expectations
# ---------------------------------------------------------------------------


class TestPricingStructure:
    def test_every_model_has_numeric_pair(self):
        data = _load("pricing.yaml")
        for model_id, pair in data["models"].items():
            assert isinstance(pair, list) and len(pair) in (2, 3), (
                f"{model_id}: not a 2-element list"
            )
            assert all(isinstance(x, (int, float)) for x in pair), (
                f"{model_id}: non-numeric price"
            )
            assert all(x >= 0 for x in pair), f"{model_id}: negative price"

    def test_unknown_price_present_and_numeric(self):
        data = _load("pricing.yaml")
        pair = data["unknown_price"]
        assert len(pair) in (2, 3) and all(isinstance(x, (int, float)) for x in pair)


class TestRegistryStructure:
    def test_registry_date_is_valid_iso(self):
        data = _load("model_registry.yaml")
        # fromisoformat raises if malformed; str() handles a YAML-parsed date object too.
        datetime.date.fromisoformat(str(data["registry_date"]))

    def test_staleness_thresholds_are_ints(self):
        data = _load("model_registry.yaml")
        assert isinstance(data["stale_notice_days"], int)
        assert isinstance(data["stale_warning_days"], int)
        assert data["stale_notice_days"] < data["stale_warning_days"]

    def test_every_superseded_has_replacement(self):
        data = _load("model_registry.yaml")
        for model_id, info in data["superseded"].items():
            assert "replacement" in info, f"{model_id}: missing replacement"
            assert isinstance(info["replacement"], str)

    def test_every_newer_available_has_newer(self):
        data = _load("model_registry.yaml")
        for model_id, info in (data.get("newer_available") or {}).items():
            assert "newer" in info, f"{model_id}: missing 'newer'"


class TestTimeoutsStructure:
    def test_required_sections_present(self):
        data = _load("timeouts.yaml")
        for key in (
            "base_seconds",
            "floor_seconds",
            "size_multipliers",
            "model_multipliers",
            "effort_multipliers",
        ):
            assert key in data, f"timeouts.yaml missing '{key}'"

    def test_size_buckets_ordered_and_open_ended(self):
        buckets = _load("timeouts.yaml")["size_multipliers"]
        caps = [b["max_chars"] for b in buckets]
        assert caps[-1] is None, (
            "largest size bucket must be open-ended (max_chars: null)"
        )
        finite = [c for c in caps if c is not None]
        assert finite == sorted(finite), "size buckets must be in ascending order"

    def test_effort_table_has_default(self):
        eff = _load("timeouts.yaml")["effort_multipliers"]
        assert "default" in eff, (
            "effort_multipliers needs a 'default' for models with no effort param"
        )

    def test_model_table_has_default(self):
        models = _load("timeouts.yaml")["model_multipliers"]
        assert "default" in models, "model_multipliers needs a 'default'"

    def test_effort_is_monotonic_nondecreasing(self):
        eff = _load("timeouts.yaml")["effort_multipliers"]
        ladder = [
            eff[k] for k in ("none", "low", "medium", "high", "xhigh") if k in eff
        ]
        assert ladder == sorted(ladder), (
            f"effort multipliers should not decrease: {ladder}"
        )

    def test_variance_margin_present_and_sane(self):
        vm = _load("timeouts.yaml").get("variance_margin")
        assert vm is not None, "timeouts.yaml should define variance_margin"
        assert vm >= 1.0, (
            "variance_margin below 1.0 would shrink timeouts below the central estimate"
        )


class TestOutputTokensStructure:
    def test_every_table_has_a_default_and_positive_values(self):
        data = _load("output_tokens.yaml")
        for table in ("reasoning_tokens", "answer_tokens", "floor_tokens_per_second"):
            assert "default" in data[table], (
                f"output_tokens.yaml {table} needs a default"
            )
            for key, value in data[table].items():
                assert isinstance(value, (int, float)) and value > 0, (table, key)

    def test_reasoning_room_does_not_shrink_as_effort_rises(self):
        r = _load("output_tokens.yaml")["reasoning_tokens"]
        ladder = [r.get(k, r["default"]) for k in ("low", "medium", "high", "xhigh")]
        assert ladder == sorted(ladder), f"reasoning_tokens decreases: {ladder}"

    def test_it_defines_no_size_table_of_its_own(self):
        """The ceiling scales with timeouts.yaml's buckets so the two cannot
        drift. A second table here would be read by nothing and mislead."""
        assert "size_multipliers" not in _load("output_tokens.yaml")
