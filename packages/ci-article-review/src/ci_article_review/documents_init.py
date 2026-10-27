"""What ``ci-docs init`` puts in the document repository, and how it decides.

The document repository (a private GitHub repository holding the articles) has to
describe itself to whatever attaches to it: Codex reads ``AGENTS.md``, Claude Code
reads ``CLAUDE.md``, a claude.ai project reads what is pasted into its
instructions. This module renders those files from sources packaged here, and
decides, file by file, what a re-run may write.

No network and no ``gh`` in this module: it renders bytes and compares blob SHAs.
``documents_cli`` does the talking. That split is what lets the never-clobber
rules be tested on plain dicts.

Managed paths and their sources:

* the five templates come from ``handoff_templates/``, the same files ``ci-setup``
  copies, so each template has exactly one source;
* ``AGENTS.md``, ``CLAUDE.md``, ``README.md``, ``.gitattributes`` and
  ``instructions/claude-project.md`` come from ``document_repo/``. Those sources
  carry a ``.tmpl`` suffix on purpose: a file called ``AGENTS.md`` or ``CLAUDE.md``
  inside this package would be read by Codex and Claude Code as instructions for
  working on *this* repository, and a ``.gitattributes`` there would apply to the
  directory it sits in.

Two paths are written once and never managed: ``articles/README.md`` (only while
``articles/`` is empty) and ``instructions/publication.md`` (only from
``--seed-instructions``, and only if absent). Nothing else under ``articles/`` is
ever touched.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ci_core.github_repo import git_blob_sha

DEFAULT_REPO = "MHammett/mikehammett.net-articles"
DEFAULT_BRANCH = "main"
REPO_DESCRIPTION = (
    "Articles for mikehammett.net: drafts, metadata, hand-offs and ci-review reviews"
)

MANIFEST_PATH = ".ci-docs/manifest.json"
PUBLICATION_PATH = "instructions/publication.md"
ARTICLES_README_PATH = "articles/README.md"
SEED_PATH = "README.md"

_PACKAGE_DIR = Path(__file__).parent
TEMPLATE_DIR = _PACKAGE_DIR / "handoff_templates"
DOCUMENT_REPO_DIR = _PACKAGE_DIR / "document_repo"

#: Shown in the marker line of each managed file, so a reader of the document
#: repository can find the source.
_SOURCE_ROOT = "packages/ci-article-review/src/ci_article_review"

#: Copied into ``templates/`` verbatim (apart from the marker line). Not
#: ``setup._WORKING_TEMPLATES``: that is what ``ci-setup`` hands a user, and the
#: drafting side also needs the close-out note and the revise prompt.
TEMPLATE_FILES = (
    "draft_submission.template.md",
    "metadata_only.md",
    "publication.md",
    "close_out.md",
    "revise_after_review_prompt.md",
)

#: (path in the document repository, source under document_repo/, marker style).
#: ``CLAUDE.md`` has no marker: its content must be exactly ``@AGENTS.md``, so it
#: is recorded in the manifest only. ``.gitattributes`` takes its marker on a
#: comment line after the one rule.
REPO_FILES = (
    ("AGENTS.md", "AGENTS.md.tmpl", "html"),
    ("CLAUDE.md", "CLAUDE.md.tmpl", None),
    ("README.md", "README.md.tmpl", "html"),
    (".gitattributes", "gitattributes.tmpl", "hash"),
    ("instructions/claude-project.md", "claude-project.md.tmpl", "html"),
)


def _lf(data):
    """Checkouts are CRLF on this machine and the repository is LF."""
    return data.replace(b"\r\n", b"\n")


def _marker(style, source):
    text = (
        f"Managed by content-intelligence: edit {source} there, not here. "
        "`ci-docs init` replaces this file only if it has not been edited here."
    )
    if style == "html":
        return f"<!-- {text} -->\n".encode("utf-8")
    if style == "hash":
        return f"# {text}\n".encode("utf-8")
    return b""


def render_managed(repo, branch, *, template_dir=None, document_repo_dir=None):
    """Every managed file as ``{path in the document repository: bytes}``."""
    template_dir = Path(template_dir or TEMPLATE_DIR)
    document_repo_dir = Path(document_repo_dir or DOCUMENT_REPO_DIR)
    out = {}
    for name in TEMPLATE_FILES:
        source = f"{_SOURCE_ROOT}/handoff_templates/{name}"
        body = _lf((template_dir / name).read_bytes())
        out[f"templates/{name}"] = _marker("html", source) + body
    for path, src, style in REPO_FILES:
        source = f"{_SOURCE_ROOT}/document_repo/{src}"
        body = _fill((document_repo_dir / src).read_bytes(), repo, branch)
        marker = _marker(style, source)
        out[path] = body + marker if style == "hash" else marker + body
    return out


def render_articles_readme(repo, branch, *, document_repo_dir=None):
    path = Path(document_repo_dir or DOCUMENT_REPO_DIR) / "articles_README.md.tmpl"
    return _fill(path.read_bytes(), repo, branch)


def _fill(data, repo, branch):
    text = _lf(data).decode("utf-8")
    return text.replace("{{repo}}", repo).replace("{{branch}}", branch).encode("utf-8")


def read_seed_instructions(path):
    """The author's own file, as bytes ready to commit (LF), never altered otherwise."""
    return _lf(Path(path).read_bytes())


