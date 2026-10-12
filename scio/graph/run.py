"""Run and resume the SCIO graph with durable checkpoints."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from scio.graph.build import TierName, compile_graph
from scio.graph.checkpointer import DURABILITY, open_checkpointer
from scio.graph.run_context import RunContext
from scio.state.models import GitHubRefs, SCIOState, TaskSpec


@dataclass
class RunOutcome:
    state: SCIOState
    interrupt: dict[str, Any] | None
    """The interrupt payload when the run paused for a human, else None."""

    @property
    def paused(self) -> bool:
        return self.interrupt is not None


def initial_state(ctx: RunContext, task: TaskSpec) -> SCIOState:
    owner, repo = "local", "repo"
    if ctx.settings.gh_repo and "/" in ctx.settings.gh_repo:
        owner, repo = ctx.settings.gh_repo.split("/", 1)
    return SCIOState(
        thread_id=ctx.thread_id,
        task=task,
        budgets=ctx.budget,
        github=GitHubRefs(owner=owner, repo=repo, issue_number=task.issue_number),
    )


def _outcome(raw: dict[str, Any]) -> RunOutcome:
    interrupts = raw.pop("__interrupt__", None)
    payload = None
    if interrupts:
        first = interrupts[0]
        payload = first.value if hasattr(first, "value") else dict(first)
    return RunOutcome(state=SCIOState.model_validate(raw), interrupt=payload)


@contextmanager
def _saver(ctx: RunContext, saver: BaseCheckpointSaver | None) -> Iterator[BaseCheckpointSaver]:
    """An explicit saver (the product graph shares its own with each feature subgraph, ADR
    0011) or one opened from the settings for the duration of the call."""
    if saver is not None:
        yield saver
        return
    with open_checkpointer(ctx.settings.checkpoint_dsn) as opened:
        yield opened


def run_graph(
    ctx: RunContext,
    task: TaskSpec,
    *,
    tier: TierName | None = None,
    saver: BaseCheckpointSaver | None = None,
) -> RunOutcome:
    """Start a run. ``tier`` restricts the graph (tests); None lets the router choose."""
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with _saver(ctx, saver) as cp:
        graph = compile_graph(ctx, cp, tier)
        raw = graph.invoke(initial_state(ctx, task), config=config, durability=DURABILITY)
    return _outcome(raw)


def resume_graph(
    ctx: RunContext,
    decision: str | None = None,
    *,
    tier: TierName | None = None,
    saver: BaseCheckpointSaver | None = None,
) -> RunOutcome:
    """Continue a paused or crashed run. ``decision`` answers a pending interrupt."""
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with _saver(ctx, saver) as cp:
        graph = compile_graph(ctx, cp, tier)
        inp: Any = Command(resume=decision) if decision is not None else None
        raw = graph.invoke(inp, config=config, durability=DURABILITY)
    return _outcome(raw)


def thread_started(ctx: RunContext, saver: BaseCheckpointSaver | None = None) -> bool:
    """True when ``ctx.thread_id`` has a checkpoint: a resume, not a fresh start."""
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with _saver(ctx, saver) as cp:
        return bool(compile_graph(ctx, cp).get_state(config).values)


# Backwards-compatible names (M3 callers and tests).
def run_t0(ctx: RunContext, task: TaskSpec) -> RunOutcome:
    return run_graph(ctx, task, tier="T0")


def resume_t0(ctx: RunContext, decision: str | None = None) -> RunOutcome:
    return resume_graph(ctx, decision, tier="T0")
