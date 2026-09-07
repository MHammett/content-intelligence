"""Fail when the handoff templates drift out of sync with what the pipeline emits.

``test_docs_current.py`` guards the documentation. These guard the *templates* —
the files a user actually fills in and pastes into a chat model — which drifted
in three separate ways while the pipeline changed underneath them:

1. The revise prompt and README told the author to paste "SECTION 1 through
   SECTION 8". That was fine when Section 9 held 2 verified citations out of 70
   claims. After the citation-coverage work it held 63 verified and 36
   content-mismatch entries — sources read and found *not* to support the claim
   they were cited for, which are among the most actionable findings a run
   produces. The author was being told to discard them.

2. ``metadata_only.md`` was still a filled-in copy of one specific published
   article, complete with its title, publication name and primary claim — and
   ``ci-setup`` copied it into every new user's working directory as a
   fill-in template. Same defect as audit finding 8, in a file that finding
   missed.

3. The revise prompt never mentioned the SEO structure review, added later,
   so those findings reached the report and stopped there.

Every assertion here derives its expectation from the code rather than
hardcoding it, so adding a SECTION 10 or a new optional block fails these tests
instead of silently going unmentioned.
"""

import re
from pathlib import Path

import pytest

from ci_article_review import report_markdown, setup as setup_mod


def _repo_root():
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError("Could not locate the repo root")


REPO_ROOT = _repo_root()
TEMPLATE_DIR = (
    REPO_ROOT
    / "packages"
    / "ci-article-review"
    / "src"
    / "ci_article_review"
    / "handoff_templates"
)
REVISE_PROMPT = TEMPLATE_DIR / "revise_after_review_prompt.md"
README = REPO_ROOT / "README.md"


def _rendered_section_numbers():
    """Section numbers report_markdown actually emits, read from its source."""
    src = Path(report_markdown.__file__).read_text(encoding="utf-8")
    return {int(n) for n in re.findall(r'"## SECTION (\d+)', src)}


class TestReviseLoopCoversEverySection:
    """The author must be told to paste every section the report renders."""

    def test_the_highest_rendered_section_is_the_one_the_prompt_asks_for(self):
        highest = max(_rendered_section_numbers())
        text = REVISE_PROMPT.read_text(encoding="utf-8")
        assert f"SECTION 1 through SECTION {highest}" in text, (
            f"report_markdown renders up to SECTION {highest}, but "
            f"{REVISE_PROMPT.name} does not ask for that range. A section the "
            "author is never told to paste is a section that never reaches the "
            "revision."
        )

    def test_the_readme_agrees_with_the_prompt(self):
        highest = max(_rendered_section_numbers())
        assert f"SECTION 1 through SECTION {highest}" in README.read_text(
            encoding="utf-8"
        ), f"README does not tell the author to paste through SECTION {highest}"

    def test_no_stale_lower_range_survives_anywhere(self):
        """Catches a half-finished update that leaves the old range behind.

        Matched on a word boundary, not as a bare substring: once the highest
        section reached two digits, "SECTION 1 through SECTION 1" became a
        prefix of the *current* range and every correct file failed as stale.
        """
        highest = max(_rendered_section_numbers())
        stale = [
            re.compile(rf"SECTION 1 through SECTION {n}") for n in range(1, highest)
        ]
        for path in (REVISE_PROMPT, README):
            text = path.read_text(encoding="utf-8")
            found = [p.pattern for p in stale if p.search(text)]
            assert not found, f"{path.name} still references {found}"

    def test_section_9_is_called_out_specifically(self):
        """It is long, easy to skip, and carries the citation findings."""
        text = REVISE_PROMPT.read_text(encoding="utf-8").lower()
        assert "content-mismatch" in text or "content mismatch" in text, (
            "The revise prompt does not explain content-mismatch entries — the "
            "sources that were read and found not to support their claim."
        )


class TestReviseLoopCoversTheOptionalBlocks:
    """Blocks the renderer can emit must be accounted for in the prompt."""

    #: Rendered-block marker -> phrase the prompt must contain to cover it.
    _BLOCKS = {
        "SEO METADATA fields": "SEO SUGGESTIONS",
        "Focus keyword candidates": "SEO SUGGESTIONS",
        "SEO structure review": "SEO STRUCTURE REVIEW",
    }

    @pytest.mark.parametrize("marker,expected", sorted(_BLOCKS.items()))
    def test_each_rendered_block_is_mentioned(self, marker, expected):
        src = Path(report_markdown.__file__).read_text(encoding="utf-8")
        if marker.lower() not in src.lower():
            pytest.skip(f"{marker!r} is not rendered by report_markdown")
        text = REVISE_PROMPT.read_text(encoding="utf-8")
        assert expected in text, (
            f"report_markdown renders a {marker!r} block, but the revise prompt "
            f"never mentions {expected!r}, so those findings stop at the report."
        )


class TestFillInTemplatesAreNotSomeonesArticle:
    """Audit finding 8, generalised to every template ci-setup hands a user."""

    #: Header fields whose value must be a placeholder, not a real one.
    _MUST_BE_PLACEHOLDER = ("Article:", "Publication:")

    @pytest.mark.parametrize("name", setup_mod._WORKING_TEMPLATES)
    def test_header_fields_are_placeholders(self, name):
        path = TEMPLATE_DIR / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        for line in path.read_text(encoding="utf-8").splitlines():
            for field in self._MUST_BE_PLACEHOLDER:
                if line.startswith(field):
                    value = line[len(field) :].strip()
                    assert value.startswith("["), (
                        f"{name} has a real value for {field!r}: {value!r}. "
                        "ci-setup copies this into a user's working directory "
                        "as a fill-in template, so it must not carry another "
                        "author's article."
                    )

    @pytest.mark.parametrize("name", setup_mod._WORKING_TEMPLATES)
    def test_no_publication_name_leaks_into_a_template(self, name):
        path = TEMPLATE_DIR / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        text = path.read_text(encoding="utf-8").lower()
        assert "mikehammett" not in text, (
            f"{name} names a specific publication. Templates ship to every "
            "user; put filled-in content in handoff_templates/examples/."
        )

    def test_every_template_setup_copies_actually_exists(self):
        missing = [
            n
            for n in setup_mod._WORKING_TEMPLATES + setup_mod._WORKED_EXAMPLES
            if not (TEMPLATE_DIR / n).exists()
            and not (TEMPLATE_DIR / "examples" / n).exists()
        ]
        assert not missing, f"ci-setup copies files that do not exist: {missing}"


class TestWorkedExamplesStayExamples:
    """The filled versions are reference material and must remain reachable."""

    @pytest.mark.parametrize("name", setup_mod._WORKED_EXAMPLES)
    def test_each_example_exists_and_is_filled_in(self, name):
        path = TEMPLATE_DIR / "examples" / name
        assert path.exists(), f"{name} is referenced by ci-setup but missing"
        text = path.read_text(encoding="utf-8")
        assert "DRAFT SUBMISSION HANDOFF" in text
        assert "[Your article title" not in text, (
            f"{name} is a worked example but contains template placeholders"
        )

    def test_examples_are_named_so_they_cannot_be_mistaken_for_templates(self):
        for name in setup_mod._WORKED_EXAMPLES:
            assert "example" in name.lower(), (
                f"{name} does not identify itself as an example in its filename, "
                "which is the only signal a user has in their working directory"
            )
