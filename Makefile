.PHONY: install setup lint test test-fast build

install:
	uv sync

setup:
	uv sync
	uv run python -m ci_article_review.setup

# mypy gets each package's `src` explicitly rather than the whole tree. The
# test directories are not packages (see conftest.py) and share file names
# across packages, so `mypy packages/` aborts on a duplicate module — today
# "test_import", in ci-core and ci-style-profile — having checked nothing at
# all: a silent no-op, not a warning. CI (.github/workflows/ci.yml), the
# pre-commit hook and `files` in pyproject.toml's [tool.mypy] (what a bare
# `uv run mypy` checks) scope it to src/ for the same reason; keep all four in
# agreement.
#
# One invocation rather than one per package: make stops at the first failing
# recipe line, so per-package calls would hide the later packages' errors until
# the first was fixed. `wildcard` covers a fourth package without an edit here.
MYPY_TARGETS := $(wildcard packages/*/src)

lint:
	uv run ruff check packages/
	uv run ruff format --check packages/
	uv run mypy $(MYPY_TARGETS)

test:
	uv run pytest packages/

# Same suite minus the inherently wall-clock-bound tests — for a tight inner
# loop. `make test` is what has to be green; see README "The `slow` marker".
test-fast:
	uv run pytest packages/ -m "not slow"

build:
	uv build --package ci-core
	uv build --package ci-article-review
	uv build --package ci-style-profile
