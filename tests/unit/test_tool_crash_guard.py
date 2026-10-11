"""A tool crash is an error result, never an exception out of the toolbox (ledger I-032)."""

from __future__ import annotations

from pathlib import Path

from scio.aci.sandbox.local import LocalSandbox
from scio.aci.toolbox import TOOLS, ToolBox, ToolDef
from scio.aci.tools import ViewFileInput, view_file
from scio.harness.failures import FatalError


def test_view_file_on_a_directory_is_a_tool_error(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    (tmp_path / "pkg").mkdir()
    tb = ToolBox(sb, ["view_file"], human_gate=False, role="explorer")
    r = tb.call("t1", "view_file", {"path": str(tmp_path / "pkg")})
    assert r.is_error and "not a readable file" in r.content and "repo_search" in r.content
    r2 = tb.call("t2", "view_file", {"path": str(tmp_path)})
    assert r2.is_error


def test_unexpected_exception_becomes_error_result(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sb = LocalSandbox(tmp_path)

    def boom(_sb: object, _inp: object) -> str:
        raise FatalError("provider exploded inside a tool")

    monkeypatch.setitem(
        TOOLS,
        "view_file",
        ToolDef("view_file", "d", ViewFileInput, boom),  # type: ignore[arg-type]
    )
    tb = ToolBox(sb, ["view_file"], human_gate=False, role="coder")
    r = tb.call("t1", "view_file", {"path": str(tmp_path / "x.py")})
    assert r.is_error and r.content.startswith("view_file failed: FatalError")
    assert view_file is not None  # the real function is untouched outside the patch
