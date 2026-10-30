"""``ClaudeCodeWorker``: one worker turn delegated to the unmodified ``claude`` binary
(ADR 0012), with the same surface as :class:`scio.loop.agent_loop.AgentLoop`.

Shape of a turn:

1. ``claude -p`` in the sandbox root with the role's static system prompt (plus a note that
   Claude Code's own tools replace SCIO's), the task projection on stdin, the role's allowed
   tools, ``acceptEdits`` permissions with nobody to answer prompts, a turn cap from the loop
   limits and a dollar cap from the remaining budget; ``stream-json`` output is rendered into
   the same transcript shape ``AgentLoop`` produces, so ``scio report`` reads it unchanged.
2. One ``--resume`` call on that session, one turn, no tools, ``--json-schema`` for the
   typed closing output (``WorkerOutput`` by default).

Verify stays SCIO's ground truth; the ACI's per-edit lint gate does not apply inside a
Claude Code turn. Cost is Claude Code's own estimate, folded into the harness ledger and
budget through :meth:`scio.harness.core.Harness.account`. Both calls run with
:func:`worker_env`, so the turn's Bash commands find the product's interpreter first (I-114).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from scio.aci.sandbox.base import Sandbox
from scio.aci.sandbox.local import with_interpreter_first
from scio.harness.core import Harness
from scio.harness.failures import FatalError
from scio.harness.providers.claude_code import (
    PROVIDER_NAME,
    ClaudeRunResult,
    Runner,
    claude_runner,
    parse_result,
    served_model,
    subprocess_env,
    usage_from_result,
)
from scio.harness.types import Message, Part, TextPart, ToolResultPart, ToolUsePart, Usage
from scio.loop.agent_loop import SUMMARY_PROMPT, LoopResult, StopKind
from scio.prompt.registry import PromptRegistry
from scio.prompt.wire_schema import wire_schema
from scio.state.models import WorkerOutput
from scio.toolchain import Toolchain

READ_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
DEFAULT_MAX_TURNS = 30
SUMMARY_MAX_TURNS = 3
"""Turns for the typed summary on ``--resume``. Claude Code delivers ``--json-schema`` output
through a ``StructuredOutput`` tool call and answers a payload that misses the schema with a
tool error the model must act on in a further turn; with one turn that retry was impossible
and the call ended ``error_max_turns`` (I-077, the first live http milestone)."""

BACKEND_NOTE = (
    "\n\nThis turn runs inside Claude Code. Its built-in tools replace the ones named above: "
    "Grep and Glob for repo_search, Read for view_file, Edit and Write for edit_file, and "
    "Bash for run_tests ({test_cmd}). Dependencies are installed by the orchestrator from the "
    "product's approved list before your turn; never install packages yourself, ask for a "
    "design decision instead. When the product has an interpreter of its own, `python` "
    "already runs the product's environment with its approved dependencies installed; never "
    "look for another interpreter. Keep every path inside the repository root, never run git "
    "commands that change history, and stop when the tests you were asked for pass."
)
"""Appended to the role's system prompt. The interpreter sentence is I-114: a live coder spent
36 tool calls hunting for an interpreter that could import a product dependency."""


def worker_env(sandbox: Sandbox, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a worker turn's ``claude -p`` process (I-114).

    :func:`subprocess_env`, plus the product interpreter's directory first on PATH and
    ``VIRTUAL_ENV`` at that interpreter's environment root, so the turn's Bash commands run
    the product's interpreter with its approved dependencies, not SCIO's own (which is first
    on SCIO's PATH under ``uv run``). SCIO's directories stay on PATH after it for the
    profile's other tools. Unchanged when the sandbox has no interpreter of its own.
    """
    return with_interpreter_first(subprocess_env(base), getattr(sandbox, "python", None))


def allowed_tools_for(tool_names: tuple[str, ...], toolchain: Toolchain) -> list[str]:
    """Claude Code permission rules equivalent to a SCIO toolbox."""
    rules: list[str] = []
    if {"repo_search", "view_file"} & set(tool_names):
        rules += READ_TOOLS
    if "edit_file" in tool_names:
        rules += EDIT_TOOLS
    if "delete_file" in tool_names:
        rules += ["Bash(rm *)"]  # Claude Code has no delete tool; rm inside the sandbox root
    if "run_app" in tool_names:
        rules += [f"Bash({' '.join(toolchain.runtime().app_argv)} *)"]
    # install_deps has no rule on purpose: the install phase runs outside the turn (M9)
    if "run_tests" in tool_names:
        # The profile's test and lint commands (``python -m pytest`` is ``python`` already;
        # ``node --test`` is ``node``), nothing stack-specific here (M10).
        test_bin = toolchain.test_argv(filter=None, path=None)[0]
        rules += [f"Bash({test_bin} *)", f"Bash({toolchain.lint_argv('.')[0]} *)"]
    return list(dict.fromkeys(rules))


