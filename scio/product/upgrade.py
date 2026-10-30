"""``scio upgrade --product DIR`` (issue #58): bring a product bootstrapped by an older SCIO
up to the current product bar without touching anything the product has made its own.

Additive only. Every new file and fragment comes from the current scaffold for the product's
own ``PRODUCT.md`` (``scaffold_for``), so the upgrade and a fresh bootstrap cannot drift:
- a missing ``Dockerfile`` or ``.dockerignore`` is written; an existing one is left alone;
- the README gets the marked run block (the skeleton command, which always terminates) under
  its ``## Run`` heading, or a new ``## Run`` section, unless the markers are already there;
- a python product's ``pyproject.toml`` gets ``[tool.coverage.run]`` unless it has one, and
  ``[build-system]`` with the package list unless it has ``[build-system]`` or
  ``[tool.setuptools]``, so ``pip install -e .`` works (I-140);
- the product's CI gets the README run step only where its last step is still the old
  scaffold's skeleton step; any other CI file is the product's and is left alone;
- an old scaffold install line (``OLD_PYTHON_INSTALLS``, which never installed the project,
  I-140) becomes the current one in the CI and the README Develop block, line for line;
  a line the product changed is its own (I-142).
Tests are not rewritten: a product whose tests reach the code only through a subprocess
misses the coverage floor at its next feature's verify, which sends it to the debugger
(I-091). The changes are one commit with a marker, on the branch that is checked out; a
clean tree is required. Run at a merge stop, the commit lands on the milestone branch, and
the build's ``recheck`` answer pushes it and reads the PR's CI again (I-142).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from scio import __version__
from scio.aci.sandbox.base import Sandbox
from scio.aci.tools import SCIO_GIT_IDENTITY
from scio.product.progress import PRODUCT_MD, parse_product_md
from scio.product.release_checks import RUN_END, RUN_START
from scio.product.scaffold import (
    OLD_PYTHON_INSTALLS,
    PYTHON_BUILD_SYSTEM,
    PYTHON_INSTALL,
    README_RUN_STEP,
    python_packages_find,
    scaffold_for,
)
from scio.toolchain import get_toolchain

CI_PATH = ".github/workflows/ci.yml"
MARKER = f"[scio:upgrade:{__version__}]"


class UpgradeRefused(RuntimeError):
    """The product cannot be upgraded as it is (no PRODUCT.md, a dirty tree)."""


@dataclass
class UpgradeReport:
    added: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    """Already there, or the product's own version: left alone, with the reason."""
    commit: str | None = None

    def lines(self) -> list[str]:
        out = [f"added: {a}" for a in self.added] + [f"kept: {k}" for k in self.kept]
        out.append(f"commit {self.commit[:12]}" if self.commit else "nothing to commit")
        return out


def _git(sb: Sandbox, *args: str) -> str:
    r = sb.exec(["git", *SCIO_GIT_IDENTITY, *args], timeout_s=60)  # I-104
    if not r.ok:
        raise UpgradeRefused(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def readme_with_run_block(text: str, block: str) -> str:
    """``text`` with ``block`` under the first ``## Run`` heading, or a new section."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == "## Run":
            return "".join([*lines[: i + 1], "\n", block, "\n", *lines[i + 1 :]])
    return text.rstrip("\n") + "\n\n## Run\n\n" + block


def replace_install_lines(text: str, *, prefix: str) -> tuple[str, int]:
    """``text`` with every line that is exactly ``prefix`` + an old scaffold install line
    replaced by the current one (I-140), and how many were replaced. Any other line, even a
    near miss, is the product's own (I-142)."""
    old = {prefix + line for line in OLD_PYTHON_INSTALLS}
    out, n = [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body in old:
            out.append(prefix + PYTHON_INSTALL + line[len(body) :])
            n += 1
        else:
            out.append(line)
    return "".join(out), n


def upgrade_product(sb: Sandbox) -> UpgradeReport:
    root = sb.root
    if not sb.exists(f"{root}/{PRODUCT_MD}"):
        raise UpgradeRefused(f"no {PRODUCT_MD} in {root}: not a SCIO product")
    if _git(sb, "status", "--porcelain"):
        raise UpgradeRefused("the working tree has uncommitted changes; commit or stash them first")
    spec = parse_product_md(sb.read_text(f"{root}/{PRODUCT_MD}"))
    tc = get_toolchain(spec.stack)
    fresh = scaffold_for(spec)
    report = UpgradeReport()

    for rel in ("Dockerfile", ".dockerignore"):
        if sb.exists(f"{root}/{rel}"):
            report.kept.append(f"{rel} (exists)")
        else:
            sb.write_text(f"{root}/{rel}", fresh[rel])
            report.added.append(rel)

    readme = f"{root}/README.md"
    text = sb.read_text(readme) if sb.exists(readme) else f"# {spec.title}\n"
    if RUN_START in text and RUN_END in text:
        report.kept.append("README run block (exists)")
    else:
        block = f"{RUN_START}\n```\n{tc.skeleton_command(spec.name)}\n```\n{RUN_END}\n"
        sb.write_text(readme, readme_with_run_block(text, block))
        report.added.append("README run block")

    if spec.stack == "python":
        pyproject = f"{root}/pyproject.toml"
        data = tomllib.loads(sb.read_text(pyproject)) if sb.exists(pyproject) else {}
        if "coverage" in data.get("tool", {}):
            report.kept.append("[tool.coverage] (exists)")
        elif sb.exists(pyproject):
            section = fresh["pyproject.toml"].split("[tool.coverage.run]", 1)[1]
            current = sb.read_text(pyproject).rstrip("\n")
            sb.write_text(pyproject, f"{current}\n\n[tool.coverage.run]{section}")
            report.added.append("[tool.coverage.run] in pyproject.toml")
        if sb.exists(pyproject):
            data = tomllib.loads(sb.read_text(pyproject))
            if "build-system" in data or "setuptools" in data.get("tool", {}):
                report.kept.append("[build-system] (the product's own)")
            else:
                current = sb.read_text(pyproject).rstrip("\n")
                packages = python_packages_find(spec.name)
                sb.write_text(pyproject, f"{PYTHON_BUILD_SYSTEM}\n{current}\n\n{packages}")
                report.added.append("[build-system] and the package list in pyproject.toml")

    ci = f"{root}/{CI_PATH}"
    old_step = f"      - run: {tc.skeleton_command(spec.name)}\n"
    ci_text = sb.read_text(ci) if sb.exists(ci) else ""
    if "README run commands" in ci_text:
        report.kept.append(f"{CI_PATH} README step (exists)")
    elif ci_text.endswith(old_step):
        sb.write_text(ci, ci_text[: -len(old_step)] + README_RUN_STEP)
        report.added.append(f"{CI_PATH} README run step")
    else:
        report.kept.append(f"{CI_PATH} (the product's own; add the README run step by hand)")

    if spec.stack == "python":
        for rel, prefix in ((CI_PATH, "      - run: "), ("README.md", "")):
            path = f"{root}/{rel}"
            if not sb.exists(path):
                continue
            text, n = replace_install_lines(sb.read_text(path), prefix=prefix)
            if n:
                sb.write_text(path, text)
                report.added.append(f"{rel}: {PYTHON_INSTALL!r}, which installs the project")

    if report.added:
        _git(sb, "add", "-A")
        message = f"chore: upgrade to the SCIO {__version__} product bar {MARKER}"
        _git(sb, "commit", "-q", "-m", message)
        report.commit = _git(sb, "rev-parse", "HEAD")
    return report
