"""Tag the rounds whose pull request was merged, and accept one from Code.

A round is accepted by merging its pull request with a merge commit, and the
annotated tag ``<slug>/rN`` marks the merge (design: ``docs/GITHUB-DOCUMENTS.md``,
"Tagging"). Nobody has to take that step on github.com: the chat that drafts the
article merges the pull request when the author says so, and the next command
that writes to the article tags it first. This module is that step
(``tag_merged_rounds``) and the two commands that are conveniences around it:

- ``ci-docs tag SLUG`` runs the step alone (``tag_round_command``);
- ``ci-docs accept SLUG`` shows the article's open round and, with ``--yes``,
  merges it and tags it (``accept_round``).

``tag_merged_rounds`` is idempotent and safe when two commands run it at once.
Creating the tag's ref is the compare-and-swap: GitHub refuses a ref that
exists, so only one writer wins, and the loser reads the tag back. A tag that
points at the round's merge commit is success; one that points elsewhere is
reported and left alone. No tag is ever moved or deleted.

It is called only from commands that already write to the article
(``tag_before_write``), and a failure there is a warning that never stops the
command's own work. A read, a replay, an ``--offline`` run or a ``ls`` never
reaches it.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ci_core.github_repo import GitHubRepo, GitHubRepoError, Signature

from .github_documents import (
    GitHubDocumentsError,
    _describe,
    _explain,
    _get_pull,
    _open_rounds,
    _round_of,
    _tags,
    round_tag,
    validate_slug,
)

log = logging.getLogger(__name__)

#: The first line of a review the pipeline posts: ``<!-- ci-review run=7
#: commit=3f2a91c -->``. Only a marker that opens the body counts, so a review
#: a person wrote that quotes one is not mistaken for the pipeline's.
_MARKER = re.compile(
    r"\A\s*<!--\s*ci-review\s+run=([1-9][0-9]*)\b([^>]*?)-->", re.IGNORECASE
)
_MARKER_COMMIT = re.compile(r"\bcommit=([0-9a-f]{4,40})", re.IGNORECASE)

CREATED = "created"
WOULD_CREATE = "would create"
PRESENT = "present"
CONFLICT = "conflict"
FAILED = "failed"


@dataclass(frozen=True)
class TagResult:
    """What ``tag_merged_rounds`` did, or found, for one round."""

    #: The round number; 0 for a failure that is not about one round.
    round: int
    tag: str
    #: ``created``, ``would create`` (dry run), ``present`` (another command
    #: tagged it first, at the same commit), ``conflict`` (the tag exists and
    #: points elsewhere) or ``failed``.
    status: str
    pull: int | None = None
    commit: str | None = None
    #: The tag's message for ``created`` and ``would create``; the reason otherwise.
    detail: str = ""
    merged_at: str | None = None

    @property
    def problem(self):
        return self.status in (CONFLICT, FAILED)


def review_marker(body):
    """``(run, commit prefix or None)`` from a review body's opening marker, else ``None``."""
    m = _MARKER.match(body or "")
    if not m:
        return None
    c = _MARKER_COMMIT.search(m.group(2))
    return int(m.group(1)), (c.group(1).lower() if c else None)


def review_runs(client, pull_number):
    """The pipeline runs a pull request carries a review of, ascending."""
    runs = set()
    for review in client.list_reviews(pull_number):
        found = review_marker(review.body)
        if found:
            runs.add(found[0])
    return sorted(runs)


def _last_review(client, pull_number):
    """``(ReviewInfo, run)`` of the newest review with a marker, or ``None``."""
    found = None
    for review in client.list_reviews(pull_number):
        marker = review_marker(review.body)
        if marker:
            found = (review, marker[0])
    return found


def _runs_text(runs):
    if not runs:
        return ""
    if len(runs) == 1:
        return f", run {runs[0]}"
    head = ", ".join(str(r) for r in runs[:-1])
    return f", runs {head} and {runs[-1]}"


def tag_message(slug, number, pull_number, runs, *, imported=False):
    """``example-article r3: PR #12, runs 6 and 7``; ``... r3 (imported): PR #12 ...`` for an import."""
    mark = " (imported)" if imported else ""
    return f"{slug} r{number}{mark}: PR #{pull_number}{_runs_text(runs)}"


def _merged_at(pull):
    text = pull.merged_at
    if text:
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _merged_rounds(client, slug, repo):
    """``{round: pull}``: the article's merged round pull requests, the earliest per round."""
    prefix = f"round/{slug}/"
    found = {}
    for pull in client.list_pulls("closed"):
        if not (pull.merged and pull.head_ref.startswith(prefix)):
            continue
        number = _round_of(pull.head_ref, prefix)
        if number is None:
            continue
        if repo and pull.head_repo.lower() != repo.lower():
            log.warning(
                "Pull request #%d came from %s, not from a branch of %s; it is "
                "not tagged as a round.",
                pull.number,
                pull.head_repo or "a deleted fork",
                repo,
            )
            continue
        best = found.get(number)
        if best is None or (pull.merged_at or "", pull.number) < (
            best.merged_at or "",
            best.number,
        ):
            found[number] = pull
    return found


