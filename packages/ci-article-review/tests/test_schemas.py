"""The domain schemas must track the prompts they mirror.

Strict mode means `additionalProperties: false`, so a field added to a prompt's
RETURN FORMAT block and not added here is a field the model is now *forbidden*
to return. That failure is quiet in the worst way: the call succeeds, the schema
is honoured, and the finding the new field carried is simply absent.

So the drift guard reads the prompt's own RETURN FORMAT block and compares it to
the schema. It is the same shape of guard the repo already uses for pricing and
the model registry.
"""

import json
import re

import pytest

from ci_article_review import schemas
from ci_article_review.pipeline import _DOMAIN_PROMPTS, _load_prompt


def _declared_in_prompt(domain):
    """Top-level bucket names from the prompt's RETURN FORMAT block."""
    text = _load_prompt(_DOMAIN_PROMPTS[domain])
    block = text.split("RETURN FORMAT")[-1]
    start = block.index("{")
    # Walk to the matching close brace so trailing prose is ignored.
    depth = 0
    for i, ch in enumerate(block[start:], start):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            body = block[start : i + 1]
            break
    else:
        pytest.fail(f"{domain}: RETURN FORMAT block is not brace-balanced")
    # Top-level keys only: those at nesting depth 1.
    names, depth = [], 0
    for line in body.splitlines():
        stripped = line.strip()
        match = re.match(r'"([a-z_]+)"\s*:', stripped)
        if match and depth == 1:
            names.append(match.group(1))
        depth += line.count("{") + line.count("[")
        depth -= line.count("}") + line.count("]")
    return names


class TestSchemasMatchPrompts:
    @pytest.mark.parametrize("domain", sorted(schemas.BY_DOMAIN))
    def test_buckets_match_the_prompt(self, domain):
        declared = set(_declared_in_prompt(domain))
        modelled = set(schemas.BY_DOMAIN[domain]["properties"])
        assert modelled == declared, (
            f"{domain}: schema and prompt disagree.\n"
            f"  in the prompt, not the schema: {sorted(declared - modelled)}\n"
            f"  in the schema, not the prompt: {sorted(modelled - declared)}\n"
            f"Under strict mode the first list is forbidden output — those "
            f"findings would vanish silently."
        )

    @pytest.mark.parametrize("domain", sorted(schemas.BY_DOMAIN))
    def test_buckets_are_in_the_same_order(self, domain):
        """Order is what a cutoff loses: everything after the bucket it hit.

        A model follows the prompt's order and a strict provider the schema's,
        and ``consolidation._truncation_extent`` reads the schema's to say what
        never arrived. Two orders would mean a truncated pass reports the wrong
        buckets lost, on whichever path it ran.
        """
        assert _declared_in_prompt(domain) == list(
            schemas.BY_DOMAIN[domain]["properties"]
        )

    def test_every_domain_the_pipeline_runs_has_a_schema(self):
        assert set(schemas.BY_DOMAIN) == set(_DOMAIN_PROMPTS)


class TestFactCheckWritesVerdictsFirst:
    """A cutoff should cost confirmations, not the verdicts that need action.

    The 2026-10-23 ``maximum`` run's mistral:fact_check spent its whole
    50,000-token ceiling on 154 confirmations and delivered no other bucket.
    """

    def test_confirmed_comes_after_every_bucket_that_asks_for_a_change(self):
        order = list(schemas.FACT_CHECK["properties"])
        for bucket in ("contradicted", "outdated", "unverifiable"):
            assert order.index(bucket) < order.index("confirmed"), bucket

    def test_the_legacy_order_is_the_same_buckets(self):
        for domain, orders in schemas.LEGACY_ORDER.items():
            for order in orders:
                assert sorted(order) == sorted(schemas.BY_DOMAIN[domain]["properties"])


