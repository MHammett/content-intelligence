"""Tests that fail when the documentation drifts out of sync with the code.

PR #44 corrected documentation that had gone stale across PRs #21-43: CLI flags
that shipped but never reached the README's options table, modules missing from
the project-structure tree, a citation adapter count frozen at four while the
code grew to ten, and doc links pointing at paths that moved during the monorepo
migration. Every one of those was mechanically detectable. These tests detect
them, in the PR that introduces them.

Links are checked past the path, too: a `#anchor` has to match a heading in the
file it points at. Renaming a heading breaks every link to it without moving a
file, and three such links had to be fixed by hand in PR #213.

Each failure message names the specific flag/module/adapter/link and the file
that needs updating — the point is that whoever trips one can fix it in a minute.
"""

import ast
import re
import subprocess
import sys
import unicodedata
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import unquote

import pytest
from markdown_it import MarkdownIt


def _find_repo_root():
    """Locate the workspace root by walking up from this file.

    These tests read repo files, so they must not depend on pytest's working
    directory (`uv run pytest packages/` and `cd packages/ci-article-review &&
    pytest` both have to work).
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError(
        "Could not locate the content-intelligence repo root above "
        f"{Path(__file__).resolve()} — expected an ancestor containing both "
        "packages/ and README.md."
    )


REPO_ROOT = _find_repo_root()
README = REPO_ROOT / "README.md"
CITATIONS_DOC = REPO_ROOT / "docs" / "CITATIONS.md"
PUBLICATION_EXAMPLE = (
    REPO_ROOT
    / "packages"
    / "ci-article-review"
    / "src"
    / "ci_article_review"
    / "configs"
    / "publication.example.yaml"
)


def _mentions(text, token):
    """True if `token` appears in `text` as a standalone token.

    Plain substring matching gives false passes: "config.py" is a substring of
    "logging_config.py", and "--publish" of "--publish-live". Requiring a
    non-identifier character on each side avoids both. A leading "/" is allowed
    because the tree writes some entries path-prefixed ("grammar/languagetool.py").
    """
    return re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-])", text) is not None


# ---------------------------------------------------------------------------
# 1. CLI flag coverage
# ---------------------------------------------------------------------------

# Every CLI whose flags the README documents. Adding a CLI here is a one-line
# change — voice_pattern_report was added this way when PR #46 restored it.
CLI_MODULES = [
    "ci_article_review.pipeline",
    "ci_article_review.history_analytics",
    "ci_article_review.voice_pattern_report",
]


def _long_flags(module_name):
    """Long-form flags of a module's parser, introspected rather than scraped.

    Walking parser._actions picks up flags inside mutually exclusive groups too,
    which is where --draft/--raw-draft/--url/--publish live.
    """
    import importlib

    module = importlib.import_module(module_name)
    parser = module.build_parser()
    flags = set()
    for action in parser._actions:
        for option in action.option_strings:
            if option.startswith("--"):
                flags.add(option)
    flags.discard("--help")
    return sorted(flags)


@pytest.mark.parametrize("module_name", CLI_MODULES)
def test_every_cli_flag_is_documented_in_readme(module_name):
    readme = README.read_text(encoding="utf-8")
    flags = _long_flags(module_name)
    assert flags, f"{module_name}.build_parser() exposed no long-form flags"

    missing = [flag for flag in flags if not _mentions(readme, flag)]
    assert not missing, (
        f"{module_name} defines flags that {README.name} never mentions: "
        f"{', '.join(missing)}. Add a row for each to the relevant options "
        f"table in {README}."
    )


# ---------------------------------------------------------------------------
# 2. Module tree coverage
# ---------------------------------------------------------------------------

# Directories the README's project-structure tree deliberately summarizes as a
# single line instead of listing file by file. Each entry is a real editorial
# decision, not a way to quiet the test — the tree describes what a directory
# holds ("10 adapters: census, crossref, ...") and listing every member would
# bury the modules a reader actually needs to find.
SUMMARIZED_DIRS = {
    # "sources/  10 adapters: census, crossref, eia, epa, ferc, fhwa, fred, ..."
    "packages/ci-article-review/src/ci_article_review/adapters/citation/sources",
    # "collectors/  wordpress, gmail, outlook365, twitter, textfiles, custom/"
    "packages/ci-style-profile/src/ci_style_profile/collectors",
}


def _project_structure_tree():
    """The fenced code block under the README's '## Project structure' heading.

    Scoped deliberately: a module name mentioned in prose elsewhere in the
    README does not mean it appears in the tree.
    """
    readme = README.read_text(encoding="utf-8")
    heading = re.search(r"^## Project structure$", readme, re.MULTILINE)
    assert heading, f"{README} no longer has a '## Project structure' heading"
    fence = re.search(r"^```\n(.*?)^```", readme[heading.end() :], re.MULTILINE | re.S)
    assert fence, (
        f"No fenced code block found under '## Project structure' in {README} — "
        "the tree this test checks against is gone."
    )
    return fence.group(1)


def _source_modules():
    for path in sorted(REPO_ROOT.glob("packages/*/src/**/*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        if path.name == "__init__.py":
            continue
        if "__pycache__" in parts or "tests" in parts:
            continue
        yield path


def test_every_source_module_appears_in_readme_tree():
    tree = _project_structure_tree()

    missing = []
    for path in _source_modules():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.rsplit("/", 1)[0] in SUMMARIZED_DIRS:
            continue
        if not _mentions(tree, path.name):
            missing.append(rel)

    assert not missing, (
        "These modules are missing from the project-structure tree in "
        f"{README}:\n  " + "\n  ".join(missing) + "\n"
        "Add a line for each under '## Project structure' (or, if the module's "
        "directory is meant to be summarized rather than itemized, add that "
        "directory to SUMMARIZED_DIRS in this file with a comment saying why)."
    )


def test_summarized_dirs_all_exist():
    """A stale allowlist entry would silently exempt nothing — or hide a rename."""
    missing = [d for d in sorted(SUMMARIZED_DIRS) if not (REPO_ROOT / d).is_dir()]
    assert not missing, (
        "SUMMARIZED_DIRS in this file lists directories that no longer exist: "
        f"{', '.join(missing)}. Update the allowlist to match the tree."
    )


# ---------------------------------------------------------------------------
# 3. Citation adapter coverage
# ---------------------------------------------------------------------------


def _documented_adapters_in_citations_doc():
    """Adapter names from the '| Adapter | Kind | Reaches |' table."""
    text = CITATIONS_DOC.read_text(encoding="utf-8")
    table = re.search(
        r"^\| Adapter \| Kind \| Reaches \|\n\|[-| ]+\|\n((?:\|.*\n)+)",
        text,
        re.MULTILINE,
    )
    assert table, (
        f"Could not find the '| Adapter | Kind | Reaches |' table in "
        f"{CITATIONS_DOC} — this test cross-checks it against "
        "resolver.ADAPTER_MAP."
    )
    return {
        row.split("|")[1].strip().strip("`")
        for row in table.group(1).strip().splitlines()
    }


def _documented_adapters_in_publication_example():
    """Adapter names from the '# Available adapters: ...' comment."""
    text = PUBLICATION_EXAMPLE.read_text(encoding="utf-8")
    comment = re.search(r"^\s*#\s*Available adapters:\s*(.+)$", text, re.MULTILINE)
    assert comment, (
        f"Could not find the '# Available adapters:' comment in "
        f"{PUBLICATION_EXAMPLE} — this test cross-checks it against "
        "resolver.ADAPTER_MAP."
    )
    return {name.strip() for name in comment.group(1).split(",") if name.strip()}


def _adapter_map():
    from ci_article_review.adapters.citation import resolver

    return set(resolver.ADAPTER_MAP)


@pytest.mark.parametrize(
    "doc_path, reader",
    [
        (CITATIONS_DOC, _documented_adapters_in_citations_doc),
        (PUBLICATION_EXAMPLE, _documented_adapters_in_publication_example),
    ],
    ids=["docs/CITATIONS.md", "configs/publication.example.yaml"],
)
def test_citation_adapters_are_documented(doc_path, reader):
    registered = _adapter_map()
    documented = reader()

    undocumented = sorted(registered - documented)
    phantom = sorted(documented - registered)

    assert not undocumented, (
        f"Citation adapters in resolver.ADAPTER_MAP that {doc_path} does not "
        f"list: {', '.join(undocumented)}. Add them to {doc_path}."
    )
    assert not phantom, (
        f"{doc_path} lists citation adapters that resolver.ADAPTER_MAP does not "
        f"define: {', '.join(phantom)}. Remove them from {doc_path}."
    )


def test_readme_states_the_real_adapter_count():
    """The README's sources/ line carries a count and a name list; both drift."""
    tree = _project_structure_tree()
    registered = _adapter_map()

    line = re.search(
        r"sources/\s+(\d+) adapters: ([^\n│]*(?:\n[^\n│]*│?[^\n]*)?)", tree
    )
    assert line, (
        f"Could not find the 'sources/  N adapters: ...' line in the "
        f"project-structure tree in {README}."
    )
    count = int(line.group(1))
    assert count == len(registered), (
        f"{README}'s project-structure tree says '{count} adapters' but "
        f"resolver.ADAPTER_MAP defines {len(registered)}. Update the tree."
    )

    listed = {name.strip(" ,\n") for name in re.split(r"[,\s]+", line.group(2))}
    missing = sorted(registered - listed)
    assert not missing, (
        f"{README}'s project-structure tree omits these citation adapters from "
        f"its sources/ line: {', '.join(missing)}."
    )


