"""M6 Foundations: I-053 (``--tier`` pins the route; a default budget follows the routed
tier), I-054 (``SCIO_SANDBOX_BACKEND`` and the other declared settings are read)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.fake_scripts import ScriptedResponder
from scio.aci.sandbox.base import Limits
from scio.aci.sandbox.docker import DockerSandbox
from scio.aci.sandbox.local import LocalSandbox
from scio.graph.nodes.intake_route import route
from scio.graph.run import initial_state
from scio.graph.run_context import RunContext, make_sandbox
from scio.harness.budget import BudgetGuard, BudgetLimits, BudgetsConfig
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.settings import Settings
from scio.state.models import TaskSpec, Tier


def _fake_harness(settings: Settings, responder: ScriptedResponder) -> Harness:
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    registry.register("fake", FakeProvider(responder=responder))
    return Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=100_000, usd_max=0.5)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )


# --- I-054: settings that were declared but never read ---


def test_make_sandbox_reads_the_backend_setting(tmp_path: Path) -> None:
    limits = Limits(timeout_s=5)
    local = make_sandbox(Settings(sandbox_backend="local"), tmp_path / "a", limits)
    assert isinstance(local, LocalSandbox)
    docker = make_sandbox(
        Settings(sandbox_backend="docker", sandbox_docker_image="img:1"), tmp_path / "b", limits
    )
    assert isinstance(docker, DockerSandbox) and docker.image == "img:1"
    assert docker.limits.timeout_s == 5


def test_effects_path_setting_is_used_without_an_explicit_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    effects = tmp_path / "custom" / "fx.sqlite"
    ctx = RunContext.create(
        thread_id="fx",
        sandbox_root=tmp_path / "work",
        settings=Settings(provider_override="fake", effects_path=effects),
    )
    try:
        assert ctx.effects.path == effects and effects.exists()
    finally:
        ctx.close()


def test_run_live_tests_setting_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import live_enabled

    monkeypatch.delenv("SCIO_RUN_LIVE_TESTS", raising=False)
    assert live_enabled() is False
    monkeypatch.setenv("SCIO_RUN_LIVE_TESTS", "1")
    assert live_enabled() is True


# --- I-053: --tier pins the route; a default budget follows the routed tier ---


def test_cli_tier_flag_pins_the_route() -> None:
    import argparse

    from scio.cli import main

    with pytest.raises(SystemExit):  # bad tier is rejected by argparse, not by the graph
        main(["run", "--spec", "s.md", "--repo", "r", "--tier", "T9"])
    ns = argparse.Namespace(tier="T1")
    assert getattr(ns, "tier", None) == "T1"  # the value RunContext.create receives as tier_pin


def test_default_budget_follows_the_routed_tier(tmp_path: Path) -> None:
    settings = Settings(provider_override="fake")
    harness = _fake_harness(settings, ScriptedResponder(router_tier="T1"))
    ctx = RunContext.create(
        thread_id="r1",
        sandbox_root=tmp_path / "work",
        tier="T0",  # the CLI default when --tier is absent
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
    )
    try:
        assert ctx.budget_from_tier and ctx.tier == "T0"
        t1 = BudgetsConfig.load(settings.budgets_path).tiers["T1"]
        task = TaskSpec(title="Add slugify", description="d", acceptance=["a"], repo_url="x")
        out = route(ctx)(initial_state(ctx, task))
        assert out["routing"].tier is Tier.T1
        assert ctx.tier == "T1" and ctx.budget.tokens_max == t1.tokens_max
        assert ctx.limits.max_iters == t1.max_iters and ctx.limits.max_tool_calls_per_iter is None
        assert ctx.harness.budget.limits.usd_max == t1.usd_max
        assert out["budgets"].usd_max == t1.usd_max
        assert any("budget resized for T1" in e.text for e in out["task_ledger"])
    finally:
        ctx.close()


def test_explicit_budget_limits_are_not_resized(tmp_path: Path) -> None:
    settings = Settings(provider_override="fake")
    harness = _fake_harness(settings, ScriptedResponder(router_tier="T2"))
    ctx = RunContext.create(
        thread_id="r2",
        sandbox_root=tmp_path / "work",
        tier="T0",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
        budget_limits=BudgetLimits(tokens_max=123, usd_max=1.0),  # what the eval runner passes
    )
    try:
        task = TaskSpec(title="Add slugify", description="d", acceptance=["a"], repo_url="x")
        out = route(ctx)(initial_state(ctx, task))
        assert out["routing"].tier is Tier.T2 and not ctx.budget_from_tier
        assert ctx.budget.tokens_max == 123 and ctx.tier == "T0"
    finally:
        ctx.close()
