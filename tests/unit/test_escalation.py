"""Each of the six escalation signals flips triage, and the never-escalate list does not."""

from __future__ import annotations

import pytest

from scio.loop.escalation import EscalationConfig, decide, detect, drifted_files, exit_for
from scio.loop.impact import assess, changed_files
from scio.state.models import (
    Budget,
    EscalationTrigger,
    ExitReason,
    GitHubRefs,
    Plan,
    PlanStep,
    ProgressLedger,
    Role,
    SCIOState,
    TaskSpec,
    WorkerOutput,
)

CFG = EscalationConfig(repeat_threshold=3, max_stall=2, high_impact_files=8)


def _state(**overrides: object) -> SCIOState:
    base = dict(
        thread_id="t",
        task=TaskSpec(title="t", description="d", acceptance=["a"], repo_url="r"),
        budgets=Budget(tokens_max=10**6, usd_max=10.0, advisor_calls_max=3),
        github=GitHubRefs(owner="o", repo="r"),
        plan=Plan(
            author=Role.PLANNER,
            steps=[
                PlanStep(
                    id="S1", description="d", feature_ids=["F1"], files_expected=["pkg/mod.py"]
                )
            ],
            rationale="r",
        ),
        progress=ProgressLedger(current_step_id="S1", last_failure_kind="tests"),
    )
    base.update(overrides)
    return SCIOState(**base)  # type: ignore[arg-type]


def _worker(**kw: object) -> WorkerOutput:
    d: dict[str, object] = dict(
        step_id="S1",
        summary="s",
        files_touched=["/w/pkg/mod.py"],
        confidence="high",
        next_action="verify",
    )
    d.update(kw)
    return WorkerOutput(**d)  # type: ignore[arg-type]


def test_clear_failure_goes_to_debug() -> None:
    d = decide(_state(last_worker_output=_worker()), CFG, root="/w")
    assert d.route == "debug" and d.triggers == []


# --- signal 1: repeated error ---


def test_repeated_error_flips() -> None:
    s = _state(
        progress=ProgressLedger(
            current_step_id="S1", fix_attempts_on_error=3, last_failure_kind="tests"
        )
    )
    assert EscalationTrigger.REPEATED_ERROR in detect(s, CFG)
    assert decide(s, CFG).route == "advise"


def test_repeated_error_below_threshold_does_not() -> None:
    s = _state(
        progress=ProgressLedger(
            current_step_id="S1", fix_attempts_on_error=2, last_failure_kind="tests"
        )
    )
    assert EscalationTrigger.REPEATED_ERROR not in detect(s, CFG)


# --- signal 2: stall ---


def test_stall_flips_and_at_limit_does_not() -> None:
    assert (
        decide(
            _state(
                progress=ProgressLedger(
                    current_step_id="S1", stall_count=3, last_failure_kind="tests"
                )
            ),
            CFG,
        ).route
        == "advise"
    )
    assert (
        decide(
            _state(
                progress=ProgressLedger(
                    current_step_id="S1", stall_count=2, last_failure_kind="tests"
                )
            ),
            CFG,
        ).route
        == "debug"
    )


# --- signal 3: low confidence / design decision ---


def test_low_confidence_and_design_question_flip() -> None:
    assert (
        decide(_state(last_worker_output=_worker(confidence="low")), CFG, root="/w").route
        == "advise"
    )
    assert (
        decide(_state(last_worker_output=_worker(needs_design_decision=True)), CFG, root="/w").route
        == "advise"
    )
    assert (
        decide(_state(last_worker_output=_worker(confidence="medium")), CFG, root="/w").route
        == "debug"
    )


# --- signal 4: plan drift ---


def test_plan_drift_flips_but_tests_are_allowed() -> None:
    drift = _worker(files_touched=["/w/pkg/mod.py", "/w/pkg/other.py"])
    assert decide(_state(last_worker_output=drift), CFG, root="/w").route == "advise"
    ok = _worker(files_touched=["/w/pkg/mod.py", "/w/tests/test_mod.py", "/w/pkg/test_helpers.py"])
    assert decide(_state(last_worker_output=ok), CFG, root="/w").route == "debug"
    assert drifted_files(["/w/pkg/sub/x.py"], ["pkg/sub"], root="/w", test_dirs=("tests/",)) == []
    assert drifted_files(["pkg\\x.py"], ["pkg/y.py"], root=None, test_dirs=("tests/",)) == [
        "pkg/x.py"
    ]


