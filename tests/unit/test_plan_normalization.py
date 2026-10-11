"""Plan steps that change nothing are dropped; uncovered features get a step (ledger I-039)."""

from __future__ import annotations

from scio.graph.nodes.planning import normalize_steps
from scio.state.models import PlanStep


def _step(id_: str, desc: str, feats: list[str], files: list[str] | None = None) -> PlanStep:
    return PlanStep(id=id_, description=desc, feature_ids=feats, files_expected=files or [])


def test_drops_explore_only_and_featureless_steps() -> None:
    steps = [
        _step("S1", "Explore only, no code changes. Locate tool/cli.py", []),
        _step("S2", "Add --json flag with tests", ["F1", "F2"], ["tool/cli.py"]),
        _step("S3", "Investigate how tests are run", ["F2"]),
    ]
    out, notes = normalize_steps(steps, ["F1", "F2"])
    assert [s.id for s in out] == ["S2"]
    assert any("S1" in n for n in notes) and any("S3" in n for n in notes)


def test_uncovered_features_get_a_final_step() -> None:
    steps = [_step("S1", "Add area_circle with tests", ["F1"], ["shapes/__init__.py"])]
    out, notes = normalize_steps(steps, ["F1", "F2"])
    assert [s.id for s in out] == ["S1", "S2"]
    assert out[1].feature_ids == ["F2"] and out[1].files_expected == ["shapes/__init__.py"]
    assert any("F2" in n for n in notes)


def test_well_formed_plan_is_untouched() -> None:
    steps = [
        _step("S1", "Fix the range bound", ["F1"], ["pager/__init__.py"]),
        _step("S2", "Add rect helper with tests", ["F2"], ["shapes/__init__.py"]),
    ]
    out, notes = normalize_steps(steps, ["F1", "F2"])
    assert out == steps and notes == []


def test_empty_plan_gets_one_step_for_everything() -> None:
    out, notes = normalize_steps([], ["F1", "F2"])
    assert len(out) == 1 and out[0].feature_ids == ["F1", "F2"] and out[0].id == "S1"
    assert notes
