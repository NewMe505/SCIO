"""``ClaudeCodeProvider``: model calls through the unmodified ``claude`` binary (ADR 0012).

Anthropic allows a Claude subscription (Pro, Max) to be used only inside Claude Code itself,
so a SCIO run that must not touch Console API billing delegates every model call to
``claude -p`` (print mode). This adapter covers the tool-less roles: router, planner,
advisor, gate, reviewer, interviewer, product planner, milestone planner and gate. Each call
is one ``claude -p`` process in restricted mode with no tools, one turn, no session
persistence, the role's static system prompt from a file, the messages on stdin, and
``--json-schema`` for structured calls. Tool-using turns (coder, debugger, documenter,
explorer) go through :class:`scio.loop.claude_code_worker.ClaudeCodeWorker` instead.

The subprocess environment drops ``CLAUDECODE`` (print mode refuses to nest otherwise) and
``ANTHROPIC_API_KEY`` (which would shadow the subscription login).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scio.harness.caching import estimate_tokens
from scio.harness.failures import FatalError, RetryableError
from scio.harness.types import (
    CompletionRequest,
    CompletionResponse,
    StopReason,
    StreamEvent,
    Usage,
)

PROVIDER_NAME = "claude_code"
STRIPPED_ENV = ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
RETRYABLE_ERRORS = ("rate_limit", "overloaded", "server_error")


@dataclass(frozen=True)
class ClaudeRunResult:
    code: int
    stdout: str
    stderr: str


Runner = Callable[..., ClaudeRunResult]
"""``runner(argv, *, cwd, stdin, timeout_s) -> ClaudeRunResult``; argv excludes the binary."""


def subprocess_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a ``claude`` subprocess: the caller's, minus the variables that
    would make print mode refuse to start or bill an API key instead of the login."""
    env = dict(os.environ if base is None else base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    return env


def claude_runner(binary: str = "claude") -> Runner:
    def run(
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float = 600.0,
    ) -> ClaudeRunResult:
        proc = subprocess.run(
            [binary, *argv],
            input=stdin,
            cwd=str(cwd) if cwd else None,
            env=subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return ClaudeRunResult(proc.returncode, proc.stdout, proc.stderr)

    return run


def usage_from_result(data: Mapping[str, Any]) -> Usage:
    """SCIO's ``Usage`` from the ``usage`` block of a ``claude -p`` JSON result."""
    u = data.get("usage") or {}
    return Usage(
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
    )


def served_model(data: Mapping[str, Any], requested: str) -> str:
    """The model Claude Code reports having used (``modelUsage`` keys), else the request's."""
    usage = data.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return str(next(iter(usage)))
    return requested


def parse_result(res: ClaudeRunResult) -> dict[str, Any]:
    """The JSON result of a ``--output-format json`` run; raises on failure."""
    if res.code != 0 and not res.stdout.strip():
        raise FatalError(f"claude -p exited {res.code}: {res.stderr.strip()[:500]}")
    text = res.stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FatalError(f"claude -p returned no JSON result: {text[:300]!r}") from exc
    if not isinstance(data, dict):
        raise FatalError(f"claude -p result is not an object: {text[:300]!r}")
    if data.get("is_error"):
        message = str(data.get("result") or data.get("error") or "unknown error")
        if any(k in message.lower() for k in RETRYABLE_ERRORS):
            raise RetryableError(f"claude -p failed: {message[:500]}")
        raise FatalError(f"claude -p failed: {message[:500]}")
    return data


def render_messages(request: CompletionRequest) -> str:
    """The conversation as one prompt: ``claude -p`` takes a single input, so earlier turns
    (the repair loop's assistant reply and the re-ask) are rendered with their roles."""
    if len(request.messages) == 1:
        return request.messages[0].text()
    lines = []
    for m in request.messages:
        lines.append(f"[{m.role}]\n{m.text()}")
    return "\n\n".join(lines)


class ClaudeCodeProvider:
    """``ModelProvider`` over ``claude -p`` for tool-less requests."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        binary: str = "claude",
        runner: Runner | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.binary = binary
        self._run = runner or claude_runner(binary)
        self.timeout_s = timeout_s
        self.calls: list[list[str]] = []
        """Every argv sent, for tests and the report."""

    # -- ModelProvider ----------------------------------------------------------

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._invoke(request, schema=None)

    def structured(self, request: CompletionRequest, schema: dict[str, Any]) -> CompletionResponse:
        return self._invoke(request, schema=schema)

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent | CompletionResponse]:
        yield self.complete(request)

    def count_tokens(self, request: CompletionRequest) -> int:
        return estimate_tokens(request.approx_input_chars())

    # -- plumbing ---------------------------------------------------------------

    def _invoke(
        self, request: CompletionRequest, *, schema: dict[str, Any] | None
    ) -> CompletionResponse:
        if request.tools:
            raise FatalError(
                "the claude_code provider runs tool-less calls only; tool-using turns go "
                "through ClaudeCodeWorker (SCIO_WORKER_BACKEND=claude_code)"
            )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="scio-claude-") as tmp:
            system_path = Path(tmp) / "system.md"
            system_path.write_text(
                request.system or "You are a careful assistant.", encoding="utf-8", newline="\n"
            )
            argv = [
                "-p",
                "--output-format",
                "json",
                "--restricted",
                "--tools",
                "",
                "--max-turns",
                "1",
                "--no-session-persistence",
                "--model",
                request.model,
                "--system-prompt-file",
                str(system_path),
            ]
            if request.effort:
                argv += ["--effort", request.effort]
            if schema is not None:
                argv += ["--json-schema", json.dumps(schema)]
            self.calls.append(list(argv))
            res = self._run(
                argv, cwd=None, stdin=render_messages(request), timeout_s=self.timeout_s
            )
        data = parse_result(res)
        if schema is not None and data.get("structured_output") is not None:
            text = json.dumps(data["structured_output"])
        else:
            text = str(data.get("result") or "")
        return CompletionResponse(
            model=served_model(data, request.model),
            text=text,
            stop_reason=StopReason.END_TURN,
            usage=usage_from_result(data),
            request_id=str(data.get("session_id") or "") or None,
            latency_ms=int(data.get("duration_api_ms") or (time.monotonic() - started) * 1000),
        )
