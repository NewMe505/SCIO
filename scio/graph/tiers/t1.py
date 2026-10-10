"""T1 diagram: plan → localize → implement/verify/triage loop → review → document → gate."""

from __future__ import annotations

from typing import Any

from scio.graph.build import build_graph
from scio.graph.registry import register


@register("t1")
def t1_diagram() -> Any:
    return build_graph(None, "T1").compile()
