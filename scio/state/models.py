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
    INTERVIEWER = "interviewer"  # product mode (ADR 0010)
    PRODUCT_PLANNER = "product_planner"
    MILESTONE_PLANNER = "milestone_planner"  # product mode (ADR 0011)
    MILESTONE_GATE = "milestone_gate"
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
    """Filter expression or path passed to ``run_tests`` (the profile's filter flag)."""
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
    last_failure_kind: Literal["none", "tests", "lint_only", "quality", "no_changes"] = "none"
    """What the last verify failed on; ``lint_only`` and ``quality`` (a product check such as
    the coverage floor failed while tests and lint passed, M11) are on the never-escalate
    list."""
    gate_replans: int = 0
    """Times the gate sent a planned run back to ``plan``; capped at one per run (I-043)."""


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
    tests_added: list[str] = Field(
        default_factory=list,
        description=(
            "The tests that prove the features worked on, existing or newly written, named "
            "the way the task's test runner selects them (a test path, a node id or a name "
            "filter). Never empty when a feature is claimed done."
        ),
    )
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


class TriageDecision(BaseModel):
    """Result of the pure triage function, stored so the conditional edge only reads state."""

    route: Literal["debug", "advise", "halt"]
    triggers: list[EscalationTrigger] = Field(default_factory=list)
    reason: str = ""


# ---------- product mode (ADR 0007, ADR 0010) ----------


class ProductFeature(BaseModel):
    id: str
    description: str = Field(max_length=400)
    acceptance: list[str] = Field(min_length=1)
    """Testable criteria; the normaliser rejects a feature without one."""


class AppSpec(BaseModel):
    """How the product starts and answers a health check (M9, ADR 0013)."""

    kind: Literal["cli", "http"] = "cli"
    """``cli``: a command that exits; ``http``: a server that answers ``health_path``."""
    module: str | None = Field(default=None, max_length=80)
    """What the toolchain's app command starts (a module for ``python -m``, an entry file for
    ``node``); the profile's default for the package when None."""
    args: list[str] = Field(default_factory=list, max_length=12)
    """Arguments that start the server; ``{port}`` is replaced by a free port."""
    health_path: str = Field(default="/health", max_length=200)


