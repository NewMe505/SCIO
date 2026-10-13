"""The product spec: normalise the planner's draft, render ``PRODUCT.md`` (ADR 0010).

Every structured output that drives control flow gets a normaliser that enforces the
invariants the graph assumes (lessons I-039, I-046): the slug is a valid package name, the
stack is a known toolchain profile, every feature has an id and at least one acceptance
criterion, and the first feature is the walking skeleton.
"""

from __future__ import annotations

import re

from scio.state.models import AppSpec, ProductFeature, ProductSpec, ProductSpecOutput
from scio.toolchain import get_toolchain

_SLUG_RE = re.compile(r"[^a-z0-9_]+")
SKELETON_WORDS = ("skeleton", "health", "--version", "smoke", "starts")


def slugify(text: str) -> str:
    """A package slug: lowercase, letters digits underscores, starts with a letter, <= 40."""
    s = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s or not s[0].isalpha():
        s = f"app_{s}" if s else "app"
    return s[:40].rstrip("_") or "app"


def normalise_spec(out: ProductSpecOutput) -> tuple[ProductSpec, list[str]]:
    """Return the spec the graph will build, plus the notes explaining every change made."""
    notes: list[str] = []
    name = slugify(out.product_name)
    if name != out.product_name:
        notes.append(f"product_name {out.product_name!r} normalised to {name!r}")
    stack = out.stack.strip().lower() or "python"
    try:
        get_toolchain(stack)
    except KeyError:
        notes.append(f"stack {out.stack!r} has no toolchain profile; using python")
        stack = "python"
    features: list[ProductFeature] = []
    for i, f in enumerate(out.features, 1):
        fid = f"P{i}"
        if f.id != fid:
            notes.append(f"feature {f.id!r} renumbered to {fid}")
        acceptance = [a.strip() for a in f.acceptance if a.strip()]
        if not acceptance:
            acceptance = [f"{f.description.strip()} (criterion to be refined before build)"]
            notes.append(f"{fid} had no acceptance criterion; a placeholder was added")
        features.append(
            ProductFeature(id=fid, description=f.description.strip(), acceptance=acceptance)
        )
    first = features[0]
    if not any(
        w in (first.description + " ".join(first.acceptance)).lower() for w in SKELETON_WORDS
    ):
        skeleton = ProductFeature(
            id="P1",
            description="Walking skeleton: the package installs, the CLI answers --version, "
            "and one smoke test passes",
            acceptance=[
                f"`python -m {name} --version` prints the version and exits 0",
                "tests/test_smoke.py passes under the profile's test command",
            ],
        )
        features = [skeleton] + [
            f.model_copy(update={"id": f"P{i}"}) for i, f in enumerate(features, 2)
        ]
        notes.append("walking skeleton inserted as P1; later features renumbered")
    app = out.app or AppSpec()
    if app.module is not None and app.module.strip() in ("", name):
        app = app.model_copy(update={"module": None})  # None means the package itself
    if app.kind == "http" and not app.health_path.startswith("/"):
        notes.append(f"runtime health path {app.health_path!r} made absolute")
        app = app.model_copy(update={"health_path": "/" + app.health_path.lstrip("/")})
    if app.kind == "http" and not any("{port}" in a for a in app.args):
        notes.append("runtime args did not name {port}; '--port {port}' appended")
        app = app.model_copy(update={"args": [*app.args, "--port", "{port}"]})
    spec = ProductSpec(
        name=name,
        title=out.title.strip() or name,
        vision=out.vision.strip(),
        users=[u.strip() for u in out.users if u.strip()] or ["the author"],
        features=features,
        quality_bar=[q.strip() for q in out.quality_bar if q.strip()] or ["tests pass"],
        stack=stack,
        dependencies=sorted({d.strip() for d in out.dependencies if d.strip()}),
        app=app,
    )
    return spec, notes


def render_product_md(spec: ProductSpec) -> str:
    """``PRODUCT.md``: the product memory a human approves and later runs read back."""
    lines = [
        f"# {spec.title}",
        "",
        f"Package: `{spec.name}` · Stack: `{spec.stack}` (toolchain profile)",
        "",
        "## Vision",
        spec.vision,
        "",
        "## Users",
        *[f"- {u}" for u in spec.users],
        "",
        "## Features (build order; each criterion is a black-box test)",
    ]
    for f in spec.features:
        lines.append(f"### {f.id}: {f.description}")
        lines.extend(f"- [ ] {a}" for a in f.acceptance)
        lines.append("")
    lines += ["## Quality bar (release gate)", *[f"- {q}" for q in spec.quality_bar], ""]
    lines += [
        "## Approved dependencies",
        *([f"- {d}" for d in spec.dependencies] or ["- none beyond the standard library"]),
        "",
        "## Runtime",
        f"- kind: {spec.app.kind}",
        f"- module: {spec.app.module or spec.name}",
        f"- args: {' '.join(spec.app.args) if spec.app.args else '(none)'}",
        f"- health: {spec.app.health_path}",
        "",
        "Generated by SCIO product mode; edit by hand or rerun `scio new` to revise.",
    ]
    return "\n".join(lines) + "\n"
