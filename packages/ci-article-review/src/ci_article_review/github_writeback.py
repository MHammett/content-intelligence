"""Write a saved run's review back to the round's pull request (``--write-back``).

A run saves its files under ``pipeline_history/<key>/`` first, as always. This
module then delivers the review to the pull request the draft was read from, in
two steps (design: ``docs/GITHUB-DOCUMENTS.md``, "Delivering the review")::

    1. one commit to the pull request's head branch, under fixed names:

       run_N_<ts>_review.md     ->  <folder>/review.md
       (built here)             ->  <folder>/pipeline/run.json
       run_N_<ts>_report.json   ->  <folder>/pipeline/report.json
       run_N_<ts>_worklist.md   ->  <folder>/pipeline/worklist.md
       run_N_<ts>_results.json  ->  <folder>/pipeline/results.json  (include_capture)

    2. one pull request review (event COMMENT, no line comments) whose body is the
       run's summary and a checklist of the Section 1 findings (``review_body``).

The commit goes first so the review can link ``review.md`` at the commit that holds
it, and so a retry is simple: ``ci-docs push-run`` finds the commit's files
identical (no new commit) and posts only the review that is missing. Both steps are
safe to repeat.

``ci-review --github SLUG --write-back`` (straight after the run) and
``ci-docs push-run REPORT_JSON`` (the retry) both come through ``push_saved_run``,
so they cannot disagree about names, line endings or refusals. A file the
pipeline owns is replaced only if it is still what it was at the commit the run
reviewed (``replace`` is a compare-and-swap on the blob at that commit); a file
that was absent then must still be absent. Anything else is refused and nothing
is overwritten. A report under ``_replay/`` is never pushed, and nothing is
written to a pull request that has been merged or closed.

Every failure is a ``GitHubDocumentsError`` (a ``ValueError``, which
``pipeline.main`` turns into one plain message and exit 1); the local files are
already saved by then and the message says how to retry.
"""

import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

from ci_core.github_repo import (
    CommitResult,
    GitHubRepo,
    GitHubRepoError,
    ReviewInfo,
    resolve_token,
)

from . import ensemble_capture
from . import history as hist
from .github_documents import (
    RUN_FILE,
    GitHubDocumentsError,
    _check_can_write,
    _explain,
    validate_slug,
)
from .review_body import MAX_BODY, RunFacts, build_review_body, is_review_of

log = logging.getLogger(__name__)

#: The fixed names a run writes, relative to the article's folder.
REVIEW_FILE = "review.md"
REPORT_FILE = "pipeline/report.json"
WORKLIST_FILE = "pipeline/worklist.md"
RESULTS_FILE = "pipeline/results.json"


class PullRequestClosed(GitHubDocumentsError):
    """The round's pull request is merged or closed: there is nowhere to write.

    Not retryable (a ``push-run`` would be refused the same way), so the message
    does not offer one.
    """


