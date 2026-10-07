"""Every package declares the repository's license, and its artifacts carry the text.

The root ``LICENSE`` is the one that matters to a reader of the repository. A
wheel or sdist is built from one package directory, though, and PEP 639's
``license-files`` globs may not leave it (no ``..``), so each package holds its
own copy of the root file. A copy that drifts, or a ``pyproject.toml`` that
stops naming the license, would ship a package whose metadata and text disagree
with the repository, and nothing else would notice.

The artifact check runs the real build backend over the real package directory,
into a temporary directory, and reads what came out.
"""

import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("ci-core", "ci-article-review", "ci-style-profile")


def _text(path):
    """A file's text with line endings normalised: a Windows checkout has CRLF."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_the_root_license_is_mit_under_the_authors_name():
    text = _text(REPO_ROOT / "LICENSE")
    assert text.startswith("MIT License\n\nCopyright (c) 2026 Mike Hammett\n")
    assert "Permission is hereby granted, free of charge" in text


@pytest.mark.parametrize("package", PACKAGES)
def test_a_packages_license_is_a_copy_of_the_root_one(package):
    assert _text(REPO_ROOT / "packages" / package / "LICENSE") == _text(
        REPO_ROOT / "LICENSE"
    )


@pytest.mark.parametrize("package", PACKAGES)
def test_a_package_declares_mit_and_a_backend_that_reads_it(package):
    config = tomllib.loads(
        (REPO_ROOT / "packages" / package / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert config["project"]["license"] == "MIT"
    assert config["project"]["license-files"] == ["LICENSE"]
    # PEP 639 metadata needs hatchling 1.27; an older one ignores both keys.
    assert config["build-system"]["requires"] == ["hatchling>=1.27"]


@pytest.mark.parametrize("package", PACKAGES)
def test_the_built_wheel_and_sdist_carry_the_license(package, tmp_path):
    package_dir = REPO_ROOT / "packages" / package
    subprocess.run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "-t",
            "wheel",
            "-t",
            "sdist",
            "-d",
            str(tmp_path),
        ],
        cwd=package_dir,
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    (sdist,) = tmp_path.glob("*.tar.gz")
    expected = _text(REPO_ROOT / "LICENSE")

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        (metadata_name,) = [n for n in names if n.endswith(".dist-info/METADATA")]
        metadata = zf.read(metadata_name).decode("utf-8")
        (license_name,) = [
            n for n in names if n.endswith(".dist-info/licenses/LICENSE")
        ]
        in_wheel = zf.read(license_name).decode("utf-8").replace("\r\n", "\n")
    assert "License-Expression: MIT" in metadata
    assert "License-File: LICENSE" in metadata
    assert in_wheel == expected

    with tarfile.open(sdist) as tf:
        (member,) = [m for m in tf.getmembers() if m.name.endswith("/LICENSE")]
        in_sdist = tf.extractfile(member).read().decode("utf-8").replace("\r\n", "\n")
    assert in_sdist == expected
