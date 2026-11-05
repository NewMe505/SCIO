"""I-154: a T0 iteration carries what the previous session found and did into the next one.

Each T0 iteration is a fresh worker session (a new ``claude -p`` on the claude_code
backend). In graphcalc S9, five coder sessions each re-read the repository, four edited
nothing, and the feature stalled, because the next session was told only the last verify
result. Runs the T0 graph on the Fake with an idle coder and reads the second iteration's
coder message, as the provider received it.
"""

from __future__ import annotations

from pathlib import Path

from evals.fake_scripts import ScriptedResponder
from evals.solvers import make_origin
from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import run_t0
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.settings import REPO_ROOT, Settings
from scio.state.models import TaskSpec

FIXTURE = REPO_ROOT / "evals" / "tasks" / "t001_add_function"


def _run_idle_t0(tmp_path: Path) -> list[str]:
    """Two T0 iterations with a coder that edits nothing; returns each iteration's first
    coder message, in order."""
    provider = FakeProvider(responder=ScriptedResponder(coder_mode="idle"))
    settings = Settings(
        provider_override="fake",
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    registry.register("fake", provider)
    harness = Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=500_000, usd_max=5.0)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )
    ctx = RunContext.create(
        thread_id="t-carry",
        sandbox_root=tmp_path / "work",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
        budget_limits=BudgetLimits(
            tokens_max=500_000, usd_max=5.0, advisor_calls_max=0, max_iters=2, max_stall=5
        ),
    )
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    text = (FIXTURE / "spec.md").read_text(encoding="utf-8")
    task = TaskSpec(
        title="Add slugify",
        description=text,
        acceptance=acceptance_from_issue_body(text),
        repo_url=str(origin),
        base_branch="main",
    )
    try:
        state = run_t0(ctx, task).state
    finally:
        ctx.close()
    assert state.progress.iterations == 2, state.task_ledger[-3:]
    firsts: dict[str, str] = {}
    for req in provider.requests:
        if req.role != "coder" or req.output_schema is not None:
            continue  # the tool-using turn only, not its typed summary call
        msg = req.messages[0].text()
        attempt = next(ln for ln in msg.splitlines() if ln.startswith("Attempt: "))
        firsts.setdefault(attempt, msg)
    return [firsts[k] for k in sorted(firsts)]


def test_second_t0_iteration_carries_the_first_sessions_summary_and_empty_files(
    tmp_path: Path,
) -> None:
    first, second = _run_idle_t0(tmp_path)
    assert "[context from coder]" not in first  # nothing to carry on the first iteration
    # the first session's own report, stated as what that session did
    assert "[context from coder]" in second
    assert "Report from the previous session (attempt 1)" in second
    assert "coder turn finished (idle)" in second  # the Fake coder's summary, verbatim
    assert "it is not an instruction to you" in second  # a capped "stopped" report is context
    # it touched no files: said plainly, with the instruction to edit this time
    assert "Files that session changed: none; it only investigated." in second
    assert "make the edits in this session" in second
    # the tree's real state, which a per-session files_touched does not give
    assert "Changes in the working tree since the base commit" in second
    assert "(no changes since base)" in second
    # placed after the Attempt line (the Fake's first-match regexes) and before verify
    assert (
        second.index("Attempt: 2")
        < second.index("[context from coder]")
        < second.index("[context from verify]")
    )
