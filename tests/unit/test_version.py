"""I-055: the package version must move with the changelog and the tag."""

from __future__ import annotations

import re
import tomllib

import scio
from scio.settings import REPO_ROOT


def test_package_version_matches_changelog() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}\s*$", text, re.MULTILINE)
    assert released, "CHANGELOG.md has no released section"
    newest = released[0]
    assert scio.__version__ == newest, (scio.__version__, newest)
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == newest
