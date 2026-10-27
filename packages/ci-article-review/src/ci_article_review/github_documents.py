"""Read one round of an article from a GitHub document repository (``--github``).

A round is a branch ``round/<slug>/rN`` with an open pull request into the base
branch (design: ``docs/GITHUB-DOCUMENTS.md``). The article is a folder named by
its slug, under an optional ``path_prefix``, with fixed file names::

    <path_prefix>/<slug>/draft.md       the draft ...
    <path_prefix>/<slug>/metadata.md    ... and its metadata (the two-file form)
    <path_prefix>/<slug>/handoff.md     Template A in one file (what the import writes)

This module turns that folder, as it is at the head of the round's pull request
(or at a tag, or at any ref), into the same handoff dict ``--draft``,
``--raw-draft`` and ``--url`` hand to ``run_draft_pipeline``, so the review
itself does not change. Nothing is written back here (``github_writeback`` does
that); with ``write_back=True`` the read also runs the write-back pre-flight, so
a refusal happens before any model is called.

Every read is pinned to one commit SHA, resolved once, so a draft and its
metadata can never come from two different commits even if the branch moves
while the run starts. Every failure is raised before a model is called, as a
``GitHubDocumentsError`` (a ``ValueError``, which ``pipeline.main`` already
turns into one plain message and exit 1).
"""

import json
import logging
import re
import time
from dataclasses import dataclass
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

#: The part of a round's branch or tag name after the slug: ``r4``.
_ROUND = re.compile(r"^r([1-9][0-9]*)$")

#: The fixed names a round's folder uses.
DRAFT_FILE = "draft.md"
METADATA_FILE = "metadata.md"
HANDOFF_FILE = "handoff.md"
#: Written by the write-back (#417) beside the run's other files; its
#: ``run_number`` is how a run made on another machine is known here.
RUN_FILE = "pipeline/run.json"


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


def round_branch(slug, number):
    """The branch of round ``number``: ``round/<slug>/rN``."""
    return f"round/{slug}/r{number}"


def round_tag(slug, number):
    """The tag of an accepted round: ``<slug>/rN`` (deliberately not the branch's name)."""
    return f"{slug}/r{number}"


def _round_of(name, slug_prefix):
    """The round number in ``name`` after ``slug_prefix``, or ``None``."""
    if not name.startswith(slug_prefix):
        return None
    m = _ROUND.match(name[len(slug_prefix) :])
    return int(m.group(1)) if m else None


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


def _build_handoff(slug, kinds, texts):
    """The handoff for one round from the file texts keyed by kind.

    Returns ``(handoff, declared)``: ``declared`` is whether the text carries a
    ``Pipeline run:`` line. The parsers answer 1 for a missing line as well as
    for one that says 1, so the absence is read from the text, not the dict.
    """
    if "draft" in kinds and "metadata" in kinds:
        handoff = build_handoff_from_raw_draft_and_metadata(
            texts["draft"], texts["metadata"], source_name=slug
        )
        declared = declares_run_number(texts["metadata"])
    elif "draft" in kinds:
        # The same lost-context warning --raw-draft prints comes from the builder.
        handoff = build_handoff_from_raw_text(texts["draft"], source_name=slug)
        declared = False
    else:
        handoff = parse_draft_submission(texts["handoff"])
        declared = declares_run_number(texts["handoff"])
    return handoff, declared


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


@dataclass(frozen=True)
class _Target:
    """What one run reads: a ref, the commit it is pinned to, the round and pull request."""

    ref: str
    commit: str
    round: int | None
    pull: object | None


