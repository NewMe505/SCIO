from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from scio.harness.failures import RepairableError, StructuredOutputError
from scio.harness.providers.fake import FakeProvider, FakeReply
from scio.harness.types import CompletionRequest, Message, StopReason
from scio.prompt.registry import PromptError, PromptRegistry
from scio.prompt.repair import parse_structured, structured_call
from scio.prompt.wire_schema import WireSchemaError, wire_schema

# ---------- registry ----------

EXPECTED_ROLES = {
    "router", "compactor", "scoper", "planner", "advisor", "gate",
    "explorer", "coder", "tester", "debugger", "reviewer", "documenter",
}  # fmt: skip


def test_registry_has_every_role_at_v1() -> None:
    reg = PromptRegistry()
    assert set(reg.roles()) == EXPECTED_ROLES
    for role in EXPECTED_ROLES:
        t = reg.get(role)
        assert t.version >= 1 and t.text.strip()
        assert t.placeholders() == []  # v1 templates are fully static (cacheable)


def test_registry_versions_and_render(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "v1.md").write_text("one", encoding="utf-8")
    (tmp_path / "r" / "v3.md").write_text("hello {{ name }}", encoding="utf-8")
    reg = PromptRegistry(tmp_path)
    assert reg.versions("r") == [1, 3]
    assert reg.get("r").version == 3
    assert reg.get("r", 1).text == "one"
    assert reg.get("r").render(name="x") == "hello x"
    with pytest.raises(PromptError):
        reg.get("r").render()
    with pytest.raises(PromptError):
        reg.get("missing")
    assert reg.get("r").id.startswith("r@v3:")


# ---------- wire schema ----------


class Kind(StrEnum):
    A = "a"
    B = "b"


class Inner(BaseModel):
    """An inner model."""

    label: str = Field(min_length=1, description="short label")
    score: float = Field(ge=0, le=1)


class Outer(BaseModel):
    kind: Kind
    mode: Literal["x", "y"]
    inner: Inner
    many: list[Inner] = Field(min_length=1)
    note: str | None = None
    when: datetime


def test_wire_schema_inlines_and_strips() -> None:
    s = wire_schema(Outer)
    assert "$defs" not in s and "$ref" not in str(s)
    assert s["additionalProperties"] is False
    assert s["required"] == ["kind", "mode", "inner", "many", "note", "when"]
    inner = s["properties"]["inner"]
    assert inner["properties"]["label"] == {"type": "string", "description": "short label"}
    assert "minimum" not in inner["properties"]["score"]
    assert "minItems" not in s["properties"]["many"]
    assert s["properties"]["kind"]["enum"] == ["a", "b"]
    assert s["properties"]["mode"]["enum"] == ["x", "y"]
    assert s["properties"]["when"]["format"] == "date-time"
    assert "title" not in s and "default" not in s["properties"]["note"]


def test_wire_schema_rejects_dict_fields_and_recursion() -> None:
    class WithDict(BaseModel):
        m: dict[str, int]

    with pytest.raises(WireSchemaError):
        wire_schema(WithDict)

    class Node(BaseModel):
        children: list[Node] = []

    with pytest.raises(WireSchemaError):
        wire_schema(Node)


# ---------- repair loop ----------


class Answer(BaseModel):
    step_id: str = Field(pattern=r"^S\d+$")
    confidence: Literal["high", "medium", "low"]


def _req() -> CompletionRequest:
    return CompletionRequest(model="fake-model", system="s", messages=[Message.user("go")])


def test_structured_call_succeeds_first_try() -> None:
    fake = FakeProvider([FakeReply(text='{"step_id": "S1", "confidence": "high"}')])
    res = structured_call(lambda r: fake.structured(r, r.output_schema or {}), _req(), Answer)
    assert res.value.step_id == "S1" and res.attempts == 1 and res.repairs == []
    assert fake.requests[0].output_schema is not None


def test_structured_call_repairs_twice_then_succeeds() -> None:
    fake = FakeProvider(
        [
            FakeReply(text="not json"),
            FakeReply(
                text='{"step_id": "nope", "confidence": "high"}'
            ),  # fails local pattern check
            FakeReply(text='```json\n{"step_id": "S2", "confidence": "low"}\n```'),
        ]
    )
    res = structured_call(
        lambda r: fake.structured(r, r.output_schema or {}), _req(), Answer, max_repairs=2
    )
    assert res.value.step_id == "S2"
    assert res.attempts == 3
    assert (
        len(res.repairs) == 2
        and "invalid JSON" in res.repairs[0]
        and "validation" in res.repairs[1]
    )
    # the third request carries both repair rounds in its messages
    third = fake.requests[2]
    assert [m.role for m in third.messages] == ["user", "assistant", "user", "assistant", "user"]
    assert "rejected" in third.messages[-1].text()


def test_structured_call_fails_cleanly_after_cap() -> None:
    fake = FakeProvider(
        [FakeReply(text="x"), FakeReply(text="y"), FakeReply(text="z"), FakeReply(text="never")]
    )
    with pytest.raises(StructuredOutputError) as ei:
        structured_call(
            lambda r: fake.structured(r, r.output_schema or {}), _req(), Answer, max_repairs=2
        )
    assert ei.value.attempts == 3 and len(ei.value.errors) == 3
    assert fake.calls == 3


def test_parse_structured_flags_truncation() -> None:
    fake = FakeProvider([FakeReply(text='{"step_id": "S1"', stop_reason=StopReason.MAX_TOKENS)])
    with pytest.raises(RepairableError, match="truncated"):
        parse_structured(fake.complete(_req()), Answer)
