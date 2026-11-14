"""Incident memory: the observed layer (ADR 0019 decision 10). It records what happened and
is not a lesson; lessons are ledger entries and guardrails. OWNER: wave-1 agent C."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recovery.model import Incident
from recovery.store import Store


def append_jsonl(path: Path, incident: Incident, event: str) -> None:
    """Append one line, ``{"at": ..., "event": event, "incident": incident.to_dict()}``, in
    a single write. Pass ``newline="\\n"`` (guardrail 23)."""
    raise NotImplementedError


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every complete line of ``path``.

    A torn last line, written by a racing writer, is left out. A corrupt line in the
    middle raises ``ValueError`` (guardrail 24).
    """
    raise NotImplementedError


def search(
    store: Store, *, signature: str | None = None, text: str | None = None, limit: int = 20
) -> list[Incident]:
    """Incidents newest first, filtered by exact signature and/or a case-insensitive
    substring of the summary, the innermost frame, the escalation or the events."""
    raise NotImplementedError


def similar(
    store: Store, signature: str, frame: str, *, exclude: str | None = None, limit: int = 5
) -> list[Incident]:
    """Past incidents with the same signature or the same innermost SCIO frame, newest
    first, without ``exclude``. They go into a repair brief with their ledger ids and PRs."""
    raise NotImplementedError