def load_handoff(
    slug,
    *,
    settings,
    repo=None,
    ref=None,
    round_number=None,
    pull_request=None,
    client=None,
    has_token=True,
    token_lookup=resolve_token,
    write_back=False,
):
    """Read one round of ``slug`` as a handoff.

    What is read depends on which of ``pull_request`` (``--pr``), ``round_number``
    (``--round``) and ``ref`` (``--github-ref``) is given; at most one may be:

    - none: the head commit of the article's one open pull request whose branch
      starts ``round/<slug>/``;
    - ``pull_request``: the head commit of that pull request;
    - ``round_number``: the open pull request of that round if there is one, else
      the tag ``<slug>/rN``;
    - ``ref``: that branch, tag or commit, as it is. Never written back.

    ``settings`` is the ``github_documents`` config block; its ``branch`` is the
    base branch rounds merge into (default: the repository's default) and ``repo``
    is overridden by ``repo``. ``client`` stands in for the GitHub repository
    (``info``, ``resolve``, ``list_dir``, ``read``, ``list_pulls``, ``get_pull``,
    ``matching_refs``, ``merge_base``); when absent, one is built with the token
    ``token_lookup`` finds.

    The handoff carries a ``document_source`` entry naming the repository, the
    ref, the commit it was read at, the round, the pull request, its merge base
    and each file's blob SHA, and ``taken_run_numbers``: the run numbers already
    recorded in ``pipeline/run.json`` at that commit and at the base branch, which
    the pipeline's run-number bump moves past. Round and run numbers are separate:
    ``run_number`` is the handoff's ``Pipeline run:`` line when it has one, and
    ``run_number_declared`` says whether it did.

    ``write_back=True`` also checks, before anything else is paid for, that the
    review can be committed back: a token, push permission, a private repository
    (unless ``allow_public_repo``), and an open pull request to commit to.
    """
    validate_slug(slug)
    given = [
        name
        for name, value in (
            ("pull_request", pull_request),
            ("round_number", round_number),
            ("ref", ref),
        )
        if value is not None
    ]
    if len(given) > 1:
        raise GitHubDocumentsError(
            "--pr, --round and --github-ref name the same thing three ways; "
            "pass only one of them."
        )
    if write_back and ref is not None:
        raise GitHubDocumentsError(
            "--github-ref reads a ref as it is and never writes back: drop "
            "--write-back, or review the round's pull request instead."
        )
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
        if write_back and not has_token:
            raise GitHubDocumentsError(
                "--write-back needs a GitHub token to commit with. Set "
                "GITHUB_TOKEN or run `gh auth login`."
            )
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
            base_branch=settings["branch"],
            ref=ref,
            round_number=round_number,
            pull_request=pull_request,
            has_token=has_token,
            write_back=write_back,
            allow_public_repo=settings["allow_public_repo"] is True,
        )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(
            _explain(exc, repo_name, has_token=has_token)
        ) from exc
    finally:
        close = getattr(client, "close", None)
        if close:
            close()


def _load(
    client,
    slug,
    *,
    repo_name,
    prefix,
    base_branch,
    ref,
    round_number,
    pull_request,
    has_token,
    write_back=False,
    allow_public_repo=False,
):
    started = time.monotonic()
    info = client.info()
    if write_back:
        _check_can_write(info, repo_name, allow_public_repo)
        # Tagging runs only from commands that write: a read without
        # --write-back stays read-only. Nothing is written before this point.
        _tag_accepted_rounds(client, slug, repo_name=repo_name)
    elif not info.private:
        log.warning(
            "%s is a public repository: drafts committed there are published.",
            repo_name,
        )
    base_branch = base_branch or info.default_branch

    target = _choose_target(
        client,
        slug,
        repo_name=repo_name,
        has_token=has_token,
        ref=ref,
        round_number=round_number,
        pull_request=pull_request,
        write_back=write_back,
    )
    if write_back and not client.has_branch(target.ref):
        raise GitHubDocumentsError(
            f"--write-back commits to the round's branch, and {target.ref!r} is "
            f"not a branch of {repo_name}. Drop --write-back."
        )
    commit, pull = target.commit, target.pull

    folder = _folder(prefix, slug)
    entries = client.list_dir(folder, commit)
    if not entries:
        raise GitHubDocumentsError(
            _missing_folder(client, repo_name, folder, prefix, target.ref, commit)
        )
    present = {e.name for e in entries if e.type == "file"}
    if DRAFT_FILE in present:
        wanted = {"draft": DRAFT_FILE}
        if METADATA_FILE in present:
            wanted["metadata"] = METADATA_FILE
        if HANDOFF_FILE in present:
            log.warning(
                "%s/%s is also in this round's folder and is not read: %s is "
                "the draft. A handoff.md left over from an import goes stale "
                "once the draft is revised; delete it from the branch.",
                folder,
                HANDOFF_FILE,
                DRAFT_FILE,
            )
    elif HANDOFF_FILE in present:
        wanted = {"handoff": HANDOFF_FILE}
    else:
        raise GitHubDocumentsError(
            f"{folder}/ has neither {DRAFT_FILE} nor {HANDOFF_FILE} at "
            f"{target.ref} ({commit[:12]}), so there is nothing to review."
        )
    texts, files = {}, []
    for kind, name in wanted.items():
        path = f"{folder}/{name}"
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

    handoff, declared = _build_handoff(slug, wanted, texts)
    _set_history_key(handoff, slug)

    base_commit = _try_resolve(client, base_branch)
    # The run number is the handoff's own line when it has one. Otherwise the
    # pipeline numbers the run one past every number it knows of (local history
    # and these), so an absent line never means "run 1" for a later round.
    handoff["run_number_declared"] = declared
    handoff["taken_run_numbers"] = _taken_run_numbers(
        client, folder, [commit, base_commit]
    )

    base_sha = None
    if pull is not None:
        base_sha = client.merge_base(
            pull.base_sha or base_commit or base_branch, commit
        )
    handoff["document_source"] = {
        "kind": "github",
        "repo": repo_name,
        "ref": target.ref,
        "commit_sha": commit,
        "round": target.round,
        "pull_request": pull.number if pull is not None else None,
        "base_sha": base_sha,
        "files": files,
    }
    log.info(
        "Read %s%s from %s at %s (%d file%s, %.1fs)",
        slug,
        f" round {target.round}" if target.round is not None else f" at {target.ref}",
        repo_name,
        commit[:12],
        len(files),
        "" if len(files) == 1 else "s",
        time.monotonic() - started,
    )
    return handoff


