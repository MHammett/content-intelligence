"""Cross-run reproducibility: what may be compared, and what the report says.

The behaviour under test is a claim about honesty, so most of these tests are
about what the module *refuses* to assert: it must not compare runs that used
different ensemble configurations, must not count a run that never ran the
domain a finding came from, and must not write a zero where it took no
measurement.

The comparability rules are not arbitrary. They were derived from this
project's own ``pipeline_history/dc-environment``, which holds 47 runs over 8
distinct drafts, whose largest same-draft cluster mixes single-pass probes with
full 25-pass runs, and in which only one of twelve otherwise-comparable runs
completed with no failed passes.
"""

import json
from datetime import datetime, timezone


from ci_article_review import reproducibility as repro
from ci_article_review.history import _slug
from ci_article_review.report_markdown import render_report_markdown

DRAFT = "The grid serving Northern Illinois produces 0.40 lbs of NOx per MWh."
OTHER_DRAFT = "A completely different article about something else entirely."

ASSIGNMENTS = [
    "openai:fact_check",
    "openai:voice_style",
    "gemini:fact_check",
    "mistral:argument_integrity",
    "openai:completeness",
]


def make_report(
    draft=DRAFT,
    assignments=None,
    thoroughness="standard",
    voice=(),
    argument=(),
    completeness=(),
    consensus=(),
    fact_check=None,
    failures=(),
    consensus_threshold=2.0,
    min_models=2,
    model_id="test-model-1",
):
    """A report shaped like ``consolidation.build_report``'s output."""
    return {
        "generated": "2026-08-09T12:00:00+00:00",
        "corrected_draft": draft,
        "model_failures": list(failures),
        "ensemble": {
            "thoroughness": thoroughness,
            "assignments": list(ASSIGNMENTS if assignments is None else assignments),
            "consensus_threshold": consensus_threshold,
            "width": {"consensus_min_models": min_models},
        },
        "api_call_log": [
            {"pass": a, "model": model_id, "effort": "none"}
            for a in (ASSIGNMENTS if assignments is None else assignments)
        ],
        "section_1_consensus": [
            {
                "passage": p,
                "models": ["openai:fact_check", "gemini:fact_check"],
                "weight_sum": 2.5,
                "flags": [],
            }
            for p in consensus
        ],
        "section_3_voice": [{"passage": p, "problem": "x"} for p in voice],
        "section_4_argument": [
            {"passage": p, "logical_problem": "x"} for p in argument
        ],
        "section_5_completeness": [
            {"passage_reference": p, "what_is_missing": "x"} for p in completeness
        ],
        "section_2_fact_check": fact_check or {},
    }


#: Long enough to survive ``history._slug``'s minimum-length guard, which
#: rewrites anything shorter to "untitled".
KEY = "reproducibility-test-article"


def write_history(root, key, reports):
    """Write ``reports`` where ``history`` will look for them.

    Slugged through ``history._slug`` rather than used raw, so these fixtures
    land in the directory the production reader actually opens.
    """
    d = root / _slug(key)
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, report in enumerate(reports, 1):
        path = d / f"run_1_202608{i:02d}_120000_report.json"
        path.write_text(json.dumps(report, default=str), encoding="utf-8")
        paths.append(path)
    return paths


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


