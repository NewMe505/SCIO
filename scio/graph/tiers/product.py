"""Product-mode diagrams: ``scio new`` (ADR 0010) and the ``scio build`` milestone loop
(ADR 0011), kept in sync with docs/ARCHITECTURE.md like the tier graphs."""

from __future__ import annotations

from typing import Any

from scio.graph.registry import register


@register("product_new")
def product_new_diagram() -> Any:
    from scio.product.graph import build_product_graph

    return build_product_graph(None).compile()


@register("product_build")
def product_build_diagram() -> Any:
    from scio.product.build import build_product_build_graph

    return build_product_build_graph(None).compile()
