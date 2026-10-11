"""Budgets count billable-equivalent tokens so prompt caching lowers the count (I-041)."""

from __future__ import annotations

from scio.harness.accounting import ModelPricing
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.types import Usage

PRICING = ModelPricing(
    input_per_mtok=1.0, output_per_mtok=5.0, cache_read_per_mtok=0.1, cache_write_per_mtok=1.25
)


def test_billable_weights_cache_reads_and_writes() -> None:
    u = Usage(
        input_tokens=100, output_tokens=50, cache_read_tokens=10_000, cache_write_tokens=1_000
    )
    assert u.total == 11_150
    assert u.billable == 100 + 50 + 1_000 + 1_250


def test_budget_counts_billable_tokens() -> None:
    g = BudgetGuard(BudgetLimits(tokens_max=10_000, usd_max=10.0))
    g.record(Usage(input_tokens=64, output_tokens=400, cache_read_tokens=50_000), 0.01)
    snap = g.snapshot()
    assert snap.tokens_used == 64 + 400 + 5_000  # not 50,464
    assert not g.exhausted
    g.reserve(est_input_tokens=1_000, max_output_tokens=2_000, pricing=PRICING)  # still room
