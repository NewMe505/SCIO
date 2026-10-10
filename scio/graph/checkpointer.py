"""Checkpointer selection from ``SCIO_CHECKPOINT_DSN``.

* ``memory://`` : in-process, for tests.
* ``sqlite:///path.sqlite`` : local development (one file, no server).
* ``postgresql://...`` : production (``langgraph-checkpoint-postgres``, the ``postgres`` extra).

Every graph run passes ``durability="sync"`` so the checkpoint is written before the
next node starts; a crash therefore resumes at the node that was running, and that
node's side effects are made idempotent by :mod:`scio.graph.effects`.
"""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from scio.state import models as state_models

DURABILITY = "sync"


def state_types() -> list[tuple[str, str]]:
    """Every Pydantic model and enum in ``scio.state.models``, for the serializer allow-list."""
    out: list[tuple[str, str]] = []
    for name, obj in inspect.getmembers(state_models, inspect.isclass):
        if obj.__module__ == state_models.__name__ and issubclass(obj, BaseModel | Enum):
            out.append((state_models.__name__, name))
    return out


def serializer() -> JsonPlusSerializer:
    """LangGraph serializer that accepts SCIO's state types without the deprecation warning."""
    return JsonPlusSerializer(allowed_msgpack_modules=state_types())


@contextmanager
def open_checkpointer(dsn: str) -> Iterator[BaseCheckpointSaver]:
    """Yield a ready checkpointer for ``dsn``; closes connections on exit."""
    if dsn.startswith("memory://"):
        yield InMemorySaver(serde=serializer())
        return
    if dsn.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = Path(dsn.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            yield SqliteSaver(conn, serde=serializer())
        finally:
            conn.close()
        return
    if dsn.startswith(("postgresql://", "postgres://")):
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield saver
        return
    raise ValueError(f"unsupported checkpoint DSN: {dsn!r}")
