"""A product's CI installs what the product declares (I-140, the graphcalc run's S6): the
0.9.0 python scaffold's CI ran ``pip install ruff pytest`` only, so the first feature that
imported an approved dependency failed the PR's CI at collection while every check SCIO ran
in its sandbox venv passed. The clean-venv proof of the current scaffold is in the ledger;
this pins its shape."""

from __future__ import annotations

import re
import tomllib

import pytest

from scio.product.scaffold import OLD_PYTHON_INSTALLS, PYTHON_INSTALL, scaffold_for
from scio.state.models import AppSpec, ProductFeature, ProductSpec

# `pip install` of the project itself: `.`, `-e .`, `.[extra]`
SELF_INSTALL = re.compile(r"\bpip install\b.*(?:^|\s)(?:-e\s+)?\.(?:\[[^\]]*\])?(?:\s|$)")


def _spec(kind: str) -> ProductSpec:
    return ProductSpec(
        name="shop_api",
        title="Shop API",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["starts"])],
        quality_bar=["q"],
        stack="python",
        dependencies=["sympy", "requests>=2"],
        app=AppSpec(kind="http", module="shop_api", args=["serve", "--port", "{port}"])
        if kind == "http"
        else AppSpec(),
    )


@pytest.mark.parametrize("kind", ["cli", "http"])
def test_a_python_products_ci_and_develop_block_install_the_project(kind: str) -> None:
    files = scaffold_for(_spec(kind))
    ci = files[".github/workflows/ci.yml"]
    installs = [ln for ln in ci.splitlines() if "pip install" in ln]
    assert installs == [f"      - run: {PYTHON_INSTALL}"]
    assert SELF_INSTALL.search(PYTHON_INSTALL)
    assert not any(SELF_INSTALL.search(old) for old in OLD_PYTHON_INSTALLS)  # what upgrade fixes
    develop = files["README.md"].split("## Develop", 1)[1]
    assert f"\n{PYTHON_INSTALL}\n" in develop
    # `pip install -e .` must not depend on setuptools guessing the packages: it refuses a
    # flat layout with a second top-level directory (`static/`, say)
    data = tomllib.loads(files["pyproject.toml"])
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    assert data["tool"]["setuptools"]["packages"]["find"]["include"] == [
        "shop_api",
        "shop_api.*",
    ]
    assert sorted(data["project"]["dependencies"]) == ["requests>=2", "sympy"]
    assert files["pyproject.toml"].rstrip().endswith('omit = ["*/__main__.py"]')  # upgrade's split
