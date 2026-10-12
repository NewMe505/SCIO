"""Error signatures and progress fingerprints.

``error_signature`` answers "is this the same failure as last time?" for escalation
signal 1 (same error after 2 fix attempts). It is stable across line-number shifts,
memory addresses and timestamps, and sensitive to the exception type, the failing
location (file and function) and the first assertion or error line.

``progress_fingerprint`` answers "did anything change?" for signal 2 (stall).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from scio.state.models import FeatureItem, ProgressLedger

_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line \d+, in (?P<func>\S+)')
_PYTEST_LOC_RE = re.compile(
    r"^(?P<file>[\w./\\-]+\.py):\d+: (?P<exc>\w+(?:Error|Exception|Failed)?)"
)
_EXC_RE = re.compile(
    r"^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Failed|Warning))\b(?::\s*(?P<msg>.*))?$"
)
_ASSERT_RE = re.compile(r"^\s*(?:E\s+)?(assert .*|AssertionError.*)$")
_NOISE_RE = re.compile(
    r"0x[0-9a-fA-F]+|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\d.:+Z]*|\btmp\w+\b|line \d+"
)


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").split("/")[-1]


def error_signature(text: str) -> str:
    """Return a short stable hash of a traceback or pytest failure block."""
    exc: str | None = None
    msg: str = ""
    file: str | None = None
    func: str | None = None
    first_assert: str | None = None
    evaluated_assert: str | None = None  # pytest's "E   assert 2 == 3" beats the source line

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _FRAME_RE.search(line)
        if m:  # last frame wins: closest to the failure
            file, func = _norm_path(m.group("file")), m.group("func")
            continue
        m = _PYTEST_LOC_RE.match(line.strip())
        if m:
            file = file or _norm_path(m.group("file"))
            exc = exc or m.group("exc")
            continue
        m = _EXC_RE.match(line.strip())
        if m and (exc is None or not line.startswith(" ")):
            exc = m.group("exc")
            msg = (m.group("msg") or "")[:80]
            continue
        m = _ASSERT_RE.match(line)
        if m:
            if line.lstrip().startswith("E ") and evaluated_assert is None:
                evaluated_assert = m.group(1)[:120]
            elif first_assert is None:
                first_assert = m.group(1)[:120]

    chosen = evaluated_assert or first_assert or _NOISE_RE.sub("", msg)
    parts = [exc or "unknown", file or "?", func or "?", chosen]
    blob = "|".join(_NOISE_RE.sub("", p).strip() for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def progress_fingerprint(progress: ProgressLedger, feature_list: list[FeatureItem]) -> str:
    """Hash of everything that counts as progress. Unchanged fingerprint => stall."""
    blob = "|".join(
        [
            progress.current_step_id or "",
            progress.current_feature_id or "",
            progress.last_error_signature or "",
            ",".join(f"{f.id}:{f.status.value}:{f.commit_sha or ''}" for f in feature_list),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def advance(
    progress: ProgressLedger,
    feature_list: list[FeatureItem],
    *,
    error_text: str | None,
    signature_fn: Callable[[str], str] = error_signature,
) -> ProgressLedger:
    """Return the ledger after one act→verify iteration (pure; the node writes it back).

    ``signature_fn`` is the toolchain's error-signature parser (ADR 0009); the default is
    the Python one, so callers without a toolchain keep the pre-M6 behaviour.
    """
    p = progress.model_copy()
    p.iterations += 1
    if error_text is None:
        p.last_error_signature = None
        p.fix_attempts_on_error = 0
    else:
        sig = signature_fn(error_text)
        p.fix_attempts_on_error = (
            p.fix_attempts_on_error + 1 if sig == p.last_error_signature else 1
        )
        p.last_error_signature = sig
    fp = progress_fingerprint(p, feature_list)
    p.stall_count = p.stall_count + 1 if fp == p.last_fingerprint else 0
    p.last_fingerprint = fp
    return p
