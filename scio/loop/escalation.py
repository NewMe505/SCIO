"""The escalation policy: six signals, a never-escalate list, and the caps.

Pure functions of state. ``decide`` returns a :class:`TriageDecision` that the ``verify``
node stores and the ``triage`` conditional edge reads, so routing never computes.

Signals (any one escalates):
  1. repeated error: the same error signature seen ``repeat_threshold`` times
  2. stall: ``stall_count`` above ``max_stall``
  3. the worker reported ``confidence=low`` or ``needs_design_decision``
  4. plan drift: the diff touched files outside the step's ``files_expected``
  5. sampled patches disagreed and none passed
  6. high-impact change (public API, schema, dependencies, > N files)

Never escalate for: lint/format-only failures, a first clear test failure (the default
path is ``debug``), docs, schema repairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from scio.state.models import EscalationTrigger, ExitReason, SCIOState, TriageDecision


@dataclass(frozen=True)
class EscalationConfig:
    repeat_threshold: int = 3
    max_stall: int = 2
    high_impact_files: int = 8
    sample_n: int = 1
    explorers_min: int = 2
    explorers_max: int = 4
    test_dirs: tuple[str, ...] = ("tests/", "test/")

    def __post_init__(self) -> None:
        # ADR 0006: the stall exit must not fire before the repeated-error signal can escalate.
        if self.max_stall < self.repeat_threshold - 1:
            raise ValueError(
                f"max_stall ({self.max_stall}) must be >= repeat_threshold - 1 "
                f"({self.repeat_threshold - 1}); see ADR 0006"
            )


def _norm(path: str, root: str | None) -> str:
    p = path.replace("\\", "/")
    if root:
        r = root.replace("\\", "/").rstrip("/") + "/"
        if p.startswith(r):
            p = p[len(r) :]
    return p.lstrip("./")


def drifted_files(
    touched: list[str], expected: list[str], *, root: str | None, test_dirs: tuple[str, ...]
) -> list[str]:
    """Touched files that are neither expected by the plan step nor test files."""
    exp = [_norm(e, root) for e in expected]
    out: list[str] = []
    for raw in touched:
        t = _norm(raw, root)
        if any(t.startswith(d) for d in test_dirs) or PurePosixPath(t).name.startswith("test_"):
            continue
        if any(t == e or t.startswith(e.rstrip("/") + "/") for e in exp):
            continue
        out.append(t)
    return out


def detect(
    state: SCIOState, cfg: EscalationConfig, *, root: str | None = None
) -> list[EscalationTrigger]:
    p = state.progress
    found: list[EscalationTrigger] = []
    if p.fix_attempts_on_error >= cfg.repeat_threshold:
        found.append(EscalationTrigger.REPEATED_ERROR)
    if p.stall_count > cfg.max_stall:
        found.append(EscalationTrigger.STALL)
    w = state.last_worker_output
    if w is not None and (w.confidence == "low" or w.needs_design_decision):
        found.append(EscalationTrigger.LOW_CONFIDENCE)
    step = state.current_step()
    if (
        w is not None
        and step is not None
        and step.files_expected
        and drifted_files(w.files_touched, step.files_expected, root=root, test_dirs=cfg.test_dirs)
    ):
        found.append(EscalationTrigger.PLAN_DRIFT)
    if state.sample_disagreement:
        found.append(EscalationTrigger.SAMPLE_DISAGREEMENT)
    if state.high_impact_flags and not state.high_impact_reviewed:
        found.append(EscalationTrigger.HIGH_IMPACT)
    return found


def decide(state: SCIOState, cfg: EscalationConfig, *, root: str | None = None) -> TriageDecision:
    """Where a failing verify goes next. Only meaningful for tiers with a plan (T1/T2)."""
    if state.exit_reason is not None:
        return TriageDecision(route="halt", reason=f"exit: {state.exit_reason.value}")
    if state.progress.last_failure_kind == "lint_only":
        return TriageDecision(route="debug", reason="never escalate: lint/format only")
    triggers = detect(state, cfg, root=root)
    if not triggers:
        return TriageDecision(route="debug", reason="no escalation signal; clear failure path")
    step_id = state.progress.current_step_id or "S?"
    if state.budgets.advisor_allowed(step_id):
        return TriageDecision(
            route="advise", triggers=triggers, reason=", ".join(t.value for t in triggers)
        )
    used, cap = state.budgets.advisor_calls_used, state.budgets.advisor_calls_max
    return TriageDecision(
        route="halt",
        triggers=triggers,
        reason=f"advisor cap hit ({used}/{cap}, step {step_id})",
    )


def exit_for(decision: TriageDecision) -> ExitReason | None:
    """The exit reason a ``halt`` triage implies (set by the node that stores the decision)."""
    if decision.route == "halt" and decision.triggers:
        return ExitReason.ADVISOR_CAP
    return None
