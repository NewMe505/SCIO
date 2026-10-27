"""I-098: every model call made inside a graph or product node sits in a ``try`` whose
handler catches ``BudgetExceeded``. An unguarded call (the documenter's PR body, live p003)
escapes as a traceback and ends a whole product build instead of the graceful ``budget``
exit every other node returns."""

from __future__ import annotations

import ast
from pathlib import Path

from scio.settings import REPO_ROOT

NODE_MODULES = [
    *sorted((REPO_ROOT / "scio" / "graph" / "nodes").glob("*.py")),
    *sorted((REPO_ROOT / "scio" / "product").glob("*.py")),
]


def _is_model_call(node: ast.Call) -> bool:
    f = node.func
    if not isinstance(f, ast.Attribute):
        return False
    on_harness = isinstance(f.value, ast.Attribute) and f.value.attr == "harness"
    on_loop = isinstance(f.value, ast.Name) and f.value.id == "loop"
    return (f.attr in ("structured", "complete") and on_harness) or (f.attr == "run" and on_loop)


def _catches_budget(node: ast.Try) -> bool:
    for handler in node.handlers:
        t = handler.type
        names = [t] if isinstance(t, ast.Name) else list(t.elts) if isinstance(t, ast.Tuple) else []
        if any(isinstance(n, ast.Name) and n.id == "BudgetExceeded" for n in names):
            return True
    return False


def unguarded_model_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    lines: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_model_call(node)):
            continue
        child: ast.AST = node
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.Try) and child in parent.body and _catches_budget(parent):
                break
            child, parent = parent, parents.get(parent)
        else:
            lines.append(node.lineno)
    return lines


def test_every_model_call_in_a_node_is_inside_a_budget_guard() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"
        for path in NODE_MODULES
        for line in unguarded_model_calls(path)
    ]
    assert offenders == [], offenders


def test_the_scan_sees_an_unguarded_call(tmp_path: Path) -> None:
    src = tmp_path / "node.py"
    src.write_text(
        "def node(ctx, loop):\n"
        "    try:\n"
        "        a = ctx.harness.structured(1, 2)\n"
        "    except BudgetExceeded:\n"
        "        pass\n"
        "    b = ctx.harness.structured(3, 4)\n"
        "    try:\n"
        "        loop.run('m')\n"
        "    except ValueError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert unguarded_model_calls(src) == [6, 8]
