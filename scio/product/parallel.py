"""Building a milestone's independent features at once (ADR 0018, issue #82).

One writer per working tree, one integrator per milestone branch. Each feature of a batch runs
the task graph in its own git worktree, outside the product tree under the run's work
directory (so the product's ``git add -A``, test discovery and lint never see it, I-155), on
its own ``agent/...`` branch from the milestone head's SHA: two worktrees cannot check out
one branch. The orchestrator alone merges a finished feature onto the milestone branch, in the
product tree, and runs the suite again when the merge was not a fast-forward. A conflict or a
red suite is undone, and the feature goes back to the serial loop for its next attempt.

Which features may share a batch is the milestone plan's call (``depends_on``,
:meth:`ProductBuildState.ready_batch`); the merge is the check, since files planned before a
feature runs are a guess (S8).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scio.aci.runtime import ToolContext
from scio.aci.sandbox.local import LocalSandbox
from scio.aci.tools import SCIO_GIT_IDENTITY
from scio.graph.effects import run_effect
from scio.graph.nodes.common import git_out
from scio.graph.nodes.prepare import exclude_path
from scio.graph.run_context import RunContext
from scio.state.models import MilestonePlan

UNION_FILES = ("CHANGELOG.md", "NOTES.md")
"""Files every feature's documenter adds lines to: git's union driver keeps both sides."""


def parallel_refused(ctx: RunContext) -> str | None:
    """Why this build runs its features one at a time, or None when a batch may run."""
    if ctx.settings.parallel_features < 2:
        return "SCIO_PARALLEL_FEATURES is 1"
    if not isinstance(ctx.sandbox, LocalSandbox):
        return "the sandbox is not local (one container per product)"
    if ctx.sandbox.python is None:
        # a stack whose environment lives inside the tree (node_modules) has none in a
        # fresh worktree; an interpreter-based one is usable from any tree
        return "the product environment is not an interpreter a worktree can share"
    return None


def worktrees_root(ctx: RunContext) -> Path:
    """``<work dir>/worktrees``: beside the run's artifacts, outside the product tree."""
    return ctx.artifacts.root.parent.parent / "worktrees"


def _git_path(ctx: RunContext, name: str) -> str:
    r = ctx.sandbox.exec(["git", "rev-parse", "--git-path", name], timeout_s=30)
    rel = r.stdout.strip().replace("\\", "/") if r.ok else f".git/{name}"
    is_abs = rel.startswith("/") or (len(rel) > 1 and rel[1] == ":")
    return rel if is_abs else f"{ctx.sandbox.root}/{rel}"


def prepare_shared_git(ctx: RunContext) -> None:
    """Write the product repository's local excludes and merge attributes. They live in the
    common git directory, which a worktree's sandbox cannot reach (I-161), and every
    feature's documenter adds to the files in :data:`UNION_FILES`."""
    sb = ctx.sandbox
    sb.write_text(exclude_path(sb), ctx.toolchain.git_exclude_text())
    union = "".join(f"{f} merge=union\n" for f in UNION_FILES)
    sb.write_text(_git_path(ctx, "info/attributes"), union)


def ensure_worktree(ctx: RunContext, thread: str, base_sha: str) -> Path:
    """The worktree of one feature attempt, detached at ``base_sha`` (probe: it exists)."""
    path = worktrees_root(ctx) / thread

    def apply() -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        r = ctx.sandbox.exec(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "worktree",
                "add",
                "-q",
                "--detach",
                str(path),
                base_sha,
            ],
            timeout_s=120,
        )
        if not r.ok:
            raise RuntimeError(f"git worktree add failed: {r.output.strip()[:300]}")
        return str(path)

    return Path(
        run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="feature",
            intent=f"worktree:{thread}",
            apply=apply,
            probe=lambda: str(path) if (path / ".git").exists() else None,
        )
    )


def in_worktree(fctx: RunContext, path: Path, python: str | None) -> RunContext:
    """A feature's context moved into its worktree: its own sandbox there (the product's
    interpreter, shared), its own tool context; everything else as ``feature_context`` built
    it (thread, budget, ledger, artifacts, no GitHub client)."""
    shared = fctx.tool_context
    own = ToolContext(
        toolchain=shared.toolchain if shared else fctx.toolchain,
        approved=shared.approved if shared else None,
    )  # `installed` is appended to per run, so never shared across threads
    return dataclasses.replace(
        fctx, sandbox=LocalSandbox(path, fctx.sandbox.limits, python=python), tool_context=own
    )


