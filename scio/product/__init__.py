"""Product mode (ADR 0007, ADR 0010): the product orchestrator above the task graph.

M7 ships the greenfield path behind ``scio new``: interview, normalised ``PRODUCT.md``, a
human approval stop, then an idempotent bootstrap of a repository with CI and a walking
skeleton whose smoke test passes under the toolchain profile.
"""

from __future__ import annotations

from scio.product.graph import ProductOutcome, resume_product, run_product
from scio.product.spec import normalise_spec, render_product_md

__all__ = [
    "ProductOutcome",
    "normalise_spec",
    "render_product_md",
    "resume_product",
    "run_product",
]
