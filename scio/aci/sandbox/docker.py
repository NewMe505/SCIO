"""Docker sandbox for local development: ``--network none``, CPU and memory caps.

Each ``exec`` is one ``docker run --rm`` with the host working tree mounted at
``/work``. Files are read and written on the host side of the mount. The ``runner`` is
injectable so the command construction is unit-tested without Docker installed.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from scio.aci.sandbox.base import ExecResult, Limits, resolve_under

CONTAINER_ROOT = "/work"

Runner = Callable[[list[str], int | None], ExecResult]


def subprocess_runner(argv: list[str], timeout_s: int | None) -> ExecResult:
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode("utf-8", "replace")
        )
        return ExecResult(exit_code=124, stdout=out, stderr="[timed out]", timed_out=True)
    except FileNotFoundError:
        return ExecResult(exit_code=127, stdout="", stderr="docker: command not found")
    return ExecResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )


class DockerSandbox:
    host_root: Path

    def __init__(
        self,
        host_root: str | Path,
        *,
        image: str,
        limits: Limits | None = None,
        runner: Runner = subprocess_runner,
    ) -> None:
        self.host_root = Path(host_root).resolve()
        self.host_root.mkdir(parents=True, exist_ok=True)
        self.root = CONTAINER_ROOT
        self.image = image
        self.limits = limits or Limits()
        self._run = runner

    def resolve(self, path: str) -> str:
        return resolve_under(self.root, path)

    def _host_path(self, path: str) -> Path:
        rel = self.resolve(path).removeprefix(self.root).lstrip("/")
        return self.host_root / rel if rel else self.host_root

    def docker_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        name = f"scio-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm", "--name", name,
            "--network", "bridge" if self.limits.network else "none",
            "--cpus", str(self.limits.cpus),
            "--memory", f"{self.limits.memory_mb}m",
            "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "-v", f"{self.host_root}:{self.root}",
            "-w", self.resolve(cwd) if cwd else self.root,
            "-e", "PYTHONUTF8=1", "-e", "SCIO_SANDBOX=docker",
        ]  # fmt: skip
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(self.image)
        cmd.extend(argv)
        return cmd

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        timeout = timeout_s or self.limits.timeout_s
        cmd = self.docker_argv(argv, cwd=cwd, env=env)
        result = self._run(cmd, timeout + 5)
        if result.timed_out:
            self._run(["docker", "kill", cmd[cmd.index("--name") + 1]], 15)
        return result

    def read_text(self, path: str) -> str:
        return self._host_path(path).read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        p = self._host_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")

    def exists(self, path: str) -> bool:
        return self._host_path(path).exists()

    def close(self) -> None:
        return None
