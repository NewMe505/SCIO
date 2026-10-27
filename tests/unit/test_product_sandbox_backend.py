"""Which sandbox a product build runs in (M9, ADR 0013 decision 10): Docker when it is there
and the worker is SCIO's own loop, otherwise local with the reason spelled out.

Every ``Settings`` here names ``product_sandbox_backend`` explicitly: CI's ``checks`` job sets
``SCIO_PRODUCT_SANDBOX_BACKEND=local`` in the environment, and a bare ``Settings()`` read it
(I-080)."""

from __future__ import annotations

import pytest

from scio.graph import run_context
from scio.settings import Settings


def test_auto_picks_docker_only_when_the_image_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    monkeypatch.setattr(docker, "docker_product_ready", lambda image, probe=(): None)
    backend, reason = run_context.product_sandbox_backend(
        Settings(product_sandbox_backend="auto", sandbox_docker_image="scio-sandbox")
    )
    assert backend == "docker" and "scio-sandbox" in reason and "egress proxy" in reason

    monkeypatch.setattr(
        docker, "docker_product_ready", lambda image, probe=(): "no Docker daemon reachable"
    )
    backend, reason = run_context.product_sandbox_backend(Settings(product_sandbox_backend="auto"))
    assert backend == "local" and "no Docker daemon" in reason and "not enforced" in reason


def test_claude_code_worker_forces_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    monkeypatch.setattr(docker, "docker_product_ready", lambda image, probe=(): None)
    backend, reason = run_context.product_sandbox_backend(
        Settings(product_sandbox_backend="auto", worker_backend="claude_code")
    )
    assert backend == "local" and "claude_code" in reason and "not enforced" in reason


def test_explicit_choice_is_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    calls: list[str] = []
    monkeypatch.setattr(docker, "docker_product_ready", lambda image, probe=(): calls.append(image))
    backend, reason = run_context.product_sandbox_backend(Settings(product_sandbox_backend="local"))
    assert backend == "local" and "not enforced" in reason  # an explicit local still says so
    assert (
        run_context.product_sandbox_backend(Settings(product_sandbox_backend="docker"))[0]
        == "docker"
    )
    assert calls == []  # never probed


def test_auto_probes_the_image_for_the_product_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """I-101: a node-ts product picks Docker only when the image runs Node with type
    stripping; the stack's probe is what the readiness check runs."""
    import scio.aci.sandbox.docker as docker
    from scio.toolchain import node_ts_toolchain

    seen: list[tuple[str, tuple[str, ...]]] = []

    def ready(image: str, probe: tuple[str, ...] = ()) -> str | None:
        seen.append((image, tuple(probe)))
        return None if probe and probe[0] == "python" else "image cannot run this stack"

    monkeypatch.setattr(docker, "docker_product_ready", ready)
    settings = Settings(product_sandbox_backend="auto", sandbox_docker_image="scio-sandbox")
    backend, reason = run_context.product_sandbox_backend(settings, node_ts_toolchain())
    assert backend == "local" and "cannot run this stack" in reason
    assert seen[-1][1][0] == "node" and "typescript" in seen[-1][1][-1]
    from scio.toolchain import python_toolchain

    assert run_context.product_sandbox_backend(settings, python_toolchain())[0] == "docker"


def test_the_readiness_check_runs_the_probe_inside_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import scio.aci.sandbox.docker as docker

    calls: list[list[str]] = []

    def run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        failing = argv[-1] == "probe-fails"
        return subprocess.CompletedProcess(argv, 1 if failing else 0, b"", b"")

    monkeypatch.setattr(docker, "docker_ready", lambda: True)
    monkeypatch.setattr(docker.subprocess, "run", run)
    assert docker.docker_product_ready("img", ("node", "-e", "ok")) is None
    assert calls[-1] == ["docker", "run", "--rm", "--network", "none", "img", "node", "-e", "ok"]
    why = docker.docker_product_ready("img", ("node", "probe-fails"))
    assert why is not None and "node probe failed" in why
    assert docker.docker_product_ready("img") is None  # no probe: git only, as before


@pytest.mark.parametrize("stack", ["python", "node-ts"])
def test_each_profile_probe_passes_on_a_machine_that_has_the_stack(stack: str) -> None:
    """The probes are real commands: they pass where the stack is installed (here; in CI,
    which installs Node 22), so a typo in the yaml cannot hide until a Docker run."""
    import subprocess
    import sys

    from scio.toolchain import get_toolchain
    from tests.node_support import HAS_NODE

    if stack == "node-ts" and not HAS_NODE:
        pytest.skip("needs Node >= 22.18 with type stripping")
    probe = list(get_toolchain(stack).sandbox_probe_argv)
    assert probe, stack
    if probe[0] == "python":
        probe[0] = sys.executable
    assert subprocess.run(probe, capture_output=True, timeout=60).returncode == 0, probe