@dataclass
class StreamTurn:
    """What one ``stream-json`` run produced."""

    messages: list[Message] = field(default_factory=list)
    tool_calls: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    turns: int = 0


def parse_stream(stdout: str) -> StreamTurn:
    """Render ``stream-json`` lines into SCIO messages; the last ``result`` line is kept."""
    turn = StreamTurn()
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "result":
            turn.result = event
            turn.turns = int(event.get("num_turns") or 0)
            continue
        if kind not in ("assistant", "user"):
            continue
        if event.get("parent_tool_use_id"):
            continue  # subagent traffic is not this worker's transcript
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        parts: list[Part] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(TextPart(text=str(block["text"])))
            elif btype == "tool_use":
                turn.tool_calls += 1
                parts.append(
                    ToolUsePart(
                        id=str(block.get("id") or f"call-{turn.tool_calls}"),
                        name=str(block.get("name") or "tool"),
                        input=dict(block.get("input") or {}),
                    )
                )
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    text = "\n".join(str(b.get("text", "")) for b in inner if isinstance(b, dict))
                else:
                    text = str(inner or "")
                parts.append(
                    ToolResultPart(
                        tool_use_id=str(block.get("tool_use_id") or ""),
                        content=text[:4000],
                        is_error=bool(block.get("is_error")),
                    )
                )
        if parts:
            turn.messages.append(
                Message(role="assistant" if kind == "assistant" else "user", parts=parts)
            )
    return turn


def validate_with_trimming[T: BaseModel](output_model: type[T], raw: Any) -> T:
    """Validate ``raw`` against ``output_model``, trimming string fields that only exceed
    their ``max_length`` (I-070). The wire schema handed to ``--json-schema`` carries no
    length caps, and a cap on prose is a presentation limit: trim, then validate again.
    Any other error is raised unchanged. ``raw`` is modified in place when trimmed."""
    try:
        return output_model.model_validate(raw)
    except ValidationError as exc:
        if not isinstance(raw, dict):
            raise
        trimmed = False
        for err in exc.errors():
            if err.get("type") != "string_too_long":
                continue
            loc = err.get("loc") or ()
            limit = (err.get("ctx") or {}).get("max_length")
            if len(loc) != 1 or not isinstance(limit, int):
                continue
            key = loc[0]
            if isinstance(raw.get(key), str):
                raw[key] = raw[key][: max(limit - 3, 0)].rstrip() + "..."
                trimmed = True
        if not trimmed:
            raise
        return output_model.model_validate(raw)


