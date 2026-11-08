"""`scripts/watch_ci.py` (issue #88): the run it waits for is the pushed head's own."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from scio.settings import REPO_ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("watch_ci", REPO_ROOT / "scripts" / "watch_ci.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_watcher_takes_the_pushed_heads_run_never_the_previous_green_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-104's mistake: right after a push the newest finished run on the branch is the
    previous head's, green, and `gh pr checks --watch` reported it (or "no checks" and
    exited). The watcher matches the run by the pushed SHA, and waits while there is none."""
    wc = _load()
    rows = [
        {"databaseId": 2, "headSha": "old", "status": "completed", "conclusion": "success"},
        {"databaseId": 1, "headSha": "older", "status": "completed", "conclusion": "failure"},
    ]
    monkeypatch.setattr(wc, "_out", lambda argv: json.dumps(rows))
    assert wc.run_for_head("feat/x", "new", "ci") is None  # not registered yet: keep waiting
    rows.insert(0, {"databaseId": 3, "headSha": "new", "status": "queued", "conclusion": ""})
    run = wc.run_for_head("feat/x", "new", "ci")
    assert run is not None and run["databaseId"] == 3


def test_a_short_sha_is_resolved_to_the_full_one_github_reports() -> None:
    """I-172: `--sha 6dba0d2` never matched GitHub's full `headSha`, so the watcher waited out
    its 300 s and gave up on a run that was in progress."""
    wc = _load()
    full = wc.full_sha(None)
    assert len(full) == 40 and wc.full_sha(full[:7]) == full


def test_the_watcher_lists_runs_of_one_workflow_on_one_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wc = _load()
    seen: list[list[str]] = []

    def out(argv: list[str]) -> str:
        seen.append(argv)
        return "[]"

    monkeypatch.setattr(wc, "_out", out)
    wc.run_for_head("feat/x", "abc", "ci")
    argv = seen[0]
    assert argv[:3] == ["gh", "run", "list"]
    assert argv[argv.index("--branch") + 1] == "feat/x"
    assert argv[argv.index("--workflow") + 1] == "ci"
    assert Path(REPO_ROOT / "scripts" / "watch_ci.py").is_file()
