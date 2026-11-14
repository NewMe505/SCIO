"""Workspace config, the recorded environment, and the default command runner."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from recovery.commands import run_command
from recovery.config import Limits, WorkspaceConfig, recorded_env, recovery_home


def test_the_recorded_environment_keeps_what_a_replay_needs_and_no_secret() -> None:
    env = {
        "PATH": "/bin",
        "USERPROFILE": "C:/Users/me",
        "LOCALAPPDATA": "C:/Users/me/AppData/Local",
        "SCIO_PROVIDER_OVERRIDE": "claude_code",
        "SCIO_PARALLEL_FEATURES": "2",
        "SCIO_GH_TOKEN": "ghp_x",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "CLAUDECODE": "1",
        "SCIO_RECOVERY_HOME": "/tmp/h",
        "GITHUB_TOKEN": "x",
        "RANDOM_VAR": "y",
    }
    assert recorded_env(env) == {
        "PATH": "/bin",
        "USERPROFILE": "C:/Users/me",
        "LOCALAPPDATA": "C:/Users/me/AppData/Local",
        "SCIO_PROVIDER_OVERRIDE": "claude_code",
        "SCIO_PARALLEL_FEATURES": "2",
    }


def test_the_home_is_the_override_else_localappdata_on_windows() -> None:
    assert recovery_home({"SCIO_RECOVERY_HOME": "/x/y"}) == Path("/x/y")
    if sys.platform == "win32":
        assert recovery_home({"LOCALAPPDATA": "C:/L"}) == Path("C:/L/SCIO/recovery")


def test_a_workspace_config_reads_its_toml_with_defaults(tmp_path: Path) -> None:
    (tmp_path / "recovery.toml").write_text(
        'launcher = ["sh", "scio.sh"]\n'
        'scio_repo = "C:/p/SCIO"\n'
        'scio_gh_repo = "o/SCIO"\n'
        "[limits]\nhang_after_s = 60\nbackoff_s = [1, 2]\n",
        encoding="utf-8",
        newline="\n",
    )
    cfg = WorkspaceConfig.load(tmp_path)
    assert cfg.launcher == ("sh", "scio.sh")
    assert cfg.live_wt == Path("C:/p/SCIO") / ".scio" / "live-wt"
    assert cfg.permission_mode == "auto"
    assert cfg.limits.hang_after_s == 60 and cfg.limits.backoff_s == (1.0, 2.0)
    assert cfg.limits.max_attempts == Limits().max_attempts


def test_a_config_missing_a_key_or_with_an_unknown_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scio_gh_repo"):
        WorkspaceConfig.from_mapping(tmp_path, {"launcher": ["x"], "scio_repo": "r"})
    with pytest.raises(ValueError, match="unknown"):
        WorkspaceConfig.from_mapping(
            tmp_path,
            {"launcher": ["x"], "scio_repo": "r", "scio_gh_repo": "o/r", "limits": {"nope": 1}},
        )


def test_a_program_on_the_child_envs_path_only_is_found(tmp_path: Path) -> None:
    """I-183: on Windows the parent's PATH, not the child env's, finds argv[0]. A tick
    under Task Scheduler (no sh on its PATH) must still start the recorded env's sh."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if sys.platform == "win32":
        (bindir / "recoveryprobe.cmd").write_text("@echo found\n", encoding="utf-8", newline="\n")
    else:
        prog = bindir / "recoveryprobe"
        prog.write_text("#!/bin/sh\necho found\n", encoding="utf-8", newline="\n")
        prog.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    result = run_command(["recoveryprobe"], env=env)
    assert result.ok and result.stdout.strip() == "found", result


def test_the_default_runner_reports_codes_a_missing_binary_and_a_timeout() -> None:
    ok = run_command([sys.executable, "-c", "print('hi')"])
    assert ok.ok and ok.stdout.strip() == "hi"
    bad = run_command([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert bad.code == 3 and not bad.ok
    missing = run_command(["scio-recovery-no-such-binary"])
    assert missing.code == 127
    slow = run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
    assert slow.timed_out and not slow.ok