def secret_values(api_keys):
    """Every non-empty string among the configured API keys, however nested."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, str) and node.strip():
            found.add(node.strip())

    walk(api_keys)
    return sorted(found)


def retry_command(report_path, config_dir="configs"):
    """The ``ci-docs push-run`` line that writes back ``report_path``."""

    def quoted(text):
        return f'"{text}"' if (" " in text or "\\" in text) else shlex.quote(text)

    parts = ["ci-docs", "push-run", quoted(str(report_path))]
    if config_dir and str(config_dir) != "configs":
        parts += ["--config-dir", quoted(str(config_dir))]
    return " ".join(parts)


def _lf(data):
    """Files saved on Windows are CRLF; the document repository is LF."""
    return data.replace(b"\r\n", b"\n")


def _load_report(path):
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
    except OSError as exc:
        raise GitHubDocumentsError(f"Cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise GitHubDocumentsError(f"{path} is not a JSON report: {exc}") from exc
    if not isinstance(report, dict):
        raise GitHubDocumentsError(f"{path} is not a pipeline report.")
    return report


class _Plan:
    """What a saved run would write: the article, its numbers and the files."""

    def __init__(self, slug, run_number, run_name, folder, source, files):
        self.slug = slug
        self.run_number = run_number
        #: ``run_N_<ts>``: how the run's files are named in ``pipeline_history``.
        self.run_name = run_name
        self.folder = folder
        self.source = source
        #: ``{path in the repository: LF bytes}``
        self.files = files

    @property
    def names(self):
        """The files relative to the article folder, as the commit message lists them."""
        return [p[len(self.folder) + 1 :] for p in self.files]

    @property
    def commit(self):
        """``S``: the commit the run read, and reviewed."""
        return str(self.source.get("commit_sha") or "")

    @property
    def pull_number(self):
        return self.source["pull_request"]

    @property
    def round_number(self):
        number = self.source.get("round")
        return number if isinstance(number, int) and number > 0 else None

    @property
    def files_read(self):
        """The draft files that were read, relative to the article folder."""
        return [
            f["path"][len(self.folder) + 1 :]
            for f in self.source.get("files") or []
            if isinstance(f, dict) and str(f.get("path")).startswith(self.folder + "/")
        ]


def _run_json(report, report_path, run_number, source, run_name):
    """``pipeline/run.json``: what a later run needs to number itself, and no review id.

    The review is posted after this file is committed, so it cannot be named
    here; the review's marker links the two instead.
    """
    cost = (report.get("cost_summary") or {}).get("total_usd")
    record = {
        "run_number": run_number,
        "history_key": report_path.parent.name,
        "history_name": run_name,
        "reviewed_commit": source.get("commit_sha"),
        "round": source.get("round"),
        "pull_request": source.get("pull_request"),
        "preset": (report.get("ensemble") or {}).get("cost_preset"),
        "cost_reported_usd": cost if isinstance(cost, (int, float)) else None,
    }
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def plan_files(report_path, report, *, include_capture):
    """The files a saved run writes back, read from beside its report.

    Everything comes from the report and the files next to it. The folder is
    where the draft was read from (the directory of the first file in
    ``document_source``), so ``path_prefix`` need not be known here.
    """
    report_path = Path(report_path)
    if "_replay" in report_path.resolve().parts:
        raise GitHubDocumentsError(
            f"{report_path} is under _replay/. A replay is a code test, not a "
            "review of the article, so it is never written back."
        )
    source = report.get("document_source")
    if not isinstance(source, dict) or source.get("kind") != "github":
        raise GitHubDocumentsError(
            f"{report_path.name} has no GitHub document_source: it was not "
            "reviewed from --github, so there is no folder to write it to."
        )
    pull = source.get("pull_request")
    if isinstance(pull, bool) or not isinstance(pull, int) or pull < 1:
        raise GitHubDocumentsError(
            f"{report_path.name}: document_source names no pull request. It was "
            "read from a tag or a ref (--round of an accepted round, or "
            "--github-ref), and a review is written only to a round's open pull "
            "request. Review the round's pull request to get one."
        )
    read = source.get("files") or []
    first = read[0].get("path") if read and isinstance(read[0], dict) else None
    if not isinstance(first, str) or "/" not in first:
        raise GitHubDocumentsError(
            f"{report_path.name}: document_source names no file to take the "
            "article folder from."
        )
    folder = first.rsplit("/", 1)[0]
    slug = validate_slug(folder.rsplit("/", 1)[-1])

    match = hist._REPORT_NAME_RE.match(report_path.name)
    if not match or not match.group("ts"):
        raise GitHubDocumentsError(
            f"{report_path.name} is not named run_N_<timestamp>_report.json."
        )
    run_number = int(match.group("run"))
    run_name = report_path.name[: -len("_report.json")]

    # (path in the repository, local file, whether the run is incomplete without it)
    wanted = [
        (
            f"{folder}/{REVIEW_FILE}",
            report_path.with_name(f"{run_name}_review.md"),
            True,
        ),
        (f"{folder}/{RUN_FILE}", None, True),
        (f"{folder}/{REPORT_FILE}", report_path, True),
        (
            f"{folder}/{WORKLIST_FILE}",
            report_path.with_name(f"{run_name}_worklist.md"),
            False,
        ),
    ]
    if include_capture:
        capture = ensemble_capture.capture_path_for(report_path)
        wanted.append((f"{folder}/{RESULTS_FILE}", capture, False))

    files = {}
    for repo_path, local, required in wanted:
        if local is None:
            files[repo_path] = _run_json(
                report, report_path, run_number, source, run_name
            )
            continue
        try:
            files[repo_path] = _lf(local.read_bytes())
        except OSError:
            if required:
                raise GitHubDocumentsError(
                    f"{local} is missing, so this run cannot be written back."
                ) from None
            log.warning("%s was not saved, so it is not written back.", local.name)
    return _Plan(slug, run_number, run_name, folder, source, files)


def _drift_lines(client, plan, branch):
    """Lines saying a draft that was reviewed has changed at its own path since."""
    try:
        head = {e.name: e.sha for e in client.list_dir(plan.folder, branch)}
    except GitHubRepoError:
        return []
    lines = []
    for f in plan.source.get("files") or []:
        name = str(f.get("path") or "").rsplit("/", 1)[-1]
        if name in head and head[name] != f.get("blob_sha"):
            lines.append(
                f"Note: {name} changed at its own path since it was read (blob "
                f"{str(f.get('blob_sha'))[:7]} then, {head[name][:7]} now)."
            )
    return lines


def _blobs_at(client, plan, commit):
    """``{path: blob sha}`` of this run's files that exist at ``commit``."""
    held = {}
    for directory in {p.rsplit("/", 1)[0] for p in plan.files}:
        for entry in client.list_dir(directory, commit):
            if entry.type == "file":
                held[entry.path] = entry.sha
    return {path: held[path] for path in plan.files if path in held}