# ---------------------------------------------------------------------------
# 4. Relative link validity
# ---------------------------------------------------------------------------

_FENCED_CODE = re.compile(r"^```.*?^```", re.MULTILINE | re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)")


def _tracked_markdown_files():
    """Markdown files tracked by git, or None if git can't answer.

    "The repo's docs" means the files git tracks — not every .md on disk. A
    plain ``**/*.md`` walk also picks up untracked and ignored trees, and the
    one that bites is a **nested git worktree**: this project's tooling creates
    them under ``.claude/worktrees/`` (git-excluded via ``.git/info/exclude``),
    each holding a full second copy of the docs at whatever commit that branch
    sits on. A stale copy there would fail these guards even when every tracked
    doc is correct — and it would only fail *locally*, since CI checks out a
    clean tree with no nested worktrees. Asking git avoids that whole class of
    false positive, and covers ``.venv``/build output for free.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.md"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [REPO_ROOT / name for name in out.split("\0") if name]


def _markdown_files():
    tracked = _tracked_markdown_files()
    if tracked is not None:
        yield from sorted(p for p in tracked if p.is_file())
        return

    # Fallback for a source tree with no usable git (release tarball, vendored
    # copy). Skips the known-noisy directories by name; less precise than
    # git ls-files, which is why it is only the fallback.
    for path in sorted(REPO_ROOT.glob("**/*.md")):
        parts = path.relative_to(REPO_ROOT).parts
        if any(
            p in {".git", ".claude", ".venv", "node_modules", "__pycache__"}
            for p in parts
        ):
            continue
        yield path


def _relative_link_targets(text):
    """Link targets in `text` that stay in the repo, excluding anything inside code.

    A bare `#anchor` stays in the repo: it is a link into the file it is written
    in. Only a scheme (`https:`, `mailto:`) or a leading `//` takes a link out.

    Code spans and fences are stripped first rather than skipped file by file:
    ci-style-profile/PLAN.md documents its own markdown handling with a literal
    `` `[text](url)` `` example, which is prose about a link, not a link.
    Stripping code handles that precisely and generalizes to any future
    occurrence.
    """
    text = _FENCED_CODE.sub("", text)
    text = _INLINE_CODE.sub("", text)
    for target in _MD_LINK.findall(text):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", target, re.IGNORECASE):
            continue
        yield target


def test_all_relative_markdown_links_resolve():
    broken = []
    for md_path in _markdown_files():
        text = md_path.read_text(encoding="utf-8")
        for target in _relative_link_targets(text):
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue  # pure in-page anchor; test_all_relative_markdown_anchors_resolve
            resolved = (md_path.parent / path_part).resolve()
            if not resolved.exists():
                broken.append(
                    f"{md_path.relative_to(REPO_ROOT).as_posix()} → {target} "
                    f"(no such path: {resolved})"
                )

    assert not broken, "Broken relative markdown links:\n  " + "\n  ".join(broken)


# A link's #anchor is checked as well as its path. Renaming a heading breaks
# every link to it without any path changing, which is how README.md and
# docs/TROUBLESHOOTING.md went on pointing at
# CONFIGURATION.md#timeouts-are-automatic-sliding-scale after that section became
# "Wall-clock backstop is automatic (sliding scale)". PR #213 fixed them by hand.
#
# What was reused and what was not, decided before writing any of it:
#   - GitHub's anchor rule is the JS package github-slugger. Its only Python
#     port (PyPI github-slugger 0.0.3) had a single release day, 2022-12-12,
#     carries a hand-transcribed copy of the Unicode table, and has not moved
#     since. The rule is short, so it is written out below and checked against
#     GitHub's own output instead of taking on that dependency.
#   - Ready-made link checkers such as remark-validate-links (Node) and lychee
#     (Rust) need a toolchain that nothing else in this suite runs.
#   - Parsing is the part worth borrowing. A line scan built on _FENCED_CODE
#     reads the `# configs/mypub.yaml` comment in docs/CONFIGURATION.md's
#     indented ```yaml fence as a heading, and misses setext (underlined)
#     headings. markdown-it-py, a CommonMark parser, does neither: over every
#     tracked doc it gave the same heading ids GitHub renders for them (fetched
#     through the contents API with `Accept: application/vnd.github.html+json`;
#     335 of 335 when this was written), none extra and none missing.

_MARKDOWN = MarkdownIt("commonmark")

# Circled and squared Latin letters are symbols by Unicode category but count as
# letters to GitHub, and unicodedata has no property that says so.
_ALPHABETIC_SYMBOLS = (
    (0x24B6, 0x24E9),
    (0x1F130, 0x1F149),
    (0x1F150, 0x1F169),
    (0x1F170, 0x1F189),
)

# `#L10` and `#L10-L12` name lines of a file, not headings.
_LINE_ANCHOR = re.compile(r"L\d+(?:C\d+)?(?:-L\d+(?:C\d+)?)?")

# On GitHub an `id` on any element, and a `name` on an <a>, is an anchor too. The
# lookbehind keeps `data-id=` and the like from counting.
_HTML_TAG = re.compile(r"<([a-z][a-z0-9]*)\b([^>]*)>", re.IGNORECASE)
_HTML_ID_OR_NAME = re.compile(
    r"""(?<![\w-])(id|name)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)