def _tag_accepted_rounds(client, slug, *, repo_name):
    """The place where #418 tags rounds whose pull request has been merged.

    Called from a read only when it is about to write back, after the
    push-permission check and before the round is chosen, so a ``--github`` run
    without ``--write-back`` never reaches it and makes no write. It does nothing
    until #418 fills it.
    """


def _try_resolve(client, ref):
    """The commit ``ref`` names, or ``None`` when there is no such ref."""
    try:
        return client.resolve(ref)
    except GitHubRepoError as exc:
        if exc.kind != "not_found":
            raise
        log.debug("No commit for %r; nothing is read from it.", ref)
        return None


def _taken_run_numbers(client, folder, commits):
    """The ``run_number`` of ``pipeline/run.json`` at each commit, as a sorted list.

    A run made on another machine is known only there. The file is written by
    the write-back, so it may be absent, unreadable or without a usable number:
    each of those is skipped with a DEBUG line, because a numbering nicety must
    never fail a run.
    """
    path = f"{folder}/{RUN_FILE}"
    taken = set()
    for commit in dict.fromkeys(c for c in commits if c):
        try:
            got = client.read(path, commit)
        except GitHubRepoError as exc:
            if exc.kind != "not_found":
                raise
            log.debug("%s is absent at %s.", path, commit[:12])
            continue
        try:
            number = json.loads(got.data.decode("utf-8")).get("run_number")
        except (ValueError, AttributeError) as exc:
            log.debug("%s at %s is unreadable: %s", path, commit[:12], exc)
            continue
        if isinstance(number, int) and not isinstance(number, bool) and number >= 1:
            taken.add(number)
        else:
            log.debug("%s at %s has no integer run_number.", path, commit[:12])
    return sorted(taken)


def _choose_target(
    client,
    slug,
    *,
    repo_name,
    has_token,
    ref,
    round_number,
    pull_request,
    write_back,
):
    """Which ref to read and which commit it is pinned to."""
    if ref is not None:
        return _Target(ref, _resolve_ref(client, ref, repo_name, has_token), None, None)

    if pull_request is not None:
        pull = _get_pull(client, slug, repo_name, pull_request)
        return _pull_target(client, slug, repo_name, pull, write_back)

    if round_number is not None:
        branch = round_branch(slug, round_number)
        matching = [p for p in _open_rounds(client, slug) if p.head_ref == branch]
        if len(matching) > 1:
            raise GitHubDocumentsError(
                f"{repo_name} has {len(matching)} open pull requests from "
                f"{branch}: {_describe(matching)}. Pass --pr N to choose one."
            )
        if matching:
            return _pull_target(client, slug, repo_name, matching[0], write_back)
        tags = _tags(client, slug)
        if round_number in tags:
            if write_back:
                raise GitHubDocumentsError(
                    f"Round {round_number} of {slug} is closed: it is the tag "
                    f"{round_tag(slug, round_number)}, and --write-back only "
                    "writes to an open pull request. Drop --write-back to review "
                    "it, or pass --pr for the round that is open."
                )
            name = round_tag(slug, round_number)
            return _Target(
                name,
                _resolve_ref(client, name, repo_name, has_token),
                round_number,
                None,
            )
        raise GitHubDocumentsError(
            f"{repo_name} has no round {round_number} of {slug}: no open pull "
            f"request from {branch} and no tag {round_tag(slug, round_number)}. "
            + _rounds_that_exist(slug, tags, _open_rounds(client, slug))
        )

    opened = _open_rounds(client, slug)
    if not opened:
        tags = _tags(client, slug)
        listed = (
            "Its rounds that were accepted: "
            + ", ".join(round_tag(slug, n) for n in sorted(tags))
            + ". Pass --round N to review one of them."
            if tags
            else "It has no tags for this article either."
        )
        raise GitHubDocumentsError(
            f"{repo_name} has no open round of {slug} (no open pull request from "
            f"a branch round/{slug}/...). {listed} To start a new round, open "
            "one as the repository's AGENTS.md describes; to read a branch, tag "
            "or commit as it is, pass --github-ref (for example --github-ref main)."
        )
    if len(opened) > 1:
        raise GitHubDocumentsError(
            f"{repo_name} has {len(opened)} open rounds of {slug}: "
            f"{_describe(opened)}. Pass --pr N to choose one."
        )
    return _pull_target(client, slug, repo_name, opened[0], write_back)


