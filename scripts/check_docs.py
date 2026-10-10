"""Docs checks. Run in CI; a failure here blocks merge like a test failure.

Checks (M1):
1. Relative links in Markdown files resolve to existing files.
2. Model IDs appear only in ``configs/models.yaml`` (and docs). Code refers to roles.
3. ``CHANGELOG.md`` has an ``[Unreleased]`` section.

M2 adds: architecture diagram vs. compiled graph, runnable code samples, public-API
docstring coverage.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")
MODEL_ID_RE = re.compile(r"\b(claude-[a-z]+-\d(?:-\d)?|gpt-\d[\w.-]*)\b")
CODE_DIRS = ("scio", "evals", "tests", "scripts")
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "docs/api",
    "evals/results",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
}


def _skip(p: Path) -> bool:
    rel = p.relative_to(ROOT).as_posix()
    return any(rel == s or rel.startswith(s + "/") for s in SKIP_DIRS)


def check_links() -> list[str]:
    errors: list[str] = []
    for md in ROOT.rglob("*.md"):
        if _skip(md):
            continue
        for m in LINK_RE.finditer(md.read_text(encoding="utf-8", errors="replace")):
            target = m.group(1)
            if re.match(r"^[a-z]+:", target) or target.startswith("mailto:"):
                continue
            if not (md.parent / target).exists():
                errors.append(f"{md.relative_to(ROOT)}: broken link -> {target}")
    return errors


def check_model_ids() -> list[str]:
    errors: list[str] = []
    for d in CODE_DIRS:
        for f in (ROOT / d).rglob("*.py"):
            if _skip(f):
                continue
            for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if MODEL_ID_RE.search(line) and "noqa: model-id" not in line:
                    errors.append(
                        f"{f.relative_to(ROOT)}:{i}: model ID in code; "
                        "use a role from configs/models.yaml"
                    )
    return errors


def check_changelog() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return [] if "## [Unreleased]" in text else ["CHANGELOG.md: missing '## [Unreleased]' section"]


def main() -> int:
    errors = check_links() + check_model_ids() + check_changelog()
    for e in errors:
        print(f"docs-check: {e}")
    print(f"docs-check: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
