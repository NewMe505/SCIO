"""The supervisor must outlive a broken SCIO (ADR 0019 decision 1). It imports nothing
outside the standard library and itself, and it starts without site-packages."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "recovery"


def foreign_imports(source: str) -> list[str]:
    """Top-level module names imported by ``source`` that are neither standard library nor
    ``recovery``."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root != "recovery" and root not in sys.stdlib_module_names:
                out.append(name)
    return out


def test_the_scan_flags_scio_and_third_party_imports() -> None:
    src = "import json\nimport pydantic\nfrom scio.cli import main\nfrom recovery import store\n"
    assert foreign_imports(src) == ["pydantic", "scio.cli"]


def test_the_supervisor_imports_only_the_standard_library_and_itself() -> None:
    found = {
        str(path.relative_to(ROOT)): bad
        for path in sorted(PACKAGE.rglob("*.py"))
        if (bad := foreign_imports(path.read_text(encoding="utf-8")))
    }
    assert found == {}, f"recovery/ must use the standard library only: {found}"


def test_the_supervisor_starts_without_site_packages() -> None:
    proc = subprocess.run(
        [sys.executable, "-S", "-m", "recovery", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout
