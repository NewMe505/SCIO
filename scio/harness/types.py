"""Provider-neutral request and response types.

Every adapter translates to and from these. Nothing here depends on a provider SDK.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class StopReason(StrEnum):
    """Why the model stopped. Adapters map provider-specific values onto these."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    TOOL_USE = "tool_use"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    PAUSE_TURN = "pause_turn"
    OTHER = "other"


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUsePart(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Part = TextPart | ToolUsePart | ToolResultPart


class Message(BaseModel):
    """One conversation turn. ``system`` is not a message; it lives on the request."""

    role: Literal["user", "assistant"]
    parts: list[Part]

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", parts=[TextPart(text=text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role="assistant", parts=[TextPart(text=text)])

    @classmethod
    def tool_results(cls, results: list[ToolResultPart]) -> Message:
        return cls(role="user", parts=list(results))

    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if isinstance(p, TextPart))


class ToolSpec(BaseModel):
    """A client-side tool definition. ``input_schema`` is JSON Schema (object)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


class CompletionRequest(BaseModel):
    """One model call. Built fresh from typed state for every call.

    Field order matters for caching: ``system`` and ``tools`` are the stable prefix.
    """

    model: str
    system: str = ""
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = 4096
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    output_schema: dict[str, Any] | None = None
    """Provider-safe JSON schema. Set by the Prompt layer for structured calls."""
    stop_sequences: list[str] = Field(default_factory=list)
    cache_system: bool = True
    cache_tools: bool = True
    role: str | None = None
    """SCIO role name, for telemetry and accounting."""
    tags: dict[str, str] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable hash of the request content (used by the Fake provider and tests)."""
        payload = self.model_dump(mode="json", exclude={"tags", "role"})
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def approx_input_chars(self) -> int:
        n = len(self.system)
        for m in self.messages:
            for p in m.parts:
                if isinstance(p, TextPart):
                    n += len(p.text)
                elif isinstance(p, ToolUsePart):
                    n += len(json.dumps(p.input))
                else:
                    n += len(p.content)
        for t in self.tools:
            n += len(t.description) + len(json.dumps(t.input_schema))
        return n


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class ToolCall(BaseModel):
    id: str
    name: str
    input: dict[str, Any]


class CompletionResponse(BaseModel):
    model: str
    """Model that actually served the response (may differ after a fallback)."""
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: StopReason
    usage: Usage
    request_id: str | None = None
    refusal_category: str | None = None
    served_by_fallback: bool = False
    latency_ms: int = 0

    def as_assistant_message(self) -> Message:
        parts: list[Part] = []
        if self.text:
            parts.append(TextPart(text=self.text))
        for call in self.tool_calls:
            parts.append(ToolUsePart(id=call.id, name=call.name, input=call.input))
        return Message(role="assistant", parts=parts)


class StreamEvent(BaseModel):
    """A streamed text delta. The final :class:`CompletionResponse` follows the stream."""

    delta: str
