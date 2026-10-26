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

from scio.aci.runtime import ensure_product_env, venv_python
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
        "except urllib.error.HTTPError as e:\n"
        "    print('HTTP', e.code)\n"
        "except Exception as e:\n"
        "    print('FAIL', type(e).__name__)\n"
    )
    sandbox.write_text("/work/probe.py", probe)
    allowed = sandbox.exec(["python", "/work/probe.py", "https://pypi.org/simple/"], network=True)
    assert allowed.ok and allowed.stdout.startswith("OK"), allowed.output
    denied = sandbox.exec(["python", "/work/probe.py", "https://example.com/"], network=True)
    assert denied.ok and denied.stdout.startswith("HTTP 403"), denied.output
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
