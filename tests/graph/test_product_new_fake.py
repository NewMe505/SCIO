"""M7 acceptance on the Fake provider: an empty directory becomes a repository with green
local CI and a passing smoke test, through two human stops, and a second resume after the
bootstrap duplicates nothing (ADR 0010)."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from evals.fake_scripts import ScriptedResponder
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.product import resume_product, run_product
from scio.product.graph import BOOTSTRAP_MARKER
from scio.product.spec import normalise_spec, render_product_md, slugify
from scio.settings import Settings
from scio.state.models import ProductFeature, ProductSpecOutput
from scio.toolchain import python_toolchain

PROMPT = "A CLI todo app with tags and JSON export"


def _ctx(tmp_path: Path, thread: str = "new-1") -> RunContext:
    settings = Settings(
        provider_override="fake",
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
        thread_id=thread,
        sandbox_root=tmp_path / "product",
        tier="T1",
        tier_pin="T1",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_empty_directory_becomes_a_repository_with_green_ci(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        # 1. interview: pauses with 2-4 questions
        out = run_product(ctx, prompt=PROMPT)
        assert out.paused and out.interrupt is not None
        assert 2 <= len(out.interrupt["questions"]) <= 4
        assert out.state.phase == "answers" and out.state.spec is None

        # 2. answers -> spec -> pauses for approval with PRODUCT.md in the payload
        out = resume_product(ctx, "1: developers; 2: add and list; 3: no sync")
        assert out.paused and out.interrupt is not None
        assert out.interrupt["product_md"].startswith("# Todo CLI")
        assert "### P1: Walking skeleton" in out.interrupt["product_md"]  # normaliser inserted it

        # 3. approve -> bootstrap -> done
        out = resume_product(ctx, "approve")
        assert not out.paused, out.interrupt
        s = out.state
        assert s.exit_reason is None and s.phase == "done" and s.approved
        assert s.spec is not None and s.spec.name == "todo_cli" and s.spec.stack == "python"
        assert s.spec.features[0].id == "P1" and len(s.spec.features) == 3
        assert s.bootstrap_commit and s.budgets.usd_used > 0
        assert any("approved" in e.text for e in s.ledger)
    finally:
        ctx.close()

    repo = tmp_path / "product"
    assert (repo / ".git").is_dir()
    for rel in (
        "PRODUCT.md",
        "NOTES.md",
        ".scio/product/progress.json",
        "pyproject.toml",
        "todo_cli/__init__.py",
        "todo_cli/cli.py",
        "tests/test_smoke.py",
        ".github/workflows/ci.yml",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        ".gitignore",
    ):
        assert (repo / rel).is_file(), rel
    progress = json.loads((repo / ".scio/product/progress.json").read_text(encoding="utf-8"))
    assert progress["product"] == "todo_cli" and progress["bootstrap_commit"] == s.bootstrap_commit
    # ADR 0011 decision 1: the scaffold proves the toolchain; P1 is verified by its own run.
    assert [f["status"] for f in progress["features"]] == ["todo"] * 3
    assert progress["milestones"] == []
    subjects = _git(repo, "log", "--format=%s").splitlines()
    assert len(subjects) == 2 and BOOTSTRAP_MARKER in subjects[-1]
    assert _git(repo, "status", "--porcelain") == ""  # nothing left uncommitted
    assert "__pycache__" not in _git(repo, "ls-files")

    # 4. the repository's own local CI is green under the profile (ground truth, not a verdict)
    tc = python_toolchain()
    lint = subprocess.run(tc.lint_argv("."), cwd=repo, capture_output=True, text=True)
    assert lint.returncode in (0, 127), lint.stdout
    tests = subprocess.run(
        tc.test_argv(filter=None, path=None), cwd=repo, capture_output=True, text=True
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "3 passed" in tests.stdout  # two smoke tests and the black-box version test (M9)
    version = subprocess.run(
        ["python", "-m", "todo_cli", "--version"], cwd=repo, capture_output=True, text=True
    )
    assert version.returncode == 0 and version.stdout.strip() == "0.1.0"


def test_resume_after_bootstrap_duplicates_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "new-2")
    try:
        run_product(ctx, prompt=PROMPT)
        resume_product(ctx, "answers")
        first = resume_product(ctx, "approve")
        again = resume_product(ctx, None)  # crash-style resume with no answer
        assert again.state.bootstrap_commit == first.state.bootstrap_commit
        assert again.state.phase == "done" and not again.paused
    finally:
        ctx.close()
    assert len(_git(tmp_path / "product", "log", "--format=%s").splitlines()) == 2


def test_rejected_spec_is_revised_then_halts_after_the_cap(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "new-3")
    try:
        run_product(ctx, prompt=PROMPT)
        resume_product(ctx, "answers")
        out = resume_product(ctx, "no, drop the export feature")
        assert out.paused and out.state.revision_notes == ["no, drop the export feature"]
        assert "(revised)" in out.interrupt["product_md"]  # the planner saw the rejection
        out = resume_product(ctx, "still no")
        assert out.paused and len(out.state.revision_notes) == 2
        out = resume_product(ctx, "no")
        assert not out.paused and out.state.exit_reason == "rejected"
        assert out.state.phase == "halted" and out.state.bootstrap_commit is None
    finally:
        ctx.close()
    assert not (tmp_path / "product" / ".git").exists()


def test_normaliser_enforces_the_spec_invariants() -> None:
    assert slugify("Todo CLI!") == "todo_cli" and slugify("123 go") == "app_123_go"
    out = ProductSpecOutput(
        product_name="My App",
        title="My App",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="X", description="Export JSON", acceptance=["prints JSON"])],
        quality_bar=["tests"],
        stack="cobol",
    )
    spec, notes = normalise_spec(out)
    assert spec.name == "my_app" and spec.stack == "python"
    assert [f.id for f in spec.features] == ["P1", "P2"] and "skeleton" in spec.features[
        0
    ].description
    assert any("cobol" in n for n in notes) and any("skeleton" in n for n in notes)
    md = render_product_md(spec)
    assert md.startswith("# My App") and "- [ ] prints JSON" in md


def test_cli_new_starts_with_a_chosen_thread_and_resumes_without_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-061: the first live run passed --thread on a fresh start and the CLI resumed a thread
    that did not exist. A prompt starts; no prompt resumes."""
    from scio.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "fake")
    monkeypatch.setenv("SCIO_FAKE_SCRIPT", "1")
    monkeypatch.setenv("SCIO_CHECKPOINT_DSN", f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}")
    target = str(tmp_path / "product")
    assert main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-cli"]) == 0
    out = capsys.readouterr().out
    assert "PAUSED for a human" in out and "questions" in out
    assert main(["new", "--thread", "new-cli", "--dir", target, "--decision", "answers"]) == 0
    assert "product_md" in capsys.readouterr().out
    assert main(["new", "--thread", "new-cli", "--dir", target, "--decision", "approve"]) == 0
    out = capsys.readouterr().out
    assert "phase=done" in out and "bootstrap commit" in out
    assert (tmp_path / "product" / ".git").is_dir()
    assert main(["new", "--dir", target]) == 2  # neither a prompt nor a thread


