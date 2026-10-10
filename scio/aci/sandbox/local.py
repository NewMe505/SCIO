"""Local subprocess sandbox: tests and development only.

It enforces the wall-clock limit and, on POSIX, address-space and CPU limits. It
**cannot** turn the network off; ``Limits.network=False`` is recorded but not enforced,
and a warning is logged once. Use ``docker`` or ``e2b`` for anything untrusted.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scio.aci.sandbox.base import ExecResult, Limits, resolve_under

log = logging.getLogger(__name__)


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


class LocalSandbox:
    host_root: Path

    def __init__(self, root: str | Path, limits: Limits | None = None) -> None:
        self.host_root = Path(root).resolve()
        self.host_root.mkdir(parents=True, exist_ok=True)
        self.root = str(self.host_root)
        self.limits = limits or Limits()
        if not self.limits.network:
            log.warning("LocalSandbox cannot disable the network; use docker or e2b for isolation")

    def resolve(self, path: str) -> str:
        return resolve_under(self.root, path)

    def _preexec(self) -> None:  # POSIX only; the module does not exist on Windows
        resource: Any = importlib.import_module("resource")
        mem = self.limits.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        cpu = max(1, int(self.limits.timeout_s * max(self.limits.cpus, 0.1)))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        timeout = timeout_s or self.limits.timeout_s
        argv = list(argv)
        if argv and argv[0] == "python":
            argv[0] = sys.executable  # the interpreter running SCIO, not whatever is on PATH
        run_env = {**os.environ, "PYTHONUTF8": "1", "SCIO_SANDBOX": "local", **(env or {})}
        if not self.limits.network:
            run_env.setdefault("PIP_NO_INDEX", "1")
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(  # noqa: S603 - argv is a list, no shell
                argv,
                cwd=self.resolve(cwd) if cwd else self.root,
                env=run_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                preexec_fn=self._preexec if sys.platform != "win32" else None,
            )
        except subprocess.TimeoutExpired as exc:
            out = _text(exc.stdout)
            err = _text(exc.stderr)
            return ExecResult(
                exit_code=124,
                stdout=out,
                stderr=f"{err}\n[timed out after {timeout}s]".strip(),
                timed_out=True,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        except FileNotFoundError as exc:
            return ExecResult(exit_code=127, stdout="", stderr=f"command not found: {exc.filename}")
        return ExecResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    def read_text(self, path: str) -> str:
        return Path(self.resolve(path)).read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        p = Path(self.resolve(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")

    def exists(self, path: str) -> bool:
        return Path(self.resolve(path)).exists()

    def close(self) -> None:
        return None
