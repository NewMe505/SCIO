"""Anthropic adapter (official ``anthropic`` SDK, Messages API).

Request shape follows the current API: adaptive thinking is left at its default (the
``thinking`` parameter is omitted), depth is controlled with ``output_config.effort``,
typed outputs use ``output_config.format``, and the system prompt and tool list carry
``cache_control`` breakpoints. SDK retries are disabled; the harness retries.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import anthropic

from scio.harness.caching import CachePolicy, estimate_tokens, worth_caching
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

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_MAP: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "tool_use": StopReason.TOOL_USE,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.PAUSE_TURN,
}


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        blocks: list[dict[str, Any]] = []
        for p in m.parts:
            if isinstance(p, TextPart):
                blocks.append({"type": "text", "text": p.text})
            elif isinstance(p, ToolUsePart):
                blocks.append({"type": "tool_use", "id": p.id, "name": p.name, "input": p.input})
            elif isinstance(p, ToolResultPart):
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": p.tool_use_id,
                    "content": p.content,
                }
                if p.is_error:
                    block["is_error"] = True
                blocks.append(block)
        out.append({"role": m.role, "content": blocks})
    return out


def build_kwargs(
    request: CompletionRequest,
    *,
    cache: CachePolicy,
    cache_min_tokens: int,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate a :class:`CompletionRequest` into ``messages.create`` keyword arguments."""
    messages = _to_anthropic_messages(request.messages)
    if cache.messages and messages and messages[-1]["content"]:
        # Third breakpoint (after system and tools): the whole conversation so far. Each
        # worker turn appends to an append-only transcript, so the previous prefix is a
        # cache hit at 0.1x instead of a full-price re-read (ledger I-040).
        messages[-1]["content"][-1]["cache_control"] = cache.control()
    kw: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": messages,
    }
    if request.system:
        prefix_tokens = estimate_tokens(len(request.system))
        if cache.system and request.cache_system and worth_caching(prefix_tokens, cache_min_tokens):
            kw["system"] = [
                {"type": "text", "text": request.system, "cache_control": cache.control()}
            ]
        else:
            kw["system"] = request.system
    if request.tools:
        tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "strict": t.strict,
            }
            for t in request.tools
        ]
        if cache.tools and request.cache_tools:
            tools[-1]["cache_control"] = cache.control()
        kw["tools"] = tools
    output_config: dict[str, Any] = {}
    if request.effort:
        output_config["effort"] = request.effort
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        kw["output_config"] = output_config
    if request.stop_sequences:
        kw["stop_sequences"] = request.stop_sequences
    return kw


def classify_exception(exc: BaseException) -> HarnessError:
    """Map SDK exceptions onto harness failure classes."""
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = None
        header = exc.response.headers.get("retry-after") if exc.response is not None else None
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        return RetryableError("rate limited", retry_after_s=retry_after, cause=exc)
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return RetryableError(f"connection: {exc}", cause=exc)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409, 429):
            return RetryableError(f"status {exc.status_code}: {exc.message}", cause=exc)
        return FatalError(f"status {exc.status_code}: {exc.message}", cause=exc)
    if isinstance(exc, HarnessError):
        return exc
    return FatalError(f"unexpected: {exc!r}", cause=exc)


def translate_response(msg: Any, *, latency_ms: int) -> CompletionResponse:
    """Translate an SDK ``Message`` (or ``BetaMessage``) into a :class:`CompletionResponse`."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in msg.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
    usage = msg.usage
    served_by_fallback = False
    iterations = getattr(usage, "iterations", None) or []
    for it in iterations:
        if getattr(it, "type", None) == "fallback_message":
            served_by_fallback = True
    refusal_category = None
    stop_details = getattr(msg, "stop_details", None)
    if msg.stop_reason == "refusal" and stop_details is not None:
        refusal_category = getattr(stop_details, "category", None)
    return CompletionResponse(
        model=msg.model,
        text="".join(text_parts),
        tool_calls=tool_calls,
        stop_reason=_STOP_MAP.get(msg.stop_reason or "", StopReason.OTHER),
        usage=Usage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        ),
        request_id=getattr(msg, "_request_id", None),
        refusal_category=refusal_category,
        served_by_fallback=served_by_fallback,
        latency_ms=latency_ms,
    )


class AnthropicProvider:
    """``ModelProvider`` for the Anthropic API."""

    name = "anthropic"

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        timeout_s: float = 600.0,
        cache: CachePolicy | None = None,
        cache_min_tokens: dict[str, int] | None = None,
        refusal_fallback_models: frozenset[str] = frozenset(),
    ) -> None:
        self._client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=0)
        self.cache = cache or CachePolicy()
        self.cache_min_tokens = cache_min_tokens or {}
        self.refusal_fallback_models = refusal_fallback_models

    # -- helpers -------------------------------------------------------------

    def _kwargs(self, request: CompletionRequest, schema: dict[str, Any] | None) -> dict[str, Any]:
        return build_kwargs(
            request,
            cache=self.cache,
            cache_min_tokens=self.cache_min_tokens.get(request.model, 0),
            schema=schema,
        )

    def _uses_fallback(self, model: str) -> bool:
        return model in self.refusal_fallback_models

    def _create(
        self, request: CompletionRequest, schema: dict[str, Any] | None
    ) -> CompletionResponse:
        kw = self._kwargs(request, schema)
        t0 = time.perf_counter()
        try:
            if self._uses_fallback(request.model):
                msg = self._client.beta.messages.create(
                    **kw, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}
                )
            else:
                msg = self._client.messages.create(**kw)
        except Exception as exc:
            raise classify_exception(exc) from exc
        return translate_response(msg, latency_ms=int((time.perf_counter() - t0) * 1000))

    # -- protocol ------------------------------------------------------------

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._create(request, None)

    def structured(self, request: CompletionRequest, schema: dict[str, Any]) -> CompletionResponse:
        return self._create(request, schema)

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent | CompletionResponse]:
        kw = self._kwargs(request, request.output_schema)
        t0 = time.perf_counter()
        cm: Any
        try:
            if self._uses_fallback(request.model):
                cm = self._client.beta.messages.stream(
                    **kw, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}
                )
            else:
                cm = self._client.messages.stream(**kw)
            with cm as s:
                for delta in s.text_stream:
                    yield StreamEvent(delta=delta)
                final = s.get_final_message()
        except Exception as exc:
            raise classify_exception(exc) from exc
        yield translate_response(final, latency_ms=int((time.perf_counter() - t0) * 1000))

    def count_tokens(self, request: CompletionRequest) -> int:
        kw = self._kwargs(request, None)
        kw.pop("max_tokens", None)
        kw.pop("output_config", None)
        kw.pop("stop_sequences", None)
        try:
            result = self._client.messages.count_tokens(**kw)
        except Exception as exc:
            raise classify_exception(exc) from exc
        return int(result.input_tokens)
