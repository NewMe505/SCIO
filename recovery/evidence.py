"""Reading what a finished run left behind, and snapshotting the workspace before any
repair. OWNER: wave-1 agent B."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from recovery.classify import (
    FEATURE_ERROR,
    FEATURE_STOP,
    TRACEBACK_HEADER,
    traceback_block_start,
)
from recovery.commands import Runner, run_command
from recovery.config import WorkspaceConfig
from recovery.model import Evidence, Frame, RunRecord, RunStatus
from recovery.scrub import scrub

PAUSE_MARKER = "PAUSED for a human:"
GITHUB_STOP_MARKER = "scio build stopped on GitHub:"
RESUME_MARKER = "resume with: scio "
"""The hint printed after a pause's interrupt JSON: it survives a tail that cut the marker."""
REASON_PREFIX = "reason: "
PROGRESS_PATH = ".scio/product/progress.json"
"""``scio/product/progress.py``'s ``PROGRESS_PATH``, relative to the product."""

_HEADER = re.compile(
    r"^(?:build |product )?thread (?P<thread>\S+) phase=(?P<phase>\S+) exit=(?P<exit>\S+)",
    re.MULTILINE,
)
"""The first line ``scio build``, ``scio new`` and ``scio run`` print at the end of a run.
Feature lines also carry ``exit=``, so only this header gives the run's exit reason."""
_AFTER_REASON = ("release ", PAUSE_MARKER, RESUME_MARKER)
"""Lines the build summary prints after ``reason:``; they end a multi-line reason."""
_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>.+?)\s*$')
_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_][\w.<>]*)(?::\s?(?P<message>.*))?$")
_CHUNK = 1 << 20


def tail(path: Path, max_bytes: int = 20_000) -> str:
    """The last ``max_bytes`` of ``path`` as text, or "" when it is missing.

    Starts at a line boundary, decodes as UTF-8 with replacement, and is scrubbed of
    secrets. A partial first line is dropped; a window with no line break at all gives "".
    Line endings are normalised to ``\\n``.
    """
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            offset = max(0, size - max_bytes)
            if offset == 0:
                fh.seek(0)
                data = fh.read()
            else:
                fh.seek(offset - 1)
                data = fh.read()
                at_boundary, data = data[:1] == b"\n", data[1:]
                if not at_boundary:
                    cut = data.find(b"\n")
                    data = b"" if cut < 0 else data[cut + 1 :]
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return scrub(text)


def read_outcome(path: Path) -> dict[str, Any] | None:
    """SCIO's outcome file. None when it is missing or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_traceback(text: str) -> tuple[str, str, list[Frame]]:
    """Parse the last Python traceback in ``text`` into its exception type, message and
    frames (outermost first).

    Handles chained tracebacks by keeping the last one, and both Windows and POSIX paths.
    Returns ``("", "", [])`` when there is none.
    """
    exc_type, message, frames, _end = _parse_last_traceback(text.splitlines())
    return exc_type, message, frames


def _parse_last_traceback(lines: Sequence[str]) -> tuple[str, str, list[Frame], int]:
    """:func:`parse_traceback` plus the index just past the exception's message, so a
    caller can tell whether the traceback is the last thing in the text."""
    start = traceback_block_start(lines)
    if start < 0:
        return "", "", [], len(lines)
    frames: list[Frame] = []
    i = start + 1 if lines[start].strip() == TRACEBACK_HEADER else start
    while i < len(lines):
        line = lines[i]
        if m := _FRAME.match(line):
            frames.append(Frame(m["path"], int(m["line"]), m["function"]))
        elif line.strip() and not line[:1].isspace():
            break  # the exception line: the first one that is not indented
        i += 1
    if i >= len(lines):
        return "", "", frames, len(lines)
    first = lines[i]
    if m := _EXCEPTION.match(first):
        exc_type, body = m["type"], [m["message"] or ""]
    else:
        exc_type, body = "", [first.strip()]
    j = i + 1
    while j < len(lines) and lines[j].strip() and lines[j].strip() != TRACEBACK_HEADER:
        body.append(lines[j])
        j += 1
    return exc_type, "\n".join(body).strip(), frames, j


def _ends_the_text(lines: Sequence[str], end: int) -> bool:
    return all(not ln.strip() for ln in lines[end:])


def _scrubbed(value: Any) -> Any:
    """``value`` (decoded JSON) with every string scrubbed."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrubbed(v) for k, v in value.items()}
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _outcome_frames(raw: Any) -> list[Frame]:
    frames: list[Frame] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        frames.append(
            Frame(
                _text(item.get("path")),
                line if isinstance(line, int) else 0,
                _text(item.get("function")),
            )
        )
    return frames


