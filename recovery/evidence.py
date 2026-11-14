"""Reading what a finished run left behind, and snapshotting the workspace before any
repair. OWNER: wave-1 agent B."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from recovery.commands import Runner, run_command
from recovery.config import WorkspaceConfig
from recovery.model import Evidence, Frame, RunRecord

PAUSE_MARKER = "PAUSED for a human:"
GITHUB_STOP_MARKER = "scio build stopped on GitHub:"


def tail(path: Path, max_bytes: int = 20_000) -> str:
    """The last ``max_bytes`` of ``path`` as text, or "" when it is missing.

    Starts at a line boundary, decodes as UTF-8 with replacement, and is scrubbed of
    secrets.
    """
    raise NotImplementedError


def read_outcome(path: Path) -> dict[str, Any] | None:
    """SCIO's outcome file. None when it is missing or not a JSON object."""
    raise NotImplementedError


def parse_traceback(text: str) -> tuple[str, str, list[Frame]]:
    """Parse the last Python traceback in ``text`` into its exception type, message and
    frames (outermost first).

    Handles chained tracebacks by keeping the last one, and both Windows and POSIX paths.
    Returns ``("", "", [])`` when there is none.
    """
    raise NotImplementedError


def read_evidence(run: RunRecord, *, max_bytes: int = 20_000) -> Evidence:
    """Build an :class:`Evidence` from a finished run's files.

    Sources, in order: the outcome file first; then stdout's pause marker and the CLI's
    ``reason:`` line; then the stderr traceback.

    ``hung`` and ``orphaned`` come from the run's status.
    """
    raise NotImplementedError


def backup_sqlite(src: Path, dst: Path) -> bool:
    """Copy a live SQLite database with ``sqlite3.Connection.backup``, which is safe while
    another process writes it in WAL mode. False when ``src`` does not exist."""
    raise NotImplementedError


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
    """
    raise NotImplementedError


def fingerprints(
    run: RunRecord, cfg: WorkspaceConfig, *, git: Runner = run_command
) -> dict[str, str]:
    """The state a repair session must not touch: live-wt's HEAD, the product's HEAD and
    branch, and the sha256 of the workspace's ``checkpoints.sqlite`` and ``effects.sqlite``.
    Compare a copy taken before the session with one taken after."""
    raise NotImplementedError


def manifest_paths(manifest: Mapping[str, str]) -> list[str]:
    """The manifest's paths in a stable order, for the repair brief."""
    raise NotImplementedError
