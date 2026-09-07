"""An unrecognised publication-config key is an error, and the list is kept honest.

Rejecting unknown keys is only safe while ``KNOWN_PUB_KEYS`` is complete. If the
list falls behind the code, a config that is entirely correct stops loading —
which is a worse failure than the silent one this replaced. Two tests here exist
solely to prevent that: one cross-checks the list against the keys the source
actually reads, and one validates every config the repo ships.

Both are the same shape as ``test_docs_current.py`` — mechanical drift, caught in
the PR that introduces it, with a message naming the file to fix.
"""

import re
from pathlib import Path

import pytest
import yaml

from ci_article_review.config_loader import (
    KNOWN_PUB_KEYS,
    _validate_publication_keys,
)


def _repo_root():
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError("Could not locate the repo root")


REPO_ROOT = _repo_root()
SRC = REPO_ROOT / "packages" / "ci-article-review" / "src" / "ci_article_review"
CONFIGS = SRC / "configs"

#: ``pub_config.get("x")`` / ``pub_config["x"]`` and the merged-config form.
_READ_KEY = re.compile(
    r"""(?:pub_config(?:_raw)?|config\["publication"\])\s*(?:\.get\(\s*["']([a-z_]+)["']|\[\s*["']([a-z_]+)["'])"""
)


def _keys_the_source_reads():
    found = set()
    for path in SRC.rglob("*.py"):
        for match in _READ_KEY.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1) or match.group(2))
    return found


class TestKnownKeysCoverWhatIsRead:
    """The list must not fall behind the code that reads publication config."""

    def test_every_key_the_source_reads_is_declared(self):
        read = _keys_the_source_reads()
        assert read, "the source scan found nothing — the regex has gone stale"
        undeclared = sorted(read - set(KNOWN_PUB_KEYS))
        assert not undeclared, (
            f"These publication keys are read by the code but missing from "
            f"KNOWN_PUB_KEYS in config_loader.py: {undeclared}. Unknown keys are "
            f"rejected, so a config setting one of these would fail to load even "
            f"though the pipeline reads it. Add them to the list."
        )


class TestEveryShippedConfigValidates:
    """If the repo's own examples do not load, neither will anyone's copy."""

    def _shipped(self):
        paths = [CONFIGS / "publication.example.yaml"]
        paths += sorted((CONFIGS / "examples").glob("*.yaml"))
        return [p for p in paths if p.is_file()]

    def test_there_are_configs_to_check(self):
        assert self._shipped(), "no shipped publication configs found"

    def test_each_one_passes_strict_validation(self):
        for path in self._shipped():
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _validate_publication_keys(config, path.stem)


class TestUnknownKeysAreRejected:
    def _validate(self, **extra):
        _validate_publication_keys(
            {"publication_name": "acme", "wordpress": {}, **extra}, "acme"
        )

    def test_a_config_of_known_keys_passes(self):
        self._validate(author_name="Someone", style_profile="Direct.")

    def test_an_unknown_key_is_an_error(self):
        with pytest.raises(ValueError) as exc:
            self._validate(byline="Someone")
        assert "byline" in str(exc.value)

    def test_a_typo_is_named_with_the_key_it_shadows(self):
        """The whole point: 'authorname' used to read as an absent author."""
        with pytest.raises(ValueError) as exc:
            self._validate(authorname="Someone")
        message = str(exc.value)
        assert "authorname" in message
        assert "did you mean 'author_name'" in message
        assert "silently falls back" in message

    def test_the_error_lists_what_is_valid(self):
        with pytest.raises(ValueError) as exc:
            self._validate(byline="Someone")
        assert "author_name" in str(exc.value)
        assert "publication_description" in str(exc.value)

    def test_every_unknown_key_is_reported_not_just_the_first(self):
        """Fixing one at a time, one failed run each, is the wrong loop."""
        with pytest.raises(ValueError) as exc:
            self._validate(byline="a", authorname="b", tone="c")
        message = str(exc.value)
        assert all(k in message for k in ("byline", "authorname", "tone"))
        assert "3 keys" in message


class TestTheExtensionPrefix:
    """`x_` keys are deliberately not ours, and are never validated.

    Borrowed from OpenAPI's `x-` specification extensions, which exist for this
    exact problem: a strict schema that still has to be extensible. Without it,
    strictness would mean a config file cannot carry a note or a value some
    other tool reads.
    """

    def _validate(self, **extra):
        _validate_publication_keys({"publication_name": "acme", **extra}, "acme")

    def test_an_extension_key_is_allowed(self):
        self._validate(x_internal_note="ask legal before publishing")

    def test_extension_keys_are_allowed_alongside_real_ones(self):
        self._validate(author_name="Someone", x_reviewed_by="editorial")

    def test_the_error_message_points_at_the_prefix(self):
        with pytest.raises(ValueError) as exc:
            self._validate(internal_note="x")
        assert "prefix it with 'x_'" in str(exc.value)

    def test_the_prefix_does_not_excuse_a_typo_of_itself(self):
        """`x-note` is not the prefix; only `x_` is."""
        with pytest.raises(ValueError):
            self._validate(**{"x-note": "x"})
