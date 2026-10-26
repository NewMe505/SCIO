"""Toolchain profiles (ADR 0009, ADR 0014): the stack-specific commands and parsers behind
the graph.

``get_toolchain(name)`` returns the profile for a stack (``python``, ``node-ts``);
``detect_toolchain(root)`` names the one whose marker files a repository has. The graph never
names ``pytest`` or ``ruff`` itself (``tests/unit/test_toolchain.py`` enforces that boundary).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path

from scio.toolchain.base import (
    CheckOutcome,
    Detect,
    QualityCheck,
    Runtime,
    TestOutcome,
    Toolchain,
)
from scio.toolchain.node_ts import load_node_ts_profile
from scio.toolchain.profile import ProfileToolchain, StackParsers
from scio.toolchain.python import PythonToolchain, load_python_profile

__all__ = [
    "CheckOutcome",
    "Detect",
    "ProfileToolchain",
    "PythonToolchain",
    "QualityCheck",
    "Runtime",
    "StackParsers",
    "TestOutcome",
    "Toolchain",
    "available_toolchains",
    "detect_toolchain",
    "get_toolchain",
    "node_ts_toolchain",
    "python_toolchain",
]


@cache
def python_toolchain() -> ProfileToolchain:
    return load_python_profile()


@cache
def node_ts_toolchain() -> ProfileToolchain:
    return load_node_ts_profile()


_REGISTRY: dict[str, Callable[[], ProfileToolchain]] = {
    "python": python_toolchain,
    "node-ts": node_ts_toolchain,
}


def available_toolchains() -> tuple[str, ...]:
    """The profile names, in registry order (``python`` first)."""
    return tuple(_REGISTRY)


def get_toolchain(name: str) -> Toolchain:
    """The profile registered under ``name``; raises ``KeyError`` for an unknown stack."""
    try:
        return _REGISTRY[name]()
    except KeyError:
        known = ", ".join(_REGISTRY)
        raise KeyError(f"unknown toolchain profile {name!r}; known: {known}") from None


def detect_toolchain(root: Path | str) -> str | None:
    """The profile whose marker files ``root`` holds, or None. A profile that needs every
    marker (``package.json`` and ``tsconfig.json``) is checked before one that needs any, so
    a TypeScript repository with a stray ``requirements.txt`` is still ``node-ts``."""
    base = Path(root)
    profiles = [(name, loader().detect) for name, loader in _REGISTRY.items()]
    ordered = sorted(
        (p for p in profiles if p[1] is not None),
        key=lambda p: (not p[1].all_of, -len(p[1].markers)),  # type: ignore[union-attr]
    )
    for name, detect in ordered:
        assert detect is not None
        present = [(base / m).exists() for m in detect.markers]
        if all(present) if detect.all_of else any(present):
            return name
    return None
