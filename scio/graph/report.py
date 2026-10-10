"""Per-run observability report: what happened, what it cost, where the time went.

Built from the durable checkpoint state and the persisted cost ledger, so it works after
the process that ran the task is gone (``scio report --thread <id>``).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scio.harness.accounting import CostEntry
from scio.state.models import SCIOState


class RoleCost(BaseModel):
    role: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    usd: float


class EscalationRow(BaseModel):
    id: str
    trigger: str
    step_id: str | None
    verdict: str | None
    outcome: str
    tokens: int
    usd: float


class TimelineRow(BaseModel):
    at: datetime
    source: str
    text: str


class RunReport(BaseModel):
    thread_id: str
    title: str
    tier: str | None
    phase: str
    exit_reason: str | None
    branch: str | None
    pr_number: int | None
    features: list[str]
    tokens_used: int
    usd_used: float
    tokens_max: int
    usd_max: float
    advisor_calls: str
    iterations: int
    tool_calls: int
    by_role: list[RoleCost] = Field(default_factory=list)
    escalations: list[EscalationRow] = Field(default_factory=list)
    timeline: list[TimelineRow] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def to_markdown(self) -> str:
        pr = f"#{self.pr_number}" if self.pr_number else "-"
        duration = f" · {self.duration_s:.0f}s" if self.duration_s is not None else ""
        head = [
            f"# Run {self.thread_id}: {self.title}",
            "",
            f"tier **{self.tier or '-'}** · phase **{self.phase}** · "
            f"exit **{self.exit_reason or '-'}** · branch `{self.branch or '-'}` · PR {pr}",
            f"tokens {self.tokens_used:,} / {self.tokens_max:,} · "
            f"${self.usd_used:.4f} / ${self.usd_max:.2f} · advisor {self.advisor_calls} · "
            f"iterations {self.iterations} · tool calls {self.tool_calls}{duration}",
            "",
            "## Features",
            *[f"- {f}" for f in self.features],
            "",
            "## Cost by role",
            "| role | calls | in | out | cache read | $ |",
            "|---|---:|---:|---:|---:|---:|",
            *[
                f"| {r.role} | {r.calls} | {r.input_tokens:,} | {r.output_tokens:,} | "
                f"{r.cache_read_tokens:,} | {r.usd:.4f} |"
                for r in self.by_role
            ],
            "",
            "## Escalations",
            "| id | trigger | step | verdict | outcome | tokens | $ |",
            "|---|---|---|---|---|---:|---:|",
            *(
                [
                    f"| {e.id} | {e.trigger} | {e.step_id or '-'} | {e.verdict or '-'} | "
                    f"{e.outcome} | {e.tokens:,} | {e.usd:.4f} |"
                    for e in self.escalations
                ]
                or ["| none | | | | | | |"]
            ),
            "",
            "## Timeline",
            *[f"- `{t.at.strftime('%H:%M:%S')}` **{t.source}** {t.text}" for t in self.timeline],
            "",
            "## Artifacts",
            *[f"- {k}: `{v}`" for k, v in self.artifacts.items()],
            "",
        ]
        return "\n".join(head)


def build_report(state: SCIOState, entries: list[CostEntry]) -> RunReport:
    by: dict[str, RoleCost] = {}
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in entries:
        a = agg[e.role]
        a["calls"] += 1
        a["in"] += e.usage.input_tokens
        a["out"] += e.usage.output_tokens
        a["cache"] += e.usage.cache_read_tokens
        a["usd"] += e.usd
    for role, a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
        by[role] = RoleCost(
            role=role,
            calls=int(a["calls"]),
            input_tokens=int(a["in"]),
            output_tokens=int(a["out"]),
            cache_read_tokens=int(a["cache"]),
            usd=a["usd"],
        )
    timeline = [TimelineRow(at=t.at, source=t.source.value, text=t.text) for t in state.task_ledger]
    return RunReport(
        thread_id=state.thread_id,
        title=state.task.title,
        tier=state.routing.tier.value if state.routing else None,
        phase=state.phase,
        exit_reason=state.exit_reason.value if state.exit_reason else None,
        branch=state.github.branch,
        pr_number=state.github.pr_number,
        features=[f"{f.id} [{f.status.value}] {f.description}" for f in state.feature_list],
        tokens_used=state.budgets.tokens_used,
        usd_used=state.budgets.usd_used,
        tokens_max=state.budgets.tokens_max,
        usd_max=state.budgets.usd_max,
        advisor_calls=f"{state.budgets.advisor_calls_used}/{state.budgets.advisor_calls_max}",
        iterations=state.progress.iterations,
        tool_calls=state.progress.tool_calls,
        by_role=list(by.values()),
        escalations=[
            EscalationRow(
                id=e.id,
                trigger=e.trigger.value,
                step_id=e.step_id,
                verdict=e.verdict.value if e.verdict else None,
                outcome=e.outcome,
                tokens=e.tokens,
                usd=e.usd,
            )
            for e in state.escalations
        ],
        timeline=timeline,
        artifacts={k: v.path for k, v in state.artifacts.items()},
        started_at=timeline[0].at if timeline else None,
        finished_at=timeline[-1].at if timeline else None,
    )


def write_report(report: RunReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{report.thread_id}.json"
    md_path = out_dir / f"{report.thread_id}.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8", newline="\n"
    )
    md_path.write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    return json_path, md_path


def load_state(ctx: Any) -> SCIOState:
    """The latest checkpointed state of ``ctx.thread_id``."""
    from scio.graph.build import compile_graph
    from scio.graph.checkpointer import open_checkpointer

    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph = compile_graph(ctx, saver)
        snapshot = graph.get_state(config)
    values = snapshot.values
    if not values:
        raise LookupError(f"no checkpoint for thread {ctx.thread_id!r}")
    return SCIOState.model_validate(values)
