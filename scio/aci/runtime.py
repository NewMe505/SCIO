"""The product runtime (M9, generalised across stacks in M10): a per-product environment,
the install phase, ``install_deps`` and ``run_app``.

Two phases, as docs/PRODUCT_MODE.md component 4 describes: during **install** the network
is open to the profile's registry allowlist and only packages on the approved list (the
``## Approved dependencies`` of ``PRODUCT.md``) are installed; during **test** the network
is off again. What the environment *is* comes from the profile's ``Runtime``: a Python
product has a virtualenv under ``.scio/sandbox/venv`` with its own interpreter (the sandbox
rewrites ``python`` to it; on a local sandbox a ``.pth`` file makes SCIO's own environment
visible so the test runner needs no download, I-074; any other sandbox installs the
profile's ``test_tools``); a Node product has ``node_modules`` that the first install
creates and no interpreter of its own.

``install_deps`` is a worker tool gated by the approved list: a package that is not on it is
refused with a message that tells the worker to ask for a design decision, which is the
escalation path a human already owns (ADR 0007). ``run_app`` starts the product, waits for a
health URL or a few seconds of output, and stops it; local sandbox only, because the process
runs on the host.
"""

from __future__ import annotations

import re
import subprocess
import sysconfig
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from scio.aci.sandbox.base import Sandbox
from scio.aci.sandbox.local import LocalSandbox, resolve_command
from scio.aci.tools import ToolError
from scio.toolchain import Toolchain, python_toolchain

HOST_PTH = "scio_host_env.pth"
"""The ``.pth`` file in a local Python product environment that appends SCIO's own
site-packages."""
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*")


def normalise_package(spec: str) -> str:
    """The canonical project name of a requirement spec: ``Flask>=3`` -> ``flask``,
    ``typing_extensions`` -> ``typing-extensions`` (PEP 503), ``@scope/Name@^1`` ->
    ``@scope/name`` (npm; scoped names keep their shape). A non-registry spec (``file:``,
    ``github:``, a URL or a path) is its own name, so it only ever matches itself (I-088)."""
    s = spec.strip()
    if ":" in s or s.startswith((".", "/")):
        return s
    if s.startswith("@"):  # npm scoped package: @scope/name[@range]
        return ("@" + s[1:].split("@", 1)[0]).lower()
    m = _NAME_RE.match(s)
    name = (m.group(0) if m else s).lower()
    return name if "/" in name else re.sub(r"[-_.]+", "-", name)


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


def _windows_root(sandbox: Sandbox) -> bool:
    # The layout follows the sandbox's root, not the host: a Docker root (``/work``) holds a
    # posix environment even when SCIO runs on Windows (same rule as ``resolve_under``).
    return len(sandbox.root) > 1 and sandbox.root[1] == ":"


def env_marker(sandbox: Sandbox, toolchain: Toolchain) -> str:
    """The file whose presence means the product environment exists (absolute)."""
    rt = toolchain.runtime()
    rel = rt.env_marker_windows if _windows_root(sandbox) else rt.env_marker_posix
    return f"{sandbox.root}/{rel}"


def env_interpreter(sandbox: Sandbox, toolchain: Toolchain) -> str | None:
    """The environment's own interpreter (absolute), or None for a stack that runs with
    the host runtime (Node)."""
    rt = toolchain.runtime()
    rel = rt.env_interpreter_windows if _windows_root(sandbox) else rt.env_interpreter_posix
    return None if rel is None else f"{sandbox.root}/{rt.env_dir}/{rel}"


def venv_python(sandbox: Sandbox, toolchain: Toolchain) -> str:
    """``env_interpreter`` for a stack that has one (the pre-M10 name)."""
    path = env_interpreter(sandbox, toolchain)
    if path is None:
        raise RuntimeError(f"the {toolchain.name} profile has no environment interpreter")
    return path


def ensure_product_env(
    sandbox: Sandbox,
    toolchain: Toolchain,
    approved: tuple[str, ...] | list[str] = (),
) -> str:
    """Create the product environment once and install the approved dependencies into it
    (the install phase, network open). Idempotent: an existing environment is reused and
    the installer skips what is already there. Returns a one-line summary for the ledger."""
    rt = toolchain.runtime()
    fresh = not sandbox.exists(env_marker(sandbox, toolchain))
    created = False
    if fresh and rt.env_create_argv:
        r = sandbox.exec([*rt.env_create_argv, f"{sandbox.root}/{rt.env_dir}"], timeout_s=300)
        if not r.ok:
            raise RuntimeError(f"could not create the product environment: {r.output[-500:]}")
        created = True
    interpreter = env_interpreter(sandbox, toolchain)
    if interpreter is not None:
        sandbox.python = interpreter
    specs = [s for s in approved if s.strip()]
    local = isinstance(sandbox, LocalSandbox)
    if fresh:
        if local and created and interpreter is not None:
            link_host_env(sandbox)
        elif not local:
            specs = [*rt.test_tools, *specs]
    installed = ""
    if specs:
        r = sandbox.exec([*rt.install_argv, *specs], timeout_s=900, network=True)
        if not r.ok:
            raise RuntimeError(f"install phase failed for {specs}: {r.output[-800:]}")
        installed = f", installed {', '.join(specs)}"
        created = created or fresh  # the first install created it (``npm install``)
    state = "created" if created else ("reused" if not fresh else "not needed yet")
    return (
        f"product environment {state} at {rt.env_dir}{installed} "
        f"(registry allowlist: {', '.join(rt.index_allowlist) or 'none'})"
    )


def link_host_env(sandbox: Sandbox) -> str:
    """Append SCIO's own site-packages to a freshly created local Python product environment
    through a ``.pth`` file, so the test runner and coverage plugin SCIO runs with are
    importable there without a download. The product's own installs come first on
    ``sys.path``, so a product dependency always wins over SCIO's copy. Returns the path."""
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
        description="Requirement specs to install, e.g. ['flask>=3', 'rich'] or "
        "['zod@^3']; only packages on the product's approved list are allowed",
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
    if sandbox.python is None and env_interpreter(sandbox, ctx.toolchain) is not None:
        # A stack with its own interpreter needs the environment before the first install;
        # for one without (Node) the install itself creates it.
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
        description="What the profile's app command starts: a module name (python -m "
        "<module>) or an entry file (node src/main.ts)",
        pattern=r"^[A-Za-z_][A-Za-z0-9_./-]*$",
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
    argv = resolve_command([*rt.app_argv, inp.module, *inp.args], sandbox.python)
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
    shown = [*rt.app_argv[1:], inp.module, *inp.args]
    line = f"run_app {' '.join(shown)}: {status}"
    return f"{line}\n{output}".strip()


def healthy(url: str) -> bool:
    """True when a local HTTP URL answers 2xx or 3xx within a second."""
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            return bool(200 <= int(resp.status) < 400)
    except (urllib.error.URLError, OSError, ValueError):
        return False


__all__ = [
    "HOST_PTH",
    "InstallDepsInput",
    "RunAppInput",
    "ToolContext",
    "ensure_product_env",
    "env_interpreter",
    "env_marker",
    "healthy",
    "install_deps",
    "link_host_env",
    "normalise_package",
    "run_app",
    "venv_python",
]