def _read_back(client, tag, number, pull):
    """A ref that already exists: is it the tag this round would get?"""
    commit = pull.merge_commit_sha
    existing = client.get_tag(tag)
    if existing is None:
        return TagResult(
            number,
            tag,
            FAILED,
            pull.number,
            commit,
            f"{tag} was reported as existing but could not be read back; run "
            "ci-docs tag again.",
        )
    if existing.commit_sha == commit:
        return TagResult(
            number,
            tag,
            PRESENT,
            pull.number,
            commit,
            "tagged by another command a moment ago",
            existing.tagged_at,
        )
    where = (
        existing.commit_sha[:7]
        if existing.commit_sha
        else "something that is not a commit"
    )
    return TagResult(
        number,
        tag,
        CONFLICT,
        pull.number,
        commit,
        f"{tag} already exists and points at {where}, not at the merge commit "
        f"{commit[:7]} of pull request #{pull.number}. It was left alone: a tag "
        "is never moved.",
    )


def tag_merged_rounds(client, slug, *, dry_run=False, repo=None, imported=None):
    """Tag every merged, untagged round of ``slug``; returns a ``TagResult`` per round touched.

    For each merged pull request from ``round/<slug>/rN`` with no tag
    ``<slug>/rN``, makes the annotated tag on its merge commit: tagger the
    token's user, dated at the merge, message ``<slug> rN: PR #n, runs ...`` with
    the runs read from the review markers. A round that already has its tag is
    not in the result, so a second call returns ``[]`` and writes nothing.

    ``imported`` is for the history import: ``{round number: accepted_at}``
    (timezone-aware datetimes). Only those rounds are tagged, the tag is dated
    ``accepted_at`` instead of the merge's own time (an import's merge happens
    now, the round was accepted then), and the message says ``(imported)``.

    A ``GitHubRepoError`` never escapes: it becomes a ``failed`` result, and the
    other rounds go on (except after a rate limit or a rejected token, where
    they would only fail the same way). ``dry_run`` reads and writes nothing.
    ``repo`` (``owner/name``) lets a pull request from a fork be skipped.
    """
    validate_slug(slug)
    results = []
    try:
        merged = _merged_rounds(client, slug, repo)
        have = set(_tags(client, slug))
    except GitHubRepoError as exc:
        return [
            TagResult(
                0, f"{slug}/r*", FAILED, detail=f"could not list the rounds: {exc}"
            )
        ]
    user = None
    for number, pull in sorted(merged.items()):
        if number in have or (imported is not None and number not in imported):
            continue
        tag = round_tag(slug, number)
        commit = pull.merge_commit_sha
        if not commit:
            results.append(
                TagResult(
                    number,
                    tag,
                    FAILED,
                    pull.number,
                    None,
                    f"pull request #{pull.number} reports no merge commit.",
                )
            )
            continue
        try:
            message = tag_message(
                slug,
                number,
                pull.number,
                review_runs(client, pull.number),
                imported=imported is not None,
            )
            if dry_run:
                results.append(
                    TagResult(
                        number,
                        tag,
                        WOULD_CREATE,
                        pull.number,
                        commit,
                        message,
                        pull.merged_at,
                    )
                )
                continue
            if user is None:
                user = client.current_user()
            tagger = Signature(
                user.name or user.login,
                f"{user.id}+{user.login}@users.noreply.github.com",
                imported[number] if imported is not None else _merged_at(pull),
            )
            client.create_tag(tag, message, commit, tagger=tagger)
            results.append(
                TagResult(
                    number, tag, CREATED, pull.number, commit, message, pull.merged_at
                )
            )
        except GitHubRepoError as exc:
            try:
                if exc.kind == "exists":
                    results.append(_read_back(client, tag, number, pull))
                    continue
            except GitHubRepoError as again:
                exc = again
            results.append(
                TagResult(number, tag, FAILED, pull.number, commit, str(exc))
            )
            if exc.kind in ("rate_limited", "auth"):
                break
    return results


def announce(results, *, say, warn):
    """Report ``tag_merged_rounds`` results: ``say`` for news, ``warn`` for a problem."""
    for r in results:
        if r.status == CREATED:
            say(f"Tagged {r.tag} at {r.commit[:7]} ({r.detail.split(': ', 1)[-1]}).")
        elif r.status == WOULD_CREATE:
            say(f"Would tag {r.tag} at {r.commit[:7]}: {r.detail}")
        elif r.status == PRESENT:
            say(f"{r.tag} is already tagged at {r.commit[:7]} ({r.detail}).")
        else:
            warn(f"{r.tag} not tagged: {r.detail}")


