"""Product memory on disk: ``PRODUCT.md`` read back, ``progress.json`` typed (ADR 0011).

``PRODUCT.md`` is the source of truth for the features (a human approved it and may edit it
by hand); ``progress.json`` records what the milestone loop has done with them. Both live in
the target repository, so a build resumes from the repository alone.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from scio.state.models import (
    AppSpec,
    MilestoneFeatureOutput,
    MilestonePlan,
    MilestonePlanOutput,
    ProductFeature,
    ProductSpec,
    Tier,
)

PROGRESS_PATH = ".scio/product/progress.json"
PRODUCT_MD = "PRODUCT.md"
MAX_FEATURES_PER_MILESTONE = 4

FeatureState = Literal["todo", "in_progress", "done", "skipped", "failed"]
"""Only ``done`` is finished; ``skipped`` means deferred to a later milestone (I-116)."""
MilestoneState = Literal["planned", "in_progress", "done", "failed"]


class FeatureRecord(BaseModel):
    id: str
    description: str
    status: FeatureState = "todo"
    commit: str | None = None
    thread: str | None = None
    tier: str | None = None
    issue: int | None = None
    usd: float = 0.0


class MilestoneRecord(BaseModel):
    id: str
    title: str
    status: MilestoneState = "planned"
    branch: str | None = None
    features: list[str] = Field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    merged_commit: str | None = None
    usd: float = 0.0


class ProgressFile(BaseModel):
    product: str
    stack: str
    bootstrap_commit: str | None = None
    build_thread: str | None = None
    milestones: list[MilestoneRecord] = Field(default_factory=list)
    features: list[FeatureRecord] = Field(default_factory=list)
    updated: str = ""

    def feature(self, fid: str) -> FeatureRecord | None:
        return next((f for f in self.features if f.id == fid), None)

    def milestone(self, mid: str) -> MilestoneRecord | None:
        return next((m for m in self.milestones if m.id == mid), None)

    def open_feature_ids(self) -> list[str]:
        """Every feature not ``done``, in record order. A ``skipped`` feature is open: the
        human's ``skip`` defers it to the next milestone, and only removing it from PRODUCT.md
        drops it (I-116; a skip used to drop it silently and the release went out without it)."""
        return [f.id for f in self.features if f.status != "done"]

    def next_milestone_number(self) -> int:
        return len(self.milestones) + 1

    def to_json(self) -> str:
        data = self.model_copy(update={"updated": datetime.now(UTC).isoformat(timespec="seconds")})
        return json.dumps(data.model_dump(mode="json"), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ProgressFile:
        return cls.model_validate(json.loads(text))

    @classmethod
    def initial(cls, spec: ProductSpec, *, bootstrap_commit: str | None) -> ProgressFile:
        """After bootstrap every feature is ``todo``, P1 included: the scaffold is a skeleton
        and "done" means verified by the feature's own run (ADR 0011 decision 1)."""
        return cls(
            product=spec.name,
            stack=spec.stack,
            bootstrap_commit=bootstrap_commit,
            features=[FeatureRecord(id=f.id, description=f.description) for f in spec.features],
        )

    def sync_features(self, spec: ProductSpec) -> list[str]:
        """Add records for features that appeared in PRODUCT.md since the last run; drop
        records for features that were removed. Returns notes about what changed."""
        notes: list[str] = []
        known = {f.id for f in self.features}
        for f in spec.features:
            if f.id not in known:
                self.features.append(FeatureRecord(id=f.id, description=f.description))
                notes.append(f"{f.id} added from PRODUCT.md")
        ids = {f.id for f in spec.features}
        gone = [f.id for f in self.features if f.id not in ids]
        if gone:
            self.features = [f for f in self.features if f.id in ids]
            notes.append(f"removed from PRODUCT.md: {', '.join(gone)}")
        return notes


# ---------- PRODUCT.md round trip ----------

_FEATURE_RE = re.compile(r"^### (?P<id>P\d+): (?P<desc>.+)$")
_META_RE = re.compile(r"Package: `(?P<name>[^`]+)` · Stack: `(?P<stack>[^`]+)`")
_CHECK_RE = re.compile(r"^- \[[ xX]\] (?P<text>.+)$")


