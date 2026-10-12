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
from scio.toolchain import Toolchain, get_toolchain

T0_TOOL_CAP = 10


def make_sandbox(settings: Settings, sandbox_root: Path | str, limits: Limits) -> Sandbox:
    """The sandbox backend named by ``settings.sandbox_backend`` (I-054)."""
    if settings.sandbox_backend == "docker":
        from scio.aci.sandbox.docker import DockerSandbox

        return DockerSandbox(sandbox_root, image=settings.sandbox_docker_image, limits=limits)
    if settings.sandbox_backend == "e2b":
        from scio.aci.sandbox.e2b import E2BSandbox

        return E2BSandbox(template=settings.sandbox_e2b_template, limits=limits)
    return LocalSandbox(sandbox_root, limits)


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
    tier_pin: str | None = None
    """When set, ``route`` skips the classifier and runs this tier (eval tasks, I-050)."""
    toolchain: Toolchain = field(default_factory=lambda: get_toolchain("python"))
    """The stack profile every command and parser comes from (ADR 0009)."""
    budget_from_tier: bool = False
    """True when the budget was sized from ``tier`` defaults rather than explicit limits, so
    ``route`` may resize it for the tier the classifier picks (I-053)."""

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
        tier_pin: str | None = None,
    ) -> RunContext:
        settings = settings or get_settings()
        budgets = BudgetsConfig.for_settings(settings)
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
        toolchain = get_toolchain(settings.toolchain)
        sandbox = sandbox or make_sandbox(
            settings,
            sandbox_root,
            Limits(
                timeout_s=settings.sandbox_timeout_s,
                memory_mb=settings.sandbox_memory_mb,
                cpus=settings.sandbox_cpus,
            ),
        )
        if github is None and settings.gh_repo:
            github = GitHubClient(settings.gh_repo)
        harness.ledger.persist_to(work / "ledger" / f"{thread_id}.jsonl")
        # An explicit work_dir isolates a run (evals); otherwise the configured effects path.
        effects_path = work / "effects.sqlite" if work_dir else settings.effects_path
        return cls(
            settings=settings,
            thread_id=thread_id,
            harness=harness,
            sandbox=sandbox,
            effects=EffectLog(effects_path),
            artifacts=ArtifactStore(work / "artifacts", thread_id),
            prompts=PromptRegistry(),
            limits=LoopLimits(
                max_iters=lim.max_iters,
                max_stall=lim.max_stall,
                max_tool_calls_per_iter=T0_TOOL_CAP if tier == "T0" else None,
            ),
            budget=budget,
            github=github,
            escalation=EscalationConfig(
                **budgets.escalation.model_dump(), test_dirs=toolchain.test_dirs
            ),
            tier=tier,
            tier_pin=tier_pin,
            toolchain=toolchain,
            budget_from_tier=budget_limits is None,
        )

    def apply_tier_budgets(self, tier: str) -> None:
        """Resize the budget, the loop limits and the harness guard for ``tier`` (I-053).

        Called by ``route`` when the classifier picks a tier other than the one the budget
        was sized for and the budget came from tier defaults (not explicit limits).
        """
        lim = BudgetsConfig.for_settings(self.settings).tiers[tier]
        self.budget = self.budget.model_copy(
            update={
                "tokens_max": lim.tokens_max,
                "usd_max": lim.usd_max,
                "advisor_calls_max": lim.advisor_calls_max,
                "max_iters": lim.max_iters,
                "max_stall": lim.max_stall,
            }
        )
        self.limits = LoopLimits(
            max_iters=lim.max_iters,
            max_stall=lim.max_stall,
            max_tool_calls_per_iter=T0_TOOL_CAP if tier == "T0" else None,
        )
        self.harness.budget.limits = BudgetLimits(tokens_max=lim.tokens_max, usd_max=lim.usd_max)
        self.tier = tier

    def close(self) -> None:
        """Release the run's file handles (the effect log's SQLite connection).

        Without this a finished trial's directory cannot be deleted on Windows (I-049).
        """
        self.effects.close()

    def budget_update(self) -> dict[str, object]:
        """State patch reflecting the harness's spend so far."""
        snap = self.harness.budget.snapshot()
        b = self.budget.model_copy(
            update={"tokens_used": snap.tokens_used, "usd_used": snap.usd_used}
        )
        self.budget = b
        return {"budgets": b}