def tag_before_write(client, slug, *, repo=None, say, warn):
    """The automatic step a writing command runs first; never raises.

    Anything that goes wrong, a ``GitHubRepoError`` or a bug, is a warning, so
    the command's own work goes on. Returns the results.
    """
    try:
        results = tag_merged_rounds(client, slug, repo=repo)
    except Exception as exc:  # best effort by contract: see the module docstring
        warn(f"automatic tagging of {slug} failed: {type(exc).__name__}: {exc}")
        return []
    announce(results, say=say, warn=warn)
    return results


def _saved_run_article(report_path):
    """``(repo, slug)`` a saved run's report was read from, or ``None``.

    ``None`` for anything ``push-run`` itself refuses (a report under
    ``_replay/``, one that was not read from GitHub, one that cannot be read),
    so a refused retry tags nothing; ``push-run`` gives the reason.
    """
    path = Path(report_path)
    if "_replay" in path.resolve().parts:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            source = json.load(f).get("document_source")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(source, dict) or source.get("kind") != "github":
        return None
    files = source.get("files") or []
    first = files[0].get("path") if files and isinstance(files[0], dict) else None
    repo = source.get("repo")
    if not isinstance(first, str) or "/" not in first or not isinstance(repo, str):
        return None
    slug = first.rsplit("/", 2)[-2]
    try:
        return repo, validate_slug(slug)
    except GitHubDocumentsError:
        return None


def tag_for_saved_run(
    report_path, *, settings, repo=None, token_lookup, session=None, out=print
):
    """``ci-docs push-run``'s automatic step: tag the article the report belongs to.

    Best effort, and silent when there is nothing it may do (no token, no push
    permission, a report ``push-run`` will refuse): the commit that follows
    reports those itself. Returns the results.
    """
    found = _saved_run_article(report_path)
    if found is None:
        return []
    name, slug = found
    name = repo or name
    warn = lambda m: out(f"warning: {m}")  # noqa: E731
    try:
        token = token_lookup()
        if not token:
            return []
        client = open_client(name, token, session)
        try:
            check_can_write(client.info(), name, settings, "ci-docs push-run")
            return tag_before_write(client, slug, repo=name, say=out, warn=warn)
        finally:
            client.close()
    except (GitHubDocumentsError, GitHubRepoError):
        return []
    except Exception as exc:  # best effort by contract
        warn(f"automatic tagging of {slug} failed: {type(exc).__name__}: {exc}")
        return []


# -- shared by the ci-docs commands ---------------------------------------------


def repo_name(settings, repo):
    name = settings["repo"] if repo is None else repo
    if not isinstance(name, str) or not name.strip():
        raise GitHubDocumentsError(
            "No document repository is set: github_documents.repo is empty. Name "
            "one as OWNER/NAME there, or pass --github-repo."
        )
    return name.strip()


def path_prefix(settings):
    return (settings["path_prefix"] or "").strip().strip("/")


def open_client(name, token, session):
    try:
        return GitHubRepo(name, token, session=session)
    except ValueError as exc:
        raise GitHubDocumentsError(str(exc)) from exc


def require_token(token_lookup):
    token = token_lookup()
    if not token:
        raise GitHubDocumentsError(
            "No GitHub token to write with. Set GITHUB_TOKEN or run `gh auth login`."
        )
    return token


def check_can_write(info, name, settings, what):
    """Refuse a public repository (unless allowed) and a token that cannot push."""
    if not info.private and settings.get("allow_public_repo") is not True:
        raise GitHubDocumentsError(
            f"{name} is a public repository: what is committed there is published, "
            f"so {what} will not write to it. Make it private, or set "
            "allow_public_repo: true under github_documents."
        )
    if not info.can_push:
        raise GitHubDocumentsError(
            f"This token cannot push to {name} (no write permission). Use a token "
            "with Contents, Pull requests and Issues read and write."
        )


def open_round_pull(client, slug, name, pr):
    """The one open round pull request of ``slug``, or the one ``pr`` names (which must be open)."""
    if pr is not None:
        pull = _get_pull(client, slug, name, pr)
    else:
        opened = _open_rounds(client, slug)
        if not opened:
            raise GitHubDocumentsError(
                f"{name} has no open round of {slug} (no open pull request from "
                f"a branch round/{slug}/...). A round starts with the drafting "
                "chat opening its branch and pull request."
            )
        if len(opened) > 1:
            raise GitHubDocumentsError(
                f"{name} has {len(opened)} open rounds of {slug}: "
                f"{_describe(opened)}. Pass --pr N to choose one."
            )
        pull = opened[0]
    if pull.state != "open":
        state = "merged" if pull.merged else pull.state
        raise GitHubDocumentsError(
            f"Pull request #{pull.number} is {state}, so nothing can be added to it."
        )
    if pull.head_repo.lower() != name.lower():
        raise GitHubDocumentsError(
            f"Pull request #{pull.number} comes from "
            f"{pull.head_repo or 'a repository that no longer exists'}, not from "
            f"a branch of {name}. A fork's branch is not written."
        )
    return pull


