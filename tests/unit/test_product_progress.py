"""Product memory (ADR 0011): PRODUCT.md round trip, progress.json, the milestone
normaliser, and the typed handoff from a product feature to a task spec."""

from __future__ import annotations

import pytest

from scio.product.build import (
    _not_done,
    _open_features,
    feature_task,
    issue_body,
    issue_title,
)
from scio.product.progress import (
    MAX_FEATURES_PER_MILESTONE,
    ProgressFile,
    normalise_milestone,
    parse_product_md,
)
from scio.product.spec import render_product_md
from scio.state.models import (
    MilestoneFeatureOutput,
    MilestonePlan,
    MilestonePlanOutput,
    ProductFeature,
    ProductSpec,
    Tier,
)


def _spec(n: int = 4) -> ProductSpec:
    return ProductSpec(
        name="todo_cli",
        title="Todo CLI",
        vision="A command-line todo list.",
        users=["people in a terminal", "scripts"],
        features=[
            ProductFeature(id=f"P{i}", description=f"Feature {i}", acceptance=[f"c{i}a", f"c{i}b"])
            for i in range(1, n + 1)
        ],
        quality_bar=["tests pass", "lint clean"],
        dependencies=["click"],
    )


def test_product_md_round_trips_through_render_and_parse() -> None:
    spec = _spec()
    parsed = parse_product_md(render_product_md(spec))
    assert parsed == spec


def test_product_md_edited_by_hand_still_parses() -> None:
    text = render_product_md(_spec(2)).replace("- [ ] c2b", "- [x] c2b edited")
    text += "\n### P3: Added by hand\n- [ ] prints hello\n"
    # a feature appended after the dependencies section is outside "## Features": ignored
    parsed = parse_product_md(text)
    assert [f.id for f in parsed.features] == ["P1", "P2"]
    assert parsed.features[1].acceptance == ["c2a", "c2b edited"]
    with pytest.raises(ValueError):
        parse_product_md("# Nothing here\n")


def test_progress_file_starts_every_feature_todo_and_syncs_with_product_md() -> None:
    spec = _spec(3)
    progress = ProgressFile.initial(spec, bootstrap_commit="abc123")
    assert [f.status for f in progress.features] == ["todo", "todo", "todo"]
    assert progress.milestones == [] and progress.next_milestone_number() == 1
    assert progress.open_feature_ids() == ["P1", "P2", "P3"]
    again = ProgressFile.from_json(progress.to_json())
    assert again.features == progress.features and again.updated
    notes = again.sync_features(_spec(4))
    assert again.feature("P4") is not None and "P4 added" in notes[0]
    notes = again.sync_features(_spec(2))
    assert [f.id for f in again.features] == ["P1", "P2"] and "removed" in notes[0]


def test_a_skipped_feature_stays_open_and_is_planned_first_again() -> None:
    """I-116: 'skip' at a halted feature defers it, it does not drop it. ``skipped`` used to
    leave the open set, so the next milestone never planned the feature and the release went
    out without it. Now it is open, keeps its PRODUCT.md place, and a planner cannot route
    around it; only ``done`` is finished."""
    spec = _spec(4)
    progress = ProgressFile.initial(spec, bootstrap_commit=None)
    for fid, status in (("P1", "skipped"), ("P2", "done"), ("P3", "failed")):
        rec = progress.feature(fid)
        assert rec is not None
        rec.status = status  # type: ignore[assignment]
    assert progress.open_feature_ids() == ["P1", "P3", "P4"]
    open_features = _open_features(spec, progress)  # what plan_milestone hands the normaliser
    assert [f.id for f in open_features] == ["P1", "P3", "P4"]

    out = MilestonePlanOutput(
        milestone_title="Next",
        features=[MilestoneFeatureOutput(id="P1"), MilestoneFeatureOutput(id="P3")],
        rationale="r",
    )
    plan, _ = normalise_milestone(out, open_features, 2)
    assert plan.feature_ids() == ["P1", "P3"]

    # a planner that leaves the deferred feature out gets it alone, not the features after it
    out = MilestonePlanOutput(
        milestone_title="Next",
        features=[MilestoneFeatureOutput(id="P3"), MilestoneFeatureOutput(id="P4")],
        rationale="r",
    )
    plan, notes = normalise_milestone(out, open_features, 2)
    assert plan.feature_ids() == ["P1"] and any("alone" in n for n in notes)

    # the release guard names what is not done; a feature with no record counts too
    progress.features = [f for f in progress.features if f.id != "P4"]
    assert _not_done(spec, progress) == ["P1 skipped", "P3 failed", "P4 no record"]
    for rec in progress.features:
        rec.status = "done"
    assert progress.open_feature_ids() == [] and _not_done(_spec(3), progress) == []


