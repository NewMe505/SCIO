"""The product eval suite on the Fake (M11): p001 and p002 are built from their fixed
``PRODUCT.md`` through ``scio new --spec`` and ``scio build`` to a release, then graded by
hidden black-box tests that start the product from outside."""

from __future__ import annotations

import shutil
from pathlib import Path

from evals.product_solver import ProductSolver
from evals.runner import TASKS_DIR, load_tasks, run_suite, to_task_like
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.core import Harness
from scio.settings import Settings


def test_the_product_suite_builds_releases_and_passes_its_hidden_tests() -> None:
    report = run_suite("product", provider="fake", trials=1)
    by_task = {r.task_id: r for r in report.results}
    assert set(by_task) == {"p001_cli_todo", "p002_rest_todo"}
    for result in by_task.values():
        assert result.passed and result.error is None, result.notes
        assert "release=v0.1.0" in result.notes and "interventions=0" in result.notes
        assert "hidden=5 passed" in result.notes
    # I-095: the feature runs' spend is in the trial's numbers, not only the orchestrator's
    assert by_task["p001_cli_todo"].usd > 0.03 and by_task["p001_cli_todo"].tokens > 10_000


def test_a_product_that_breaks_the_contract_fails_its_hidden_tests(tmp_path: Path) -> None:
    """The grading is not vacuous: the same build, a hidden test the product cannot meet."""
    task = next(t for t in load_tasks(TASKS_DIR, "product") if t.id == "p001_cli_todo")
    copy = tmp_path / "p001_strict"
    shutil.copytree(task.path, copy)
    hidden = copy / "hidden" / "test_p001_hidden.py"
    hidden.write_text(
        hidden.read_text(encoding="utf-8").replace('"added 1: buy milk"', '"added #1: buy milk"'),
        encoding="utf-8",
        newline="\n",
    )
    like = to_task_like(task).model_copy(update={"path": copy})
    settings = Settings(provider_override="fake")
    harness = Harness.from_settings(
        settings, budget=BudgetGuard(BudgetLimits(tokens_max=20_000_000, usd_max=40.0))
    )
    outcome = ProductSolver().solve(like, harness)
    assert not outcome.passed
    assert "release=v0.1.0" in outcome.notes and "hidden=1 failed, 4 passed" in outcome.notes


def test_scio_new_with_a_spec_bootstraps_without_a_stop(tmp_path: Path) -> None:
    """``scio new --spec``: no interview, no planner, PRODUCT.md written as given."""
    from scio.graph.run_context import RunContext
    from scio.harness.providers.fake import FakeProvider
    from scio.product import run_product

    spec_md = (TASKS_DIR / "p002_rest_todo" / "PRODUCT.md").read_text(encoding="utf-8")
    settings = Settings(
        provider_override="fake",
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )
    calls: list[str] = []

    def responder(req: object) -> object:  # any model call is a failure of this path
        calls.append(getattr(req, "role", "?") or "?")
        raise AssertionError("scio new --spec must not call a model")

    harness = Harness.from_settings(
        settings, budget=BudgetGuard(BudgetLimits(tokens_max=1_000, usd_max=1.0))
    )
    harness.providers.register("fake", FakeProvider(responder=responder))
    product = tmp_path / "product"
    product.mkdir()
    ctx = RunContext.create(
        thread_id="new-spec",
        sandbox_root=product,
        tier="T1",
        tier_pin="T1",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
    )
    try:
        out = run_product(ctx, prompt="from a spec", spec_md=spec_md)
    finally:
        ctx.close()
    assert not out.paused and out.state.phase == "done" and out.state.exit_reason is None
    assert out.state.bootstrap_commit and calls == []
    assert (product / "PRODUCT.md").read_text(encoding="utf-8") == spec_md
    assert (product / "Dockerfile").is_file() and (product / "todo_api").is_dir()
