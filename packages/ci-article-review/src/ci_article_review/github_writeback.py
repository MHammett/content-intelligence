"""Write a saved run's review back to the article's folder on GitHub (``--write-back``).

A run saves its files under ``pipeline_history/<key>/`` first, as always. This
module copies three of them (four with ``include_capture``) into the folder
``--github`` read the draft from, in one commit::

    run_N_<ts>_review.md     ->  <folder>/<slug>-review-rN.md
    run_N_<ts>_worklist.md   ->  <folder>/pipeline/<slug>-worklist-rN.md
    run_N_<ts>_report.json   ->  <folder>/pipeline/run_N_<ts>_report.json
    run_N_<ts>_results.json  ->  <folder>/pipeline/run_N_<ts>_results.json

The top level is what the drafting side reads. ``pipeline/`` is not, because the
worklist lists documents nobody has read yet. ``N`` is the run number the run
actually used, which is what ``docs/CLOSE-OUT.md`` means by ``r<N>`` on a review.

``ci-review --github SLUG --write-back`` (straight after the run) and
``ci-docs push-run REPORT_JSON`` (the retry) both come through
``push_saved_run``, so they cannot disagree about names, line endings or
refusals. The commit only adds paths: ``GitHubRepo.commit_files`` refuses to
replace a file whose bytes differ and nothing here asks it to, and a report under
``_replay/`` is never pushed.

Every failure is a ``GitHubDocumentsError`` (a ``ValueError``, which
``pipeline.main`` turns into one plain message and exit 1); the local files are
already saved by then and the message says how to retry.
"""

import json
import logging
import shlex
from pathlib import Path

from ci_core.github_repo import GitHubRepo, GitHubRepoError, resolve_token

from . import ensemble_capture
from . import history as hist
from .github_documents import (
    GitHubDocumentsError,
    _check_can_write,
    _explain,
    review_name,
    validate_slug,
)

log = logging.getLogger(__name__)


def worklist_name(slug, run_number):
    return f"{slug}-worklist-r{run_number}.md"


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

    def __init__(self, slug, run_number, folder, source, files):
        self.slug = slug
        self.run_number = run_number
        self.folder = folder
        self.source = source
        #: ``{path in the repository: LF bytes}``
        self.files = files

    @property
    def names(self):
        """The files relative to the article folder, as the commit message lists them."""
        return [p[len(self.folder) + 1 :] for p in self.files]


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
    stem = report_path.name[: -len("_report.json")]

    # (path in the repository, local file, whether the run is incomplete without it)
    wanted = [
        (
            f"{folder}/{review_name(slug, run_number)}",
            report_path.with_name(f"{stem}_review.md"),
            True,
        ),
        (
            f"{folder}/pipeline/{worklist_name(slug, run_number)}",
            report_path.with_name(f"{stem}_worklist.md"),
            False,
        ),
        (f"{folder}/pipeline/{report_path.name}", report_path, True),
    ]
    if include_capture:
        capture = ensemble_capture.capture_path_for(report_path)
        wanted.append((f"{folder}/pipeline/{capture.name}", capture, False))

    files = {}
    for repo_path, local, required in wanted:
        try:
            files[repo_path] = _lf(local.read_bytes())
        except OSError:
            if required:
                raise GitHubDocumentsError(
                    f"{local} is missing, so this run cannot be written back."
                ) from None
            log.warning("%s was not saved, so it is not written back.", local.name)
    return _Plan(slug, run_number, folder, source, files)


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


def commit_message(plan, report, report_path, drift=()):
    cost = (report.get("cost_summary") or {}).get("total_usd")
    preset = (report.get("ensemble") or {}).get("cost_preset") or "unknown"
    read = [
        f["path"][len(plan.folder) + 1 :]
        for f in plan.source.get("files") or []
        if isinstance(f, dict) and str(f.get("path")).startswith(plan.folder + "/")
    ]
    price = (
        f"${cost:.2f} reported" if isinstance(cost, (int, float)) else "cost unknown"
    )
    run = Path(report_path).name[: -len("_report.json")]
    lines = [
        f"review: {plan.slug} r{plan.run_number}",
        "",
        f"Reviewed: {', '.join(read)} at {str(plan.source.get('commit_sha'))[:7]}",
        f"Run: {run} (preset {preset}, {price})",
        f"Files: {', '.join(plan.names)}",
        *drift,
    ]
    return "\n".join(lines) + "\n"


def _failure(exc, repo_name):
    if exc.kind == "rate_limited":
        return _explain(exc, repo_name, has_token=True)
    if exc.kind == "exists":
        return (
            f"{exc}. Nothing was overwritten. Those names belong to another "
            "run's files: run the review again so it takes a new run number."
        )
    if exc.kind == "secret":
        return (
            f"{exc}. The local files hold a configured API key or the token, "
            "so nothing was sent. Find and remove it before any retry."
        )
    return str(exc)


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
    """Commit one saved run's review to its article folder; returns the CommitResult.

    ``settings`` is the normalised ``github_documents`` block (``include_capture``
    and ``allow_public_repo`` are read here); ``api_keys`` is the merged key
    table, none of whose values may appear in what is committed. ``repo`` stands
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
    branch = plan.source.get("ref")
    if not repo_name or not isinstance(branch, str) or not branch:
        raise GitHubDocumentsError(
            f"{report_path.name}: document_source names no repository or branch."
        )

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
    try:
        _check_can_write(
            client.info(), repo_name, settings.get("allow_public_repo") is True
        )
        if not client.has_branch(branch):
            raise GitHubDocumentsError(
                f"{repo_name} has no branch {branch!r}, which is where "
                f"{report_path.name} was read from."
            )
        result = client.commit_files(
            branch,
            plan.files,
            commit_message(
                plan, report, report_path, _drift_lines(client, plan, branch)
            ),
            refuse_if_contains=secret_values(api_keys),
        )
    except GitHubRepoError as exc:
        raise GitHubDocumentsError(_failure(exc, repo_name)) from exc
    finally:
        if owns_client:
            client.close()

    if not result.written:
        out(
            f"Nothing new to write: all {len(result.unchanged)} file(s) are "
            f"already in {repo_name} with the same bytes. {result.html_url}".rstrip()
        )
    else:
        out(f"Wrote {len(result.written)} file(s) to {repo_name}: {result.html_url}")
        for path in result.written:
            out(f"  {path}")
    return result


def write_back_after_run(report_path, *, settings, api_keys, config_dir="configs"):
    """``push_saved_run`` for a run that has just finished, with the retry spelled out.

    The review is already on disk, so a failure here costs nothing but the
    commit: the error says so and names the command that tries again.
    """
    try:
        return push_saved_run(report_path, settings=settings, api_keys=api_keys)
    except GitHubDocumentsError as exc:
        raise GitHubDocumentsError(
            f"{exc}\nThe review itself is saved: {report_path}\n"
            f"To write it back later:  {retry_command(report_path, config_dir)}"
        ) from exc
