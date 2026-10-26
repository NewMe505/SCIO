"""The product runtime (M9): a per-product environment, the install phase, ``install_deps``
and ``run_app``.

Two phases, as docs/PRODUCT_MODE.md component 4 describes: during **install** the network
is open to the profile's registry allowlist and only packages on the approved list (the
``## Approved dependencies`` of ``PRODUCT.md``) are installed; during **test** the network
is off again. The environment is a plain virtualenv under ``.scio/sandbox/venv`` in the
product repository (git-ignored); the sandbox rewrites ``python`` to its interpreter. On a
local sandbox a ``.pth`` file makes SCIO's own environment visible, so the test runner and
the coverage plugin need no download (not ``--system-site-packages``: that exposes the base
interpreter, whose packages differ per machine, I-074); any other sandbox installs the
profile's ``test_tools`` when the environment is created.

``install_deps`` is a worker tool gated by the approved list: a package that is not on it is
refused with a message that tells the worker to ask for a design decision, which is the
escalation path a human already owns (ADR 0007). ``run_app`` starts the product, waits for a
health URL or a few seconds of output, and stops it; local sandbox only, because the process
runs on the host.
"""

from __future__ import annotations

import re
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from scio.aci.sandbox.base import Sandbox
from scio.aci.sandbox.local import LocalSandbox
from scio.aci.tools import ToolError
from scio.toolchain import Toolchain, python_toolchain

VENV_DIR = ".scio/sandbox/venv"
HOST_PTH = "scio_host_env.pth"
"""The ``.pth`` file in a local product environment that appends SCIO's own site-packages."""
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def normalise_package(spec: str) -> str:
    """The canonical project name of a requirement spec (``Flask>=3`` -> ``flask``)."""
    m = _NAME_RE.match(spec.strip())
    name = m.group(0) if m else spec.strip()
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class ToolContext:
    """What context-aware tools need beyond the sandbox: the product's approved dependency
    list and the toolchain. ``approved`` is None in task mode (no PRODUCT.md), which makes
    ``install_deps`` refuse everything with a pointer to the owner."""

    toolchain: Toolchain = field(default_factory=python_toolchain)
    approved: tuple[str, ...] | None = None
    installed: list[str] = field(default_factory=list)
    """Package specs installed so far in this run (reported in the summary and the PR)."""

    def is_approved(self, spec: str) -> bool:
        return self.approved is not None and normalise_package(spec) in {
            normalise_package(a) for a in self.approved
        }


# ---------- the environment ----------


def venv_python(sandbox: Sandbox, toolchain: Toolchain, venv_dir: str = VENV_DIR) -> str:
    rt = toolchain.runtime()
    # The layout follows the sandbox's root, not the host: a Docker root (``/work``) holds a
    # posix venv even when SCIO runs on Windows (same rule as ``resolve_under``).
    windows_root = len(sandbox.root) > 1 and sandbox.root[1] == ":"
    rel = rt.env_interpreter_windows if windows_root else rt.env_interpreter_posix
    if rel is None:
        raise RuntimeError(f"the {toolchain.name} profile has no environment interpreter")
    return f"{sandbox.root}/{venv_dir}/{rel}"


def ensure_product_env(
    sandbox: Sandbox,
    toolchain: Toolchain,
    approved: tuple[str, ...] | list[str] = (),
    *,
    venv_dir: str = VENV_DIR,
) -> str:
    """Create the product environment once and install the approved dependencies into it
    (the install phase, network open). Idempotent: an existing environment is reused and
    pip skips what is already there. Returns a one-line summary for the ledger."""
    rt = toolchain.runtime()
    python = venv_python(sandbox, toolchain, venv_dir)
    created = False
    if not sandbox.exists(python):
        create = rt.env_create_argv or ()
        r = sandbox.exec([*create, f"{sandbox.root}/{venv_dir}"], timeout_s=300)
        if not r.ok:
            raise RuntimeError(f"could not create the product environment: {r.output[-500:]}")
        created = True
    sandbox.python = python
    installed = ""
    specs = [s for s in approved if s.strip()]
    if created:
        if isinstance(sandbox, LocalSandbox):
            link_host_env(sandbox)
        else:
            specs = [*rt.test_tools, *specs]
    if specs:
        r = sandbox.exec([*rt.install_argv, *specs], timeout_s=900, network=True)
        if not r.ok:
            raise RuntimeError(f"install phase failed for {specs}: {r.output[-800:]}")
        installed = f", installed {', '.join(specs)}"
    return (
        f"product environment {'created' if created else 'reused'} at {venv_dir}{installed} "
        f"(registry allowlist: {', '.join(rt.index_allowlist) or 'none'})"
    )


def link_host_env(sandbox: Sandbox) -> str:
    """Append SCIO's own site-packages to a freshly created local product environment through
    a ``.pth`` file, so the test runner and coverage plugin SCIO runs with are importable
    there without a download. The product's own installs come first on ``sys.path``, so a
    product dependency always wins over SCIO's copy. Returns the ``.pth`` path."""
    r = sandbox.exec(["python", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
    if not r.ok:
        raise RuntimeError(f"could not locate the product environment's site-packages: {r.output}")
    pth = f"{r.stdout.strip()}/{HOST_PTH}"
    sandbox.write_text(pth, sysconfig.get_paths()["purelib"] + "\n")
    return pth


# ---------- install_deps ----------


class InstallDepsInput(BaseModel):
    packages: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Requirement specs to install, e.g. ['flask>=3', 'rich']; only packages "
        "on the product's approved list are allowed",
    )


def install_deps(sandbox: Sandbox, inp: InstallDepsInput, ctx: ToolContext) -> str:
    """Install approved packages into the product environment (network open for this call).

    An unapproved package is refused: the worker must set ``needs_design_decision`` and name
    the package in ``design_question`` so the owner can add it to PRODUCT.md.
    """
    if ctx.approved is None:
        raise ToolError(
            "install_deps is not available in this run: there is no approved dependency list "
            "(PRODUCT.md). Ask for a design decision if the task needs a new dependency."
        )
    refused = [p for p in inp.packages if not ctx.is_approved(p)]
    if refused:
        raise ToolError(
            f"install refused: {', '.join(refused)} not on the approved dependency list "
            f"({', '.join(ctx.approved) or 'empty'}). Set needs_design_decision=true and name "
            "the package in design_question; the owner adds it to PRODUCT.md."
        )
    if sandbox.python is None:
        ensure_product_env(sandbox, ctx.toolchain, ())
    rt = ctx.toolchain.runtime()
    r = sandbox.exec([*rt.install_argv, *inp.packages], timeout_s=900, network=True)
    if not r.ok:
        raise ToolError(f"install failed (exit {r.exit_code}):\n{r.output[-1500:]}")
    ctx.installed.extend(p for p in inp.packages if p not in ctx.installed)
    return f"installed {', '.join(inp.packages)} into the product environment"


# ---------- run_app ----------


class RunAppInput(BaseModel):
    module: str = Field(
        description="Module to start with the toolchain's app command (python -m <module>)",
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
    )
    args: list[str] = Field(default_factory=list, max_length=20)
    health_url: str | None = Field(
        default=None,
        description="HTTP URL polled until it answers 2xx/3xx; omit for a command that exits",
    )
    timeout_s: int = Field(default=20, ge=1, le=120, description="Wait for health or exit")


def run_app(sandbox: Sandbox, inp: RunAppInput, ctx: ToolContext) -> str:
    """Start the app, wait for its health URL (or for it to exit), report, stop it."""
    if not isinstance(sandbox, LocalSandbox):
        raise ToolError("run_app needs the local sandbox in M9 (the app runs on the host)")
    rt = ctx.toolchain.runtime()
    argv = [*rt.app_argv, inp.module, *inp.args]
    if argv[0] == "python":
        argv[0] = sandbox.python or sys.executable
    if inp.health_url and not inp.health_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise ToolError("health_url must be a local http://127.0.0.1 or http://localhost URL")
    proc = subprocess.Popen(
        argv,
        cwd=sandbox.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + inp.timeout_s
    status = "timed out"
    try:
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                status = f"exited {code}"
                break
            if inp.health_url and healthy(inp.health_url):
                status = f"healthy: {inp.health_url}"
                break
            time.sleep(0.2)
        else:
            if inp.health_url is None:
                status = f"still running after {inp.timeout_s}s"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    output = (proc.stdout.read() if proc.stdout else "")[-2000:]
    line = f"run_app {' '.join(argv[1:])}: {status}"
    return f"{line}\n{output}".strip()


def healthy(url: str) -> bool:
    """True when a local HTTP URL answers 2xx or 3xx within a second."""
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            return bool(200 <= int(resp.status) < 400)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def product_env_dir(sandbox: Sandbox) -> Path:
    return Path(sandbox.root) / VENV_DIR
