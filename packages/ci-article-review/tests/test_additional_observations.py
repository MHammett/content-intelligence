"""Section 8 — cross-domain observations: grouping, ranking, and rendering.

Two findings, both cases of the pipeline collecting a signal and looking for it
somewhere else.

**Confidence never reaches consensus.** ``_DEFAULT_CONFIDENCE_MULTIPLIERS`` is
inert by design — self-reported confidence is not calibrated and not comparable
across providers. What was not noticed is that it could barely fire even when
configured on: the weighting reads ``confidence`` off flags on their way into
consensus, and the schema puts ``confidence`` on ``additional_observations[]``
plus three ``fact_check`` buckets, of which consensus reads two. Measured on the
2026-09-04 maximum run: 0 of 119 consensus-bound findings carried a confidence,
against 55 that did and never reached consensus at all.

**Observations were never grouped.** ``_collect_additional_observations``
appended in dict-iteration order, so two models making the same observation
rendered as two unrelated bullets. Section 1 has grouped by passage since the
identical problem was found there. On that same run, Section 8's 55 bullets were
24 distinct observations, 12 of them corroborated — including one four models
agreed on, which appeared as four scattered entries.
"""

from ci_article_review import consolidation


def _observation(model, domain, passage, confidence, category="voice"):
    """One model emitting one cross-domain observation at a stated confidence."""
    return {
        (model, domain): {
            "failed": False,
            "data": {
                "additional_observations": [
                    {
                        "passage": passage,
                        "category": category,
                        "observation": "o",
                        "confidence": confidence,
                    }
                ]
            },
            "model": model,
            "tokens": {},
        }
    }


def _merge(results):
    return consolidation._merge_additional_observations(
        consolidation._collect_additional_observations(results)
    )


class TestConfidenceNeverReachesConsensus:
    """The inert multiplier, restated as an executable fact."""

    def test_observations_are_invisible_to_consensus(self):
        results = {
            **_observation("grok", "voice_style", "P", "high"),
            **_observation("mistral", "red_team", "P", "high"),
        }
        consensus, _ = consolidation._find_consensus(results, [], {})
        assert consensus == []

    def test_the_schema_puts_confidence_where_consensus_does_not_look(self):
        """A guard, not a description: if one of these buckets gains a
        ``confidence``, the inert default becomes a live choice rather than a
        no-op, and that should fail here rather than pass silently."""
        from ci_article_review import schemas

        for bucket in ("unverifiable", "primary_source_needed"):
            props = schemas.FACT_CHECK["properties"][bucket]["items"]["properties"]
            assert "confidence" not in props, (
                f"{bucket} gained a confidence - the weighting can now fire on "
                "it, so the inert default is a live choice rather than a no-op."
            )


class TestObservationsAreMerged:
    def test_two_models_on_one_passage_merge_into_one_entry(self):
        results = {
            **_observation("grok", "voice_style", "The grid is complex.", "high"),
            **_observation("mistral", "red_team", "The grid is complex.", "medium"),
        }
        merged = _merge(results)
        assert len(merged) == 1
        assert merged[0]["model_count"] == 2
        assert merged[0]["models"] == ["grok", "mistral"]

    def test_the_merged_entry_keeps_the_most_confident_fields(self):
        results = {
            **_observation("grok", "voice_style", "P", "low"),
            **_observation("mistral", "red_team", "P", "high"),
        }
        assert _merge(results)[0]["confidence"] == "high"

    def test_distinct_passages_are_not_merged(self):
        results = {
            **_observation("grok", "voice_style", "One thing entirely.", "high"),
            **_observation(
                "mistral", "red_team", "A completely different remark.", "high"
            ),
        }
        assert len(_merge(results)) == 2

    def test_existing_consumer_fields_survive_the_merge(self):
        """``voice_pattern_report`` reads these fields off Section 8 entries."""
        merged = _merge(_observation("grok", "voice_style", "P", "high"))
        assert merged[0]["category"] == "voice"
        assert merged[0]["source_model"] == "grok"
        assert merged[0]["source_domain"] == "voice_style"


class TestRanking:
    def test_corroboration_outranks_confidence(self):
        """Two models at "low" beat one at "high". Agreement is evidence; a
        self-reported level is a claim about a claim."""
        results = {
            **_observation("grok", "voice_style", "Agreed passage.", "low"),
            **_observation("mistral", "red_team", "Agreed passage.", "low"),
            **_observation("gemini", "completeness", "Lone passage.", "high"),
        }
        merged = _merge(results)
        assert merged[0]["passage"] == "Agreed passage."
        assert merged[1]["passage"] == "Lone passage."

    def test_confidence_orders_the_uncorroborated_tier(self):
        results = {
            **_observation("grok", "voice_style", "Low one.", "low"),
            **_observation("mistral", "red_team", "High one.", "high"),
            **_observation("gemini", "completeness", "Medium one.", "medium"),
        }
        assert [o["passage"] for o in _merge(results)] == [
            "High one.",
            "Medium one.",
            "Low one.",
        ]

    def test_a_missing_confidence_sorts_last_rather_than_raising(self):
        results = {
            ("grok", "voice_style"): {
                "failed": False,
                "data": {
                    "additional_observations": [
                        {"passage": "No confidence.", "category": "voice"}
                    ]
                },
                "model": "grok",
                "tokens": {},
            },
            **_observation("mistral", "red_team", "Has one.", "low"),
        }
        assert [o["passage"] for o in _merge(results)] == [
            "Has one.",
            "No confidence.",
        ]

    def test_empty_input_is_empty_output(self):
        assert consolidation._merge_additional_observations([]) == []


