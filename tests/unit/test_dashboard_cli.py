"""``scio dashboard`` (issue #68, ADR 0017): loopback only, every root listed, nothing created
in a root, and a clear message when the extra is missing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from scio.cli import DASHBOARD_HOST, DASHBOARD_SHUTDOWN_S, main


def test_the_dashboard_binds_loopback_only_and_lists_its_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    (tmp_path / "a" / ".scio").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    argv = ["dashboard", "--root", str(tmp_path / "a"), "--root", str(tmp_path / "b")]
    assert main([*argv, "--port", "9911"]) == 0
    assert [(c["host"], c["port"]) for c in calls] == [("127.0.0.1", 9911)]
    assert DASHBOARD_HOST == "127.0.0.1"
    # a bounded stop: an unbounded one can hang on Windows, through a second Ctrl+C (I-132)
    assert calls[0]["timeout_graceful_shutdown"] == DASHBOARD_SHUTDOWN_S > 0
    out = capsys.readouterr().out
    assert "root a:" in out and "root b:" in out and "http://127.0.0.1:9911" in out
    assert "no checkpoint DB yet" in out
    # read-only: a root without a checkpoint DB is reported, never given one
    assert not (tmp_path / "a" / ".scio" / "checkpoints.sqlite").exists()
    assert not (tmp_path / "b" / ".scio").exists()


def test_the_dashboard_has_no_host_option() -> None:
    with pytest.raises(SystemExit):
        main(["dashboard", "--host", "0.0.0.0"])


def test_the_dashboard_without_the_extra_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # import uvicorn now raises ImportError
    assert main(["dashboard"]) == 2
    err = capsys.readouterr().err
    # never a bare `uv sync --extra dashboard`: it would uninstall the other extras (I-124)
    assert "uv sync --all-extras" in err and "--extra dashboard" in err
