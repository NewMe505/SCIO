"""Toolchain profiles (ADR 0009): the stack-specific commands and parsers behind the graph.

``get_toolchain(name)`` returns the profile for a stack; ``python`` is the only one until
M10. The graph never names ``pytest`` or ``ruff`` itself (``tests/unit/test_toolchain.py``
enforces that boundary).
"""

from __future__ import annotations

from functools import cache

from scio.toolchain.base import CheckOutcome, QualityCheck, Runtime, TestOutcome, Toolchain
from scio.toolchain.python import PythonToolchain, load_python_profile

__all__ = [
    "CheckOutcome",
    "PythonToolchain",
    "QualityCheck",
    "Runtime",
    "TestOutcome",
    "Toolchain",
    "get_toolchain",
    "python_toolchain",
]


@cache
def python_toolchain() -> PythonToolchain:
    return load_python_profile()


def get_toolchain(name: str) -> Toolchain:
    """The profile registered under ``name``; raises ``KeyError`` for an unknown stack."""
    if name == "python":
        return python_toolchain()
    raise KeyError(f"unknown toolchain profile {name!r}; known: python")
