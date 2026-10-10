"""T2 diagram: scope → parallel explorers → plan → implement/verify/triage loop → review."""

from __future__ import annotations

from typing import Any

from scio.graph.build import build_graph
from scio.graph.registry import register


@register("t2")
def t2_diagram() -> Any:
    return build_graph(None, "T2").compile()
