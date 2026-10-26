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
        tmp_path,
        image="python:3.12-slim",
        limits=Limits(memory_mb=512, cpus=0.5, timeout_s=90),
        runner=runner,
        name="scio-test",
        git_identity=("Owner", "owner@example.com"),
    )
    r = sb.exec(["python", "-V"], cwd="/work/sub", env={"A": "1"})
    assert r.ok
    # one persistent container on an internal network, then docker exec into it
    assert calls[0] == ["docker", "network", "create", "--internal", "scio-test-net"]
    run = calls[1]
    assert run[:4] == ["docker", "run", "-d", "--name"] and run[4] == "scio-test"
    assert run[run.index("--network") + 1] == "scio-test-net"
    assert run[run.index("--memory") + 1] == "512m" and run[run.index("--cpus") + 1] == "0.5"
    assert run[run.index("-v") + 1] == f"{tmp_path.resolve()}:/work"
    assert "GIT_CONFIG_VALUE_0=*" in run and "GIT_CONFIG_VALUE_1=Owner" in run
    assert run[-3:] == ["python:3.12-slim", "sleep", "infinity"]
    ex = calls[2]
    assert ex[:2] == ["docker", "exec"] and ex[ex.index("-w") + 1] == "/work/sub" and "A=1" in ex
    assert ex[-5:] == ["scio-test", "timeout", "90", "python", "-V"]
    # the second exec reuses the container and `python` is rewritten to the product env
    sb.python = "/work/.scio/sandbox/venv/bin/python"
    sb.exec(["python", "-c", "1"])
    assert len(calls) == 4 and calls[3][-3:] == ["/work/.scio/sandbox/venv/bin/python", "-c", "1"]
    sb.write_text("/work/d/f.txt", "z")
    assert (tmp_path / "d" / "f.txt").read_text() == "z" and sb.read_text("/work/d/f.txt") == "z"
    # a symlink whose target exists only inside the container still exists (I-078)
    try:
        (tmp_path / "d" / "python").symlink_to("/usr/local/bin/python-only-in-the-container")
    except OSError:
        pass  # no symlink privilege on this host; the live test covers it
    else:
        assert not (tmp_path / "d" / "python").exists() and sb.exists("/work/d/python")
    sb.close()
    assert calls[-2] == ["docker", "rm", "-f", "scio-test"]
    assert calls[-1] == ["docker", "network", "rm", "scio-test-net"]
    sb.close()
    assert len(calls) == 6  # idempotent


def test_docker_network_open_call_attaches_the_egress_proxy_for_its_duration(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout_s: int | None) -> ExecResult:
        calls.append(argv)
        return ExecResult(0, "ok", "")

    allow = ("pypi.org", "files.pythonhosted.org")
    sb = DockerSandbox(
        tmp_path,
        image="img",
        limits=Limits(network_allowlist=allow),
        runner=runner,
        name="scio-p",
        git_identity=("a", "b"),
    )
    sb.exec(["python", "-m", "pip", "install", "x"], network=True)
    kinds = [c[1] + ("/" + c[2] if c[1] == "network" else "") for c in calls]
    expected = ["network/create", "run", "run", "network/connect", "exec", "network/disconnect"]
    assert kinds == expected
    proxy = calls[2]
    assert proxy[proxy.index("--name") + 1] == "scio-p-proxy" and "--network" not in proxy
    assert proxy[-4:] == ["/egress_proxy.py", "8888", *allow]
    assert proxy[proxy.index("-v") + 1].endswith("egress_proxy.py:/egress_proxy.py:ro")
    assert calls[3] == ["docker", "network", "connect", "scio-p-net", "scio-p-proxy"]
    ex = calls[4]
    assert "HTTPS_PROXY=http://scio-p-proxy:8888" in ex
    assert "HTTP_PROXY=http://scio-p-proxy:8888" in ex
    assert calls[5] == ["docker", "network", "disconnect", "scio-p-net", "scio-p-proxy"]
    # an offline call gets no proxy and no env
    sb.exec(["python", "-m", "pytest"])
    assert calls[-1][1] == "exec" and not any("PROXY" in a for a in calls[-1])
    # the proxy is started once and removed on close
    sb.exec(["python", "-m", "pip", "install", "y"], network=True)
    assert [c[1] for c in calls[-3:]] == ["network", "exec", "network"]
    sb.close()
    assert ["docker", "rm", "-f", "scio-p-proxy"] in calls[-3:]


def test_docker_network_open_without_an_allowlist_joins_the_bridge(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout_s: int | None) -> ExecResult:
        calls.append(argv)
        return ExecResult(0, "ok", "")

    sb = DockerSandbox(tmp_path, image="img", runner=runner, name="scio-b", git_identity=("a", "b"))
    sb.exec(["curl", "x"], network=True)
    assert ["docker", "network", "connect", "bridge", "scio-b"] in calls
    assert calls[-1] == ["docker", "network", "disconnect", "bridge", "scio-b"]


def test_docker_timeouts_inside_and_outside_the_container(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    wedge = {"on": False}

    def runner(argv: list[str], timeout_s: int | None) -> ExecResult:
        calls.append(argv)
        if argv[1] == "exec":
            if wedge["on"]:
                return ExecResult(124, "", "[timed out]", timed_out=True)
            return ExecResult(124, "", "")  # coreutils `timeout` ended the command
        return ExecResult(0, "", "")

    sb = DockerSandbox(tmp_path, image="img", runner=runner, name="scio-t", git_identity=("a", "b"))
    r = sb.exec(["sleep", "99"], timeout_s=1)
    assert r.timed_out and r.exit_code == 124 and calls[-1][1] == "exec"  # container kept
    n = len(calls)
    sb.exec(["true"])
    assert len(calls) == n + 1  # no restart needed
    wedge["on"] = True
    assert sb.exec(["sleep", "99"], timeout_s=1).timed_out
    assert calls[-1] == ["docker", "rm", "-f", "scio-t"]  # the wedged container is replaced
    wedge["on"] = False
    sb.exec(["true"])
    assert calls[-2][:3] == ["docker", "run", "-d"] and calls[-1][1] == "exec"


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

    def remove(self, path: str) -> None:
        del self.store[path]


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