class TestSubset:
    def test_holds_only_the_buckets_asked_for_in_schema_order(self):
        reduced = schemas.subset(
            "fact_check", ["additional_observations", "contradicted", "confirmed"]
        )
        order = [
            b
            for b in schemas.FACT_CHECK["properties"]
            if b in {"additional_observations", "contradicted", "confirmed"}
        ]
        assert list(reduced["properties"]) == order
        assert reduced["required"] == order
        assert reduced["additionalProperties"] is False
        assert reduced["type"] == "object"

    def test_bucket_definitions_are_the_full_schemas(self):
        reduced = schemas.subset("fact_check", ["outdated"])
        assert (
            reduced["properties"]["outdated"]
            == schemas.FACT_CHECK["properties"]["outdated"]
        )

    def test_it_does_not_change_the_full_schema(self):
        before = json.dumps(schemas.FACT_CHECK, sort_keys=True)
        schemas.subset("fact_check", ["outdated"])
        assert json.dumps(schemas.FACT_CHECK, sort_keys=True) == before

    def test_custom_domain_gets_none(self):
        assert schemas.subset("my_custom_domain", ["flags"]) is None

    def test_a_bucket_the_domain_lacks_is_refused(self):
        with pytest.raises(ValueError):
            schemas.subset("fact_check", ["flags"])

    def test_asking_for_nothing_is_refused(self):
        with pytest.raises(ValueError):
            schemas.subset("fact_check", [])


class TestStrictModeShape:
    """OpenAI's strict mode rejects a schema that does not follow its rules."""

    def _objects(self, node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from self._objects(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._objects(item)

    @pytest.mark.parametrize("domain", sorted(schemas.BY_DOMAIN))
    def test_every_object_is_closed_and_fully_required(self, domain):
        for obj in self._objects(schemas.BY_DOMAIN[domain]):
            assert obj.get("additionalProperties") is False, (
                f"{domain}: an object allows extra properties; strict mode rejects it"
            )
            assert set(obj.get("required", [])) == set(obj["properties"]), (
                f"{domain}: strict mode requires every property to be in `required` "
                f"— use a nullable type for genuinely optional fields"
            )

    def test_optional_fields_are_nullable_rather_than_absent(self):
        """`note` is the one field the prompt calls optional.

        Strict mode cannot express "may be omitted", so it is required and
        nullable — the model can decline it without being pushed into inventing
        one, which for a fact-check matters more than tidiness.
        """
        note = schemas.FACT_CHECK["properties"]["confirmed"]["items"]["properties"][
            "note"
        ]
        assert note["type"] == ["string", "null"]

    @pytest.mark.parametrize("domain", sorted(schemas.BY_DOMAIN))
    def test_schema_is_json_serialisable(self, domain):
        json.dumps(schemas.BY_DOMAIN[domain])


class TestForDomain:
    def test_returns_name_and_schema(self):
        out = schemas.for_domain("fact_check")
        assert out["name"] == "fact_check"
        assert out["schema"] is schemas.FACT_CHECK

    def test_custom_domain_gets_none(self):
        """A publication-defined domain has a prompt but no declared shape.

        Enforcing one nobody wrote would reject output that is perfectly good.
        """
        assert schemas.for_domain("some_custom_domain") is None


class TestEvidenceFieldsAreRequiredWhereTheVerdictAssertsOne:
    """The prompt says a claim without a quotable source does not belong in a
    verdict bucket — it belongs in `unverifiable` or `primary_source_needed`.
    Requiring the evidence fields there enforces the rule the prompt states.
    """

    @pytest.mark.parametrize("bucket", ["confirmed", "outdated", "contradicted"])
    def test_verdict_buckets_require_url_and_quote(self, bucket):
        props = schemas.FACT_CHECK["properties"][bucket]["items"]["properties"]
        assert "source_url" in props
        assert "supporting_quote" in props

    @pytest.mark.parametrize("bucket", ["unverifiable", "primary_source_needed"])
    def test_the_honest_buckets_demand_no_evidence(self, bucket):
        props = schemas.FACT_CHECK["properties"][bucket]["items"]["properties"]
        assert "supporting_quote" not in props
