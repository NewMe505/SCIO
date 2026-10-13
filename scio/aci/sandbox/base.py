"""The ``Sandbox`` protocol: where agent-driven code runs.

Network is off by default; CPU, memory and wall-clock limits are explicit. Paths
handed to tools are absolute and must stay under :attr:`Sandbox.root`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol, runtime_checkable


class SandboxError(RuntimeError):
    pass


class PathEscapeError(SandboxError):
    pass


@dataclass(frozen=True)
class Limits:
    timeout_s: int = 120
    memory_mb: int = 2048
    cpus: float = 1.0
    network: bool = False


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return self.stdout if not self.stderr else f"{self.stdout}\n{self.stderr}".strip()


@runtime_checkable
class Sandbox(Protocol):
    root: str
    """Absolute path of the working tree *as seen inside the sandbox*."""
    limits: Limits
    python: str | None
    """Interpreter that ``python`` in an argv is rewritten to (the product environment once
    ``ensure_product_env`` created it, M9); None means the backend's default."""

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
        network: bool | None = None,
    ) -> ExecResult:
        """Run ``argv``. ``network=True`` opens the network for this one call (the install
        phase, M9); None keeps ``limits.network``."""
        ...

    def read_text(self, path: str) -> str: ...

    def write_text(self, path: str, content: str) -> None: ...

    def exists(self, path: str) -> bool: ...

    def delete(self, path: str) -> None:
        """Remove one file under ``root`` (M9, ``delete_file``). Directories are refused;
        a missing file is not an error."""
        ...

    def resolve(self, path: str) -> str:
        """Absolute, normalized sandbox path under ``root``; raises ``PathEscapeError``."""
        ...

    def close(self) -> None: ...


def resolve_under(root: str, path: str) -> str:
    """Pure path check shared by backends. ``path`` must be absolute and inside ``root``."""
    windows = len(root) > 1 and root[1] == ":"
    cls = PureWindowsPath if windows else PurePosixPath
    r = cls(root)
    p = cls(path)
    if not p.is_absolute():
        raise PathEscapeError(f"path must be absolute (under {root}): {path!r}")
    parts: list[str] = []
    for part in p.parts[1:]:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    normalized = cls(p.anchor, *parts)
    try:
        normalized.relative_to(r)
    except ValueError as exc:
        raise PathEscapeError(f"path escapes the sandbox root {root}: {path!r}") from exc
    return str(normalized)