class TestComparability:
    def test_same_draft_and_ensemble_is_comparable(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(voice=["a"])])
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 1

    def test_a_different_draft_is_not_comparable(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(draft=OTHER_DRAFT, voice=["a"])])
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        block = current["reproducibility"]
        assert block["comparable_run_count"] == 0
        assert block["skipped"][0]["reason"] == "different draft"

    def test_whitespace_only_differences_are_the_same_draft(self, tmp_path):
        rewrapped = DRAFT.replace(" ", "\n  ")
        write_history(tmp_path, KEY, [make_report(draft=rewrapped, voice=["a"])])
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 1

    def test_a_different_ensemble_is_not_comparable(self, tmp_path):
        """The rule that stops history from manufacturing a reproduction rate.

        ``dc-environment``'s largest same-draft cluster is 23 runs spanning
        single-pass probes and full 25-pass runs. Grouping on draft alone would
        score a finding as absent from eleven runs that never ran the domain
        capable of producing it.
        """
        write_history(
            tmp_path,
            KEY,
            [make_report(assignments=["openai:voice_style"], voice=["a"])],
        )
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        block = current["reproducibility"]
        assert block["comparable_run_count"] == 0
        assert "different ensemble" in block["skipped"][0]["reason"]

    def test_a_different_model_behind_the_same_provider_is_not_comparable(
        self, tmp_path
    ):
        """``assignments`` names providers, not models.

        ``openai:fact_check`` is the same string on ``gpt-5.4-mini`` and on
        ``gpt-5.5``, so without the model ids folded in, a cheap run and an
        expensive one would be scored against each other as repeats.
        """
        write_history(
            tmp_path, KEY, [make_report(voice=["a"], model_id="gpt-5.4-mini")]
        )
        current = make_report(voice=["a"], model_id="gpt-5.5")
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_a_retuned_consensus_gate_is_not_comparable(self, tmp_path):
        """``consensus_threshold`` decides Section 1 membership outright."""
        write_history(
            tmp_path, KEY, [make_report(voice=["a"], consensus_threshold=3.0)]
        )
        current = make_report(voice=["a"], consensus_threshold=2.0)
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_a_different_consensus_min_models_is_not_comparable(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(voice=["a"], min_models=3)])
        current = make_report(voice=["a"], min_models=2)
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_a_citation_verification_call_does_not_break_comparability(self, tmp_path):
        """``api_call_log`` mixes review passes with citation verification.

        One real run logged three ``citation_verification:known_url`` calls on
        ``mistral-small-latest`` and was thereby scored incomparable with four
        runs whose five *review* models matched it exactly. Only the assignment
        set counts toward the signature.
        """
        prior = make_report(voice=["a"])
        current = make_report(voice=["a"])
        current["api_call_log"] = list(current["api_call_log"]) + [
            {
                "pass": "citation_verification:known_url",
                "model": "mistral-small-latest",
                "effort": "none",
            }
        ]
        write_history(tmp_path, KEY, [prior])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 1

    def test_the_grounding_suffix_does_not_split_a_group(self, tmp_path):
        """Measured cost of splitting on it was 20 comparable runs down to 14.

        The same model appears with and without the suffix inside one run, so
        it identifies a call rather than a configuration.
        """
        write_history(
            tmp_path, KEY, [make_report(voice=["a"], model_id="gemini-2.5-pro")]
        )
        current = make_report(voice=["a"], model_id="gemini-2.5-pro [grounded]")
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 1

    def test_a_different_thoroughness_is_not_comparable(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(thoroughness="maximum", voice=["a"])])
        current = make_report(thoroughness="standard", voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_a_report_without_ensemble_metadata_is_skipped(self, tmp_path):
        stale = make_report(voice=["a"])
        del stale["ensemble"]
        write_history(tmp_path, KEY, [stale])
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        block = current["reproducibility"]
        assert block["comparable_run_count"] == 0
        assert block["skipped"][0]["reason"] == "no ensemble metadata"

    def test_runs_at_or_after_the_cutoff_are_excluded(self, tmp_path):
        """A run must never count itself, nor a later one, as corroboration."""
        write_history(tmp_path, KEY, [make_report(voice=["a"])])
        current = make_report(voice=["a"])
        repro.annotate(
            current,
            str(tmp_path),
            KEY,
            before_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_missing_history_directory_is_not_an_error(self, tmp_path):
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path / "nope"), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_unreadable_report_is_skipped_not_raised(self, tmp_path):
        d = tmp_path / KEY
        d.mkdir()
        (d / "run_1_20260801_120000_report.json").write_text(
            "{ not json", encoding="utf-8"
        )
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["reproducibility"]["comparable_run_count"] == 0


class TestCounting:
    def test_counts_reflect_how_many_runs_raised_the_finding(self, tmp_path):
        write_history(
            tmp_path,
            KEY,
            [
                make_report(voice=["kept", "dropped"]),
                make_report(voice=["kept"]),
                make_report(voice=["kept"]),
            ],
        )
        current = make_report(voice=["kept", "dropped", "brand new"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        by_passage = {f["passage"]: f for f in current["section_3_voice"]}
        assert by_passage["kept"]["reproduced_in"] == 3
        assert by_passage["dropped"]["reproduced_in"] == 1
        assert by_passage["brand new"]["reproduced_in"] == 0
        assert by_passage["kept"]["reproduced_of"] == 3

    def test_no_comparable_runs_writes_no_count_at_all(self, tmp_path):
        """An untaken measurement is absent, never a zero.

        ``reproduced_in: 0`` beside ``reproduced_of: 0`` reads as "checked,
        found nothing" — the opposite of the truth.
        """
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        finding = current["section_3_voice"][0]
        assert "reproduced_in" not in finding
        assert "reproduced_of" not in finding

    def test_re_annotating_without_history_clears_stale_counts(self, tmp_path):
        """Annotation is idempotent, and never leaves a superseded number.

        A report scored once and scored again against a history holding nothing
        comparable must come back saying "not measured", not still carrying the
        counts from the first pass — those describe a comparison that is no
        longer being claimed.
        """
        write_history(tmp_path, KEY, [make_report(voice=["a"])])
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["section_3_voice"][0]["reproduced_in"] == 1

        repro.annotate(current, str(tmp_path / "empty"), KEY, before_ts=NOW)
        finding = current["section_3_voice"][0]
        assert "reproduced_in" not in finding
        assert "reproduced_of" not in finding
        assert current["reproducibility"]["comparable_run_count"] == 0

    def test_a_run_that_lost_the_domain_leaves_that_section_uncounted(self, tmp_path):
        """Per-section denominators.

        A prior run whose ``voice_style`` passes all failed had no opportunity
        to raise a voice finding. Counting it as a run that did not reproduce
        would understate every voice finding in the report. Its other sections
        still count.
        """
        write_history(
            tmp_path,
            KEY,
            [
                make_report(
                    voice=[], argument=["arg"], failures=["openai:voice_style"]
                ),
                make_report(voice=["v"], argument=["arg"]),
            ],
        )
        current = make_report(voice=["v"], argument=["arg"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["section_3_voice"][0]["reproduced_of"] == 1
        assert current["section_3_voice"][0]["reproduced_in"] == 1
        assert current["section_4_argument"][0]["reproduced_of"] == 2
        assert current["reproducibility"]["degraded_runs"] == 1

    def test_fact_check_verdicts_are_counted_per_bucket(self, tmp_path):
        """A claim confirmed in one run and contradicted in the next is not a
        reproduction — the buckets disagree about the draft."""
        prior = make_report(
            fact_check={"confirmed": [{"claim": "c1"}], "contradicted": []}
        )
        write_history(tmp_path, KEY, [prior])
        current = make_report(
            fact_check={"confirmed": [], "contradicted": [{"claim": "c1"}]}
        )
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert current["section_2_fact_check"]["contradicted"][0]["reproduced_in"] == 0

    def test_section_totals_are_summarised(self, tmp_path):
        write_history(
            tmp_path, KEY, [make_report(voice=["a"]), make_report(voice=["a", "b"])]
        )
        current = make_report(voice=["a", "b", "c"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        stats = current["reproducibility"]["sections"]["section_3_voice"]
        assert stats == {
            "findings": 3,
            "reproduced": 2,
            "at_least_half": 2,
            "denominator": 2,
        }


class TestRendering:
    def test_the_caveat_is_above_the_findings(self, tmp_path):
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "## Reading this report" in md
        assert md.index("Reading this report") < md.index("SECTION 1")

    def test_unmeasured_reports_say_so_and_give_the_calibration(self, tmp_path):
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "Not measured for this draft" in md
        assert str(repro.CALIBRATION["distinct_findings"]) in md

    def test_measured_reports_show_the_counts(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(voice=["a"]) for _ in range(3)])
        current = make_report(voice=["a", "b"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "Measured on this draft" in md
        assert "also in 3 of 3 comparable prior runs" in md
        assert "in none of 3 comparable prior runs" in md

    def test_section_1_is_banded_not_ranked(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(consensus=["strong"])] * 2)
        current = make_report(consensus=["strong", "weak"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "Grouped into bands, not ranked" in md
        assert "### Reproduced in earlier runs (1)" in md
        assert "### New in this run (1)" in md
        # The reproduced band must precede the new one.
        assert md.index("Reproduced in earlier runs") < md.index("New in this run")

    def test_bands_avoid_the_word_the_report_uses_for_within_run_agreement(
        self, tmp_path
    ):
        """Section 8 and Ensemble Width call cross-model agreement within one
        run "corroboration". Cross-run recurrence is a different axis, so the
        bands say "reproduced" — a reader must be able to tell which is meant.
        """
        write_history(tmp_path, KEY, [make_report(consensus=["strong"])] * 2)
        current = make_report(consensus=["strong", "weak"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        band_titles = [
            ln for ln in md.splitlines() if ln.startswith("### ") and "run" in ln
        ]
        assert band_titles
        assert not any("orroborat" in ln for ln in band_titles), band_titles

    def test_section_1_bands_by_pass_count_without_history(self, tmp_path):
        current = make_report(consensus=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "Flagged by two passes" in md

    def test_bookkeeping_fields_are_not_dumped_as_raw_bullets(self, tmp_path):
        write_history(tmp_path, KEY, [make_report(voice=["a"])])
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "Reproduced in:" not in md
        assert "Reproduced of:" not in md

    def test_a_report_never_annotated_still_renders(self):
        """The renderer must not require the reproducibility block.

        ``--replay`` of an old capture and any externally-held report predate
        the field entirely.
        """
        md = render_report_markdown(make_report(voice=["a"], consensus=["c"]))
        assert "## Reading this report" in md
        assert "Not measured for this draft" in md


class TestCalibrationMatchesDocs:
    def test_the_quoted_measurement_is_the_documented_one(self):
        """The calibration figure is quoted to users; it must not drift.

        If ``docs/CONFIGURATION.md`` is re-measured, this constant moves with
        it — the report would otherwise keep citing a number the project no
        longer stands behind.
        """
        from pathlib import Path

        root = Path(__file__).resolve()
        while not (root / "docs").is_dir():
            root = root.parent
        text = (root / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        assert (
            f"only {repro.CALIBRATION['reproduced_in_3_or_more']} of "
            f"{repro.CALIBRATION['distinct_findings']} distinct findings"
        ) in text


class TestRendererStaysInStepWithModule:
    def test_summary_table_covers_every_tracked_section(self):
        from ci_article_review import report_markdown

        tracked = set(repro._LIST_SECTIONS) | {"section_2_fact_check"}
        rendered = {key for key, _label in report_markdown._REPRODUCIBILITY_SECTIONS}
        assert rendered == tracked

    def test_excluded_fields_match_the_field_names_written(self):
        from ci_article_review import report_markdown

        assert set(report_markdown._REPRODUCIBILITY_FIELDS) == {
            repro.REPRODUCED_FIELD,
            repro.DENOMINATOR_FIELD,
        }


class TestDroppedFindings:
    """Findings every comparable prior run raised, and this one did not.

    The rest of the module can only annotate findings that are present. What a
    run *missed* is invisible by construction, and on measured history each run
    misses a great many — so the thresholds here are about keeping the list
    short enough to be read, not about whether the data exists.
    """

    def _dropped(self, current, tmp_path):
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        return current["reproducibility"]["dropped"]

    def test_a_finding_every_prior_run_raised_and_this_one_did_not(self, tmp_path):
        write_history(
            tmp_path, KEY, [make_report(voice=["gone"]), make_report(voice=["gone"])]
        )
        dropped = self._dropped(make_report(voice=["kept"]), tmp_path)
        assert dropped["total"] == 1
        entry = dropped["findings"][0]
        assert entry["section"] == "section_3_voice"
        assert entry["passage"] == "gone"
        assert (entry["raised_in"], entry["of"]) == (2, 2)

    def test_a_finding_still_present_is_not_dropped(self, tmp_path):
        write_history(
            tmp_path, KEY, [make_report(voice=["kept"]), make_report(voice=["kept"])]
        )
        assert self._dropped(make_report(voice=["kept"]), tmp_path)["total"] == 0

    def test_a_finding_only_some_prior_runs_raised_is_not_dropped(self, tmp_path):
        """Unanimity, not majority.

        Measured on the dc-environment cluster: "raised by at least one prior
        run" listed 424 findings and "at least half" listed 54, against 4 for
        unanimity. The looser rules print a backlog that grows with history.
        """
        write_history(
            tmp_path, KEY, [make_report(voice=["sometimes"]), make_report(voice=[])]
        )
        assert self._dropped(make_report(voice=[]), tmp_path)["total"] == 0

    def test_one_prior_run_is_never_enough(self, tmp_path):
        """At N=1 every threshold degenerates to "everything the other run found".

        That was 130 findings on real history — the run-to-run noise this
        module exists to describe, not a signal about this run.
        """
        write_history(tmp_path, KEY, [make_report(voice=["gone"])])
        assert self._dropped(make_report(voice=[]), tmp_path)["total"] == 0

    def test_no_comparable_history_drops_nothing(self, tmp_path):
        assert self._dropped(make_report(voice=["a"]), tmp_path)["total"] == 0

    def test_a_prior_run_that_lost_the_domain_cannot_break_unanimity(self, tmp_path):
        """Consistent with the reproduction denominators.

        A run whose voice passes all failed never had the chance to raise a
        voice finding, so it neither counts toward that section's denominator
        nor breaks another run's unanimity.
        """
        write_history(
            tmp_path,
            KEY,
            [
                make_report(voice=["gone"]),
                make_report(voice=["gone"]),
                make_report(voice=[], failures=["openai:voice_style"]),
            ],
        )
        entry = self._dropped(make_report(voice=[]), tmp_path)["findings"][0]
        assert (entry["raised_in"], entry["of"]) == (2, 2)

    def test_fact_check_buckets_are_named_separately(self, tmp_path):
        prior = make_report(fact_check={"contradicted": [{"claim": "c1"}]})
        write_history(tmp_path, KEY, [prior, prior])
        entry = self._dropped(make_report(), tmp_path)["findings"][0]
        assert entry["section"] == "section_2_fact_check"
        assert entry["bucket"] == "contradicted"

    def test_the_list_is_capped_and_the_remainder_counted(self, tmp_path):
        many = [f"finding {i}" for i in range(repro._MAX_DROPPED_LISTED + 5)]
        write_history(tmp_path, KEY, [make_report(voice=many), make_report(voice=many)])
        dropped = self._dropped(make_report(voice=[]), tmp_path)
        assert dropped["total"] == len(many)
        assert len(dropped["findings"]) == repro._MAX_DROPPED_LISTED

    def test_the_passage_is_shown_as_written_not_as_a_match_key(self, tmp_path):
        """Keys are lowercased and clipped; a reader needs the real sentence."""
        text = "These Conditions Produce Better Outcomes Than A Standard Park."
        write_history(
            tmp_path, KEY, [make_report(voice=[text]), make_report(voice=[text])]
        )
        entry = self._dropped(make_report(voice=[]), tmp_path)["findings"][0]
        assert entry["passage"] == text


class TestDroppedRendering:
    def test_the_block_names_what_was_missed(self, tmp_path):
        write_history(
            tmp_path, KEY, [make_report(voice=["gone"]), make_report(voice=["gone"])]
        )
        current = make_report(voice=[])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert "### What this run may have missed (1)" in md
        assert "Section 3, voice" in md
        assert "gone" in md

    def test_the_block_says_nothing_was_fixed(self, tmp_path):
        """The claim that makes the block safe to print.

        Comparability pins the draft, so a dropped finding cannot have been
        resolved between runs — and a reader must not infer that it was.
        """
        write_history(
            tmp_path, KEY, [make_report(voice=["gone"]), make_report(voice=["gone"])]
        )
        current = make_report(voice=[])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert "nothing below was fixed" in render_report_markdown(current)

    def test_no_block_when_nothing_was_dropped(self, tmp_path):
        current = make_report(voice=["a"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        assert "may have missed" not in render_report_markdown(current)

    def test_the_block_sits_above_the_findings(self, tmp_path):
        write_history(
            tmp_path, KEY, [make_report(voice=["gone"]), make_report(voice=["gone"])]
        )
        current = make_report(voice=[], consensus=["c"])
        repro.annotate(current, str(tmp_path), KEY, before_ts=NOW)
        md = render_report_markdown(current)
        assert md.index("may have missed") < md.index("SECTION 1")


class TestFindingIndexAndKeysAgree:
    def test_keys_are_derived_from_the_index(self):
        report = make_report(
            voice=["v"],
            argument=["a"],
            completeness=["c"],
            consensus=["s1"],
            fact_check={"outdated": [{"claim": "fc"}]},
        )
        index = repro.finding_index(report)
        assert repro.finding_keys(report) == {s: set(v) for s, v in index.items()}
        assert index["section_3_voice"] == {"v": "v"}
