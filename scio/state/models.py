"""SCIO graph state (the M0 schema, now executable).

Rules baked in:

* Large content is passed by :class:`ArtifactRef` (path + sha256), never inline.
* Lists that parallel branches append to carry an ``operator.add`` reducer; fan-out nodes
  may return only those keys.
* Every worker output cites the plan ``step_id`` it executed. The triage edge compares
  ``files_touched`` to the step's ``files_expected`` (escalation signal 4).
* Structured-output models contain no free-form ``dict`` fields (the wire schema rejects
  them); mappings in the state itself are fine because the state is never a model output.
"""

from __future__ import annotations

import operator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------- enums ----------


class Tier(StrEnum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"


class FeatureStatus(StrEnum):
    FAILING = "failing"
    PASSING = "passing"
    SKIPPED = "skipped"  # only by human decision, recorded in task_ledger


class Role(StrEnum):
    ROUTER = "router"
    PLANNER = "planner"
    SCOPER = "scoper"
    EXPLORER = "explorer"
    CODER = "coder"
    TESTER = "tester"
    DEBUGGER = "debugger"
    REVIEWER = "reviewer"
    DOCUMENTER = "documenter"
    ADVISOR = "advisor"
    GATE = "gate"
    COMPACTOR = "compactor"
    HUMAN = "human"
    SYSTEM = "system"


class Verdict(StrEnum):
    CONTINUE = "continue"
    REPLAN = "replan"
    CHANGE_APPROACH = "change_approach"
    ESCALATE_HUMAN = "escalate_human"


class ExitReason(StrEnum):
    SUCCESS = "success"
    MAX_ITERS = "max_iters"
    BUDGET = "budget"
    STALL = "stall"
    ESCALATE_HUMAN = "escalate_human"
    ADVISOR_CAP = "advisor_cap"


class EscalationTrigger(StrEnum):
    FIXED_PLAN = "fixed_plan"  # touchpoint (a)
    FIXED_GATE = "fixed_gate"  # touchpoint (b)
    REPEATED_ERROR = "repeated_error"  # 1
    STALL = "stall"  # 2
    LOW_CONFIDENCE = "low_confidence"  # 3
    PLAN_DRIFT = "plan_drift"  # 4
    SAMPLE_DISAGREEMENT = "sample_disagreement"  # 5
    HIGH_IMPACT = "high_impact"  # 6


Phase = Literal[
    "intake",
    "route",
    "prepare",
    "scope",
    "explore",
    "plan",
    "post_plan",
    "localize",
    "implement",
    "verify",
    "debug",
    "advise",
    "checkpoint",
    "review",
    "document",
    "gate",
    "pr_ready",
    "merge_wait",
    "done",
    "halted",
]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- references and ledgers ----------


class ArtifactRef(BaseModel):
    """Pass-by-reference handle. Never put file contents in state."""

    kind: Literal[
        "diff", "test_output", "lint_output", "summary", "brief", "log", "plan_comment", "pr_body"
    ]
    path: str
    """Absolute path inside the sandbox or artifact store."""
    sha256: str
    tokens: int | None = None
    produced_by: Role
    created_at: datetime = Field(default_factory=_now)


class TaskSpec(BaseModel):
    title: str
    description: str
    acceptance: list[str]
    """One line each; becomes ``feature_list``."""
    repo_url: str
    base_branch: str = "main"
    issue_number: int | None = None
    tier_hint: Tier | None = None
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)


class FeatureItem(BaseModel):
    id: str
    """``F1``, ``F2``, ..."""
    description: str
    test_filter: str | None = None
    """``pytest -k`` expression or path passed to ``run_tests``."""
    status: FeatureStatus = FeatureStatus.FAILING
    commit_sha: str | None = None
    evidence: ArtifactRef | None = None


class PlanStep(BaseModel):
    id: str
    """``S1``, ``S2``, ..."""
    description: str
    feature_ids: list[str]
    files_expected: list[str]
    """Used by plan-drift detection (signal 4)."""
    done: bool = False


class Plan(BaseModel):
    version: int = 1
    author: Role
    """``PLANNER``, or ``ADVISOR`` after a replan."""
    steps: list[PlanStep]
    rationale: str
    revision_reason: str | None = None

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)


class LedgerEntry(BaseModel):
    kind: Literal["fact", "guess", "decision"]
    text: str
    source: Role
    step_id: str | None = None
    at: datetime = Field(default_factory=_now)


class ProgressLedger(BaseModel):
    current_step_id: str | None = None
    current_feature_id: str | None = None
    attempts_on_step: int = 0
    fix_attempts_on_error: int = 0
    last_error_signature: str | None = None
    stall_count: int = 0
    """+1 for each iteration whose progress fingerprint did not change."""
    iterations: int = 0
    tool_calls: int = 0
    """T0 cap = 10."""
    last_fingerprint: str | None = None


class Budget(BaseModel):
    tokens_max: int
    tokens_used: int = 0
    usd_max: float
    usd_used: float = 0.0
    advisor_calls_max: int
    advisor_calls_used: int = 0
    advisor_calls_on_step: dict[str, int] = Field(default_factory=dict)
    max_iters: int = 30
    max_stall: int = 2

    @property
    def exhausted(self) -> bool:
        return self.tokens_used >= self.tokens_max or self.usd_used >= self.usd_max

    def advisor_allowed(self, step_id: str) -> bool:
        return (
            self.advisor_calls_used < self.advisor_calls_max
            and self.advisor_calls_on_step.get(step_id, 0) < 1
        )


