"""Every text file SCIO writes from Python has LF endings (I-006, I-126). On Windows,
``Path.write_text`` and ``open(..., "w")`` translate ``\\n`` to ``\\r\\n`` unless they are given
``newline=``. ``Path.write_text`` is told apart from the sandboxes' own ``write_text(path,
content)``, which always writes LF, by its ``encoding=`` keyword: the sandbox method has none."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE_DIRS = ("scio", "evals", "scripts")


def _keywords(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg}


def _writes_text(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "write_text":
        return "encoding" in _keywords(call)
    if isinstance(func, ast.Name) and func.id == "open" and len(call.args) > 1:
        mode = call.args[1]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return "b" not in mode.value and any(m in mode.value for m in "wax")
    return False


def crlf_write_lines(source: str) -> list[int]:
    """The lines of ``source`` with a text write that does not pass ``newline=``."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _writes_text(node) and "newline" not in _keywords(node)
    ]


def _code_files() -> list[Path]:
    files = [p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")]
    return [
        p
        for p in files
        if "__pycache__" not in p.parts
        # the fixture repositories are the agent's code, not ours
        and not p.relative_to(ROOT).as_posix().startswith("evals/tasks/")
    ]


def test_every_text_write_in_the_code_directories_passes_newline() -> None:
    found = [
        f"{p.relative_to(ROOT).as_posix()}:{line}"
        for p in _code_files()
        for line in crlf_write_lines(p.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_the_scan_finds_a_text_write_without_newline() -> None:
    source = "\n".join(
        [
            "from pathlib import Path",
            "Path('a').write_text('x', encoding='utf-8')",
            "Path('b').write_text('x', encoding='utf-8', newline='\\n')",
            "open('c', 'w', encoding='utf-8')",
            "open('d', 'rb')",
            "sandbox.write_text('e', 'x')",
            "open('f', 'a', newline='')",
        ]
    )
    assert crlf_write_lines(source) == [2, 4]
