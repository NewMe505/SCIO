"""Config loading and provider request/response translation, without network."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scio.harness.caching import CachePolicy
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.providers import anthropic as A
from scio.harness.providers import openai as O
from scio.harness.types import (
    CompletionRequest,
    Message,
    StopReason,
    TextPart,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
)
from scio.settings import Settings

# ---------- models.yaml ----------


def test_models_config_roles_resolve(config: ModelsConfig) -> None:
    for role, rc in config.roles.items():
        m = config.model(rc.model)
        assert m.provider in config.providers, role
        assert rc.max_tokens <= m.max_output_tokens
    assert set(config.roles) >= {
        "router",
        "planner",
        "advisor",
        "gate",
        "coder",
        "reviewer",
        "documenter",
    }


def test_models_config_rejects_dangling_refs() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        ModelsConfig.model_validate(
            {
                "providers": {},
                "models": {
                    "m": {
                        "provider": "x",
                        "input_per_mtok": 1,
                        "output_per_mtok": 1,
                        "context_window": 1,
                        "max_output_tokens": 1,
                    }
                },
            }
        )
    with pytest.raises(ValueError, match="unknown model"):
        ModelsConfig.model_validate(
            {"providers": {}, "models": {}, "roles": {"r": {"model": "nope"}}}
        )


def test_provider_override_and_lazy_registry(config: ModelsConfig, settings: Settings) -> None:
    assert config.provider_name_for("fake-model") == "fake"
    assert config.provider_name_for("fake-model", override="anthropic") == "anthropic"
    reg = ProviderRegistry(config, settings)
    assert reg.get("fake").name == "fake"
    assert reg.get("fake") is reg.get("fake")
    with pytest.raises(KeyError):
        reg.get("nope")


# ---------- anthropic translation ----------


def _req(**kw: Any) -> CompletionRequest:
    base: dict[str, Any] = {
        "model": "m",
        "system": "S" * 100,
        "messages": [
            Message.user("hi"),
            Message(
                role="assistant",
                parts=[
                    TextPart(text="calling"),
                    ToolUsePart(id="t1", name="view_file", input={"path": "/a"}),
                ],
            ),
            Message.tool_results(
                [ToolResultPart(tool_use_id="t1", content="contents", is_error=True)]
            ),
        ],
        "tools": [
            ToolSpec(
                name="view_file", description="d", input_schema={"type": "object", "properties": {}}
            )
        ],
        "effort": "high",
        "max_tokens": 123,
    }
    base.update(kw)
    return CompletionRequest(**base)


def test_anthropic_kwargs_shape_and_cache_breakpoints() -> None:
    kw = A.build_kwargs(
        _req(), cache=CachePolicy(ttl="1h"), cache_min_tokens=0, schema={"type": "object"}
    )
    assert kw["model"] == "m" and kw["max_tokens"] == 123
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert kw["tools"][-1]["cache_control"]["type"] == "ephemeral"
    assert kw["tools"][0]["strict"] is True
    assert kw["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert "thinking" not in kw  # adaptive by default on current models
    m = kw["messages"]
    assert m[0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert m[1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "view_file",
        "input": {"path": "/a"},
    }
    assert m[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "contents",
        "is_error": True,
    }


def test_anthropic_skips_cache_marker_below_model_minimum() -> None:
    kw = A.build_kwargs(_req(), cache=CachePolicy(), cache_min_tokens=4096)
    assert kw["system"] == "S" * 100  # plain string, no breakpoint on a too-short prefix


def test_anthropic_translate_response_and_fallback_signal() -> None:
    msg = SimpleNamespace(
        model="m-served",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="hello "),
            SimpleNamespace(type="text", text="world"),
            SimpleNamespace(type="tool_use", id="x", name="run_tests", input={"filter": "k"}),
        ],
        stop_reason="tool_use",
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=None,
            iterations=[SimpleNamespace(type="fallback_message")],
        ),
        _request_id="req_1",
    )
    r = A.translate_response(msg, latency_ms=7)
    assert r.text == "hello world" and r.tool_calls[0].name == "run_tests"
    assert r.stop_reason is StopReason.TOOL_USE and r.usage.cache_read_tokens == 100
    assert r.served_by_fallback and r.request_id == "req_1" and r.model == "m-served"


def test_anthropic_translate_refusal_category() -> None:
    msg = SimpleNamespace(
        model="m",
        content=[],
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber"),
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
        _request_id=None,
    )
    r = A.translate_response(msg, latency_ms=1)
    assert r.stop_reason is StopReason.REFUSAL and r.refusal_category == "cyber"


def test_anthropic_exception_classification() -> None:
    import anthropic
    import httpx2 as httpx

    def status(
        code: int, cls: type[anthropic.APIStatusError], headers: dict[str, str] | None = None
    ) -> anthropic.APIStatusError:
        resp = httpx.Response(
            code, headers=headers or {}, request=httpx.Request("POST", "https://x")
        )
        return cls("msg", response=resp, body=None)

    from scio.harness.failures import FatalError, RetryableError

    rl = A.classify_exception(status(429, anthropic.RateLimitError, {"retry-after": "3"}))
    assert isinstance(rl, RetryableError) and rl.retry_after_s == 3.0
    assert isinstance(
        A.classify_exception(status(500, anthropic.InternalServerError)), RetryableError
    )
    assert isinstance(A.classify_exception(status(400, anthropic.BadRequestError)), FatalError)
    assert isinstance(A.classify_exception(status(401, anthropic.AuthenticationError)), FatalError)
    assert isinstance(
        A.classify_exception(
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
        ),
        RetryableError,
    )


# ---------- openai translation ----------


def test_openai_kwargs_shape() -> None:
    kw = O.build_kwargs(_req(), schema={"type": "object"})
    assert kw["max_completion_tokens"] == 123 and kw["reasoning_effort"] == "high"
    assert kw["response_format"]["json_schema"]["strict"] is True
    msgs = kw["messages"]
    assert msgs[0] == {"role": "system", "content": "S" * 100}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "view_file"
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "contents"}
    assert kw["tools"][0]["function"]["strict"] is True


def test_openai_translate_response() -> None:
    resp = SimpleNamespace(
        model="g",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    refusal=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="c1", function=SimpleNamespace(name="f", arguments='{"a": 1}')
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=50,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
        ),
    )
    r = O.translate_response(resp, latency_ms=1)
    assert r.stop_reason is StopReason.TOOL_USE and r.tool_calls[0].input == {"a": 1}
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (30, 20, 5)
