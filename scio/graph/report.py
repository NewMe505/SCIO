"""Per-run observability report: what happened, what it cost, where the time went.

Built from the durable checkpoint state and the persisted cost ledger, so it works after
the process that ran the task is gone (``scio report --thread <id>``).
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scio.harness.accounting import CostEntry
from scio.state.models import ArtifactRef, Role, SCIOState

TOOL_USE_RE = re.compile(r"^assistant: <tool_use (?P<name>\w+) (?P<input>\{.*\})>$")
TOOL_RESULT_RE = re.compile(r"^tool_result(?P<error> \(error\))?: ")
DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S+) b/", re.MULTILINE)
DOC_PATH_RE = re.compile(r"(^|/)(README|CHANGELOG|CONTRIBUTING)[^/]*$|(^|/)docs/|\.(md|rst|txt)$")
WORKER_ROLES = (Role.CODER, Role.DEBUGGER, Role.DOCUMENTER)


class ToolRow(BaseModel):
    tool: str
    calls: int
    errors: int

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0


class ToolSignals(BaseModel):
    """Cheap signals from the worker transcripts (``docs/IDEAS_TRIAGE.md`` items 9, 10, 12).

    No model call: every number is counted from the ``log`` artifacts the coder, debugger
    and documenter turns leave behind.
    """

    turns: int = 0
    """Transcripts parsed."""
    by_tool: list[ToolRow] = Field(default_factory=list)
    repeated_calls: int = 0
    """Identical (tool, input) calls within one turn, beyond the first."""
    repeated_examples: list[str] = Field(default_factory=list)
    rereads: int = 0
    """``view_file`` calls on a path the same turn already viewed (paging included)."""
    docs_touched: list[str] = Field(default_factory=list)
    """Files the documenter edited that look like documentation."""
    code_changed: bool | None = None
    """The final diff touched non-documentation files (None without a ``diff:final``)."""

    @property
    def documenter_completed(self) -> bool | None:
        """None when nothing says either way; False when code changed and no docs were edited."""
        if self.code_changed is None or not self.turns:
            return None
        return bool(self.docs_touched) if self.code_changed else None

    def to_lines(self) -> list[str]:
        lines = [
            "## Tool signals",
            "| tool | calls | errors | error rate |",
            "|---|---:|---:|---:|",
            *[f"| {r.tool} | {r.calls} | {r.errors} | {r.error_rate:.2f} |" for r in self.by_tool],
        ]
        if not self.by_tool:
            lines.append("| none | | | |")
        examples = f" (e.g. {'; '.join(self.repeated_examples)})" if self.repeated_examples else ""
        lines += [
            f"- repeated identical calls: {self.repeated_calls}{examples}",
            f"- re-reads (view_file on a path already viewed in the turn): {self.rereads}",
        ]
        done = self.documenter_completed
        if done is True:
            lines.append(f"- documenter: touched {', '.join(self.docs_touched)}")
        elif done is False:
            lines.append("- documenter: code changed but no documentation file was edited")
        else:
            lines.append("- documenter: n/a (no code change to document, or no transcript)")
        return lines


def parse_transcript(text: str) -> list[tuple[str, dict[str, Any], bool]]:
    """``(tool, input, is_error)`` per call, in order, from an ``AgentLoop`` transcript.

    Tool results follow their calls in order within a turn, so the k-th result header
    after a block of calls belongs to the k-th call of that block.
    """
    calls: list[tuple[str, dict[str, Any], bool]] = []
    pending: list[int] = []
    for line in text.splitlines():
        m = TOOL_USE_RE.match(line)
        if m:
            try:
                inp = ast.literal_eval(m.group("input"))
            except (ValueError, SyntaxError):
                inp = {"raw": m.group("input")}
            calls.append((m.group("name"), inp if isinstance(inp, dict) else {"raw": inp}, False))
            pending.append(len(calls) - 1)
            continue
        r = TOOL_RESULT_RE.match(line)
        if r and pending:
            i = pending.pop(0)
            if r.group("error"):
                name, inp, _ = calls[i]
                calls[i] = (name, inp, True)
    return calls


def _is_doc(path: str) -> bool:
    return bool(DOC_PATH_RE.search(path.replace("\\", "/")))


def tool_signals(
    state: SCIOState, read: Callable[[ArtifactRef], str | None] | None = None
) -> ToolSignals:
    read = read or _read_artifact
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    repeated = 0
    examples: list[str] = []
    rereads = 0
    docs_touched: list[str] = []
    turns = 0
    for key, ref in sorted(state.artifacts.items()):
        if ref.kind != "log" or ref.produced_by not in WORKER_ROLES:
            continue
        text = read(ref)
        if text is None:
            continue
        turns += 1
        seen: Counter[str] = Counter()
        viewed: set[str] = set()
        for name, inp, is_error in parse_transcript(text):
            counts[name] += 1
            errors[name] += int(is_error)
            sig = f"{name} {json.dumps(inp, sort_keys=True, default=str)}"
            seen[sig] += 1
            if seen[sig] == 2 and len(examples) < 3:
                examples.append(f"{key}: {sig[:120]}")
            if seen[sig] > 1:
                repeated += 1
            if name == "view_file":
                path = str(inp.get("path", "")).replace("\\", "/")
                if path in viewed:
                    rereads += 1
                viewed.add(path)
            if (
                name == "edit_file"
                and ref.produced_by is Role.DOCUMENTER
                and not is_error
                and _is_doc(str(inp.get("path", "")))
            ):
                short = str(inp.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
                if short not in docs_touched:
                    docs_touched.append(short)
    code_changed: bool | None = None
    final = state.artifacts.get("diff:final")
    if final is not None and (diff := read(final)) is not None:
        files = DIFF_FILE_RE.findall(diff)
        code_changed = any(not _is_doc(f) for f in files)
    return ToolSignals(
        turns=turns,
        by_tool=[ToolRow(tool=t, calls=c, errors=errors[t]) for t, c in sorted(counts.items())],
        repeated_calls=repeated,
        repeated_examples=examples,
        rereads=rereads,
        docs_touched=docs_touched,
        code_changed=code_changed,
    )


def _read_artifact(ref: ArtifactRef) -> str | None:
    """A transcript of a thread that may still be running: a file caught mid-write can end
    inside a UTF-8 character, which is replaced rather than raised (I-127)."""
    try:
        return Path(ref.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class RoleCost(BaseModel):
    role: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    usd: float


class EscalationRow(BaseModel):
    id: str
    trigger: str
    step_id: str | None
    verdict: str | None
    outcome: str
    tokens: int
    usd: float


class TimelineRow(BaseModel):
    at: datetime
    source: str
    text: str


class RunReport(BaseModel):
    thread_id: str
    title: str
    tier: str | None
    phase: str
    exit_reason: str | None
    branch: str | None
    pr_number: int | None
    features: list[str]
    tokens_used: int
    usd_used: float
    tokens_max: int
    usd_max: float
    advisor_calls: str
    iterations: int
    tool_calls: int
    by_role: list[RoleCost] = Field(default_factory=list)
    escalations: list[EscalationRow] = Field(default_factory=list)
    timeline: list[TimelineRow] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    tool_signals: ToolSignals = Field(default_factory=ToolSignals)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def to_markdown(self) -> str:
        pr = f"#{self.pr_number}" if self.pr_number else "-"
        duration = f" · {self.duration_s:.0f}s" if self.duration_s is not None else ""
        head = [
            f"# Run {self.thread_id}: {self.title}",
            "",
            f"tier **{self.tier or '-'}** · phase **{self.phase}** · "
            f"exit **{self.exit_reason or '-'}** · branch `{self.branch or '-'}` · PR {pr}",
            f"tokens {self.tokens_used:,} / {self.tokens_max:,} · "
            f"${self.usd_used:.4f} / ${self.usd_max:.2f} · advisor {self.advisor_calls} · "
            f"iterations {self.iterations} · tool calls {self.tool_calls}{duration}",
            "",
            "## Features",
            *[f"- {f}" for f in self.features],
            "",
            "## Cost by role",
            "| role | calls | in | out | cache read | $ |",
            "|---|---:|---:|---:|---:|---:|",
            *[
                f"| {r.role} | {r.calls} | {r.input_tokens:,} | {r.output_tokens:,} | "
                f"{r.cache_read_tokens:,} | {r.usd:.4f} |"
                for r in self.by_role
            ],
            "",
            "## Escalations",
            "| id | trigger | step | verdict | outcome | tokens | $ |",
            "|---|---|---|---|---|---:|---:|",
            *(
                [
                    f"| {e.id} | {e.trigger} | {e.step_id or '-'} | {e.verdict or '-'} | "
                    f"{e.outcome} | {e.tokens:,} | {e.usd:.4f} |"
                    for e in self.escalations
                ]
                or ["| none | | | | | | |"]
            ),
            "",
            *self.tool_signals.to_lines(),
            "",
            "## Timeline",
            *[f"- `{t.at.strftime('%H:%M:%S')}` **{t.source}** {t.text}" for t in self.timeline],
            "",
            "## Artifacts",
            *[f"- {k}: `{v}`" for k, v in self.artifacts.items()],
            "",
        ]
        return "\n".join(head)


def build_report(
    state: SCIOState,
    entries: list[CostEntry],
    *,
    read_artifact: Callable[[ArtifactRef], str | None] | None = None,
) -> RunReport:
    by: dict[str, RoleCost] = {}
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in entries:
        a = agg[e.role]
        a["calls"] += 1
        a["in"] += e.usage.input_tokens
        a["out"] += e.usage.output_tokens
        a["cache"] += e.usage.cache_read_tokens
        a["usd"] += e.usd
    for role, a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
        by[role] = RoleCost(
            role=role,
            calls=int(a["calls"]),
            input_tokens=int(a["in"]),
            output_tokens=int(a["out"]),
            cache_read_tokens=int(a["cache"]),
            usd=a["usd"],
        )
    timeline = [TimelineRow(at=t.at, source=t.source.value, text=t.text) for t in state.task_ledger]
    return RunReport(
        thread_id=state.thread_id,
        title=state.task.title,
        tier=state.routing.tier.value if state.routing else None,
        phase=state.phase,
        exit_reason=state.exit_reason.value if state.exit_reason else None,
        branch=state.github.branch,
        pr_number=state.github.pr_number,
        features=[f"{f.id} [{f.status.value}] {f.description}" for f in state.feature_list],
        tokens_used=state.budgets.tokens_used,
        usd_used=state.budgets.usd_used,
        tokens_max=state.budgets.tokens_max,
        usd_max=state.budgets.usd_max,
        advisor_calls=f"{state.budgets.advisor_calls_used}/{state.budgets.advisor_calls_max}",
        iterations=state.progress.iterations,
        tool_calls=state.progress.tool_calls,
        by_role=list(by.values()),
        escalations=[
            EscalationRow(
                id=e.id,
                trigger=e.trigger.value,
                step_id=e.step_id,
                verdict=e.verdict.value if e.verdict else None,
                outcome=e.outcome,
                tokens=e.tokens,
                usd=e.usd,
            )
            for e in state.escalations
        ],
        timeline=timeline,
        artifacts={k: v.path for k, v in state.artifacts.items()},
        tool_signals=tool_signals(state, read_artifact),
        started_at=timeline[0].at if timeline else None,
        finished_at=timeline[-1].at if timeline else None,
    )


def write_report(report: RunReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{report.thread_id}.json"
    md_path = out_dir / f"{report.thread_id}.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8", newline="\n"
    )
    md_path.write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    return json_path, md_path


def load_state(ctx: Any) -> SCIOState:
    """The latest checkpointed state of ``ctx.thread_id``."""
    from scio.graph.build import compile_graph
    from scio.graph.checkpointer import open_checkpointer

    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph = compile_graph(ctx, saver)
        snapshot = graph.get_state(config)
    values = snapshot.values
    if not values:
        raise LookupError(f"no checkpoint for thread {ctx.thread_id!r}")
    return SCIOState.model_validate(values)