def _facts(plan, report, drift, review_commit, repo_name):
    cost = (report.get("cost_summary") or {}).get("total_usd")
    return RunFacts(
        slug=plan.slug,
        run_number=plan.run_number,
        run_name=plan.run_name,
        reviewed_commit=plan.commit,
        pull_number=plan.pull_number,
        repo=repo_name,
        review_path=f"{plan.folder}/{REVIEW_FILE}",
        review_commit=review_commit,
        round_number=plan.round_number,
        preset=(report.get("ensemble") or {}).get("cost_preset") or "unknown",
        cost_usd=cost if isinstance(cost, (int, float)) else None,
        files_read=plan.files_read,
        drift=list(drift),
    )


def commit_message(plan, report, drift=()):
    cost = (report.get("cost_summary") or {}).get("total_usd")
    preset = (report.get("ensemble") or {}).get("cost_preset") or "unknown"
    price = (
        f"${cost:.2f} reported" if isinstance(cost, (int, float)) else "cost unknown"
    )
    rnd = f" r{plan.round_number}" if plan.round_number is not None else ""
    lines = [
        f"review: {plan.slug}{rnd} run {plan.run_number}",
        "",
        f"Reviewed: {', '.join(plan.files_read)} at {plan.commit[:7]} "
        f"(PR #{plan.pull_number})",
        f"Run: {plan.run_name} (preset {preset}, {price})",
        f"Files: {', '.join(plan.names)}",
        *drift,
    ]
    return "\n".join(lines) + "\n"