def _reason_line(stdout: str) -> str:
    """The build summary's ``reason:`` text: the last such line, with the lines that
    continue it (a multi-line ledger entry) up to the next part of the summary."""
    lines = stdout.splitlines()
    idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith(REASON_PREFIX)), -1)
    if idx < 0:
        return ""
    body = [lines[idx][len(REASON_PREFIX) :]]
    for line in lines[idx + 1 :]:
        if line.startswith(_AFTER_REASON):
            break
        body.append(line)
    return "\n".join(body).strip()


def read_evidence(run: RunRecord, *, max_bytes: int = 20_000) -> Evidence:
    """Build an :class:`Evidence` from a finished run's files.

    Sources, in order: the outcome file first; then stdout's pause marker and the CLI's
    ``reason:`` line; then the stderr traceback.

    ``hung`` and ``orphaned`` come from the run's status.

    Details:
    - An outcome file decides on its own when SCIO wrote one; the text is the fallback
      for a broken SCIO that wrote none.
    - An orphaned run whose child still wrote its outcome file exited on its own, and is
      read from that file: ``orphaned`` is only set when there is none. Its exit code
      comes from the file, since the supervisor never saw it.
    - A stderr traceback counts only when it ended the run: the outcome says ``crashed``
      without an exception, or there is no outcome and stdout has no build summary (SCIO
      prints the summary only when nothing escaped), and the traceback is the last thing
      in stderr. A traceback logged on the way (a caught tool crash) is not the failure.
    - The pause shows by its marker, or by the ``resume with:`` hint when a long
      interrupt pushed the marker out of the tail.
    """
    stdout = tail(Path(run.stdout_path), max_bytes) if run.stdout_path else ""
    stderr = tail(Path(run.stderr_path), max_bytes) if run.stderr_path else ""
    raw = read_outcome(Path(run.outcome_path)) if run.outcome_path else None
    outcome: dict[str, Any] | None = _scrubbed(raw) if raw is not None else None
    exit_code = run.exit_code
    if exit_code is None and outcome is not None and isinstance(outcome.get("exit_code"), int):
        exit_code = outcome["exit_code"]
    ev = Evidence(
        exit_code=exit_code,
        stdout_tail=stdout,
        stderr_tail=stderr,
        outcome=outcome,
        hung=run.status == RunStatus.KILLED_HANG,
        orphaned=run.status == RunStatus.ORPHANED and outcome is None,
    )
    stderr_traceback = False
    if outcome is not None:
        status = outcome.get("status")
        ev.paused = status == "paused"
        ev.halt_reason = _text(outcome.get("exit_reason")) or None
        ev.last_ledger_line = _text(outcome.get("reason"))
        exc = outcome.get("exception")
        if isinstance(exc, dict):
            ev.exception_type = _text(exc.get("type"))
            ev.exception_message = _text(exc.get("message"))
            ev.frames = _outcome_frames(exc.get("frames"))
        else:
            stderr_traceback = status == "crashed"
    else:
        ev.paused = PAUSE_MARKER in stdout or any(
            line.startswith(RESUME_MARKER) for line in stdout.splitlines()
        )
        headers = list(_HEADER.finditer(stdout))
        if headers and headers[-1]["exit"] != "None":
            ev.halt_reason = headers[-1]["exit"]
        ev.last_ledger_line = _reason_line(stdout)
        if ev.halt_reason is None and (
            FEATURE_ERROR.fullmatch(ev.last_ledger_line)
            or FEATURE_STOP.fullmatch(ev.last_ledger_line)
        ):
            ev.halt_reason = "feature_failed"  # the tail cut the header; the line says it
        stderr_traceback = not headers and not ev.paused and not ev.last_ledger_line
    if stderr_traceback:
        lines = stderr.splitlines()
        exc_type, message, frames, end = _parse_last_traceback(lines)
        if (exc_type or frames) and _ends_the_text(lines, end):
            ev.exception_type, ev.exception_message, ev.frames = exc_type, message, frames
    return ev


