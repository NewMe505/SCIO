"""The ``python`` toolchain profile: pytest, ruff and the pre-M6 parsers, driven by
``configs/profiles/python.yaml`` (ADR 0009). The data and the shared behaviour live in
``scio.toolchain.profile``; this module holds the Python parsers."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from scio.state.signatures import error_signature as _python_error_signature
from scio.toolchain.profile import PROFILES_DIR, ProfileToolchain, StackParsers, load_profile

_FAIL_SECTION_RE = re.compile(r"^=+ (FAILURES|ERRORS) =+$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^=+ .*(passed|failed|error|no tests ran).* =+$", re.MULTILINE)
_REMOVED_PUBLIC_RE = re.compile(r"^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z][\w]*)")

PythonToolchain = ProfileToolchain
"""The pre-M10 name; every profile is a ``ProfileToolchain`` now."""


def failures_only(output: str) -> str:
    """Keep the FAILURES/ERRORS sections and the summary line; drop passing noise."""
    m = _FAIL_SECTION_RE.search(output)
    body = output[m.start() :] if m else output
    lines = [ln for ln in body.splitlines() if " PASSED" not in ln and not ln.startswith("PASSED")]
    return "\n".join(lines).strip()


def has_summary(output: str) -> bool:
    return bool(_SUMMARY_RE.search(output))


def python_syntax_error(source: str, path: str) -> str | None:
    try:
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        return f"syntax error at {path}:{exc.lineno}: {exc.msg}"
    return None


def removed_public_symbol(diff_line: str) -> str | None:
    m = _REMOVED_PUBLIC_RE.match(diff_line)
    if m and not m.group(1).startswith("_"):
        return m.group(1)
    return None


PYTHON_PARSERS = StackParsers(
    name="python",
    failures_only=failures_only,
    has_summary=has_summary,
    error_signature=_python_error_signature,
    removed_public_symbol=removed_public_symbol,
    syntax_error=python_syntax_error,
)


def load_python_profile(path: Path | None = None) -> ProfileToolchain:
    """Build the profile from ``configs/profiles/python.yaml``."""
    return load_profile(path or PROFILES_DIR / "python.yaml", PYTHON_PARSERS)
