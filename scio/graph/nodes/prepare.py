"""``prepare_repo``: clone the target repository into the sandbox and create the branch.

Idempotent under ``run_effect``: the probe checks for an existing clone on the branch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from scio.graph.effects import run_effect
from scio.graph.nodes.common import fact, git_out, slug
from scio.graph.run_context import RunContext
from scio.state.models import Role, SCIOState
from scio.toolchain import detect_toolchain, get_toolchain

Node = Callable[[SCIOState], dict[str, Any]]


def branch_name(state: SCIOState) -> str:
    n = state.task.issue_number
    return f"agent/{n}-{slug(state.task.title)}" if n else f"agent/{slug(state.task.title)}"


def prepare_repo(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        sb = ctx.sandbox
        branch = branch_name(state)

        def probe() -> dict[str, str] | None:
            if not sb.exists(f"{sb.root}/.git"):
                return None
            current = git_out(sb, "rev-parse", "--abbrev-ref", "HEAD")
            if current != branch:
                return None
            base = git_out(sb, "merge-base", branch, state.task.base_branch)
            return {"branch": branch, "base_sha": base or git_out(sb, "rev-parse", "HEAD")}

        def apply() -> dict[str, str]:
            if not sb.exists(f"{sb.root}/.git"):
                r = sb.exec(["git", "clone", "-q", state.task.repo_url, "."], timeout_s=300)
                if not r.ok:
                    raise RuntimeError(f"git clone failed: {r.stderr.strip()[:300]}")
            # The clone says what stack it is (M10): adopt its profile unless one was forced.
            if ctx.settings.toolchain == "auto":
                detected = detect_toolchain(sb.root, lambda rel: sb.exists(f"{sb.root}/{rel}"))
                if detected is not None:
                    ctx.adopt_toolchain(get_toolchain(detected))
            # Repo-local excludes (not committed): never let test caches into a checkpoint.
            sb.write_text(f"{sb.root}/.git/info/exclude", ctx.toolchain.git_exclude_text())
            sb.exec(["git", "config", "user.name", "scio"], timeout_s=30)
            sb.exec(["git", "config", "user.email", "scio@users.noreply.github.com"], timeout_s=30)
            sb.exec(["git", "checkout", "-q", state.task.base_branch], timeout_s=60)
            base = git_out(sb, "rev-parse", "HEAD")
            r = sb.exec(["git", "checkout", "-q", "-B", branch], timeout_s=60)
            if not r.ok:
                raise RuntimeError(f"git checkout -B failed: {r.stderr.strip()[:300]}")
            return {"branch": branch, "base_sha": base}

        out = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="prepare_repo",
            intent=f"clone+branch:{branch}",
            apply=apply,
            probe=probe,
        )
        github = state.github.model_copy(
            update={"branch": out["branch"], "base_sha": out["base_sha"]}
        )
        entry = fact(
            f"repository ready on branch {branch} at {out['base_sha'][:12]}", source=Role.SYSTEM
        )
        return {"github": github, "task_ledger": [entry], "phase": "implement"}

    return node