def backup_sqlite(src: Path, dst: Path) -> bool:
    """Copy a live SQLite database with ``sqlite3.Connection.backup``, which is safe while
    another process writes it in WAL mode. False when ``src`` does not exist.

    The source is opened read-only (``mode=ro``). A read-write connection that closes
    last checkpoints the WAL into the main file and deletes it, which writes the original
    (measured, 2026-11-14). The read-only connection leaves the main file and any WAL
    byte-identical. On a database closed cleanly (no ``-wal`` beside it) it creates an
    empty ``-wal`` and the ``-shm`` index, as any reader does, and cannot remove them:
    no data changes, and :func:`fingerprints` reads an empty WAL as none. The copy is
    written beside ``dst`` and moved into place, so a reader never sees half of one."""
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    finally:
        source.close()
    os.replace(tmp, dst)
    return True


def _product_dir(run: RunRecord, cfg: WorkspaceConfig) -> Path | None:
    if not run.product_dir:
        return None
    product = Path(run.product_dir)
    return product if product.is_absolute() else cfg.workspace / product


GIT_QUERIES: tuple[tuple[str, ...], ...] = (
    ("git", "rev-parse", "HEAD"),
    ("git", "branch", "--show-current"),
    ("git", "--no-optional-locks", "status", "--porcelain"),
    (
        "git",
        "for-each-ref",
        "--format=%(refname:short) %(objectname)",
        "refs/heads/milestone",
        "refs/heads/agent",
    ),
)
"""What ``git.txt`` records about the product. ``--no-optional-locks`` keeps ``status``
from refreshing the product's index, which would write an original."""


def _git_report(product: Path, git: Runner) -> str:
    parts: list[str] = []
    for argv in GIT_QUERIES:
        res = git(list(argv), cwd=product, timeout_s=60)
        parts.append(f"$ {' '.join(argv)}  (exit {res.code})")
        parts.append(res.stdout.rstrip("\n"))
        if res.stderr.strip():
            parts.append(res.stderr.rstrip("\n"))
        parts.append("")
    return "\n".join(parts)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _copy_text(src: Path, dst: Path) -> bool:
    """A scrubbed copy of a text file; False when ``src`` is missing."""
    try:
        data = src.read_bytes()
    except OSError:
        return False
    _write_text(dst, scrub(data.decode("utf-8", errors="replace").replace("\r\n", "\n")))
    return True


