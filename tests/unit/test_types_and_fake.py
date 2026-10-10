from __future__ import annotations

from scio.harness.providers.fake import FakeProvider, FakeReply, instance_from_schema
from scio.harness.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    StopReason,
    StreamEvent,
    ToolCall,
)


def _req(text: str = "hi") -> CompletionRequest:
    return CompletionRequest(model="fake-model", system="sys", messages=[Message.user(text)])


def test_fingerprint_is_stable_and_ignores_tags() -> None:
    a = _req().model_copy(update={"tags": {"x": "1"}})
    b = _req().model_copy(update={"tags": {"x": "2"}})
    assert a.fingerprint() == b.fingerprint()
    assert _req("other").fingerprint() != a.fingerprint()


def test_fake_default_echo_is_deterministic() -> None:
    p = FakeProvider()
    r1 = p.complete(_req())
    r2 = FakeProvider().complete(_req())
    assert r1.text == r2.text
    assert r1.stop_reason is StopReason.END_TURN
    assert r1.usage.input_tokens > 0
    assert p.requests[0].messages[0].text() == "hi"


def test_fake_script_is_consumed_in_order() -> None:
    p = FakeProvider(
        [FakeReply(text="one"), FakeReply(text="two", stop_reason=StopReason.MAX_TOKENS)]
    )
    assert p.complete(_req()).text == "one"
    r = p.complete(_req())
    assert r.text == "two"
    assert r.stop_reason is StopReason.MAX_TOKENS
    assert p.complete(_req()).text.startswith("fake:")  # back to default


def test_fake_responder_and_tool_calls() -> None:
    def responder(req: CompletionRequest) -> FakeReply:
        return FakeReply(
            tool_calls=[ToolCall(id="t1", name="view_file", input={"path": "/a"})],
            stop_reason=StopReason.TOOL_USE,
        )

    r = FakeProvider(responder=responder).complete(_req())
    assert r.tool_calls[0].name == "view_file"
    msg = r.as_assistant_message()
    assert msg.role == "assistant" and msg.parts[0].type == "tool_use"


def test_fake_stream_yields_deltas_then_response() -> None:
    p = FakeProvider([FakeReply(text="a" * 40)])
    events = list(p.stream(_req()))
    deltas = [e for e in events if isinstance(e, StreamEvent)]
    assert "".join(d.delta for d in deltas) == "a" * 40
    assert isinstance(events[-1], CompletionResponse)


def test_fake_structured_default_matches_schema() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "n": {"type": "integer"},
            "kind": {"enum": ["a", "b"]},
            "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "n", "kind", "maybe", "items"],
        "additionalProperties": False,
    }
    inst = instance_from_schema(schema)
    assert inst == {"items": [], "kind": "a", "maybe": "fake", "n": 0, "name": "fake"}
    r = FakeProvider().structured(_req(), schema)
    assert '"name": "fake"' in r.text
