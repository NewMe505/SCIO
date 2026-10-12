"""The SCIO graph: one builder for every tier.

``build_graph(ctx, tier=None)`` wires the whole graph: after ``route`` the run follows the
chosen tier. ``tier="T0"|"T1"|"T2"`` restricts the build to that tier's nodes (used for
the registered diagrams and for tests). Shared nodes are added once.

    T0: prepare → t0_loop ⇄ verify → git_checkpoint → document → gate → pr_ready → merge_wait
    T1: prepare → plan → post_plan → localize → implement → verify → {checkpoint | triage}
    T2: prepare → scope → explore xN → plan → post_plan → implement → verify → {...}
    triage → debug → verify | advise → {implement | plan | halt}
    checkpoint → {implement (next step) | review → document}
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from scio.graph.nodes import checkpoint_gate as cg
from scio.graph.nodes import implement as im
from scio.graph.nodes import intake_route as ir
from scio.graph.nodes import planning as pl
from scio.graph.nodes import prepare as pr
from scio.graph.nodes import verify as vf
from scio.graph.nodes import worker as wk
from scio.graph.run_context import RunContext
from scio.state.models import SCIOState, Tier

TierName = Literal["T0", "T1", "T2"]


def _unbound(name: str) -> Any:
    def node(_state: Any) -> dict[str, Any]:
        raise RuntimeError(
            f"node {name!r} compiled without a RunContext; use compile_graph(ctx, ...)"
        )

    return node


def after_route_factory(tiers: set[str]) -> Any:
    def after_route(state: SCIOState) -> str:
        if state.exit_reason is not None or state.routing is None:
            return "halt"
        return state.routing.tier.value if state.routing.tier.value in tiers else "halt"

    return after_route


def after_prepare_factory(tiers: set[str]) -> Any:
    def after_prepare(state: SCIOState) -> str:
        tier = state.routing.tier if state.routing else Tier.T0
        if tier is Tier.T0 and "T0" in tiers:
            return "t0_loop"
        if tier is Tier.T2 and "T2" in tiers:
            return "scope"
        return "plan"

    return after_prepare


def build_graph(ctx: RunContext | None, tier: TierName | None = None) -> StateGraph:
    tiers: set[str] = {tier} if tier else {"T0", "T1", "T2"}
    g = StateGraph(SCIOState)

    def n(name: str, factory: Any) -> None:
        g.add_node(name, factory(ctx) if ctx is not None else _unbound(name))

    # shared
    n("intake", ir.intake)
    n("route", ir.route)
    n("prepare_repo", pr.prepare_repo)
    n("verify", vf.verify)
    n("git_checkpoint", cg.checkpoint)
    n("document", wk.document)
    n("gate", cg.gate)
    n("pr_ready", cg.pr_ready)
    n("merge_wait", cg.merge_wait)
    n("halt", cg.halt)
    if "T0" in tiers:
        n("t0_loop", wk.t0_loop)
    planned = bool(tiers & {"T1", "T2"})
    if planned:
        n("plan", pl.plan)
        n("post_plan", pl.post_plan)
        n("implement", im.implement)
        n("debug", im.debug)
        n("advise", im.advise)
        n("review", im.review)
    if "T1" in tiers:
        n("localize", pl.localize)
    if "T2" in tiers:
        n("scope", pl.scope)
        n("explore", pl.explore)

    g.add_edge(START, "intake")
    g.add_edge("intake", "route")
    route_map: dict[Hashable, str] = {"halt": "halt"}
    for t in tiers:
        route_map[t] = "prepare_repo"
    g.add_conditional_edges("route", after_route_factory(tiers), route_map)
    prepare_map: dict[Hashable, str] = {}
    if "T0" in tiers:
        prepare_map["t0_loop"] = "t0_loop"
    if "T2" in tiers:
        prepare_map["scope"] = "scope"
    if planned:
        prepare_map["plan"] = "plan"
    g.add_conditional_edges("prepare_repo", after_prepare_factory(tiers), prepare_map)

    if "T0" in tiers:
        g.add_conditional_edges("t0_loop", wk.after_implement, {"verify": "verify", "halt": "halt"})
    if "T2" in tiers:
        g.add_conditional_edges("scope", pl.fan_out_explore, ["explore", "halt"])
        g.add_edge("explore", "plan")
    if planned:
        g.add_conditional_edges("plan", pl.after_plan, {"post_plan": "post_plan", "halt": "halt"})
        if "T1" in tiers:
            g.add_conditional_edges(
                "post_plan",
                lambda s: (
                    "localize"
                    if s.routing and s.routing.tier is Tier.T1 and not s.explorer_summaries
                    else "implement"
                ),
                {"localize": "localize", "implement": "implement"},
            )
            g.add_conditional_edges(
                "localize", im.after_worker, {"verify": "implement", "halt": "halt"}
            )
        else:
            g.add_edge("post_plan", "implement")
        g.add_conditional_edges("implement", im.after_worker, {"verify": "verify", "halt": "halt"})
        g.add_conditional_edges("debug", im.after_worker, {"verify": "verify", "halt": "halt"})
        g.add_conditional_edges(
            "advise",
            im.after_advise,
            {
                "implement": "implement",
                "plan": "plan",
                "checkpoint": "git_checkpoint",  # ADR 0008: approved high-impact review
                "halt": "halt",
            },
        )
        g.add_conditional_edges("review", im.after_review, {"document": "document", "halt": "halt"})

    verify_map: dict[Hashable, str] = {"checkpoint": "git_checkpoint", "halt": "halt"}
    if "T0" in tiers:
        verify_map["t0_loop"] = "t0_loop"
    if planned:
        verify_map["triage"] = "triage"
        g.add_node(
            "triage", lambda s: {}
        )  # a named no-op so the decision is visible in the diagram
        g.add_conditional_edges(
            "triage", im.triage, {"debug": "debug", "advise": "advise", "halt": "halt"}
        )
    g.add_conditional_edges("verify", vf.after_verify, verify_map)

    checkpoint_map: dict[Hashable, str] = {"document": "document"}
    if planned:
        checkpoint_map["implement"] = "implement"
        checkpoint_map["review"] = "review"
    g.add_conditional_edges("git_checkpoint", cg.after_checkpoint, checkpoint_map)
    g.add_conditional_edges("document", wk.after_document, {"gate": "gate", "halt": "halt"})
    gate_map: dict[Hashable, str] = {"pr_ready": "pr_ready", "halt": "halt"}
    if planned:
        gate_map["plan"] = "plan"  # one gate replan per run; T0 has no plan and halts
    g.add_conditional_edges("gate", cg.after_gate, gate_map)
    g.add_edge("pr_ready", "merge_wait")
    g.add_edge("merge_wait", END)
    g.add_edge("halt", END)
    return g


def compile_graph(
    ctx: RunContext, checkpointer: BaseCheckpointSaver | None, tier: TierName | None = None
) -> Any:
    return build_graph(ctx, tier).compile(checkpointer=checkpointer)
