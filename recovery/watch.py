"""``python -m recovery watch``: an incident and its repair session, live (issue #107).

It prints the incident's header and timeline from the store. While a repair session works
in the incident's worktree, it also prints that session's steps (each tool call and each
thing the session says), read from Claude Code's transcript of the worktree.

The transcript is Claude Code's own file, not a contract. It is found under the Claude
config directory (``CLAUDE_CONFIG_DIR`` or ``~/.claude``) in ``projects/<key>``, where the
key is the worktree path with every ``:``, ``/``, backslash and ``.`` written as ``-``.
A missing or unreadable transcript, or a line in a shape this module does not know, is
shown as such or skipped, and never fails the command.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from recovery.model import TERMINAL, Incident
from recovery.scrub import scrub
from recovery.store import Store

STEP_CHARS = 110
"""How much of one step's text a line shows."""
_KEY_CHARS = re.compile(r"[:\\/.]")


@dataclass(frozen=True)
class Step:
    stamp: str
    """``HH:MM:SS`` from the transcript, or "" when the line has none."""
    kind: str
    """The tool's name (``Bash``, ``Read`` ...), or ``says`` for the session's own text."""
    text: str


def claude_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def project_key(path: str | Path) -> str:
    """Claude Code's directory name for a working directory: ``C:\\a\\.b`` -> ``C--a--b``."""
    return _KEY_CHARS.sub("-", str(path))


def transcript_for(worktree: str | Path, env: Mapping[str, str] | None = None) -> Path | None:
    """The newest session transcript of ``worktree``, or None.

    The exact key is tried first. If it is absent, the lookup falls back to the one
    project directory whose name ends with the worktree's own key, so a spelling of the
    parent path that differs (a drive letter's case, a short name) still finds it."""
    projects = claude_home(env) / "projects"
    candidates = [projects / project_key(worktree)]
    if not candidates[0].is_dir() and projects.is_dir():
        tail = project_key(Path(worktree).name)
        candidates = sorted(p for p in projects.iterdir() if p.is_dir() and p.name.endswith(tail))
    files = [f for d in candidates if d.is_dir() for f in d.glob("*.jsonl")]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _first_line(text: str) -> str:
    line = next((ln for ln in str(text).splitlines() if ln.strip()), "")
    line = line.strip()
    return line if len(line) <= STEP_CHARS else line[: STEP_CHARS - 3] + "..."


def _tool_text(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("description", "command", "file_path", "pattern", "url", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(tool_input, sort_keys=True)[:STEP_CHARS]


def session_steps(path: Path) -> list[Step]:
    """Every step in a transcript, oldest first.

    Only complete lines are read: a torn last line, still being written, is left out
    (guardrail 24). Lines in other shapes are skipped. Text is scrubbed of secrets."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    complete = data[: data.rfind(b"\n") + 1].decode("utf-8", errors="replace")
    steps: list[Step] = []
    for raw in complete.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        stamp = str(event.get("timestamp") or "")[11:19]
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                text = _tool_text(part.get("input"))
                steps.append(Step(stamp, str(part.get("name") or "tool"), scrub(_first_line(text))))
            elif part.get("type") == "text" and str(part.get("text") or "").strip():
                steps.append(Step(stamp, "says", scrub(_first_line(str(part["text"])))))
    return steps


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%H:%M:%S")


def header(incident: Incident) -> list[str]:
    issue = f"#{incident.issue_number}" if incident.issue_number is not None else "-"
    pr = f"#{incident.pr_number}" if incident.pr_number is not None else "-"
    lines = [
        f"incident {incident.incident_id}: {incident.state.value} ({incident.failure_class.value})",
        f"  issue {issue}  PR {pr}  attempts {incident.attempts}  replays {incident.replays}",
    ]
    if incident.innermost_scio_frame:
        lines.append(f"  frame {incident.innermost_scio_frame}")
    if incident.branch:
        lines.append(f"  branch {incident.branch}")
    if incident.escalation:
        lines.append(f"  escalation: {incident.escalation}")
    return lines


def event_line(at: float, text: str) -> str:
    return f"  {_when(at)}  {_first_line(text)}"


def step_line(step: Step) -> str:
    return f"  {step.stamp or '--:--:--'}  {step.kind}: {step.text}"


def render(
    store: Store, incident_id: str, *, last: int = 20, env: Mapping[str, str] | None = None
) -> list[str]:
    """The whole picture at one moment. Raises ``KeyError`` for an unknown incident."""
    incident = store.get_incident(incident_id)
    if incident is None:
        raise KeyError(incident_id)
    lines = [*header(incident), "timeline:"]
    lines += [event_line(at, text) for at, text in store.events(incident_id)] or ["  (none)"]
    if incident.worktree:
        transcript = transcript_for(incident.worktree, env)
        if transcript is None:
            lines.append(f"repair session: no transcript yet for {incident.worktree}")
        else:
            steps = session_steps(transcript)
            shown = steps[-last:] if last > 0 else []
            lines.append(f"repair session ({len(steps)} steps, the last {len(shown)}):")
            lines += [step_line(s) for s in shown]
    return lines


def follow(
    store: Store,
    incident_id: str,
    *,
    out: TextIO,
    last: int = 20,
    interval_s: float = 5.0,
    env: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_rounds: int | None = None,
) -> int:
    """Print :func:`render` once, then only what is new (state changes, timeline events,
    session steps) every ``interval_s``, until the incident is closed or escalated, or
    ``max_rounds`` is reached. Returns the number of rounds polled."""
    for line in render(store, incident_id, last=last, env=env):
        print(line, file=out, flush=True)
    seen_events = len(store.events(incident_id))
    incident = store.get_incident(incident_id)
    assert incident is not None
    state = incident.state
    transcript = transcript_for(incident.worktree, env) if incident.worktree else None
    seen_steps = len(session_steps(transcript)) if transcript else 0
    rounds = 0
    while incident.state not in TERMINAL and (max_rounds is None or rounds < max_rounds):
        sleep(interval_s)
        rounds += 1
        incident = store.get_incident(incident_id) or incident
        events = store.events(incident_id)
        for at, text in events[seen_events:]:
            print(event_line(at, text), file=out, flush=True)
        seen_events = len(events)
        if incident.state is not state:
            print(f"state: {state.value} -> {incident.state.value}", file=out, flush=True)
            state = incident.state
        if incident.worktree:
            now = transcript_for(incident.worktree, env)
            if now != transcript:
                transcript, seen_steps = now, 0  # a new attempt starts a new session
            steps = session_steps(transcript) if transcript else []
            for step in steps[seen_steps:]:
                print(step_line(step), file=out, flush=True)
            seen_steps = len(steps)
    if incident.state in TERMINAL:
        print(f"incident {incident_id} is {incident.state.value}", file=out, flush=True)
    return rounds
