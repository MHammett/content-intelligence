"""Read one round of an article from a GitHub document repository (``--github``).

An article is a folder named by its slug, under an optional ``path_prefix``::

    <path_prefix>/<slug>/<slug>-handoff-r4.md    Template A, one file
    <path_prefix>/<slug>/<slug>-draft-r4.md      the two-file form: the draft ...
    <path_prefix>/<slug>/<slug>-metadata-r4.md   ... and its metadata

This module turns that folder into the same handoff dict ``--draft``,
``--raw-draft`` and ``--url`` hand to ``run_draft_pipeline``, so the review
itself does not change. Nothing is written back here.

Every read is pinned to one commit SHA, resolved once, so a draft and its
metadata can never come from two different commits even if the branch moves
while the run starts. Every failure is raised before a model is called, as a
``GitHubDocumentsError`` (a ``ValueError``, which ``pipeline.main`` already
turns into one plain message and exit 1).
"""

import logging
import re
import time
from datetime import datetime, timezone

from ci_core.github_repo import GitHubRepo, GitHubRepoError, resolve_token

from . import history as hist
from .config_loader import normalize_github_documents
from .handoff_parser import (
    build_handoff_from_raw_draft_and_metadata,
    build_handoff_from_raw_text,
    declares_run_number,
    parse_draft_submission,
)

log = logging.getLogger(__name__)

#: A slug is a folder name and the stem of every file in it, so it may not carry
#: a path separator, whitespace, or start with a dot.
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GitHubDocumentsError(ValueError):
    """A document could not be read from GitHub; the message says what to do."""


def validate_slug(slug):
    if not slug or not _SLUG.match(slug) or ".." in slug:
        raise GitHubDocumentsError(
            f"--github expects an article slug (letters, digits, '-', '_' and "
            f"'.', e.g. example-article), got {slug!r}."
        )
    return slug


def _folder(path_prefix, slug):
    return f"{path_prefix}/{slug}" if path_prefix else slug


def find_rounds(names, slug):
    """Which files each round has, from the names in an article folder.

    Returns ``{round: {"handoff": name, "draft": name, "metadata": name}}``
    holding only the kinds present, and only the rounds that have a handoff or
    a draft (a lone metadata file is not a round). The match is exact:
    ``<slug>-draft-r4-old.md`` and ``<slug>-draft-r04.md`` are not round 4.
    """
    pattern = re.compile(
        rf"^{re.escape(slug)}-(handoff|draft|metadata)-r([1-9][0-9]*)\.md$"
    )
    rounds = {}
    for name in names:
        m = pattern.match(name)
        if m:
            rounds.setdefault(int(m.group(2)), {})[m.group(1)] = name
    return {
        n: kinds
        for n, kinds in rounds.items()
        if "handoff" in kinds or "draft" in kinds
    }


def pick_round(rounds, requested, slug, folder):
    """The requested round, or the highest when none was asked for."""
    if not rounds:
        raise GitHubDocumentsError(
            f"{folder}/ has no {slug}-handoff-rN.md or {slug}-draft-rN.md file "
            "to review."
        )
    have = sorted(rounds)
    if requested is None:
        return have[-1]
    if requested not in rounds:
        listed = ", ".join(f"r{n}" for n in have)
        raise GitHubDocumentsError(
            f"{folder}/ has no round {requested} (it has {listed})."
        )
    return requested