def _github_slug(heading):
    """The anchor GitHub gives a heading's text, before repeats are numbered.

    Lowercase, keep the characters Unicode calls word characters (letters, marks,
    decimal digits, connector punctuation such as `_`) plus `-` and spaces, drop
    everything else, then turn each space into `-`. Dropping is not replacing, so
    an em dash leaves the spaces around it behind: "Claude — adaptive vs extended
    thinking" becomes `claude--adaptive-vs-extended-thinking`.

    This is github-slugger 2.0.0's rule. Compared with that package's table over
    every code point, it drops nothing the table keeps, and the only extra
    characters it keeps are ones Unicode added after version 13, which the table
    predates. Like that package it does not strip: the parser has already trimmed
    the heading.
    """
    kept = []
    for char in heading.lower():
        category = unicodedata.category(char)
        if (
            char in "- "
            or category[0] in "LM"
            or category in ("Nd", "Nl", "Pc")
            or any(low <= ord(char) <= high for low, high in _ALPHABETIC_SYMBOLS)
        ):
            kept.append(char)
    return "".join(kept).replace(" ", "-")


def _html_anchors(html):
    for tag, attributes in _HTML_TAG.findall(html):
        for attribute, double, single, bare in _HTML_ID_OR_NAME.findall(attributes):
            if attribute.lower() == "id" or tag.lower() == "a":
                yield double or single or bare


