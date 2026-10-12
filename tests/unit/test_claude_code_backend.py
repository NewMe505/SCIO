"""ADR 0012: the Claude Code backend with a canned ``claude`` runner (no binary, no login).

The provider turns a tool-less request into one restricted ``claude -p`` run and a
structured call into ``--json-schema``; the worker turns a tool-using turn into a
``stream-json`` run plus one ``--resume`` summary call, renders the transcript in the shape
``scio report`` parses, and folds Claude Code's usage into the harness ledger and budget.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.graph.report import parse_transcript
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.failures import FatalError, RetryableError
from scio.harness.providers.claude_code import (
    ClaudeCodeProvider,
    ClaudeRunResult,
    render_messages,
    subprocess_env,
)
from scio.harness.types import CompletionRequest, Message, ToolSpec
from scio.loop.claude_code_worker import ClaudeCodeWorker, allowed_tools_for, parse_stream
from scio.prompt.registry import PromptRegistry
from scio.settings import Settings
from scio.state.models import RouterOutput, WorkerOutput
from scio.toolchain import python_toolchain

HAIKU = ModelsConfig.load(Settings().models_path).role("router").model
"""A real model id from models.yaml, so no id is written in code (check_docs)."""


class FakeClaude:
    """Canned ``claude -p`` runs keyed by the call order; records argv, cwd and stdin."""

    def __init__(
        self, results: list[dict[str, Any] | list[dict[str, Any]] | ClaudeRunResult]
    ) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float = 0.0,
    ) -> ClaudeRunResult:
        self.calls.append((list(argv), str(cwd) if cwd else None, stdin))
        nxt = self.results.pop(0)
        if isinstance(nxt, ClaudeRunResult):
            return nxt
        if isinstance(nxt, list):  # stream-json lines
            return ClaudeRunResult(0, "\n".join(json.dumps(e) for e in nxt), "")
        return ClaudeRunResult(0, json.dumps(nxt), "")


def _result(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": "sess-1",
        "total_cost_usd": 0.01,
        "duration_api_ms": 1200,
        "num_turns": 1,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 40,
        },
        "modelUsage": {HAIKU: {"costUSD": 0.01}},
    }
    base.update(extra)
    return base


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# ---------- provider ----------


def test_subprocess_env_strips_nesting_and_api_key_variables() -> None:
    env = subprocess_env({"CLAUDECODE": "1", "ANTHROPIC_API_KEY": "x", "PATH": "p", "HOME": "h"})
    assert env == {"PATH": "p", "HOME": "h"}


def test_provider_structured_call_is_one_restricted_turn_with_a_schema() -> None:
    fake = FakeClaude(
        [_result(structured_output={"tier": "T0", "rationale": "r", "confidence": 0.9})]
    )
    provider = ClaudeCodeProvider(runner=fake)
    req = CompletionRequest(
        model=HAIKU,
        system="You are the router.",
        messages=[Message.user("classify this")],
        role="router",
        effort="low",
    )
    schema = {"type": "object", "properties": {"tier": {"type": "string"}}}
    resp = provider.structured(req, schema)
    argv, cwd, stdin = fake.calls[0]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert (
        "--restricted" in argv
        and _flag(argv, "--tools") == ""
        and _flag(argv, "--max-turns") == "1"
    )
    assert "--no-session-persistence" in argv and _flag(argv, "--model") == HAIKU
    assert _flag(argv, "--effort") == "low" and json.loads(_flag(argv, "--json-schema")) == schema
    assert stdin == "classify this" and cwd is None
    assert "--system-prompt-file" in argv  # the static prompt travels by file, not argv
    assert json.loads(resp.text) == {"tier": "T0", "rationale": "r", "confidence": 0.9}
    assert resp.model == HAIKU and resp.request_id == "sess-1"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (10, 20)
    assert (resp.usage.cache_read_tokens, resp.usage.cache_write_tokens) == (300, 40)
    assert resp.latency_ms == 1200


def test_provider_complete_renders_a_repair_conversation_and_rejects_tools() -> None:
    fake = FakeClaude([_result(result="plain text")])
    provider = ClaudeCodeProvider(runner=fake)
    req = CompletionRequest(
        model=HAIKU,
        messages=[Message.user("q"), Message.assistant("bad json"), Message.user("fix it")],
    )
    assert provider.complete(req).text == "plain text"
    assert fake.calls[0][2] == render_messages(req)
    assert render_messages(req).startswith("[user]\nq\n\n[assistant]\nbad json")
    with_tools = CompletionRequest(
        model=HAIKU,
        messages=[Message.user("q")],
        tools=[ToolSpec(name="t", description="d", input_schema={"type": "object"})],
    )
    with pytest.raises(FatalError, match="tool-less"):
        provider.complete(with_tools)


def test_provider_failures_are_classified() -> None:
    fake = FakeClaude(
        [
            ClaudeRunResult(1, "", "Error: --json-schema is not a valid JSON Schema"),
            _result(is_error=True, result="API Error: rate_limit exceeded"),
            _result(is_error=True, result="Not logged in"),
            ClaudeRunResult(0, "not json", ""),
        ]
    )
    provider = ClaudeCodeProvider(runner=fake)
    req = CompletionRequest(model=HAIKU, messages=[Message.user("q")])
    with pytest.raises(FatalError, match="exited 1"):
        provider.complete(req)
    with pytest.raises(RetryableError, match="rate_limit"):
        provider.complete(req)
    with pytest.raises(FatalError, match="Not logged in"):
        provider.complete(req)
    with pytest.raises(FatalError, match="no JSON"):
        provider.complete(req)


def test_harness_routes_every_role_through_the_provider_with_the_override() -> None:
    settings = Settings(provider_override="claude_code")
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    fake = FakeClaude(
        [_result(structured_output={"tier": "T1", "rationale": "r", "confidence": 0.5})]
    )
    registry.register("claude_code", ClaudeCodeProvider(runner=fake))
    harness = Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=100_000, usd_max=1.0)),
    )
    req = harness.build_request(
        "router", system="s", messages=[Message.user("route")], tags={"thread": "t"}
    )
    out = harness.structured(req, RouterOutput).value
    assert out.tier.value == "T1"
    assert _flag(fake.calls[0][0], "--model") == config.role("router").model
    entries = harness.ledger.to_dict()
    assert harness.budget.snapshot().usd_used > 0 and harness.budget.snapshot().calls == 1
    assert entries


# ---------- worker ----------

STREAM = [
    {"type": "system", "subtype": "init", "session_id": "sess-w"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Looking at the module."},
                {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "a.py"}},
            ]
        },
        "parent_tool_use_id": None,
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": [{"type": "text", "text": "x = 1"}],
                }
            ]
        },
        "parent_tool_use_id": None,
    },
    {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "sub", "name": "Grep", "input": {}}]},
        "parent_tool_use_id": "tu-parent",  # a subagent's call: not this transcript
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "tu2", "name": "Edit", "input": {"file_path": "a.py"}},
            ]
        },
        "parent_tool_use_id": None,
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu2",
                    "content": "error: x",
                    "is_error": True,
                }
            ]
        },
        "parent_tool_use_id": None,
    },
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Done."}]},
        "parent_tool_use_id": None,
    },
    _result(session_id="sess-w", num_turns=3),
]


def test_parse_stream_renders_the_transcript_the_report_reads() -> None:
    turn = parse_stream(
        "\n".join(json.dumps(e) for e in STREAM) + "\nWaiting for background work\n"
    )
    assert turn.tool_calls == 2 and turn.turns == 3 and turn.result["session_id"] == "sess-w"
    from scio.loop.agent_loop import LoopResult

    text = LoopResult(
        output=None, stop="end_turn", tool_calls=2, model_calls=3, messages=turn.messages
    ).transcript()
    calls = parse_transcript(text)
    assert [(n, e) for n, _, e in calls] == [("Read", False), ("Edit", True)]
    assert "assistant: Looking at the module." in text and "tool_result (error): error: x" in text


def test_allowed_tools_follow_the_toolbox() -> None:
    tc = python_toolchain()
    assert allowed_tools_for(("repo_search", "view_file"), tc) == ["Read", "Glob", "Grep"]
    rules = allowed_tools_for(("repo_search", "view_file", "edit_file", "run_tests"), tc)
    assert rules[:6] == ["Read", "Glob", "Grep", "Edit", "Write", "MultiEdit"]
    assert any(r.startswith("Bash(") and "pytest" in r for r in rules)
    assert any(r.startswith("Bash(ruff") for r in rules)


def test_worker_turn_then_resume_summary_accounts_both_calls(tmp_path: Path) -> None:
    settings = Settings(provider_override="fake", worker_backend="claude_code")
    config = ModelsConfig.load(settings.models_path)
    harness = Harness(
        config,
        ProviderRegistry(config, settings),
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=1_000_000, usd_max=3.0)),
    )
    summary = {
        "step_id": "S1",
        "summary": "added slugify",
        "files_touched": ["a.py"],
        "confidence": "high",
        "needs_design_decision": False,
        "design_question": None,
        "next_action": "verify",
        "tests_added": ["slugify"],
        "sweep": None,
    }
    fake = FakeClaude([STREAM, _result(session_id="sess-w", structured_output=summary)])
    worker = ClaudeCodeWorker(
        harness,
        LocalSandbox(tmp_path),
        PromptRegistry(),
        "coder",
        toolchain=python_toolchain(),
        tool_names=("repo_search", "view_file", "edit_file", "run_tests"),
        max_turns=10,
        runner=fake,
    )
    result = worker.run("# Task: do it", tags={"thread": "t"})
    assert isinstance(result.output, WorkerOutput) and result.output.summary == "added slugify"
    assert result.stop == "end_turn" and result.tool_calls == 2 and result.model_calls == 4
    first, second = fake.calls[0][0], fake.calls[1][0]
    assert _flag(first, "--output-format") == "stream-json" and "--verbose" in first
    assert (
        _flag(first, "--permission-mode") == "acceptEdits"
        and _flag(first, "--permission-prompts") == "none"
    )
    assert _flag(first, "--max-turns") == "10" and float(_flag(first, "--max-budget-usd")) == 3.0
    assert "Edit" in _flag(first, "--allowedTools") and fake.calls[0][1] == str(tmp_path)
    assert fake.calls[0][2] == "# Task: do it"
    assert _flag(second, "--resume") == "sess-w" and _flag(second, "--tools") == ""
    assert "WorkerOutput" not in _flag(second, "--json-schema")  # a wire schema, not a title
    assert json.loads(_flag(second, "--json-schema"))["type"] == "object"
    assert "--no-session-persistence" not in first  # the session must survive for --resume
    system_file = Path(_flag(first, "--system-prompt-file"))
    assert not system_file.exists()  # temp file removed after the run
    snap = harness.budget.snapshot()
    assert snap.calls == 2 and snap.usd_used > 0
    rows = harness.ledger.to_dict()
    assert all(r["provider"] == "claude_code" and r["role"] == "coder" for r in rows["entries"])
    text = result.transcript()
    assert text.startswith("user: # Task: do it") and "<tool_use Read" in text
    assert text.endswith(json.dumps(summary))


def test_worker_retries_an_invalid_summary_once_then_fails(tmp_path: Path) -> None:
    settings = Settings(provider_override="fake")
    config = ModelsConfig.load(settings.models_path)
    harness = Harness(config, ProviderRegistry(config, settings), settings=settings)
    bad = {"step_id": "S1"}  # missing required fields
    fake = FakeClaude([STREAM, _result(structured_output=bad), _result(structured_output=bad)])
    worker = ClaudeCodeWorker(
        harness, LocalSandbox(tmp_path), PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=("edit_file",), runner=fake,
    )  # fmt: skip
    with pytest.raises(FatalError, match="never matched WorkerOutput"):
        worker.run("task")
    assert len(fake.calls) == 3 and "previous reply was invalid" in (fake.calls[2][2] or "")


def test_worker_without_a_result_line_fails_loudly(tmp_path: Path) -> None:
    settings = Settings(provider_override="fake")
    config = ModelsConfig.load(settings.models_path)
    harness = Harness(config, ProviderRegistry(config, settings), settings=settings)
    fake = FakeClaude([ClaudeRunResult(1, "", "Not logged in")])
    worker = ClaudeCodeWorker(
        harness, LocalSandbox(tmp_path), PromptRegistry(), "coder",
        toolchain=python_toolchain(), tool_names=("edit_file",), runner=fake,
    )  # fmt: skip
    with pytest.raises(FatalError, match="no result"):
        worker.run("task")
