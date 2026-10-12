"""High-impact change detection (escalation signal 6).

Cheap, textual, deliberately conservative: it flags, the advisor judges.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from scio.toolchain import Toolchain, python_toolchain

DEPENDENCY_FILES = python_toolchain().dependency_files
"""The Python profile's list (ADR 0009); ``assess`` reads the toolchain it is given."""
_SCHEMA_RE = re.compile(r"(^|/)(migrations?|alembic)/|\.sql$|schema\.(json|ya?ml|prisma)$")


def changed_files(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:].strip())
    return files


def assess(diff_text: str, *, max_files: int, toolchain: Toolchain | None = None) -> list[str]:
    """Return human-readable flags; an empty list means not high impact.

    ``toolchain`` decides what counts as a source file, a test path and a removed public
    symbol (ADR 0009); the default is the Python profile.
    """
    tc = toolchain or python_toolchain()
    files = changed_files(diff_text)
    flags: list[str] = []
    if len(files) > max_files:
        flags.append(f"{len(files)} files changed (> {max_files})")
    deps = [f for f in files if PurePosixPath(f).name in tc.dependency_files]
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
        if current is None or not tc.is_source(current) or tc.is_test_path(current):
            continue
        symbol = tc.removed_public_symbol(line)
        if symbol:
            removed.append(f"{current}:{symbol}")
    if removed:
        flags.append(f"public API removed or renamed: {', '.join(removed[:5])}")
    return flags
