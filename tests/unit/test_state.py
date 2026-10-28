from __future__ import annotations

from scio.state import (
    Budget,
    FeatureItem,
    FeatureStatus,
    GitHubRefs,
    ProgressLedger,
    Role,
    SCIOState,
    SweepRecord,
    TaskSpec,
    WorkerOutput,
    error_signature,
    progress_fingerprint,
)
from scio.state.models import (
    FeatureRun,
    MilestoneFeatureOutput,
    MilestonePlan,
    ProductBuildState,
    Tier,
)
from scio.state.signatures import advance

TB_A = """Traceback (most recent call last):
  File "/tmp/x/pkg/mod.py", line 12, in run
    return items[page * size]
  File "/tmp/x/pkg/util.py", line 40, in paginate
    assert len(pages) == 3
AssertionError: assert 2 == 3
"""
TB_A_SHIFTED = (
    TB_A.replace("line 12", "line 19").replace("line 40", "line 47").replace("/tmp/x/", "/tmp/y/")
)
TB_B = TB_A.replace("AssertionError: assert 2 == 3", "TypeError: unsupported operand")

PYTEST_OUT = """=================================== FAILURES ===================================
__________________________ test_last_partial_page ______________________________
tests/test_paginate.py:9: in test_last_partial_page
    assert len(paginate(list(range(7)), 3)) == 3
E   assert 2 == 3
=========================== short test summary info ============================
FAILED tests/test_paginate.py::test_last_partial_page - assert 2 == 3
"""


def test_error_signature_stable_across_line_numbers_and_paths() -> None:
    assert error_signature(TB_A) == error_signature(TB_A_SHIFTED)


def test_error_signature_changes_with_exception() -> None:
    assert error_signature(TB_A) != error_signature(TB_B)


def test_error_signature_pytest_style() -> None:
    a = error_signature(PYTEST_OUT)
    b = error_signature(PYTEST_OUT.replace(":9:", ":14:"))
    c = error_signature(PYTEST_OUT.replace("assert 2 == 3", "assert 1 == 3"))
    assert a == b and a != c and len(a) == 12


def test_advance_counts_repeats_and_stalls() -> None:
    features = [FeatureItem(id="F1", description="d")]
    p = ProgressLedger(current_step_id="S1")
    p = advance(p, features, error_text=TB_A)
    assert (p.fix_attempts_on_error, p.stall_count, p.iterations) == (1, 0, 1)
    p = advance(p, features, error_text=TB_A_SHIFTED)  # same signature, nothing else changed
    assert (p.fix_attempts_on_error, p.stall_count) == (2, 1)
    p = advance(p, features, error_text=TB_B)  # different error: progress of a sort
    assert (p.fix_attempts_on_error, p.stall_count) == (1, 0)
    features[0].status = FeatureStatus.PASSING
    p = advance(p, features, error_text=None)
    assert p.last_error_signature is None and p.fix_attempts_on_error == 0 and p.stall_count == 0
    assert progress_fingerprint(p, features) == p.last_fingerprint


def test_budget_advisor_caps() -> None:
    b = Budget(
        tokens_max=1,
        usd_max=1,
        advisor_calls_max=2,
        advisor_calls_on_step={"S1": 1},
        advisor_calls_used=1,
    )
    assert not b.advisor_allowed("S1")
    assert b.advisor_allowed("S2")
    b.advisor_calls_used = 2
    assert not b.advisor_allowed("S2")


def test_state_helpers_and_sweep_field() -> None:
    st = SCIOState(
        thread_id="t",
        task=TaskSpec(title="t", description="d", acceptance=["a"], repo_url="r"),
        budgets=Budget(tokens_max=1, usd_max=1, advisor_calls_max=0),
        github=GitHubRefs(owner="o", repo="r"),
        feature_list=[
            FeatureItem(id="F1", description="d"),
            FeatureItem(id="F2", description="d", status=FeatureStatus.PASSING),
        ],
    )
    assert [f.id for f in st.failing_features()] == ["F1"] and not st.all_passing()
    out = WorkerOutput(
        step_id="S1", summary="s", files_touched=[], confidence="high", next_action="verify",
        sweep=SweepRecord(pattern="write_text(", files_checked=["a.py"], files_fixed=["a.py"]),
    )  # fmt: skip
    assert out.sweep is not None and out.sweep.pattern == "write_text("
    assert st.github.full_name == "o/r" and Role.DEBUGGER.value == "debugger"


def _build_state(milestone: int, runs: list[FeatureRun]) -> ProductBuildState:
    plan = MilestonePlan(
        number=milestone,
        title="t",
        branch=f"milestone/{milestone}",
        features=[MilestoneFeatureOutput(id="P1"), MilestoneFeatureOutput(id="P3")],
    )
    return ProductBuildState(
        thread_id="b",
        product_dir="/p",
        milestone=plan,
        feature_runs=runs,
        budgets=Budget(tokens_max=1, usd_max=1, advisor_calls_max=0),
    )


def _run(
    fid: str, attempt: int, status: str, milestone: str | None, usd: float = 0.0
) -> FeatureRun:
    return FeatureRun(
        feature_id=fid,
        thread_id=f"b-{fid.lower()}-a{attempt}",
        attempt=attempt,
        tier=Tier.T1,
        status=status,  # type: ignore[arg-type]
        milestone=milestone,
        usd=usd,
    )


def test_a_skip_finishes_a_feature_only_in_the_milestone_it_was_skipped_in() -> None:
    """I-116: 'skip' defers a feature to the next milestone, and a build thread keeps its runs
    across milestones. A run skipped in M1 used to finish P1 in M2 as well, so the deferred
    feature was planned again and passed over; now it runs again with its next attempt."""
    m1_skip = _run("P1", 1, "skipped", "M1", usd=0.5)
    m1_pass = _run("P2", 1, "passed", "M1", usd=0.25)

    in_m1 = _build_state(1, [m1_skip, m1_pass])
    nxt = in_m1.next_feature()
    assert nxt is not None and nxt.id == "P3"  # skipped in M1, so M1 goes on without P1

    in_m2 = _build_state(2, [m1_skip, m1_pass])
    nxt = in_m2.next_feature()
    assert nxt is not None and nxt.id == "P1"  # deferred to M2: it runs again there
    latest = in_m2.latest_run("P1")
    assert latest is not None and latest.attempt == 1  # so M2's attempt is a2, a new thread
    assert in_m2.milestone_runs() == []  # M1's cost and attempts are not M2's

    m2_skip = _run("P1", 2, "skipped", "M2", usd=0.75)
    again = _build_state(2, [m1_skip, m1_pass, m2_skip])
    nxt = again.next_feature()
    assert nxt is not None and nxt.id == "P3"  # skipped in M2 itself: finished for M2
    assert again.milestone_runs() == [m2_skip]

    done = _build_state(2, [m1_skip, m1_pass, m2_skip, _run("P3", 1, "passed", "M2")])
    assert done.next_feature() is None


def test_runs_recorded_before_the_milestone_field_keep_the_old_rules() -> None:
    """A checkpoint written before I-116 has runs without a milestone: a skip still finishes
    the feature, and the milestone's runs are matched by feature id, as before."""
    legacy = [_run("P1", 1, "skipped", None), _run("P2", 1, "passed", None)]
    st = _build_state(2, legacy)
    nxt = st.next_feature()
    assert nxt is not None and nxt.id == "P3"
    assert st.milestone_runs() == [legacy[0]]
    assert (
        FeatureRun.model_validate(
            {"feature_id": "P1", "thread_id": "t", "tier": "T1", "status": "passed"}
        ).milestone
        is None
    )
