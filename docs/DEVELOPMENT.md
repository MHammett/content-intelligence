# Development notes

A companion to [CLAUDE.md](../CLAUDE.md), which holds the rules for sharing a checkout (worktrees, `git stash`, `uv sync`, mergeability) and the ladder for verifying a change without spending real money. This page holds the rest of what contributors, human or automated, keep having to relearn: starting a task, the worktree traps, the test-suite guard rails, dependency changes, and the hygiene a public repository needs. Landing a PR is in `docs/LANDING-A-PR.md` and replaying saved runs is in `docs/REPLAY-AND-HISTORY.md`.

## Starting a task

**Check that nobody else is already doing it.** Several people or agents work on this repository at once, and an obvious, well-specified bug attracts more than one of them: two fixes for the same `make lint` failure were started from the same commit 22 seconds apart. A scan is only valid as of the second it ran, and each of these looks somewhere different:

- The base moved: `git fetch`, then `git log --oneline <base>..origin/<branch>` if you build on someone's branch.
- Sibling branches and open work: `git log --oneline --all --since=1.day -- <path>`, `git ls-remote --heads origin`, `gh pr list`, `gh issue list`.
- **Uncommitted work in other worktrees, which no scan of refs can see:**

  ```bash
  git worktree list --porcelain | sed -n 's/^worktree //p' | while read -r w; do
    echo "== $w"; git --no-optional-locks -C "$w" status --porcelain | head -5
  done
  ```

  Use `--no-optional-locks`: a plain `git status` can take another session's index lock and make its next git command fail. Read a peer's worktree; never edit it.
- Other running sessions, if your tool can list them; their titles often name the same bug.

Look again before you push, and again before you write a duplicate, because PRs here can merge within minutes of opening. If someone got there first, read their version before defending yours and concede what it covers. If two of you started the same task, agree a rule before writing any code: the earliest start owns the implementation and the PR, and the other does read-only cross-checks and reviews it. Do not merge a PR you did not make. Two PRs (or your PR and someone's uncommitted edits) that insert at the same anchor, such as between the same two test classes, are a certain add/add conflict for whichever merges second, so give each a different anchor.

**Two sessions that need to edit the same file: one owns the PR, the other hands over text.** The session already mid-flight owns the single PR. The idle one sends plain text, never a patch (a patch needs the owner's branch, which neither side may touch): the replacement wording verbatim, each figure with its basis (what was measured, on what, when, and the file and anchor it belongs at) and a list of what the wording must not contradict. Re-check any pointer that is relative to a block ("the note under `wide:`") against current `master` before it ships, because those move. Approval relayed by another agent is not approval: each session waits for the maintainer in its own conversation. If a hand-off seems ignored, suspect the send (a message tool can be blocked by an approval layer) and give the maintainer the text.

**Do not trust a brief's claims about what is merged.** They are wrong in both directions: code a brief calls unmerged may already be on `master`, and code it assumes present may exist only on a branch, or only as uncommitted edits in someone's worktree. `git merge-base --is-ancestor <sha> HEAD` answers it for a SHA. For an identifier, grep and run `git log --all --oneline -S'<identifier>'`, which searches commits only, so scan the worktrees as well (above). If the fix is already there, check that it is *wired* end to end (a parameter that exists but is never passed is the usual trap) and look one layer downstream for the part that was missed. If the code is missing, say which base you are building on: a PR cut from someone's branch carries every commit of that branch, so it usually should not target `master`.

**Measure the baseline.** A test count quoted in a brief goes stale within days (1092 quoted, 1362 real, six days on).

- Run each package alone (`uv run pytest packages/<pkg>/tests/`) and the whole repo, because the two can disagree: on a cold worktree, before #197 fixed a Windows tiktoken download, the repo-wide run was green while ci-core alone failed 106 tests, since collection-time imports bypass pytest-socket.
- Keep the full output of every run (`... -q -rfE > out.txt 2>&1`). The run that flakes is the one you need, and a summary line cannot name the test.
- A `grep` count is not a test count: "16 `main()` calls" included docstrings and strings.
- Measure against the base you started from. Capture `git rev-parse HEAD` at the start and read baseline files from it or from `git merge-base HEAD origin/master`, never from a bare `origin/master`, which moves under you. A negative delta from a purely additive change means the comparison is wrong, not the change (`git diff --numstat` showing insertions only was the tell).

## Worktree traps

- **A fresh worktree is not ready.** Besides `uv sync` (see CLAUDE.md), it has no `configs/` (git-ignored: pass `--config-dir` pointing at your main checkout's, or at a scratch copy), and its `.env` may be a placeholder. A stub `.env` full of `your_key_here` shadows the real credentials, and every model call then fails in under five seconds at $0.00, which reads like a provider outage. The stubs were planted by tests that ran `ci-setup` against the real checkout (fixed in #208; a branch that predates it still plants one). Where a worktree finds a `.env` depends on where it sits: `find_dotenv()` walks up from the package file, so a worktree inside the main checkout's directory reaches the main checkout's real `.env`, while one elsewhere finds none and stops at `OPENAI_API_KEY is not set`. Check which file will load with `uv run python -c "from ci_article_review import config_loader as c; print(c._DOTENV_PATH)"`. To run live from a worktree with a stub, move the stub aside for that one command.
- **Do not `source` a `.env` that has CRLF line endings.** Every value gains a trailing `\r` and every call fails at $0.00 again. Let the pipeline load the file; python-dotenv handles CRLF.
- **A worktree can be removed or deregistered under a running session.** Git then resolves against the main checkout: `git status` reports the main checkout on `master`, and finished work looks lost. When git output contradicts what you just did, run `git rev-parse --git-dir` and `git worktree list` before believing it. The commits survive in the shared object database (`git branch -a --contains <sha>`). Do not run git write commands from the orphaned directory.
- **Removing a worktree deletes its git-ignored files** (`pipeline_history/`, `configs/`, local drafts); see `docs/REPLAY-AND-HISTORY.md`.

## Tests

The suite runs under pytest-socket (`--disable-socket --allow-hosts=127.0.0.1,::1`) plus `pytest_plugins/socket_guard.py` (`-p socket_guard` in each package's inifile), which extends the guard to collection, fixture teardown and forward name lookups. Reverse lookups (`gethostbyaddr`, `getnameinfo`, `getfqdn`) and child processes are not covered, so do not read a green run as proof of "no network" for those. A `SocketBlockedError: A test tried to use socket.getaddrinfo() with host "x"` means a test reached a real lookup: stub the host guard (`resolver.classify_host`, `links._is_public_host`) or `getaddrinfo`; do not mark the test `enable_socket`.

- **Tests must not touch the real `pipeline_history/`.** Two autouse fixtures in `packages/ci-article-review/tests/conftest.py`, `cwd_history_reads` and `cwd_history_untouched`, fail a test at teardown when the pipeline created or read history under the working directory, and the message names the fix: the `tmp_history_root` fixture, or a root under `tmp_path`. `tmp_history_root` moves only `pipeline.HISTORY_ROOT`, not `history_analytics.HISTORY_ROOT`. A third autouse fixture, `history_root_env_unset`, clears `CI_HISTORY_ROOT` for every test, because that variable (see `docs/CONFIGURATION.md`, "History location") beats the constant and would otherwise send a developer's suite into their shared store; a test about the override sets it itself, under `tmp_path`. Never point a test at the real history: `iter_reports` parses every report in it.
- **To find which tests touch a path,** use throwaway `-p` plugins, not edits to the repo. A `pytest_runtest_protocol` hookwrapper notes whether the path existed before each test and removes it afterwards (without the reset, only the first offender shows). A `sys.addaudithook` on `open`, `os.listdir`, `os.scandir`, `os.mkdir`, `os.rename`, `os.remove` and `os.rmdir` attributes the rest to the running test. `os.stat` raises no audit event, so `Path.is_dir()` and `exists()` lookups are invisible to it: wrap the reader functions too, including by-value imports. `git status --ignored` never shows an empty directory, so git cannot reveal an empty-directory leak: snapshot the files and empty directories of a fresh worktree before and after the suite.
- **Run every detector on the unfixed base first.** A zero means nothing until the detector has caught the known case: the audit hook on the unfixed tree recorded exactly the eight known `mkdir`s, which is what made its zero on the fixed branch credible. Mutation-prove new guards. For a teardown assertion, run a 2x2 of fix applied or removed against assertion on or off, where only "fix removed, assertion on" may fail; a self-test that clears the record never notices the assertion being weakened. Mutate a CRLF file as bytes and restore it byte for byte.
- **Assert the behaviour, not an incidental fact about a fixture.** A test that pinned the exact sections named for a missing `primary_claim` broke when an unrelated PR retuned the preset the stub fixture ran. Derive the expectation from the run's own output (the domains in `report["api_call_log"]`) and assert the relationship. When widened coverage makes a "does not include X" test vacuously true, add a case that forces the narrow condition.
- **Test a guard in its own scenario.** A warning added at the end of `_submit_missing_archives` for a tripped rate-limit breaker was unreachable in exactly that case: the breaker makes lookups return `archived: None`, the pass selects on `archived is False`, the target list is empty, and the function returns a hundred lines before the warning. Ten tests passed because they called the function with a target already in hand. After adding a guard, trace the state its triggering scenario produces and confirm it reaches the new line, write the test from the scenario and not from the function signature, and negative-control it against the old placement.
- **After rebasing a fix for a *class* of bug, grep the merged file for the pattern**, not only for conflict markers. 45 commits landed under one such fix, three of them added new instances of the bug (`(x or {}).get(...)` guards `None` but not a string), and the auto-merge was clean. Re-run the whole suite.

## Verifying a publish without the live site

`--replay` verifies the review pipeline for free (`docs/REPLAY-AND-HISTORY.md`), and
cannot touch `--publish` at all: that path POSTs to a real site, every upload is
public the moment it lands, and a test post has to be cleaned up by hand. So the one
part of the pipeline with irreversible side effects was the one part nobody
rehearsed. `tools/` holds what closed that gap.

**`tools/publish_rehearsal.py` runs the real CLI with every socket blocked.**
Argument parsing, config loading, the handoff parser, `run_publish_pipeline`,
`wp.push`, the block conversion — all of it, against the suite's own `FakeWordPress`
(imported from `packages/ci-article-review/tests/`, deliberately: a second fake of
the same API would be a second thing to keep true). `socket.connect` and
`socket.create_connection` are replaced with something that raises, as a tripwire
rather than a courtesy — a new code path that reaches the network fails loudly
instead of quietly publishing. `--wp-user`/`--wp-password` are forced to dummy
values, which beat the config file in this project's credential precedence, so the
real ones are never read. Flags after a bare `--` are forwarded, which is how to
rehearse `--publish-live` or `--keep-image-metadata`.

```bash
uv run python tools/publish_rehearsal.py handoff.md --publication mikehammett \
    --config-dir ../../configs -- --keep-image-metadata
```

Use it before committing any change to handoff parsing, `run_publish_pipeline` or
`adapters/cms/`. It found the old parser sending "Schema type: AboutPage" to Rank
Math as the Facebook and Twitter descriptions (PR #194); unit tests pinned that
afterwards, but this is what found it.

**`tools/image_metadata.py` reads an image with exifread, not Pillow.** The
uploader's strip is built on Pillow, so checking it with Pillow would be Pillow
agreeing with itself. `make` writes a photo carrying an 11-tag GPS block, make,
model, software, an artist, a body serial number, a capture time, EXIF orientation
6 and a real sRGB ICC profile; `report` prints what a file holds, including the
JPEG APPn/COM segments walked straight off the bytes, and diffs two files when
given two. Orientation 6 matters: dropping the tag without turning the pixels is
how a stripped phone photo ends up on its side.

```bash
uv run python tools/image_metadata.py make /tmp/photo.jpg
uv run python tools/publish_rehearsal.py ... --out-dir /tmp
uv run python tools/image_metadata.py report /tmp/photo.jpg /tmp/rehearsal-upload-photo.jpg
```

**`tools/media_exif_audit.py` reports what is already public.** GETs only; it
issues no DELETE, because a media DELETE without `force` is refused here (`501
rest_trash_not_supported`, no `MEDIA_TRASH`) and `force=true` is permanent, so
removal is a person's decision under Media > Library. Two traps it exists to
avoid: `media_details.image_meta` holds **no GPS fields at all**
(`wp_read_image_metadata` keeps aperture, camera and `created_timestamp`), so each
file's own bytes have to be fetched; and WordPress serves a `-scaled` copy of
anything over `big_image_size_threshold` (2560px) regenerated *without* EXIF while
the untouched original stays public under `media_details.original_image` — so an
audit that reads only `source_url` reports a clean library that is not clean.

**Two things a rehearsal handoff needs.** A real `Article:` line: since PR #215
`--publish` exits 1 when the title is missing, blank or a bracketed placeholder,
before the SEO call and before the checklist, so a handoff without one never
reaches the part being rehearsed. An unfilled `Post type:` is the opposite — it
fails inside `wp.push`, after both — so a placeholder there exercises more of the
path, not less.

**To see whether the publish-time SEO backstop fires, without paying for a model
call,** drop `--no-seo-suggestions` from the forwarded flags and patch
`pipeline.seo_suggest.generate` to record its arguments and return `(None, None)`.
That is how the bracketed-placeholder handling in PR #210 was checked.

A rehearsal is not a live run. It proves this project's side of the conversation —
what would have been sent, and in what order — and nothing about what WordPress
does with it, and that includes whether the block editor accepts the blocks (see
"Checking block markup in the real editor", below). Do one live run after the
rehearsal is clean, upload as few files as
the cases need, name them so they can be found, and expect the attachments to
survive it.

## Checking block markup in the real editor

A block that the REST API stored can still open in the editor as "Block contains
unexpected or invalid content". The editor accepts a block only if its stored
markup equals what that block's `save()` writes for the attributes in its
delimiter comment. REST stores whatever it is sent and answers `200`, and the
front end renders the markup fine, so a green publish, a rehearsal and a unit test
written from the converter's own output all miss it. Issue #325 was a quote, a
table, a code block and a separator, found by opening a pushed draft. Only an
editor can say, so ask one.

**What is checked in.** `packages/ci-article-review/tests/golden/wordpress_editor/`
holds what a real editor wrote for each construct that `adapters/cms/blocks.py`
emits (`blocks.json`), the script that captured it (`capture.js`), a document with
one of every construct (`every_construct.md`) and a README saying how that capture
was made. `test_wordpress_editor_blocks.py` holds the converter to those strings.
They were written by an editor and not by hand, and the file records the editor's
own verdict on each. Do not edit `blocks.json`; capture it again.

**When to ask an editor.** Before committing a change that adds a block or changes
what one writes. When the site's WordPress updates, because a block's `save()` can
change: capture again, and the tests then say what moved. And when a published
draft opens with that message.

**Get an editor of the site's build.** None of this saves anything. Either:

- a **disposable WordPress Playground**, which needs no login and touches no site.
  Open
  `https://playground.wordpress.net/#%7B%22landingPage%22:%22/wp-admin/post-new.php%22,%22preferredVersions%22:%7B%22php%22:%228.3%22,%22wp%22:%22latest%22%7D,%22login%22:true%7D`
  (a blueprint that logs in and lands on a new post; set `wp` to the site's
  release if `latest` is not it). It takes 20 to 30 seconds to boot. The editor
  runs in a frame of another origin, which a console on that page cannot reach, so
  read the frame's address from the outer page,
  `document.querySelector('iframe').contentWindow.document.querySelector('iframe#wp').src`
  (it is `https://playground.wordpress.net/scope:<name>/...`), and open
  `https://playground.wordpress.net/scope:<name>/wp-admin/post-new.php` in a second
  tab. Keep the first tab open, because it hosts the site. The second tab's console
  is the editor's.
- or **a post editor on the site itself** (Posts > Add New), logged in. Run the
  same script in its console and save nothing: lock saving first with
  `wp.data.dispatch('core/editor').lockPostSaving('x')`, and close the tab instead
  of navigating away, because the editor marks the post changed and the browser
  asks before leaving.

**Is it the site's editor?** The validator, the serialiser and every block's
`save()` are in two public script files, `/wp-includes/js/dist/blocks.min.js` and
`/wp-includes/js/dist/block-library.min.js`. A site can hide its WordPress version
but not those. Hash them from the site and from the editor you are using; the same
digests mean the same editor. A release's own copies are at
`https://raw.githubusercontent.com/WordPress/WordPress/<version>/wp-includes/js/dist/<file>.min.js`.
`capture.js` records the first 12 hex digits of each in `blocks.json`. A
Playground adds a shim to a third file, `block-editor.min.js`, so that one will not
match a release; the other two will.

```bash
for f in blocks block-library; do curl -sL "https://example.com/wp-includes/js/dist/$f.min.js" | sha256sum | cut -c1-12; done
```

**Capture again.** Paste `capture.js` into the editor's console, then run
`await wpEditorBlocks.capture()`. It returns `{ sha256, json }`. Save `json` as
`blocks.json` and compare `sha256sum blocks.json` with `sha256`, because a long
string is easy to lose a character from on its way out of a console. (A console's
`copy(...)` puts text on the clipboard. A tool that returns a page script's result
may cut a long one, so ask it for the hash and the length first.) Every case must
come back `valid` and `round_trip`: that is the editor accepting what it wrote.
Then run the tests. They fail where the converter and the editor now disagree,
which is what they are for.

**Check what `to_blocks` writes.** For a change to the converter, or a document the
captures do not cover, run its markup through the editor's validator. This prints
the markup for the sample as a JSON string:

```bash
uv run python -c "import json, pathlib; from ci_article_review.adapters.cms.blocks import to_blocks; print(json.dumps([{'name': 'every-construct', 'markup': to_blocks(pathlib.Path('packages/ci-article-review/tests/golden/wordpress_editor/every_construct.md').read_text(encoding='utf-8'))}]))"
```

In the editor's console, after pasting `capture.js`, run
`await wpEditorBlocks.check(<that output>)`. Every top-level block must be
`valid: true`, and inner blocks are checked too. `round_trip: false` is not a
failure: the editor writes a heading with a class the converter leaves out and
spaces a list differently, and accepts both. Each of the quote, table, code and
separator blocks is `true` for both.

A rehearsal and this are two halves. `tools/publish_rehearsal.py` shows what leaves
the process, and the editor says whether it is accepted. The body a rehearsal
prints is the publish path's own output, so it can be run through `check` too.

## Dependencies and packaging

- **`uv lock --upgrade-package X` can fork the lock.** When X's new release raises a shared dependency's floor, uv can keep the old locked version of that dependency in one Python fork and settle X at an older release there, leaving two versions of each and rewriting about twenty unrelated entries. Dry-run first: `uv lock --upgrade-package X --dry-run` prints `Update X v1 -> v2, v3`, and **two versions after the arrow means a fork**. Name the dependency too (`--upgrade-package X --upgrade-package <dep>`), which gave one version of each and a 7-line diff in the case that taught this. To compare options without disturbing a suite running in your worktree, lock a scratch copy.
- **httpx, httpcore and anyio advisories.** This repository's own HTTPS fetching (link checks, Wayback, citation sources, WordPress, `ci_core/http.py`) uses `requests` and urllib3 with `truststore` and `curl_cffi`. Only the LLM client imports httpx, and it drives litellm's synchronous API, which uses httpcore's sync backend and the standard library's `ssl`, so anyio is not involved. anyio's TLS is reached only through httpcore's async backend, which no production code or test uses. Grep before trusting a brief that says link validation "runs through httpx".
- **hatchling reads the root `.gitignore` but matches it relative to the package directory** (pypa/hatch#304), so a line with a path separator, such as `packages/ci-style-profile/src/ci_style_profile/staging/`, never matches when building a wheel or an sdist; lines with no leading path (`.env`, `*.pem`, `pipeline_history/`) work at any depth. When a `.gitignore` line names a path under a package's `src/`, add the same path to that package's `[tool.hatch.build] exclude` (global, so it covers the sdist) or make the pattern unanchored — but unanchor only a name that is unique to what you want gone, because it cuts both ways: writing `configs/` for `/configs/` empties `configs/` from all three wheels and all three sdists (measured 2026-09-30; `packages/ci-article-review/tests/test_gitignore_configs.py` guards it). `packages/ci-style-profile/tests/test_packaging.py` guards ci-style-profile. A per-package `.gitignore` is not a fix (it shadows the root file entirely, so `.env` would ship), and neither is `ignore-vcs = true`. To test an ignore rule without hatchling, ask git in a scratch repo that holds a copy of the `.gitignore`: `git -c core.excludesFile= check-ignore --no-index --quiet -- <path>`, once with the repo-root path and once with the path relative to `packages/<pkg>`; `test_gitignore_private_drafts.py` does exactly this.

## Public repository hygiene

- **Stage files by explicit path** (`git add <path>`); never `git add -A` or `git add .`. Untracked working files, including private article drafts, can sit in a checkout.
- **Do not name an unpublished draft** in a PR, a commit message or a test. Use counts, or names that are already public in tracked docs. To check what is untracked, ask by name only: `git --no-optional-locks ls-files -o [-i] --exclude-standard -- handoff_templates`. Do not read, copy or modify drafts.
- **Ignore rules that look right can be wrong.** Test one against the real files with `git check-ignore -v`, and build the package to see what ships; a leak is a file that is git-ignored and shipped.
