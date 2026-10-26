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

    monkeypatch.setattr(docker, "docker_product_ready", lambda image: None)
    backend, reason = run_context.product_sandbox_backend(
        Settings(product_sandbox_backend="auto", sandbox_docker_image="scio-sandbox")
    )
    assert backend == "docker" and "scio-sandbox" in reason and "egress proxy" in reason

    monkeypatch.setattr(docker, "docker_product_ready", lambda image: "no Docker daemon reachable")
    backend, reason = run_context.product_sandbox_backend(Settings(product_sandbox_backend="auto"))
    assert backend == "local" and "no Docker daemon" in reason and "not enforced" in reason


def test_claude_code_worker_forces_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    monkeypatch.setattr(docker, "docker_product_ready", lambda image: None)
    backend, reason = run_context.product_sandbox_backend(
        Settings(product_sandbox_backend="auto", worker_backend="claude_code")
    )
    assert backend == "local" and "claude_code" in reason and "not enforced" in reason


def test_explicit_choice_is_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    import scio.aci.sandbox.docker as docker

    calls: list[str] = []
    monkeypatch.setattr(docker, "docker_product_ready", lambda image: calls.append(image))
    backend, reason = run_context.product_sandbox_backend(Settings(product_sandbox_backend="local"))
    assert backend == "local" and "not enforced" in reason  # an explicit local still says so
    assert (
        run_context.product_sandbox_backend(Settings(product_sandbox_backend="docker"))[0]
        == "docker"
    )
    assert calls == []  # never probed