def _anchors_in(text):
    """Every `#fragment` a link into `text`, as GitHub renders it, can use.

    That is each heading's slug, with a repeated one numbered `-1`, `-2`, ... the
    way github-slugger numbers them, and any `id` or `<a name>` written as raw
    HTML. `text` is parsed as CommonMark rather than scanned, so a `# comment`
    inside a fence is not a heading even when the fence is indented inside a list
    item, and an underlined (setext) heading is one.
    """
    tokens = _MARKDOWN.parse(text)
    anchors = set()
    occurrences = {}  # github-slugger's name for it: slug -> repeats so far
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            heading = "".join(
                child.content
                for child in tokens[index + 1].children
                if child.type in ("text", "code_inline")
            )
            slug = unique = _github_slug(heading)
            while unique in occurrences:
                occurrences[slug] += 1
                unique = f"{slug}-{occurrences[slug]}"
            occurrences[unique] = 0
            anchors.add(unique)
        elif token.type == "html_block":
            anchors.update(_html_anchors(token.content))
        elif token.type == "inline":
            for child in token.children:
                if child.type == "html_inline":
                    anchors.update(_html_anchors(child.content))
    return anchors


def _broken_anchor_links(md_paths, root):
    """One line per relative link whose `#anchor` names nothing in its target.

    Only links that reach an existing markdown file are judged. A missing file is
    test_all_relative_markdown_links_resolve's finding, and an anchor on any other
    kind of file (`resolver.py#L60`) is a line number, not a heading. A bare
    `#anchor` is a link into the file it is written in.
    """
    anchors_by_file = {}
    broken = []
    for md_path in md_paths:
        for target in _relative_link_targets(md_path.read_text(encoding="utf-8")):
            path_part, _, fragment = target.partition("#")
            fragment = unquote(fragment)
            if not fragment or fragment.lower() == "top":
                continue  # `#` and `#top` mean the top of any page
            if _LINE_ANCHOR.fullmatch(fragment):
                continue
            if path_part:
                linked = (md_path.parent / path_part).resolve()
            else:
                linked = md_path.resolve()
            if linked.suffix != ".md" or not linked.is_file():
                continue

            if linked not in anchors_by_file:
                text = linked.read_text(encoding="utf-8")
                anchors_by_file[linked] = _anchors_in(text)
            anchors = anchors_by_file[linked]
            if fragment in anchors:
                continue

            near = get_close_matches(fragment, anchors, n=1)
            hint = f"; did you mean #{near[0]}?" if near else ""
            broken.append(
                f"{md_path.relative_to(root).as_posix()} → {target} "
                f"(no heading or id there produces #{fragment}{hint})"
            )
    return broken


def test_all_relative_markdown_anchors_resolve():
    broken = _broken_anchor_links(_markdown_files(), REPO_ROOT)
    assert not broken, (
        "Markdown links whose #anchor matches no heading in the file they point "
        "to. An anchor is GitHub's slug of the heading text, so renaming a "
        "heading breaks every link to it. Fix the link, not the heading, unless "
        "the rename was the mistake:\n  " + "\n  ".join(broken)
    )


# What follows tests the checker itself, on throwaway trees. The real test above
# can only ever say "this repo is fine today"; these say it would notice.


