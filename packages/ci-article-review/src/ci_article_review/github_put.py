"""``ci-docs ls`` and ``ci-docs put``: look at an article's rounds, and add a file to the open one.

An article in the document repository is a folder with fixed names, one branch
and one pull request per round, and a tag for each accepted round (design:
``docs/GITHUB-DOCUMENTS.md``). ``ls`` shows that: the article's rounds (tags
with their dates, merged and open pull requests with branch, head and the last
review's run), then the files of the folder at a ref with each one's last
commit. It reads and writes nothing.

``put`` commits **one** local file to the open round's branch, never to the base
branch. It is how a Code session hands a close-out note
(``handoff_templates/close_out.md``, Template D) to the drafting chat, and it
takes the drafting side's own files for an author who commits from the command
line. The file name is the name it gets::

    close-out-to-chat.md   close-out-to-code.md   draft.md   metadata.md   publication.md

``review.md`` and everything under ``pipeline/`` are written by the pipeline
(``ci-review --write-back``, ``ci-docs push-run``) and are refused. An existing
file is replaced only through ``replace={path: blob at the head put read}``, so an
edit made in between is refused, and identical bytes make no commit. After a
close-out note's commit it comments on the pull request with a link to the note
at that commit. The configured API keys and the token must not appear in the
file, and a public repository is refused unless ``allow_public_repo`` is set.

Both ``put`` and ``accept`` tag a merged round first (``github_rounds``); ``ls``
never does. Every refusal that can be decided locally is a
``GitHubDocumentsError`` raised before anything is sent.
"""

import re
from pathlib import Path

from ci_core.github_repo import GitHubRepoError, resolve_token

from .github_documents import (
    GitHubDocumentsError,
    _explain,
    _folder,
    _missing_folder,
    _open_rounds,
    _round_of,
    _tags,
    validate_slug,
)
from .github_rounds import (
    _last_review,
    check_can_write,
    open_client,
    open_round_pull,
    path_prefix,
    repo_name,
    require_token,
    round_number_of,
    tag_before_write,
)
from .github_writeback import _lf, secret_values

#: The names ``put`` writes, and the commit message prefix of each.
CLOSE_OUT_NAMES = {
    "close-out-to-chat.md": "to-chat",
    "close-out-to-code.md": "to-code",
}
PUT_NAMES = (
    "close-out-to-chat.md",
    "close-out-to-code.md",
    "draft.md",
    "metadata.md",
    "publication.md",
)

#: ``pipeline/`` is the one subfolder the pipeline writes.
PIPELINE_DIR = "pipeline"

#: A comment on the pull request, for a note's direction.
_COMMENT = {"to-chat": "Close-out note to chat", "to-code": "Close-out note to code"}


def check_name(name, local_file):
    """The file name ``put`` accepts, or a refusal that lists the ones it does."""
    if name in PUT_NAMES:
        return name
    parent = Path(local_file).parent.name
    if name == "review.md" or parent == PIPELINE_DIR:
        why = (
            f"{name} is written by the pipeline (ci-review --write-back, ci-docs "
            "push-run), so put will not add it."
        )
    else:
        why = f"{name} is not a name put accepts."
    raise GitHubDocumentsError(
        f"{why} The file's name is the name it gets in the article's folder, "
        f"and must be one of: {', '.join(PUT_NAMES)}."
    )


def commit_subject(slug, round_number, name):
    """``close-out: <slug> r<N> to-chat``, or ``<kind>: <slug> r<N>``."""
    label = f"r{round_number}" if round_number is not None else "round"
    if name in CLOSE_OUT_NAMES:
        return f"close-out: {slug} {label} {CLOSE_OUT_NAMES[name]}"
    return f"{name[: -len('.md')]}: {slug} {label}"


_HEADER_LINE = re.compile(r"^(Contains|Article|Round)\s*:\s*(.+)$", re.IGNORECASE)


def covers(data, name):
    """One line on what a note covers, from its header, for the commit's body.

    A close-out note's ``Contains:`` line (the counts of what it carries), else
    its ``Article:`` line. Any other file gets none: the diff is its change.
    """
    if name not in CLOSE_OUT_NAMES:
        return ""
    found = {}
    for line in data.decode("utf-8", "replace").splitlines():
        m = _HEADER_LINE.match(line.strip())
        if m and m.group(1).lower() not in found:
            found[m.group(1).lower()] = " ".join(m.group(2).split())
    for key in ("contains", "article"):
        text = found.get(key, "")
        # An unfilled template still holds its bracketed instruction.
        if text and not text.startswith("["):
            return text[:200]
    return ""


def commit_message(slug, round_number, name, data):
    subject = commit_subject(slug, round_number, name)
    body = covers(data, name)
    return f"{subject}\n\n{body}\n" if body else f"{subject}\n"


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


def put_file(
    slug,
    local_file,
    *,
    settings,
    api_keys,
    repo=None,
    pr=None,
    token_lookup=resolve_token,
    session=None,
    out=print,
):
    """Commit ``local_file`` to the open round's branch; returns the ``CommitResult``.

    ``settings`` is the normalised ``github_documents`` block; ``api_keys`` is the
    merged key table, none of whose values may appear in the file. ``pr`` names the
    round's pull request when the article has more than one open.
    """
    validate_slug(slug)
    name = check_name(Path(local_file).name, local_file)
    data = _read_local(local_file)
    repo_full = repo_name(settings, repo)
    token = require_token(token_lookup)
    folder = _folder(path_prefix(settings), slug)
    path = f"{folder}/{name}"

    client = open_client(repo_full, token, session)
    say = out
    try:
        check_can_write(client.info(), repo_full, settings, "ci-docs put")
        # Before our own work, as every writing command does; never fatal.
        tag_before_write(
            client,
            slug,
            repo=repo_full,
            say=say,
            warn=lambda m: out(f"warning: {m}"),
        )
        pull = open_round_pull(client, slug, repo_full, pr)
        branch = pull.head_ref
        round_number = round_number_of(slug, pull)
        # The branch is the truth; the pull request's head pointer can lag a push.
        head = client.resolve(branch)
        if name in CLOSE_OUT_NAMES and not client.list_dir(folder, head):
            raise GitHubDocumentsError(
                _missing_folder(
                    client, repo_full, folder, path_prefix(settings), branch, head
                )
                + " A close-out note is about an article that is already there; "
                "check the slug."
            )
        replace = {}
        try:
            current = client.read(path, head)
            replace[path] = current.blob_sha
        except GitHubRepoError as exc:
            if exc.kind != "not_found":
                raise
        result = client.commit_files(
            branch,
            {path: data},
            commit_message(slug, round_number, name, data),
            replace=replace,
            refuse_if_contains=secret_values(api_keys),
        )
        if not result.written:
            out(
                f"Nothing new to write: {path} is already on {branch} with the "
                f"same bytes. {result.html_url}".rstrip()
            )
        else:
            verb = "Replaced" if replace else "Added"
            out(f"{verb} {path} on {branch}: {result.html_url}")
            if name in CLOSE_OUT_NAMES:
                _announce_note(
                    client, repo_full, pull, path, result.commit_sha, name, out
                )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(_put_failure(exc, repo_full, path)) from exc
    finally:
        client.close()
    return result


def _announce_note(client, repo_full, pull, path, commit, name, out):
    """Comment on the pull request with a link to the note at its new commit."""
    link = f"https://github.com/{repo_full}/blob/{commit}/{path}"
    text = f"{_COMMENT[CLOSE_OUT_NAMES[name]]}: {link}"
    try:
        url = client.comment_on_issue(pull.number, text)
    except GitHubRepoError as exc:
        # The note is committed; only the announcement failed.
        out(
            f"warning: the note is committed but the comment on pull request "
            f"#{pull.number} was not posted: {exc}"
        )
        return
    out(f"Commented on pull request #{pull.number}: {url}")


def _put_failure(exc, repo_full, path):
    if exc.kind == "exists":
        return (
            f"{path} was changed by someone else after put read it, so nothing "
            "was overwritten. Run ci-docs put again to start from what is there."
        )
    if exc.kind == "secret":
        return (
            f"{exc}. The file holds a configured API key or the token, so "
            "nothing was sent. Remove it and try again."
        )
    return _explain(exc, repo_full, has_token=True)


# -- ls ---------------------------------------------------------------------------


def _date(text):
    return (text or "")[:10]


def _round_rows(client, slug):
    """The article's rounds, ascending: tags and round pull requests joined by round number."""
    prefix = f"round/{slug}/"
    tags = {}
    for number, tag in _tags(client, slug).items():
        tags[number] = client.get_tag(tag)
    pulls = {}
    for pull in client.list_pulls("all"):
        if not pull.head_ref.startswith(prefix):
            continue
        pulls.setdefault(_round_of(pull.head_ref, prefix), []).append(pull)
    rows = []
    for number in sorted(set(tags) | {n for n in pulls if n is not None}):
        entry = {"round": number, "tag": tags.get(number), "pulls": []}
        for pull in sorted(pulls.get(number, []), key=lambda p: p.number):
            review = _last_review(client, pull.number)
            entry["pulls"].append(
                {
                    "pull": pull,
                    "state": "merged"
                    if pull.merged
                    else ("open" if pull.state == "open" else "closed"),
                    "run": review[1] if review else None,
                    "review_commit": review[0].commit_id if review else None,
                }
            )
        rows.append(entry)
    # Pull requests from a round/<slug>/ branch that is not rN: listed last.
    for pull in pulls.get(None, []):
        review = _last_review(client, pull.number)
        rows.append(
            {
                "round": None,
                "tag": None,
                "pulls": [
                    {
                        "pull": pull,
                        "state": "merged"
                        if pull.merged
                        else ("open" if pull.state == "open" else "closed"),
                        "run": review[1] if review else None,
                        "review_commit": review[0].commit_id if review else None,
                    }
                ],
            }
        )
    return rows


