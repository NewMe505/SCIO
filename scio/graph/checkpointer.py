"""Checkpointer selection from ``SCIO_CHECKPOINT_DSN``.

* ``memory://`` : in-process, for tests.
* ``sqlite:///path.sqlite`` : local development (one file, no server).
* ``postgresql://...`` : production (``langgraph-checkpoint-postgres``, the ``postgres`` extra).

Every graph run passes ``durability="sync"`` so the checkpoint is written before the
next node starts; a crash therefore resumes at the node that was running, and that
node's side effects are made idempotent by :mod:`scio.graph.effects`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

DURABILITY = "sync"


@contextmanager
def open_checkpointer(dsn: str) -> Iterator[BaseCheckpointSaver]:
    """Yield a ready checkpointer for ``dsn``; closes connections on exit."""
    if dsn.startswith("memory://"):
        yield InMemorySaver()
        return
    if dsn.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = Path(dsn.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield saver
        return
    if dsn.startswith(("postgresql://", "postgres://")):
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield saver
        return
    raise ValueError(f"unsupported checkpoint DSN: {dsn!r}")