def source_commit(run=subprocess.run, package_dir=None):
    """``(full, short)`` for the commit the managed files are rendered from.

    ``git rev-parse HEAD`` in a checkout; the package version when this is an
    installed copy with no git around it. ``+dirty`` is appended when the
    sources have uncommitted changes, because then the commit does not describe
    what was rendered.
    """
    cwd = Path(package_dir or _PACKAGE_DIR)

    def git(*args):
        try:
            proc = run(
                ["git", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                timeout=20,
                check=False,
                cwd=str(cwd),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    full = git("rev-parse", "HEAD")
    if not full or not re.fullmatch(r"[0-9a-f]{40}", full):
        try:
            from importlib.metadata import version

            v = version("ci-article-review")
        except Exception:  # pragma: no cover - not installed at all
            v = "unknown"
        return f"ci-article-review {v}", f"v{v}"
    dirty = git("status", "--porcelain", "--", "handoff_templates", "document_repo")
    suffix = "+dirty" if dirty else ""
    return full + suffix, full[:7] + suffix


@dataclass
class RepoState:
    """What the document repository holds that the plan depends on."""

    #: path -> blob SHA, for the directories the plan looks at.
    blobs: dict = field(default_factory=dict)
    #: ``files`` of the manifest the last run wrote; ``{}`` if there is none.
    manifest_files: dict = field(default_factory=dict)
    manifest_sha: str | None = None
    manifest_source_commit: str | None = None
    manifest_unreadable: bool = False
    articles_empty: bool = True


@dataclass
class Plan:
    add: list = field(default_factory=list)
    replace: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    #: In the repository and different from both the new render and what the
    #: manifest recorded: edited there, so left alone.
    edited: list = field(default_factory=list)
    #: Edited there and replaced anyway, under ``--overwrite-edited``.
    overwritten: list = field(default_factory=list)
    #: path -> bytes for the commit, manifest included.
    files: dict = field(default_factory=dict)
    #: path -> blob SHA it must still have for the replace to go ahead.
    expect: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    needs_commit: bool = False


def make_plan(rendered, state, *, source_full, articles_readme, seed, overwrite_edited):
    """Decide, path by path, what a run writes. Pure: no network."""
    plan = Plan()
    new_manifest = {}
    for path, data in rendered.items():
        want = git_blob_sha(data)
        have = state.blobs.get(path)
        recorded = state.manifest_files.get(path)
        if have is None:
            plan.add.append(path)
            plan.files[path] = data
            new_manifest[path] = want
        elif have == want:
            plan.unchanged.append(path)
            new_manifest[path] = want
        elif have == recorded or overwrite_edited:
            plan.replace.append(path)
            plan.files[path] = data
            plan.expect[path] = have
            new_manifest[path] = want
            if have != recorded:
                plan.overwritten.append(path)
        else:
            plan.edited.append(path)
            if recorded:
                new_manifest[path] = recorded

    if state.articles_empty and ARTICLES_README_PATH not in state.blobs:
        plan.add.append(ARTICLES_README_PATH)
        plan.files[ARTICLES_README_PATH] = articles_readme
    elif ARTICLES_README_PATH not in state.blobs:
        plan.notes.append("articles/ has content; articles/README.md is not created.")

    if PUBLICATION_PATH in state.blobs:
        if seed is not None:
            plan.notes.append(
                f"{PUBLICATION_PATH} already exists; --seed-instructions was ignored "
                "and the file was not touched."
            )
    elif seed is not None:
        plan.add.append(PUBLICATION_PATH)
        plan.files[PUBLICATION_PATH] = seed
    else:
        plan.notes.append(
            f"{PUBLICATION_PATH} is not in the repository. Seed it once with "
            "--seed-instructions PATH (the publication's drafting rules); it is "
            "then the author's file and is never touched again."
        )

    if state.manifest_unreadable:
        plan.notes.append(
            f"{MANIFEST_PATH} could not be read as JSON; every managed file that "
            "differs is treated as edited. A commit will rewrite the manifest."
        )

    plan.needs_commit = bool(
        plan.files
        or new_manifest != state.manifest_files
        or state.manifest_sha is None
        or state.manifest_unreadable
    )
    if plan.needs_commit:
        plan.files[MANIFEST_PATH] = manifest_bytes(source_full, new_manifest)
        if state.manifest_sha is not None:
            plan.expect[MANIFEST_PATH] = state.manifest_sha
    return plan


def manifest_bytes(source_full, files):
    return (
        json.dumps(
            {"source_commit": source_full, "files": dict(sorted(files.items()))},
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def parse_manifest(data):
    """``(source_commit, files)`` or ``None`` if ``data`` is not a manifest."""
    try:
        body = json.loads(data.decode("utf-8"))
        files = body["files"]
        if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            return None
        return body.get("source_commit"), files
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def read_state(gh, branch, managed_paths):
    """Look at the document repository once and return a ``RepoState``."""
    state = RepoState()
    dirs = {"", "articles", ".ci-docs", "instructions"}
    dirs.update(p.rsplit("/", 1)[0] for p in managed_paths if "/" in p)
    for d in sorted(dirs):
        entries = gh.list_dir(d, branch)
        if d == "articles":
            state.articles_empty = not entries
        for e in entries:
            if e.type == "file":
                state.blobs[e.path] = e.sha
    state.manifest_sha = state.blobs.get(MANIFEST_PATH)
    if state.manifest_sha is not None:
        parsed = parse_manifest(gh.read(MANIFEST_PATH, branch).data)
        if parsed is None:
            state.manifest_unreadable = True
        else:
            state.manifest_source_commit, state.manifest_files = parsed
    return state


def commit_message(short, plan):
    lines = [f"onboarding: refresh from content-intelligence {short}", ""]
    for label, paths in (
        ("Added", plan.add),
        ("Replaced", [p for p in plan.replace if p not in plan.overwritten]),
        ("Replaced although edited here", plan.overwritten),
    ):
        if paths:
            lines.append(f"{label}: {', '.join(paths)}")
    if plan.edited:
        lines.append(f"Left alone, edited here: {', '.join(plan.edited)}")
    return "\n".join(lines).rstrip() + "\n"
