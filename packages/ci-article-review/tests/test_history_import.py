"""``ci-docs import scan`` and ``check`` (issue #420), on invented files only.

Every source is made under ``tmp_path`` with the invented slug ``example-article``
and invented text. Nothing here reads the real ``pipeline_history/`` or any real
article: the history root is always a directory under ``tmp_path``.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ci_article_review import documents_cli, history_import as hi

SLUG = "example-article"
OLD_KEY = "an-older-title-derived-key"
UTC = timezone.utc


def when(day, hh=0, mm=0, ss=0, month=3):
    return datetime(2026, month, day, hh, mm, ss, tzinfo=UTC)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Synthetic sources
# ---------------------------------------------------------------------------


def make_draft(marker):
    """A draft that shares nearly all its words with the others, plus a marker line."""
    lines = [
        f"Paragraph {i} discusses topic{i} with words{i} and detail{i}."
        for i in range(40)
    ]
    lines.append(f"Revision marker {marker} adds unique{marker}.")
    return "\n".join(lines) + "\n"


def handoff_text(draft):
    return (
        "DRAFT SUBMISSION HANDOFF\n"
        "Article: Example Article\n"
        "Publication: example\n"
        "Pipeline run: 1\n\n"
        "PRIMARY CLAIM\nAn invented claim.\n\n"
        "DRAFT\n" + draft
    )


def stamp(path, moment):
    seconds = moment.timestamp()
    os.utime(path, (seconds, seconds))


def put(directory, name, text, moment=None, newline="\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    if moment is not None:
        stamp(path, moment)
    return path


def put_run(history, key, number, moment, corrected, changes=None, tag=None):
    directory = history / key
    directory.mkdir(parents=True, exist_ok=True)
    tag = tag or moment.strftime("%Y%m%d_%H%M%S")
    path = directory / f"run_{number}_{tag}_report.json"
    report = {
        "generated": moment.isoformat(),
        "run_number": number,
        "article_title": "Example Article",
        "corrected_draft": corrected,
        "lt_corrections_applied": changes or [],
        "ensemble": {"cost_preset": "wide"},
        "cost_summary": {"total_usd": 0.5},
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    (directory / f"run_{number}_{tag}_review.md").write_text("review", encoding="utf-8")
    return path


class Sources:
    """The synthetic article: drafts D0..D4 over files, Drive and runs."""

    def __init__(self, base):
        self.base = base
        self.history = base / "history"
        self.files = base / "files"
        self.drive = base / "drive"
        self.recovered = base / "recovered"
        self.drafts = {n: make_draft(n) for n in (0, 1, 2, 3, 4, 7, 8)}
        self._build()

    def _build(self):
        d = self.drafts
        # D1 is only held by a handoff and a run that reviewed it after LanguageTool.
        d1_submitted = d[1].replace("Paragraph 3", "Paragraph 3 teh", 1)
        offset = d1_submitted.index("teh")
        d1_corrected = d1_submitted.replace("teh", "the", 1)
        self.d1_text = d1_submitted
        put(
            self.files, f"{SLUG}-v1-handoff.md", handoff_text(d1_submitted), when(5, 12)
        )
        put(
            self.files,
            f"{SLUG}-v1-metadata.md",
            "PRIMARY CLAIM\nAn invented claim.\n",
            when(5, 12, 30),
        )
        put_run(
            self.history,
            SLUG,
            1,
            when(5, 9),
            d1_corrected,
            [
                {
                    "rule_id": "TYPO",
                    "category": "TYPOS",
                    "original": "teh",
                    "replacement": "the",
                    "offset": offset,
                    "message": "m",
                }
            ],
        )
        # D2: a file (CRLF), a Drive copy created earlier, and two runs 10 minutes apart.
        put(self.files, f"{SLUG}-draft-v2.md", d[2], when(10, 11), newline="\r\n")
        drive_bytes = d[2].encode("utf-8")
        self.drive.mkdir(parents=True, exist_ok=True)
        (self.drive / f"{SLUG}-draft-v2.md").write_bytes(drive_bytes)
        self.manifest = [
            {
                "id": "drive-id-1",
                "name": f"{SLUG}-draft-v2.md",
                "createdTime": "2026-03-10T08:30:00.000Z",
                "modifiedTime": "2026-03-10T08:30:00.000Z",
                "md5Checksum": hashlib.md5(drive_bytes).hexdigest(),
            }
        ]
        self.write_manifest()
        put_run(self.history, SLUG, 2, when(10, 9), d[2])
        put_run(self.history, SLUG, 2, when(10, 9, 10), d[2])
        # D3: a file stamped 2 minutes before the run that reviewed it.
        put(self.files, f"{SLUG}-draft-r3.md", d[3], when(15, 8, 58))
        put_run(self.history, SLUG, 3, when(15, 9), d[3])
        # D4: only a run holds it, after LanguageTool changed one word.
        d4_submitted = d[4].replace("Paragraph 5", "Paragraph 5 recieve", 1)
        d4_offset = d4_submitted.index("recieve")
        put_run(
            self.history,
            SLUG,
            4,
            when(20, 9),
            d4_submitted.replace("recieve", "receive", 1),
            [
                {
                    "rule_id": "TYPO",
                    "category": "TYPOS",
                    "original": "recieve",
                    "replacement": "receive",
                    "offset": d4_offset,
                    "message": "m",
                }
            ],
        )
        # D0: an older run under a title-derived key.
        put_run(self.history, OLD_KEY, 1, when(1, 10), d[0])
        # Runs that must never be read.
        put_run(self.history, "_experiments", 1, when(2, 10), make_draft("x"))
        put_run(self.history / SLUG, "_replay", 7, when(2, 11), make_draft("y"))
        # Not documents, and not about the article.
        put(self.files, f"{SLUG}-push-citations.py", "print('x')\n")
        put(self.files, "build_posts.py", "print('x')\n")
        put(self.files, f"{SLUG}-posting-brief.txt", "brief\n")
        put(self.files, "unrelated-notes.md", "notes\n")
        put(self.files / f"{SLUG}-images", "photo.png", "png")
        put(
            self.files / f"{SLUG}-v5",
            "notes.md",
            "# Notes for v5\nPRIMARY CLAIM\nA claim.\n",
            when(22, 8),
        )

    def write_manifest(self):
        (self.drive / "drive-manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def add_recovered(self):
        d = self.drafts
        # Differs from D2 by one word: superseded by D2, matched by label v2.
        put(
            self.recovered,
            f"{SLUG}-draft-v2.md",
            d[2].replace("topic1 ", "topic1x ", 1),
            when(30),
        )
        # Differs from the run-only D4: superseded through the run number (r4).
        put(
            self.recovered,
            f"{SLUG}-draft-r4.md",
            d[4].replace("topic2 ", "topic2x ", 1),
            when(30),
        )
        # The same bytes as D3: it is D3, with a second source and no flag.
        put(self.recovered, f"{SLUG}-draft-r3-again.md", d[3], when(30))
        # Nothing else holds v7: kept, flagged, dated by its sidecar only.
        put(self.recovered, f"{SLUG}-draft-v7.md", d[7], when(30))
        (self.recovered / f"{SLUG}-draft-v7.md.json").write_text(
            json.dumps({"claimed_at": "2026-03-25T10:00:00Z", "note": "from the chat"}),
            encoding="utf-8",
        )
        # Nothing else holds r5 and no time is claimed: placed after r4 by its label.
        put(self.recovered, f"{SLUG}-draft-r5.md", d[8], when(30))

    def scan(self, **overrides):
        options = dict(
            history_keys=[SLUG, OLD_KEY],
            history_root=self.history,
            files=[self.files],
            drive_dir=self.drive,
        )
        options.update(overrides)
        return hi.scan(SLUG, **options)


@pytest.fixture
def sources(tmp_path):
    return Sources(tmp_path)


def events(plan):
    return [e for r in plan["rounds"] for e in r["events"]]


def drafts_of(plan):
    return [e for e in events(plan) if e["kind"] == "draft"]


def draft_with(plan, text):
    sha = hashlib.sha256(hi.normalise(text).encode("utf-8")).hexdigest()
    found = [
        e for e in drafts_of(plan) if e["sha256"] == sha and not e.get("superseded_by")
    ]
    assert len(found) == 1, f"expected one version, found {len(found)}"
    return found[0]


def types(event):
    return sorted(s["type"] for s in event["sources"])


# ---------------------------------------------------------------------------
# Classifying files
# ---------------------------------------------------------------------------


def test_kind_and_label_come_from_the_name_first(sources):
    plan, _ = sources.scan()
    by_name = {
        Path(s["path"]).name: e
        for e in events(plan)
        if e["kind"] != "run"
        for s in e["sources"]
    }
    assert by_name[f"{SLUG}-v1-handoff.md"]["kind"] == "handoff"
    assert by_name[f"{SLUG}-v1-handoff.md"]["label"] == "v1"
    assert by_name[f"{SLUG}-v1-metadata.md"]["kind"] == "metadata"
    assert by_name[f"{SLUG}-draft-r3.md"]["kind"] == "draft"
    assert by_name[f"{SLUG}-draft-r3.md"]["label"] == "r3"
    assert by_name[f"{SLUG}-draft-v2.md"]["label"] == "v2"


def test_a_folder_named_for_the_article_labels_and_admits_its_files(sources):
    plan, _ = sources.scan()
    event = next(
        e
        for e in events(plan)
        if e["kind"] != "run"
        and any(s["path"].endswith("v5/notes.md") for s in e["sources"])
    )
    assert event["label"] == "v5"
    assert event["kind"] == "metadata"  # by content: a PRIMARY CLAIM section, no DRAFT


def test_content_names_a_file_whose_name_says_nothing(tmp_path):
    files = tmp_path / "files"
    put(files, f"{SLUG}-a.md", handoff_text(make_draft(1)), when(5))
    put(files, f"{SLUG}-b.md", "PUBLICATION HANDOFF\nArticle: x\n", when(6))
    put(files, f"{SLUG}-c.md", "just prose\n", when(7))
    plan, _ = hi.scan(SLUG, history_root=tmp_path / "none", files=[files])
    kinds = {
        Path(e["sources"][0]["path"]).name: e["kind"]
        for e in events(plan)
        if e["kind"] != "run"
    }
    assert kinds[f"{SLUG}-a.md"] == "handoff"
    assert kinds[f"{SLUG}-b.md"] == "publication"
    assert kinds[f"{SLUG}-c.md"] == "unknown"
    unknown = next(e for e in events(plan) if e["kind"] == "unknown")
    assert unknown["include"] is False


def test_a_slug_containing_a_kind_word_does_not_misclassify(tmp_path):
    files = tmp_path / "files"
    put(files, "draft-review-guide-v2.md", make_draft(1), when(5))
    plan, _ = hi.scan(
        "draft-review-guide", history_root=tmp_path / "none", files=[files]
    )
    only = events(plan)[0]
    assert only["kind"] == "unknown"
    assert only["label"] == "v2"


def test_scripts_prompts_and_image_folders_are_excluded_as_not_documents(sources):
    plan, stats = sources.scan()
    why = {Path(x["path"]).name: x["why"] for x in plan["excluded"]}
    assert why[f"{SLUG}-push-citations.py"].startswith("not a document")
    assert why[f"{SLUG}-posting-brief.txt"].startswith("not a document")
    assert why[f"{SLUG}-images"].startswith("not a document")
    assert (
        "unrelated-notes.md" not in why and "build_posts.py" not in why
    )  # not about the article: counted, not listed
    assert stats["skipped_by_name"] == 2
    assert all(x["why"].startswith("not a document") for x in plan["excluded"])


def test_the_slug_must_stand_alone_in_a_name(tmp_path):
    files = tmp_path / "files"
    put(files, f"{SLUG}-draft-v1.md", make_draft(1), when(5))
    put(files, f"{SLUG}al-draft-v1.md", make_draft(2), when(6))
    plan, stats = hi.scan(SLUG, history_root=tmp_path / "none", files=[files])
    assert len(drafts_of(plan)) == 1
    assert stats["skipped_by_name"] == 1


def test_include_reads_a_file_whose_name_does_not_mention_the_article(tmp_path):
    files = tmp_path / "files"
    put(files, f"{SLUG}-draft-v1.md", make_draft(1), when(5))
    put(files, "final-copy-draft.md", make_draft(2), when(6))
    plan, _ = hi.scan(
        SLUG,
        history_root=tmp_path / "none",
        files=[files],
        include=["final-copy-draft.md"],
    )
    assert len(drafts_of(plan)) == 2


# ---------------------------------------------------------------------------
# De-duplication and timestamps
# ---------------------------------------------------------------------------


def test_one_text_in_two_places_is_one_version_with_both_sources(sources):
    plan, stats = sources.scan()
    d2 = draft_with(plan, sources.drafts[2])
    assert types(d2) == ["drive", "file", "run", "run"]
    assert stats["duplicates_removed"] >= 3
    # CRLF in the file and LF in Drive did not make two versions.
    assert len([e for e in drafts_of(plan) if e["sha256"] == d2["sha256"]]) == 1


def test_dedupe_takes_the_earliest_trusted_time(sources):
    plan, _ = sources.scan()
    d2 = draft_with(plan, sources.drafts[2])
    # Drive 08:30 beats the file's 11:00 and the run's 09:00.
    assert d2["at"] == iso(when(10, 8, 30))
    assert {s["time"] for s in d2["sources"]} == {
        iso(when(10, 8, 30)),
        iso(when(10, 11)),
        iso(when(10, 9)),
        iso(when(10, 9, 10)),
    }


def test_a_more_trusted_source_within_five_minutes_sets_the_time(sources):
    plan, _ = sources.scan()
    d3 = draft_with(plan, sources.drafts[3])
    # The file says 08:58, the run 09:00: the run is the pipeline's own clock.
    assert d3["at"] == iso(when(15, 9))


def test_a_less_trusted_source_further_than_five_minutes_does_not_win(tmp_path):
    files = tmp_path / "files"
    history = tmp_path / "history"
    put(files, f"{SLUG}-draft-r1.md", make_draft(1), when(5, 8, 0))
    put_run(history, SLUG, 1, when(5, 9), make_draft(1))
    plan, _ = hi.scan(SLUG, history_root=history, files=[files])
    assert draft_with(plan, make_draft(1))["at"] == iso(when(5, 8))


def test_a_run_is_linked_to_the_submitted_text_it_reviewed(sources):
    plan, _ = sources.scan()
    d1 = draft_with(plan, sources.d1_text)
    assert types(d1) == ["file", "run"]
    run_source = next(s for s in d1["sources"] if s["type"] == "run")
    assert run_source["lt_undone"] is True
    run_event = next(
        e
        for e in events(plan)
        if e["kind"] == "run" and e["run"] == 1 and e["history_key"] == SLUG
    )
    assert run_event["draft"] == d1["id"]
    assert d1["at"] == iso(when(5, 9))


def test_a_run_whose_submitted_draft_is_missing_gets_a_synthetic_version(sources):
    plan, _ = sources.scan()
    d4 = next(
        e
        for e in drafts_of(plan)
        if e.get("note", "").startswith("LanguageTool-corrected")
    )
    assert d4["note"] == "LanguageTool-corrected; the submitted bytes were not found"
    assert types(d4) == ["run"]
    assert d4["sources"][0]["part"] == "corrected_draft"
    assert d4["at"] == iso(when(20, 9))


def test_near_duplicate_versions_are_listed_not_merged(sources):
    plan, _ = sources.scan()
    ids = {e["id"] for e in drafts_of(plan)}
    assert plan["near_duplicates"]
    for first, second in plan["near_duplicates"]:
        assert first in ids and second in ids and first != second
    # Every version is still its own event.
    assert len({e["sha256"] for e in drafts_of(plan)}) == len(drafts_of(plan))


def test_a_title_derived_history_key_is_read_too(sources):
    plan, _ = sources.scan()
    keys = {e["history_key"] for e in events(plan) if e["kind"] == "run"}
    assert keys == {SLUG, OLD_KEY}
    assert plan["history_keys"] == [SLUG, OLD_KEY]
    only, _ = sources.scan(history_keys=[SLUG])
    assert {e["history_key"] for e in events(only) if e["kind"] == "run"} == {SLUG}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_experiment_directories_are_never_read(sources):
    plan, _ = sources.scan()
    reports = [e["report"] for e in events(plan) if e["kind"] == "run"]
    assert reports
    assert not any("_experiments" in r or "_replay" in r for r in reports)
    # Their drafts (made in the fixture, under _experiments/ and <key>/_replay/)
    # did not become versions either.
    shas = {e["sha256"] for e in drafts_of(plan)}
    for name in ("x", "y"):
        unread = hashlib.sha256(hi.normalise(make_draft(name)).encode()).hexdigest()
        assert unread not in shas


@pytest.mark.parametrize(
    "key", ["_experiments", "_replay", "_nested", "../outside", "a/b"]
)
def test_a_history_key_cannot_name_an_unread_or_outside_directory(sources, key):
    with pytest.raises(hi.ImportScanError):
        sources.scan(history_keys=[key])


def test_runs_on_one_draft_close_together_are_marked_not_excluded(sources):
    plan, _ = sources.scan()
    runs = [e for e in events(plan) if e["kind"] == "run"]
    d2 = draft_with(plan, sources.drafts[2])
    twins = [r for r in runs if r["draft"] == d2["id"]]
    assert len(twins) == 2
    assert all(r["suspect"] == "experiment?" for r in twins)
    assert all(r["include"] is True for r in twins)
    assert "2 runs on this draft within 30 minutes" in twins[0]["suspect_why"]


def test_a_run_whose_draft_matches_no_other_source_is_marked(sources):
    plan, stats = sources.scan()
    runs = {(e["history_key"], e["run"]): e for e in events(plan) if e["kind"] == "run"}
    assert runs[(SLUG, 4)]["suspect"] == "experiment?"
    assert "matches no other source" in runs[(SLUG, 4)]["suspect_why"]
    # A run on a draft a file also holds, with no neighbour, is not suspect.
    assert runs[(SLUG, 1)]["suspect"] is None
    assert runs[(SLUG, 3)]["suspect"] is None
    assert stats["suspect_runs"] == 4  # both D2 twins, D4 and the old key's D0


def test_a_run_event_carries_its_facts_and_files(sources):
    plan, _ = sources.scan()
    run = next(e for e in events(plan) if e["kind"] == "run" and e["run"] == 3)
    assert run["preset"] == "wide"
    assert run["cost_usd"] == 0.5
    assert run["report"].endswith("_report.json")
    assert any(f.endswith("_review.md") for f in run["files"])
    assert run["at"] == iso(when(15, 9))


# ---------------------------------------------------------------------------
# Reproductions (--recovered)
# ---------------------------------------------------------------------------


def recovered_scan(sources):
    sources.add_recovered()
    return sources.scan(recovered=sources.recovered)


def test_a_reproduction_is_superseded_by_a_files_copy_of_its_version(sources):
    plan, _ = recovered_scan(sources)
    d2 = draft_with(plan, sources.drafts[2])
    reproduction = next(
        e for e in drafts_of(plan) if e.get("superseded_by") == d2["id"]
    )
    assert reproduction["include"] is False
    assert reproduction["recovered"] is True
    assert types(reproduction) == ["recovered"]


def test_a_reproduction_is_superseded_by_the_runs_copy_through_the_run_number(sources):
    plan, _ = recovered_scan(sources)
    d4 = next(
        e
        for e in drafts_of(plan)
        if e.get("note", "").startswith("LanguageTool-corrected")
    )
    assert any(e.get("superseded_by") == d4["id"] for e in drafts_of(plan))


def test_a_reproduction_with_the_same_bytes_is_that_version(sources):
    plan, _ = recovered_scan(sources)
    d3 = draft_with(plan, sources.drafts[3])
    assert types(d3) == ["file", "recovered", "run"]
    assert "recovered" not in d3
    assert d3["at"] == iso(when(15, 9))  # the reproduction's file time played no part


def test_a_reproduction_nothing_else_holds_is_kept_and_flagged(sources):
    plan, stats = recovered_scan(sources)
    d7 = draft_with(plan, sources.drafts[7])
    assert d7["recovered"] is True
    assert d7["include"] is True
    assert "superseded_by" not in d7
    assert d7["label"] == "v7"
    assert stats["recovered_kept"] == 2


def test_a_reproduction_is_dated_by_its_claim_never_by_its_file(sources):
    plan, _ = recovered_scan(sources)
    d7 = draft_with(plan, sources.drafts[7])
    assert d7["at"] == "2026-03-25T10:00:00Z"
    assert d7["sources"][0]["time"] == "2026-03-25T10:00:00Z"
    # Its file was stamped March 30; that date appears nowhere in the event.
    assert "2026-03-30" not in json.dumps(d7)


def test_an_unclaimed_reproduction_is_placed_by_its_label(sources):
    plan, _ = recovered_scan(sources)
    d8 = draft_with(plan, sources.drafts[8])
    assert d8["recovered"] is True
    assert d8["at"] == iso(
        when(20, 9, 0, 1)
    )  # one second after r4, the nearest lower label
    assert "after r4" in d8["placement"]


def test_a_reproduction_with_no_basis_for_a_time_has_none_and_check_says_so(tmp_path):
    recovered = tmp_path / "recovered"
    put(recovered, f"{SLUG}-draft-notes.md", make_draft(9), when(30))
    plan, _ = hi.scan(SLUG, history_root=tmp_path / "none", recovered=recovered)
    only = events(plan)[0]
    assert only["at"] is None and only["recovered"] is True
    problems = hi.check_plan(plan)
    assert any("no usable time" in p for p in problems)


def test_the_timeline_marks_reproductions(sources):
    plan, _ = recovered_scan(sources)
    text = "\n".join(hi.format_timeline(plan))
    assert "recovered" in text
    assert "superseded by" in text


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------


def round_of(plan, event_id):
    return next(
        r["round"]
        for r in plan["rounds"]
        if any(e["id"] == event_id for e in r["events"])
    )


def test_a_new_round_starts_at_each_submitted_draft(sources):
    plan, _ = sources.scan()
    starts = [r["events"][0] for r in plan["rounds"]]
    # D0, D1, D2, D3, D4, and the folder's v5 notes (a metadata file, so no round of its own).
    assert [s["kind"] for s in starts] == ["draft"] * 5
    assert len(plan["rounds"]) == 5
    assert [r["round"] for r in plan["rounds"]] == [1, 2, 3, 4, 5]


def test_handoff_and_metadata_ride_with_the_nearest_draft_in_their_round(sources):
    plan, _ = sources.scan()
    d1 = draft_with(plan, sources.d1_text)
    handoff = next(e for e in events(plan) if e["kind"] == "handoff")
    metadata = next(
        e for e in events(plan) if e["kind"] == "metadata" and e["label"] == "v1"
    )
    assert round_of(plan, handoff["id"]) == round_of(plan, d1["id"])
    assert handoff["attached_to"] == d1["id"]
    assert metadata["attached_to"] == d1["id"]


def test_an_unsubmitted_draft_is_a_revision_inside_the_earlier_round(tmp_path):
    files = tmp_path / "files"
    put(files, f"{SLUG}-draft-v1.md", make_draft(1), when(5))
    put(
        files, f"{SLUG}-draft.md", make_draft(4), when(6, 12)
    )  # no label, no run, no Drive
    put(files, f"{SLUG}-draft-v3.md", make_draft(3), when(7))
    plan, _ = hi.scan(SLUG, history_root=tmp_path / "none", files=[files])
    first, second = plan["rounds"]
    labels = [
        (e["label"], e.get("submitted"))
        for e in first["events"]
        if e["kind"] == "draft"
    ]
    assert labels == [("v1", True), (None, False)]
    assert [e["label"] for e in second["events"] if e["kind"] == "draft"] == ["v3"]


def test_accepted_at_is_a_second_before_the_next_round_and_the_last_events_for_the_last(
    sources,
):
    plan, _ = sources.scan()
    rounds = plan["rounds"]
    for current, following in zip(rounds, rounds[1:]):
        first_next = min(e["at"] for e in following["events"] if e["at"])
        assert current["accepted_at"] == iso(
            datetime.strptime(first_next, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            - timedelta(seconds=1)
        ) or current["accepted_at"] >= max(e["at"] for e in current["events"])
    last = rounds[-1]
    assert last["accepted_at"] == max(e["at"] for e in last["events"])
    # And never before the round's own last event.
    for group in rounds:
        assert group["accepted_at"] >= max(e["at"] for e in group["events"])


def test_events_in_a_round_are_in_time_order_and_ids_are_unique(sources):
    plan, _ = recovered_scan(sources)
    seen = set()
    for group in plan["rounds"]:
        times = [e["at"] for e in group["events"]]
        assert times == sorted(times)
        for event in group["events"]:
            assert event["id"] not in seen
            seen.add(event["id"])


def test_a_label_on_a_draft_in_another_round_is_hinted(tmp_path):
    files = tmp_path / "files"
    put(files, f"{SLUG}-draft-v1.md", make_draft(1), when(5))
    put(files, f"{SLUG}-draft-v2.md", make_draft(2), when(10))
    # The v1 metadata file was copied after v2 existed: placed by time, hinted by label.
    put(files, f"{SLUG}-v1-metadata.md", "PRIMARY CLAIM\nA claim.\n", when(12))
    plan, _ = hi.scan(SLUG, history_root=tmp_path / "none", files=[files])
    meta = next(e for e in events(plan) if e["kind"] == "metadata")
    assert round_of(plan, meta["id"]) == 2
    assert "label v1 is on a draft in round 1" in meta["hint"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_drive_file_whose_md5_differs_from_the_manifest_is_refused(sources):
    (sources.drive / f"{SLUG}-draft-v2.md").write_bytes(
        b"altered after the manifest was written\n"
    )
    with pytest.raises(hi.ImportScanError, match="md5Checksum"):
        sources.scan()


def test_a_drive_directory_without_a_manifest_is_refused(sources):
    (sources.drive / "drive-manifest.json").unlink()
    with pytest.raises(hi.ImportScanError, match="drive-manifest.json"):
        sources.scan()


def test_a_manifest_entry_without_a_created_time_is_refused(sources):
    del sources.manifest[0]["createdTime"]
    sources.write_manifest()
    with pytest.raises(hi.ImportScanError, match="createdTime"):
        sources.scan()


def test_a_drive_file_missing_from_the_manifest_is_listed_not_read(sources):
    put(sources.drive, f"{SLUG}-draft-v9.md", make_draft(9))
    plan, _ = sources.scan()
    assert any(
        x["path"].endswith("-draft-v9.md") and "not in drive-manifest.json" in x["why"]
        for x in plan["excluded"]
    )
    assert not any(hi.normalise(make_draft(9)) == "" for _ in [0])


def test_out_inside_the_repository_is_refused(sources):
    inside = Path(hi.__file__).resolve().parents[4] / "plan-that-must-not-exist.json"
    assert not inside.exists()
    with pytest.raises(
        hi.ImportScanError, match="inside the content-intelligence checkout"
    ):
        hi.refuse_out_inside_checkout(inside)
    plan, _ = sources.scan()
    with pytest.raises(hi.ImportScanError):
        hi.write_plan(plan, inside)
    assert not inside.exists()


def test_the_cli_refuses_an_out_inside_the_repository_before_reading(sources, capsys):
    inside = Path(hi.__file__).resolve().parents[4] / "plan-that-must-not-exist.json"
    lines = []
    code = documents_cli.main(
        [
            "import",
            "scan",
            SLUG,
            "--history-root",
            str(sources.history),
            "--out",
            str(inside),
        ],
        out=lines.append,
    )
    assert code == 1
    assert "inside the content-intelligence checkout" in capsys.readouterr().err
    assert not inside.exists()
    assert lines == []


def test_the_main_checkout_is_off_limits_too(sources, monkeypatch):
    main = sources.base / "main-checkout"
    (main / "private").mkdir(parents=True)
    monkeypatch.setattr(hi, "checkout_roots", lambda: [sources.base / "worktree", main])
    with pytest.raises(hi.ImportScanError):
        hi.refuse_out_inside_checkout(main / "private" / "plan.json")
    hi.refuse_out_inside_checkout(sources.base / "elsewhere" / "plan.json")


def test_nothing_found_is_an_error_not_an_empty_plan(tmp_path):
    with pytest.raises(hi.ImportScanError, match="nothing found"):
        hi.scan(SLUG, history_root=tmp_path / "none", files=[])


def test_a_missing_history_key_directory_is_a_warning(tmp_path):
    files = tmp_path / "files"
    put(files, f"{SLUG}-draft-v1.md", make_draft(1), when(5))
    plan, stats = hi.scan(
        SLUG,
        history_keys=[SLUG, "no-such-key"],
        history_root=tmp_path / "h",
        files=[files],
    )
    assert any("no-such-key" in w for w in plan["warnings"])
    assert stats["warnings"]


# ---------------------------------------------------------------------------
# The plan file, the timeline and check
# ---------------------------------------------------------------------------


def test_the_plan_is_byte_identical_across_two_scans(sources, tmp_path):
    out1, out2 = tmp_path / "out" / "one.json", tmp_path / "out" / "two.json"
    sources.add_recovered()
    plan1, _ = sources.scan(recovered=sources.recovered)
    plan2, _ = sources.scan(recovered=sources.recovered)
    hi.write_plan(plan1, out1)
    hi.write_plan(plan2, out2)
    assert out1.read_bytes() == out2.read_bytes()
    assert b"\r" not in out1.read_bytes()
    data = json.loads(out1.read_text(encoding="utf-8"))
    assert list(data) == sorted(data)
    assert data["applied"] == {}
    assert data["version"] == 1


def test_the_plan_holds_paths_and_hashes_never_text(sources, tmp_path):
    plan, _ = sources.scan()
    blob = json.dumps(plan)
    assert (
        "Paragraph" not in blob
        and "unique" not in blob
        and "Revision marker" not in blob
    )


def test_cli_scan_prints_a_timeline_without_contents_and_writes_the_plan(
    sources, tmp_path
):
    out = tmp_path / "private" / "plan.json"
    lines = []
    code = documents_cli.main(
        [
            "import",
            "scan",
            SLUG,
            "--history-key",
            SLUG,
            "--history-key",
            OLD_KEY,
            "--history-root",
            str(sources.history),
            "--files",
            str(sources.files),
            "--drive-dir",
            str(sources.drive),
            "--out",
            str(out),
        ],
        out=lines.append,
    )
    printed = "\n".join(lines)
    assert code == 0
    assert out.is_file()
    assert "round" in printed and "accepted_at" in printed
    assert "Excluded (not events):" in printed
    assert "--history-root" in printed
    assert "Paragraph" not in printed and "Revision marker" not in printed


def test_cli_reports_where_the_history_root_came_from(sources, tmp_path, monkeypatch):
    monkeypatch.setenv("CI_HISTORY_ROOT", str(sources.history))
    lines = []
    code = documents_cli.main(
        [
            "import",
            "scan",
            SLUG,
            "--history-key",
            SLUG,
            "--out",
            str(tmp_path / "p" / "plan.json"),
        ],
        out=lines.append,
    )
    assert code == 0
    assert any("$CI_HISTORY_ROOT" in line for line in lines)


def test_the_default_history_root_is_the_main_checkouts_not_the_working_directory(
    monkeypatch, tmp_path
):
    main = tmp_path / "main"
    main.mkdir()
    monkeypatch.setattr(hi, "checkout_roots", lambda: [tmp_path / "worktree", main])
    root, why = hi.default_history_root({})
    assert root == main / "pipeline_history"
    assert why == "the main checkout"
    root, why = hi.default_history_root({"CI_HISTORY_ROOT": str(tmp_path / "shared")})
    assert root == tmp_path / "shared" and why == "$CI_HISTORY_ROOT"
    monkeypatch.setattr(hi, "checkout_roots", lambda: [])
    root, why = hi.default_history_root({})
    assert root == Path.cwd() / "pipeline_history" and "working directory" in why


def test_checkout_roots_finds_this_checkout():
    roots = hi.checkout_roots()
    assert roots
    assert (Path(hi.__file__).resolve().parents[4] / "packages").is_dir()
    assert roots[0].resolve() == Path(hi.__file__).resolve().parents[4]


@pytest.fixture
def written(sources, tmp_path):
    plan, _ = sources.scan()
    path = tmp_path / "private" / "plan.json"
    hi.write_plan(plan, path)
    return path, sources


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_check_accepts_an_unedited_plan(written):
    path, _ = written
    assert hi.check_plan(load(path)) == []


def test_check_runs_through_the_cli_and_prints_the_timeline(written):
    path, _ = written
    lines = []
    assert documents_cli.main(["import", "check", str(path)], out=lines.append) == 0
    assert any("accepted_at" in line for line in lines)
    assert lines[-1].endswith(": ok")


def test_check_accepts_the_authors_edits(written):
    path, _ = written
    plan = load(path)
    run = next(e for e in events(plan) if e["kind"] == "run" and e["suspect"])
    run["include"] = False  # the author drops an experiment
    plan["rounds"][0]["accepted_at"] = "2026-03-04T00:00:00Z"
    assert hi.check_plan(plan) == []


def test_check_catches_an_out_of_order_edit_within_a_round(written):
    path, _ = written
    plan = load(path)
    group = next(r for r in plan["rounds"] if len(r["events"]) > 2)
    group["events"][-1]["at"] = "2026-01-01T00:00:00Z"
    problems = hi.check_plan(plan)
    assert any("earlier than the event before it" in p for p in problems)


def test_check_catches_rounds_out_of_order(written):
    path, _ = written
    plan = load(path)
    plan["rounds"][0]["accepted_at"] = "2027-01-01T00:00:00Z"
    problems = hi.check_plan(plan)
    assert any("starts before the previous round was accepted" in p for p in problems)
    plan = load(path)
    plan["rounds"][1]["round"] = 1
    assert any("is not after round" in p for p in hi.check_plan(plan))


def test_check_catches_an_accepted_at_before_the_rounds_last_event(written):
    path, _ = written
    plan = load(path)
    plan["rounds"][-1]["accepted_at"] = "2026-01-01T00:00:00Z"
    assert any(
        "accepted_at is before its last included event" in p
        for p in hi.check_plan(plan)
    )


def test_check_catches_a_missing_source_file(written):
    path, sources = written
    (sources.files / f"{SLUG}-v1-metadata.md").unlink()
    problems = hi.check_plan(load(path))
    assert any("is missing" in p and "no source file is present" in p for p in problems)


def test_check_catches_a_changed_source_file(written):
    path, sources = written
    put(
        sources.files,
        f"{SLUG}-v1-metadata.md",
        "PRIMARY CLAIM\nChanged since the scan.\n",
    )
    problems = hi.check_plan(load(path))
    assert any("has changed since the scan" in p for p in problems)


def test_check_is_content_not_mtime_so_a_touched_file_is_fine(written):
    path, sources = written
    stamp(sources.files / f"{SLUG}-draft-r3.md", when(28, 1))
    assert hi.check_plan(load(path)) == []


def test_check_survives_one_missing_copy_when_another_source_holds_the_version(written):
    path, sources = written
    (sources.files / f"{SLUG}-draft-v2.md").unlink()  # the Drive copy remains
    assert hi.check_plan(load(path)) == []


def test_check_ignores_excluded_events(written):
    path, sources = written
    plan = load(path)
    meta = next(
        e for e in events(plan) if e["kind"] == "metadata" and e["label"] == "v1"
    )
    meta["include"] = False
    (sources.files / f"{SLUG}-v1-metadata.md").unlink()
    assert hi.check_plan(plan) == []


def test_check_catches_a_run_report_that_changed(written):
    path, sources = written
    report = next(sources.history.glob(f"{SLUG}/run_3_*_report.json"))
    data = json.loads(report.read_text(encoding="utf-8"))
    data["corrected_draft"] += "An added sentence.\n"
    report.write_text(json.dumps(data), encoding="utf-8")
    assert any("has changed since the scan" in p for p in hi.check_plan(load(path)))


def test_check_fails_the_cli_on_a_problem(written, capsys):
    path, _ = written
    plan = load(path)
    plan["rounds"][-1]["accepted_at"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(plan), encoding="utf-8")
    lines = []
    assert documents_cli.main(["import", "check", str(path)], out=lines.append) == 1
    assert any(line.startswith("problem:") for line in lines)
    assert "problem(s)" in capsys.readouterr().err


def test_check_rejects_something_that_is_not_a_plan(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert documents_cli.main(["import", "check", str(bad)], out=lambda *_: None) == 1
    assert hi.check_plan({"version": 99, "rounds": []}) == ["not a version 1 plan"]
    assert hi.check_plan({"version": 1, "rounds": []}) == ["the plan has no rounds"]


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------


def test_normalise_makes_line_endings_and_trailing_space_irrelevant():
    assert hi.normalise("a  \r\nb\t\r\n\r\n") == hi.normalise("a\nb") == "a\nb"
    assert hi.normalise("\N{ZERO WIDTH NO-BREAK SPACE}\n\na\n") == "a"
    assert hi.normalise("a\n\nb") != hi.normalise("a\nb")


def test_undo_lt_corrections_with_changes_of_different_lengths():
    original = "We recieve the the data teh day."
    changes = [
        {
            "original": "recieve",
            "replacement": "receive",
            "offset": original.index("recieve"),
        },
        {
            "original": "the the",
            "replacement": "the",
            "offset": original.index("the the"),
        },
        {"original": "teh", "replacement": "the", "offset": original.index("teh")},
    ]
    corrected = "We receive the data the day."
    assert hi.undo_lt_corrections(corrected, changes) == original
    assert hi.undo_lt_corrections(corrected, list(reversed(changes))) == original


def test_undo_lt_corrections_refuses_to_guess():
    changes = [{"original": "teh", "replacement": "the", "offset": 3}]
    assert hi.undo_lt_corrections("A big the day.", changes) is None
    assert hi.undo_lt_corrections("xyz", [{"original": "a"}]) is None
    assert hi.undo_lt_corrections("abc", []) == "abc"


def test_a_run_whose_corrections_do_not_undo_falls_back_to_a_synthetic_version(
    tmp_path,
):
    history = tmp_path / "history"
    put_run(
        history,
        SLUG,
        1,
        when(5, 9),
        make_draft(1),
        [{"original": "zzz", "replacement": "qqq", "offset": 5}],
    )
    plan, _ = hi.scan(SLUG, history_root=history)
    draft = drafts_of(plan)[0]
    assert draft["note"].startswith("LanguageTool-corrected")
    assert "lt_undone" not in draft["sources"][0]


def test_a_run_with_no_corrections_that_no_file_holds_says_so(tmp_path):
    history = tmp_path / "history"
    put_run(history, SLUG, 1, when(5, 9), make_draft(1))
    plan, _ = hi.scan(SLUG, history_root=history)
    assert (
        drafts_of(plan)[0]["note"]
        == "from the run's saved draft; no file holds this version"
    )


def test_a_report_without_a_draft_still_becomes_a_suspect_run(tmp_path):
    history = tmp_path / "history"
    put_run(history, SLUG, 1, when(5, 9), "")
    plan, _ = hi.scan(SLUG, history_root=history)
    run = events(plan)[0]
    assert run["kind"] == "run" and run["draft"] is None
    assert run["suspect"] == "experiment?"


def test_an_unreadable_report_is_listed_as_excluded(tmp_path):
    history = tmp_path / "history"
    put_run(history, SLUG, 1, when(5, 9), make_draft(1))
    (history / SLUG / "run_2_20260305_100000_report.json").write_text(
        "{not json", encoding="utf-8"
    )
    plan, _ = hi.scan(SLUG, history_root=history)
    assert any("unreadable report" in x["why"] for x in plan["excluded"])


def test_a_report_without_generated_falls_back_to_its_file_name(tmp_path):
    history = tmp_path / "history"
    path = put_run(history, SLUG, 1, when(5, 9), make_draft(1), tag="20260305_091500")
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["generated"]
    path.write_text(json.dumps(data), encoding="utf-8")
    plan, _ = hi.scan(SLUG, history_root=history)
    assert events(plan)[-1]["at"] == "2026-03-05T09:15:00Z"


def test_undo_inverts_what_the_real_languagetool_adapter_wrote():
    from ci_article_review.adapters.grammar.languagetool import apply_corrections

    original = "We recieve teh data, and and then we goes home. Their was a typo."
    spans = [
        ("recieve", "receive"),
        ("teh", "the"),
        ("and and", "and"),
        ("goes", "go"),
        ("Their", "There"),
    ]
    matches = [
        {
            "offset": original.index(old),
            "length": len(old),
            "rule": {"id": "R", "category": {"id": "TYPOS"}},
            "replacements": [{"value": new}],
        }
        for old, new in spans
    ]
    corrected, change_log = apply_corrections(original, matches, {"TYPOS"}, set())
    assert corrected != original and len(change_log) == len(spans)
    assert hi.undo_lt_corrections(corrected, change_log) == original


# ---------------------------------------------------------------------------
# research/ and the placement of metadata (PR #435)
# ---------------------------------------------------------------------------


def test_a_research_folder_gives_its_drafts_and_metadata_and_loses_its_notes(tmp_path):
    files = tmp_path / "files"
    history = tmp_path / "history"
    history.mkdir()
    older, newer = make_draft(1), make_draft(2)
    put(files, f"{SLUG}-draft-r2.md", newer, when(5, 10))
    put(
        files / "research",
        f"{SLUG}-draft-r1-superseded-2026-03-02.md",
        older,
        when(2, 10),
    )
    put(
        files / "research",
        f"{SLUG}-metadata-r1-superseded-2026-03-02.md",
        "PRIMARY CLAIM\nAn invented claim.\n",
        when(2, 10, 5),
    )
    put(files / "research", f"{SLUG}-notes.md", make_draft(7), when(2, 11))
    put(files / "research", "comparables-batch2.md", make_draft(8), when(2, 12))
    put(files / "research", "v4-changes-and-research-log.md", "log\n", when(2, 13))
    plan, _ = hi.scan(SLUG, history_root=history, files=[files])
    kinds = [(e["kind"], e["include"]) for e in events(plan)]
    assert kinds.count(("draft", True)) == 2
    assert ("metadata", True) in kinds
    assert draft_with(plan, older)["include"] is True
    left = {Path(item["path"]).name: item["why"] for item in plan["excluded"]}
    assert set(left) == {
        f"{SLUG}-notes.md",
        "comparables-batch2.md",
        "v4-changes-and-research-log.md",
    }
    assert all(why == hi._RESEARCH_WHY for why in left.values())
    assert not any("not read" in why for why in left.values())


def metadata_before_its_draft(tmp_path, *, with_run=False):
    files = tmp_path / "files"
    history = tmp_path / "history"
    history.mkdir()
    put(files, f"{SLUG}-draft-r1.md", make_draft(1), when(1, 10))
    put(
        files,
        f"{SLUG}-metadata-r2.md",
        "PRIMARY CLAIM\nAn invented claim.\n",
        when(3, 9, 58),
    )
    put(files, f"{SLUG}-draft-r2.md", make_draft(2), when(3, 10))
    if with_run:
        put_run(history, SLUG, 1, when(3, 9, 59), make_draft(1))
    return hi.scan(SLUG, history_root=history, files=[files])[0]


def test_metadata_saved_shortly_before_its_draft_goes_into_the_drafts_round(tmp_path):
    plan = metadata_before_its_draft(tmp_path)
    assert len(plan["rounds"]) == 2
    first, second = (r["events"] for r in plan["rounds"])
    assert [e["kind"] for e in first] == ["draft"]
    assert [e["kind"] for e in second] == ["metadata", "draft"]
    assert "hint" not in second[0]
    assert "starts round 2" in second[0]["placement"]
    assert hi.check_plan(plan) == []
    # The round it left is accepted just before the metadata, as before.
    assert plan["rounds"][0]["accepted_at"] <= second[0]["at"]


def test_metadata_with_a_run_between_it_and_its_draft_stays_with_its_hint(tmp_path):
    plan = metadata_before_its_draft(tmp_path, with_run=True)
    first = plan["rounds"][0]["events"]
    assert [e["kind"] for e in first] == ["draft", "metadata", "run"]
    meta = first[1]
    assert "label r2 is on a draft in round 2" in meta["hint"]
    assert "placement" not in meta
