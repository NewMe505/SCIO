"""Registry of compiled graphs whose Mermaid diagrams are kept in sync with the docs.

M3 registers ``t0``; M4 adds ``t1`` and ``t2``. Each factory must compile without a
checkpointer and without credentials (the diagram is structural).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

GRAPHS: dict[str, Callable[[], Any]] = {}


def register(name: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def deco(factory: Callable[[], Any]) -> Callable[[], Any]:
        GRAPHS[name] = factory
        return factory

    return deco