class ProductSpec(BaseModel):
    """The normalised content of PRODUCT.md."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    """Package and repository slug."""
    title: str = Field(max_length=120)
    vision: str = Field(max_length=1200)
    users: list[str] = Field(min_length=1, max_length=6)
    features: list[ProductFeature] = Field(min_length=1, max_length=24)
    quality_bar: list[str] = Field(min_length=1, max_length=12)
    stack: str = "python"
    """Toolchain profile name (ADR 0009)."""
    dependencies: list[str] = Field(default_factory=list)
    """Approved third-party dependencies; empty for a walking skeleton."""
    app: AppSpec = Field(default_factory=AppSpec)
    """How the product runs (``## Runtime`` in PRODUCT.md)."""


class InterviewOutput(BaseModel):
    """The interviewer's questions for the human (schema marker: ``interview_questions``)."""

    interview_questions: list[str] = Field(min_length=2, max_length=4)
    rationale: str = Field(max_length=600)


class ProductSpecOutput(BaseModel):
    """The product planner's spec draft (schema marker: ``product_name``)."""

    product_name: str = Field(max_length=60)
    title: str = Field(max_length=120)
    vision: str = Field(max_length=1200)
    users: list[str] = Field(min_length=1, max_length=6)
    features: list[ProductFeature] = Field(min_length=1, max_length=24)
    quality_bar: list[str] = Field(min_length=1, max_length=12)
    stack: str = Field(max_length=40)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    app: AppSpec | None = None
    """``cli`` when omitted; ``http`` products name the serve arguments and health path."""


ProductPhase = Literal[
    "interview", "answers", "specify", "approval", "bootstrap", "publish", "done", "halted"
]


class ProductState(BaseModel):
    """State of the product graph (``scio new``). Checkpointed per thread like SCIOState."""

    thread_id: str
    prompt: str
    target_dir: str
    gh_repo: str | None = None
    phase: ProductPhase = "interview"
    questions: list[str] = Field(default_factory=list)
    answers: str | None = None
    spec: ProductSpec | None = None
    product_md: str | None = None
    revision_notes: list[str] = Field(default_factory=list)
    """Human feedback on rejected spec drafts; each entry triggers one revision."""
    approved: bool = False
    bootstrap_commit: str | None = None
    repo_url: str | None = None
    exit_reason: str | None = None
    """None while running or on success; otherwise ``rejected``, ``bootstrap_failed`` or
    ``budget``."""
    ledger: Annotated[list[LedgerEntry], operator.add] = Field(default_factory=list)
    budgets: Budget


# ---------- product build: the milestone loop (ADR 0011) ----------


class MilestoneFeatureOutput(BaseModel):
    id: str
    """A feature id from PRODUCT.md (``P3``)."""
    tier: Tier = Tier.T1
    notes: str = Field(default="", max_length=400)
    """Guidance for the task run (which modules to touch, what to avoid)."""


class MilestonePlanOutput(BaseModel):
    """The milestone planner's proposal (schema marker: ``milestone_title``)."""

    milestone_title: str = Field(max_length=100)
    features: list[MilestoneFeatureOutput] = Field(min_length=1, max_length=6)
    rationale: str = Field(max_length=800)


class MilestonePlan(BaseModel):
    """The normalised milestone the orchestrator runs: a consecutive prefix of the open
    features in PRODUCT.md order, one pinned tier each."""

    number: int
    title: str
    branch: str
    features: list[MilestoneFeatureOutput] = Field(min_length=1)

    @property
    def id(self) -> str:
        return f"M{self.number}"

    def feature_ids(self) -> list[str]:
        return [f.id for f in self.features]


class FeatureRun(BaseModel):
    """What one feature's task-graph run produced, as the product graph records it."""

    feature_id: str
    thread_id: str
    attempt: int = 1
    tier: Tier
    status: Literal["running", "passed", "failed", "skipped"] = "running"
    exit_reason: str | None = None
    commit_sha: str | None = None
    branch: str | None = None
    issue_number: int | None = None
    usd: float = 0.0
    tokens: int = 0
    iterations: int = 0
    escalations: int = 0
    escalations_resolved: int = 0
    human_notes: list[str] = Field(default_factory=list)


class MilestonePR(BaseModel):
    number: int | None = None
    url: str | None = None
    body_path: str | None = None
    """Offline: where the PR body was written instead."""


class ReleaseRecord(BaseModel):
    """The release gate's outcome (M11): what was checked on ``main`` and what was shipped."""

    version: str
    tag: str
    report: str
    """The product-ready bar on ``main``, as ``integrate`` reports it."""
    ok: bool
    commit: str | None = None
    """The changelog-cut commit the tag points at, once tagged."""
    artifacts: list[str] = Field(default_factory=list)
    """Repository-relative paths under ``.scio/sandbox/release/`` (git-ignored)."""
    url: str | None = None
    """The GitHub Release, when the build runs with ``--gh``."""


ProductBuildPhase = Literal[
    "load",
    "plan",
    "issues",
    "feature",
    "integrate",
    "gate",
    "pr",
    "merge_wait",
    "update",
    "release_check",
    "release_decision",
    "release",
    "done",
    "halted",
]


class ProductBuildState(BaseModel):
    """State of the milestone loop (``scio build``). One thread per product build."""

    thread_id: str
    product_dir: str
    gh_repo: str | None = None
    phase: ProductBuildPhase = "load"
    spec: ProductSpec | None = None
    milestone: MilestonePlan | None = None
    issues: dict[str, int] = Field(default_factory=dict)
    """Feature id -> GitHub issue number (or the offline file index)."""
    feature_runs: list[FeatureRun] = Field(default_factory=list)
    """Every run of this build, oldest first; the latest per feature id is current."""
    integration: str | None = None
    """Output of the integration checks on the milestone branch, or None before they ran."""
    integration_ok: bool | None = None
    last_gate: GateOutput | None = None
    pr: MilestonePR | None = None
    merged_commit: str | None = None
    """``main`` after the milestone merge (local merge offline, pulled from origin on GitHub)."""
    stop_after_merge: bool = False
    """The human answered ``merge and stop``: record the milestone, then end the thread instead
    of planning the next milestone (``scio build`` starts a new thread for it)."""
    milestones_done: list[str] = Field(default_factory=list)
    release: ReleaseRecord | None = None
    """Set by the release gate once no milestone remains (M11)."""
    exit_reason: str | None = None
    """None while running or on success; ``feature_failed``, ``integration_failed``,
    ``gate_escalated``, ``stopped``, ``budget``, ``load_failed`` or ``release_failed``
    otherwise."""
    human_notes: Annotated[list[str], operator.add] = Field(default_factory=list)
    ledger: Annotated[list[LedgerEntry], operator.add] = Field(default_factory=list)
    budgets: Budget

    def latest_run(self, feature_id: str) -> FeatureRun | None:
        return next((r for r in reversed(self.feature_runs) if r.feature_id == feature_id), None)

    def next_feature(self) -> MilestoneFeatureOutput | None:
        """The first feature of the milestone without a passed or skipped run."""
        if self.milestone is None:
            return None
        for f in self.milestone.features:
            run = self.latest_run(f.id)
            if run is None or run.status not in ("passed", "skipped"):
                return f
        return None


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
    """Reducer: parallel explorers append. Every other list is written by one node at a time."""
    escalations: list[EscalationRecord] = Field(default_factory=list)
    github: GitHubRefs
    last_worker_output: WorkerOutput | None = None
    last_review: ReviewerOutput | None = None
    last_gate: GateOutput | None = None
    exit_reason: ExitReason | None = None
    human_notes: Annotated[list[str], operator.add] = Field(default_factory=list)
    # --- M4: planning and escalation ---
    exploration_questions: list[str] = Field(default_factory=list)
    advisor_guidance: str | None = None
    """Latest advisor guidance, shown to the next worker turn and then cleared."""
    high_impact_flags: list[str] = Field(default_factory=list)
    high_impact_reviewed: bool = False
    sample_disagreement: bool = False
    triage: TriageDecision | None = None

    def current_step(self) -> PlanStep | None:
        if self.plan is None or self.progress.current_step_id is None:
            return None
        return self.plan.step(self.progress.current_step_id)

    def step_features(self) -> list[FeatureItem]:
        step = self.current_step()
        if step is None:
            return list(self.feature_list)
        return [f for f in self.feature_list if f.id in step.feature_ids]

    def failing_features(self) -> list[FeatureItem]:
        return [f for f in self.feature_list if f.status is FeatureStatus.FAILING]

    def all_passing(self) -> bool:
        return bool(self.feature_list) and all(
            f.status is not FeatureStatus.FAILING for f in self.feature_list
        )