def _resolve_ref(client, ref, repo_name, has_token):
    try:
        return client.resolve(ref)
    except GitHubRepoError as exc:
        if exc.kind == "not_found":
            raise GitHubDocumentsError(
                _explain(exc, repo_name, has_token=has_token, ref=ref)
            ) from exc
        raise


def _open_rounds(client, slug):
    """The article's open pull requests: those from a ``round/<slug>/`` branch."""
    prefix = f"round/{slug}/"
    found = [p for p in client.list_pulls("open") if p.head_ref.startswith(prefix)]
    # GitHub lists the newest first; messages read in round order.
    return sorted(found, key=lambda p: p.number)


def _tags(client, slug):
    """``{round: tag name}`` for the article's well-formed tags ``<slug>/rN``."""
    found = {}
    for full, _sha in client.matching_refs(f"tags/{slug}/"):
        name = full[len("refs/tags/") :] if full.startswith("refs/tags/") else full
        number = _round_of(name, f"{slug}/")
        if number is not None:
            found[number] = name
    return found


def _describe(pulls):
    return "; ".join(f'#{p.number} {p.head_ref} "{p.title}"' for p in pulls)


def _rounds_that_exist(slug, tags, opened):
    found = [(n, f"r{n} (tag)") for n in tags]
    for p in opened:
        n = _round_of(p.head_ref, f"round/{slug}/")
        label = f"r{n}" if n is not None else p.head_ref
        found.append((n or 0, f"{label} (open pull request #{p.number})"))
    if not found:
        return "It has no rounds of this article yet."
    return "Rounds that exist: " + ", ".join(text for _, text in sorted(found)) + "."


def _get_pull(client, slug, repo_name, number):
    try:
        pull = client.get_pull(number)
    except GitHubRepoError as exc:
        if exc.kind == "not_found":
            raise GitHubDocumentsError(
                f"{repo_name} has no pull request #{number}, or the repository "
                "is not visible to this token."
            ) from exc
        raise
    prefix = f"round/{slug}/"
    if not pull.head_ref.startswith(prefix):
        raise GitHubDocumentsError(
            f"Pull request #{number} is from {pull.head_ref!r}, not a round of "
            f"{slug} (its branch would start {prefix})."
        )
    return pull


def _pull_target(client, slug, repo_name, pull, write_back):
    """A round's pull request as a ``_Target``, after the checks that apply to all."""
    if pull.head_repo.lower() != repo_name.lower():
        where = pull.head_repo or "a repository that no longer exists"
        raise GitHubDocumentsError(
            f"Pull request #{pull.number} comes from {where}, not from a branch "
            f"of {repo_name}. A fork's branch is not read or written: ask for "
            "the round to be opened from a branch of this repository."
        )
    if write_back and pull.state != "open":
        state = "merged" if pull.merged else pull.state
        raise GitHubDocumentsError(
            f"Pull request #{pull.number} is {state}, so --write-back has "
            "nothing to commit to. Drop --write-back to review it as it is."
        )
    commit = pull.head_sha
    if pull.state == "open":
        # GitHub's pull request head pointer can lag a push; the branch is the
        # truth. A closed pull request keeps the head it ended with.
        tip = _try_resolve(client, pull.head_ref)
        if tip and tip != commit:
            log.warning(
                "Pull request #%d reports its head as %s, but %s is at %s: "
                "GitHub's pull request pointer is lagging a push. Reading the "
                "branch.",
                pull.number,
                commit[:12],
                pull.head_ref,
                tip[:12],
            )
            commit = tip
    return _Target(
        pull.head_ref, commit, _round_of(pull.head_ref, f"round/{slug}/"), pull
    )


def _check_can_write(info, repo_name, allow_public_repo):
    if not info.private:
        if not allow_public_repo:
            raise GitHubDocumentsError(
                f"{repo_name} is a public repository: drafts committed there are "
                "published, so --write-back will not write to it. Make it "
                "private, or set allow_public_repo: true under github_documents."
            )
        log.warning(
            "%s is a public repository and allow_public_repo is set: the review "
            "will be published.",
            repo_name,
        )
    if not info.can_push:
        raise GitHubDocumentsError(
            f"This token cannot push to {repo_name} (no write permission), so "
            "--write-back cannot commit the review. Use a token with Contents "
            "read and write, or drop --write-back."
        )


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
