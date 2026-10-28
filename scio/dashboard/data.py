"""The dashboard's data layer (issue #68, ADR 0017): :class:`CheckpointDataSource` reads each
workspace's checkpoint DB, cost ledgers and the products' ``progress.json``, read-only by
construction. Wave 0 stub: the signatures are fixed, the bodies arrive in wave 1."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from scio.dashboard.contract import (
    GraphDiagram,
    ThreadDetail,
    ThreadSummary,
    ThreadVersion,
    Workspace,
)


def workspace(
    path: Path,
    *,
    name: str | None = None,
    checkpoints: Path | None = None,
    ledger_dir: Path | None = None,
) -> Workspace:
    """A root at ``path``; the DB defaults to ``path/.scio/checkpoints.sqlite`` and the
    ledgers to ``path/.scio/ledger``, the name to the directory's name."""
    raise NotImplementedError


class CheckpointDataSource:
    """:class:`scio.dashboard.contract.DataSource` over SQLite checkpoint DBs."""

    def __init__(self, workspaces: Sequence[Workspace]) -> None:
        raise NotImplementedError

    @classmethod
    def from_roots(cls, roots: Sequence[Path]) -> CheckpointDataSource:
        """One workspace per directory, at the default locations, names made unique."""
        raise NotImplementedError

    def roots(self) -> list[Workspace]:
        raise NotImplementedError

    def list_threads(self, root: str) -> list[ThreadSummary]:
        raise NotImplementedError

    def thread(self, root: str, thread_id: str) -> ThreadDetail:
        raise NotImplementedError

    def graphs(self) -> list[GraphDiagram]:
        raise NotImplementedError

    def versions(self, root: str) -> dict[str, ThreadVersion]:
        raise NotImplementedError