def test_the_draft_shown_is_the_draft_approved_and_no_model_call_repeats(tmp_path: Path) -> None:
    """I-065: on resume a node re-runs from its start, so a model call and an interrupt in the
    same node ran the planner twice and the human approved a draft they never saw. A responder
    whose drafts differ on every call makes the duplicate visible."""
    from scio.harness.types import CompletionRequest

    inner = ScriptedResponder()
    calls = {"interviewer": 0, "product_spec": 0}

    def responder(req: CompletionRequest) -> object:
        reply = inner(req)
        props = (req.output_schema or {}).get("properties", {})
        if "interview_questions" in props:
            calls["interviewer"] += 1
        if "product_name" in props:
            calls["product_spec"] += 1
            data = json.loads(reply.text)
            data["title"] = f"Todo CLI draft {calls['product_spec']}"
            return dataclasses.replace(reply, text=json.dumps(data))
        return reply

    settings = Settings(
        provider_override="fake",
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    registry.register("fake", FakeProvider(responder=responder))
    harness = Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=500_000, usd_max=5.0)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )
    ctx = RunContext.create(
        thread_id="new-4",
        sandbox_root=tmp_path / "product",
        tier="T1",
        tier_pin="T1",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
    )
    try:
        out = run_product(ctx, prompt=PROMPT)
        questions = out.interrupt["questions"]
        out = resume_product(ctx, "answers")
        assert calls["interviewer"] == 1 and out.state.questions == questions
        shown = out.interrupt["product_md"]
        assert shown.startswith("# Todo CLI draft 1")
        out = resume_product(ctx, "approve")
        assert calls["product_spec"] == 1 and out.state.phase == "done"
        assert out.state.product_md == shown
    finally:
        ctx.close()
    assert (tmp_path / "product" / "PRODUCT.md").read_text(encoding="utf-8") == shown
