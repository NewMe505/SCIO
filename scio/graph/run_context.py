"""``RunContext``: the runtime objects every node needs, built once per run.

Nodes are closures over a context so the compiled graph stays a pure structure (it can
be compiled with ``ctx=None`` to render a diagram) while runs carry the harness, the
sandbox, the effect log, the artifact store and the optional GitHub client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scio.aci.sandbox.base import Limits, Sandbox
from scio.aci.sandbox.local import LocalSandbox
from scio.context.artifacts import ArtifactStore
from scio.graph.effects import EffectLog
from scio.harness.budget import BudgetGuard, BudgetLimits, BudgetsConfig
from scio.harness.core import Harness
from scio.harness.github.client import GitHubClient
from scio.loop.escalation import EscalationConfig
from scio.loop.exits import LoopLimits
from scio.prompt.registry import PromptRegistry
from scio.settings import Settings, get_settings
from scio.state.models import Budget

T0_TOOL_CAP = 10


@dataclass
class RunContext:
    settings: Settings
    thread_id: str
    harness: Harness
    sandbox: Sandbox
    effects: EffectLog
    artifacts: ArtifactStore
    prompts: PromptRegistry
    limits: LoopLimits
    budget: Budget
    github: GitHubClient | None = None
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    tier: str = "T0"

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        sandbox_root: Path | str,
        tier: str = "T0",
        settings: Settings | None = None,
        harness: Harness | None = None,
        sandbox: Sandbox | None = None,
        github: GitHubClient | None = None,
        work_dir: Path | str | None = None,
        budget_limits: BudgetLimits | None = None,
    ) -> RunContext:
        settings = settings or get_settings()
        budgets = BudgetsConfig.load(settings.budgets_path)
        lim = budget_limits or budgets.tiers[tier]
        budget = Budget(
            tokens_max=lim.tokens_max,
            usd_max=lim.usd_max,
            advisor_calls_max=lim.advisor_calls_max,
            max_iters=lim.max_iters,
            max_stall=lim.max_stall,
        )
        if harness is None:
            guard = BudgetGuard(
                BudgetLimits(tokens_max=lim.tokens_max, usd_max=lim.usd_max),
                reserve_output_fraction=budgets.reserve_output_fraction,
            )
            harness = Harness.from_settings(settings, budget=guard)
        work = Path(work_dir) if work_dir else Path(".scio")
        sandbox = sandbox or LocalSandbox(
            sandbox_root,
            Limits(
                timeout_s=settings.sandbox_timeout_s,
                memory_mb=settings.sandbox_memory_mb,
                cpus=settings.sandbox_cpus,
            ),
        )
        if github is None and settings.gh_repo:
            github = GitHubClient(settings.gh_repo)
        return cls(
            settings=settings,
            thread_id=thread_id,
            harness=harness,
            sandbox=sandbox,
            effects=EffectLog(work / "effects.sqlite"),
            artifacts=ArtifactStore(work / "artifacts", thread_id),
            prompts=PromptRegistry(),
            limits=LoopLimits(
                max_iters=lim.max_iters,
                max_stall=lim.max_stall,
                max_tool_calls_per_iter=T0_TOOL_CAP if tier == "T0" else None,
            ),
            budget=budget,
            github=github,
            escalation=EscalationConfig(**budgets.escalation.model_dump()),
            tier=tier,
        )

    def budget_update(self) -> dict[str, object]:
        """State patch reflecting the harness's spend so far."""
        snap = self.harness.budget.snapshot()
        b = self.budget.model_copy(
            update={"tokens_used": snap.tokens_used, "usd_used": snap.usd_used}
        )
        self.budget = b
        return {"budgets": b}
