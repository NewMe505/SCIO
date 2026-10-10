from __future__ import annotations

from pathlib import Path

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.aci.toolbox import ToolBox
from scio.context.artifacts import ArtifactStore
from scio.context.assembler import coder_message, gate_message, router_message, tag_context
from scio.context.trim import PLACEHOLDER, total_chars, trim_messages
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider, FakeReply
from scio.harness.types import Message, StopReason, ToolCall, ToolResultPart
from scio.loop.agent_loop import AgentLoop
from scio.loop.exits import LoopLimits, check_exit
from scio.prompt.registry import PromptRegistry
from scio.state.models import Budget, ExitReason, FeatureItem, ProgressLedger, Role, TaskSpec

TASK = TaskSpec(
    title="Add slugify", description="d", acceptance=["slugify lowercases"], repo_url="r"
)


def test_artifact_store_roundtrip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "t1")
    ref = store.put("test_output", "x" * 5000, produced_by=Role.TESTER)
    assert ref.sha256 and ref.tokens and Path(ref.path).exists()
    assert store.get(ref) == "x" * 5000
    assert store.head(ref, 100).startswith("x" * 100) and "more characters" in store.head(ref, 100)


def test_assembler_projections() -> None:
    msg = coder_message(
        TASK,
        [FeatureItem(id="F1", description="slugify lowercases")],
        repo_root="/w",
        attempt=2,
        last_verify="FAILED x",
    )
    assert "# Task: Add slugify" in msg and "F1 [failing]" in msg and "Repository root: /w" in msg
    assert "[context from verify]" in msg and "Attempt: 2" in msg
    assert "Classify this task" in router_message(TASK, repo_stats="3 files")
    g = gate_message(
        TASK, [], diff_stat="1 file", test_output="ok", lint_output="", docs_check=None
    )
    assert "objective actually met" in g and "Lint:\nclean" in g
    assert tag_context("explorer", " hi ") == "[context from explorer]\nhi"


def test_trim_shortens_old_tool_results_only() -> None:
    msgs = [Message.user("start")]
    for i in range(6):
        msgs.append(Message(role="assistant", parts=[]))
        msgs.append(
            Message.tool_results([ToolResultPart(tool_use_id=str(i), content="y" * 20_000)])
        )
    before = total_chars(msgs)
    out = trim_messages(msgs, max_chars=50_000, keep_last=2)
    assert total_chars(out) < before and len(out) == len(msgs)
    results = [m for m in out if m.parts and m.parts[0].type == "tool_result"]
    assert [r.parts[0].content == PLACEHOLDER for r in results] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]  # type: ignore[union-attr]
    assert trim_messages(msgs[:3], max_chars=10**9) == msgs[:3]


def test_check_exit_order() -> None:
    lim = LoopLimits(max_iters=3, max_stall=1)
    b = Budget(tokens_max=100, usd_max=1, advisor_calls_max=0)
    assert check_exit(ProgressLedger(), b, lim) is None
    assert check_exit(ProgressLedger(iterations=3), b, lim) is ExitReason.MAX_ITERS
    assert check_exit(ProgressLedger(stall_count=2), b, lim) is ExitReason.STALL
    assert (
        check_exit(ProgressLedger(), b.model_copy(update={"tokens_used": 100}), lim)
        is ExitReason.BUDGET
    )


WORKER_JSON = (
    '{"step_id": "S1", "summary": "did it", "files_touched": ["/w/a.py"], "confidence": "high", '
    '"needs_design_decision": false, "next_action": "verify", "tests_added": ["a"]}'
)


def _tool(name: str, **inp: object) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(id=f"c-{name}", name=name, input=dict(inp))],
        stop_reason=StopReason.TOOL_USE,
    )


def test_agent_loop_runs_tools_then_summarizes(
    harness: Harness, fake: FakeProvider, tmp_path: Path
) -> None:
    sb = LocalSandbox(tmp_path)
    sb.write_text(f"{sb.root}/README.md", "hello\n")
    fake.enqueue(
        _tool("view_file", path=f"{sb.root}/README.md"),
        FakeReply(text="done"),
        FakeReply(text=WORKER_JSON),
    )
    tb = ToolBox(sb, ["view_file", "edit_file"], human_gate=False, max_calls=10)
    res = AgentLoop(harness, tb, PromptRegistry(), "coder").run("go", tags={"t": "1"})
    assert res.stop == "end_turn" and res.tool_calls == 1 and res.model_calls == 3
    assert res.output.tests_added == ["a"]
    roles = [m.role for m in res.messages]
    assert roles == ["user", "assistant", "user", "assistant", "assistant"]
    assert "1| hello" in res.messages[2].parts[0].content  # type: ignore[union-attr]
    assert "tool_result: " in res.transcript()
    # the summary request carried the whole transcript plus the summary prompt, no tools
    last_req = fake.requests[-1]
    assert (
        last_req.output_schema is not None
        and last_req.tools == []
        and "JSON summary" in last_req.messages[-1].text()
    )


def test_agent_loop_enforces_tool_cap(harness: Harness, fake: FakeProvider, tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    sb.write_text(f"{sb.root}/README.md", "hello\n")
    # 3 allowed + 1 refused tool turns, then the summary
    fake.enqueue(
        *[_tool("view_file", path=f"{sb.root}/README.md") for _ in range(4)],
        FakeReply(text=WORKER_JSON),
    )
    tb = ToolBox(sb, ["view_file"], human_gate=False, max_calls=3)
    res = AgentLoop(harness, tb, PromptRegistry(), "coder", max_model_calls=50).run("go")
    assert res.stop == "tool_cap" and res.tool_calls == 4  # 3 real + the one refused with an error
    refused = res.messages[-2].parts[
        0
    ]  # [..., assistant(tool_use), user(results), assistant(summary)]
    assert (
        refused.type == "tool_result" and refused.is_error and "budget exhausted" in refused.content
    )


def test_agent_loop_model_cap(harness: Harness, fake: FakeProvider, tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    sb.write_text(f"{sb.root}/README.md", "hello\n")
    fake.enqueue(
        *[_tool("view_file", path=f"{sb.root}/README.md") for _ in range(2)],
        FakeReply(text=WORKER_JSON),
    )
    tb = ToolBox(sb, ["view_file"], human_gate=False)
    res = AgentLoop(harness, tb, PromptRegistry(), "coder", max_model_calls=2).run("go")
    assert res.stop == "model_cap" and res.tool_calls == 2


@pytest.mark.parametrize("stop", [StopReason.MAX_TOKENS])
def test_agent_loop_truncated_turn(
    harness: Harness, fake: FakeProvider, tmp_path: Path, stop: StopReason
) -> None:
    sb = LocalSandbox(tmp_path)
    fake.enqueue(FakeReply(text="partial", stop_reason=stop), FakeReply(text=WORKER_JSON))
    res = AgentLoop(
        harness, ToolBox(sb, ["view_file"], human_gate=False), PromptRegistry(), "coder"
    ).run("go")
    assert res.stop == "truncated"
