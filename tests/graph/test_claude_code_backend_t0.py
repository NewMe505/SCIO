"""ADR 0012 acceptance on the Fake: t001 runs through the T0 graph with the Claude Code
worker backend. A canned ``claude`` runner plays the coder and documenter (it performs the
scripted edits in the sandbox and emits ``stream-json``), the tool-less roles go through
the Fake provider, and verify runs the real pytest and ruff on the result: the typed handoff
between SCIO and a Claude Code turn, and ground truth staying with SCIO."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from evals.fake_scripts import (
    SLUGIFY_NEW,
    SLUGIFY_OLD,
    SLUGIFY_TESTS,
    ScriptedResponder,
    changelog,
)
from evals.solvers import make_origin
from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import resume_t0, run_t0
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.claude_code import ClaudeRunResult
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.loop import backends
from scio.settings import REPO_ROOT, Settings
from scio.state.models import ExitReason, FeatureStatus, Role, TaskSpec

FIXTURE = REPO_ROOT / "evals" / "tasks" / "t001_add_function"
CODER_MODEL = ModelsConfig.load(Settings().models_path).role("coder").model


def _flag(argv: Sequence[str], name: str) -> str | None:
    return argv[list(argv).index(name) + 1] if name in argv else None


class ClaudeActor:
    """A canned ``claude -p`` that edits files in its cwd like the real binary would."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sessions = 0

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float = 0.0,
        env: Any = None,  # the worker passes the product's environment (I-114)
    ) -> ClaudeRunResult:
        self.calls.append(list(argv))
        root = Path(str(cwd))
        if _flag(argv, "--resume"):  # the typed summary turn
            role = "documenter" if self.sessions % 2 == 0 else "coder"
            touched = (
                ["CHANGELOG.md"]
                if role == "documenter"
                else [
                    "textutils/__init__.py",
                    "tests/test_slugify.py",
                ]
            )
            out = {
                "step_id": "S1",
                "summary": f"{role} turn done inside Claude Code",
                "files_touched": [str(root / p) for p in touched],
                "confidence": "high",
                "needs_design_decision": False,
                "design_question": None,
                "next_action": "verify",
                "tests_added": ["slugify"] if role == "coder" else [],
                "sweep": None,
            }
            return ClaudeRunResult(0, json.dumps(self._result("s", structured_output=out)), "")
        self.sessions += 1
        name = _flag(argv, "--name") or ""
        events: list[dict[str, Any]] = [{"type": "system", "subtype": "init"}]
        if name.endswith("coder"):
            edits = [
                ("textutils/__init__.py", SLUGIFY_OLD, SLUGIFY_NEW),
                ("tests/test_slugify.py", "", SLUGIFY_TESTS),
            ]
        else:
            e = changelog("`slugify(text)` in `textutils` (written by Claude Code).")
            edits = [(e.path, e.old, e.new)]
        for i, (rel, old, new) in enumerate(edits, 1):
            path = root / rel
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                text.replace(old, new, 1) if old else new, encoding="utf-8", newline="\n"
            )
            events.append(
                {
                    "type": "assistant",
                    "parent_tool_use_id": None,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"tu{i}",
                                "name": "Edit" if old else "Write",
                                "input": {"file_path": str(path)},
                            }
                        ]
                    },
                }
            )
            events.append(
                {
                    "type": "user",
                    "parent_tool_use_id": None,
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": f"tu{i}", "content": "ok"}
                        ]
                    },
                }
            )
        events.append(self._result(f"sess-{self.sessions}", num_turns=len(edits) + 1))
        return ClaudeRunResult(0, "\n".join(json.dumps(e) for e in events), "")

    @staticmethod
    def _result(session: str, **extra: Any) -> dict[str, Any]:
        base = {
            "type": "result",
            "is_error": False,
            "result": "done",
            "session_id": session,
            "duration_api_ms": 500,
            "num_turns": 1,
            "usage": {
                "input_tokens": 50,
                "output_tokens": 80,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "modelUsage": {CODER_MODEL: {"costUSD": 0.001}},
        }
        base.update(extra)
        return base


def _ctx(tmp_path: Path) -> RunContext:
    settings = Settings(
        provider_override="fake",
        worker_backend="claude_code",
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    registry.register("fake", FakeProvider(responder=ScriptedResponder()))
    harness = Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=500_000, usd_max=5.0)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )
    return RunContext.create(
        thread_id="t-cc",
        sandbox_root=tmp_path / "work",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
        budget_limits=BudgetLimits(
            tokens_max=500_000, usd_max=5.0, advisor_calls_max=0, max_iters=10, max_stall=2
        ),
    )


def test_t0_runs_through_claude_code_turns_with_verify_as_ground_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = ClaudeActor()
    monkeypatch.setattr(backends, "RUNNER_OVERRIDE", actor)
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    text = (FIXTURE / "spec.md").read_text(encoding="utf-8")
    task = TaskSpec(
        title="Add slugify",
        description=text,
        acceptance=acceptance_from_issue_body(text),
        repo_url=str(origin),
        base_branch="main",
    )
    ctx = _ctx(tmp_path)
    try:
        outcome = run_t0(ctx, task)
        assert outcome.paused and outcome.state.phase == "merge_wait", outcome.state.task_ledger[
            -3:
        ]
        s = resume_t0(ctx, "approved").state
    finally:
        ctx.close()
    assert s.exit_reason is ExitReason.SUCCESS
    assert all(f.status is FeatureStatus.PASSING and f.commit_sha for f in s.feature_list)
    assert s.progress.iterations == 1 and s.progress.tool_calls == 2  # the two Edit calls
    # the coder turn: stream run in the sandbox root, then the resume summary
    coder, summary = actor.calls[0], actor.calls[1]
    assert _flag(coder, "--name") == "scio-coder" and _flag(coder, "--max-turns") == "10"  # T0 cap
    assert (
        "Edit" in (_flag(coder, "--allowedTools") or "") and _flag(summary, "--resume") == "sess-1"
    )
    documenter = next(a for a in actor.calls if _flag(a, "--name") == "scio-documenter")
    assert "Bash(" in (_flag(documenter, "--allowedTools") or "")
    # Claude Code's usage reached the ledger and the budget under the claude_code provider
    rows = ctx.harness.ledger.to_dict()["entries"]
    assert {r["provider"] for r in rows} == {"fake", "claude_code"}
    assert {r["role"] for r in rows if r["provider"] == "claude_code"} == {"coder", "documenter"}
    assert s.budgets.usd_used > 0
    # the transcript artifact is in the shape scio report parses
    log = Path(s.artifacts["coder:1"].path).read_text(encoding="utf-8")
    assert "<tool_use Edit" in log and "<tool_use Write" in log and log.startswith("user: # Task:")
    # ground truth: SCIO's verify ran the real tests on what Claude Code wrote
    work = tmp_path / "work"
    assert "def slugify" in (work / "textutils" / "__init__.py").read_text(encoding="utf-8")
    assert "Claude Code" in (work / "CHANGELOG.md").read_text(encoding="utf-8")
    subjects = subprocess.run(
        ["git", "log", "--format=%s"], cwd=work, capture_output=True, text=True
    ).stdout.splitlines()
    assert (
        len(subjects) == 3 and subjects[0].startswith("docs:") and subjects[1].startswith("feat:")
    )
    assert s.artifacts["documenter:1"].produced_by is Role.DOCUMENTER
