"""The product eval solver (M11): one trial is a whole product. The task's ``PRODUCT.md`` is
bootstrapped as written (``scio new --spec``), ``scio build`` runs every milestone to the
release gate, and hidden black-box tests then drive the released product from outside: a
subprocess for a CLI (``APP_CMD``, ``APP_DIR``), HTTP for a server (``APP_URL``).

Every human stop is answered the way the owner would answer a green one: ``merge`` at a
milestone, ``release`` at the release gate, ``proceed`` past a gate that wants a human on
green checks. A feature that halts is answered ``stop``: that trial fails, because the
product did not build without help. Every answer is counted in the trial's notes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from evals.solvers import EvalTaskLike, TrialOutcome, remove_tree
from scio.aci.sandbox.local import INTERPRETER_WORD
from scio.graph.run_context import RunContext
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.product import run_product
from scio.product.acceptance import app_argv, free_port, start_app, wait_healthy
from scio.product.build import build_thread_id, product_env, resume_build, run_build
from scio.product.progress import parse_product_md
from scio.state.models import ProductSpec

log = logging.getLogger(__name__)

ANSWERS = {
    "merge_wait": "merge",
    "release_decision": "release",
    "gate": "proceed",
}
"""Stops the owner answers on a green build; any other stop is answered ``stop``."""
MAX_STOPS = 40
"""A runaway guard on the answer loop (milestones x stops), not a quality bar."""


class ProductSolver:
    def __init__(
        self,
        *,
        responder_factory: Any | None = None,
        keep_dirs: bool = False,
    ) -> None:
        self.responder_factory = responder_factory
        self.keep_dirs = keep_dirs

    def solve(self, task: EvalTaskLike, harness: Harness) -> TrialOutcome:
        tmp = Path(tempfile.mkdtemp(prefix=f"scio-{task.id}-"))
        try:
            return self._solve(task, harness, tmp)
        finally:
            if self.keep_dirs:
                log.info("kept the %s trial at %s", task.id, tmp)
            else:
                try:
                    remove_tree(tmp)
                except OSError as exc:  # a leak must be visible, never fail the trial (I-049)
                    log.warning("could not remove trial directory %s: %s", tmp, exc)

    def _solve(self, task: EvalTaskLike, harness: Harness, tmp: Path) -> TrialOutcome:
        settings = harness.settings.model_copy(
            update={"checkpoint_dsn": f"sqlite:///{(tmp / 'ckpt.sqlite').as_posix()}"}
        )
        if settings.provider_override == "fake":
            from evals.fake_scripts import ScriptedResponder

            factory = self.responder_factory or (lambda: ScriptedResponder(**task.scenario))
            harness.providers.register("fake", FakeProvider(responder=factory()))
        spec = parse_product_md(task.spec)
        product = tmp / "product"
        product.mkdir()

        def context(thread: str) -> RunContext:
            return RunContext.create(
                thread_id=thread,
                sandbox_root=product,
                tier="T1",
                tier_pin="T1",
                settings=settings,
                harness=harness,
                work_dir=tmp / ".scio",
            )

        ctx = context(f"new-{task.id}")
        try:
            new = run_product(ctx, prompt=f"product eval {task.id}", spec_md=task.spec)
        finally:
            ctx.close()
        if new.state.exit_reason is not None or new.paused:
            return TrialOutcome(passed=False, notes=f"bootstrap exit={new.state.exit_reason}")

        ctx = context(build_thread_id(spec.name))
        answers: list[str] = []
        try:
            out = run_build(ctx)
            while out.paused and len(answers) < MAX_STOPS:
                answer = ANSWERS.get(out.state.phase, "stop")
                answers.append(f"{out.state.phase}:{answer}")
                out = resume_build(ctx, answer)
            state = out.state
            released = state.release is not None and state.release.commit is not None
            graded = grade(ctx, spec, task) if released else (False, "no release")
        finally:
            ctx.close()
        interventions = [a for a in answers if not a.startswith(("merge_wait", "release"))]
        passed = released and graded[0] and state.exit_reason is None
        runs = state.feature_runs
        notes = (
            f"exit={state.exit_reason} phase={state.phase} "
            f"milestones={','.join(state.milestones_done)} "
            f"features={sum(r.status == 'passed' for r in runs)}/{len(runs)} "
            f"release={state.release.tag if state.release else None} "
            f"stops={len(answers)} interventions={len(interventions)} hidden={graded[1]}"
        )
        escalations = sum(r.escalations for r in runs)
        resolved = sum(r.escalations_resolved for r in runs)
        return TrialOutcome(
            passed=passed,
            advisor_calls=escalations,
            advisor_resolved=resolved,
            notes=notes,
            usd_elsewhere=sum(r.usd for r in runs),  # each feature has a child harness (I-095)
            tokens_elsewhere=sum(r.tokens for r in runs),
        )


def grade(ctx: RunContext, spec: ProductSpec, task: EvalTaskLike) -> tuple[bool, str]:
    """Run the task's hidden tests against the released product, from outside it: SCIO's own
    interpreter, a scratch directory, the product only reachable through its command or URL."""
    hidden = task.path / (task.hidden_tests or "hidden")
    sb, tc = ctx.sandbox, ctx.toolchain
    product_env(ctx, spec)  # this process needs the product environment (the interpreter)
    command = app_argv(spec, tc)
    if command[0] == INTERPRETER_WORD and sb.python:
        command[0] = sb.python
    env = {**os.environ, "APP_CMD": json.dumps(command), "APP_DIR": str(sb.root)}
    app = None
    if spec.app.kind == "http":
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        app = start_app(sb, app_argv(spec, tc, port), url, tc)
        if not wait_healthy(app, spec.app.health_path):
            return False, f"the app did not answer {spec.app.health_path}: {app.stop()[-300:]}"
        env["APP_URL"] = url
    try:
        with tempfile.TemporaryDirectory(prefix=f"scio-{task.id}-hidden-") as scratch:
            tests = Path(scratch) / "hidden"
            shutil.copytree(hidden, tests)
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)],
                cwd=scratch,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
    finally:
        if app is not None:
            app.stop()
    lines = (r.stdout + r.stderr).strip().splitlines()
    summary = lines[-1][:200] if lines else f"exit {r.returncode}"
    return r.returncode == 0, summary