def test_milestone_normaliser_keeps_a_consecutive_prefix_in_product_order() -> None:
    spec = _spec(6)
    open_features = spec.features[1:]  # P1 done
    out = MilestonePlanOutput(
        milestone_title="  Core commands ",
        features=[
            MilestoneFeatureOutput(id="P4", tier=Tier.T2),
            MilestoneFeatureOutput(id="P2", tier=Tier.T0, notes="small"),
            MilestoneFeatureOutput(id="P9"),
            MilestoneFeatureOutput(id="P3"),
        ],
        rationale="r",
    )
    plan, notes = normalise_milestone(out, open_features, 2)
    assert plan.id == "M2" and plan.branch == "milestone/2" and plan.title == "Core commands"
    assert plan.feature_ids() == ["P2", "P3", "P4"]
    assert [f.tier for f in plan.features] == [Tier.T0, Tier.T1, Tier.T2]
    assert plan.features[0].notes == "small"
    assert any("P9" in n for n in notes)

    # a gap: the milestone stops before it
    out = MilestonePlanOutput(
        milestone_title="t",
        features=[MilestoneFeatureOutput(id="P2"), MilestoneFeatureOutput(id="P5")],
        rationale="r",
    )
    plan, notes = normalise_milestone(out, open_features, 2)
    assert plan.feature_ids() == ["P2"] and any("gap" in n for n in notes)

    # nothing runnable: the first open feature alone at T1
    out = MilestonePlanOutput(
        milestone_title="t", features=[MilestoneFeatureOutput(id="P6")], rationale="r"
    )
    plan, notes = normalise_milestone(out, open_features, 2)
    assert plan.feature_ids() == ["P2"] and plan.features[0].tier is Tier.T1
    assert any("alone" in n for n in notes)

    # the cap
    big = MilestonePlanOutput(
        milestone_title="t",
        features=[MilestoneFeatureOutput(id=f"P{i}") for i in range(2, 7)],
        rationale="r",
    )
    plan, notes = normalise_milestone(big, open_features, 2)
    assert len(plan.features) == MAX_FEATURES_PER_MILESTONE and any("capped" in n for n in notes)
    with pytest.raises(ValueError):
        normalise_milestone(big, [], 2)


def test_feature_task_is_the_typed_handoff_to_the_task_graph() -> None:
    spec = _spec(3)
    plan = MilestonePlan(
        number=1,
        title="Skeleton and core",
        branch="milestone/1",
        features=[
            MilestoneFeatureOutput(id="P1", tier=Tier.T0),
            MilestoneFeatureOutput(id="P2", tier=Tier.T1, notes="keep the store in one module"),
        ],
    )
    task = feature_task(spec, plan, plan.features[1], 42, repo_dir="/work/todo")
    assert task.title == "P2: Feature 2" and task.acceptance == ["c2a", "c2b"]
    assert task.base_branch == "milestone/1" and task.issue_number == 42
    assert task.repo_url == "/work/todo" and task.tier_hint is Tier.T1
    assert "keep the store in one module" in task.description
    assert "M1 (Skeleton and core) builds P1, P2" in task.description
    assert "do not edit them" in task.description
    f = spec.features[1]
    assert issue_title(f) == "P2: Feature 2"
    body = issue_body(spec, plan, f, "T1")
    assert "- [ ] c2a" in body and "<!-- scio:feature:P2 -->" in body and "M1" in body


