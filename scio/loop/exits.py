"""Exit conditions. Evaluated after every verify; the graph routes on the result.

``success`` is decided by the feature list (all passing), not here. This module answers
"must we stop even though we are not done?": ``max_iters``, ``budget``, ``stall``.
"""

from __future__ import annotations

from dataclasses import dataclass

from scio.state.models import Budget, ExitReason, ProgressLedger


@dataclass(frozen=True)
class LoopLimits:
    max_iters: int = 30
    max_stall: int = 2
    max_tool_calls_per_iter: int | None = None
    """T0: 10. None means unlimited (bounded by model calls instead)."""


def limits_for(budget: Budget, *, tool_cap: int | None = None) -> LoopLimits:
    return LoopLimits(
        max_iters=budget.max_iters, max_stall=budget.max_stall, max_tool_calls_per_iter=tool_cap
    )


def check_exit(progress: ProgressLedger, budget: Budget, limits: LoopLimits) -> ExitReason | None:
    if budget.exhausted:
        return ExitReason.BUDGET
    if progress.iterations >= limits.max_iters:
        return ExitReason.MAX_ITERS
    if progress.stall_count > limits.max_stall:
        return ExitReason.STALL
    return None
