"""Budget guard: checked before every call, updated after every call.

The pre-check is conservative. It assumes the call will use its full ``max_tokens``
(scaled by ``reserve_output_fraction``) and refuses the call if that *could* exceed the
cap. The post-call update records what was actually used.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from scio.harness.accounting import ModelPricing
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Usage


class BudgetLimits(BaseModel):
    tokens_max: int
    usd_max: float
    advisor_calls_max: int = 0
    max_iters: int = 30
    max_stall: int = 2


class BudgetSnapshot(BaseModel):
    tokens_max: int
    tokens_used: int
    usd_max: float
    usd_used: float
    calls: int

    @property
    def tokens_remaining(self) -> int:
        return max(self.tokens_max - self.tokens_used, 0)

    @property
    def usd_remaining(self) -> float:
        return max(self.usd_max - self.usd_used, 0.0)


class BudgetGuard:
    """Thread-safe token and dollar budget for one run."""

    def __init__(self, limits: BudgetLimits, *, reserve_output_fraction: float = 1.0) -> None:
        self.limits = limits
        self.reserve_output_fraction = reserve_output_fraction
        self._tokens_used = 0
        self._usd_used = 0.0
        self._calls = 0
        self._lock = threading.Lock()

    def reserve(
        self, *, est_input_tokens: int, max_output_tokens: int, pricing: ModelPricing
    ) -> None:
        """Raise :class:`BudgetExceeded` if this call could push the run over either cap."""
        reserved_out = int(max_output_tokens * self.reserve_output_fraction)
        est_usage = Usage(input_tokens=est_input_tokens, output_tokens=reserved_out)
        est_usd = pricing.cost_of(est_usage)
        with self._lock:
            tokens_after = self._tokens_used + est_usage.total
            usd_after = self._usd_used + est_usd
            if tokens_after > self.limits.tokens_max:
                raise BudgetExceeded(
                    f"token budget: {self._tokens_used} used + {est_usage.total} reserved "
                    f"> {self.limits.tokens_max}",
                    tokens_used=self._tokens_used,
                    usd_used=self._usd_used,
                )
            if usd_after > self.limits.usd_max:
                raise BudgetExceeded(
                    f"usd budget: {self._usd_used:.4f} used + {est_usd:.4f} reserved "
                    f"> {self.limits.usd_max:.4f}",
                    tokens_used=self._tokens_used,
                    usd_used=self._usd_used,
                )

    def record(self, usage: Usage, usd: float) -> None:
        with self._lock:
            self._tokens_used += usage.total
            self._usd_used += usd
            self._calls += 1

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                tokens_max=self.limits.tokens_max,
                tokens_used=self._tokens_used,
                usd_max=self.limits.usd_max,
                usd_used=self._usd_used,
                calls=self._calls,
            )

    @property
    def exhausted(self) -> bool:
        s = self.snapshot()
        return s.tokens_used >= s.tokens_max or s.usd_used >= s.usd_max


class EscalationSettings(BaseModel):
    repeat_threshold: int = 3
    max_stall: int = 2
    high_impact_files: int = 8
    sample_n: int = 1
    explorers_min: int = 2
    explorers_max: int = 4


class BudgetsConfig(BaseModel):
    tiers: dict[str, BudgetLimits]
    reserve_output_fraction: float = 1.0
    escalation: EscalationSettings = Field(default_factory=EscalationSettings)

    @classmethod
    def load(cls, path: Path) -> BudgetsConfig:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def guard_for(self, tier: str) -> BudgetGuard:
        return BudgetGuard(self.tiers[tier], reserve_output_fraction=self.reserve_output_fraction)
