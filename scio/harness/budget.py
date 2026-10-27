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
        """Account actual usage. Tokens are *billable* tokens (cache reads at 0.1x, I-041)."""
        with self._lock:
            self._tokens_used += usage.billable
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


def budget_factor(settings: Any) -> float:
    """How much to scale dollar and token caps for the configured worker backend.

    Budgets in ``budgets.yaml`` are API-price guards. A Claude Code turn reports about four
    times that for the same work (its own prompt and tool set as cache writes and reads,
    ADR 0012 live trial), so the ``claude_code`` backend scales every cap by
    ``settings.claude_code_budget_factor`` (I-069). Nothing is billed either way.
    """
    if getattr(settings, "worker_backend", "aci") == "claude_code":
        return float(getattr(settings, "claude_code_budget_factor", 1.0))
    return 1.0


def token_factor(settings: Any) -> float:
    """How much to scale token caps for the worker backend; differs from the dollar factor.

    A Claude Code turn's tokens are mostly cache reads and writes of its own prompt, cheap per
    token: the live product evals measured about 380,000 billable tokens per estimated dollar
    against 125,000 to 200,000 in ``budgets.yaml``, so scaling tokens by the dollar factor made
    the token cap bind first, at about half a feature's dollar cap (I-099). The ``claude_code``
    backend scales tokens by ``settings.claude_code_token_factor`` so both caps bind together.
    """
    if getattr(settings, "worker_backend", "aci") == "claude_code":
        return float(getattr(settings, "claude_code_token_factor", 1.0))
    return 1.0


class BudgetsConfig(BaseModel):
    tiers: dict[str, BudgetLimits]
    reserve_output_fraction: float = 1.0
    escalation: EscalationSettings = Field(default_factory=EscalationSettings)

    @classmethod
    def load(cls, path: Path) -> BudgetsConfig:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    @classmethod
    def for_settings(cls, settings: Any) -> BudgetsConfig:
        """The tier budgets for a run: ``budgets.yaml`` with dollar caps scaled by
        :func:`budget_factor` and token caps by :func:`token_factor`."""
        return cls.load(settings.budgets_path).scaled(
            budget_factor(settings), tokens=token_factor(settings)
        )

    def scaled(self, factor: float, *, tokens: float | None = None) -> BudgetsConfig:
        """Every tier's dollar caps multiplied by ``factor`` (I-069) and token caps by
        ``tokens`` (the same factor when not given, I-099)."""
        token_scale = factor if tokens is None else tokens
        if factor == 1.0 and token_scale == 1.0:
            return self
        tiers = {
            name: lim.model_copy(
                update={
                    "usd_max": round(lim.usd_max * factor, 4),
                    "tokens_max": int(lim.tokens_max * token_scale),
                }
            )
            for name, lim in self.tiers.items()
        }
        return self.model_copy(update={"tiers": tiers})

    def guard_for(self, tier: str) -> BudgetGuard:
        return BudgetGuard(self.tiers[tier], reserve_output_fraction=self.reserve_output_fraction)
