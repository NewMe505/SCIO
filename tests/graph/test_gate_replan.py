"""Gate ``replan`` on a planned tier returns to ``plan`` once; everything else halts (I-043).

Before this, every non-pass gate verdict ended the run as ``escalate_human``, so a gate that
only wanted one more pass stopped a green run for a human.
"""

from __future__ import annotations

from pathlib import Path

from evals.fake_scripts import ScriptedResponder
from scio.graph.nodes.checkpoint_gate import after_gate
from scio.graph.run import resume_graph
from scio.state.models import (
    Budget,
    ExitReason,
    FeatureStatus,
    GateOutput,
    GitHubRefs,
    Role,
    SCIOState,
    TaskSpec,
)
from tests.graph.test_t1_t2_fake import _finish, _run


def test_t1_gate_replan_returns_to_plan_then_passes(tmp_path: Path) -> None:
    responder = ScriptedResponder(gate_verdicts=["replan", "pass"])
    ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", responder)
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert responder.counts["gate"] == 2
    assert s.progress.gate_replans == 1
    assert s.plan is not None and s.plan.version == 2
    assert s.plan.revision_reason is not None and s.plan.revision_reason.startswith("gate replan")
    assert all(st.done for st in s.plan.steps)
    assert all(f.status is FeatureStatus.PASSING for f in s.feature_list)
    texts = [e.text for e in s.task_ledger]
    assert any(t.startswith("gate replan: returning to plan") for t in texts)
    assert s.advisor_guidance is None  # consumed by the planner


def test_t1_second_gate_replan_halts_for_a_human(tmp_path: Path) -> None:
    responder = ScriptedResponder(gate_verdict="replan")
    ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", responder)
    assert outcome.paused and outcome.interrupt is not None
    assert outcome.interrupt["reason"] == "escalate_human"
    assert outcome.interrupt["last_gate"]["verdict"] == "replan"
    final = resume_graph(ctx, "stop")
    assert final.state.exit_reason is ExitReason.ESCALATE_HUMAN
    assert final.state.progress.gate_replans == 1 and responder.counts["gate"] == 2


def test_t1_gate_escalate_human_halts_at_once(tmp_path: Path) -> None:
    responder = ScriptedResponder(gate_verdict="escalate_human")
    _ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", responder)
    assert outcome.paused and outcome.interrupt is not None
    assert outcome.interrupt["reason"] == "escalate_human"
    assert responder.counts["gate"] == 1 and outcome.state.progress.gate_replans == 0


def test_worker_cannot_move_the_run_onto_a_finished_step(tmp_path: Path) -> None:
    """I-046: the scripted coder names S1 for every step; with gate_verdicts ``replan`` the
    appended step S2 must still be the current step, so the run finishes instead of looping
    on passing verifies. The stall cap is the second guard: it must stay unused here."""
    responder = ScriptedResponder(gate_verdicts=["replan", "pass"])
    ctx, outcome = _run(tmp_path, "t004_two_features", "T1", responder, max_iters=12)
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert s.progress.iterations <= 4 and s.progress.stall_count <= 1
    assert [st.id for st in s.plan.steps if st.done] == ["S1", "S2", "S3"]  # type: ignore[union-attr]


def test_after_gate_edge_reads_state_only() -> None:
    def state(verdict: str, phase: str, exit_reason: ExitReason | None) -> SCIOState:
        return SCIOState(
            thread_id="t",
            task=TaskSpec(title="t", description="d", acceptance=[], repo_url="x"),
            budgets=Budget(tokens_max=1, usd_max=1.0, advisor_calls_max=0),
            github=GitHubRefs(owner="o", repo="r"),
            phase=phase,  # type: ignore[arg-type]
            exit_reason=exit_reason,
            last_gate=GateOutput(
                verdict=verdict,  # type: ignore[arg-type]
                objective_met=verdict == "pass",
                unmet_acceptance=[],
                reasoning="r",
            ),
        )

    assert after_gate(state("pass", "pr_ready", None)) == "pr_ready"
    assert after_gate(state("replan", "plan", None)) == "plan"
    assert after_gate(state("replan", "halted", ExitReason.ESCALATE_HUMAN)) == "halt"
    assert after_gate(state("escalate_human", "halted", ExitReason.ESCALATE_HUMAN)) == "halt"
    assert Role.GATE  # the ledger entry source used by the gate node exists
