"""Classify a finished run: a pure function with one flip test per row (ADR 0019 decision 5).
OWNER: wave-1 agent B."""

from __future__ import annotations

import hashlib
import re
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
    "claude -p timed out",  # SCIO's TIMEOUT_MESSAGE: a hung claude, killed (I-180, #105)
)

NOT_A_DEFECT: frozenset[str] = frozenset({"BudgetExceeded", "RefusalError"})
"""Harness errors that end a feature attempt without any SCIO defect behind them: a spend
cap and a model's refusal. A feature line naming one is a task fault, never a repair."""

GITHUB_HALT = "github"
"""The outcome file's ``exit_reason`` for ``scio build stopped on GitHub:``."""

HARNESS_STOP = re.compile(
    r"scio (?P<command>\w+) stopped on a harness error: (?P<type>[A-Za-z_][\w.]*): "
    r"(?P<message>.*)"
)
"""``scio/cli.py``'s ``harness_stop_line``: a ``HarnessError`` that no node caught stopped
``scio <command>`` (outcome ``exit_reason`` ``harness``, I-182). Before, the same failure
was a traceback through SCIO (row 8)."""

TRACEBACK_HEADER = "Traceback (most recent call last):"
CHAIN_SENTENCES: tuple[str, ...] = (
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
)

FEATURE_ERROR = re.compile(
    r"(?P<feature>\S+) attempt (?P<attempt>\d+) failed: (?P<type>[A-Za-z_][\w.]*): "
    r"(?P<message>.*?)(?:; the next `scio build` starts attempt \d+)?\s*",
    re.DOTALL,
)
"""``scio/product/build.py``'s ledger line for a ``HarnessError`` inside a feature attempt."""

FEATURE_STOP = re.compile(
    r"(?P<feature>\S+) failed \((?P<reason>.*)\); build stopped\s*", re.DOTALL
)
"""The ledger line for a human's stop at the feature decision."""

FEATURE_ERROR_RUN = re.compile(
    r"^\s+(?P<feature>\S+) a(?P<attempt>\d+) \S+ failed exit=error\b", re.MULTILINE
)
"""The build summary's feature line for an attempt a ``HarnessError`` ended (both of
``build.py``'s ``HarnessError`` branches, and only they, record ``exit_reason="error"``).
Row 9 falls back to it when parallel features put a passing feature's line after the
failing one, so the last ledger line does not name the exception."""

_FRAME_LINE = re.compile(r'^\s*File "[^"]+", line \d+')
_TIMESTAMP = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_WINDOWS_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'<>|:*?,;()\[\]]*")
_UNC_PATH = re.compile(r"\\\\[^\s\"'<>|:*?,;()\[\]]+")
_POSIX_PATH = re.compile(r"(?<![\w.:/~<>-])~?/[\w.@+-]+(?:/[\w.@+-]*)*")
_BACKSLASH_PATH = re.compile(r"[\w.-]*(?:\\[\w.-]+)+\\?")
_BRACKET_QUOTED = re.compile(r"(?<!\w)([\"'])\[.*?\]\1")
_DOUBLE_QUOTED = re.compile(r"(?<!\w)\"[^\"\n]*\"")
_SINGLE_QUOTED = re.compile(r"(?<!\w)'[^'\n]*'")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_LITERAL = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_HEX_ID = re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\b")
_DURATION = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b"
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_REPEATED = re.compile(r"(<(?:ts|path|q|n|dur)>)(?:[\s,;:.x/-]*\1)+")
_SPACE = re.compile(r"\s+")


def normalise_message(text: str) -> str:
    """The message with volatile parts replaced: numbers, hex ids, paths, quoted values,
    durations and timestamps. Two occurrences of the same defect normalise equally."""
    out = _TIMESTAMP.sub("<ts>", text)
    out = _WINDOWS_PATH.sub("<path>", out)
    out = _UNC_PATH.sub("<path>", out)
    out = _POSIX_PATH.sub("<path>", out)
    out = _BACKSLASH_PATH.sub("<path>", out)
    # a quoted list first: ``Command '['claude', '-p', ...]'`` is one value, whatever its length
    out = _BRACKET_QUOTED.sub("<q>", out)
    out = _DOUBLE_QUOTED.sub("<q>", out)
    out = _SINGLE_QUOTED.sub("<q>", out)
    out = _UUID.sub("<n>", out)
    out = _HEX_LITERAL.sub("<n>", out)
    out = _HEX_ID.sub("<n>", out)
    out = _DURATION.sub("<dur>", out)
    out = _NUMBER.sub("<n>", out)
    out = _REPEATED.sub(r"\1", out)
    return _SPACE.sub(" ", out).strip()


