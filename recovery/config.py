"""Where the supervisor keeps its state, and each workspace's ``recovery.toml``.

``recovery.toml`` lives in the workspace, the directory the SCIO launcher runs from (for
graphcalc, ``graphcalc-run/``)::

    launcher = ["sh", "scio.sh"]
    scio_repo = "C:/Users/me/projects/SCIO"
    scio_gh_repo = "NewMe505/SCIO"
    # live_wt defaults to "<scio_repo>/.scio/live-wt"
    # claude_bin defaults to "claude"

    [limits]
    hang_after_s = 2700
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HOME_ENV = "SCIO_RECOVERY_HOME"

RECORDED_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "USERPROFILE",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "UV_SYSTEM_CERTS",
    "PYTHONUTF8",
    "LANG",
)
"""Variables a replay needs when it starts from Task Scheduler's environment (ADR 0019
section 12a). None of them holds a secret."""

RECORDED_ENV_PREFIXES: tuple[str, ...] = ("SCIO_",)

SECRET_NAME_PARTS: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
"""A variable whose name holds one of these parts is never recorded, even with a
``SCIO_`` prefix."""

NEVER_RECORDED: tuple[str, ...] = (
    "CLAUDECODE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    HOME_ENV,
)


def recorded_env(env: Mapping[str, str]) -> dict[str, str]:
    """The subset of ``env`` a run record keeps and a replay restores."""
    out: dict[str, str] = {}
    for name, value in env.items():
        upper = name.upper()
        if name in NEVER_RECORDED or any(part in upper for part in SECRET_NAME_PARTS):
            continue
        if upper in RECORDED_ENV_NAMES or upper.startswith(RECORDED_ENV_PREFIXES):
            out[name] = value
    return out


def recovery_home(env: Mapping[str, str] | None = None) -> Path:
    """``$SCIO_RECOVERY_HOME``, else ``%LOCALAPPDATA%/SCIO/recovery`` on Windows, else
    ``$XDG_STATE_HOME/scio/recovery`` (``~/.local/state/scio/recovery``)."""
    env = os.environ if env is None else env
    if env.get(HOME_ENV):
        return Path(env[HOME_ENV])
    if sys.platform == "win32" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "SCIO" / "recovery"
    state = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "scio" / "recovery"


@dataclass(frozen=True)
class Limits:
    """Runaway guards (ADR 0019 decision 7). The repair caps are set from the live drill."""

    poll_s: float = 15.0
    hang_after_s: float = 2700.0
    """No progress for this long kills the run: above the Claude Code worker's 1800 s turn."""
    repair_timeout_s: float = 5400.0
    repair_max_turns: int = 400
    repair_max_budget_usd: float = 40.0
    max_attempts: int = 2
    max_incidents_per_day: int = 3
    max_replays: int = 3
    backoff_s: tuple[float, ...] = (300.0, 900.0, 1800.0)
    ci_wait_s: float = 2700.0


@dataclass(frozen=True)
class WorkspaceConfig:
    workspace: Path
    launcher: tuple[str, ...]
    scio_repo: Path
    scio_gh_repo: str
    live_wt: Path
    claude_bin: str = "claude"
    permission_mode: str = "auto"
    limits: Limits = field(default_factory=Limits)

    @property
    def lease_name(self) -> str:
        return f"workspace:{self.workspace.resolve().as_posix().lower()}"

    @classmethod
    def load(cls, workspace: Path | str) -> WorkspaceConfig:
        """Read ``<workspace>/recovery.toml``. Raises ``FileNotFoundError`` when it is
        missing and ``ValueError`` when a required key is absent."""
        ws = Path(workspace).resolve()
        data = tomllib.loads((ws / "recovery.toml").read_text(encoding="utf-8"))
        return cls.from_mapping(ws, data)

    @classmethod
    def from_mapping(cls, workspace: Path, data: Mapping[str, Any]) -> WorkspaceConfig:
        missing = [k for k in ("launcher", "scio_repo", "scio_gh_repo") if k not in data]
        if missing:
            raise ValueError(f"recovery.toml is missing {', '.join(missing)}")
        repo = Path(str(data["scio_repo"]))
        limits_data = dict(data.get("limits", {}))
        known = {f.name for f in dataclasses.fields(Limits)}
        unknown = sorted(set(limits_data) - known)
        if unknown:
            raise ValueError(f"recovery.toml [limits] has unknown keys: {', '.join(unknown)}")
        if "backoff_s" in limits_data:
            limits_data["backoff_s"] = tuple(float(x) for x in limits_data["backoff_s"])
        return cls(
            workspace=workspace,
            launcher=tuple(str(x) for x in data["launcher"]),
            scio_repo=repo,
            scio_gh_repo=str(data["scio_gh_repo"]),
            live_wt=Path(str(data.get("live_wt", repo / ".scio" / "live-wt"))),
            claude_bin=str(data.get("claude_bin", "claude")),
            permission_mode=str(data.get("permission_mode", "auto")),
            limits=Limits(**limits_data),
        )
