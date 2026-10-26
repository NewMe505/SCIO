"""M9 acceptance runner: black-box tests against the started product (ADR 0013)."""

from __future__ import annotations

from pathlib import Path

from scio.aci.sandbox.local import LocalSandbox
from scio.graph.run_context import RunContext
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.product.acceptance import run_acceptance
from scio.product.progress import parse_product_md
from scio.product.scaffold import python_scaffold
from scio.product.spec import render_product_md
from scio.settings import Settings
from scio.state.models import AppSpec, ProductFeature, ProductSpec


def _spec(app: AppSpec) -> ProductSpec:
    return ProductSpec(
        name="demo_app",
        title="Demo",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["runs"])],
        quality_bar=["q"],
        app=app,
    )


def _ctx(tmp_path: Path) -> RunContext:
    settings = Settings(provider_override="fake")
    config = ModelsConfig.load(settings.models_path)
    harness = Harness(config, ProviderRegistry(config, settings), settings=settings)
    return RunContext.create(
        thread_id="acc",
        sandbox_root=tmp_path / "product",
        settings=settings,
        harness=harness,
        sandbox=LocalSandbox(tmp_path / "product"),
        work_dir=tmp_path / ".scio",
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def test_cli_product_acceptance_runs_the_black_box_tests(tmp_path: Path) -> None:
    spec = _spec(AppSpec())
    ctx = _ctx(tmp_path)
    try:
        _write(Path(ctx.sandbox.root), python_scaffold(spec))
        text, ok = run_acceptance(ctx, spec)
    finally:
        ctx.close()
    assert ok and text.startswith("[acceptance] passed (cli)"), text


def test_http_product_is_started_probed_tested_and_stopped(tmp_path: Path) -> None:
    spec = _spec(AppSpec(kind="http", args=["serve", "--port", "{port}"]))
    ctx = _ctx(tmp_path)
    try:
        root = Path(ctx.sandbox.root)
        _write(root, python_scaffold(spec))
        text, ok = run_acceptance(ctx, spec)
        assert ok and text.startswith("[acceptance] passed (http http://127.0.0.1:"), text
        # the same tests skip when nobody started the app (the plain suite stays green)
        r = ctx.sandbox.exec(ctx.toolchain.test_argv(filter=None, path=f"{root}/tests"))
        assert r.ok and "skipped" in r.stdout and "passed" in r.stdout, r.output
        # a server that never answers its health path fails acceptance, with its output
        broken = spec.model_copy(
            update={"app": spec.app.model_copy(update={"health_path": "/nope"})}
        )
        text, ok = run_acceptance(ctx, broken)
        assert not ok and "did not answer /nope" in text
    finally:
        ctx.close()


def test_acceptance_is_skipped_without_the_directory(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        text, ok = run_acceptance(ctx, _spec(AppSpec()))
    finally:
        ctx.close()
    assert ok and text.startswith("[acceptance] skipped")


def test_runtime_section_round_trips_and_normalises() -> None:
    from scio.product.spec import normalise_spec
    from scio.state.models import ProductSpecOutput

    spec = _spec(
        AppSpec(
            kind="http",
            module="demo_app.server",
            args=["serve", "--port", "{port}"],
            health_path="/healthz",
        )
    )
    md = render_product_md(spec)
    assert "## Runtime" in md and "- kind: http" in md and "- health: /healthz" in md
    assert parse_product_md(md) == spec
    out = ProductSpecOutput(
        product_name="demo_app",
        title="Demo",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["runs"])],
        quality_bar=["q"],
        stack="python",
        app=AppSpec(kind="http", module="demo_app", args=["serve"], health_path="health"),
    )
    normalised, notes = normalise_spec(out)
    assert normalised.app.module is None  # the package itself
    assert normalised.app.health_path == "/healthz" or normalised.app.health_path == "/health"
    assert normalised.app.args == ["serve", "--port", "{port}"]
    assert any("{port}" in n for n in notes) and any("absolute" in n for n in notes)