def _anchor_report(tmp_path, files):
    """Write `files` (name -> text) under tmp_path; return the broken-anchor report."""
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    markdown = [tmp_path / name for name in sorted(files) if name.endswith(".md")]
    return _broken_anchor_links(markdown, tmp_path)


def test_a_renamed_heading_is_caught_and_its_new_anchor_suggested(tmp_path):
    """The bug PR #213 fixed by hand: the heading changed, the links did not."""
    stale = "timeouts-are-automatic-sliding-scale"
    current = "wall-clock-backstop-is-automatic-sliding-scale"
    files = {
        "docs/CONFIGURATION.md": "## Wall-clock backstop is automatic (sliding scale)\n",
        "README.md": f"[t](docs/CONFIGURATION.md#{stale})\n",
    }
    (report,) = _anchor_report(tmp_path, files)
    assert f"README.md → docs/CONFIGURATION.md#{stale}" in report
    assert f"did you mean #{current}?" in report

    files["README.md"] = f"[t](docs/CONFIGURATION.md#{current})\n"
    assert _anchor_report(tmp_path, files) == []


def test_a_bare_anchor_is_checked_against_its_own_file(tmp_path):
    doc = "## Present\n\n[ok](#present) [bad](#absent)\n"
    (report,) = _anchor_report(tmp_path, {"A.md": doc})
    assert report.startswith("A.md → #absent ")


def test_a_comment_in_a_fence_indented_in_a_list_item_is_not_a_heading(tmp_path):
    """docs/CONFIGURATION.md nests ```yaml fences inside list items."""
    doc = (
        "1. Step\n"
        "   ```yaml\n"
        "   # configs/mypub.yaml\n"
        "   ```\n\n"
        "## Real\n\n"
        "[fake](#configsmypubyaml) [real](#real)\n"
    )
    (report,) = _anchor_report(tmp_path, {"A.md": doc})
    assert "#configsmypubyaml" in report


def test_repeated_headings_are_numbered_the_way_github_numbers_them():
    assert _anchors_in("## Setup\n\n## Setup\n\n## Setup\n") == {
        "setup",
        "setup-1",
        "setup-2",
    }
    # github-slugger's own edge: a heading that already looks like a numbered repeat.
    assert _anchors_in("## a\n\n## a\n\n## a-1\n") == {"a", "a-1", "a-1-1"}


def test_underlined_headings_are_headings():
    assert _anchors_in("Title\n=====\n\nSub-title\n--------\n") == {
        "title",
        "sub-title",
    }


def test_heading_markup_is_reduced_to_its_text():
    text = "## The `--cost-preset` flag, _really_ (and [more](x.md))\n"
    assert _anchors_in(text) == {"the---cost-preset-flag-really-and-more"}


def test_explicit_html_anchors_count():
    text = (
        '<a name="legacy"></a>\n\n'
        '<div id="box">x</div>\n\n'
        'text <span id="inline"></span>\n\n'
        '<meta name="not-an-anchor"> <p data-id="nor-this">\n'
    )
    assert _anchors_in(text) == {"legacy", "box", "inline"}


def test_only_anchors_into_markdown_headings_are_judged(tmp_path):
    files = {
        "A.md": (
            "[line](code.py#L60) [range](B.md#L10-L12) [top](#top) [bare](#) "
            "[gone](missing.md#x)\n"
        ),
        "B.md": "# B\n",
        "code.py": "x = 1\n",
    }
    assert _anchor_report(tmp_path, files) == []


def test_a_percent_encoded_anchor_is_decoded_before_matching(tmp_path):
    assert _anchor_report(tmp_path, {"A.md": "## Café\n\n[x](#caf%C3%A9)\n"}) == []


# Headings from this repo with the ids GitHub renders for them, and cases from
# github-slugger's fixtures (which it generated from GitHub's rendering).
@pytest.mark.parametrize(
    "heading, slug",
    [
        (
            "Claude — adaptive vs extended thinking",
            "claude--adaptive-vs-extended-thinking",
        ),
        (
            "Option B — Vertex AI (reserved capacity, no 503s)",
            "option-b--vertex-ai-reserved-capacity-no-503s",
        ),
        (
            "Resolved: ci-web-intel → ci-style-profile",
            "resolved-ci-web-intel--ci-style-profile",
        ),
        (
            'Pointer only — verification: "pointer"',
            "pointer-only--verification-pointer",
        ),
        (
            "4. Perplexity's grounded search citations are captured and thrown away",
            "4-perplexitys-grounded-search-citations-are-captured-and-thrown-away",
        ),
        ("bravoCharlieDelta", "bravocharliedelta"),
        ("heading with a - dash", "heading-with-a---dash"),
        ("heading with an _ underscore", "heading-with-an-_-underscore"),
        ("heading with a period.txt", "heading-with-a-periodtxt"),
        ("apostrophe’s should be trimmed", "apostrophes-should-be-trimmed"),
        ("en–dash", "endash"),
        ("😄 unicode emoji", "-unicode-emoji"),
        ("Привет non-latin 你好", "привет-non-latin-你好"),
    ],
)
def test_github_slug_matches_githubs(heading, slug):
    assert _github_slug(heading) == slug


