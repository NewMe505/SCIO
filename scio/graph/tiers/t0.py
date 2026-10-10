"""T0 diagram and the M3-compatible ``build_t0`` / ``compile_t0`` helpers.

The T0 path (intake → route → prepare → (t0_loop → verify)* → git_checkpoint → document →
gate → pr_ready → merge_wait, with ``halt``) is the ``tier="T0"`` restriction of the
single builder in :mod:`scio.graph.build`.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph

from scio.graph.build import build_graph
from scio.graph.registry import register
from scio.graph.run_context import RunContext


def build_t0(ctx: RunContext | None) -> StateGraph:
    return build_graph(ctx, "T0")


def compile_t0(ctx: RunContext, checkpointer: BaseCheckpointSaver | None) -> Any:
    return build_t0(ctx).compile(checkpointer=checkpointer)


@register("t0")
def t0_diagram() -> Any:
    return build_t0(None).compile()
