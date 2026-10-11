"""Live Docker sandbox checks (ledger I-016).

Runs wherever a Docker daemon is reachable (GitHub's ubuntu runners included) and skips
elsewhere. No credentials or env flag needed: the container is local and offline.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scio.aci.sandbox.base import Limits
from scio.aci.sandbox.docker import DockerSandbox

IMAGE = "python:3.12-slim"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [pytest.mark.live, pytest.mark.skipif(not _docker_ready(), reason="no Docker daemon")]


@pytest.fixture(scope="module")
def image() -> str:
    subprocess.run(["docker", "pull", "-q", IMAGE], check=True, capture_output=True, timeout=600)
    return IMAGE


def test_exec_and_files_through_the_mount(tmp_path: Path, image: str) -> None:
    sb = DockerSandbox(tmp_path, image=image, limits=Limits(timeout_s=60, memory_mb=256, cpus=0.5))
    sb.write_text("/work/hello.py", "print('hi from', open('/work/data.txt').read())\n")
    sb.write_text("/work/data.txt", "the mount")
    r = sb.exec(["python", "/work/hello.py"])
    assert r.ok and r.stdout.strip() == "hi from the mount"
    r = sb.exec(["sh", "-c", "echo out > /work/made-inside.txt && echo $SCIO_SANDBOX"])
    assert r.ok and r.stdout.strip() == "docker"
    assert (tmp_path / "made-inside.txt").read_text() == "out\n"
    assert sb.read_text("/work/made-inside.txt") == "out\n"


def test_network_is_off_by_default(tmp_path: Path, image: str) -> None:
    sb = DockerSandbox(tmp_path, image=image, limits=Limits(timeout_s=60))
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('example.com', 80), timeout=5)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
    )
    r = sb.exec(["python", "-c", code])
    assert r.ok and r.stdout.startswith("BLOCKED"), r.stdout


def test_timeout_kills_the_container(tmp_path: Path, image: str) -> None:
    sb = DockerSandbox(tmp_path, image=image, limits=Limits(timeout_s=60))
    r = sb.exec(["sleep", "30"], timeout_s=2)
    assert r.timed_out and r.exit_code == 124
    alive = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=scio-"], capture_output=True, text=True
    )
    assert alive.stdout.strip() == ""


def test_non_zero_exit_is_reported(tmp_path: Path, image: str) -> None:
    sb = DockerSandbox(tmp_path, image=image)
    r = sb.exec(["python", "-c", "import sys; sys.exit(3)"])
    assert r.exit_code == 3 and not r.ok