# ---------------------------------------------------------------------------
# 5. CLI invocation style
# ---------------------------------------------------------------------------

# The docs invoke the CLIs as console scripts (`uv run ci-check`), not as
# modules (`uv run python -m ci_article_review.check`). Both run, but only the
# console script reports its own name in argparse's usage line — `python -m`
# prints "usage: check.py", a path that has not been runnable since the src/
# layout landed. Setting prog= to compensate would bake "uv run" into --help,
# which is wrong for anyone inside an activated venv, so the docs move instead.

_CONSOLE_SCRIPT_REF = re.compile(r"\buv run (ci-[a-z0-9-]+)")
_MODULE_FORM_REF = re.compile(r"\bpython -m (ci_[a-z_]+)\.([a-z_]+)")
# The bare form (`python pipeline.py`) predates the src/ layout and never runs
# from the repo root. It is matched separately from the module form because it
# names the file, not the import path, so there is no package to key on.
_BARE_SCRIPT_REF = re.compile(r"\bpython ([a-z_]+)\.py\b")
_SCRIPTS_SECTION = re.compile(r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", re.M | re.S)
_SCRIPT_ENTRY = re.compile(r"^([\w-]+)\s*=\s*[\"']([\w.]+):(\w+)[\"']", re.M)


def _declared_console_scripts():
    """Map console-script name -> "module:function", across every package.

    Parsed with a regex rather than tomllib because tomllib is 3.11+ and this
    project supports 3.10.
    """
    scripts = {}
    for pyproject in sorted(REPO_ROOT.glob("packages/*/pyproject.toml")):
        section = _SCRIPTS_SECTION.search(pyproject.read_text(encoding="utf-8"))
        if not section:
            continue
        for name, module, func in _SCRIPT_ENTRY.findall(section.group(1)):
            scripts[name] = f"{module}:{func}"
    return scripts


def test_documented_console_scripts_are_declared():
    """Every `uv run ci-foo` in the docs resolves to a real entry point.

    Without this, renaming a script in pyproject.toml leaves the docs quietly
    pointing at a command that no longer exists.
    """
    declared = _declared_console_scripts()
    assert declared, (
        "No [project.scripts] entries found under packages/*/pyproject.toml — "
        "the parser in this test is probably out of date."
    )

    unknown = []
    for md_path in _markdown_files():
        text = md_path.read_text(encoding="utf-8")
        for name in sorted(set(_CONSOLE_SCRIPT_REF.findall(text))):
            if name not in declared:
                unknown.append(f"{md_path.relative_to(REPO_ROOT).as_posix()} → {name}")

    assert not unknown, (
        "Docs invoke console scripts that no package declares:\n  "
        + "\n  ".join(unknown)
        + "\n\nDeclared: "
        + ", ".join(sorted(declared))
    )


def test_docs_do_not_reintroduce_the_module_invocation_form():
    """Docs use `uv run ci-foo`, not `uv run python -m ci_article_review.foo`.

    Only flags modules that actually have a console script — `probe` has a
    parser but no entry point, so documenting it as a module would be correct.
    """
    by_target = {target: name for name, target in _declared_console_scripts().items()}

    offenders = []
    for md_path in _markdown_files():
        text = md_path.read_text(encoding="utf-8")
        for package, module in set(_MODULE_FORM_REF.findall(text)):
            script = by_target.get(f"{package}.{module}:main")
            if script:
                offenders.append(
                    f"{md_path.relative_to(REPO_ROOT).as_posix()}: "
                    f"python -m {package}.{module} → use `uv run {script}`"
                )

    assert not offenders, (
        "Docs use the module invocation form where a console script exists:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_docs_do_not_use_the_bare_script_invocation_form():
    """No `python pipeline.py` anywhere in markdown.

    Separate from the module-form guard, which keys on the import path and so
    cannot see this. The bare form is the older mistake of the two: it survived
    in handoff_templates/ after the docs were swept, where it reached users as
    an instruction the chat model relays back to them.

    Keyed on the module basenames that have console scripts, so unrelated
    filenames in prose (`conftest.py`) do not trip it.
    """
    runnable = {}
    for name, target in _declared_console_scripts().items():
        module_stem = target.split(":")[0].rsplit(".", 1)[-1]
        runnable[module_stem] = name

    offenders = []
    for md_path in _markdown_files():
        text = md_path.read_text(encoding="utf-8")
        for stem in set(_BARE_SCRIPT_REF.findall(text)):
            script = runnable.get(stem)
            if script:
                offenders.append(
                    f"{md_path.relative_to(REPO_ROOT).as_posix()}: "
                    f"python {stem}.py → use `uv run {script}`"
                )

    assert not offenders, (
        "Docs use the bare script form, which has not been runnable from the "
        "repo root since the src/ layout landed:\n  " + "\n  ".join(sorted(offenders))
    )


# ---------------------------------------------------------------------------
# 6. CLI invocation style in strings the CODE prints
# ---------------------------------------------------------------------------
#
# The guards above scan documentation. They cannot see the commands the tools
# print at runtime, and that blind spot cost real breakage: `ci-setup` — the
# very first thing a new user runs — printed its "run the pipeline" command with
# a bash line-continuation backslash (which splits the command on Windows
# cmd.exe, the documented primary platform) and in the `python -m` form that
# PR #51 standardised away and PR #53 removed from the handoff templates. It
# survived both cleanups because nothing checks printed strings.
#
# Audit finding 9. The premise of every guard in this file is that these
# mistakes are mechanically detectable; a string in a print() is exactly as
# detectable as one in a markdown file.

_USER_FACING_MODULES = (
    "setup.py",
    "check.py",
    "discover.py",
    "pipeline.py",
    "history_analytics.py",
    "voice_pattern_report.py",
)

# argparse prints these two verbatim on --help, so they are subject to the same
# guards as a print(). Matched on the ArgumentParser call rather than on the
# kwarg name alone: `description=` is ordinary enough that pipeline.py's
# publication_description= and ci-style-profile's description=v.get(...) would
# otherwise be swept in.
_HELP_TEXT_KWARGS = ("epilog", "description")


def _user_facing_sources():
    src = REPO_ROOT / "packages" / "ci-article-review" / "src" / "ci_article_review"
    for name in _USER_FACING_MODULES:
        path = src / name
        if path.is_file():
            yield path


def _terminal_bound_expressions(tree):
    """Argument expressions whose strings reach the user's terminal verbatim.

    Two call shapes qualify: `print(...)`, and the help text handed to
    `argparse.ArgumentParser(...)`. The second is why pipeline.py's epilog kept
    four `python pipeline.py` examples through every sweep meant to retire that
    form — it is printed on `--help` like any other output, but it arrives as a
    keyword argument, so a guard that only reads print() never saw it.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            yield from node.args
            continue
        # argparse.ArgumentParser(...) or a bare ArgumentParser(...) import.
        called = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if called == "ArgumentParser":
            for keyword in node.keywords:
                if keyword.arg in _HELP_TEXT_KWARGS:
                    yield keyword.value


def _printed_strings(path):
    """Every string literal in `path` that the code prints to the terminal.

    An ast walk, not a regex. The regex this replaced captured only the *first*
    string literal of a print(...) — and this codebase writes its multi-line
    printed blocks as implicitly concatenated literals, so every line after the
    first was invisible to the guards below. discover.py's closing legend is
    exactly that shape, and the `python check.py` buried in it survived both of
    the sweeps (PR #51, PR #53) that were supposed to retire that form. Across
    the user-facing modules the walk sees roughly twice the literals the regex
    did, plus the argparse help text the regex could not reach at all.

    Two node types have to be collected. Python folds a run of adjacent plain
    literals into a single ast.Constant, but one f-string anywhere in the run
    makes the whole run an ast.JoinedStr whose parts stay separate — and the
    legend block interleaves both. Walking each expression covers that, and
    explicit `"a" + "b"` concatenation, without enumerating shapes.

    Values are decoded strings, not the source text between the quotes: an
    escaped backslash is one character here where the regex reported the two
    source characters. The line-continuation guard below depends on that.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = []
    for expression in _terminal_bound_expressions(tree):
        for part in ast.walk(expression):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                literals.append(part.value)
    return literals


def test_printed_commands_do_not_use_the_module_invocation_form():
    """Printed commands use `uv run ci-foo`, like the docs do."""
    by_target = {target: name for name, target in _declared_console_scripts().items()}

    offenders = []
    for path in _user_facing_sources():
        for literal in _printed_strings(path):
            for package, module in set(_MODULE_FORM_REF.findall(literal)):
                script = by_target.get(f"{package}.{module}:main")
                if script:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}: "
                        f"python -m {package}.{module} → use `uv run {script}`"
                    )

    assert not offenders, (
        "Code prints the module invocation form where a console script exists:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_printed_commands_do_not_use_the_bare_script_invocation_form():
    """Printed commands use `uv run ci-foo`, not `python foo.py`.

    Section 5 applies _BARE_SCRIPT_REF to markdown only, which left the older
    of the two mistakes unguarded on the side that reaches users at runtime:
    discover.py's legend closed every model sweep by telling the reader to run
    `python check.py`, a command that has not worked from the repo root since
    the src/ layout landed.

    Keyed on module basenames that have console scripts, like its markdown
    counterpart, so a printed `python setup.py` in unrelated prose about some
    other project's file would not trip it.
    """
    runnable = {}
    for name, target in _declared_console_scripts().items():
        module_stem = target.split(":")[0].rsplit(".", 1)[-1]
        runnable[module_stem] = name

    offenders = []
    for path in _user_facing_sources():
        for literal in _printed_strings(path):
            for stem in set(_BARE_SCRIPT_REF.findall(literal)):
                script = runnable.get(stem)
                if script:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}: "
                        f"python {stem}.py → use `uv run {script}`"
                    )

    assert not offenders, (
        "Code prints the bare script form, which has not been runnable from "
        "the repo root since the src/ layout landed:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_printed_commands_do_not_use_bash_line_continuations():
    """A trailing backslash splits the command on Windows cmd.exe.

    The README warns about this explicitly. Windows is the documented primary
    platform, so a printed command that only works in bash is a broken
    instruction, not a cosmetic issue.

    Checked line by line rather than at the end of the literal. Now that
    _printed_strings() returns whole concatenated blocks instead of their first
    line, a continuation sits mid-literal — which is exactly where one appears,
    since a continuation by definition has a following line to continue onto.
    """
    offenders = []
    for path in _user_facing_sources():
        for literal in _printed_strings(path):
            for line in literal.splitlines():
                # One real backslash: these are decoded values, not source text.
                if line.rstrip().endswith("\\"):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}: {line.strip()!r}"
                    )

    assert not offenders, (
        "Printed commands use a bash line-continuation backslash, which splits "
        "the command on Windows cmd.exe. Keep the command on one line:\n  "
        + "\n  ".join(sorted(offenders))
    )


