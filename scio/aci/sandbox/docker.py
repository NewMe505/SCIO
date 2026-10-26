"""Docker sandbox: one persistent container per sandbox, offline by construction.

The container runs ``sleep infinity`` on a user-defined **internal** network (no route out of
the host, no DNS beyond the network) with the host working tree mounted at ``/work``; every
``exec`` is a ``docker exec`` into it, so a product environment created by one command is
there for the next. ``exec(network=True)`` (the install phase, M9, ADR 0013 decision 10)
connects an egress-proxy sidecar to that network for the duration of the call and points
``HTTPS_PROXY`` at it; the sidecar runs ``scio/aci/sandbox/egress_proxy.py`` from the same
image and admits ``CONNECT`` to ``Limits.network_allowlist`` only, so pip reaches the
registry and nothing else leaves the build. With an empty allowlist the container is joined
to the default bridge instead (unrestricted, reported by the caller). Files are read and
written on the host side of the mount. The ``runner`` is injectable so the command
construction is unit-tested without Docker installed; ``tests/live/test_docker_live.py``
runs the real thing wherever a daemon is reachable (CI).
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from scio.aci.sandbox.base import ExecResult, Limits, SandboxError, resolve_under

CONTAINER_ROOT = "/work"
PROXY_PORT = 8888
PROXY_MODULE = Path(__file__).with_name("egress_proxy.py")
PROXY_MOUNT = "/egress_proxy.py"

Runner = Callable[[list[str], int | None], ExecResult]


def subprocess_runner(argv: list[str], timeout_s: int | None) -> ExecResult:
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
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


def docker_ready(timeout_s: int = 30) -> bool:
    """True when a Docker daemon answers ``docker info`` (the product build default picks
    Docker only then)."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=timeout_s).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def host_git_identity() -> tuple[str, str]:
    """The host's git user, so commits made inside the container carry the owner's name."""
    out: list[str] = []
    for key in ("user.name", "user.email"):
        try:
            r = subprocess.run(
                ["git", "config", "--get", key], capture_output=True, text=True, timeout=10
            )
            out.append(r.stdout.strip() if r.returncode == 0 else "")
        except (OSError, subprocess.TimeoutExpired):
            out.append("")
    return out[0] or "SCIO", out[1] or "scio@localhost"


def docker_product_ready(image: str) -> str | None:
    """None when a product build can run in Docker with ``image``, else the reason it cannot:
    no daemon, the image is not available locally, or it has no ``git`` (product builds run
    git through the sandbox; ``docker/sandbox.Dockerfile`` adds it to the Python image)."""
    if not docker_ready():
        return "no Docker daemon reachable"
    try:
        have = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=60
        )
        if have.returncode != 0:
            return f"image {image} is not available locally (docker build -t {image} docker/)"
        git = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", image, "git", "--version"],
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker did not answer: {exc}"
    if git.returncode != 0:
        return f"image {image} has no git (build docker/sandbox.Dockerfile)"
    return None