def round_number_of(slug, pull):
    return _round_of(pull.head_ref, f"round/{slug}/")


# -- ci-docs tag ------------------------------------------------------------------


def tag_command(
    slug,
    *,
    settings,
    repo=None,
    dry_run=False,
    token_lookup,
    session=None,
    out=print,
):
    """``ci-docs tag``: tag what is merged and untagged; returns the results."""
    validate_slug(slug)
    name = repo_name(settings, repo)
    token = token_lookup() if dry_run else require_token(token_lookup)
    client = open_client(name, token, session)
    try:
        info = client.info()
        if not dry_run:
            check_can_write(info, name, settings, "ci-docs tag")
        results = tag_merged_rounds(client, slug, dry_run=dry_run, repo=name)
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(_explain(exc, name, has_token=bool(token))) from exc
    finally:
        client.close()
    announce(results, say=out, warn=lambda m: out(f"warning: {m}"))
    if not results:
        out(f"Nothing to tag: every merged round of {slug} has its tag.")
    return results


# -- ci-docs accept ---------------------------------------------------------------


def _since_review(client, review, head):
    """One line on whether any commit has landed since ``review``'s commit."""
    if review is None:
        return "no ci-review review on this pull request yet"
    got, run = review
    short = got.commit_id[:7]
    if got.commit_id == head:
        return f"run {run}, of this head ({short}): no commit has landed since"
    cmp = client.compare(got.commit_id, head)
    if cmp.status == "ahead":
        n = cmp.ahead_by
        return (
            f"run {run}, of {short}: {n} commit{'' if n == 1 else 's'} landed "
            "since, so the review does not cover the head"
        )
    if cmp.status == "identical":
        return f"run {run}, of this head ({short}): no commit has landed since"
    return (
        f"run {run}, of {short}: the branch no longer contains that commit "
        f"({cmp.status}), so the review does not cover the head"
    )


def accept_round(
    slug,
    *,
    settings,
    repo=None,
    pr=None,
    yes=False,
    token_lookup,
    session=None,
    out=print,
):
    """``ci-docs accept``: show the open round; with ``yes``, merge it and tag it.

    Returns the merge commit's SHA, or ``None`` when nothing was merged. A merge
    is the author's acceptance, so without ``yes`` this only shows. The merge is
    pinned to the head it showed (``sha=``): a commit pushed after that is not
    merged unseen, GitHub answers 409 and nothing merges.
    """
    validate_slug(slug)
    name = repo_name(settings, repo)
    token = token_lookup() if not yes else require_token(token_lookup)
    client = open_client(name, token, session)
    say = out
    warn = lambda m: out(f"warning: {m}")  # noqa: E731
    try:
        info = client.info()
        if yes:
            check_can_write(info, name, settings, "ci-docs accept")
            tag_before_write(client, slug, repo=name, say=say, warn=warn)
        pull = open_round_pull(client, slug, name, pr)
        # The branch is the truth; the pull request's pointer can lag a push.
        head = client.resolve(pull.head_ref)
        number = round_number_of(slug, pull)
        label = f"r{number}" if number is not None else pull.head_ref
        review = _last_review(client, pull.number)
        out(f'{slug} {label}: pull request #{pull.number} "{pull.title}"')
        out(f"  branch   {pull.head_ref} into {pull.base_ref}")
        out(f"  head     {head[:7]}  {pull.html_url}")
        out(f"  review   {_since_review(client, review, head)}")
        if not yes:
            out(
                "Not merged. A merge is the author's acceptance: run this again "
                "with --yes only when the author has said to accept this round."
            )
            return None
        title = f"{slug} {label} (#{pull.number})"
        merge_sha = client.merge_pull(
            pull.number, sha=head, merge_method="merge", commit_title=title
        )
        out(f"Merged pull request #{pull.number}: {merge_sha[:7]} ({title}).")
        tag_before_write(client, slug, repo=name, say=say, warn=warn)
        return merge_sha
    except GitHubRepoError as exc:
        if exc.kind == "conflict":
            raise GitHubDocumentsError(
                f"{exc}. Nothing was merged. If the head moved after it was shown, "
                f"run `ci-docs accept {slug}` again to see it as it is now; "
                "otherwise the pull request cannot be merged as it stands "
                "(a conflict with the base branch, or a required check)."
            ) from exc
        raise GitHubDocumentsError(_explain(exc, name, has_token=bool(token))) from exc
    finally:
        client.close()
