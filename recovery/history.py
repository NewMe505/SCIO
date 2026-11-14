"""Incident memory: the observed layer (ADR 0019 decision 10). It records what happened and
is not a lesson; lessons are ledger entries and guardrails. OWNER: wave-1 agent C."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from recovery.model import Incident
from recovery.store import Store


def append_jsonl(path: Path, incident: Incident, event: str) -> None:
    """Append one line, ``{"at": ..., "event": event, "incident": incident.to_dict()}``, in
    a single write. Pass ``newline="\\n"`` (guardrail 23)."""
    record = {"at": time.time(), "event": event, "incident": incident.to_dict()}
    # json.dumps escapes every newline inside a value, so one record is one line
    line = json.dumps(record, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every complete line of ``path``.

    A torn last line, written by a racing writer, is left out. A corrupt line in the
    middle raises ``ValueError`` (guardrail 24).

    - Everything after the last newline is a line still being written: left out.
    - The last complete line is left out when it does not parse (a write cut short and
      padded by the filesystem).
    - Any earlier line that does not parse as a JSON object raises ``ValueError`` naming
      the path and the line number. A missing file reads as no lines.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    complete = data[: data.rfind(b"\n") + 1]
    lines = [(n, raw) for n, raw in enumerate(complete.split(b"\n"), 1) if raw.strip()]
    out: list[dict[str, Any]] = []
    for index, (lineno, raw) in enumerate(lines):
        try:
            # decoded per line: a UTF-8 error is a ValueError like a JSON one
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"a {type(value).__name__}, not a JSON object")
        except ValueError as exc:
            if index == len(lines) - 1:
                break
            raise ValueError(f"{path}:{lineno}: corrupt line: {exc}") from exc
        out.append(value)
    return out


def _newest_first(incidents: list[Incident]) -> list[Incident]:
    return sorted(incidents, key=lambda i: (i.created_at, i.incident_id), reverse=True)


def _mentions(store: Store, incident: Incident, needle: str) -> bool:
    fields = (incident.summary, incident.innermost_scio_frame, incident.escalation)
    if any(needle in f.lower() for f in fields):
        return True
    return any(needle in text.lower() for _, text in store.events(incident.incident_id))


def search(
    store: Store, *, signature: str | None = None, text: str | None = None, limit: int = 20
) -> list[Incident]:
    """Incidents newest first, filtered by exact signature and/or a case-insensitive
    substring of the summary, the innermost frame, the escalation or the events."""
    if limit <= 0:
        return []
    candidates = _newest_first(store.incidents(signature=signature or None))
    needle = (text or "").lower()
    out: list[Incident] = []
    for incident in candidates:
        if needle and not _mentions(store, incident, needle):
            continue
        out.append(incident)
        if len(out) >= limit:
            break
    return out


def similar(
    store: Store, signature: str, frame: str, *, exclude: str | None = None, limit: int = 5
) -> list[Incident]:
    """Past incidents with the same signature or the same innermost SCIO frame, newest
    first, without ``exclude``. They go into a repair brief with their ledger ids and PRs.

    An empty signature or frame matches nothing: an incident with no SCIO frame is not
    like every other incident with none."""
    if limit <= 0 or (not signature and not frame):
        return []
    found = [
        i
        for i in store.incidents()
        if i.incident_id != exclude
        and (
            (signature and i.signature == signature) or (frame and i.innermost_scio_frame == frame)
        )
    ]
    return _newest_first(found)[:limit]
