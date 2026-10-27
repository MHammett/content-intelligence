"""``ci-docs ls`` and ``ci-docs put``: look at an article's folder, and add a file to it.

``ls`` lists the article folder (the top level, then ``pipeline/``) with each
file's round and kind read from its name. ``put`` commits one local file into
the folder, which is how a Code session hands a close-out note
(``handoff_templates/close_out.md``, Template D) to the drafting side through the
repository instead of through Drive. It also takes the drafting side's own files,
for an author who commits on the command line instead of through a connector.

The names are the ones ``docs/CLOSE-OUT.md`` and the document repository's
``AGENTS.md`` define::

    <slug>-(draft|metadata|handoff|publication)-rN.md
    <slug>-close-out-rN-to-(code|chat)[-vM].md          M is 2 or more

``put`` only ever adds a path. A file already there with the same bytes is a
no-op; one with different bytes is refused, and for a close-out note the refusal
names the next free ``-vM``. A ``-vM`` whose predecessor is missing is refused.
One commit, one file, the message ``<kind>: <slug> r<N>[ to-<dir>][ v<M>]``. The
configured API keys and the token must not appear in the file, and a public
repository is refused unless ``allow_public_repo`` is set.

Every refusal that can be decided locally is a ``GitHubDocumentsError`` raised
before anything is sent.
"""

import re
from pathlib import Path

from ci_core.github_repo import (
    GitHubRepo,
    GitHubRepoError,
    git_blob_sha,
    resolve_token,
)

from .github_documents import (
    GitHubDocumentsError,
    _explain,
    _folder,
    _missing_folder,
    validate_slug,
)
from .github_writeback import _lf, secret_values

#: Kinds of file the drafting side writes, one per name.
DRAFTING_KINDS = ("draft", "metadata", "handoff", "publication")

#: How ``ls`` orders kinds within one round.
_KIND_ORDER = (
    "handoff",
    "draft",
    "metadata",
    "publication",
    "close-out",
    "review",
    "worklist",
    "report",
    "results",
)

_RUN_FILE = re.compile(
    r"^run_(?P<run>[1-9][0-9]*)_\d{8}_\d{6}_(?P<what>report|results)\.json$"
)

#: ``pipeline/`` is the one subfolder the pipeline writes.
PIPELINE_DIR = "pipeline"


def parse_name(slug, name):
    """What an article file's name says: ``{kind, round, direction, version}``, or ``None``.

    ``direction`` and ``version`` are set for a close-out note only (the
    unsuffixed note is version 1). ``round`` is the round of drafting for a
    draft, metadata, handoff or publication file and the pipeline run number
    for a review, worklist, close-out note or run file. The match is exact:
    ``-r04`` and trailing text are not names.
    """
    s = re.escape(slug)
    m = re.match(
        rf"^{s}-(draft|metadata|handoff|publication|review|worklist)-r([1-9][0-9]*)\.md$",
        name,
    )
    if m:
        return {
            "kind": m.group(1),
            "round": int(m.group(2)),
            "direction": None,
            "version": None,
        }
    m = re.match(
        rf"^{s}-close-out-r([1-9][0-9]*)-to-(code|chat)(?:-v([2-9]|[1-9][0-9]+))?\.md$",
        name,
    )
    if m:
        return {
            "kind": "close-out",
            "round": int(m.group(1)),
            "direction": m.group(2),
            "version": int(m.group(3)) if m.group(3) else 1,
        }
    m = _RUN_FILE.match(name)
    if m:
        return {
            "kind": m.group("what"),
            "round": int(m.group("run")),
            "direction": None,
            "version": None,
        }
    return None


def close_out_name(slug, round_number, direction, version):
    suffix = f"-v{version}" if version > 1 else ""
    return f"{slug}-close-out-r{round_number}-to-{direction}{suffix}.md"


def commit_message(slug, parsed):
    """``<kind>: <slug> r<N>[ to-<dir>][ v<M>]``."""
    text = f"{parsed['kind']}: {slug} r{parsed['round']}"
    if parsed["direction"]:
        text += f" to-{parsed['direction']}"
    if parsed["version"] and parsed["version"] > 1:
        text += f" v{parsed['version']}"
    return text


def describe(parsed):
    """The ``ls`` columns for a parsed name: kind and the detail beside it."""
    if parsed is None:
        return "other", ""
    if parsed["kind"] == "close-out":
        detail = f"to-{parsed['direction']}"
        if parsed["version"] > 1:
            detail += f" v{parsed['version']}"
        return "close-out", detail
    return parsed["kind"], ""


