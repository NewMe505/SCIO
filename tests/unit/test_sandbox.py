from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scio.aci.sandbox.base import ExecResult, Limits, PathEscapeError, resolve_under
from scio.aci.sandbox.docker import DockerSandbox
from scio.aci.sandbox.e2b import E2BSandbox
from scio.aci.sandbox.local import LocalSandbox

# ---------- path rules ----------


def test_resolve_under_posix() -> None:
    assert resolve_under("/work", "/work/a/../b.py") == "/work/b.py"
    with pytest.raises(PathEscapeError):
        resolve_under("/work", "relative.py")
    with pytest.raises(PathEscapeError):
        resolve_under("/work", "/work/../etc/passwd")
    with pytest.raises(PathEscapeError):
        resolve_under("/work", "/worker/x")


def test_resolve_under_windows() -> None:
    assert resolve_under("C:\\repo", "C:\\repo\\a\\..\\b.py") == "C:\\repo\\b.py"
    with pytest.raises(PathEscapeError):
        resolve_under("C:\\repo", "C:\\other\\x")


# ---------- local ----------


def test_local_exec_read_write_and_timeout(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path, Limits(timeout_s=5))
    sb.write_text(f"{sb.root}/x.txt", "hi\n")
    assert sb.exists(f"{sb.root}/x.txt") and sb.read_text(f"{sb.root}/x.txt") == "hi\n"
    r = sb.exec(
        ["python", "-c", "import os,sys; print(os.environ['SCIO_SANDBOX']); print(sys.executable)"]
    )
    assert (
        r.ok and r.stdout.splitlines()[0] == "local" and r.stdout.splitlines()[1] == sys.executable
    )
    t = sb.exec(["python", "-c", "import time; time.sleep(5)"], timeout_s=1)
    assert t.timed_out and t.exit_code == 124 and not t.ok
    missing = sb.exec(["definitely-not-a-command-xyz"])
    assert missing.exit_code == 127
    with pytest.raises(PathEscapeError):
        sb.read_text("/etc/passwd" if sys.platform != "win32" else "C:\\Windows\\win.ini")


# ---------- docker (no daemon needed) ----------


def test_docker_argv_and_exec_via_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout_s: int | None) -> ExecResult:
        calls.append(argv)
        return ExecResult(0, "ok", "")

    sb = DockerSandbox(
        tmp_path, image="python:3.12-slim", limits=Limits(memory_mb=512, cpus=0.5), runner=runner
    )
    r = sb.exec(["python", "-V"], cwd="/work/sub", env={"A": "1"})
    assert r.ok
    argv = calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "512m" and argv[argv.index("--cpus") + 1] == "0.5"
    assert argv[argv.index("-v") + 1] == f"{tmp_path.resolve()}:/work"
    assert argv[argv.index("-w") + 1] == "/work/sub" and "A=1" in argv
    assert argv[-3:] == ["python:3.12-slim", "python", "-V"]
    sb.write_text("/work/d/f.txt", "z")
    assert (tmp_path / "d" / "f.txt").read_text() == "z" and sb.read_text("/work/d/f.txt") == "z"


def test_docker_timeout_kills_container(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout_s: int | None) -> ExecResult:
        calls.append(argv)
        if argv[1] == "run":
            return ExecResult(124, "", "[timed out]", timed_out=True)
        return ExecResult(0, "", "")

    sb = DockerSandbox(tmp_path, image="img", runner=runner)
    assert sb.exec(["sleep", "99"], timeout_s=1).timed_out
    assert calls[1][:2] == ["docker", "kill"] and calls[1][2].startswith("scio-")


# ---------- e2b (fake client) ----------


class _FakeFiles:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def read(self, path: str, format: str = "text") -> str:
        return self.store[path]

    def write(self, path: str, data: str) -> None:
        self.store[path] = data

    def exists(self, path: str) -> bool:
        return path in self.store


class _FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, cmd: str, **kw: object) -> SimpleNamespace:
        self.calls.append((cmd, kw))
        if "fail" in cmd:
            raise type(
                "CommandExitException",
                (Exception,),
                {"exit_code": 3, "stdout": "", "stderr": "bad"},
            )()
        return SimpleNamespace(exit_code=0, stdout="out", stderr="")


def test_e2b_adapter_with_fake_client() -> None:
    client = SimpleNamespace(commands=_FakeCommands(), files=_FakeFiles(), kill=lambda: None)
    sb = E2BSandbox(client=client, limits=Limits(timeout_s=7))
    assert client.commands.calls[0][0].startswith("mkdir -p")
    r = sb.exec(["echo", "a b"], cwd="/home/user/work/x")
    assert r.ok and r.stdout == "out"
    cmd, kw = client.commands.calls[1]
    assert cmd == "echo 'a b'" and kw["cwd"] == "/home/user/work/x" and kw["timeout"] == 7
    bad = sb.exec(["fail"])
    assert bad.exit_code == 3 and bad.stderr == "bad"
    sb.write_text("/home/user/work/f.py", "x=1")
    assert sb.exists("/home/user/work/f.py") and sb.read_text("/home/user/work/f.py") == "x=1"
    with pytest.raises(PathEscapeError):
        sb.read_text("/etc/passwd")
    sb.close()