def parse_product_md(text: str) -> ProductSpec:
    """Read a ``PRODUCT.md`` written by :func:`scio.product.spec.render_product_md` (or
    edited by hand in the same shape) back into a spec. Raises ``ValueError`` when the
    file has no features."""
    title = "product"
    name = stack = None
    section = ""
    vision: list[str] = []
    users: list[str] = []
    quality: list[str] = []
    deps: list[str] = []
    runtime: dict[str, str] = {}
    features: list[ProductFeature] = []
    current: tuple[str, str, list[str]] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and title == "product":
            title = line[2:].strip()
            continue
        meta = _META_RE.search(line)
        if meta:
            name, stack = meta.group("name"), meta.group("stack")
            continue
        if line.startswith("## "):
            if current:
                features.append(_feature(current))
                current = None
            head = line[3:].lower()
            section = (
                "vision"
                if head.startswith("vision")
                else "users"
                if head.startswith("users")
                else "features"
                if head.startswith("features")
                else "quality"
                if head.startswith("quality")
                else "deps"
                if head.startswith("approved dependencies")
                else "runtime"
                if head.startswith("runtime")
                else "other"
            )
            continue
        if section == "features":
            m = _FEATURE_RE.match(line)
            if m:
                if current:
                    features.append(_feature(current))
                current = (m.group("id"), m.group("desc").strip(), [])
                continue
            c = _CHECK_RE.match(line)
            if c and current:
                current[2].append(c.group("text").strip())
            continue
        if section == "vision" and line.strip():
            vision.append(line.strip())
        elif section == "users" and line.startswith("- "):
            users.append(line[2:].strip())
        elif section == "quality" and line.startswith("- "):
            quality.append(line[2:].strip())
        elif section == "deps" and line.startswith("- "):
            d = line[2:].strip()
            if not d.startswith("none"):
                deps.append(d)
        elif section == "runtime" and line.startswith("- ") and ":" in line:
            key, _, value = line[2:].partition(":")
            runtime[key.strip().lower()] = value.strip()
    if current:
        features.append(_feature(current))
    if not features:
        raise ValueError("PRODUCT.md has no '### Pn: ...' feature sections")
    return ProductSpec(
        name=name or "app",
        title=title,
        vision=" ".join(vision)[:1200] or title,
        users=users or ["the author"],
        features=features,
        quality_bar=quality or ["tests pass"],
        stack=stack or "python",
        dependencies=deps,
        app=_app(runtime, name or "app"),
    )


def _app(runtime: dict[str, str], name: str) -> AppSpec:
    kind = runtime.get("kind", "cli")
    args = runtime.get("args", "")
    module = runtime.get("module") or None
    return AppSpec(
        kind="http" if kind == "http" else "cli",
        module=None if module in (None, name) else module,  # None means the package itself
        args=[] if args in ("", "(none)") else args.split(),
        health_path=runtime.get("health", "/health"),
    )


def _feature(current: tuple[str, str, list[str]]) -> ProductFeature:
    fid, desc, acceptance = current
    return ProductFeature(
        id=fid, description=desc, acceptance=acceptance or [f"{desc} (criterion to be refined)"]
    )


# ---------- milestone normaliser ----------


def normalise_milestone(
    out: MilestonePlanOutput, open_features: list[ProductFeature], number: int
) -> tuple[MilestonePlan, list[str]]:
    """The planner proposes, the orchestrator decides (I-046): the milestone is the longest
    prefix of the open features (PRODUCT.md order) the planner named, capped at
    :data:`MAX_FEATURES_PER_MILESTONE`; an empty or out-of-order proposal becomes the first
    open feature alone. Tiers default to T1. Notes explain every change. A feature skipped in
    an earlier milestone is open and keeps its place in PRODUCT.md order, so a planner cannot
    route around it: leaving it out stops the milestone before it, and when it is the first
    open feature the milestone is that feature alone (I-116)."""
    notes: list[str] = []
    if not open_features:
        raise ValueError("no open features to plan")
    proposed = {f.id: f for f in out.features}
    unknown = [fid for fid in proposed if fid not in {f.id for f in open_features}]
    if unknown:
        notes.append(f"planner named features that are not open: {', '.join(unknown)}; dropped")
    chosen: list[MilestoneFeatureOutput] = []
    for feat in open_features:
        if feat.id not in proposed:
            break
        p = proposed[feat.id]
        chosen.append(MilestoneFeatureOutput(id=feat.id, tier=p.tier, notes=p.notes))
        if len(chosen) >= MAX_FEATURES_PER_MILESTONE:
            break
    skipped = [f.id for f in open_features[len(chosen) :] if f.id in proposed]
    if skipped and len(chosen) < MAX_FEATURES_PER_MILESTONE:
        notes.append(
            f"planner skipped ahead to {', '.join(skipped)}; features run in PRODUCT.md order, "
            "so the milestone stops at the gap"
        )
    elif skipped:
        notes.append(
            f"milestone capped at {MAX_FEATURES_PER_MILESTONE} features; "
            f"{', '.join(skipped)} wait for the next one"
        )
    if not chosen:
        first = open_features[0]
        chosen = [MilestoneFeatureOutput(id=first.id, tier=Tier.T1)]
        notes.append(f"planner proposed nothing runnable; milestone is {first.id} alone")
    title = out.milestone_title.strip() or f"Milestone {number}"
    return (
        MilestonePlan(number=number, title=title, branch=f"milestone/{number}", features=chosen),
        notes,
    )
