"""The ``node-ts`` toolchain profile: Node's built-in test runner (TAP output), eslint and
tsc as the quality bar, npm as the install phase, driven by ``configs/profiles/node-ts.yaml``
(ADR 0014). Node 22.18+ strips TypeScript types itself, so a TypeScript product runs and
tests with no download; the data lives in the yaml, this module holds the TAP parsers.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from scio.toolchain.profile import PROFILES_DIR, ProfileToolchain, StackParsers, load_profile

_SUMMARY_RE = re.compile(r"^# (tests|pass|fail) \d+", re.MULTILINE)
_RESULT_LINE_RE = re.compile(r"^(not )?ok \d+")
_REMOVED_EXPORT_RE = re.compile(
    r"^-\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function\*?|class|const|let|var|type|interface|enum|abstract\s+class)\s+([A-Za-z_$][\w$]*)"
)
_LOCATION_RE = re.compile(r"^\s*location:\s*'(?P<loc>.+)'\s*$")
_FIELD_RE = re.compile(r"^\s*(?P<key>name|code|failureType):\s*'(?P<val>[^']*)'\s*$")
_ERROR_RE = re.compile(r"^\s*error:\s*(?P<val>.*)$")
_STACK_FRAME_RE = re.compile(r"\((?:file:///)?(?P<file>[^()]+?):\d+:\d+\)")
_NOISE_RE = re.compile(r"0x[0-9a-fA-F]+|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\d.:+Z]*|:\d+:\d+")


def failures_only(output: str) -> str:
    """Keep every ``not ok`` block (with its YAML diagnostics) and the ``#`` summary lines;
    drop passing subtests and their diagnostics."""
    kept: list[str] = []
    keep_block = False
    for line in output.splitlines():
        if _RESULT_LINE_RE.match(line):
            keep_block = line.startswith("not ok")
            if keep_block:
                kept.append(line)
            continue
        if line.startswith("# Subtest:") or re.match(r"^\d+\.\.\d+$", line):
            keep_block = False
            continue
        if line.startswith("# "):
            kept.append(line)
            continue
        if keep_block:
            kept.append(line)
    return "\n".join(kept).strip()


def has_summary(output: str) -> bool:
    return bool(_SUMMARY_RE.search(output))


def _basename(p: str) -> str:
    return p.replace("\\\\", "/").replace("\\", "/").rstrip("'").split("/")[-1]


def error_signature(text: str) -> str:
    """A short stable hash of a TAP failure block: the error name or code, the failing file
    (no line numbers) and the error message (values included, as pytest's evaluated assert
    is in the Python signature; a different wrong value is a different failure)."""
    name = code = failure_type = ""
    file = ""
    message = ""
    in_error = False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _FIELD_RE.match(line)
        if m:
            in_error = False
            key, val = m.group("key"), m.group("val")
            if key == "name":
                name = name or val
            elif key == "code":
                code = code or val
            else:
                failure_type = failure_type or val
            continue
        m = _LOCATION_RE.match(line)
        if m:
            in_error = False
            file = file or _basename(_NOISE_RE.sub("", m.group("loc")))
            continue
        m = _ERROR_RE.match(line)
        if m:
            val = m.group("val").strip()
            if val and val not in ("|-", "|", ">-"):
                message = message or val
            else:
                in_error = not message  # a block scalar: collect its lines until the next key
            continue
        if in_error:
            if re.match(r"^\s{2}[A-Za-z_]+:", line):  # the next diagnostic key ends the block
                in_error = False
            elif line.strip():
                message = f"{message} {line.strip()}".strip()
            continue
        if not file:
            fm = _STACK_FRAME_RE.search(line)
            if fm and not fm.group("file").startswith("node:"):
                file = _basename(fm.group("file"))
        # a plain uncaught error line (TypeError: x is not a function)
        head, sep, rest = line.strip().partition(":")
        if not name and not message and sep and head.endswith("Error"):
            name, message = head, rest.strip()
    key = "|".join((name or code or failure_type, file, _NOISE_RE.sub("", message)[:200]))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def removed_public_symbol(diff_line: str) -> str | None:
    m = _REMOVED_EXPORT_RE.match(diff_line)
    return m.group(1) if m else None


def _no_in_process_check(source: str, path: str) -> str | None:
    return None  # ``node --check`` runs through ``syntax_check_argv`` instead


NODE_TS_PARSERS = StackParsers(
    name="node-ts",
    failures_only=failures_only,
    has_summary=has_summary,
    error_signature=error_signature,
    removed_public_symbol=removed_public_symbol,
    syntax_error=_no_in_process_check,
)


def load_node_ts_profile(path: Path | None = None) -> ProfileToolchain:
    """Build the profile from ``configs/profiles/node-ts.yaml``."""
    return load_profile(path or PROFILES_DIR / "node-ts.yaml", NODE_TS_PARSERS)
