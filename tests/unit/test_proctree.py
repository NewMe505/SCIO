"""I-180, issue #105: a child process with a timeout runs through ``scio.proctree.run_tree``,
whose timeout kills the whole tree and bounds the drain after it. ``subprocess.run``'s own
timeout kills only the first process, and on Windows then waits on the pipes with no limit,
so a child holding them (a worker's Bash command, a test's server) made the timeout a hang.
The behaviour is tested where it is used (the ``claude`` runner and the local sandbox); this
scan keeps a new call site from bringing the old construct back."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPAWNERS = {"run", "call", "check_call", "check_output"}
ALLOWED = {
    "scio/proctree.py": "the tree kill's own taskkill, which starts no children",
    "scio/aci/sandbox/docker.py": (
        "the docker CLI and host git probes: no child of theirs holds the pipes, and the "
        "container's own `timeout` ends the work inside it"
    ),
}


def timed_subprocess_calls(path: Path) -> list[int]:
    """The lines of ``subprocess.<spawner>(..., timeout=...)`` calls in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SPAWNERS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and any(k.arg == "timeout" for k in node.keywords)
    ]


def test_no_subprocess_timeout_outside_run_tree() -> None:
    found = {
        f"{path.relative_to(ROOT).as_posix()}:{line}"
        for path in sorted((ROOT / "scio").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() not in ALLOWED
        for line in timed_subprocess_calls(path)
    }
    assert not found, f"use scio.proctree.run_tree, whose timeout kills the tree: {found}"


def test_the_scan_sees_the_construct_it_forbids(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\n"
        "subprocess.run(['claude', '-p'], capture_output=True, timeout=600)\n"
        "subprocess.run(['gh', 'repo', 'view'], capture_output=True)\n",
        encoding="utf-8",
        newline="\n",
    )
    assert timed_subprocess_calls(sample) == [2]
    for rel in ALLOWED:
        assert (ROOT / rel).is_file(), rel
