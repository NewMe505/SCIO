"""Shared by the dashboard's API and page tests (issue #68): the example payloads in
``tests/fixtures/dashboard/`` and a :class:`~scio.dashboard.contract.DataSource` that serves
them, so neither test needs a checkpoint DB."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from scio.dashboard.contract import (
    GraphDiagram,
    ThreadDetail,
    ThreadSummary,
    ThreadVersion,
    UnknownRoot,
    UnknownThread,
    Workspace,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dashboard"
DETAIL_FILES = ("thread_build.json", "thread_feature.json", "thread_bootstrap.json")


def load_fixture(name: str) -> Any:
    """The parsed JSON of one fixture file (``threads.json``, ``thread_build.json`` ...)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureDataSource:
    """A DataSource over the fixture payloads. A thread listed in ``threads.json`` without a
    detail fixture gets a detail with its summary only. :meth:`bump` changes a thread's
    version the way a new checkpoint would. The versions are guarded by a lock: the server
    reads them from a worker thread while the test bumps them."""

    def __init__(self) -> None:
        self._roots = [Workspace.model_validate(w) for w in load_fixture("roots.json")]
        self._threads = [ThreadSummary.model_validate(t) for t in load_fixture("threads.json")]
        self._details = {
            (d.summary.root, d.summary.thread_id): d
            for d in (ThreadDetail.model_validate(load_fixture(f)) for f in DETAIL_FILES)
        }
        self._graphs = [GraphDiagram.model_validate(g) for g in load_fixture("graphs.json")]
        self._versions = {
            (t.root, t.thread_id): ThreadVersion(checkpoint_id=t.checkpoint_id)
            for t in self._threads
        }
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _check_root(self, root: str) -> None:
        if root not in {w.name for w in self._roots}:
            raise UnknownRoot(root)

    def roots(self) -> list[Workspace]:
        self.calls.append("roots")
        return list(self._roots)

    def list_threads(self, root: str) -> list[ThreadSummary]:
        self.calls.append(f"list_threads:{root}")
        self._check_root(root)
        return [t for t in self._threads if t.root == root]

    def thread(self, root: str, thread_id: str) -> ThreadDetail:
        self.calls.append(f"thread:{root}/{thread_id}")
        self._check_root(root)
        if (root, thread_id) in self._details:
            return self._details[(root, thread_id)]
        key = (root, thread_id)
        summary = next((t for t in self._threads if (t.root, t.thread_id) == key), None)
        if summary is None:
            raise UnknownThread(thread_id)
        return ThreadDetail(summary=summary)

    def graphs(self) -> list[GraphDiagram]:
        self.calls.append("graphs")
        return list(self._graphs)

    def versions(self, root: str) -> dict[str, ThreadVersion]:
        self.calls.append(f"versions:{root}")
        self._check_root(root)
        with self._lock:
            return {tid: v for (r, tid), v in self._versions.items() if r == root}

    def bump(
        self, root: str, thread_id: str, *, checkpoint: bool = True, ledger_bytes: int = 100
    ) -> None:
        """A new checkpoint for ``thread_id`` (``checkpoint=False``: only a pending write, the
        way a stop arrives) and ledger growth; adds the thread if new."""
        with self._lock:
            old = self._versions.get((root, thread_id)) or ThreadVersion(checkpoint_id="new")
            self._versions[(root, thread_id)] = ThreadVersion(
                checkpoint_id=old.checkpoint_id + ("+" if checkpoint else ""),
                writes=old.writes + 1,
                ledger_bytes=old.ledger_bytes + ledger_bytes,
            )

    def set_summary(self, root: str, thread_id: str, **fields: Any) -> None:
        """Change a listed thread's summary (its list row and its detail's summary) and bump
        its version, the way a new checkpoint would."""
        for i, t in enumerate(self._threads):
            if (t.root, t.thread_id) == (root, thread_id):
                self._threads[i] = ThreadSummary.model_validate({**t.model_dump(), **fields})
        key = (root, thread_id)
        if key in self._details:
            detail = self._details[key]
            summary = {**detail.summary.model_dump(), **fields}
            self._details[key] = ThreadDetail.model_validate(
                {**detail.model_dump(), "summary": summary}
            )
        self.bump(root, thread_id)

    def drop(self, root: str, thread_id: str) -> None:
        """The thread disappears (its checkpoints were deleted)."""
        with self._lock:
            self._versions.pop((root, thread_id), None)