def test_walking_skeleton_criteria_never_need_an_installed_entry_point() -> None:
    """M9: two live planners asked for `todo --version` through a console script that no
    sandbox installs; the normaliser rewrites such P1 criteria to the module form."""
    from scio.product.spec import normalise_spec
    from scio.state.models import ProductSpecOutput

    out = ProductSpecOutput(
        product_name="tagtodo",
        title="t",
        vision="v",
        users=["u"],
        features=[
            ProductFeature(
                id="P1",
                description="Walking skeleton with a `tagtodo` console entry point",
                acceptance=[
                    "`tagtodo --version` prints exactly `tagtodo 0.1.0` and exits 0",
                    "`python -m tagtodo --version` prints the version and exits 0",
                    "The console script is installed with pip install -e .",
                    "tests/test_smoke.py passes under pytest",
                ],
            ),
            ProductFeature(id="P2", description="add", acceptance=["`tagtodo add x` stores x"]),
        ],
        quality_bar=["q"],
        stack="python",
    )
    spec, notes = normalise_spec(out)
    p1 = spec.features[0]
    assert p1.acceptance == [
        "`python -m tagtodo --version` prints the version and exits 0",
        "tests/test_smoke.py passes under pytest",
    ]
    assert any("2 rewritten" in n for n in notes)
    assert spec.features[1].acceptance == ["`tagtodo add x` stores x"]  # later features untouched


@pytest.mark.parametrize(
    ("stack", "bar", "kept", "replaced"),
    [
        (
            "python",
            [
                "Tests use only the stdlib unittest module (python -m unittest discover)",
                "Targets Python 3.11 or higher",
                "Zero third-party test packages",
                "lint clean with ruff",
                "Runs on Python 3.12 and 3.13",
            ],
            ["lint clean with ruff", "Runs on Python 3.12 and 3.13"],
            3,
        ),
        ("node-ts", ["tests pass under jest", "Node 20 support", "tsc --noEmit is clean"], 1, 2),
    ],
)
def test_a_quality_bar_never_promises_tools_the_profile_does_not_use(
    stack: str, bar: list[str], kept: list[str] | int, replaced: int
) -> None:
    """I-107: the graphcalc interview offered "pytest or only unittest?" and the approved bar
    said unittest and Python 3.11; the python profile's skeleton, CI and pyproject use pytest
    and 3.12. A line that promises other tools is replaced by the profile's fact, quoting the
    original, so the owner sees the change in the draft they approve."""
    from scio.product.spec import normalise_spec
    from scio.state.models import ProductSpecOutput

    out = ProductSpecOutput(
        product_name="graphcalc",
        title="t",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["it starts"])],
        quality_bar=bar,
        stack=stack,
    )
    spec, notes = normalise_spec(out)
    swapped = [q for q in spec.quality_bar if "(replaces: " in q]
    assert len(swapped) == replaced and len(spec.quality_bar) == len(bar)
    assert sum("contradicts the" in n for n in notes) == replaced
    if isinstance(kept, list):
        assert all(k in spec.quality_bar for k in kept)  # 3.12+ and ruff are the profile's own
    if stack == "python":
        assert spec.quality_bar[0].startswith("Tests run with pytest")
        assert spec.quality_bar[1].startswith("Targets Python 3.12 or newer")
        assert "Targets Python 3.11 or higher" in spec.quality_bar[1]  # the original, quoted


def test_the_interviewer_and_planner_see_what_each_stack_fixes() -> None:
    """I-107: the stack menu names each profile's fixed tools, and both prompts render it."""
    from scio.product.spec import stack_menu
    from scio.prompt.registry import PromptRegistry

    menu = stack_menu()
    assert "pytest" in menu and "Python 3.12 or newer" in menu and "node --test" in menu
    reg = PromptRegistry()
    for role in ("interviewer", "product_planner"):
        text = reg.get(role).render(stacks=menu)
        assert "pytest" in text and "{{" not in text, role
