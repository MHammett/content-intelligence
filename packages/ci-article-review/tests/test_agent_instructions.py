"""The agent-instruction layer stays in one piece.

Claude Code reads ``.claude/skills/`` and Codex reads ``.agents/skills/``, and neither reads the other's directory,
so every skill exists in both places and the two copies must not drift. A skill is a short trigger that points at a
document under ``docs/``; the procedure lives there, once.
"""

import re
from pathlib import Path

import pytest


def _find_repo_root():
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError(
        "could not find the repository root (a directory holding packages/ and README.md)"
    )


ROOT = _find_repo_root()
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
CODEX_SKILLS = ROOT / ".agents" / "skills"
# The Agent Skills format both tools follow: a lowercase-hyphen name of at most 64 characters that equals the
# directory name, and a description of at most 1024 characters.
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DOC_REFERENCE = re.compile(r"docs/[A-Za-z0-9._-]+\.md")


def _skills(base):
    return {path.parent.name: path for path in sorted(base.glob("*/SKILL.md"))}


def _text(path):
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "a SKILL.md starts with a --- frontmatter block"
    fields = {}
    for line in match.group(1).split("\n"):
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            fields[key.strip()] = value.strip()
    return fields


SKILL_NAMES = sorted(_skills(CLAUDE_SKILLS))


def test_there_is_at_least_one_skill():
    assert SKILL_NAMES, f"no skills under {CLAUDE_SKILLS.relative_to(ROOT).as_posix()}"


def test_every_skill_exists_for_both_tools():
    claude, codex = set(_skills(CLAUDE_SKILLS)), set(_skills(CODEX_SKILLS))
    assert claude == codex, (
        f"only under .claude/skills: {sorted(claude - codex)}; only under .agents/skills: {sorted(codex - claude)}. "
        "Claude Code and Codex read different directories, so each skill has to exist in both."
    )


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_the_two_copies_of_a_skill_are_identical(name):
    claude = _text(_skills(CLAUDE_SKILLS)[name])
    codex = _text(_skills(CODEX_SKILLS)[name])
    assert claude == codex, (
        f"{name}: .claude/skills and .agents/skills have drifted apart; edit one and copy it to the other"
    )


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_a_skills_frontmatter_follows_the_format(name):
    fields = _frontmatter(_text(_skills(CLAUDE_SKILLS)[name]))
    assert fields.get("name") == name, (
        f"the name must equal the directory name {name!r}, got {fields.get('name')!r}"
    )
    assert NAME.match(name) and len(name) <= 64, (
        f"{name!r} is not a lowercase-hyphen name of at most 64 characters"
    )
    description = fields.get("description", "")
    assert description, (
        f"{name}: a skill needs a description (it is what makes a tool pick the skill)"
    )
    assert len(description) <= 1024, (
        f"{name}: the description is {len(description)} characters; the limit is 1024"
    )


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_a_skill_points_at_a_document_that_exists(name):
    text = _text(_skills(CLAUDE_SKILLS)[name])
    references = sorted(set(DOC_REFERENCE.findall(text)))
    assert references, (
        f"{name}: a skill is a trigger that points at a document under docs/, and this one names none"
    )
    missing = [ref for ref in references if not (ROOT / ref).is_file()]
    assert not missing, f"{name}: points at documents that do not exist: {missing}"
