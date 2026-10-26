"""M9 runtime: the product environment, the approved-list gate of install_deps, run_app."""

from __future__ import annotations

import socket
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from scio.aci.runtime import (
    HOST_PTH,
    InstallDepsInput,
    RunAppInput,
    ToolContext,
    ensure_product_env,
    install_deps,
    normalise_package,
    run_app,
    venv_python,
)
from scio.aci.sandbox.base import ExecResult, Limits
from scio.aci.sandbox.local import LocalSandbox
from scio.aci.toolbox import ToolBox
from scio.aci.tools import ToolError
from scio.toolchain import python_toolchain


class RecordingSandbox(LocalSandbox):
    """A local sandbox whose exec is recorded and answered without running anything."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[tuple[list[str], bool | None]] = []

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
        network: bool | None = None,
    ) -> ExecResult:
        self.calls.append((list(argv), network))
        return ExecResult(exit_code=0, stdout="ok", stderr="")


class RemoteRecordingSandbox:
    """A non-local sandbox (what Docker or E2B look like to the runtime): records execs,
    answers them without running anything, and reports the venv as created after the first
    ``venv`` call."""

    def __init__(self, root: Path) -> None:
        self.root = str(root).replace("\\", "/")
        self.python: str | None = None
        self.calls: list[tuple[list[str], bool | None]] = []
        self._paths: set[str] = set()

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: Mapping[str, str] | None = None,
        network: bool | None = None,
    ) -> ExecResult:
        self.calls.append((list(argv), network))
        if "venv" in argv:
            self._paths.add(venv_python(self, python_toolchain()))
        return ExecResult(exit_code=0, stdout="ok", stderr="")

    def read_text(self, path: str) -> str:
        return ""

    def write_text(self, path: str, content: str) -> None:
        self._paths.add(path)

    def exists(self, path: str) -> bool:
        return path in self._paths

    def delete(self, path: str) -> None:
        self._paths.discard(path)

    def resolve(self, path: str) -> str:
        return path

    def close(self) -> None:
        return None


def test_normalise_package_names() -> None:
    assert normalise_package("Flask>=3.0") == "flask"
    assert normalise_package("typing_extensions") == "typing-extensions"
    assert normalise_package(" Rich[jupyter] ") == "rich"


def test_product_env_is_created_once_and_rewrites_python(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    tc = python_toolchain()
    first = ensure_product_env(sb, tc, ())
    assert first.startswith("product environment created at .scio/sandbox/venv")
    assert sb.python == venv_python(sb, tc) and Path(sb.python).is_file()
    again = ensure_product_env(sb, tc, ())
    assert again.startswith("product environment reused")
    r = sb.exec(["python", "-c", "import sys; print(sys.prefix)"])
    assert r.ok and ".scio" in r.stdout and "venv" in r.stdout
    # SCIO's own environment is linked through a .pth file, so the test runner and the
    # coverage plugin SCIO runs with are importable without a download, and they come from
    # that environment, not from the base interpreter (I-074: the runner had no pytest there)
    pths = list((tmp_path / ".scio" / "sandbox" / "venv").rglob(HOST_PTH))
    assert len(pths) == 1
    host_purelib = sysconfig.get_paths()["purelib"]
    assert pths[0].read_text(encoding="utf-8").strip() == host_purelib
    r = sb.exec(["python", "-c", "import pytest, pytest_cov; print(pytest.__file__)"])
    assert r.ok, r.output
    assert Path(r.stdout.strip()).resolve().is_relative_to(Path(host_purelib).resolve())


def test_non_local_sandbox_installs_the_test_tools_on_creation(tmp_path: Path) -> None:
    """A Docker or E2B environment has no host tools to link: the profile's ``test_tools``
    go in with the approved packages during the network-open install call."""
    sb = RemoteRecordingSandbox(tmp_path)
    tc = python_toolchain()
    summary = ensure_product_env(sb, tc, ("pyyaml",))
    assert "created" in summary and "pytest>=8.3" in summary and "pyyaml" in summary
    installs = [(argv, net) for argv, net in sb.calls if "pip" in argv]
    assert len(installs) == 1
    argv, network = installs[0]
    assert network is True and argv[-3:] == ["pytest>=8.3", "pytest-cov>=6.0", "pyyaml"]
    assert not list(tmp_path.rglob(HOST_PTH))
    sb.calls.clear()
    assert "reused" in ensure_product_env(sb, tc, ("pyyaml",))
    argv, _ = sb.calls[-1]
    assert argv[-1:] == ["pyyaml"] and "pytest>=8.3" not in argv  # tools only once


def test_install_phase_opens_the_network_for_approved_packages_only(tmp_path: Path) -> None:
    sb = RecordingSandbox(tmp_path)
    tc = python_toolchain()
    (tmp_path / ".scio" / "sandbox" / "venv").mkdir(parents=True)
    Path(venv_python(sb, tc)).parent.mkdir(parents=True, exist_ok=True)
    Path(venv_python(sb, tc)).write_text("", encoding="utf-8")  # pretend the venv exists
    summary = ensure_product_env(sb, tc, ("flask>=3", "rich"))
    assert "installed flask>=3, rich" in summary and "pypi.org" in summary
    argv, network = sb.calls[-1]
    assert argv[:3] == ["python", "-m", "pip"] and argv[-2:] == ["flask>=3", "rich"]
    assert network is True
    # the test phase keeps the network off: PIP_NO_INDEX is set unless network=True
    plain = LocalSandbox(tmp_path, Limits(network=False))
    r = plain.exec(["python", "-c", "import os; print(os.environ.get('PIP_NO_INDEX'))"])
    assert r.stdout.strip() == "1"
    r = plain.exec(
        ["python", "-c", "import os; print(os.environ.get('PIP_NO_INDEX'))"], network=True
    )
    assert r.stdout.strip() == "None"


def test_install_deps_is_gated_by_the_approved_list(tmp_path: Path) -> None:
    sb = RecordingSandbox(tmp_path)
    sb.python = sys.executable
    tc = python_toolchain()
    no_list = ToolContext(toolchain=tc, approved=None)
    with pytest.raises(ToolError, match="no approved dependency list"):
        install_deps(sb, InstallDepsInput(packages=["rich"]), no_list)
    ctx = ToolContext(toolchain=tc, approved=("Flask", "rich"))
    with pytest.raises(ToolError, match="requests not on the approved") as exc:
        install_deps(sb, InstallDepsInput(packages=["flask", "requests"]), ctx)
    assert "needs_design_decision" in str(exc.value) and sb.calls == []
    out = install_deps(sb, InstallDepsInput(packages=["flask>=3", "rich"]), ctx)
    assert out.startswith("installed flask>=3, rich")
    argv, network = sb.calls[-1]
    assert network is True and argv[-2:] == ["flask>=3", "rich"]
    assert ctx.installed == ["flask>=3", "rich"]


def test_toolbox_passes_the_context_to_context_aware_tools(tmp_path: Path) -> None:
    sb = RecordingSandbox(tmp_path)
    sb.python = sys.executable
    ctx = ToolContext(toolchain=python_toolchain(), approved=("rich",))
    box = ToolBox(sb, ("install_deps", "edit_file"), human_gate=False, context=ctx)
    result = box.call("c1", "install_deps", {"packages": ["rich"]})
    assert not result.is_error and "installed rich" in result.content
    denied = box.call("c2", "install_deps", {"packages": ["numpy"]})
    assert denied.is_error and "approved" in denied.content
    bare = ToolBox(sb, ("install_deps",), human_gate=False)
    assert bare.call("c3", "install_deps", {"packages": ["rich"]}).is_error


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_run_app_waits_for_health_then_stops(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    (tmp_path / "index.html").write_text("hi", encoding="utf-8")
    port = _free_port()
    ctx = ToolContext(toolchain=python_toolchain())
    out = run_app(
        sb,
        RunAppInput(
            module="http.server",
            args=[str(port), "--bind", "127.0.0.1"],
            health_url=f"http://127.0.0.1:{port}/index.html",
            timeout_s=30,
        ),
        ctx,
    )
    assert out.startswith("run_app -m http.server") and "healthy: http://127.0.0.1" in out
    with socket.socket() as s:  # the server was stopped
        s.settimeout(0.5)
        assert s.connect_ex(("127.0.0.1", port)) != 0


def test_run_app_reports_an_exiting_command_and_refuses_remote_urls(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    ctx = ToolContext(toolchain=python_toolchain())
    out = run_app(sb, RunAppInput(module="json.tool", args=["--help"], timeout_s=10), ctx)
    assert "exited 0" in out and "usage" in out.lower()
    with pytest.raises(ToolError, match="local"):
        run_app(sb, RunAppInput(module="http.server", health_url="http://example.com"), ctx)


def test_node_ts_environment_is_node_modules_created_by_the_first_install(tmp_path: Path) -> None:
    """M10: a stack without an interpreter of its own. Nothing is created up front, nothing is
    rewritten, no ``.pth``; the first ``npm install`` makes ``node_modules`` and the approved
    packages go in through the network-open call."""
    from scio.aci.runtime import env_interpreter, env_marker
    from scio.toolchain import node_ts_toolchain

    sb = RecordingSandbox(tmp_path)
    tc = node_ts_toolchain()
    assert env_interpreter(sb, tc) is None
    assert env_marker(sb, tc).endswith("node_modules/.package-lock.json")
    summary = ensure_product_env(sb, tc, ())
    assert summary.startswith("product environment not needed yet at node_modules")
    assert sb.calls == [] and sb.python is None
    summary = ensure_product_env(sb, tc, ("zod@^3", "pino"))
    assert summary.startswith("product environment created at node_modules, installed zod@^3, pino")
    assert "registry.npmjs.org" in summary
    argv, network = sb.calls[-1]
    assert argv[:2] == ["npm", "install"] and argv[-2:] == ["zod@^3", "pino"] and network is True
    assert len(sb.calls) == 1 and not list(tmp_path.rglob(HOST_PTH))
    ctx = ToolContext(toolchain=tc, approved=("@scope/Thing", "zod"))
    assert (
        ctx.is_approved("@scope/thing@^2")
        and ctx.is_approved("zod@3.1")
        and not ctx.is_approved("left-pad")
    )
    out = install_deps(sb, InstallDepsInput(packages=["zod@^3"]), ctx)
    assert out.startswith("installed zod@^3") and sb.calls[-1][0][:2] == ["npm", "install"]


def test_normalise_package_handles_npm_specs() -> None:
    assert normalise_package("zod@^3.22") == "zod"
    assert normalise_package("@types/node@22") == "@types/node"
    assert normalise_package("@Scope/Pkg") == "@scope/pkg"
    assert normalise_package("left-pad") == "left-pad"
