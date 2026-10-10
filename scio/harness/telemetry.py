"""OpenTelemetry GenAI spans.

Attribute names follow the OTel GenAI semantic conventions (``gen_ai.*``), which are
still in Development status; the version is pinned in :data:`SEMCONV_VERSION` and any
rename is a one-line change here. Prompt and completion text are recorded only when
``SCIO_OTEL_CAPTURE_CONTENT=1``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, StatusCode

from scio.harness.types import CompletionRequest, CompletionResponse

SEMCONV_VERSION = "1.37.0"

# gen_ai.* attribute keys (pinned, see SEMCONV_VERSION)
OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQ_MODEL = "gen_ai.request.model"
RES_MODEL = "gen_ai.response.model"
RES_ID = "gen_ai.response.id"
IN_TOKENS = "gen_ai.usage.input_tokens"
OUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_WRITE_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
FINISH_REASONS = "gen_ai.response.finish_reasons"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
# scio.* keys
SCIO_USD = "scio.usd"
SCIO_ATTEMPTS = "scio.attempts"
SCIO_THREAD_ID = "scio.thread_id"

_TRACER_NAME = "scio"
_configured_provider: TracerProvider | None = None


def configure_tracing(
    exporter: Literal["none", "console", "otlp"] | SpanExporter = "none",
    *,
    service_name: str = "scio",
    simple: bool = False,
) -> TracerProvider:
    """Install a tracer provider and attach an exporter.

    OpenTelemetry allows one global provider per process. The first call creates it;
    later calls attach their exporter to the existing provider (so tests can add an
    in-memory exporter without fighting the global).
    """
    global _configured_provider
    if _configured_provider is None:
        _configured_provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        trace.set_tracer_provider(_configured_provider)
    provider = _configured_provider
    span_exporter: SpanExporter | None
    if isinstance(exporter, SpanExporter):
        span_exporter = exporter
    elif exporter == "console":
        span_exporter = ConsoleSpanExporter()
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter()  # endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
    else:
        span_exporter = None
    if span_exporter is not None:
        processor = (
            SimpleSpanProcessor(span_exporter) if simple else BatchSpanProcessor(span_exporter)
        )
        provider.add_span_processor(processor)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME, SEMCONV_VERSION)


@contextmanager
def invoke_agent_span(
    request: CompletionRequest, *, provider: str, capture_content: bool = False
) -> Iterator[Span]:
    """Span for one model call made on behalf of a SCIO role."""
    role = request.role or "model"
    with tracer().start_as_current_span(f"invoke_agent {role}") as span:
        span.set_attribute(OPERATION, "invoke_agent")
        span.set_attribute(AGENT_NAME, role)
        span.set_attribute(PROVIDER, provider)
        span.set_attribute(REQ_MODEL, request.model)
        for k, v in request.tags.items():
            span.set_attribute(f"scio.tag.{k}", v)
        if capture_content:
            span.set_attribute(SYSTEM_INSTRUCTIONS, request.system)
            span.set_attribute(
                INPUT_MESSAGES,
                json.dumps([m.model_dump(mode="json") for m in request.messages]),
            )
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise


def record_response(
    span: Span,
    response: CompletionResponse,
    *,
    usd: float,
    attempts: int,
    capture_content: bool = False,
) -> None:
    span.set_attribute(RES_MODEL, response.model)
    if response.request_id:
        span.set_attribute(RES_ID, response.request_id)
    span.set_attribute(IN_TOKENS, response.usage.input_tokens)
    span.set_attribute(OUT_TOKENS, response.usage.output_tokens)
    span.set_attribute(CACHE_READ_TOKENS, response.usage.cache_read_tokens)
    span.set_attribute(CACHE_WRITE_TOKENS, response.usage.cache_write_tokens)
    span.set_attribute(FINISH_REASONS, [response.stop_reason.value])
    span.set_attribute(SCIO_USD, usd)
    span.set_attribute(SCIO_ATTEMPTS, attempts)
    if capture_content:
        span.set_attribute(
            OUTPUT_MESSAGES, json.dumps(response.as_assistant_message().model_dump(mode="json"))
        )


@contextmanager
def execute_tool_span(tool_name: str, **attrs: Any) -> Iterator[Span]:
    """Span for one tool execution (used by the ACI layer from M2)."""
    with tracer().start_as_current_span(f"execute_tool {tool_name}") as span:
        span.set_attribute(OPERATION, "execute_tool")
        span.set_attribute(TOOL_NAME, tool_name)
        for k, v in attrs.items():
            span.set_attribute(f"scio.{k}", v)
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise
