"""Deterministic Fake provider for tests, CI and offline evals.

Three ways to drive it, in priority order:

1. A **script**: a queue of :class:`FakeReply` consumed one per call.
2. A **responder** callable ``(request) -> FakeReply`` for request-dependent behaviour.
3. The **default**: an echo whose text is derived from the request fingerprint, or a
   schema-conforming JSON instance for structured calls.

Every request is recorded in :attr:`FakeProvider.requests`.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from scio.harness.caching import estimate_tokens
from scio.harness.failures import HarnessError
from scio.harness.types import (
    CompletionRequest,
    CompletionResponse,
    StopReason,
    StreamEvent,
    ToolCall,
    Usage,
)


@dataclass
class FakeReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    raise_exc: HarnessError | None = None
    """Raise this instead of replying (to exercise retry and failure paths)."""
    latency_ms: int = 1


Responder = Callable[[CompletionRequest], FakeReply]


def instance_from_schema(schema: dict[str, Any]) -> Any:
    """Smallest deterministic JSON value that satisfies a (wire) JSON schema."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        options = schema["anyOf"]
        non_null = [o for o in options if o.get("type") != "null"]
        return instance_from_schema(non_null[0] if non_null else options[0])
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            merged.update(part)
        return instance_from_schema(merged)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object":
        return {k: instance_from_schema(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return []
    if t == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt == "uuid":
            return "00000000-0000-4000-8000-000000000000"
        return "fake"
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None


class FakeProvider:
    """``ModelProvider`` that never touches the network."""

    name = "fake"

    def __init__(
        self,
        script: list[FakeReply] | None = None,
        *,
        responder: Responder | None = None,
    ) -> None:
        self._script: deque[FakeReply] = deque(script or [])
        self._responder = responder
        self.requests: list[CompletionRequest] = []
        self.calls = 0

    def enqueue(self, *replies: FakeReply) -> None:
        self._script.extend(replies)

    def _next(self, request: CompletionRequest, schema: dict[str, Any] | None) -> FakeReply:
        if self._script:
            return self._script.popleft()
        if self._responder is not None:
            return self._responder(request)
        if schema is not None:
            return FakeReply(text=json.dumps(instance_from_schema(schema), sort_keys=True))
        return FakeReply(text=f"fake:{request.fingerprint()}")

    def _respond(
        self, request: CompletionRequest, schema: dict[str, Any] | None
    ) -> CompletionResponse:
        self.requests.append(request)
        self.calls += 1
        reply = self._next(request, schema)
        if reply.raise_exc is not None:
            raise reply.raise_exc
        in_tokens = (
            reply.input_tokens if reply.input_tokens is not None else self.count_tokens(request)
        )
        out_tokens = (
            reply.output_tokens
            if reply.output_tokens is not None
            else estimate_tokens(len(reply.text))
        )
        return CompletionResponse(
            model=request.model,
            text=reply.text,
            tool_calls=list(reply.tool_calls),
            stop_reason=reply.stop_reason,
            usage=Usage(
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cache_read_tokens=reply.cache_read_tokens,
                cache_write_tokens=reply.cache_write_tokens,
            ),
            request_id=f"fake-{self.calls:06d}",
            latency_ms=reply.latency_ms,
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._respond(request, None)

    def structured(self, request: CompletionRequest, schema: dict[str, Any]) -> CompletionResponse:
        return self._respond(request, schema)

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent | CompletionResponse]:
        resp = self._respond(request, request.output_schema)
        step = 16
        for i in range(0, len(resp.text), step):
            yield StreamEvent(delta=resp.text[i : i + step])
        yield resp

    def count_tokens(self, request: CompletionRequest) -> int:
        return estimate_tokens(request.approx_input_chars())
