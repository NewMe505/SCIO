"""Token and cost accounting per call, per role and per run."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scio.harness.types import Usage


class ModelPricing(BaseModel):
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0

    def cost_of(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
        ) / 1_000_000


class CostEntry(BaseModel):
    at: datetime
    role: str
    model: str
    provider: str
    usage: Usage
    usd: float
    latency_ms: int
    request_id: str | None = None
    attempts: int = 1
    tags: dict[str, str] = Field(default_factory=dict)


class RoleTotals(BaseModel):
    calls: int = 0
    usage: Usage = Field(default_factory=Usage)
    usd: float = 0.0


class CostLedger:
    """Append-only record of every model call in a run. Thread-safe."""

    def __init__(self, persist_path: Path | None = None) -> None:
        self._entries: list[CostEntry] = []
        self._lock = threading.Lock()
        self.persist_path = persist_path

    def persist_to(self, path: Path) -> None:
        """Append every entry (existing and future) to ``path`` as JSON lines."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.persist_path = path
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as fh:
            for e in self._entries:
                fh.write(e.model_dump_json() + "\n")

    @staticmethod
    def load_entries(path: Path) -> list[CostEntry]:
        if not path.exists():
            return []
        return [
            CostEntry.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def record(
        self,
        *,
        role: str,
        model: str,
        provider: str,
        usage: Usage,
        usd: float,
        latency_ms: int,
        request_id: str | None = None,
        attempts: int = 1,
        tags: dict[str, str] | None = None,
    ) -> CostEntry:
        entry = CostEntry(
            at=datetime.now(UTC),
            role=role,
            model=model,
            provider=provider,
            usage=usage,
            usd=usd,
            latency_ms=latency_ms,
            request_id=request_id,
            attempts=attempts,
            tags=tags or {},
        )
        with self._lock:
            self._entries.append(entry)
            if self.persist_path is not None:
                with self.persist_path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(entry.model_dump_json() + "\n")
        return entry

    @property
    def entries(self) -> list[CostEntry]:
        with self._lock:
            return list(self._entries)

    def totals(self) -> RoleTotals:
        t = RoleTotals()
        for e in self.entries:
            t.calls += 1
            t.usage = t.usage + e.usage
            t.usd += e.usd
        return t

    def totals_by_role(self) -> dict[str, RoleTotals]:
        by: dict[str, RoleTotals] = defaultdict(RoleTotals)
        for e in self.entries:
            t = by[e.role]
            t.calls += 1
            t.usage = t.usage + e.usage
            t.usd += e.usd
        return dict(by)

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals().model_dump(),
            "by_role": {k: v.model_dump() for k, v in self.totals_by_role().items()},
            "entries": [e.model_dump(mode="json") for e in self.entries],
        }
