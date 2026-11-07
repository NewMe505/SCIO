"""Git facts the parallel features rest on (ADR 0018, issue #82), on real repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scio.aci.sandbox.local import LocalSandbox
from scio.graph.nodes.prepare import exclude_path, under_root


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_worktrees_excludes_are_the_common_directorys_file(tmp_path: Path) -> None:
    """I-161: `prepare_repo` wrote `<root>/.git/info/exclude` by hand. In a worktree `.git`
    is a file, so the write failed (`FileExistsError`), and the excludes git reads live in the
    common directory, outside the worktree's sandbox. Git names the file now."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "a")
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(tree))
    assert (tree / ".git").is_file()

    plain = exclude_path(LocalSandbox(repo))
    assert plain.replace("\\", "/").endswith("repo/.git/info/exclude")
    # I-169: a plain clone's file is under its root, so prepare_repo writes it (the first
    # version compared a backslash path with a forward-slash root, wrote nothing, and caches
    # were committed again, I-028)
    assert under_root(LocalSandbox(repo), plain)
    shared = exclude_path(LocalSandbox(tree))
    assert shared.replace("\\", "/").endswith("repo/.git/info/exclude")  # not under wt/
    assert not under_root(LocalSandbox(tree), shared)  # the orchestrator writes it
