"""Prompt-cache placement rules.

Caching is a prefix match over ``tools -> system -> messages``. SCIO keeps the system
prompt and tool list byte-stable per role version and marks them as cache breakpoints.
Volatile, task-specific content goes in ``messages`` after the last breakpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CachePolicy:
    system: bool = True
    tools: bool = True
    messages: bool = True
    """Breakpoint on the last message block so a growing transcript is re-read at cache price."""
    ttl: Literal["5m", "1h"] | None = None
    """None means the provider default (5 minutes on Anthropic)."""

    def control(self) -> dict[str, Any]:
        ctl: dict[str, Any] = {"type": "ephemeral"}
        if self.ttl:
            ctl["ttl"] = self.ttl
        return ctl


def worth_caching(prefix_tokens_estimate: int, cache_min_tokens: int) -> bool:
    """Prefixes shorter than the model minimum silently do not cache; skip the marker."""
    return cache_min_tokens == 0 or prefix_tokens_estimate >= cache_min_tokens


def estimate_tokens(chars: int) -> int:
    """Cheap local estimate used for pre-checks. Roughly 4 characters per token."""
    return max(1, (chars + 3) // 4)
