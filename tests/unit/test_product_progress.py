"""Product memory (ADR 0011): PRODUCT.md round trip, progress.json, the milestone
normaliser, and the typed handoff from a product feature to a task spec."""

from __future__ import annotations

import pytest

from scio.product.build import feature_task, issue_body, issue_title
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