def _failure(exc, repo_name, plan=None):
    if exc.kind == "rate_limited":
        return _explain(exc, repo_name, has_token=True)
    if exc.kind == "exists":
        since = f" since {plan.commit[:7]}, the commit it reviewed" if plan else ""
        return (
            f"{exc}. Nothing was overwritten. review.md and pipeline/ are written "
            f"only by the pipeline, and one of them has changed{since}: another "
            "run or an edit got there first. Review the round again so it reads "
            "the branch as it is now."
        )
    if exc.kind == "secret":
        return (
            f"{exc}. The local files hold a configured API key or the token, "
            "so nothing was sent. Find and remove it before any retry."
        )
    return str(exc)


def _refuse_secrets(body, secrets):
    if any(secret in body for secret in secrets):
        raise GitHubDocumentsError(
            "the review body contains a configured API key; nothing was sent. "
            "The local files hold it. Find and remove it before any retry."
        )


@dataclass
class PushResult:
    """What ``push_saved_run`` did: the commit, and the review (new or already there)."""

    commit: CommitResult
    review: ReviewInfo | None
    #: ``True`` if this call posted the review, ``False`` if one was already there.
    review_posted: bool

    @property
    def written(self):
        return self.commit.written

    @property
    def unchanged(self):
        return self.commit.unchanged


def _open_pull(client, plan, repo_name, report_path):
    """The round's pull request as it is now; refuses one that cannot take a commit."""
    number = plan.pull_number
    try:
        pull = client.get_pull(number)
    except GitHubRepoError as exc:
        if exc.kind == "not_found":
            raise GitHubDocumentsError(
                f"{repo_name} has no pull request #{number}, or the repository is "
                "not visible to this token."
            ) from exc
        raise
    if pull.state != "open":
        state = "merged" if pull.merged else pull.state
        raise PullRequestClosed(
            f"Pull request #{number} is {state}, so nothing was written: a commit "
            "to its branch would never reach the base branch. The review stays in "
            "local history."
        )
    if pull.head_repo.lower() != repo_name.lower():
        raise GitHubDocumentsError(
            f"Pull request #{number} comes from {pull.head_repo or 'a deleted fork'}, "
            f"not from a branch of {repo_name}. A fork's branch is never written."
        )
    ref = plan.source.get("ref")
    if isinstance(ref, str) and ref and ref != pull.head_ref:
        raise GitHubDocumentsError(
            f"Pull request #{number} is from {pull.head_ref!r}, but "
            f"{report_path.name} was read from {ref!r}. Nothing was written."
        )
    return pull


