"""Choose the worker backend for a tool-using turn (ADR 0012).

``aci`` (default): :class:`scio.loop.agent_loop.AgentLoop` drives SCIO's sandbox tools.
``claude_code``: :class:`scio.loop.claude_code_worker.ClaudeCodeWorker` delegates the turn
to the unmodified ``claude`` binary on the user's subscription. Both expose ``run`` and
``run_typed`` returning a :class:`scio.loop.agent_loop.LoopResult`.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from scio.aci.toolbox import ToolBox
from scio.graph.run_context import RunContext
from scio.loop.agent_loop import SUMMARY_PROMPT, AgentLoop, LoopResult

RUNNER_OVERRIDE: Any = None
"""Tests inject a canned ``claude`` runner here; None runs the real binary."""


class Worker(Protocol):
    def run(self, user_message: str, *, tags: dict[str, str] | None = None) -> LoopResult: ...

    def run_typed[T: BaseModel](
        self,
        user_message: str,
        output_model: type[T],
        *,
        summary_prompt: str = SUMMARY_PROMPT,
        tags: dict[str, str] | None = None,
    ) -> LoopResult: ...


def make_worker(
    ctx: RunContext, toolbox: ToolBox, role: str, *, max_model_calls: int | None = None
) -> Worker:
    """The configured backend for ``role``; ``toolbox`` names the tools either way."""
    if ctx.settings.worker_backend == "claude_code":
        from scio.loop.claude_code_worker import ClaudeCodeWorker

        return ClaudeCodeWorker(
            ctx.harness,
            ctx.sandbox,
            ctx.prompts,
            role,
            toolchain=ctx.toolchain,
            tool_names=tuple(toolbox.tools),
            max_turns=toolbox.max_calls or max_model_calls,
            binary=ctx.settings.claude_code_bin,
            runner=RUNNER_OVERRIDE,
            timeout_s=max(ctx.settings.request_timeout_s, 600.0) * 3,
        )
    if max_model_calls is not None:
        return AgentLoop(ctx.harness, toolbox, ctx.prompts, role, max_model_calls=max_model_calls)
    return AgentLoop(ctx.harness, toolbox, ctx.prompts, role)
