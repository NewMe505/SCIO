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
from scio.graph.run_context import T0_TOOL_CAP, T0_TURN_CAP, RunContext
from scio.loop.agent_loop import SUMMARY_PROMPT, AgentLoop, LoopResult

RUNNER_OVERRIDE: Any = None
"""Tests inject a canned ``claude`` runner here; None runs the real binary."""

DOCUMENTER_TURN_CAP = 20
"""The documenter's cap on the claude_code backend, in Claude Code turns (I-153). Its tool cap
(``max_calls=12`` in ``document``, SCIO's own loop) had been passed through as
``--max-turns 12``. In the graphcalc run all 8 documenter sessions hit that cap, and 2 edited
nothing ("Stopped mid-investigation, before making any documentation edits", P1 and P4).
The 6 that edited made their first edit at turns 6 to 12 (median 8.5) and their last at 9 to
12. 20 = the latest first edit (12) + the longest edit span (7 turns) + the closing turn.
A 12-turn session cost a median of $0.45; about $0.52 is expected at 20 (the median of the
coder's 17 to 22-turn sessions). Censored at 12: measure again after the next live run."""


def claude_code_max_turns(
    role: str, toolbox: ToolBox, max_model_calls: int | None = None
) -> int | None:
    """``--max-turns`` for a Claude Code worker turn, in Claude Code's own unit (I-153).

    Claude Code counts agentic turns: one assistant API message each, which can carry
    several tool calls. SCIO's ``ToolBox`` counts tool calls. So where a measured turn cap
    exists, a tool cap is converted here and never passed through. The T0 tool cap maps to
    ``T0_TURN_CAP``, and the documenter gets ``DOCUMENTER_TURN_CAP``. The explorer's tool cap
    (8) still passes through: every explorer session delivered its summary, so it is left
    as it was. None gives the worker's default (30 turns: T1 and T2 coder and debugger).
    """
    if toolbox.max_calls == T0_TOOL_CAP:
        return T0_TURN_CAP
    if role == "documenter":
        return DOCUMENTER_TURN_CAP
    return toolbox.max_calls or max_model_calls


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

        if ctx.settings.sandbox_backend != "local":
            # The binary runs on the host in the sandbox root; a Docker or E2B root is not a
            # host path (ADR 0012 boundary, enforced since M9).
            raise ValueError(
                "SCIO_WORKER_BACKEND=claude_code needs SCIO_SANDBOX_BACKEND=local; "
                f"got {ctx.settings.sandbox_backend!r}"
            )
        return ClaudeCodeWorker(
            ctx.harness,
            ctx.sandbox,
            ctx.prompts,
            role,
            toolchain=ctx.toolchain,
            tool_names=tuple(toolbox.tools),
            max_turns=claude_code_max_turns(role, toolbox, max_model_calls),
            binary=ctx.settings.claude_code_bin,
            runner=RUNNER_OVERRIDE,
            timeout_s=max(ctx.settings.request_timeout_s, 600.0) * 3,
        )
    if max_model_calls is not None:
        return AgentLoop(ctx.harness, toolbox, ctx.prompts, role, max_model_calls=max_model_calls)
    return AgentLoop(ctx.harness, toolbox, ctx.prompts, role)
