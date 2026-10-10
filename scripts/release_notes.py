"""Extract one version's section from CHANGELOG.md as GitHub Release notes.

Usage: ``python scripts/release_notes.py 0.1.0`` (prints the section) or
``python scripts/release_notes.py --cut 0.1.0`` (moves ``[Unreleased]`` into a new
``[0.1.0] - <today>`` section and prints it).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
HEADER_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$", re.MULTILINE
)


def sections(text: str) -> dict[str, str]:
    """Map version -> body (text under its header, up to the next header)."""
    out: dict[str, str] = {}
    matches = list(HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group("version")] = text[m.end() : end].strip("\n")
    return out


def notes_for(version: str, text: str | None = None) -> str:
    text = text if text is not None else CHANGELOG.read_text(encoding="utf-8")
    body = sections(text).get(version)
    if body is None:
        raise KeyError(f"CHANGELOG.md has no section for {version!r}")
    return body.strip() + "\n"


def cut(version: str, text: str, today: str | None = None) -> str:
    """Rename ``[Unreleased]`` to ``[version] - today`` and open a new empty Unreleased."""
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if f"## [{version}]" in text:
        raise ValueError(f"{version} already has a section")
    if "## [Unreleased]" not in text:
        raise ValueError("no [Unreleased] section to cut")
    return text.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{version}] - {today}", 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--cut", action="store_true", help="move [Unreleased] into this version first")
    args = ap.parse_args(argv)
    version = args.version.lstrip("v")
    text = CHANGELOG.read_text(encoding="utf-8")
    if args.cut:
        text = cut(version, text)
        CHANGELOG.write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(notes_for(version, text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