def scio_module_path(path: str) -> str:
    """``scio/<module path>.py`` for a frame path inside a ``scio`` package, else "".

    The path is taken from the LAST component named exactly ``scio`` (case-sensitive, so
    the ``SCIO`` checkout and ``.scio`` are not it) that is followed by a module path:
    identifiers, then a ``.py`` file. So ``.../SCIO/.scio/live-wt/scio/x.py`` is
    ``scio/x.py``, and ``.../scio/.venv/Lib/site-packages/langgraph/x.py`` is not scio's.
    """
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] != "scio":
            continue
        *dirs, leaf = parts[i + 1 :]
        if (
            leaf.endswith(".py")
            and leaf[:-3].isidentifier()
            and all(d.isidentifier() for d in dirs)
        ):
            return "/".join(["scio", *dirs, leaf])
    return ""


def innermost_scio_frame(frames: Sequence[Frame]) -> str:
    """``scio/<relative path>:<function>`` for the innermost frame whose path contains a
    ``scio`` package directory. Paths are forward slashes from ``scio/`` on. "" if none."""
    for frame in reversed(frames):
        module = scio_module_path(frame.path)
        if module:
            return f"{module}:{frame.function}"
    return ""


def signature(failure_class: FailureClass, exception_type: str, frame: str, message: str) -> str:
    """``sha256`` over the class, the exception type, the frame and the normalised message:
    16 hex characters."""
    material = "\x1f".join(
        (str(failure_class), exception_type.strip(), frame.strip(), normalise_message(message))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _stderr_failure(ev: Evidence) -> str:
    """The part of stderr that says why the run ended.

    - A halt SCIO reported itself (any exit reason but a GitHub stop): none. Its ledger
      line says why, and stderr holds only what the run logged on the way, such as a
      ``retry 1/3 after 2.0s: rate limit`` warning it rode out.
    - Else the lines that are not indented, from the start of the last traceback chain
      (or its first frame, when the tail cut the header) or the last GitHub stop line,
      whichever is later. Frames and source lines are left out: a source line is code,
      not the failure.
    - Else, with neither, the whole stderr tail.
    """
    if ev.halt_reason and ev.halt_reason != GITHUB_HALT:
        return ""
    lines = ev.stderr_tail.splitlines()
    start = max(
        traceback_chain_start(lines),
        _last_index(lines, "scio build stopped on github:"),
        _last_index(lines, "stopped on a harness error:"),
    )
    if start < 0:
        return ev.stderr_tail
    kept = [ln for ln in lines[start:] if ln.strip() and not ln[:1].isspace()]
    return "\n".join(kept)


def _last_index(lines: Sequence[str], needle: str) -> int:
    for i in range(len(lines) - 1, -1, -1):
        if needle in lines[i].lower():
            return i
    return -1


def is_frame_line(line: str) -> bool:
    return _FRAME_LINE.match(line) is not None


def traceback_block_start(lines: Sequence[str], end: int | None = None) -> int:
    """The first line of the last traceback block in ``lines[:end]``: its header, or its
    first frame when the tail cut the header off. -1 when there is none.

    The block is found from its last frame backwards, over frame lines and indented lines
    (source, carets, ``[Previous line repeated ...]``), so a later block whose header was
    cut is not mistaken for the one before it."""
    end = len(lines) if end is None else end
    last = next((i for i in range(end - 1, -1, -1) if is_frame_line(lines[i])), -1)
    if last < 0:
        return next((i for i in range(end - 1, -1, -1) if lines[i].strip() == TRACEBACK_HEADER), -1)
    start = last
    while start > 0 and (
        is_frame_line(lines[start - 1])
        or (lines[start - 1][:1].isspace() and bool(lines[start - 1].strip()))
    ):
        start -= 1
    if start > 0 and lines[start - 1].strip() == TRACEBACK_HEADER:
        return start - 1
    return start


def traceback_chain_start(lines: Sequence[str]) -> int:
    """The first line of the last traceback chain (blocks joined by "During handling of the
    above exception" or "The above exception was the direct cause"), or -1."""
    start = traceback_block_start(lines)
    while start > 0:
        prev = next((i for i in range(start - 1, -1, -1) if lines[i].strip()), -1)
        if prev < 0 or lines[prev].strip() not in CHAIN_SENTENCES:
            break
        earlier = traceback_block_start(lines, prev)
        if earlier < 0:
            break
        start = earlier
    return start


def failure_text(ev: Evidence) -> str:
    """What rows 5 and 6 search: the exception, the last ledger line and the part of
    stderr that ended the run (:func:`_stderr_failure`)."""
    parts = (ev.exception_type, ev.exception_message, ev.last_ledger_line, _stderr_failure(ev))
    return "\n".join(p for p in parts if p)


def _first_marker(text: str, markers: Sequence[str]) -> tuple[str, str]:
    """The first marker found in ``text`` (case-insensitive) and the line that holds it."""
    for line in text.splitlines():
        low = line.lower()
        for marker in markers:
            if marker in low:
                return marker, line
    return "", ""


def _one_line(text: str, limit: int = 160) -> str:
    flat = _SPACE.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _first_line(text: str) -> str:
    return next((ln for ln in text.splitlines() if ln.strip()), "")


def _result(
    failure_class: FailureClass,
    summary: str,
    frame: str,
    *,
    exception_type: str = "",
    sig_frame: str = "",
    message: str = "",
) -> Classification:
    sig = signature(failure_class, exception_type, sig_frame, message)
    return Classification(failure_class, sig, _one_line(summary), frame)


def classify(ev: Evidence) -> Classification:
    """Rows are checked in order, and the first match wins.

    1. ``orphaned`` with no traceback: ``ORPHAN``.
    2. ``hung``: ``HANG``.
    3. Exit 0 and ``paused``: ``HUMAN_STOP``.
    4. Exit 0 and not paused: ``DONE``.
    4a. A ``KeyboardInterrupt`` (a person pressed Ctrl+C): ``HUMAN_STOP``.
    4b. ``feature_failed`` whose last ledger line is a human's stop at the feature decision
        (``... failed (...); build stopped``): ``TASK_FAULT``. This row sits above the marker
        rows because a person's free text can hold a marker word.
    5. An auth marker anywhere in the failure text (message, last ledger line, stderr):
       ``EXTERNAL_AUTH``.
    6. A transient marker: ``EXTERNAL_TRANSIENT``.
    7. ``halt_reason`` in ``TASK_FAULT_REASONS``: ``TASK_FAULT``.
    8. A traceback with an innermost frame under ``scio/``: ``HARNESS_SUSPECT``.
    9. ``halt_reason == "feature_failed"`` whose last ledger line names an exception
       (``... failed: <Type>: ...``): ``HARNESS_SUSPECT``. ``BudgetExceeded`` and
       ``RefusalError`` (``NOT_A_DEFECT``) are ``TASK_FAULT``: a spend cap and a model's
       refusal are not SCIO defects.
    9b. SCIO's ``scio <command> stopped on a harness error: <Type>: ...`` line (outcome
        ``exit_reason`` ``harness``, I-182): ``HARNESS_SUSPECT``, and ``NOT_A_DEFECT`` types
        ``TASK_FAULT``, as in row 9. A timed-out ``claude -p`` was row 6 already.
    10. Anything else: ``UNKNOWN``.

    ``signature`` is "" for ``DONE`` and ``HUMAN_STOP``.

    How the rows read the evidence:
    - "stderr" in rows 5 and 6 is the part that ended the run (:func:`failure_text`), so a
      rate limit the run rode out earlier does not hide a crash after it.
    - Row 6 also takes a GitHub stop the outcome file records (``exit_reason`` ``github``).
    - Row 9 falls back to the summary's ``failed exit=error`` feature line when the last
      ledger line names neither an exception nor a human stop.
    - The signature of an external class is its marker, so repeated sign-in failures or
      rate limits gather in one incident and its replay cap holds.
    """
    frame = innermost_scio_frame(ev.frames)
    has_traceback = bool(ev.frames or ev.exception_type)
    # rows 1-4: the run's own end state
    if ev.orphaned and not has_traceback:
        return _result(
            FailureClass.ORPHAN,
            "the run's process is gone and no exit was recorded",
            frame,
            message="orphaned",
        )
    if ev.hung:
        return _result(
            FailureClass.HANG,
            "no progress within the hang limit; the process tree was killed",
            frame,
            message="no progress",
        )
    if ev.exit_code == 0:
        if ev.paused:
            return Classification(FailureClass.HUMAN_STOP, "", "the build paused for a human", "")
        return Classification(FailureClass.DONE, "", "the run finished", "")
    # row 4a: a person pressed Ctrl+C; no incident and no automation
    if _short_type(ev.exception_type) == "KeyboardInterrupt":
        return Classification(FailureClass.HUMAN_STOP, "", "a person interrupted the run", "")
    # row 4b: a human's stop at the feature decision, before any marker row: its free text
    # ("... failed (rate limit handling wrong); build stopped") must never read as a cause
    ledger = ev.last_ledger_line.strip()
    if ev.halt_reason == "feature_failed" and FEATURE_STOP.fullmatch(ledger):
        return _task_fault(ev, frame)
    # rows 5-6: causes outside SCIO, even inside a traceback with scio frames
    text = failure_text(ev)
    marker, line = _first_marker(text, AUTH_MARKERS)
    if marker:
        return _result(FailureClass.EXTERNAL_AUTH, f"sign in again: {line}", frame, message=marker)
    marker, line = _first_marker(text, TRANSIENT_MARKERS)
    if marker or ev.halt_reason == GITHUB_HALT:
        what = line or f"scio build stopped on GitHub: {_first_line(ev.last_ledger_line)}"
        return _result(FailureClass.EXTERNAL_TRANSIENT, what, frame, message=marker or GITHUB_HALT)
    # row 7: the product's or a human's business
    if ev.halt_reason in TASK_FAULT_REASONS:
        return _task_fault(ev, frame)
    # row 8: a traceback through SCIO
    if frame:
        what = f"{ev.exception_type}: {_first_line(ev.exception_message)}".strip(": ")
        return _result(
            FailureClass.HARNESS_SUSPECT,
            what or frame,
            frame,
            exception_type=ev.exception_type,
            sig_frame=frame,
            message=ev.exception_message,
        )
    # row 9: a feature attempt a HarnessError ended (a human's stop was row 4b)
    if ev.halt_reason == "feature_failed":
        if (m := FEATURE_ERROR.fullmatch(ledger)) and _short_type(m["type"]) in NOT_A_DEFECT:
            return _task_fault(ev, frame)
        if m:
            return _result(
                FailureClass.HARNESS_SUSPECT,
                f"{m['feature']} attempt {m['attempt']} failed: {m['type']}: "
                f"{_first_line(m['message'])}",
                frame,
                exception_type=m["type"],
                message=m["message"],
            )
        if m := FEATURE_ERROR_RUN.search(ev.stdout_tail):
            return _result(
                FailureClass.HARNESS_SUSPECT,
                f"{m['feature']} attempt {m['attempt']} failed with a harness error",
                frame,
                message="feature attempt failed exit=error",
            )
    # row 9b: a HarnessError no node caught, which SCIO's CLI stopped on (I-182): read as
    # row 9 reads a feature's, since before I-182 it was a traceback through scio (row 8)
    if m := HARNESS_STOP.search(text):
        if _short_type(m["type"]) in NOT_A_DEFECT:
            return _task_fault(ev, frame)
        return _result(
            FailureClass.HARNESS_SUSPECT,
            f"scio {m['command']} stopped on {m['type']}: {_first_line(m['message'])}",
            frame,
            exception_type=m["type"],
            message=m["message"],
        )
    # row 10
    detail = (
        _first_line(ev.exception_message)
        or _first_line(ev.last_ledger_line)
        or _last_line(ev.stderr_tail)
    )
    what = f"exit {ev.exit_code}" + (f" ({ev.halt_reason})" if ev.halt_reason else "")
    return _result(
        FailureClass.UNKNOWN,
        f"{what}: {detail}" if detail else what,
        frame,
        exception_type=ev.exception_type,
        sig_frame=frame,
        message=detail or what,
    )


def _short_type(exception_type: str) -> str:
    """``scio.harness.failures.BudgetExceeded`` -> ``BudgetExceeded``."""
    return exception_type.strip().rsplit(".", 1)[-1]


def _task_fault(ev: Evidence, frame: str) -> Classification:
    first = _first_line(ev.last_ledger_line)
    return _result(
        FailureClass.TASK_FAULT,
        f"{ev.halt_reason}: {first}" if first else str(ev.halt_reason),
        frame,
        message=f"{ev.halt_reason}: {ev.last_ledger_line}",
    )


def _last_line(text: str) -> str:
    return next((ln for ln in reversed(text.splitlines()) if ln.strip()), "")
