"""The product graph behind ``scio new`` (ADR 0010).

    interview -> answers (human) -> specify -> approval (human) -> bootstrap
      -> [publish: human confirms the GitHub repository] -> done

Every stop for a human is a LangGraph ``interrupt`` in a node of its own, after the node
that made the model call: a node re-runs from its start on resume, so a call and an
interrupt in one node would run the call twice and approve a draft nobody saw (I-065).
The run resumes with ``scio new --thread ID --decision "<text>"``. Bootstrap steps are
idempotent effects with world probes, so a crash or a second resume never duplicates a
commit (ADR 0003).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from scio.aci.tools import SCIO_GIT_IDENTITY, find_commit
from scio.graph.checkpointer import DURABILITY, open_checkpointer
from scio.graph.effects import run_effect
from scio.graph.nodes.common import fact, git_out
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.product.progress import parse_product_md
from scio.product.scaffold import progress_json, scaffold_for
from scio.product.spec import normalise_spec, render_product_md, stack_menu
from scio.state.models import InterviewOutput, ProductSpecOutput, ProductState, Role
from scio.toolchain import get_toolchain

Node = Callable[[ProductState], dict[str, Any]]
MAX_REVISIONS = 2
BOOTSTRAP_MARKER = "[scio:bootstrap]"


@dataclass
class ProductOutcome:
    state: ProductState
    interrupt: dict[str, Any] | None

    @property
    def paused(self) -> bool:
        return self.interrupt is not None


# ---------- nodes ----------


def interview(ctx: RunContext) -> Node:
    """The interviewer's questions. No interrupt here: a node re-runs from its start when the
    graph resumes, so a model call and an interrupt in one node means the call runs twice and
    the human answers a draft they never saw (I-065). The stop is the next node."""

    def node(state: ProductState) -> dict[str, Any]:
        req = ctx.harness.build_request(
            "interviewer",
            system=ctx.prompts.get("interviewer").render(),
            messages=[Message.user(f"# Product prompt\n{state.prompt.strip()}")],
            tags={"thread": ctx.thread_id},
        )
        try:
            out: InterviewOutput = ctx.harness.structured(req, InterviewOutput).value
        except BudgetExceeded as exc:
            return {"exit_reason": "budget", "phase": "halted", **_fact(f"budget: {exc}")}
        questions = [q.strip() for q in out.interview_questions]
        return {
            "questions": questions,
            "phase": "answers",
            "ledger": [fact(f"interview: {len(questions)} questions", source=Role.INTERVIEWER)],
            **ctx.budget_update(),
        }

    return node


def after_interview(state: ProductState) -> Literal["answers", "halt"]:
    return "halt" if state.exit_reason is not None else "answers"


def answers(_ctx: RunContext) -> Node:
    """Human stop: the interview answers (interrupt only, so a resume repeats no model call)."""

    def node(state: ProductState) -> dict[str, Any]:
        reply = interrupt(
            {
                "question": "Answer the interview (one reply; number or separate your answers)",
                "questions": state.questions,
                "resume": 'scio new --thread <id> --decision "<answers>"',
            }
        )
        return {"answers": str(reply), "phase": "specify"}

    return node


def specify(ctx: RunContext) -> Node:
    def node(state: ProductState) -> dict[str, Any]:
        parts = [
            f"# Product prompt\n{state.prompt.strip()}",
            "",
            "# Interview",
            *[f"Q{i}: {q}" for i, q in enumerate(state.questions, 1)],
            f"Answers: {state.answers or '(none given)'}",
        ]
        if state.revision_notes:
            parts += [
                "",
                "# Previous draft was rejected; apply these notes verbatim",
                *[f"- {n}" for n in state.revision_notes],
                "",
                state.product_md or "",
            ]
        req = ctx.harness.build_request(
            "product_planner",
            system=ctx.prompts.get("product_planner").render(stacks=stack_menu()),
            messages=[Message.user("\n".join(parts))],
            tags={"thread": ctx.thread_id, "revision": str(len(state.revision_notes))},
        )
        try:
            out: ProductSpecOutput = ctx.harness.structured(req, ProductSpecOutput).value
        except BudgetExceeded as exc:
            return {"exit_reason": "budget", "phase": "halted", **_fact(f"budget: {exc}")}
        spec, notes = normalise_spec(out)
        product_md = render_product_md(spec)
        entries = [
            fact(
                f"spec: {len(spec.features)} features, stack {spec.stack}",
                source=Role.PRODUCT_PLANNER,
            )
        ]
        entries += [fact(n, source=Role.SYSTEM) for n in notes]
        return {
            "spec": spec,
            "product_md": product_md,
            "phase": "approval",
            "ledger": entries,
            **ctx.budget_update(),
        }

    return node


def after_specify(state: ProductState) -> Literal["approval", "halt"]:
    return "halt" if state.exit_reason is not None else "approval"


def approval(ctx: RunContext) -> Node:
    """Human stop on the draft in state: the text shown is the text approved (I-065)."""

    def node(state: ProductState) -> dict[str, Any]:
        decision = str(
            interrupt(
                {
                    "question": "Approve PRODUCT.md? Reply 'approve', or give revision notes.",
                    "product_md": state.product_md,
                    "target_dir": state.target_dir,
                    "cost_usd": round(state.budgets.usd_used, 4),
                }
            )
        )
        if decision.strip().lower().startswith("approve"):
            return {
                "approved": True,
                "phase": "bootstrap",
                "ledger": [fact("PRODUCT.md approved", source=Role.HUMAN)],
            }
        if len(state.revision_notes) >= MAX_REVISIONS:
            return {
                "exit_reason": "rejected",
                "phase": "halted",
                "ledger": [
                    fact(
                        f"PRODUCT.md rejected {MAX_REVISIONS + 1} times; halting", source=Role.HUMAN
                    )
                ],
            }
        return {
            "revision_notes": [*state.revision_notes, decision.strip()],
            "phase": "specify",
            "ledger": [fact(f"revision requested: {decision[:200]}", source=Role.HUMAN)],
        }

    return node


def after_approval(state: ProductState) -> Literal["bootstrap", "specify", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    return "bootstrap" if state.approved else "specify"


def bootstrap(ctx: RunContext) -> Node:
    def node(state: ProductState) -> dict[str, Any]:
        sb = ctx.sandbox
        spec = state.spec
        assert spec is not None
        ctx.adopt_toolchain(get_toolchain(spec.stack))  # the spec's stack, not the default (M10)
        tc = ctx.toolchain
        entries: list[Any] = []

        def _effect(intent: str, apply: Callable[[], Any], probe: Callable[[], Any]) -> Any:
            return run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="bootstrap",
                intent=intent,
                apply=apply,
                probe=probe,
            )

        # 1. git init (probe: .git exists)
        def init_apply() -> str:
            if not sb.exists(f"{sb.root}/.git"):
                r = sb.exec(["git", "init", "-q", "-b", "main"], timeout_s=60)
                if not r.ok:
                    raise RuntimeError(f"git init failed: {r.stderr.strip()[:300]}")
            sb.exec(["git", "config", "user.name", "scio"], timeout_s=30)
            sb.exec(["git", "config", "user.email", "scio@users.noreply.github.com"], timeout_s=30)
            return "initialised"

        _effect("git-init", init_apply, lambda: "exists" if sb.exists(f"{sb.root}/.git") else None)
        entries.append(fact("git repository initialised on main", source=Role.SYSTEM))

        # 2. scaffold + product memory (probe: PRODUCT.md matches)
        files = scaffold_for(spec)
        files["PRODUCT.md"] = state.product_md or render_product_md(spec)
        files[".scio/product/progress.json"] = progress_json(spec, bootstrap_commit=None)

        def scaffold_apply() -> int:
            for rel, text in files.items():
                sb.write_text(f"{sb.root}/{rel}", text)
            return len(files)

        def scaffold_probe() -> int | None:
            path = f"{sb.root}/PRODUCT.md"
            return (
                len(files)
                if sb.exists(path) and sb.read_text(path) == files["PRODUCT.md"]
                else None
            )

        n = _effect("scaffold", scaffold_apply, scaffold_probe)
        entries.append(
            fact(f"scaffold written: {n} files for stack {spec.stack}", source=Role.SYSTEM)
        )

        # 3. local CI under the profile: lint then tests (ground truth, no model verdict)
        lint = sb.exec(tc.lint_argv("."), timeout_s=120)
        findings = tc.lint_findings(lint.exit_code, lint.stdout, lint.stderr)
        tests = sb.exec(tc.test_argv(filter=None, path=None), timeout_s=sb.limits.timeout_s)
        outcome = tc.test_outcome(tests.exit_code, tests.stdout)
        if findings or outcome != "passed":
            detail = (findings or "") + "\n" + tests.output[-1500:]
            entries.append(fact(f"bootstrap checks failed: {detail[:600]}", source=Role.TESTER))
            return {
                "exit_reason": "bootstrap_failed",
                "phase": "halted",
                "ledger": entries,
                **ctx.budget_update(),
            }
        entries.append(
            fact("bootstrap checks green: lint clean, smoke test passed", source=Role.TESTER)
        )

        # 4. first commit (probe: a commit with the marker exists)
        def commit_apply() -> str:
            sb.exec(["git", "add", "-A"], timeout_s=60)
            msg = f"chore: bootstrap {spec.name} walking skeleton {BOOTSTRAP_MARKER}"
            r = sb.exec(["git", *SCIO_GIT_IDENTITY, "commit", "-q", "-m", msg], timeout_s=60)
            if not r.ok and "nothing to commit" not in (r.stdout + r.stderr):
                raise RuntimeError(f"git commit failed: {r.stderr.strip()[:300]}")
            return git_out(sb, "rev-parse", "HEAD")

        def commit_probe() -> str | None:
            # fixed-string: as a regex the bracketed marker is a character class (I-090)
            return find_commit(sb, BOOTSTRAP_MARKER)

        sha = _effect("bootstrap-commit", commit_apply, commit_probe)
        # progress.json records the commit; it is a second commit so the first stays the marker
        sb.write_text(
            f"{sb.root}/.scio/product/progress.json", progress_json(spec, bootstrap_commit=sha)
        )
        sb.exec(["git", "add", "-A"], timeout_s=60)
        sb.exec(
            [
                "git",
                *SCIO_GIT_IDENTITY,
                "commit",
                "-q",
                "-m",
                "chore: record bootstrap commit in progress.json",
            ],
            timeout_s=60,
        )
        entries.append(fact(f"bootstrap commit {sha[:12]}", source=Role.SYSTEM))
        return {
            "bootstrap_commit": sha,
            "phase": "publish" if state.gh_repo else "done",
            "ledger": entries,
            **ctx.budget_update(),
        }

    return node


def after_bootstrap(state: ProductState) -> Literal["publish", "done", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    return "publish" if state.phase == "publish" else "done"


def publish(ctx: RunContext) -> Node:
    """Create the GitHub repository and push: publishing, so a human confirms first."""

    def node(state: ProductState) -> dict[str, Any]:
        decision = str(
            interrupt(
                {
                    "question": f"Create GitHub repository {state.gh_repo} and push main? "
                    "Reply 'confirm' to publish, anything else to skip.",
                    "gh_repo": state.gh_repo,
                    "bootstrap_commit": state.bootstrap_commit,
                }
            )
        )
        if not decision.strip().lower().startswith("confirm"):
            return {
                "phase": "done",
                "ledger": [fact("publish skipped by human", source=Role.HUMAN)],
            }
        sb = ctx.sandbox

        def apply() -> str:
            r = sb.exec(
                [
                    "gh",
                    "repo",
                    "create",
                    str(state.gh_repo),
                    "--private",
                    "--source",
                    ".",
                    "--push",
                ],
                timeout_s=300,
            )
            if not r.ok:
                raise RuntimeError(f"gh repo create failed: {r.stderr.strip()[:300]}")
            return f"https://github.com/{state.gh_repo}"

        def probe() -> str | None:
            remote = git_out(sb, "remote", "get-url", "origin")
            return remote if remote and not remote.startswith("(git") else None

        url = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="publish",
            intent="gh-repo-create",
            apply=apply,
            probe=probe,
        )
        return {
            "repo_url": url,
            "phase": "done",
            "ledger": [fact(f"published to {url}", source=Role.SYSTEM)],
        }

    return node


def done(_ctx: RunContext) -> Node:
    def node(state: ProductState) -> dict[str, Any]:
        return {"phase": "done" if state.exit_reason is None else "halted"}

    return node


def _fact(text: str) -> dict[str, Any]:
    return {"ledger": [fact(text, source=Role.SYSTEM)]}


# ---------- graph ----------


def _unbound(name: str) -> Any:
    def node(_state: Any) -> dict[str, Any]:
        raise RuntimeError(f"node {name!r} compiled without a RunContext")

    return node


def start_route(state: ProductState) -> Literal["interview", "bootstrap"]:
    """A ``PRODUCT.md`` handed in (``scio new --spec``, the product evals, M11) is already the
    approved contract: no interview, no planner, its text is written as it is."""
    return "bootstrap" if state.approved and state.spec is not None else "interview"


def build_product_graph(ctx: RunContext | None) -> Any:
    """``ctx=None`` compiles a structural graph for the docs diagram (ADR 0011)."""
    g: Any = StateGraph(ProductState)  # Any: node callables are closures, as in build.py

    def n(name: str, factory: Callable[[RunContext], Node]) -> None:
        g.add_node(name, factory(ctx) if ctx is not None else _unbound(name))

    n("interview", interview)
    n("answers", answers)
    n("specify", specify)
    n("approval", approval)
    n("bootstrap", bootstrap)
    n("publish", publish)
    n("done", done)
    g.add_conditional_edges(
        START, start_route, {"interview": "interview", "bootstrap": "bootstrap"}
    )
    g.add_conditional_edges("interview", after_interview, {"answers": "answers", "halt": "done"})
    g.add_edge("answers", "specify")
    g.add_conditional_edges("specify", after_specify, {"approval": "approval", "halt": "done"})
    g.add_conditional_edges(
        "approval",
        after_approval,
        {"bootstrap": "bootstrap", "specify": "specify", "halt": "done"},
    )
    g.add_conditional_edges(
        "bootstrap", after_bootstrap, {"publish": "publish", "done": "done", "halt": "done"}
    )
    g.add_edge("publish", "done")
    g.add_edge("done", END)
    return g


def initial_product_state(
    ctx: RunContext, *, prompt: str, gh_repo: str | None, spec_md: str | None = None
) -> ProductState:
    """``spec_md``: a ``PRODUCT.md`` to bootstrap from as written (it must parse)."""
    spec = parse_product_md(spec_md) if spec_md is not None else None
    return ProductState(
        thread_id=ctx.thread_id,
        prompt=prompt,
        target_dir=str(ctx.sandbox.root),
        gh_repo=gh_repo,
        spec=spec,
        product_md=spec_md,
        approved=spec is not None,
        phase="bootstrap" if spec is not None else "interview",
        budgets=ctx.budget,
    )


def _outcome(raw: dict[str, Any]) -> ProductOutcome:
    interrupts = raw.pop("__interrupt__", None)
    payload = None
    if interrupts:
        first = interrupts[0]
        payload = first.value if hasattr(first, "value") else dict(first)
    return ProductOutcome(state=ProductState.model_validate(raw), interrupt=payload)


def run_product(
    ctx: RunContext, *, prompt: str, gh_repo: str | None = None, spec_md: str | None = None
) -> ProductOutcome:
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_graph(ctx).compile(checkpointer=saver)
        raw = graph.invoke(
            initial_product_state(ctx, prompt=prompt, gh_repo=gh_repo, spec_md=spec_md),
            config=config,
            durability=DURABILITY,
        )
    return _outcome(raw)


def product_target_dir(checkpoint_dsn: str, thread_id: str) -> str | None:
    """The target directory a product thread was started with, from its checkpoint; None
    when the thread has none. ``scio new --thread X`` without ``--dir`` resumes there instead
    of in a default directory (I-076: the first live http bootstrap landed under
    ``.scio/products/<thread>`` because the resume calls omitted ``--dir``)."""
    config = {"configurable": {"thread_id": thread_id}}
    with open_checkpointer(checkpoint_dsn) as saver:
        graph: Any = build_product_graph(None).compile(checkpointer=saver)
        values = graph.get_state(config).values
    target = values.get("target_dir") if values else None
    return str(target) if target else None


def resume_product(ctx: RunContext, decision: str | None) -> ProductOutcome:
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_graph(ctx).compile(checkpointer=saver)
        inp: Any = Command(resume=decision) if decision is not None else None
        raw = graph.invoke(inp, config=config, durability=DURABILITY)
    return _outcome(raw)
