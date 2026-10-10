"""Tier graphs. Importing this package registers their diagrams in ``scio.graph.registry``."""

from scio.graph.tiers import t1, t2  # noqa: F401 - registers the t1 and t2 diagrams
from scio.graph.tiers.t0 import build_t0, compile_t0

__all__ = ["build_t0", "compile_t0"]
