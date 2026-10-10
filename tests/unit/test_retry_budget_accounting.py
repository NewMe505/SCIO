from __future__ import annotations

import random

import pytest

from scio.harness.accounting import CostLedger, ModelPricing
from scio.harness.budget import BudgetGuard, BudgetLimits, BudgetsConfig
from scio.harness.failures import BudgetExceeded, FatalError, RetryableError
from scio.harness.retry import RetryPolicy, with_retry
from scio.harness.types import Usage
from scio.settings import REPO_ROOT

PRICING = ModelPricing(
    input_per_mtok=1.0, output_per_mtok=5.0, cache_read_per_mtok=0.1, cache_write_per_mtok=1.25
)


def test_retry_retries_retryable_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("boom")
        return "ok"

    out = with_retry(
        fn,
        RetryPolicy(max_attempts=4, base_delay_s=1.0, jitter=False),
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_retry_honors_retry_after_and_gives_up() -> None:
    sleeps: list[float] = []

    def fn() -> None:
        raise RetryableError("rl", retry_after_s=7.0)

    with pytest.raises(RetryableError):
        with_retry(fn, RetryPolicy(max_attempts=3, jitter=False), sleep=sleeps.append)
    assert sleeps == [7.0, 7.0]


def test_retry_does_not_retry_fatal() -> None:
    calls = {"n": 0}

    def fn() -> None:
        calls["n"] += 1
        raise FatalError("no")

    with pytest.raises(FatalError):
        with_retry(fn, RetryPolicy(max_attempts=5), sleep=lambda _s: None)
    assert calls["n"] == 1


def test_pricing_cost() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=2_000_000)
    assert PRICING.cost_of(usage) == pytest.approx(1.0 + 0.5 + 0.2)


def test_budget_guard_refuses_call_that_could_exceed_tokens() -> None:
    g = BudgetGuard(BudgetLimits(tokens_max=1000, usd_max=10.0))
    g.reserve(est_input_tokens=100, max_output_tokens=500, pricing=PRICING)  # ok: 600 <= 1000
    g.record(Usage(input_tokens=100, output_tokens=500), 0.001)
    with pytest.raises(BudgetExceeded) as ei:
        g.reserve(est_input_tokens=100, max_output_tokens=400, pricing=PRICING)  # 600 + 500 > 1000
    assert ei.value.tokens_used == 600


def test_budget_guard_refuses_call_that_could_exceed_usd() -> None:
    g = BudgetGuard(BudgetLimits(tokens_max=10**9, usd_max=0.001))
    with pytest.raises(BudgetExceeded):
        g.reserve(est_input_tokens=10, max_output_tokens=1_000_000, pricing=PRICING)  # $5


def test_budget_reserve_fraction_and_snapshot() -> None:
    g = BudgetGuard(BudgetLimits(tokens_max=1000, usd_max=10.0), reserve_output_fraction=0.5)
    g.reserve(est_input_tokens=100, max_output_tokens=1600, pricing=PRICING)  # 100 + 800 <= 1000
    g.record(Usage(input_tokens=100, output_tokens=50), 0.0004)
    s = g.snapshot()
    assert (s.tokens_used, s.calls, s.tokens_remaining) == (150, 1, 850)
    assert not g.exhausted


def test_budgets_yaml_loads_and_builds_guard() -> None:
    cfg = BudgetsConfig.load(REPO_ROOT / "configs" / "budgets.yaml")
    assert set(cfg.tiers) == {"T0", "T1", "T2"}
    g = cfg.guard_for("T1")
    assert g.limits.advisor_calls_max == 3


def test_cost_ledger_totals_by_role() -> None:
    ledger = CostLedger()
    ledger.record(
        role="coder",
        model="m",
        provider="fake",
        usage=Usage(input_tokens=10, output_tokens=5),
        usd=0.01,
        latency_ms=3,
    )
    ledger.record(
        role="coder",
        model="m",
        provider="fake",
        usage=Usage(input_tokens=10),
        usd=0.02,
        latency_ms=3,
    )
    ledger.record(
        role="gate",
        model="m",
        provider="fake",
        usage=Usage(output_tokens=7),
        usd=0.03,
        latency_ms=3,
    )
    by = ledger.totals_by_role()
    assert by["coder"].calls == 2 and by["coder"].usage.input_tokens == 20
    assert ledger.totals().usd == pytest.approx(0.06)
    assert ledger.to_dict()["totals"]["calls"] == 3