class TestRendering:
    def _render(self, items):
        from ci_article_review.report_markdown import _render_section_8

        return "\n".join(_render_section_8(items))

    def _entry(self, **kw):
        base = {
            "passage": "P",
            "category": "voice",
            "models": ["grok"],
            "model_count": 1,
            "source_domain": "voice_style",
        }
        base.update(kw)
        return base

    def test_agreement_is_stated_in_the_bullet(self):
        out = self._render([self._entry(models=["grok", "mistral"], model_count=2)])
        assert "2 models agree" in out
        assert "grok, mistral" in out

    def test_a_lone_observation_still_names_its_model_and_domain(self):
        out = self._render([self._entry()])
        assert "grok:voice_style" in out
        assert "models agree" not in out

    def test_bookkeeping_keys_are_not_printed_as_model_output(self):
        """``models``/``model_count`` are this pass's arithmetic, and rendering
        them as key/value lines reads as something a model supplied."""
        out = self._render([self._entry()])
        assert "Model count" not in out
        assert "Models:" not in out

    def test_the_reader_is_told_what_the_order_means(self):
        out = self._render([{"passage": "P", "category": "voice"}])
        assert "corroborated first" in out.lower()
        assert "not comparable" in out

    def test_an_entry_predating_the_merge_still_renders(self):
        """A report read back from ``pipeline_history`` has no ``models`` key."""
        out = self._render(
            [
                {
                    "passage": "P",
                    "category": "voice",
                    "source_model": "grok",
                    "source_domain": "voice_style",
                }
            ]
        )
        assert "grok:voice_style" in out


class TestUnsourcedConfirmationsAreDemoted:
    """`confirmed` is the strongest thing Section 2 says; it has to mean a
    document backs the claim.

    Measured 2026-09-05 on the Honda draft: 4 of 19 `confirmed` findings cited
    no document. One named "Draft Article" -- the pipeline confirming the draft
    against itself -- and three named "Manual Calculation", where the model did
    the arithmetic and reported the result as confirmed. All four carried
    `source_url: "N/A"`. The sums may well be right; that is not what the tier
    claims.
    """

    def _fact_check(self, *items):
        return {
            ("gemini", "fact_check"): {
                "failed": False,
                "data": {"confirmed": list(items), "unverifiable": []},
                "model": "gemini",
                "tokens": {},
            }
        }

    def test_the_draft_cannot_confirm_itself(self):
        fc = consolidation._build_fact_check(
            self._fact_check(
                {"claim": "c", "source": "Draft Article", "source_url": "N/A"}
            ),
            {},
        )
        assert fc["confirmed"] == []
        assert len(fc["unverifiable"]) == 1

    def test_the_model_doing_arithmetic_is_not_a_source(self):
        fc = consolidation._build_fact_check(
            self._fact_check(
                {"claim": "c", "source": "Manual Calculation", "source_url": "N/A"}
            ),
            {},
        )
        assert fc["confirmed"] == []

    def test_a_named_document_without_a_url_still_counts(self):
        """An unlinked "Honda ServiceNews B18010I" is a real document; demoting
        it would throw away the leads this pass is most useful for."""
        fc = consolidation._build_fact_check(
            self._fact_check(
                {"claim": "c", "source": "Honda ServiceNews B18010I", "source_url": ""}
            ),
            {},
        )
        assert len(fc["confirmed"]) == 1

    def test_a_url_settles_it_whatever_the_source_text_says(self):
        fc = consolidation._build_fact_check(
            self._fact_check(
                {
                    "claim": "c",
                    "source": "Draft Article",
                    "source_url": "https://example.org/x",
                }
            ),
            {},
        )
        assert len(fc["confirmed"]) == 1

    def test_one_real_source_among_several_is_enough(self):
        fc = consolidation._build_fact_check(
            self._fact_check(
                {
                    "claim": "c",
                    "source": "Draft Article; Bianchi Honda",
                    "source_url": "N/A",
                }
            ),
            {},
        )
        assert len(fc["confirmed"]) == 1

    def test_the_claim_is_moved_rather_than_dropped(self):
        """It still needs checking; it just is not confirmed."""
        fc = consolidation._build_fact_check(
            self._fact_check(
                {
                    "claim": "the sum is 1024",
                    "source": "Manual Calculation",
                    "source_url": "N/A",
                }
            ),
            {},
        )
        demoted = fc["unverifiable"][0]
        assert demoted["claim"] == "the sum is 1024"
        assert demoted["checked"] == "Manual Calculation"
        assert "no external source" in demoted["reason"]

    def test_existing_unverifiable_findings_are_preserved(self):
        results = {
            ("gemini", "fact_check"): {
                "failed": False,
                "data": {
                    "confirmed": [
                        {"claim": "c", "source": "Draft Article", "source_url": "N/A"}
                    ],
                    "unverifiable": [{"claim": "already here", "reason": "r"}],
                },
                "model": "gemini",
                "tokens": {},
            }
        }
        fc = consolidation._build_fact_check(results, {})
        claims = {u["claim"] for u in fc["unverifiable"]}
        assert claims == {"c", "already here"}

    def test_a_clean_result_is_left_alone(self):
        results = self._fact_check(
            {"claim": "c", "source": "GPS.gov", "source_url": "https://gps.gov/x"}
        )
        before = dict(results[("gemini", "fact_check")]["data"])
        fc = consolidation._build_fact_check(results, {})
        assert len(fc["confirmed"]) == 1
        assert results[("gemini", "fact_check")]["data"] == before, (
            "the caller's results must not be mutated"
        )