class ClaudeCodeWorker:
    def __init__(
        self,
        harness: Harness,
        sandbox: Sandbox,
        prompts: PromptRegistry,
        role: str,
        *,
        toolchain: Toolchain,
        tool_names: tuple[str, ...],
        max_turns: int | None = None,
        binary: str = "claude",
        runner: Runner | None = None,
        timeout_s: float = 1800.0,
    ) -> None:
        self.harness = harness
        self.sandbox = sandbox
        self.prompts = prompts
        self.role = role
        self.toolchain = toolchain
        self.tool_names = tool_names
        self.max_turns = max_turns or DEFAULT_MAX_TURNS
        self.binary = binary
        self._run = runner or claude_runner(binary)
        self.timeout_s = timeout_s
        self.calls: list[list[str]] = []

    def run(self, user_message: str, *, tags: dict[str, str] | None = None) -> LoopResult:
        return self.run_typed(user_message, WorkerOutput, tags=tags)

    def run_typed[T: BaseModel](
        self,
        user_message: str,
        output_model: type[T],
        *,
        summary_prompt: str = SUMMARY_PROMPT,
        tags: dict[str, str] | None = None,
    ) -> LoopResult:
        model = self.harness.config.role(self.role).model
        test_cmd = " ".join(self.toolchain.test_argv(filter=None, path=None))
        system = self.prompts.get(self.role).render() + BACKEND_NOTE.format(test_cmd=test_cmd)
        snap = self.harness.budget.snapshot()
        remaining = snap.usd_max - snap.usd_used
        # Built per turn, not in __init__: the install phase sets ``sandbox.python`` after the
        # worker exists (a resumed build, a lazy install_deps).
        env = worker_env(self.sandbox)
        with tempfile.TemporaryDirectory(prefix="scio-claude-") as tmp:
            system_path = Path(tmp) / "system.md"
            system_path.write_text(system, encoding="utf-8", newline="\n")
            argv = [
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                model,
                "--system-prompt-file",
                str(system_path),
                "--allowedTools",
                ",".join(allowed_tools_for(self.tool_names, self.toolchain)),
                "--permission-mode",
                "acceptEdits",
                "--permission-prompts",
                "none",
                "--max-turns",
                str(self.max_turns),
                "--max-budget-usd",
                f"{max(remaining, 0.01):.2f}",
                "--name",
                f"scio-{self.role}",
            ]
            self.calls.append(list(argv))
            res = self._run(
                argv, cwd=self.sandbox.root, stdin=user_message, timeout_s=self.timeout_s, env=env
            )
        turn = parse_stream(res.stdout)
        if not turn.result:
            raise FatalError(
                f"claude -p ({self.role}) produced no result: exit {res.code}, "
                f"{(res.stderr or res.stdout).strip()[:400]!r}"
            )
        capped = turn.result.get("subtype") == "error_max_turns"
        if capped:  # the turn cap is a normal stop for SCIO, not an error: summarise the session
            turn.result = {**turn.result, "is_error": False}
        result = parse_result(ClaudeRunResult(res.code, json.dumps(turn.result), res.stderr))
        session_id = str(result.get("session_id") or "")
        self._account(result, model, tags)
        messages = [Message.user(user_message), *turn.messages]
        stop: StopKind = "tool_cap" if capped or turn.turns >= self.max_turns else "end_turn"

        # The typed closing output: one more turn on the same session, no tools.
        schema = wire_schema(output_model)
        output, summary_text, calls = self._summary(
            session_id, schema, output_model, summary_prompt, model, tags, env
        )
        messages.append(Message.assistant(summary_text))
        return LoopResult(
            output=output,
            stop=stop,
            tool_calls=turn.tool_calls,
            model_calls=max(turn.turns, 1) + calls,
            messages=messages,
        )

    def _summary[T: BaseModel](
        self,
        session_id: str,
        schema: dict[str, Any],
        output_model: type[T],
        prompt: str,
        model: str,
        tags: dict[str, str] | None,
        env: Mapping[str, str],
    ) -> tuple[T, str, int]:
        last_error = ""
        for attempt in range(1, 3):
            ask = (
                prompt
                if attempt == 1
                else f"{prompt}\n\nYour previous reply was invalid: {last_error}"
            )
            argv = [
                "-p",
                "--output-format",
                "json",
                "--resume",
                session_id,
                "--tools",
                "",
                "--max-turns",
                str(SUMMARY_MAX_TURNS),
                "--model",
                model,
                "--json-schema",
                json.dumps(schema),
            ]
            self.calls.append(list(argv))
            res = self._run(
                argv, cwd=self.sandbox.root, stdin=ask, timeout_s=self.timeout_s, env=env
            )
            try:
                data = parse_result(res)
            except FatalError as exc:
                if "error_max_turns" not in str(exc):
                    raise
                # Claude Code's schema check refused every attempt within the turn cap
                # (I-077): re-ask once with the schema's required keys spelled out.
                last_error = (
                    "the structured output never matched the schema within the turn cap; "
                    f"required keys: {', '.join(schema.get('required', []))}"
                )
                continue
            self._account(data, model, tags)
            payload = data.get("structured_output")
            text = json.dumps(payload) if payload is not None else str(data.get("result") or "")
            try:
                raw = payload if payload is not None else json.loads(text)
                value = validate_with_trimming(output_model, raw)
                return value, json.dumps(raw), attempt
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = str(exc)[:600]
        raise FatalError(
            f"claude -p ({self.role}) summary never matched {output_model.__name__}: {last_error}"
        )

    def _account(self, data: dict[str, Any], model: str, tags: dict[str, str] | None) -> Usage:
        usage = usage_from_result(data)
        self.harness.account(
            role=self.role,
            model=served_model(data, model),
            provider=PROVIDER_NAME,
            usage=usage,
            latency_ms=int(data.get("duration_api_ms") or 0),
            request_id=str(data.get("session_id") or "") or None,
            tags=tags,
        )
        return usage
