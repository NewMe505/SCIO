"""M5: A/B role overrides, cost-ledger persistence, run reports, release notes, scenario tasks."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.runner import load_tasks, to_task_like
from scio.graph.report import build_report, write_report
from scio.harness.accounting import CostLedger
from scio.harness.config import ModelsConfig
from scio.harness.core import Harness
from scio.harness.types import Usage
from scio.settings import REPO_ROOT, Settings
from scio.state.models import (
    ArtifactRef,
    Budget,
    EscalationRecord,
    EscalationTrigger,
    ExitReason,
    FeatureItem,
    GitHubRefs,
    LedgerEntry,
    Role,
    Routing,
    SCIOState,
    TaskSpec,
    Tier,
    Verdict,
)

CONFIGS = REPO_ROOT / "evals" / "configs"


def test_role_overrides_change_only_named_roles(config: ModelsConfig) -> None:
    planner_before = config.role("planner").model
    over = config.with_role_overrides({"planner": "fake-model", "coder": {"effort": "low"}})
    assert over.role("planner").model == "fake-model"
    assert (
        over.role("coder").effort == "low"
        and over.role("coder").model == config.role("coder").model
    )
    assert over.role("gate").model == config.role("gate").model
    assert config.role("planner").model == planner_before  # original untouched


@pytest.mark.parametrize("name", ["sonnet_only", "sonnet_opus_advisor", "opus_only"])
def test_ab_configs_load_through_settings(name: str) -> None:
    settings = Settings(provider_override="fake", role_overrides_path=CONFIGS / f"{name}.yaml")
    h = Harness.from_settings(settings)
    models = {role: h.config.role(role).model for role in ("router", "coder", "advisor", "gate")}
    if name == "sonnet_only":
        assert (
            len(set(models.values()) - {models["coder"]}) <= 1
            and models["advisor"] == models["coder"]
        )
    if name == "opus_only":
        assert models["coder"] == models["advisor"] == models["router"]
    if name == "sonnet_opus_advisor":
        assert models["advisor"] != models["coder"]


def test_cost_ledger_persists_and_reloads(tmp_path: Path) -> None:
    ledger = CostLedger()
    ledger.record(
        role="coder", model="m", provider="fake", usage=Usage(input_tokens=1), usd=0.1, latency_ms=1
    )
    path = tmp_path / "ledger" / "t.jsonl"
    ledger.persist_to(path)  # flushes the existing entry
    ledger.record(
        role="gate", model="m", provider="fake", usage=Usage(output_tokens=2), usd=0.2, latency_ms=1
    )
    entries = CostLedger.load_entries(path)
    assert [e.role for e in entries] == ["coder", "gate"] and entries[1].usd == 0.2
    assert CostLedger.load_entries(tmp_path / "missing.jsonl") == []


def _state() -> SCIOState:
    ref = ArtifactRef(kind="brief", path="/a/brief.txt", sha256="x", produced_by=Role.SYSTEM)
    return SCIOState(
        thread_id="run-1",
        phase="done",
        task=TaskSpec(title="Add thing", description="d", acceptance=["a"], repo_url="r"),
        routing=Routing(
            tier=Tier.T1,
            pattern="plan_execute_gate",
            effort_tier="high",
            rationale="r",
            classifier_model="m",
            confidence=0.9,
        ),
        feature_list=[FeatureItem(id="F1", description="a", status="passing", commit_sha="abc")],  # type: ignore[arg-type]
        budgets=Budget(
            tokens_max=1000,
            usd_max=1.0,
            advisor_calls_max=3,
            tokens_used=400,
            usd_used=0.25,
            advisor_calls_used=1,
        ),
        github=GitHubRefs(owner="o", repo="r", branch="agent/x", pr_number=12),
        escalations=[
            EscalationRecord(
                id="E1",
                trigger=EscalationTrigger.REPEATED_ERROR,
                step_id="S1",
                brief=ref,
                verdict=Verdict.CHANGE_APPROACH,
                tokens=300,
                usd=0.05,
                outcome="resolved",
            )
        ],
        task_ledger=[
            LedgerEntry(kind="fact", text="started", source=Role.SYSTEM),
            LedgerEntry(kind="fact", text="done", source=Role.GATE),
        ],
        exit_reason=ExitReason.SUCCESS,
        artifacts={"pr_body": ref},
    )


def test_run_report_markdown_and_files(tmp_path: Path) -> None:
    entries = [
        type("E", (), {})()  # placeholder replaced below
    ]
    ledger = CostLedger()
    ledger.record(
        role="coder",
        model="m",
        provider="fake",
        usage=Usage(input_tokens=100, output_tokens=50),
        usd=0.2,
        latency_ms=1,
    )
    ledger.record(
        role="advisor",
        model="m",
        provider="fake",
        usage=Usage(input_tokens=30, output_tokens=20, cache_read_tokens=5),
        usd=0.05,
        latency_ms=1,
    )
    entries = ledger.entries
    report = build_report(_state(), entries)
    assert report.tier == "T1" and report.exit_reason == "success" and report.pr_number == 12
    assert [r.role for r in report.by_role] == ["coder", "advisor"]  # sorted by cost
    assert report.escalations[0].outcome == "resolved" and report.advisor_calls == "1/3"
    md = report.to_markdown()
    for needle in (
        "# Run run-1: Add thing",
        "## Cost by role",
        "| coder | 1 | 100 |",
        "| E1 | repeated_error |",
        "## Timeline",
        "**gate** done",
        "pr_body",
    ):
        assert needle in md
    json_path, md_path = write_report(report, tmp_path / "reports")
    assert json_path.exists() and md_path.read_text(encoding="utf-8").startswith("# Run run-1")


def test_release_notes_sections_and_cut() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "release_notes", REPO_ROOT / "scripts" / "release_notes.py"
    )
    assert spec and spec.loader
    rn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rn)
    text = (
        "# Changelog\n\n## [Unreleased]\n### Added\n- new thing\n\n"
        "## [0.0.1] - 2026-01-01\n### Added\n- first\n"
    )
    assert rn.sections(text)["0.0.1"].startswith("### Added\n- first")
    assert rn.notes_for("Unreleased", text) == "### Added\n- new thing\n"
    cut_text = rn.cut("0.1.0", text, today="2026-10-10")
    assert "## [Unreleased]\n\n## [0.1.0] - 2026-10-10\n### Added\n- new thing" in cut_text
    assert rn.notes_for("0.1.0", cut_text).startswith("### Added\n- new thing")
    with pytest.raises(ValueError):
        rn.cut("0.0.1", text)
    with pytest.raises(KeyError):
        rn.notes_for("9.9.9", text)
    assert rn.notes_for("Unreleased")  # the repo changelog has an Unreleased section


def test_capability_scenario_tasks_reuse_fixtures() -> None:
    tasks = {t.id: t for t in load_tasks(suite="capability")}
    assert set(tasks) >= {"c001_escalation_resolved", "c002_plan_drift", "c003_advisor_cap"}
    c1 = tasks["c001_escalation_resolved"]
    assert c1.fixture == "t002_fix_failing_test" and (c1.fixture_path / "repo" / "pager").is_dir()
    like = to_task_like(c1)
    assert (
        like.scenario == {"coder_mode": "wrong_first"}
        and like.expect_pass
        and like.path == c1.fixture_path
    )
    assert not tasks["c003_advisor_cap"].expect_pass
    assert all(t.suite == "regression" for t in load_tasks(suite="regression"))