def _decode(data, path):
    """File bytes as the text ``Path.read_text`` would give for the same file."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubDocumentsError(f"{path} is not UTF-8 text: {exc}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _explain(exc, repo, *, has_token, ref=None):
    """A ``GitHubRepoError`` as one plain message with the next step in it."""
    if exc.kind == "auth":
        return (
            f"GitHub rejected the token for {repo} (401): it is expired or "
            "revoked. Set a fresh GITHUB_TOKEN or run `gh auth login`."
        )
    if exc.kind == "not_found":
        if ref is not None:
            return (
                f"{repo} has no branch, tag or commit {ref!r}, or the repository "
                "is not visible to this token."
            )
        if not has_token:
            return (
                f"{repo} was not found. If it is private, set GITHUB_TOKEN or "
                "run `gh auth login`."
            )
        return (
            f"{repo} is missing, or not visible to this token (GitHub answers 404 "
            "for both). Check the name, and that the token can read it."
        )
    if exc.kind == "forbidden":
        return f"The token cannot read {repo} (403): {exc}"
    if exc.kind == "rate_limited":
        when = ""
        if exc.reset_at:
            when = (
                " It resets at "
                + datetime.fromtimestamp(exc.reset_at, timezone.utc).strftime(
                    "%H:%M:%S UTC"
                )
                + "."
            )
        return f"GitHub's rate limit is used up.{when}"
    if exc.kind == "empty":
        return f"{repo} has no commits yet, so there is nothing to read."
    return f"Could not read {repo} from GitHub: {exc}"


def _build_handoff(slug, round_number, kinds, texts):
    """The handoff for one round, from the file texts keyed by kind."""
    if "handoff" in kinds:
        text = texts["handoff"]
        handoff = parse_draft_submission(text)
        declared = declares_run_number(text)
    elif "metadata" in kinds:
        handoff = build_handoff_from_raw_draft_and_metadata(
            texts["draft"], texts["metadata"], source_name=slug
        )
        declared = declares_run_number(texts["metadata"])
    else:
        # The same lost-context warning --raw-draft prints comes from the builder.
        handoff = build_handoff_from_raw_text(texts["draft"], source_name=slug)
        declared = False
    if not declared:
        # The parsers answer 1 for "no Pipeline run: line" as well as for a
        # line that says 1, so the absence is read from the text, not the dict.
        handoff["run_number"] = round_number
    return handoff


def _set_history_key(handoff, slug):
    """Make the folder and the history directory one string, when we may."""
    declared = (handoff.get("history_key") or "").strip()
    if not declared:
        handoff["history_key"] = slug
        if hist._slug(slug) != slug:
            log.warning(
                "The history directory for %r will be %r, not %r: history names "
                "are lower-cased and stripped of punctuation.",
                slug,
                hist._slug(slug),
                slug,
            )
    elif hist._slug(declared) != slug:
        log.warning(
            "The handoff's 'History key:' is %r but the article folder is %r. "
            "Keeping the History key, because history is the fixed point; the "
            "folder name and pipeline_history/ will not match.",
            declared,
            slug,
        )


def load_handoff(
    slug,
    *,
    settings,
    repo=None,
    ref=None,
    round_number=None,
    client=None,
    has_token=True,
    token_lookup=resolve_token,
):
    """Read round ``round_number`` (default: the highest) of ``slug`` as a handoff.

    ``settings`` is the ``github_documents`` config block. ``repo`` and ``ref``
    override its ``repo`` and ``branch`` for this run. ``client`` stands in for
    the GitHub repository (``info``, ``resolve``, ``list_dir``, ``read``); when
    absent, one is built with the token ``token_lookup`` finds.

    The handoff carries a ``document_source`` entry naming the repository, the
    ref, the commit it was read at, the round and each file's blob SHA.
    """
    validate_slug(slug)
    settings = normalize_github_documents(settings)
    repo_name = settings["repo"] if repo is None else repo
    if not isinstance(repo_name, str) or not repo_name.strip():
        raise GitHubDocumentsError(
            "No document repository is set: github_documents.repo is empty. Name "
            "one as OWNER/NAME there, or pass --github-repo."
        )
    repo_name = repo_name.strip()
    prefix = (settings["path_prefix"] or "").strip().strip("/")

    if client is None:
        token = token_lookup()
        has_token = bool(token)
        try:
            client = GitHubRepo(repo_name, token)
        except ValueError as exc:
            raise GitHubDocumentsError(str(exc)) from exc
    try:
        return _load(
            client,
            slug,
            repo_name=repo_name,
            prefix=prefix,
            ref=ref or settings["branch"],
            round_number=round_number,
            has_token=has_token,
        )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(
            _explain(exc, repo_name, has_token=has_token)
        ) from exc
    finally:
        close = getattr(client, "close", None)
        if close:
            close()


def _load(client, slug, *, repo_name, prefix, ref, round_number, has_token):
    started = time.monotonic()
    info = client.info()
    if not info.private:
        log.warning(
            "%s is a public repository: drafts committed there are published.",
            repo_name,
        )
    ref = ref or info.default_branch
    try:
        commit = client.resolve(ref)
    except GitHubRepoError as exc:
        if exc.kind == "not_found":
            raise GitHubDocumentsError(
                _explain(exc, repo_name, has_token=has_token, ref=ref)
            ) from exc
        raise

    folder = _folder(prefix, slug)
    entries = client.list_dir(folder, commit)
    if not entries:
        raise GitHubDocumentsError(
            _missing_folder(client, repo_name, folder, prefix, ref, commit)
        )
    rounds = find_rounds([e.name for e in entries if e.type == "file"], slug)
    chosen = pick_round(rounds, round_number, slug, folder)
    kinds = rounds[chosen]

    wanted = (
        ["handoff"]
        if "handoff" in kinds
        else [k for k in ("draft", "metadata") if k in kinds]
    )
    texts, files = {}, []
    for kind in wanted:
        path = f"{folder}/{kinds[kind]}"
        try:
            got = client.read(path, commit)
        except GitHubRepoError as exc:
            if exc.kind == "not_found":
                raise GitHubDocumentsError(
                    f"{path} was listed at {commit[:12]} but could not be read."
                ) from exc
            raise
        texts[kind] = _decode(got.data, path)
        files.append({"path": path, "blob_sha": got.blob_sha})

    handoff = _build_handoff(slug, chosen, {k: kinds[k] for k in wanted}, texts)
    _set_history_key(handoff, slug)
    handoff["document_source"] = {
        "kind": "github",
        "repo": repo_name,
        "ref": ref,
        "commit_sha": commit,
        "round": chosen,
        "files": files,
    }
    log.info(
        "Read %s round %d from %s at %s (%d file%s, %.1fs)",
        slug,
        chosen,
        repo_name,
        commit[:12],
        len(files),
        "" if len(files) == 1 else "s",
        time.monotonic() - started,
    )
    return handoff


def _missing_folder(client, repo_name, folder, prefix, ref, commit):
    message = f"{repo_name} has no folder {folder}/ at {ref} ({commit[:12]})."
    try:
        names = sorted(
            e.name for e in client.list_dir(prefix, commit) if e.type == "dir"
        )
    except GitHubRepoError:
        names = []
    if names:
        shown = ", ".join(names[:20]) + (", ..." if len(names) > 20 else "")
        message += f" Articles there: {shown}."
    return message