class DockerSandbox:
    host_root: Path

    def __init__(
        self,
        host_root: str | Path,
        *,
        image: str,
        limits: Limits | None = None,
        runner: Runner = subprocess_runner,
        name: str | None = None,
        git_identity: tuple[str, str] | None = None,
    ) -> None:
        self.host_root = Path(host_root).resolve()
        self.host_root.mkdir(parents=True, exist_ok=True)
        self.root = CONTAINER_ROOT
        self.image = image
        self.limits = limits or Limits()
        self._run = runner
        self.python: str | None = None  # a product environment inside the sandbox (M9)
        self.name = name or f"scio-{uuid.uuid4().hex[:12]}"
        self._git_identity = git_identity
        self._network_created = False
        self._container_running = False
        self._proxy_running = False

    # ---------- names and command shapes (pure, unit-tested) ----------

    @property
    def network_name(self) -> str:
        return f"{self.name}-net"

    @property
    def proxy_name(self) -> str:
        return f"{self.name}-proxy"

    def resolve(self, path: str) -> str:
        return resolve_under(self.root, path)

    def _host_path(self, path: str) -> Path:
        rel = self.resolve(path).removeprefix(self.root).lstrip("/")
        return self.host_root / rel if rel else self.host_root

    def network_create_argv(self) -> list[str]:
        return ["docker", "network", "create", "--internal", self.network_name]

    def run_argv(self) -> list[str]:
        """Start the long-lived container (``sleep infinity``) with the caps and the mount."""
        name, email = self._git_identity or host_git_identity()
        return [
            "docker", "run", "-d", "--name", self.name,
            "--network", self.network_name,
            "--cpus", str(self.limits.cpus),
            "--memory", f"{self.limits.memory_mb}m",
            "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "-v", f"{self.host_root}:{self.root}",
            "-w", self.root,
            "-e", "PYTHONUTF8=1", "-e", "SCIO_SANDBOX=docker",
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            # The mount belongs to another uid as far as git in the container can tell.
            "-e", "GIT_CONFIG_COUNT=3",
            "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*",
            "-e", "GIT_CONFIG_KEY_1=user.name", "-e", f"GIT_CONFIG_VALUE_1={name}",
            "-e", "GIT_CONFIG_KEY_2=user.email", "-e", f"GIT_CONFIG_VALUE_2={email}",
            self.image, "sleep", "infinity",
        ]  # fmt: skip

    def proxy_run_argv(self) -> list[str]:
        """Start the egress proxy sidecar on the default bridge (it reaches the registry)."""
        return [
            "docker", "run", "-d", "--name", self.proxy_name,
            "--security-opt", "no-new-privileges",
            "-v", f"{PROXY_MODULE}:{PROXY_MOUNT}:ro",
            self.image, "python", PROXY_MOUNT, str(PROXY_PORT), *self.limits.network_allowlist,
        ]  # fmt: skip

    def exec_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: int,
    ) -> list[str]:
        cmd = ["docker", "exec", "-w", self.resolve(cwd) if cwd else self.root]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.name, "timeout", str(timeout_s), *argv]
        return cmd

    # ---------- lifecycle ----------

    def _check(self, result: ExecResult, what: str) -> None:
        if not result.ok:
            raise SandboxError(f"docker: could not {what}: {result.output.strip()[-800:]}")

    def _ensure_started(self) -> None:
        if not self._network_created:
            self._check(self._run(self.network_create_argv(), 60), "create the network")
            self._network_created = True
        if not self._container_running:
            self._check(self._run(self.run_argv(), 300), "start the container")
            self._container_running = True

    def _ensure_proxy(self) -> None:
        if not self._proxy_running:
            self._check(self._run(self.proxy_run_argv(), 300), "start the egress proxy")
            self._proxy_running = True

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
        network: bool | None = None,
    ) -> ExecResult:
        self._ensure_started()
        argv = list(argv)
        if argv and argv[0] == "python" and self.python:
            argv[0] = self.python
        timeout = timeout_s or self.limits.timeout_s
        allow_network = self.limits.network if network is None else network
        run_env = dict(env or {})
        joined: str | None = None  # what to disconnect afterwards
        if allow_network:
            if self.limits.network_allowlist:
                self._ensure_proxy()
                self._check(
                    self._run(
                        ["docker", "network", "connect", self.network_name, self.proxy_name], 60
                    ),
                    "attach the egress proxy",
                )
                joined = "proxy"
                proxy_url = f"http://{self.proxy_name}:{PROXY_PORT}"
                run_env.update({"HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url, "NO_PROXY": ""})
            else:
                self._check(
                    self._run(["docker", "network", "connect", "bridge", self.name], 60),
                    "join the bridge network",
                )
                joined = "bridge"
        cmd = self.exec_argv(argv, cwd=cwd, env=run_env, timeout_s=timeout)
        try:
            result = self._run(cmd, timeout + 5)
        finally:
            if joined == "proxy":
                detach = ["docker", "network", "disconnect", self.network_name, self.proxy_name]
                self._run(detach, 60)
            elif joined == "bridge":
                self._run(["docker", "network", "disconnect", "bridge", self.name], 60)
        if result.exit_code == 124 and not result.timed_out:
            # coreutils `timeout` inside the container ended the command
            result = replace(result, timed_out=True, stderr=result.stderr or "[timed out]")
        elif result.timed_out:
            # the runner's backstop fired: the container itself is wedged, replace it
            self._run(["docker", "rm", "-f", self.name], 30)
            self._container_running = False
        return result

    def read_text(self, path: str) -> str:
        return self._host_path(path).read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        p = self._host_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")

    def exists(self, path: str) -> bool:
        # A symlink made inside the container may point at a path that exists only there
        # (a venv's bin/python -> /usr/local/bin/python); it still exists (I-078).
        p = self._host_path(path)
        return p.exists() or p.is_symlink()

    def delete(self, path: str) -> None:
        p = self._host_path(path)
        if p.is_dir():
            raise IsADirectoryError(f"refusing to delete a directory: {path}")
        p.unlink(missing_ok=True)

    def close(self) -> None:
        """Remove the container, the proxy and the network (idempotent)."""
        if self._container_running:
            self._run(["docker", "rm", "-f", self.name], 60)
            self._container_running = False
        if self._proxy_running:
            self._run(["docker", "rm", "-f", self.proxy_name], 60)
            self._proxy_running = False
        if self._network_created:
            self._run(["docker", "network", "rm", self.network_name], 60)
            self._network_created = False