def push_saved_run(
    report_path,
    *,
    settings,
    api_keys,
    repo=None,
    client=None,
    token_lookup=None,
    session=None,
    out=print,
):
    """Deliver one saved run's review to its round's pull request; returns a ``PushResult``.

    Step 1 commits the run's files to the pull request's head branch; step 2 posts
    the review with the findings checklist. Each is skipped when it is already
    done: the commit when every file is identical, the review when one on the pull
    request starts with this run's marker.

    ``settings`` is the normalised ``github_documents`` block (``include_capture``
    and ``allow_public_repo`` are read here); ``api_keys`` is the merged key
    table, none of whose values may appear in what is sent. ``repo`` stands
    in for the repository the report names. Raises ``GitHubDocumentsError`` with
    a plain message, before anything is sent for every refusal that can be
    decided locally.
    """
    report_path = Path(report_path)
    report = _load_report(report_path)
    plan = plan_files(
        report_path, report, include_capture=bool(settings.get("include_capture"))
    )
    repo_name = (repo or plan.source.get("repo") or "").strip()
    if not repo_name or not plan.commit:
        raise GitHubDocumentsError(
            f"{report_path.name}: document_source names no repository or commit."
        )
    secrets = secret_values(api_keys)

    owns_client = client is None
    if owns_client:
        token = (token_lookup or resolve_token)()
        if not token:
            raise GitHubDocumentsError(
                "No GitHub token to commit with. Set GITHUB_TOKEN or run "
                "`gh auth login`."
            )
        try:
            client = GitHubRepo(repo_name, token, session=session)
        except ValueError as exc:
            raise GitHubDocumentsError(str(exc)) from exc
    committed = None
    try:
        _check_can_write(
            client.info(), repo_name, settings.get("allow_public_repo") is True
        )
        pull = _open_pull(client, plan, repo_name, report_path)
        branch = pull.head_ref
        if not client.has_branch(branch):
            raise GitHubDocumentsError(
                f"{repo_name} has no branch {branch!r}, which is where "
                f"{report_path.name} was read from."
            )
        drift = _drift_lines(client, plan, branch)
        # Refuse a body that holds a key before anything at all is sent, not
        # after the commit has landed. The commit's SHA, which the final body
        # links, is the only thing that differs.
        _refuse_secrets(
            build_review_body(
                report, _facts(plan, report, drift, plan.commit, repo_name)
            ),
            secrets,
        )
        # A file the pipeline owns may be replaced only while it is still what it
        # was at the commit this run reviewed; one absent then must still be
        # absent (commit_files refuses a new file it did not expect).
        result = client.commit_files(
            branch,
            plan.files,
            commit_message(plan, report, drift),
            replace=_blobs_at(client, plan, plan.commit),
            refuse_if_contains=secrets,
        )
        committed = result
        _say_commit(out, repo_name, result)

        facts = _facts(plan, report, drift, result.commit_sha, repo_name)
        body = build_review_body(report, facts, limit=MAX_BODY)
        _refuse_secrets(body, secrets)
        existing = next(
            (
                r
                for r in client.list_reviews(plan.pull_number)
                if is_review_of(r.body, plan.run_number, plan.commit)
            ),
            None,
        )
        if existing is not None:
            out(
                f"The review of run {plan.run_number} is already on pull request "
                f"#{plan.pull_number}, so none was posted: {existing.html_url}"
            )
            return PushResult(result, existing, False)
        review = client.create_review(
            plan.pull_number, commit_id=plan.commit, body=body, event="COMMENT"
        )
        out(f"Posted the review on pull request #{plan.pull_number}: {review.html_url}")
        return PushResult(result, review, True)
    except GitHubRepoError as exc:
        message = _failure(exc, repo_name, plan)
        if committed is not None:
            message = (
                f"The files are committed ({committed.html_url or committed.commit_sha}) "
                f"but the review was not posted: {message}"
            )
        raise GitHubDocumentsError(message) from exc
    except GitHubDocumentsError as exc:
        if committed is not None and not isinstance(exc, PullRequestClosed):
            raise GitHubDocumentsError(
                f"The files are committed ({committed.html_url or committed.commit_sha}) "
                f"but the review was not posted: {exc}"
            ) from exc
        raise
    finally:
        if owns_client:
            client.close()


def _say_commit(out, repo_name, result):
    if not result.written:
        out(
            f"Nothing new to write: all {len(result.unchanged)} file(s) are "
            f"already in {repo_name} with the same bytes. {result.html_url}".rstrip()
        )
    else:
        out(f"Wrote {len(result.written)} file(s) to {repo_name}: {result.html_url}")
        for path in result.written:
            out(f"  {path}")


def write_back_after_run(report_path, *, settings, api_keys, config_dir="configs"):
    """``push_saved_run`` for a run that has just finished, with the retry spelled out.

    The review is already on disk, so a failure here costs nothing but the
    commit and the review post: the error says so and names the command that
    tries again (unless the pull request is closed, where a retry is refused).
    """
    try:
        return push_saved_run(report_path, settings=settings, api_keys=api_keys)
    except PullRequestClosed as exc:
        raise PullRequestClosed(
            f"{exc}\nThe review itself is saved: {report_path}"
        ) from exc
    except GitHubDocumentsError as exc:
        raise GitHubDocumentsError(
            f"{exc}\nThe review itself is saved: {report_path}\n"
            f"To write it back later:  {retry_command(report_path, config_dir)}"
        ) from exc