class Routing(BaseModel):
    tier: Tier
    pattern: Literal["single_loop", "plan_execute_gate", "explore_plan_execute_gate"]
    effort_tier: Literal["low", "medium", "high", "xhigh"]
    rationale: str
    classifier_model: str
    confidence: float
    promoted_from: Tier | None = None


class GitHubRefs(BaseModel):
    owner: str
    repo: str
    issue_number: int | None = None
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    plan_comment_id: int | None = None
    pr_number: int | None = None
    pr_state: Literal["none", "draft", "ready", "merged"] = "none"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


class EscalationRecord(BaseModel):
    id: str
    trigger: EscalationTrigger
    step_id: str | None
    brief: ArtifactRef
    verdict: Verdict | None = None
    tokens: int = 0
    usd: float = 0.0
    outcome: Literal["resolved", "unresolved", "pending"] = "pending"
    at: datetime = Field(default_factory=_now)


# ---------- structured outputs (one per model-calling role) ----------


class RouterOutput(BaseModel):
    tier: Tier
    rationale: str = Field(max_length=400)
    confidence: float = Field(ge=0, le=1)


class ScopeOutput(BaseModel):
    exploration_questions: list[str] = Field(min_length=2, max_length=4)
    draft_features: list[FeatureItem]


class ExplorerSummary(BaseModel):
    question: str
    findings: list[str]
    """Facts with ``file:line`` citations."""
    relevant_files: list[str]
    open_questions: list[str] = Field(default_factory=list)
    # The Context layer enforces <=1.5k tokens on the serialized form.


class PlannerOutput(BaseModel):
    steps: list[PlanStep]
    feature_list: list[FeatureItem]
    rationale: str
    risks: list[str]


class SweepRecord(BaseModel):
    """The systemic sweep a Debugger performs after a confirmed root cause (CLAUDE.md)."""

    pattern: str
    """The searchable construct that caused the bug."""
    files_checked: list[str]
    files_fixed: list[str]
    ruled_out: list[str] = Field(default_factory=list)


class WorkerOutput(BaseModel):
    """Returned by Coder / Tester / Debugger / Documenter at the end of a loop iteration."""

    step_id: str
    """REQUIRED: the plan step being executed."""
    summary: str = Field(max_length=800)
    files_touched: list[str]
    confidence: Literal["high", "medium", "low"]
    needs_design_decision: bool = False
    design_question: str | None = None
    next_action: Literal["verify", "continue", "done"]
    tests_added: list[str] = Field(default_factory=list)
    """pytest ``-k`` expressions (or test file paths) that cover the features worked on."""
    sweep: SweepRecord | None = None
    """Set by the Debugger after a root-cause fix; None for other roles."""


class ReviewFinding(BaseModel):
    severity: Literal["blocker", "major", "minor", "nit"]
    file: str
    line: int | None
    summary: str
    suggestion: str | None = None


class ReviewerOutput(BaseModel):
    findings: list[ReviewFinding]
    """Advisory: never overrides a passing check."""
    overall: Literal["approve", "request_changes"]


class AdvisorOutput(BaseModel):
    """Opus Advisor / Planner replan. 400 to 700 tokens, never calls tools."""

    verdict: Verdict
    guidance: str = Field(max_length=2800)
    revised_steps: list[PlanStep] | None = None
    confidence: Literal["high", "medium", "low"]


class GateOutput(BaseModel):
    verdict: Literal["pass", "replan", "escalate_human"]
    objective_met: bool
    unmet_acceptance: list[str]
    reasoning: str = Field(max_length=2000)


# ---------- the graph state ----------


class SCIOState(BaseModel):
    thread_id: str
    phase: Phase = "intake"
    task: TaskSpec
    routing: Routing | None = None
    feature_list: list[FeatureItem] = Field(default_factory=list)
    plan: Plan | None = None
    task_ledger: Annotated[list[LedgerEntry], operator.add] = Field(default_factory=list)
    progress: ProgressLedger = Field(default_factory=ProgressLedger)
    budgets: Budget
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    """Keyed by logical name, e.g. ``diff:S3:attempt2``."""
    explorer_summaries: Annotated[list[ArtifactRef], operator.add] = Field(default_factory=list)
    escalations: Annotated[list[EscalationRecord], operator.add] = Field(default_factory=list)
    github: GitHubRefs
    last_worker_output: WorkerOutput | None = None
    last_review: ReviewerOutput | None = None
    last_gate: GateOutput | None = None
    exit_reason: ExitReason | None = None
    human_notes: Annotated[list[str], operator.add] = Field(default_factory=list)

    def failing_features(self) -> list[FeatureItem]:
        return [f for f in self.feature_list if f.status is FeatureStatus.FAILING]

    def all_passing(self) -> bool:
        return bool(self.feature_list) and all(
            f.status is not FeatureStatus.FAILING for f in self.feature_list
        )
