"""Tests for the ci-markers CLI.

The library is covered in ci-core's test_text_markers.py; what is worth testing
here is the part that touches the author's files -- that a report never writes,
that --fix does not quietly rewrite every line ending, and that the labels tell
the truth about which pass removes what.
"""

import pytest

from ci_article_review.markers import (
    _context,
    _fixed_by,
    analyse,
    build_parser,
    main,
    to_json,
)
from ci_core.text_markers import scan

ZWSP = "\u200b"
NBSP = "\u00a0"
CYRILLIC_A = "а"


def write(path, text, newline=""):
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return path


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["ci-markers", *argv])
    return main()


class TestReportingIsReadOnly:
    def test_a_report_does_not_modify_the_file(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", f"The b{CYRILLIC_A}nk{ZWSP} — it's fine.")
        before = p.read_bytes()
        run(monkeypatch, str(p))
        assert p.read_bytes() == before

    def test_clean_file_says_so(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", "Plain ASCII, nothing to find.")
        run(monkeypatch, str(p))
        assert "No authorship markers found." in capsys.readouterr().out


class TestFix:
    def test_fix_removes_invisible_residue(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", f"The per{ZWSP}mit allows it.")
        run(monkeypatch, str(p), "--fix")
        assert p.read_text(encoding="utf-8") == "The permit allows it."

    def test_fix_keeps_visible_typography(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", f"The permit — as{ZWSP} written.")
        run(monkeypatch, str(p), "--fix")
        assert p.read_text(encoding="utf-8") == "The permit — as written."

    def test_aggressive_flattens_typography(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", "The permit — as written.")
        run(monkeypatch, str(p), "--fix", "--aggressive")
        assert p.read_text(encoding="utf-8") == "The permit -- as written."

    def test_crlf_line_endings_survive_a_fix(self, tmp_path, monkeypatch, capsys):
        # Path.read_text could not take newline="" until 3.13, and this project
        # supports 3.10 on a CRLF platform. Reading with translation would turn
        # every line ending in the draft into LF on write-back -- a diff across
        # the whole file for a one-character fix.
        p = write(tmp_path / "d.md", f"one{ZWSP}\r\ntwo\r\nthree\r\n")
        run(monkeypatch, str(p), "--fix")
        raw = p.read_bytes()
        assert raw.count(b"\r\n") == 3
        assert raw.count(b"\n") - raw.count(b"\r\n") == 0

    def test_lf_line_endings_are_not_converted_either(
        self, tmp_path, monkeypatch, capsys
    ):
        p = write(tmp_path / "d.md", f"one{ZWSP}\ntwo\n")
        run(monkeypatch, str(p), "--fix")
        assert b"\r\n" not in p.read_bytes()

    def test_nothing_to_remove_leaves_the_file_untouched(
        self, tmp_path, monkeypatch, capsys
    ):
        p = write(tmp_path / "d.md", "Plain ASCII.\r\n")
        before = p.read_bytes()
        run(monkeypatch, str(p), "--fix")
        assert p.read_bytes() == before


class TestExitCode:
    """Meant for a publish gate: fail on what is never innocent, not on style."""

    def test_clean_file_passes(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", "Plain ASCII text.")
        assert run(monkeypatch, str(p)) == 0

    def test_cosmetic_findings_alone_do_not_fail(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", f"An em dash — and a{NBSP}space.")
        assert run(monkeypatch, str(p)) == 0, "an em dash must not fail a build"

    def test_invisible_character_fails(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", f"The per{ZWSP}mit.")
        assert run(monkeypatch, str(p)) == 1

    def test_homoglyph_fails(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", f"The b{CYRILLIC_A}nk.")
        assert run(monkeypatch, str(p)) == 1

    def test_decode_damage_fails(self, tmp_path, monkeypatch, capsys):
        p = write(tmp_path / "d.md", "Content: %PDF-1.6 �� obj")
        assert run(monkeypatch, str(p)) == 1

    def test_missing_file_is_a_usage_error_not_a_finding(
        self, tmp_path, monkeypatch, capsys
    ):
        assert run(monkeypatch, str(tmp_path / "nope.md")) == 2

    def test_a_clean_file_does_not_clear_an_earlier_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        bad = write(tmp_path / "bad.md", f"a{ZWSP}b")
        good = write(tmp_path / "good.md", "plain")
        assert run(monkeypatch, str(bad), str(good)) == 1


class TestArgumentGuards:
    def test_aggressive_without_fix_is_rejected(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            run(monkeypatch, "--aggressive", "x.md")

    def test_no_paths_and_no_stdin_is_rejected(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            run(monkeypatch)

    def test_parser_documents_the_exit_code(self):
        assert "Exit code" in build_parser().epilog


class TestLabels:
    """`removable` says a replacement exists; it does not say which pass uses
    it. Reporting both the same way told the author a plain --fix would flatten
    their punctuation."""

    def test_invisible_is_removed_by_the_default_pass(self):
        assert _fixed_by(scan(f"a{ZWSP}b")[0]) == "removed by --fix"

    def test_lookalike_space_is_removed_by_the_default_pass(self):
        assert _fixed_by(scan(f"a{NBSP}b")[0]) == "removed by --fix"

    def test_typography_needs_aggressive(self):
        assert _fixed_by(scan("a — b")[0]) == "removed by --fix --aggressive"

    def test_homoglyph_needs_aggressive(self):
        assert _fixed_by(scan(f"b{CYRILLIC_A}nk")[0]) == "removed by --fix --aggressive"

    def test_decode_damage_is_reported_only(self):
        assert _fixed_by(scan("a�b")[0]) == "reported only"


class TestContext:
    def test_position_is_reported_as_line_and_column(self):
        text = "first line\nsecond line\nthird"
        assert _context(text, 11).startswith("line 2, col 1:")

    def test_context_does_not_run_past_the_end_of_the_line(self):
        assert "\n" not in _context("aaa\nbbb\nccc", 5)


class TestJson:
    def test_findings_are_serialisable(self, tmp_path):
        p = write(tmp_path / "d.md", f"The per{ZWSP}mit — it's fine.")
        payload = to_json(analyse(p))
        assert payload["serious"] == 1
        kinds = {f["kind"] for f in payload["findings"]}
        assert kinds == {"invisible", "typography"}

    def test_inventory_is_opt_in(self, tmp_path):
        p = write(tmp_path / "d.md", "cost €40")
        assert "inventory" not in to_json(analyse(p))
        assert (
            to_json(analyse(p), show_inventory=True)["inventory"][0]["codepoint"]
            == "U+20AC"
        )

    def test_inventory_reports_code_points_scan_has_no_verdict_on(self, tmp_path):
        # The forward-looking half: a character nothing recognises is still
        # counted, which is what makes a draft-to-draft diff meaningful.
        p = write(tmp_path / "d.md", "cost €40")
        entry = to_json(analyse(p), show_inventory=True)["inventory"][0]
        assert entry["marker_kind"] == ""


class TestTheRepoIsClean:
    """Point the scanner at the project's own source.

    This is not decoration. Writing the test fixtures for this feature, an NBSP
    typed as a literal arrived as a plain space and the assertion silently
    tested the wrong character -- an invisible character in source is invisible
    in review too. Source files carry markers as escapes for that reason, and
    this is what keeps them that way.

    Scoped to ``src`` because test files legitimately contain fixtures made of
    exactly what this forbids.
    """

    def test_no_invisible_or_damaged_characters_in_any_source_file(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3]
        offenders = []
        for path in sorted(root.glob("packages/*/src/**/*.py")):
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
            for f in scan(text):
                if f.kind in ("invisible", "anomaly"):
                    offenders.append(
                        f"{path.relative_to(root)}: {f.codepoint} {f.name} "
                        f"x{f.count} -- write it as an escape instead"
                    )
        assert offenders == []