def snapshot(
    run: RunRecord, cfg: WorkspaceConfig, dest: Path, *, git: Runner = run_command
) -> dict[str, str]:
    """Preserve the failed run's state under ``dest`` before any repair. The originals are
    never written.

    It copies:
    - backups of the workspace's ``.scio/checkpoints.sqlite`` and ``.scio/effects.sqlite``;
    - ``<product>/.scio/product/progress.json``;
    - ``git.txt``: the product's ``rev-parse HEAD``, ``branch --show-current``,
      ``status --porcelain`` and the heads of ``milestone/*`` and ``agent/*``;
    - the run's stdout, stderr and outcome files;
    - the tails of the thread's cost ledgers (``.scio/ledger/<thread>*.jsonl``).

    Returns a manifest that maps each kind to its path. The manifest is also written to
    ``dest/manifest.json``.

    Kinds: ``checkpoints``, ``effects``, ``progress``, ``git``, ``stdout``, ``stderr``,
    ``outcome``, ``ledger:<file name>`` for each ledger, and ``errors`` when a database
    could not be copied. A missing source has no entry. The text copies are scrubbed of
    secrets; the database copies are byte-faithful. Running it twice gives the same result.
    """
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    errors: list[str] = []
    scio_dir = cfg.workspace / ".scio"
    for kind in ("checkpoints", "effects"):
        target = dest / f"{kind}.sqlite"
        try:
            if backup_sqlite(scio_dir / f"{kind}.sqlite", target):
                manifest[kind] = str(target)
        except sqlite3.Error as exc:
            errors.append(f"{kind}.sqlite: {type(exc).__name__}: {exc}")
    product = _product_dir(run, cfg)
    if product is not None:
        if _copy_text(product / PROGRESS_PATH, dest / "progress.json"):
            manifest["progress"] = str(dest / "progress.json")
        if product.is_dir():
            _write_text(dest / "git.txt", _git_report(product, git))
            manifest["git"] = str(dest / "git.txt")
    for kind, source, name in (
        ("stdout", run.stdout_path, "stdout.txt"),
        ("stderr", run.stderr_path, "stderr.txt"),
        ("outcome", run.outcome_path, "outcome.json"),
    ):
        if source and _copy_text(Path(source), dest / name):
            manifest[kind] = str(dest / name)
    ledger_dir = scio_dir / "ledger"
    if run.build_thread and ledger_dir.is_dir():
        ledgers = sorted(
            p
            for p in ledger_dir.iterdir()
            if p.is_file() and p.name.startswith(run.build_thread) and p.suffix == ".jsonl"
        )
        for ledger in ledgers:
            target = dest / "ledger" / ledger.name
            _write_text(target, tail(ledger))
            manifest[f"ledger:{ledger.name}"] = str(target)
    if errors:
        _write_text(dest / "errors.txt", "\n".join(errors) + "\n")
        manifest["errors"] = str(dest / "errors.txt")
    _write_text(dest / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _file_digest(path: Path) -> str:
    """sha256 of a database and its ``-wal``: a committed write still in the WAL leaves
    the main file unchanged. An empty ``-wal`` counts as none (a reader may create one).
    ``(missing)`` when the database is absent."""
    if not path.is_file():
        return "(missing)"
    digest = hashlib.sha256()
    for label, part in (("db", path), ("wal", path.with_name(path.name + "-wal"))):
        try:
            with open(part, "rb") as fh:
                chunk = fh.read(_CHUNK)
                if not chunk and label == "wal":
                    continue
                digest.update(f"\0{label}\0".encode())
                while chunk:
                    digest.update(chunk)
                    chunk = fh.read(_CHUNK)
        except FileNotFoundError:
            continue
    return digest.hexdigest()


def _git_value(argv: Sequence[str], cwd: Path, git: Runner) -> str:
    if not cwd.is_dir():
        return "(missing)"
    res = git(list(argv), cwd=cwd, timeout_s=60)
    return res.stdout.strip() if res.ok else f"(git exit {res.code})"


def fingerprints(
    run: RunRecord, cfg: WorkspaceConfig, *, git: Runner = run_command
) -> dict[str, str]:
    """The state a repair session must not touch: live-wt's HEAD, the product's HEAD and
    branch, and the sha256 of the workspace's ``checkpoints.sqlite`` and ``effects.sqlite``.
    Compare a copy taken before the session with one taken after.

    Keys: ``live_wt_head``, ``product_head``, ``product_branch``, ``checkpoints_sha256``,
    ``effects_sha256``. A database's digest covers its ``-wal`` too. Something missing
    reads ``(missing)``, and a failed git command ``(git exit N)``, so both sides compare."""
    product = _product_dir(run, cfg)
    scio_dir = cfg.workspace / ".scio"
    no_product = "(missing)"
    return {
        "live_wt_head": _git_value(("git", "rev-parse", "HEAD"), cfg.live_wt, git),
        "product_head": (
            _git_value(("git", "rev-parse", "HEAD"), product, git) if product else no_product
        ),
        "product_branch": (
            _git_value(("git", "branch", "--show-current"), product, git) if product else no_product
        ),
        "checkpoints_sha256": _file_digest(scio_dir / "checkpoints.sqlite"),
        "effects_sha256": _file_digest(scio_dir / "effects.sqlite"),
    }


def manifest_paths(manifest: Mapping[str, str]) -> list[str]:
    """The manifest's paths in a stable order, for the repair brief: sorted by kind."""
    return [manifest[kind] for kind in sorted(manifest)]
