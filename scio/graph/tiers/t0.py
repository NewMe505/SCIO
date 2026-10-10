"""T0: intake → route → prepare → (t0_loop → verify)* → checkpoint → document → gate →
pr_ready → merge_wait → END, with ``halt`` for every non-success exit."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from scio.graph.nodes import checkpoint_gate as cg
from scio.graph.nodes import intake_route as ir
from scio.graph.nodes import prepare as pr
from scio.graph.nodes import verify as vf
from scio.graph.nodes import worker as wk
from scio.graph.registry import register
from scio.graph.run_context import RunContext
from scio.state.models import SCIOState


def _unbound(name: str) -> Any:
    def node(_state: SCIOState) -> dict[str, Any]:
        raise RuntimeError(f"node {name!r} compiled without a RunContext; use compile_t0(ctx, ...)")

    return node


def build_t0(ctx: RunContext | None) -> StateGraph:
    g = StateGraph(SCIOState)

    def n(name: str, factory: Any) -> None:
        g.add_node(name, factory(ctx) if ctx is not None else _unbound(name))

    n("intake", ir.intake)
    n("route", ir.route)
    n("prepare_repo", pr.prepare_repo)
    n("t0_loop", wk.t0_loop)
    n("verify", vf.verify)
    n("git_checkpoint", cg.checkpoint)
    n("document", wk.document)
    n("gate", cg.gate)
    n("pr_ready", cg.pr_ready)
    n("merge_wait", cg.merge_wait)
    n("halt", cg.halt)

    g.add_edge(START, "intake")
    g.add_edge("intake", "route")
    g.add_conditional_edges("route", ir.after_route, {"prepare": "prepare_repo", "halt": "halt"})
    g.add_edge("prepare_repo", "t0_loop")
    g.add_conditional_edges("t0_loop", wk.after_implement, {"verify": "verify", "halt": "halt"})
    g.add_conditional_edges(
        "verify",
        vf.after_verify,
        {"checkpoint": "git_checkpoint", "t0_loop": "t0_loop", "halt": "halt"},
    )
    g.add_edge("git_checkpoint", "document")
    g.add_conditional_edges("document", wk.after_document, {"gate": "gate", "halt": "halt"})
    g.add_conditional_edges("gate", cg.after_gate, {"pr_ready": "pr_ready", "halt": "halt"})
    g.add_edge("pr_ready", "merge_wait")
    g.add_edge("merge_wait", END)
    g.add_edge("halt", END)
    return g


def compile_t0(ctx: RunContext, checkpointer: BaseCheckpointSaver | None) -> Any:
    return build_t0(ctx).compile(checkpointer=checkpointer)


@register("t0")
def t0_diagram() -> Any:
    return build_t0(None).compile()
