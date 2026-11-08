"""`scripts/gate.py` (issue #88), the local CI-equivalent: it fails when a step fails, and its
regression evals are CI's (pre-flight 10: a chain that gates a push must be able to fail)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scio.settings import REPO_ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gate", REPO_ROOT / "scripts" / "gate.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gate"] = mod  # @dataclass looks its module up there (I-174)
    spec.loader.exec_module(mod)
    return mod


def _touch(path: Path) -> list[str]:
    """A step that passes and leaves ``path`` behind, so a test can see it ran."""
    return [sys.executable, "-c", f"open({str(path)!r}, 'w').close()"]


FAIL = [sys.executable, "-c", "raise SystemExit(3)"]


def test_a_failing_first_step_fails_the_gate_before_the_tests_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load()
    ran = tmp_path / "tests-ran"
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [("ruff check", FAIL)])
    monkeypatch.setattr(gate, "TOGETHER", [("pytest", _touch(ran))])
    monkeypatch.setattr(gate, "LAST", [])
    assert gate.main() == 1
    assert not ran.exists()
    out = capsys.readouterr().out
    assert "FAIL ruff check" in out and "exit 3" in out and "FAILED" not in out  # stopped early


def test_a_failing_test_step_fails_the_gate_and_the_docs_check_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load()
    evals_ran, docs_ran = tmp_path / "evals-ran", tmp_path / "docs-ran"
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [])
    monkeypatch.setattr(gate, "TOGETHER", [("pytest", FAIL), ("evals", _touch(evals_ran))])
    monkeypatch.setattr(gate, "LAST", [("docs", _touch(docs_ran))])
    assert gate.main() == 1
    assert evals_ran.exists() and docs_ran.exists()
    assert "gate: FAILED" in capsys.readouterr().out


def test_a_gate_whose_steps_all_pass_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load()
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [("ruff check", _touch(tmp_path / "a"))])
    monkeypatch.setattr(gate, "TOGETHER", [("pytest", _touch(tmp_path / "b"))])
    monkeypatch.setattr(gate, "LAST", [("docs", _touch(tmp_path / "c"))])
    assert gate.main() == 0
    assert "gate: passed" in capsys.readouterr().out


def test_the_gates_regression_evals_are_cis() -> None:
    """The suite, provider, trials, threshold and trial concurrency are written in two places,
    `scripts/gate.py` and CI's `evals` job; CI adds only `--out` for its job summary."""
    evals = dict(_load().steps())["evals"]
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    line = next(ln for ln in ci.splitlines() if "evals.runner" in ln)
    words = line.split("run:", 1)[1].split()
    if "--out" in words:
        i = words.index("--out")
        del words[i : i + 2]
    assert words[words.index("--suite") :] == evals[evals.index("--suite") :], line
