"""I-050: an eval task can pin its tier so the router never under-tiers it away from the
nodes the task exists to exercise."""

from __future__ import annotations

from pathlib import Path

from evals.runner import load_tasks
from scio.graph.nodes.intake_route import route
from scio.graph.run import initial_state
from scio.graph.run_context import RunContext
from scio.settings import Settings
from scio.state.models import TaskSpec, Tier


def _ctx(tmp_path: Path, pin: str | None) -> RunContext:
    return RunContext.create(
        thread_id="pin-1",
        sandbox_root=tmp_path / "work",
        tier="T1",
        settings=Settings(provider_override="fake"),
        work_dir=tmp_path / ".scio",
        tier_pin=pin,
    )


def test_pinned_route_skips_the_classifier(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "T1")
    task = TaskSpec(title="t", description="d", acceptance=["a"], repo_url="x", base_branch="main")
    out = route(ctx)(initial_state(ctx, task))
    routing = out["routing"]
    assert routing.tier is Tier.T1 and routing.pattern == "plan_execute_gate"
    assert routing.classifier_model == "pinned" and routing.confidence == 1.0
    assert out["phase"] == "prepare"
    assert ctx.harness.budget.snapshot().usd_used == 0  # no model call was made


def test_t008_is_pinned_to_t1() -> None:
    task = next(t for t in load_tasks() if t.id == "t008_snapshot_coupling")
    assert task.tier_pin == "T1" and task.tier_hint == "T1"
    # Every other task leaves the router free: the pin is for tasks built around one path.
    assert all(t.tier_pin is None for t in load_tasks() if t.id != "t008_snapshot_coupling")