def remove_worktree(ctx: RunContext, path: Path) -> str | None:
    """Drop a finished worktree; its branch stays (the merge, or the record of an attempt).
    Git is told to handle long paths (a worktree sits deeper than the product tree, and its
    bytecode goes past Windows' 260 characters); when it still fails, the directory is
    deleted directly and git forgets the worktree. Returns why the directory is left, or None."""
    r = ctx.sandbox.exec(
        ["git", "-c", "core.longpaths=true", "worktree", "remove", "--force", str(path)],
        timeout_s=120,
    )
    if r.ok and not path.exists():
        return None
    try:
        _remove_tree(path)
    except OSError as exc:
        return f"worktree {path} is left on disk: {(r.output.strip() or str(exc))[:300]}"
    finally:
        ctx.sandbox.exec(["git", "worktree", "prune"], timeout_s=60)
    return None


def _remove_tree(path: Path) -> None:
    """Delete a directory past Windows' 260-character limit (an extended-length path) and
    with git's read-only objects in it (I-049)."""
    if not path.exists():
        return
    target = str(path.resolve())
    if os.name == "nt" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + target

    def writable(func: Callable[[str], object], p: str, _exc: BaseException) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(target, onexc=writable)


def merge_feature(
    ctx: RunContext, plan: MilestonePlan, feature_id: str, attempt: int, branch: str, head: str
) -> dict[str, Any]:
    """Merge one finished feature onto the milestone branch in the product tree, once (probe:
    its head is in the milestone branch). A fast-forward is the tree the feature's own verify
    passed; any other merge runs the suite on the merged tree. A conflict or a red suite is
    undone. Returns ``{"ok", "sha", "how"}``; ``how`` says what happened, for the ledger."""
    sb, tc = ctx.sandbox, ctx.toolchain
    marker = f"[scio:feature-merge:{plan.id}:{feature_id}:a{attempt}]"

    def apply() -> dict[str, Any]:
        _run(ctx, "checkout", "-q", plan.branch)
        before = git_out(sb, "rev-parse", "HEAD")
        message = f"Merge {branch} ({feature_id}) {marker}"
        r = sb.exec(
            ["git", *SCIO_GIT_IDENTITY, "merge", "--no-edit", "-m", message, branch],  # I-104
            timeout_s=120,
        )
        if not r.ok:
            conflicts = git_out(sb, "diff", "--name-only", "--diff-filter=U")
            sb.exec(["git", "merge", "--abort"], timeout_s=60)
            _run(ctx, "reset", "-q", "--hard", before)
            files = ", ".join(conflicts.splitlines()) or r.output.strip()[:200]
            return {"ok": False, "sha": before, "how": f"conflict in {files}"}
        after = git_out(sb, "rev-parse", "HEAD")
        if after == head:  # a fast-forward: the very tree the feature's verify passed
            return {"ok": True, "sha": after, "how": f"fast-forwarded to {after[:12]}"}
        tests = sb.exec(tc.test_argv(filter=None, path=None), timeout_s=sb.limits.timeout_s)
        outcome = tc.test_outcome(tests.exit_code, tests.stdout)
        if outcome != "passed":
            _run(ctx, "reset", "-q", "--hard", before)
            return {
                "ok": False,
                "sha": before,
                "how": f"merged cleanly, but the suite on the merged tree is {outcome}",
            }
        return {"ok": True, "sha": after, "how": f"merged at {after[:12]}; suite passed"}

    def probe() -> dict[str, Any] | None:
        r = sb.exec(["git", "merge-base", "--is-ancestor", head, plan.branch], timeout_s=30)
        if not r.ok:
            return None
        sha = git_out(sb, "rev-parse", plan.branch)
        return {"ok": True, "sha": sha, "how": f"already in {plan.branch} at {sha[:12]}"}

    result: dict[str, Any] = run_effect(
        ctx.effects,
        thread_id=ctx.thread_id,
        node="feature",
        intent=f"feature-merge:{plan.id}:{feature_id}:a{attempt}",
        apply=apply,
        probe=probe,
    )
    return result


def _run(ctx: RunContext, *args: str) -> None:
    r = ctx.sandbox.exec(["git", *SCIO_GIT_IDENTITY, *args], timeout_s=120)  # I-104
    if not r.ok:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.output.strip()[:300]}")
