"""High-impact change detection (escalation signal 6).

Cheap, textual, deliberately conservative: it flags, the advisor judges.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

DEPENDENCY_FILES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "go.mod",
    "Cargo.toml",
}
_SCHEMA_RE = re.compile(r"(^|/)(migrations?|alembic)/|\.sql$|schema\.(json|ya?ml|prisma)$")
_REMOVED_PUBLIC_RE = re.compile(r"^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z][\w]*)")


def changed_files(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:].strip())
    return files


def assess(diff_text: str, *, max_files: int) -> list[str]:
    """Return human-readable flags; an empty list means not high impact."""
    files = changed_files(diff_text)
    flags: list[str] = []
    if len(files) > max_files:
        flags.append(f"{len(files)} files changed (> {max_files})")
    deps = [f for f in files if PurePosixPath(f).name in DEPENDENCY_FILES]
    if deps:
        flags.append(f"dependency file changed: {', '.join(deps)}")
    schema = [f for f in files if _SCHEMA_RE.search(f)]
    if schema:
        flags.append(f"schema/migration changed: {', '.join(schema)}")
    current: str | None = None
    removed: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if current is None or not current.endswith(".py"):
            continue
        name = PurePosixPath(current).name
        if name.startswith("test_") or "/tests/" in f"/{current}":
            continue
        m = _REMOVED_PUBLIC_RE.match(line)
        if m and not m.group(1).startswith("_"):
            removed.append(f"{current}:{m.group(1)}")
    if removed:
        flags.append(f"public API removed or renamed: {', '.join(removed[:5])}")
    return flags
