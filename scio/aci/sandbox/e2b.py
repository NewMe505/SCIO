"""E2B sandbox (gVisor-backed microVM) for CI and production.

Requires the ``e2b`` extra and ``E2B_API_KEY``. Internet access follows
``Limits.network`` (off by default). The SDK surface used here, as introspected from
``e2b`` 2.55: ``Sandbox.create(template, timeout, allow_internet_access)``,
``sandbox.commands.run(cmd, cwd, timeout)``, ``sandbox.files.read/write/exists``,
``sandbox.kill()``.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Mapping, Sequence
from typing import Any

from scio.aci.sandbox.base import ExecResult, Limits, SandboxError, resolve_under

DEFAULT_ROOT = "/home/user/work"


class E2BSandbox:
    def __init__(
        self,
        *,
        template: str | None = None,
        limits: Limits | None = None,
        root: str = DEFAULT_ROOT,
        session_timeout_s: int = 3600,
        client: Any | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self.root = root
        self.python: str | None = None  # a product environment inside the sandbox (M9)
        if client is None:
            try:
                from e2b import Sandbox as E2B
            except ImportError as exc:  # pragma: no cover - depends on extra
                raise SandboxError("install the 'e2b' extra to use the E2B sandbox") from exc
            client = E2B.create(
                template=template,
                timeout=session_timeout_s,
                allow_internet_access=self.limits.network,
            )
        self._sb = client
        self._sb.commands.run(f"mkdir -p {shlex.quote(self.root)}", timeout=30)

    def resolve(self, path: str) -> str:
        return resolve_under(self.root, path)

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
        network: bool | None = None,  # E2B fixes network access per sandbox; ignored here
    ) -> ExecResult:
        timeout = timeout_s or self.limits.timeout_s
        cmd = shlex.join(argv)
        t0 = time.perf_counter()
        try:
            r = self._sb.commands.run(
                cmd,
                cwd=self.resolve(cwd) if cwd else self.root,
                envs={"PYTHONUTF8": "1", "SCIO_SANDBOX": "e2b", **(env or {})},
                timeout=timeout,
            )
            exit_code, stdout, stderr = int(r.exit_code), r.stdout or "", r.stderr or ""
        except Exception as exc:
            exit_code = int(getattr(exc, "exit_code", 124))
            stdout = str(getattr(exc, "stdout", "") or "")
            stderr = str(getattr(exc, "stderr", "") or str(exc))
            timed_out = "timeout" in type(exc).__name__.lower() or "timed out" in stderr.lower()
            return ExecResult(
                exit_code=124 if timed_out else exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    def read_text(self, path: str) -> str:
        return str(self._sb.files.read(self.resolve(path), format="text"))

    def write_text(self, path: str, content: str) -> None:
        self._sb.files.write(self.resolve(path), content)

    def exists(self, path: str) -> bool:
        return bool(self._sb.files.exists(self.resolve(path)))

    def delete(self, path: str) -> None:
        target = self.resolve(path)
        if self._sb.files.exists(target):
            self._sb.files.remove(target)  # e2b Filesystem.remove(path)

    def close(self) -> None:
        self._sb.kill()