# --- signal 5: sampled patches disagree ---


def test_sample_disagreement_flips() -> None:
    assert decide(_state(sample_disagreement=True), CFG).route == "advise"
    assert decide(_state(sample_disagreement=False), CFG).route == "debug"


# --- signal 6: high impact ---


def test_high_impact_flips_once() -> None:
    assert (
        decide(_state(high_impact_flags=["dependency file changed: pyproject.toml"]), CFG).route
        == "advise"
    )
    reviewed = _state(
        high_impact_flags=["dependency file changed: pyproject.toml"], high_impact_reviewed=True
    )
    assert decide(reviewed, CFG).route == "debug"


def test_impact_assessment() -> None:
    diff = (
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n+x\n"
        "--- a/pkg/api.py\n+++ b/pkg/api.py\n"
        "-def public_thing():\n-def _private():\n+def renamed():\n"
        "--- a/tests/test_api.py\n+++ b/tests/test_api.py\n-def test_old():\n"
        "--- a/db/migrations/0002_x.sql\n+++ b/db/migrations/0002_x.sql\n+alter\n"
    )
    flags = assess(diff, max_files=8)
    assert changed_files(diff) == [
        "pyproject.toml",
        "pkg/api.py",
        "tests/test_api.py",
        "db/migrations/0002_x.sql",
    ]
    assert any("dependency" in f for f in flags) and any("schema" in f for f in flags)
    assert any(
        "public API" in f and "public_thing" in f and "_private" not in f and "test_old" not in f
        for f in flags
    )
    many = "".join(f"--- a/f{i}.py\n+++ b/f{i}.py\n+x\n" for i in range(9))
    assert any("9 files changed" in f for f in assess(many, max_files=8))
    assert assess("--- a/pkg/x.py\n+++ b/pkg/x.py\n+y = 1\n", max_files=8) == []


# --- never escalate ---


def test_lint_only_never_escalates_even_with_signals() -> None:
    s = _state(
        progress=ProgressLedger(
            current_step_id="S1",
            fix_attempts_on_error=5,
            stall_count=5,
            last_failure_kind="lint_only",
        ),
        high_impact_flags=["x"],
    )
    d = decide(s, CFG)
    assert d.route == "debug" and "lint" in d.reason


# --- caps ---


def test_cap_per_step_and_per_run_route_to_halt_with_advisor_cap_exit() -> None:
    trig = ProgressLedger(current_step_id="S1", stall_count=3, last_failure_kind="tests")
    per_step = _state(
        progress=trig,
        budgets=Budget(
            tokens_max=1,
            usd_max=1,
            advisor_calls_max=3,
            advisor_calls_used=1,
            advisor_calls_on_step={"S1": 1},
        ),
    )
    d = decide(per_step, CFG)
    assert d.route == "halt" and exit_for(d) is ExitReason.ADVISOR_CAP and "cap" in d.reason
    per_run = _state(
        progress=trig,
        budgets=Budget(tokens_max=1, usd_max=1, advisor_calls_max=1, advisor_calls_used=1),
    )
    assert decide(per_run, CFG).route == "halt"
    other_step = _state(
        progress=trig.model_copy(update={"current_step_id": "S2"}),
        budgets=Budget(
            tokens_max=1,
            usd_max=1,
            advisor_calls_max=3,
            advisor_calls_used=1,
            advisor_calls_on_step={"S1": 1},
        ),
    )
    assert decide(other_step, CFG).route == "advise"


def test_exit_reason_wins_over_everything() -> None:
    s = _state(
        exit_reason=ExitReason.BUDGET,
        progress=ProgressLedger(current_step_id="S1", stall_count=9, last_failure_kind="tests"),
    )
    d = decide(s, CFG)
    assert d.route == "halt" and exit_for(d) is None


@pytest.mark.parametrize("trigger", list(EscalationTrigger))
def test_every_trigger_has_a_flip_test(trigger: EscalationTrigger) -> None:
    covered = {
        EscalationTrigger.REPEATED_ERROR,
        EscalationTrigger.STALL,
        EscalationTrigger.LOW_CONFIDENCE,
        EscalationTrigger.PLAN_DRIFT,
        EscalationTrigger.SAMPLE_DISAGREEMENT,
        EscalationTrigger.HIGH_IMPACT,
        EscalationTrigger.FIXED_PLAN,  # the plan node itself
        EscalationTrigger.FIXED_GATE,  # the gate node itself
    }
    assert trigger in covered
