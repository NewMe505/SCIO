"""Classify a finished run: a pure function with one flip test per row (ADR 0019 decision 5).
OWNER: wave-1 agent B."""

from __future__ import annotations

from collections.abc import Sequence

from recovery.model import Classification, Evidence, FailureClass, Frame

TASK_FAULT_REASONS: frozenset[str] = frozenset(
    {
        "integration_failed",
        "gate_escalated",
        "release_failed",
        "stopped",
        "budget",
        "load_failed",
        "sync_failed",
    }
)
"""Halt reasons that are the product's or a human's business, never a SCIO repair."""

AUTH_MARKERS: tuple[str, ...] = (
    "not logged in",
    "authentication",
    "invalid_grant",
    "please run /login",
    "oauth token",
)
TRANSIENT_MARKERS: tuple[str, ...] = (
    "rate_limit",
    "rate limit",
    "usage limit",
    "overloaded",
    "server_error",
    "api_retry",
    "scio build stopped on github:",
    "connection reset",
    "temporarily unavailable",
)


def normalise_message(text: str) -> str:
    """The message with volatile parts replaced: numbers, hex ids, paths, quoted values,
    durations and timestamps. Two occurrences of the same defect normalise equally."""
    raise NotImplementedError


def innermost_scio_frame(frames: Sequence[Frame]) -> str:
    """``scio/<relative path>:<function>`` for the innermost frame whose path contains a
    ``scio`` package directory. Paths are forward slashes from ``scio/`` on. "" if none."""
    raise NotImplementedError


def signature(failure_class: FailureClass, exception_type: str, frame: str, message: str) -> str:
    """``sha256`` over the class, the exception type, the frame and the normalised message:
    16 hex characters."""
    raise NotImplementedError


def classify(ev: Evidence) -> Classification:
    """Rows are checked in order, and the first match wins.

    1. ``orphaned`` with no traceback: ``ORPHAN``.
    2. ``hung``: ``HANG``.
    3. Exit 0 and ``paused``: ``HUMAN_STOP``.
    4. Exit 0 and not paused: ``DONE``.
    5. An auth marker anywhere in the failure text (message, last ledger line, stderr):
       ``EXTERNAL_AUTH``.
    6. A transient marker: ``EXTERNAL_TRANSIENT``.
    7. ``halt_reason`` in ``TASK_FAULT_REASONS``: ``TASK_FAULT``.
    8. A traceback with an innermost frame under ``scio/``: ``HARNESS_SUSPECT``.
    9. ``halt_reason == "feature_failed"`` whose last ledger line names an exception
       (``... failed: <Type>: ...``): ``HARNESS_SUSPECT``. A human stop at the feature
       decision (``... failed (...); build stopped``) is ``TASK_FAULT``.
    10. Anything else: ``UNKNOWN``.

    ``signature`` is "" for ``DONE`` and ``HUMAN_STOP``.
    """
    raise NotImplementedError