def check_name(slug, name):
    """The parsed name, or a refusal that shows the accepted patterns."""
    parsed = parse_name(slug, name)
    if parsed is None or parsed["kind"] not in DRAFTING_KINDS + ("close-out",):
        kind = parsed["kind"] if parsed else None
        why = (
            f"{name} is a {kind} file, which the pipeline writes (ci-review "
            "--write-back, ci-docs push-run), so put will not add it."
            if kind
            else f"{name} is not a name put accepts for the article {slug!r}."
        )
        raise GitHubDocumentsError(
            f"{why} The file name must be one of:\n"
            f"  {slug}-(draft|metadata|handoff|publication)-rN.md\n"
            f"  {slug}-close-out-rN-to-(code|chat)[-vM].md   (M is 2 or more)\n"
            f"with N from 1, and the slug {slug!r} exactly as the article folder "
            "is named."
        )
    return parsed


def _versions_present(names, slug, parsed):
    """Versions of the close-out note for this round and direction already in the folder."""
    found = set()
    for name in names:
        other = parse_name(slug, name)
        if (
            other
            and other["kind"] == "close-out"
            and other["round"] == parsed["round"]
            and other["direction"] == parsed["direction"]
        ):
            found.add(other["version"])
    return found


def check_close_out(slug, parsed, data, entries):
    """Refuse a close-out note that would clash with, or skip, an earlier one.

    ``entries`` are the article folder's top-level entries. A note whose exact
    name is there with the same bytes passes (the commit will find nothing to
    write); with different bytes it is refused with the next free ``-vM``.
    """
    files = {e.name: e for e in entries if e.type == "file"}
    present = _versions_present(files, slug, parsed)
    round_number, direction = parsed["round"], parsed["direction"]
    version = parsed["version"]
    here = close_out_name(slug, round_number, direction, version)

    if version in present:
        if files[here].sha == git_blob_sha(data):
            return
    elif version == 1:
        if not present:
            return  # the first note for this round and direction
    elif version - 1 in present:
        return
    else:
        before = close_out_name(slug, round_number, direction, version - 1)
        raise GitHubDocumentsError(
            f"{here} is a rewrite of {before}, which is not in the folder. A -vM "
            "note supersedes the one before it, so that has to exist first."
        )

    have = ", ".join(
        close_out_name(slug, round_number, direction, v) for v in sorted(present)
    )
    free = close_out_name(slug, round_number, direction, max(present) + 1)
    raise GitHubDocumentsError(
        f"A close-out note for round {round_number} to-{direction} is already in "
        f"the folder ({have}). Earlier notes are never overwritten. Save this "
        f"one as {free} and have its Supersedes: line name the note it replaces."
    )


def _read_local(path):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GitHubDocumentsError(f"Cannot read {path}: {exc}") from exc
    if not raw.strip():
        raise GitHubDocumentsError(f"{path} is empty; there is nothing to commit.")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubDocumentsError(f"{path} is not UTF-8 text: {exc}") from exc
    return _lf(raw)


def _client(repo_name, token, session):
    try:
        return GitHubRepo(repo_name, token, session=session)
    except ValueError as exc:
        raise GitHubDocumentsError(str(exc)) from exc


def _repo_name(settings, repo):
    name = settings["repo"] if repo is None else repo
    if not isinstance(name, str) or not name.strip():
        raise GitHubDocumentsError(
            "No document repository is set: github_documents.repo is empty. Name "
            "one as OWNER/NAME there, or pass --github-repo."
        )
    return name.strip()


def _prefix(settings):
    return (settings["path_prefix"] or "").strip().strip("/")


def put_file(
    slug,
    local_file,
    *,
    settings,
    api_keys,
    repo=None,
    branch=None,
    token_lookup=resolve_token,
    session=None,
    out=print,
):
    """Commit ``local_file`` into the article folder; returns the ``CommitResult``.

    ``settings`` is the normalised ``github_documents`` block; ``api_keys`` is the
    merged key table, none of whose values may appear in the file.
    """
    validate_slug(slug)
    name = Path(local_file).name
    parsed = check_name(slug, name)
    data = _read_local(local_file)
    repo_name = _repo_name(settings, repo)
    token = token_lookup()
    if not token:
        raise GitHubDocumentsError(
            "No GitHub token to commit with. Set GITHUB_TOKEN or run `gh auth login`."
        )
    prefix = _prefix(settings)
    folder = _folder(prefix, slug)

    client = _client(repo_name, token, session)
    try:
        info = client.info()
        if not info.private and settings.get("allow_public_repo") is not True:
            raise GitHubDocumentsError(
                f"{repo_name} is a public repository: what is committed there is "
                "published, so ci-docs put will not write to it. Make it private, "
                "or set allow_public_repo: true under github_documents."
            )
        if not info.can_push:
            raise GitHubDocumentsError(
                f"This token cannot push to {repo_name} (no write permission). "
                "Use a token with Contents read and write."
            )
        ref = branch or settings["branch"] or info.default_branch
        if not client.has_branch(ref):
            raise GitHubDocumentsError(
                f"{repo_name} has no branch {ref!r}; a file is committed onto a branch."
            )
        head = client.resolve(ref)
        entries = client.list_dir(folder, head)
        if parsed["kind"] == "close-out":
            if not entries:
                raise GitHubDocumentsError(
                    _missing_folder(client, repo_name, folder, prefix, ref, head)
                    + " A close-out note is about an article that is already "
                    "there; check the slug."
                )
            check_close_out(slug, parsed, data, entries)
        elif not entries:
            out(f"Note: {folder}/ is not in {repo_name} yet; this creates it.")
        result = client.commit_files(
            ref,
            {f"{folder}/{name}": data},
            commit_message(slug, parsed) + "\n",
            refuse_if_contains=secret_values(api_keys),
        )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(_put_failure(exc, repo_name, folder, name)) from exc
    finally:
        client.close()

    if not result.written:
        out(
            f"Nothing new to write: {folder}/{name} is already in {repo_name} with "
            f"the same bytes. {result.html_url}".rstrip()
        )
    else:
        out(f"Wrote {folder}/{name} to {repo_name}: {result.html_url}")
    return result


def _put_failure(exc, repo_name, folder, name):
    if exc.kind == "exists":
        return (
            f"{folder}/{name} is already in {repo_name} with different content. "
            "Nothing was overwritten. A new round is a new file; a close-out note "
            "that needs rewriting gets the next -vM."
        )
    if exc.kind == "secret":
        return (
            f"{exc}. The file holds a configured API key or the token, so "
            "nothing was sent. Remove it and try again."
        )
    return _explain(exc, repo_name, has_token=True)


def list_article(
    slug,
    *,
    settings,
    repo=None,
    branch=None,
    token_lookup=resolve_token,
    session=None,
):
    """The article folder as ``(header, rows)``.

    ``header`` is a dict naming the repository, folder, ref and commit; each row
    is ``{"path", "name", "kind", "detail", "round", "parsed"}``, top level first
    and ``pipeline/`` after, ordered by round and kind.
    """
    validate_slug(slug)
    repo_name = _repo_name(settings, repo)
    prefix = _prefix(settings)
    folder = _folder(prefix, slug)
    token = token_lookup()
    client = _client(repo_name, token, session)
    try:
        info = client.info()
        ref = branch or settings["branch"] or info.default_branch
        head = client.resolve(ref)
        entries = client.list_dir(folder, head)
        if not entries:
            raise GitHubDocumentsError(
                _missing_folder(client, repo_name, folder, prefix, ref, head)
            )
        piped = client.list_dir(f"{folder}/{PIPELINE_DIR}", head)
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(
            _explain(exc, repo_name, has_token=bool(token))
        ) from exc
    finally:
        client.close()

    rows = []
    for entry in entries + piped:
        if (
            entry.type == "dir"
            and entry.name == PIPELINE_DIR
            and entry.path == f"{folder}/{PIPELINE_DIR}"
        ):
            continue
        parsed = parse_name(slug, entry.name) if entry.type == "file" else None
        kind, detail = describe(parsed) if entry.type == "file" else (entry.type, "")
        rows.append(
            {
                "path": entry.path,
                "name": entry.name,
                "kind": kind,
                "detail": detail,
                "round": parsed["round"] if parsed else None,
                "parsed": parsed,
                "pipeline": entry.path.startswith(f"{folder}/{PIPELINE_DIR}/"),
            }
        )

    def order(row):
        kind_rank = _KIND_ORDER.index(row["kind"]) if row["kind"] in _KIND_ORDER else 99
        version = (row["parsed"] or {}).get("version") or 0
        return (
            row["pipeline"],
            row["round"] is None,
            row["round"] or 0,
            kind_rank,
            row["detail"].split()[0] if row["detail"] else "",
            version,
            row["name"],
        )

    rows.sort(key=order)
    header = {"repo": repo_name, "folder": folder, "ref": ref, "commit_sha": head}
    return header, rows


def format_listing(header, rows):
    """The lines ``ci-docs ls`` prints."""
    lines = [
        f"{header['repo']}  {header['folder']}/  {header['ref']} "
        f"({header['commit_sha'][:7]})"
    ]
    if not rows:
        lines.append("  (empty)")
    section = None
    for row in rows:
        if row["pipeline"] != section:
            section = row["pipeline"]
            if section:
                lines.append(f"  {PIPELINE_DIR}/")
        rnd = f"r{row['round']}" if row["round"] else "-"
        shown = f"{row['kind']} {row['detail']}".rstrip()
        lines.append(f"    {rnd:<5}{shown:<22}{row['name']}")
    return lines