# ---------------------------------------------------------------------------
# 7. Printed output survives a default Windows console
# ---------------------------------------------------------------------------
#
# Same premise as section 6, one layer down: not what the printed command says,
# but whether the print reaches the terminal at all. A default Windows console
# encodes cp1252, and `ci-discover` spent its whole life dying on its first
# provider row — `✓`, `←`, `→` and `⚠` have no cp1252 encoding, so printing a
# model row raised UnicodeEncodeError partway through the report. `ci-review`
# had been immune since it reconfigured stdout at import; nothing propagated
# that to the other six entry points, and nothing noticed.
#
# The guard is deliberately structural rather than a scan for offending
# characters. Half the exposure is data — article titles, flagged passages,
# provider error bodies — which no scan of our own literals can see.

_UTF8_GUARD_CALL = re.compile(r"^force_utf8_stdio\(\)\s*$", re.M)


def _module_source_path(dotted):
    """Resolve "ci_article_review.discover" to its file under packages/*/src/."""
    parts = dotted.split(".")
    for src_root in sorted(REPO_ROOT.glob("packages/*/src")):
        candidate = src_root.joinpath(*parts).with_suffix(".py")
        if candidate.is_file():
            return candidate
    return None


def test_every_cli_entry_point_forces_utf8_stdio():
    """Each console script calls force_utf8_stdio() before it prints anything.

    Adding a CLI is the moment this gets forgotten, so the list of entry points
    comes from [project.scripts] rather than being enumerated here — a new
    script is covered the day it is declared.
    """
    declared = _declared_console_scripts()
    assert declared, (
        "No [project.scripts] entries found under packages/*/pyproject.toml — "
        "the parser in this test is probably out of date."
    )

    missing = []
    unresolved = []
    for script, target in sorted(declared.items()):
        dotted = target.split(":")[0]
        path = _module_source_path(dotted)
        if path is None:
            unresolved.append(f"{script} -> {target}")
            continue
        if not _UTF8_GUARD_CALL.search(path.read_text(encoding="utf-8")):
            missing.append(f"{script} ({path.relative_to(REPO_ROOT).as_posix()})")

    assert not unresolved, (
        "Console script targets that resolve to no source file — the entry "
        "point or the resolver is wrong:\n  " + "\n  ".join(unresolved)
    )
    assert not missing, (
        "CLI entry points that can crash with UnicodeEncodeError on a stock "
        "Windows console (cp1252). Add `from ci_core.console import "
        "force_utf8_stdio` and call it above the first print:\n  "
        + "\n  ".join(missing)
    )


def test_force_utf8_stdio_survives_a_cp1252_stream():
    """The helper must actually make an unencodable character printable.

    Asserting the call is present only helps if the call works. This pins the
    behaviour the entry points are relying on, including the lone surrogate
    that UTF-8 itself cannot encode.
    """
    import io

    from ci_core.console import force_utf8_stdio as force

    original = sys.stdout
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    try:
        sys.stdout = stream
        # Guard against a no-op test: strict cp1252 must reject this first.
        with pytest.raises(UnicodeEncodeError):
            stream.write("✓")
            stream.flush()

        force()
        assert sys.stdout.encoding.lower().replace("-", "") == "utf8"

        print("✓ ← → ⚠ ≤ \ud800")
        sys.stdout.flush()
    finally:
        sys.stdout = original
