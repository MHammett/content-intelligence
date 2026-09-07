"""Report and optionally strip machine-authorship markers from a draft.

The detection lives in ``ci_core.text_markers``; this is the part you point at a
file. It defaults to reporting and writes nothing without ``--fix``, because the
common use is checking what came back from a revise step before it goes into the
article, and a tool that edits on sight cannot be used for that.

Typical use, on a draft just pasted back from a chat model::

    ci-markers draft.md                    # what is in there
    ci-markers draft.md --fix              # remove the invisible residue
    ci-markers draft.md --fix --aggressive # also flatten typography to ASCII
    ci-markers draft.md --inventory        # every non-ASCII code point, no verdicts

The exit code is meant for a publish gate: non-zero when the file contains
something that is never innocent -- an invisible character, a mixed-script word,
or text that failed to decode -- and zero when the only findings are cosmetic.
An em dash should not fail a build.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from ci_core.console import force_utf8_stdio
from ci_core.text_markers import (
    KIND_ANOMALY,
    KIND_HOMOGLYPH,
    KIND_INVISIBLE,
    KIND_TYPOGRAPHY,
    KIND_WHITESPACE,
    SAFE_KINDS,
    inventory,
    sanitize,
    scan,
)

# Every finding this prints quotes a character out of the author's draft, and
# the report is mostly made of characters cp1252 cannot encode -- which is the
# whole subject. Without this the tool dies printing its own first finding.
force_utf8_stdio()

log = logging.getLogger(__name__)

# Kinds that fail the exit code. Cosmetic findings (typography, lookalike
# spaces) are worth reporting and not worth blocking on.
SERIOUS_KINDS = frozenset({KIND_INVISIBLE, KIND_ANOMALY, KIND_HOMOGLYPH})

_KIND_HEADINGS = {
    KIND_INVISIBLE: "Invisible characters (render as nothing; never innocent in prose)",
    KIND_ANOMALY: "Decode damage (bytes were lost upstream; do not just delete these)",
    KIND_HOMOGLYPH: "Mixed-script words (a letter substituted for a lookalike)",
    KIND_WHITESPACE: "Lookalike spaces (normalised to a plain space by --fix)",
    KIND_TYPOGRAPHY: "Typography (kept unless --aggressive; an em dash is punctuation)",
}

# Enough of the line to recognise the spot, without reprinting the paragraph.
_CONTEXT_RADIUS = 32


def _read(path):
    """Read a file preserving its line endings, so --fix does not rewrite them.

    ``open`` rather than ``Path.read_text`` because the ``newline`` parameter
    only reached the latter in 3.13, and this project supports 3.10. Without it
    every CRLF file on the project's primary platform would come back as LF and
    --fix would silently rewrite every line ending in the draft.
    """
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
        return fh.read()


def _write(path, text):
    Path(path).write_text(text, encoding="utf-8", errors="surrogateescape", newline="")


def _fixed_by(finding):
    """Which pass actually removes this finding.

    ``Finding.removable`` says only that a replacement exists, which is not the
    same question: an em dash has one and the default pass deliberately declines
    to use it. Labelling both the same way told the reader that a plain --fix
    would flatten their punctuation.
    """
    if not finding.removable:
        return "reported only"
    if finding.kind in SAFE_KINDS:
        return "removed by --fix"
    return "removed by --fix --aggressive"


def _context(text, position):
    """One line of context with the marker's position called out.

    The marker itself is usually invisible, so showing the raw slice would print
    something that looks identical to clean text. The position is given as a
    line and column instead, which is what an editor takes you to.
    """
    line = text.count("\n", 0, position) + 1
    line_start = text.rfind("\n", 0, position) + 1
    column = position - line_start + 1
    start = max(line_start, position - _CONTEXT_RADIUS)
    end = min(len(text), position + _CONTEXT_RADIUS)
    excerpt = text[start:end].split("\n")[0].strip()
    return f"line {line}, col {column}: ...{excerpt}..."


def analyse(path):
    """Scan one file and return a result dict; reads only."""
    text = _read(path)
    findings = scan(text)
    return {
        "path": str(path),
        "chars": len(text),
        "findings": findings,
        "serious": sum(f.count for f in findings if f.kind in SERIOUS_KINDS),
        "text": text,
    }


def print_report(result, show_inventory=False):
    text = result["text"]
    findings = result["findings"]
    print(f"\n{result['path']}  ({result['chars']:,} chars)")

    if not findings:
        print("  No authorship markers found.")
    else:
        by_kind = {}
        for f in findings:
            by_kind.setdefault(f.kind, []).append(f)
        for kind, heading in _KIND_HEADINGS.items():
            group = by_kind.get(kind)
            if not group:
                continue
            total = sum(f.count for f in group)
            print(f"\n  {heading}")
            print(f"  {'-' * len(heading)}")
            for f in group:
                print(f"    {f.codepoint:9s} x{f.count:<5d} {f.name}  [{_fixed_by(f)}]")
                if f.note:
                    print(f"      {f.note}")
                for pos in f.positions[:3]:
                    print(f"      {_context(text, pos)}")
            print(f"    ({total} occurrence{'s' if total != 1 else ''} in this group)")

    if show_inventory:
        census = inventory(text)
        print(f"\n  Non-ASCII census ({len(census)} distinct code points, no verdicts)")
        print(f"  {'-' * 58}")
        for c in census:
            print(
                f"    {c.codepoint:9s} x{c.count:<5d} {c.category}  "
                f"{c.marker_kind or '-':11s} {c.name}"
            )


def to_json(result, show_inventory=False):
    out = {
        "path": result["path"],
        "chars": result["chars"],
        "serious": result["serious"],
        "findings": [
            {
                "codepoint": f.codepoint,
                "name": f.name,
                "category": f.category,
                "kind": f.kind,
                "count": f.count,
                "positions": f.positions,
                "removable": f.removable,
                "note": f.note,
            }
            for f in result["findings"]
        ],
    }
    if show_inventory:
        out["inventory"] = [
            {
                "codepoint": c.codepoint,
                "name": c.name,
                "category": c.category,
                "count": c.count,
                "marker_kind": c.marker_kind,
            }
            for c in inventory(result["text"])
        ]
    return out


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ci-markers",
        description=(
            "Report, and optionally remove, machine-authorship markers in a draft. "
            "Reports by default; writes nothing without --fix."
        ),
        epilog=(
            "Exit code is 1 when a file contains an invisible character, a "
            "mixed-script word, or decode damage; 0 when it is clean or the only "
            "findings are cosmetic."
        ),
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, help="files to scan (omit with --stdin)"
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read text from stdin and write the sanitized text to stdout "
        "(the report goes to stderr, so this composes in a pipe)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite each file with its markers removed",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="with --fix, also flatten typography to ASCII and substitute known "
        "confusables -- this changes visible text",
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="also list every distinct non-ASCII code point, with no verdicts. "
        "Diff this between drafts to notice a marker nothing here recognises",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not args.paths and not args.stdin:
        parser.error("give at least one path, or --stdin")
    if args.aggressive and not args.fix:
        parser.error("--aggressive only means something with --fix")

    if args.stdin:
        # The report goes to stderr so the cleaned text on stdout stays pipeable.
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="surrogateescape")
        text = sys.stdin.read()
        out, findings = sanitize(text, aggressive=args.aggressive)
        sys.stdout.write(out)
        serious = sum(f.count for f in findings if f.kind in SERIOUS_KINDS)
        for f in findings:
            print(f"{f.codepoint} x{f.count} {f.kind}: {f.name}", file=sys.stderr)
        return 1 if serious else 0

    exit_code = 0
    payload = []
    for path in args.paths:
        if not path.is_file():
            log.error("not a file: %s", path)
            exit_code = 2
            continue
        result = analyse(path)
        if result["serious"]:
            exit_code = exit_code or 1

        if args.fix:
            cleaned, _ = sanitize(result["text"], aggressive=args.aggressive)
            if cleaned != result["text"]:
                _write(path, cleaned)
                removed = len(result["text"]) - len(cleaned)
                log.info("%s: rewritten (%+d chars)", path, -removed)
            else:
                log.info("%s: nothing to remove", path)

        if args.json:
            payload.append(to_json(result, show_inventory=args.inventory))
        else:
            print_report(result, show_inventory=args.inventory)

    if args.json:
        print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
