"""The product release (M11, PRODUCT_MODE component 5): the version, the changelog cut, the
tag and the artifact. The graph nodes that use these live in ``scio.product.build``.

Everything here is stack-neutral: the version is whatever the product's own skeleton
command prints (``python -m <pkg> --version``, ``node src/main.ts --version``), and the
artifact is a ``git archive`` of the tagged commit (it carries the Dockerfile), plus
whatever the profile's build check left in ``.scio/sandbox/dist``.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime

from scio.aci.sandbox.base import Sandbox
from scio.state.models import ProductSpec
from scio.toolchain import Toolchain

RELEASE_DIR = ".scio/sandbox/release"
"""Where artifacts are kept: git-ignored by every scaffold (``.scio/sandbox/``)."""
DIST_DIR = ".scio/sandbox/dist"
"""Where the profiles' build checks write their output."""

_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")
_SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\][^\n]*$", re.MULTILINE)


def read_version(sb: Sandbox, tc: Toolchain, spec: ProductSpec) -> tuple[str | None, str]:
    """The version the product reports about itself, and the command's output."""
    argv = shlex.split(tc.skeleton_command(spec.name))
    r = sb.exec(argv, timeout_s=60)
    if not r.ok:
        return None, r.output[-400:]
    found = _VERSION_RE.findall(r.stdout)
    return (found[-1] if found else None), r.output[-400:]


def cut_changelog(text: str, version: str, today: str | None = None) -> str:
    """Move ``[Unreleased]`` into ``[version] - today`` and open a new empty one. A
    changelog that already has the section is returned unchanged (idempotent)."""
    if f"## [{version}]" in text:
        return text
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if "## [Unreleased]" not in text:
        return text.rstrip("\n") + f"\n\n## [Unreleased]\n\n## [{version}] - {today}\n"
    return text.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{version}] - {today}", 1)


def release_notes(text: str, version: str) -> str:
    """The body of ``version``'s changelog section, for the GitHub Release."""
    parts = _SECTION_RE.split(text)
    bodies = dict(zip(parts[1::2], parts[2::2], strict=True))
    body = (bodies.get(version) or "").strip()
    return body + "\n" if body else f"Release {version}.\n"


def tag_exists(sb: Sandbox, tag: str) -> str | None:
    """The commit ``tag`` points at, or None."""
    r = sb.exec(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"])
    return (r.stdout.strip() or None) if r.ok else None


def build_artifacts(sb: Sandbox, spec: ProductSpec, version: str, tag: str) -> list[str]:
    """``<name>-<version>.tar.gz`` from the tag (``git archive``, every stack), plus the
    build check's output when it produced any. Returns repository-relative paths."""
    out_dir = f"{RELEASE_DIR}/{tag}"
    sb.write_text(f"{sb.root}/{out_dir}/README.txt", f"{spec.title} {version} ({tag})\n")
    archive = f"{out_dir}/{spec.name}-{version}.tar.gz"
    r = sb.exec(
        [
            "git",
            "archive",
            "--format=tar.gz",
            f"--prefix={spec.name}-{version}/",
            "-o",
            f"{sb.root}/{archive}",
            tag,
        ],
        timeout_s=120,
    )
    if not r.ok:
        raise RuntimeError(f"git archive {tag} failed: {r.output[-300:]}")
    paths = [archive]
    if sb.exists(f"{sb.root}/{DIST_DIR}"):
        built = f"{out_dir}/{spec.name}-{version}-dist.tar.gz"
        r = sb.exec(
            ["tar", "-czf", f"{sb.root}/{built}", "-C", f"{sb.root}/{DIST_DIR}", "."],
            timeout_s=120,
        )
        if r.ok:
            paths.append(built)
    return paths
