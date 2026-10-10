from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scio.aci.capabilities import UnsafeToolsetError, check_toolset
from scio.aci.sandbox.base import ExecResult
from scio.aci.sandbox.local import LocalSandbox
from scio.aci.toolbox import ToolBox
from scio.aci.tools import (
    EditFileInput,
    GitCheckpointInput,
    RepoSearchInput,
    RunTestsInput,
    ToolError,
    ViewFileInput,
    edit_file,
    failures_only,
    git_checkpoint,
    git_push,
    repo_search,
    run_tests,
    view_file,
)
from scio.aci.truncate import truncate

PKG = '''"""tiny package"""


def paginate(items, size):
    pages = []
    for i in range(0, len(items) - 1, size):
        pages.append(items[i : i + size])
    return pages


def slugify(text):
    return text.lower()
'''

TESTS = """from pkg import paginate, slugify


def test_slug_passes():
    assert slugify("A") == "a"


def test_last_partial_page():
    assert len(paginate(list(range(7)), 3)) == 3
"""


@pytest.fixture
def repo(tmp_path: Path) -> LocalSandbox:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text(PKG, encoding="utf-8", newline="\n")
    (root / "tests" / "test_pkg.py").write_text(TESTS, encoding="utf-8", newline="\n")
    (root / "tests" / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "README.md").write_text("# demo\nslugify docs\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    return LocalSandbox(root)


def p(sb: LocalSandbox, rel: str) -> str:
    return str(Path(sb.root) / rel)


# ---------- truncate ----------


def test_truncate_keeps_head_and_tail_with_hint() -> None:
    text = "\n".join(f"line {i}" for i in range(2000))
    out = truncate(text, max_chars=500, hint="use filter=")
    assert (
        out.startswith("line 0")
        and out.endswith("line 1999")
        and "narrow your query: use filter=" in out
    )
    assert truncate("short", hint="x") == "short"


# ---------- repo_search ----------


def test_repo_search_concise_and_detailed(repo: LocalSandbox) -> None:
    out = repo_search(repo, RepoSearchInput(query="slugify", mode="concise"))
    assert "match(es)" in out and "pkg" in out and "lines" in out and "def slugify" not in out
    det = repo_search(repo, RepoSearchInput(query="def slugify", mode="detailed", glob="*.py"))
    assert "def slugify(text):" in det and "README" not in det
    assert "no matches" in repo_search(repo, RepoSearchInput(query="zzz_nothing"))
    with pytest.raises(ToolError, match="absolute"):
        repo_search(repo, RepoSearchInput(query="x", path="relative/dir"))


def test_repo_search_python_fallback_when_rg_missing(
    repo: LocalSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo, "exec", lambda *a, **k: ExecResult(127, "", "command not found: rg"))
    import scio.aci.tools as tools_mod

    monkeypatch.setattr(tools_mod.shutil, "which", lambda _n: None)
    out = repo_search(repo, RepoSearchInput(query=r"def \w+", mode="detailed", glob="*.py"))
    assert "def paginate" in out and "def slugify" in out
    with pytest.raises(ToolError, match="invalid regular expression"):
        repo_search(repo, RepoSearchInput(query="(unclosed"))


def test_repo_search_caps_and_hints(repo: LocalSandbox) -> None:
    big = "\n".join(f"needle_{i} = {i}" for i in range(3000))
    repo.write_text(p(repo, "pkg/big.py"), big + "\n")
    out = repo_search(repo, RepoSearchInput(query="needle_", mode="detailed", max_results=500))
    assert "(capped)" in out and "narrow your query" in out


# ---------- view_file ----------


def test_view_file_window_and_paging(repo: LocalSandbox) -> None:
    repo.write_text(p(repo, "long.txt"), "\n".join(f"L{i}" for i in range(1, 251)) + "\n")
    first = view_file(repo, ViewFileInput(path=p(repo, "long.txt")))
    assert (
        first.splitlines()[0].endswith("| L1")
        and "lines 1-100 of 250; next: start_line=101" in first
    )
    last = view_file(repo, ViewFileInput(path=p(repo, "long.txt"), start_line=201))
    assert "lines 201-250 of 250; end of file" in last
    with pytest.raises(ToolError, match="past the end"):
        view_file(repo, ViewFileInput(path=p(repo, "long.txt"), start_line=999))
    with pytest.raises(ToolError, match="not found"):
        view_file(repo, ViewFileInput(path=p(repo, "nope.txt")))


# ---------- edit_file ----------


def test_edit_file_replaces_exactly_once(repo: LocalSandbox) -> None:
    path = p(repo, "pkg/__init__.py")
    out = edit_file(
        repo, EditFileInput(path=path, old_text="len(items) - 1", new_text="len(items)")
    )
    assert out.startswith("edited") and "len(items), size" in repo.read_text(path)
    with pytest.raises(ToolError, match="not found"):
        edit_file(repo, EditFileInput(path=path, old_text="nope", new_text="x"))
    with pytest.raises(ToolError, match="ambiguous"):
        edit_file(repo, EditFileInput(path=path, old_text="    return", new_text="x"))


def test_edit_file_rejects_syntax_and_lint_errors_and_reverts(repo: LocalSandbox) -> None:
    path = p(repo, "pkg/__init__.py")
    before = repo.read_text(path)
    with pytest.raises(ToolError, match="syntax error"):
        edit_file(
            repo,
            EditFileInput(path=path, old_text="def slugify(text):", new_text="def slugify(text:"),
        )
    assert repo.read_text(path) == before
    with pytest.raises(ToolError, match="lint"):
        edit_file(
            repo,
            EditFileInput(
                path=path, old_text="return text.lower()", new_text="return undefined_name.lower()"
            ),
        )
    assert repo.read_text(path) == before


def test_edit_file_creates_new_file_and_refuses_overwrite(repo: LocalSandbox) -> None:
    new = p(repo, "pkg/new.py")
    assert edit_file(repo, EditFileInput(path=new, old_text="", new_text="X = 1\n")).startswith(
        "created"
    )
    with pytest.raises(ToolError, match="exists"):
        edit_file(repo, EditFileInput(path=new, old_text="", new_text="Y = 2\n"))


# ---------- run_tests ----------


def test_run_tests_returns_failures_only(repo: LocalSandbox) -> None:
    out = run_tests(repo, RunTestsInput())
    assert "test_last_partial_page" in out and "FAILED" in out
    assert "test_slug_passes" not in out  # passing tests are noise
    ok = run_tests(repo, RunTestsInput(filter="slug"))
    assert ok.startswith("all tests passed")
    none = run_tests(repo, RunTestsInput(filter="nothing_matches"))
    assert none.startswith("no tests collected")


def test_failures_only_parser() -> None:
    raw = (
        "tests/a.py::t PASSED\n=== FAILURES ===\n___ t2 ___\nE assert 1 == 2\n"
        "=== short test summary info ===\nFAILED tests/a.py::t2\n1 failed, 1 passed in 0.1s\n"
    )
    out = failures_only(raw)
    assert (
        out.startswith("=== FAILURES ===")
        and "PASSED" not in out
        and "FAILED tests/a.py::t2" in out
    )


# ---------- git ----------


def test_git_checkpoint_and_push_guard(repo: LocalSandbox) -> None:
    out = git_checkpoint(repo, GitCheckpointInput(message="feat: initial"))
    assert out.startswith("committed ")
    again = git_checkpoint(repo, GitCheckpointInput(message="feat: nothing"))
    assert again.startswith("nothing to commit")
    with pytest.raises(ToolError, match="default branch"):
        git_push(repo, "main", default_branch="main")


# ---------- toolbox + capabilities ----------


def test_capability_check_refuses_trifecta_without_gate() -> None:
    with pytest.raises(UnsafeToolsetError):
        check_toolset(["view_file", "run_tests", "git_push"], human_gate=False)
    caps = check_toolset(["view_file", "run_tests", "git_push"], human_gate=True)
    assert {"private_data", "untrusted_input", "exfiltration"} <= caps
    assert check_toolset(["view_file", "edit_file", "run_tests"], human_gate=False) == {
        "private_data",
        "mutation",
        "untrusted_input",
    }
    with pytest.raises(UnsafeToolsetError, match="no capability tags"):
        check_toolset(["mystery_tool"], human_gate=True)


def test_toolbox_specs_dispatch_errors_and_call_cap(repo: LocalSandbox, spans) -> None:  # type: ignore[no-untyped-def]
    tb = ToolBox(
        repo, ["repo_search", "view_file", "edit_file", "run_tests"], human_gate=False, max_calls=3
    )
    specs = tb.specs()
    assert [s.name for s in specs] == ["repo_search", "view_file", "edit_file", "run_tests"]
    assert (
        specs[0].input_schema["additionalProperties"] is False
        and "mode" in specs[0].input_schema["properties"]
    )
    r = tb.call("t1", "view_file", {"path": p(repo, "README.md")})
    assert not r.is_error and "slugify docs" in r.content
    bad = tb.call("t2", "view_file", {"path": "relative.md"})
    assert bad.is_error and "absolute" in bad.content
    invalid = tb.call("t3", "view_file", {"start_line": 0})
    assert invalid.is_error and "invalid input" in invalid.content
    capped = tb.call("t4", "view_file", {"path": p(repo, "README.md")})
    assert capped.is_error and "budget exhausted" in capped.content
    assert [s.name for s in spans.get_finished_spans()][:1] == ["execute_tool view_file"]
    with pytest.raises(KeyError):
        ToolBox(repo, ["nope"], human_gate=True)
