"""Live Docker sandbox checks (ledger I-016; M9, ADR 0013 decision 10).

Runs wherever a Docker daemon is reachable (GitHub's ubuntu runners included) and skips
elsewhere. No credentials or env flag needed. ``SCIO_SANDBOX_DOCKER_IMAGE`` names the image
(CI builds ``docker/sandbox.Dockerfile`` as ``scio-sandbox:ci``); the default Python image
serves the tests that need no git. The install-phase tests reach pypi.org through the egress
proxy, so they need the runner's own network; everything else is offline by construction.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from scio.aci.runtime import RunAppInput, ToolContext, ensure_product_env, run_app, venv_python
from scio.aci.sandbox.base import Limits
from scio.aci.sandbox.docker import DockerSandbox, docker_ready
from scio.product.acceptance import start_app, wait_healthy
from scio.toolchain import python_toolchain

IMAGE = os.environ.get("SCIO_SANDBOX_DOCKER_IMAGE", "python:3.12-slim")
ALLOW = ("pypi.org", "files.pythonhosted.org")

pytestmark = [pytest.mark.live, pytest.mark.skipif(not docker_ready(), reason="no Docker daemon")]


@pytest.fixture(scope="module")
def image() -> str:
    have = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60)
    if have.returncode != 0:
        subprocess.run(
            ["docker", "pull", "-q", IMAGE], check=True, capture_output=True, timeout=600
        )
    return IMAGE


@pytest.fixture
def sandbox(tmp_path: Path, image: str) -> Iterator[DockerSandbox]:
    sb = DockerSandbox(
        tmp_path,
        image=image,
        limits=Limits(timeout_s=120, memory_mb=1024, cpus=1.0, network_allowlist=ALLOW),
        git_identity=("SCIO CI", "ci@scio.local"),
    )
    yield sb
    sb.close()
    alive = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={sb.name}"], capture_output=True, text=True
    )
    assert alive.stdout.strip() == "", "close() must remove the container and the proxy"


def test_exec_and_files_through_the_mount(tmp_path: Path, sandbox: DockerSandbox) -> None:
    sandbox.write_text("/work/hello.py", "print('hi from', open('/work/data.txt').read())\n")
    sandbox.write_text("/work/data.txt", "the mount")
    r = sandbox.exec(["python", "/work/hello.py"])
    assert r.ok and r.stdout.strip() == "hi from the mount"
    r = sandbox.exec(["sh", "-c", "echo out > /work/made-inside.txt && echo $SCIO_SANDBOX"])
    assert r.ok and r.stdout.strip() == "docker"
    assert (tmp_path / "made-inside.txt").read_text() == "out\n"
    assert sandbox.read_text("/work/made-inside.txt") == "out\n"


def test_one_container_persists_across_execs(sandbox: DockerSandbox) -> None:
    assert sandbox.exec(["sh", "-c", "echo kept > /tmp/state"]).ok  # outside the mount
    r = sandbox.exec(["cat", "/tmp/state"])
    assert r.ok and r.stdout.strip() == "kept"
    running = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{sandbox.name}$"], capture_output=True, text=True
    )
    assert len(running.stdout.split()) == 1


def test_network_is_off_by_construction(sandbox: DockerSandbox) -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('example.com', 80), timeout=5)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
    )
    r = sandbox.exec(["python", "-c", code])
    assert r.ok and r.stdout.startswith("BLOCKED"), r.stdout


def test_install_phase_reaches_the_registry_only_through_the_proxy(sandbox: DockerSandbox) -> None:
    probe = (
        "import sys, urllib.request, urllib.error\n"
        "url = sys.argv[1]\n"
        "try:\n"
        "    with urllib.request.urlopen(url, timeout=20) as r:\n"
        "        print('OK', r.status)\n"
        "except Exception as e:\n"
        "    print('FAIL', type(e).__name__, e)\n"
    )
    sandbox.write_text("/work/probe.py", probe)
    allowed = sandbox.exec(["python", "/work/probe.py", "https://pypi.org/simple/"], network=True)
    assert allowed.ok and allowed.stdout.startswith("OK"), allowed.output
    denied = sandbox.exec(["python", "/work/probe.py", "https://example.com/"], network=True)
    # a refused CONNECT reaches urllib as URLError("Tunnel connection failed: 403 Forbidden")
    assert denied.ok and denied.stdout.startswith("FAIL URLError") and "403" in denied.stdout, (
        denied.output
    )
    # the proxy is gone again once the call ends: the next command is offline
    after = sandbox.exec(["python", "/work/probe.py", "https://pypi.org/simple/"])
    assert after.ok and after.stdout.startswith("FAIL"), after.output


def test_product_environment_lives_inside_the_container(sandbox: DockerSandbox) -> None:
    tc = python_toolchain()
    summary = ensure_product_env(sandbox, tc, ("pyyaml",))
    assert "created" in summary and "pyyaml" in summary
    assert sandbox.python == venv_python(sandbox, tc) == "/work/.scio/sandbox/venv/bin/python"
    r = sandbox.exec(["python", "-c", "import pytest, pytest_cov, yaml, sys; print(sys.prefix)"])
    assert r.ok and r.stdout.strip() == "/work/.scio/sandbox/venv", r.output
    assert "reused" in ensure_product_env(sandbox, tc, ("pyyaml",))
    assert sandbox.exec(["python", "-m", "pytest", "--version"]).ok


def test_timeout_inside_the_container_keeps_it_usable(sandbox: DockerSandbox) -> None:
    r = sandbox.exec(["sleep", "30"], timeout_s=2)
    assert r.timed_out and r.exit_code == 124
    assert sandbox.exec(["echo", "still here"]).stdout.strip() == "still here"


def test_non_zero_exit_is_reported(sandbox: DockerSandbox) -> None:
    r = sandbox.exec(["python", "-c", "import sys; sys.exit(3)"])
    assert r.exit_code == 3 and not r.ok


def test_http_app_starts_probes_and_stops_inside_the_container(sandbox: DockerSandbox) -> None:
    sandbox.write_text("/work/index.html", "hello")
    app = start_app(
        sandbox,
        ["python", "-m", "http.server", "8765", "--bind", "127.0.0.1"],
        "http://127.0.0.1:8765",
    )
    assert wait_healthy(app, "/index.html", timeout_s=30)
    inside = sandbox.exec(
        [
            "python",
            "-c",
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/index.html').read().decode())",
        ]
    )
    assert inside.ok and inside.stdout.strip() == "hello"
    log = app.stop()
    assert "GET /index.html" in log
    assert not wait_healthy(app, "/index.html", timeout_s=3)


def test_run_app_starts_probes_and_stops_inside_the_container(sandbox: DockerSandbox) -> None:
    """Issue #58: the worker's ``run_app`` tool on a Docker sandbox (it refused one before):
    healthy from inside, stopped afterwards, and an app that exits reports its exit code."""
    tc = python_toolchain()
    ctx = ToolContext(toolchain=tc)
    sandbox.write_text("/work/index.html", "hello")
    url = "http://127.0.0.1:8766/index.html"
    served = run_app(
        sandbox,
        RunAppInput(
            module="http.server", args=["8766", "--bind", "127.0.0.1"], health_url=url, timeout_s=30
        ),
        ctx,
    )
    assert served.splitlines()[0] == f"run_app -m http.server 8766 --bind 127.0.0.1: healthy: {url}"
    assert "GET /index.html" in served, served
    assert not sandbox.exec(tc.health_probe_argv(url), timeout_s=10).ok  # stopped
    exited = run_app(sandbox, RunAppInput(module="json.tool", args=["--help"], timeout_s=20), ctx)
    assert exited.splitlines()[0] == "run_app -m json.tool --help: exited 0", exited
    assert "usage" in exited.lower()
    assert not sandbox.exists("/work/.scio/sandbox/app.pid")


@pytest.mark.skipif(
    subprocess.run(["docker", "run", "--rm", "--network", "none", IMAGE, "git", "--version"],
                   capture_output=True).returncode != 0 if docker_ready() else True,
    reason="image has no git (build docker/sandbox.Dockerfile)",
)  # fmt: skip
def test_git_works_on_the_mount(tmp_path: Path, sandbox: DockerSandbox) -> None:
    assert sandbox.exec(["git", "init", "-q", "/work"]).ok
    sandbox.write_text("/work/a.txt", "a\n")
    r = sandbox.exec(["sh", "-c", "git add -A && git commit -q -m init && git log --format=%an -1"])
    assert r.ok and r.stdout.strip() == "SCIO CI", r.output
    assert (tmp_path / ".git").is_dir()


def _scaffold(root: Path, stack: str, kind: str) -> None:
    from scio.product.scaffold import scaffold_for
    from scio.state.models import AppSpec, ProductFeature, ProductSpec

    spec = ProductSpec(
        name="shop_api",
        title="Shop API",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["starts"])],
        quality_bar=["q"],
        stack=stack,
        app=(
            AppSpec(
                kind="http",
                module="shop_api" if stack == "python" else None,
                args=["serve", "--port", "{port}"],
            )
            if kind == "http"
            else AppSpec()
        ),
    )
    for rel, text in scaffold_for(spec).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.parametrize("stack", ["python", "node-ts"])
@pytest.mark.parametrize("kind", ["cli", "http"])
def test_the_scaffold_dockerfile_builds_and_runs(tmp_path: Path, stack: str, kind: str) -> None:
    """M11: the release image every scaffold ships builds and runs; an http product answers
    its health path through a published port (``HOST=0.0.0.0`` inside the container). Written
    without a local daemon (I-016): the first docker-live run is the experiment."""
    import json
    import socket
    import time
    import urllib.request

    _scaffold(tmp_path, stack, kind)
    tag = f"scio-scaffold-{stack}-{kind}:ci"
    built = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    try:
        if kind == "cli":
            r = subprocess.run(
                ["docker", "run", "--rm", tag], capture_output=True, text=True, timeout=120
            )
            assert r.returncode == 0 and r.stdout.strip() == "0.1.0", r.stdout + r.stderr
            return
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        run = subprocess.run(
            ["docker", "run", "-d", "-p", f"127.0.0.1:{port}:8000", tag],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert run.returncode == 0, run.stderr
        cid = run.stdout.strip()
        try:
            body: dict[str, str] = {}
            health = f"http://127.0.0.1:{port}/health"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not body:
                try:
                    with urllib.request.urlopen(health, timeout=2) as res:
                        body = json.load(res)
                except OSError:
                    time.sleep(0.5)
            logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True)
            assert body == {"status": "ok", "version": "0.1.0"}, logs.stdout + logs.stderr
        finally:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60)
    finally:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, timeout=120)


def test_a_node_product_installs_and_tests_inside_the_container(tmp_path: Path, image: str) -> None:
    """I-101 (issue #58): the sandbox image runs a node-ts product. A real ``npm install``
    reaches the registry only through the allowlist proxy, then ``node --test`` runs a
    TypeScript test (type stripping) that imports the installed package. Written without a
    local daemon: the first docker-live run is the experiment."""
    from scio.toolchain import node_ts_toolchain

    tc = node_ts_toolchain()
    sb = DockerSandbox(
        tmp_path,
        image=image,
        limits=Limits(
            timeout_s=300,
            memory_mb=1024,
            cpus=1.0,
            network_allowlist=tc.runtime().index_allowlist,
        ),
        git_identity=("SCIO CI", "ci@scio.local"),
    )
    try:
        if not sb.exec(list(tc.sandbox_probe_argv)).ok:
            # the default python image has no Node; a named image (CI's docker-live job builds
            # docker/sandbox.Dockerfile) must pass, so this never skips where it matters (I-105)
            if "SCIO_SANDBOX_DOCKER_IMAGE" not in os.environ:
                pytest.skip(f"{image} has no Node (build docker/sandbox.Dockerfile)")
            raise AssertionError(f"{image} fails the node-ts probe")
        sb.write_text(
            "/work/package.json",
            '{"name": "p", "version": "0.1.0", "private": true, "type": "module"}\n',
        )
        sb.write_text(
            "/work/tests/num.test.ts",
            'import { test } from "node:test";\n'
            'import assert from "node:assert/strict";\n'
            'import isNumber from "is-number";\n\n'
            'test("the installed package loads", () => {\n'
            "  const n: number = 3;\n"
            "  assert.equal(isNumber(n), true);\n"
            "});\n",
        )
        summary = ensure_product_env(sb, tc, ["is-number@7.0.0"])
        assert "installed is-number@7.0.0" in summary, summary
        assert sb.exists("/work/node_modules/is-number/package.json")
        r = sb.exec(tc.test_argv(filter=None, path=None), timeout_s=120)
        assert tc.test_outcome(r.exit_code, r.stdout) == "passed", r.output
        assert "# pass 1" in r.stdout
    finally:
        sb.close()
