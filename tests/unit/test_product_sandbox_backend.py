"""Which sandbox a product build runs in (M9, ADR 0013 decision 10): Docker when it is there
and the worker is SCIO's own loop, otherwise local with the reason spelled out."""

from __future__ import annotations

import pytest

from scio.graph import run_context
from scio.settings import Settings


def test_auto_picks_docker_only_when_the_image_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    monkeypatch.setattr(docker, "docker_product_ready", lambda image: None)
    backend, reason = run_context.product_sandbox_backend(
        Settings(sandbox_docker_image="scio-sandbox")
    )
    assert backend == "docker" and "scio-sandbox" in reason and "egress proxy" in reason

    monkeypatch.setattr(docker, "docker_product_ready", lambda image: "no Docker daemon reachable")
    backend, reason = run_context.product_sandbox_backend(Settings())
    assert backend == "local" and "no Docker daemon" in reason and "not enforced" in reason


def test_claude_code_worker_forces_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    monkeypatch.setattr(docker, "docker_product_ready", lambda image: None)
    backend, reason = run_context.product_sandbox_backend(Settings(worker_backend="claude_code"))
    assert backend == "local" and "claude_code" in reason


def test_explicit_choice_is_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    calls: list[str] = []
    monkeypatch.setattr(docker, "docker_product_ready", lambda image: calls.append(image))
    assert (
        run_context.product_sandbox_backend(Settings(product_sandbox_backend="local"))[0] == "local"
    )
    assert (
        run_context.product_sandbox_backend(Settings(product_sandbox_backend="docker"))[0]
        == "docker"
    )
    assert calls == []  # never probed
