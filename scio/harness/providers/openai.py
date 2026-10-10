"""OpenAI adapter (official ``openai`` SDK, Chat Completions).

The "one other provider" required by the spec (assumption F-6). Token counting is a
local estimate because the API has no counting endpoint; the pre-check treats it as
approximate and the post-call accounting uses the reported usage.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import openai

from scio.harness.caching import estimate_tokens
from scio.harness.failures import FatalError, HarnessError, RetryableError
from scio.harness.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    StopReason,
    StreamEvent,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    Usage,
)

_FINISH_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "content_filter": StopReason.REFUSAL,
}

_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "assistant":
            text = "".join(p.text for p in m.parts if isinstance(p, TextPart))
            calls = [
                {
                    "id": p.id,
                    "type": "function",
                    "function": {"name": p.name, "arguments": json.dumps(p.input)},
                }
                for p in m.parts
                if isinstance(p, ToolUsePart)
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            texts = [p.text for p in m.parts if isinstance(p, TextPart)]
            results = [p for p in m.parts if isinstance(p, ToolResultPart)]
            for r in results:
                out.append({"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content})
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def build_kwargs(
    request: CompletionRequest, *, schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "model": request.model,
        "messages": _to_openai_messages(request.system, request.messages),
        "max_completion_tokens": request.max_tokens,
    }
    if request.tools:
        kw["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": t.strict,
                },
            }
            for t in request.tools
        ]
    if request.effort:
        kw["reasoning_effort"] = _EFFORT_MAP[request.effort]
    if schema is not None:
        kw["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema, "strict": True},
        }
    if request.stop_sequences:
        kw["stop"] = request.stop_sequences
    return kw


def classify_exception(exc: BaseException) -> HarnessError:
    if isinstance(exc, openai.RateLimitError):
        retry_after = None
        header = exc.response.headers.get("retry-after") if exc.response is not None else None
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        return RetryableError("rate limited", retry_after_s=retry_after, cause=exc)
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return RetryableError(f"connection: {exc}", cause=exc)
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409, 429):
            return RetryableError(f"status {exc.status_code}: {exc.message}", cause=exc)
        return FatalError(f"status {exc.status_code}: {exc.message}", cause=exc)
    if isinstance(exc, HarnessError):
        return exc
    return FatalError(f"unexpected: {exc!r}", cause=exc)


def _usage_from(u: Any) -> Usage:
    if u is None:
        return Usage()
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    completion = int(getattr(u, "completion_tokens", 0) or 0)
    details = getattr(u, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    return Usage(
        input_tokens=max(prompt - cached, 0), output_tokens=completion, cache_read_tokens=cached
    )


def translate_response(resp: Any, *, latency_ms: int) -> CompletionResponse:
    choice = resp.choices[0]
    message = choice.message
    tool_calls: list[ToolCall] = []
    for call in getattr(message, "tool_calls", None) or []:
        fn = call.function
        try:
            args = json.loads(fn.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.arguments}
        tool_calls.append(ToolCall(id=call.id, name=fn.name, input=args))
    refusal = getattr(message, "refusal", None)
    stop = _FINISH_MAP.get(choice.finish_reason or "", StopReason.OTHER)
    if refusal:
        stop = StopReason.REFUSAL
    return CompletionResponse(
        model=resp.model,
        text=message.content or "",
        tool_calls=tool_calls,
        stop_reason=stop,
        usage=_usage_from(getattr(resp, "usage", None)),
        request_id=getattr(resp, "_request_id", None),
        refusal_category="content_filter" if stop is StopReason.REFUSAL else None,
        latency_ms=latency_ms,
    )


class OpenAIProvider:
    """``ModelProvider`` for OpenAI Chat Completions."""

    name = "openai"

    def __init__(self, client: openai.OpenAI | None = None, *, timeout_s: float = 600.0) -> None:
        self._client = client or openai.OpenAI(timeout=timeout_s, max_retries=0)

    def _create(
        self, request: CompletionRequest, schema: dict[str, Any] | None
    ) -> CompletionResponse:
        kw = build_kwargs(request, schema=schema)
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kw)
        except Exception as exc:
            raise classify_exception(exc) from exc
        return translate_response(resp, latency_ms=int((time.perf_counter() - t0) * 1000))

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._create(request, None)

    def structured(self, request: CompletionRequest, schema: dict[str, Any]) -> CompletionResponse:
        return self._create(request, schema)

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent | CompletionResponse]:
        kw = build_kwargs(request, schema=request.output_schema)
        kw["stream"] = True
        kw["stream_options"] = {"include_usage": True}
        t0 = time.perf_counter()
        text: list[str] = []
        finish: str | None = None
        usage: Usage = Usage()
        model = request.model
        try:
            for chunk in self._client.chat.completions.create(**kw):
                model = getattr(chunk, "model", model) or model
                if getattr(chunk, "usage", None) is not None:
                    usage = _usage_from(chunk.usage)
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta is not None and delta.content:
                        text.append(delta.content)
                        yield StreamEvent(delta=delta.content)
                    if chunk.choices[0].finish_reason:
                        finish = chunk.choices[0].finish_reason
        except Exception as exc:
            raise classify_exception(exc) from exc
        yield CompletionResponse(
            model=model,
            text="".join(text),
            stop_reason=_FINISH_MAP.get(finish or "", StopReason.OTHER),
            usage=usage,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return estimate_tokens(request.approx_input_chars())
