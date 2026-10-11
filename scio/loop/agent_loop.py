"""``AgentLoop``: one worker turn with tools, ending in a typed ``WorkerOutput``.

Shape of a turn (append-only message history, static prefix first for caching):

1. user: the role's task projection (from the Context layer)
2. assistant: text and/or tool calls → user: tool results, repeated until the model
   stops calling tools, the tool-call cap is hit, or the model-call cap is hit
3. user: "produce your JSON summary" → assistant: ``WorkerOutput`` (validated, repaired)

The loop never decides success. ``verify`` (tests, lint) does.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from scio.aci.toolbox import ToolBox
from scio.context.trim import trim_messages
from scio.harness.core import Harness
from scio.harness.types import Message, StopReason, TextPart, ToolResultPart
from scio.prompt.registry import PromptRegistry
from scio.state.models import WorkerOutput

SUMMARY_PROMPT = (
    "Stop working now. Produce the JSON summary of what you did in this turn, matching the "
    "schema: cite the plan step id, list every file you touched, and state your confidence. "
    "If you need a design decision, set needs_design_decision and design_question."
)

StopKind = Literal["end_turn", "tool_cap", "model_cap", "truncated"]


@dataclass
class LoopResult:
    output: Any
    """The closing structured output: ``WorkerOutput`` by default, else the model passed."""
    stop: StopKind
    tool_calls: int
    model_calls: int
    messages: list[Message] = field(default_factory=list)

    def transcript(self) -> str:
        lines: list[str] = []
        for m in self.messages:
            for p in m.parts:
                if p.type == "text":
                    lines.append(f"{m.role}: {p.text}")
                elif p.type == "tool_use":
                    lines.append(f"{m.role}: <tool_use {p.name} {p.input}>")
                else:
                    flag = " (error)" if p.is_error else ""
                    lines.append(f"tool_result{flag}: {p.content}")
        return "\n".join(lines)


class AgentLoop:
    def __init__(
        self,
        harness: Harness,
        toolbox: ToolBox,
        prompts: PromptRegistry,
        role: str,
        *,
        max_model_calls: int = 16,
        max_repairs: int = 2,
    ) -> None:
        self.harness = harness
        self.toolbox = toolbox
        self.prompts = prompts
        self.role = role
        self.max_model_calls = max_model_calls
        self.max_repairs = max_repairs

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
        """Same turn shape, but the closing structured call returns ``output_model``."""
        system = self.prompts.get(self.role).render()
        specs = self.toolbox.specs()
        messages: list[Message] = [Message.user(user_message)]
        model_calls = 0
        stop: StopKind = "model_cap"
        start_calls = self.toolbox.calls

        while model_calls < self.max_model_calls:
            req = self.harness.build_request(
                self.role, system=system, messages=trim_messages(messages), tools=specs, tags=tags
            )
            resp = self.harness.complete(req)
            model_calls += 1
            assistant = resp.as_assistant_message()
            if not assistant.parts:
                assistant = Message(role="assistant", parts=[TextPart(text="(no output)")])
            messages.append(assistant)

            if resp.stop_reason is StopReason.MAX_TOKENS and not resp.tool_calls:
                stop = "truncated"
                break
            if not resp.tool_calls:
                stop = "end_turn"
                break

            results: list[ToolResultPart] = []
            cap_hit = False
            for call in resp.tool_calls:
                result = self.toolbox.call(call.id, call.name, call.input)
                results.append(result)
                if (
                    self.toolbox.max_calls is not None
                    and self.toolbox.calls > self.toolbox.max_calls
                ):
                    cap_hit = True
            messages.append(Message.tool_results(results))
            if cap_hit:
                stop = "tool_cap"
                break

        summary_req = self.harness.build_request(
            self.role,
            system=system,
            messages=[*trim_messages(messages), Message.user(summary_prompt)],
            tags=tags,
        )
        summary = self.harness.structured(summary_req, output_model, max_repairs=self.max_repairs)
        model_calls += summary.attempts
        messages.append(Message.assistant(summary.response.text))
        return LoopResult(
            output=summary.value,
            stop=stop,
            tool_calls=self.toolbox.calls - start_calls,
            model_calls=model_calls,
            messages=messages,
        )
