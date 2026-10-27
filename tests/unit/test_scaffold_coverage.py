"""Every fresh walking skeleton meets the product coverage floor (M11, I-091): the floor
measures the product's own code in-process, so a scaffold whose tests only reach the code
through a child process would fail its first milestone at ``integrate``."""

from __future__ import annotations

from pathlib import Path

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.product.scaffold import scaffold_for
from scio.settings import Settings
from scio.state.models import AppSpec, ProductFeature, ProductSpec
from scio.toolchain import get_toolchain
from tests.node_support import requires_node

FLOOR = Settings.model_fields["coverage_floor"].default


def _spec(stack: str, kind: str) -> ProductSpec:
    return ProductSpec(
        name="shop_api",
        title="Shop API",
        vision="A small shop service.",
        users=["a developer"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["starts"])],
        quality_bar=["tests"],
        stack=stack,
        dependencies=[],
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


@pytest.mark.parametrize(
    ("stack", "kind"),
    [
        ("python", "cli"),
        ("python", "http"),
        pytest.param("node-ts", "cli", marks=requires_node),
        pytest.param("node-ts", "http", marks=requires_node),
    ],
)
def test_a_fresh_scaffold_meets_the_coverage_floor(tmp_path: Path, stack: str, kind: str) -> None:
    for rel, text in scaffold_for(_spec(stack, kind)).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    tc = get_toolchain(stack)
    coverage = next(c for c in tc.quality_checks() if c.name == "coverage")
    r = LocalSandbox(tmp_path).exec(coverage.command({"coverage_floor": FLOOR}), timeout_s=300)
    assert coverage.outcome(r.exit_code, r.output) == "passed", r.output[-2500:]
