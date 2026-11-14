"""Isolation for the recovery tests: no test, and no xdist worker, touches the real store
under %LOCALAPPDATA%, and no two tests share a lease."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def recovery_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "recovery-home"
    monkeypatch.setenv("SCIO_RECOVERY_HOME", str(home))
    return home
