"""Tier graphs. Importing this package registers their diagrams in ``scio.graph.registry``."""

from scio.graph.tiers import product, t1, t2  # noqa: F401 - registers the diagrams
from scio.graph.tiers.t0 import build_t0, compile_t0

__all__ = ["build_t0", "compile_t0"]
