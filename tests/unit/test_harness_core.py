from __future__ import annotations

from typing import Literal

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from scio.harness import telemetry as T
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig
from scio.harness.core import Harness
from scio.harness.failures import BudgetExceeded, FatalError, RefusalError, RetryableError
from scio.harness.providers.fake import FakeProvider, FakeReply
from scio.harness.types import Message, StopReason, ToolSpec


class Out(BaseModel):
    step_id: str
    confidence: Literal["high", "medium", "low"]


def test_build_request_uses_role_config(harness: Harness, config: ModelsConfig) -> None:
    req = harness.build_request("planner", system="sys", messages=[Message.user("x")])
    rc = config.role("planner")
    assert (req.model, req.effort, req.max_tokens, req.role) == (
        rc.model,
        rc.effort,
        rc.max_tokens,
        "planner",
    )


def test_complete_routes_to_override_provider_and_accounts(
    harness: Harness, fake: FakeProvider
) -> None:
    req = harness.build_request(
        "coder", system="sys", messages=[Message.user("x")], tags={"step": "S1"}
    )
    resp = harness.complete(req)
    assert resp.text.startswith("fake:")
    assert fake.calls == 1
    entry = harness.ledger.entries[0]
    assert entry.role == "coder" and entry.provider == "fake" and entry.tags == {"step": "S1"}
    assert entry.usd > 0  # priced at the configured model's rate, even though Fake served it
    assert harness.budget.snapshot().calls == 1


def test_complete_retries_retryable_then_succeeds(harness: Harness, fake: FakeProvider) -> None:
    fake.enqueue(FakeReply(raise_exc=RetryableError("429")), FakeReply(text="ok"))
    req = harness.build_request("coder", system="sys", messages=[Message.user("x")])
    assert harness.complete(req).text == "ok"
    assert fake.calls == 2
    assert harness.ledger.entries[0].attempts == 2


def test_complete_propagates_fatal(harness: Harness, fake: FakeProvider) -> None:
    fake.enqueue(FakeReply(raise_exc=FatalError("400")))
    req = harness.build_request("coder", system="sys", messages=[Message.user("x")])
    with pytest.raises(FatalError):
        harness.complete(req)
    assert harness.ledger.entries == []


def test_refusal_is_fatal_but_still_accounted(harness: Harness, fake: FakeProvider) -> None:
    fake.enqueue(FakeReply(text="", stop_reason=StopReason.REFUSAL))
    req = harness.build_request("coder", system="sys", messages=[Message.user("x")])
    with pytest.raises(RefusalError):
        harness.complete(req)
    assert len(harness.ledger.entries) == 1


def test_budget_guard_stops_call_before_provider(
    config: ModelsConfig, settings, fake: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    from scio.harness.config import ProviderRegistry

    reg = ProviderRegistry(config, settings)
    reg.register("fake", fake)
    h = Harness(
        config,
        reg,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=100, usd_max=1.0)),
    )
    req = h.build_request("coder", system="sys", messages=[Message.user("x")], max_tokens=1000)
    with pytest.raises(BudgetExceeded):
        h.complete(req)
    assert fake.calls == 0


def test_structured_via_harness_repairs_and_accounts_every_attempt(
    harness: Harness, fake: FakeProvider
) -> None:
    fake.enqueue(
        FakeReply(text="garbage"), FakeReply(text='{"step_id": "S1", "confidence": "high"}')
    )
    req = harness.build_request("coder", system="sys", messages=[Message.user("x")])
    res = harness.structured(req, Out)
    assert res.value.step_id == "S1" and res.attempts == 2
    assert len(harness.ledger.entries) == 2


def test_tools_pass_through_request(harness: Harness, fake: FakeProvider) -> None:
    tool = ToolSpec(
        name="view_file", description="d", input_schema={"type": "object", "properties": {}}
    )
    req = harness.build_request("coder", system="sys", messages=[Message.user("x")], tools=[tool])
    harness.complete(req)
    assert fake.requests[0].tools[0].name == "view_file"


def test_spans_record_usage_without_content(harness: Harness, spans: InMemorySpanExporter) -> None:
    req = harness.build_request(
        "gate", system="secret system", messages=[Message.user("secret user")]
    )
    harness.complete(req)
    finished = spans.get_finished_spans()
    assert len(finished) == 1
    span = finished[0]
    assert span.name == "invoke_agent gate"
    attrs = dict(span.attributes or {})
    assert attrs[T.OPERATION] == "invoke_agent"
    assert attrs[T.PROVIDER] == "fake"
    assert attrs[T.REQ_MODEL] == req.model
    assert attrs[T.IN_TOKENS] > 0 and attrs[T.OUT_TOKENS] > 0
    assert attrs[T.FINISH_REASONS] == ("end_turn",)
    assert attrs[T.SCIO_ATTEMPTS] == 1
    assert T.INPUT_MESSAGES not in attrs and T.SYSTEM_INSTRUCTIONS not in attrs


def test_spans_capture_content_when_opted_in(
    config: ModelsConfig, settings, fake: FakeProvider, spans: InMemorySpanExporter
) -> None:  # type: ignore[no-untyped-def]
    from scio.harness.config import ProviderRegistry

    settings = settings.model_copy(update={"otel_capture_content": True})
    reg = ProviderRegistry(config, settings)
    reg.register("fake", fake)
    h = Harness(config, reg, settings=settings)
    h.complete(h.build_request("gate", system="S", messages=[Message.user("U")]))
    attrs = dict(spans.get_finished_spans()[0].attributes or {})
    assert attrs[T.SYSTEM_INSTRUCTIONS] == "S" and "U" in str(attrs[T.INPUT_MESSAGES])
    assert T.OUTPUT_MESSAGES in attrs


def test_execute_tool_span(spans: InMemorySpanExporter) -> None:
    with T.execute_tool_span("run_tests", filter="k"):
        pass
    span = spans.get_finished_spans()[0]
    assert span.name == "execute_tool run_tests"
    assert dict(span.attributes or {})[T.TOOL_NAME] == "run_tests"