def list_article(
    slug,
    *,
    settings,
    repo=None,
    ref=None,
    token_lookup=resolve_token,
    session=None,
):
    """The article as ``(header, rounds, files)``. Reads only.

    ``rounds`` are the article's tags and round pull requests, ascending. ``files``
    are the folder's files at ``ref`` (the open round's branch when there is
    exactly one, else the base branch), the top level first and ``pipeline/``
    after, each with its last commit.
    """
    validate_slug(slug)
    name = repo_name(settings, repo)
    prefix = path_prefix(settings)
    folder = _folder(prefix, slug)
    token = token_lookup()
    client = open_client(name, token, session)
    try:
        info = client.info()
        rounds = _round_rows(client, slug)
        opened = _open_rounds(client, slug)
        note = None
        if ref is None:
            if len(opened) == 1:
                ref = opened[0].head_ref
            else:
                ref = settings["branch"] or info.default_branch
                if len(opened) > 1:
                    note = (
                        f"{len(opened)} rounds are open, so the files are the "
                        f"{ref} branch's; --ref round/{slug}/rN lists one round's."
                    )
        head = client.resolve(ref)
        entries = client.list_dir(folder, head)
        if not entries and not rounds:
            raise GitHubDocumentsError(
                _missing_folder(client, name, folder, prefix, ref, head)
            )
        piped = client.list_dir(f"{folder}/{PIPELINE_DIR}", head) if entries else []
        files = []
        for entry in entries + piped:
            if entry.type != "file":
                continue
            last = client.last_commit(entry.path, head)
            files.append(
                {
                    "path": entry.path,
                    "name": entry.name,
                    "pipeline": entry.path.startswith(f"{folder}/{PIPELINE_DIR}/"),
                    "last": last,
                }
            )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(_explain(exc, name, has_token=bool(token))) from exc
    finally:
        client.close()
    files.sort(key=lambda f: (f["pipeline"], f["name"]))
    header = {
        "repo": name,
        "folder": folder,
        "ref": ref,
        "commit_sha": head,
        "has_folder": bool(entries),
        "note": note,
    }
    return header, rounds, files


def _round_state(entry):
    states = [p["state"] for p in entry["pulls"]]
    if entry["tag"] is not None:
        return "accepted"
    if "open" in states:
        return "open"
    if "merged" in states:
        return "merged, not yet tagged"
    return "closed without a merge"


def format_listing(header, rounds, files):
    """The lines ``ci-docs ls`` prints."""
    lines = [f"{header['repo']}  {header['folder']}/"]
    lines.append("Rounds")
    if not rounds:
        lines.append("  (none: no tag and no pull request from a round branch)")
    for entry in rounds:
        label = f"r{entry['round']}" if entry["round"] is not None else "other"
        tag = entry["tag"]
        detail = []
        if tag is not None:
            when = _date(tag.tagged_at)
            commit = tag.commit_sha[:7] if tag.commit_sha else "?"
            detail.append(
                f"tag {tag.name}" + (f" {when}" if when else "") + f" at {commit}"
            )
        lines.append(
            f"  {label:<6}{_round_state(entry)}" + (f"  {detail[0]}" if detail else "")
        )
        for item in entry["pulls"]:
            pull = item["pull"]
            parts = [f"#{pull.number} {item['state']}", pull.head_ref]
            if item["state"] == "open":
                parts.append(f"head {pull.head_sha[:7]}")
            elif item["state"] == "merged":
                parts.append(
                    f"merge {pull.merge_commit_sha[:7] if pull.merge_commit_sha else '?'}"
                )
                if pull.merged_at:
                    parts.append(_date(pull.merged_at))
            parts.append(
                f"last review: run {item['run']} of {item['review_commit'][:7]}"
                if item["run"] is not None
                else "no ci-review review"
            )
            lines.append("        PR " + ", ".join(parts))
    shown = f"{header['ref']} ({header['commit_sha'][:7]})"
    if header["note"]:
        lines.append(f"Note: {header['note']}")
    if not header["has_folder"]:
        lines.append(f"Files: {header['folder']}/ is not at {shown}.")
        return lines
    lines.append(f"Files at {shown}")
    section = None
    for f in files:
        if f["pipeline"] != section:
            section = f["pipeline"]
            if section:
                lines.append(f"  {PIPELINE_DIR}/")
        last = f["last"]
        tail = (
            f"{last.sha[:7]}  {_date(last.date)}  {last.subject}"
            if last
            else "(no history)"
        )
        lines.append(f"    {f['name']:<24}{tail}")
    return lines
