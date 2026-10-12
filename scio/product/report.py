"""The product-thread view of ``scio report`` (ADR 0011): milestones, feature runs with
their own threads, cost by feature and by orchestrator role, escalations, timeline."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from scio.harness.accounting import CostEntry
from scio.state.models import FeatureRun, ProductBuildState


class FeatureRow(BaseModel):
    feature_id: str
    attempt: int
    thread_id: str
    tier: str
    status: str
    exit_reason: str | None
    commit: str | None
    usd: float
    iterations: int
    escalations: int
    escalations_resolved: int


class ProductReport(BaseModel):
    thread_id: str
    product_dir: str
    phase: str
    exit_reason: str | None
    milestones_done: list[str]
    current_milestone: str | None
    features: list[FeatureRow] = Field(default_factory=list)
    orchestrator_usd: float
    features_usd: float
    by_role: dict[str, float] = Field(default_factory=dict)
    timeline: list[str] = Field(default_factory=list)
    human_notes: list[str] = Field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return round(self.orchestrator_usd + self.features_usd, 4)

    def to_markdown(self) -> str:
        rows = [
            f"| {r.feature_id} | {r.attempt} | `{r.thread_id}` | {r.tier} | {r.status} | "
            f"{r.exit_reason or '-'} | {(r.commit or '-')[:12]} | {r.usd:.4f} | {r.iterations} | "
            f"{r.escalations_resolved}/{r.escalations} |"
            for r in self.features
        ]
        roles = [f"| {role} | {usd:.4f} |" for role, usd in self.by_role.items()]
        return "\n".join(
            [
                f"# Product build {self.thread_id}",
                "",
                f"dir `{self.product_dir}` · phase **{self.phase}** · "
                f"exit **{self.exit_reason or '-'}** · milestones done "
                f"{', '.join(self.milestones_done) or '-'} · current "
                f"{self.current_milestone or '-'}",
                f"cost: features ${self.features_usd:.4f} + orchestrator "
                f"${self.orchestrator_usd:.4f} = ${self.total_usd:.4f}",
                "",
                "## Feature runs",
                "| feature | attempt | thread | tier | status | exit | commit | $ | iterations "
                "| escalations resolved/total |",
                "|---|---:|---|---|---|---|---|---:|---:|---|",
                *(rows or ["| none | | | | | | | | | |"]),
                "",
                "## Orchestrator cost by role",
                "| role | $ |",
                "|---|---:|",
                *(roles or ["| none | |"]),
                "",
                "## Human notes",
                *([f"- {n}" for n in self.human_notes] or ["- none"]),
                "",
                "## Timeline",
                *[f"- {t}" for t in self.timeline],
                "",
            ]
        )


def _row(r: FeatureRun) -> FeatureRow:
    return FeatureRow(
        feature_id=r.feature_id,
        attempt=r.attempt,
        thread_id=r.thread_id,
        tier=r.tier.value,
        status=r.status,
        exit_reason=r.exit_reason,
        commit=r.commit_sha,
        usd=r.usd,
        iterations=r.iterations,
        escalations=r.escalations,
        escalations_resolved=r.escalations_resolved,
    )


def build_product_report(state: ProductBuildState, entries: list[CostEntry]) -> ProductReport:
    by: dict[str, float] = defaultdict(float)
    for e in entries:
        by[e.role] += e.usd
    return ProductReport(
        thread_id=state.thread_id,
        product_dir=state.product_dir,
        phase=state.phase,
        exit_reason=state.exit_reason,
        milestones_done=list(state.milestones_done),
        current_milestone=state.milestone.id if state.milestone else None,
        features=[_row(r) for r in state.feature_runs],
        orchestrator_usd=round(state.budgets.usd_used, 4),
        features_usd=round(sum(r.usd for r in state.feature_runs), 4),
        by_role={k: round(v, 4) for k, v in sorted(by.items(), key=lambda kv: -kv[1])},
        timeline=[
            f"`{t.at.strftime('%H:%M:%S')}` **{t.source.value}** {t.text}" for t in state.ledger
        ],
        human_notes=list(state.human_notes),
    )


def write_product_report(report: ProductReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{report.thread_id}.json"
    md_path = out_dir / f"{report.thread_id}.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8", newline="\n"
    )
    md_path.write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    return json_path, md_path
