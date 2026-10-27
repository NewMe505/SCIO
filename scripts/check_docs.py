"""Docs checks. Run in CI; a failure here blocks merge like a test failure.

Checks (M1):
1. Relative links in Markdown files resolve to existing files.
2. Model IDs appear only in ``configs/models.yaml`` (and docs). Code refers to roles.
3. ``CHANGELOG.md`` has an ``[Unreleased]`` section.

M2 adds: architecture diagram vs. compiled graph, runnable code samples, public-API
docstring coverage.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")
MODEL_ID_RE = re.compile(r"\b(claude-[a-z]+-\d(?:-\d)?|gpt-\d[\w.-]*)\b")
CODE_DIRS = ("scio", "evals", "tests", "scripts")
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "docs/api",
    "evals/results",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
}


def _skip(p: Path) -> bool:
    rel = p.relative_to(ROOT).as_posix()
    return any(rel == s or rel.startswith(s + "/") for s in SKIP_DIRS)


FENCE_RE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans; links inside code are not links."""
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))


def links_in(md: Path) -> list[str]:
    """Broken relative links in one Markdown file, as error strings."""
    errors: list[str] = []
    rel = md.relative_to(ROOT) if md.is_relative_to(ROOT) else md.name
    text = _strip_code(md.read_text(encoding="utf-8", errors="replace"))
    for m in LINK_RE.finditer(text):
        target = m.group(1)
        if re.match(r"^[a-z]+:", target) or target.startswith("mailto:"):
            continue
        # Windows resolves '..', '...' etc. to parent directories; only '..' is a real link.
        if set(target) == {"."} and target != "..":
            errors.append(f"{rel}: broken link -> {target}")
            continue
        if not (md.parent / target).exists():
            errors.append(f"{rel}: broken link -> {target}")
    return errors


def check_links() -> list[str]:
    errors: list[str] = []
    for md in ROOT.rglob("*.md"):
        if not _skip(md):
            errors.extend(links_in(md))
    return errors


def check_model_ids() -> list[str]:
    errors: list[str] = []
    for d in CODE_DIRS:
        for f in (ROOT / d).rglob("*.py"):
            if _skip(f):
                continue
            for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if MODEL_ID_RE.search(line) and "noqa: model-id" not in line:
                    errors.append(
                        f"{f.relative_to(ROOT)}:{i}: model ID in code; "
                        "use a role from configs/models.yaml"
                    )
    return errors


def check_changelog() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return [] if "## [Unreleased]" in text else ["CHANGELOG.md: missing '## [Unreleased]' section"]


MILESTONE_RE = re.compile(r"\bM(\d+)\b")
SECTION_RE = re.compile(r"^## \[([^\]]+)\][^\n]*$", re.MULTILINE)


def _product_mode_status() -> str:
    pm = (ROOT / "docs" / "PRODUCT_MODE.md").read_text(encoding="utf-8")
    return next((ln for ln in pm.splitlines() if ln.startswith("- **Status:**")), "")


def unreleased_done_milestones(changelog: str, pm_status: str) -> list[str]:
    """Milestones the PRODUCT_MODE status line calls done that CHANGELOG.md names under
    ``[Unreleased]`` and in no released section (I-086)."""
    done = set(re.findall(r"\b(M\d+) [A-Za-z +\-]+? done\b", pm_status))
    parts = SECTION_RE.split(changelog)
    bodies = dict(zip(parts[1::2], parts[2::2], strict=True))
    unreleased = bodies.pop("Unreleased", "")
    released = "\n".join(bodies.values())
    named = {f"M{n}" for n in MILESTONE_RE.findall(unreleased)}
    named -= {f"M{n}" for n in MILESTONE_RE.findall(released)}
    return sorted(done & named, key=lambda m: int(m[1:]))


def check_release_cadence() -> list[str]:
    """A release follows every milestone merge: one finished milestone may wait under
    ``[Unreleased]`` for its release PR, two may not (I-086: M8 to M10 piled up behind
    v0.5.0 for two weeks)."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pending = unreleased_done_milestones(changelog, _product_mode_status())
    if len(pending) < 2:
        return []
    return [
        f"CHANGELOG.md: {', '.join(pending)} are done but unreleased; cut a release after "
        "each milestone merges (I-086)"
    ]


def check_readme_status() -> list[str]:
    """The README is the repository's landing page and must say where the project stands at
    every push (owner's rule, 2026-10-26). Its ``> Status:`` line names the latest released
    version from CHANGELOG.md and every milestone docs/PRODUCT_MODE.md calls ``in progress``;
    its ADR list names the newest ADR."""
    errors: list[str] = []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status = next((ln for ln in readme.splitlines() if ln.startswith("> Status:")), "")
    if not status:
        return ["README.md: no '> Status:' line (the landing page must say where we are)"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    if released and f"v{released.group(1)} released" not in status:
        errors.append(
            f"README.md status line does not say 'v{released.group(1)} released' "
            "(CHANGELOG.md's latest release)"
        )
    pm_status = _product_mode_status()
    for milestone in re.findall(r"(M\d+) [A-Za-z +\-]+? in progress", pm_status):
        if not re.search(rf"\b{milestone}\b.{{0,40}}in progress", status):
            errors.append(
                f"README.md status line does not say {milestone} is in progress, as "
                "docs/PRODUCT_MODE.md does"
            )
    adrs = sorted(p.name[:4] for p in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
    adr_line = next((ln for ln in readme.splitlines() if "docs/adr/" in ln and "0001" in ln), "")
    if adrs and adrs[-1] not in adr_line:
        errors.append(f"README.md ADR list does not name the newest ADR {adrs[-1]}")
    return errors


def check_ignored_sources() -> list[str]:
    """No source file under the code directories may be git-ignored (ledger I-017)."""
    import subprocess

    proc = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", *CODE_DIRS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []  # not a git checkout (e.g. an sdist); nothing to check
    ignored = [
        ln for ln in proc.stdout.splitlines()
        if ln.endswith((".py", ".md", ".yaml", ".yml"))
        and "__pycache__" not in ln
        and not any(ln.startswith(s + "/") for s in SKIP_DIRS)  # generated outputs are fine
    ]  # fmt: skip
    return [f"{ln}: source file is git-ignored; fix .gitignore" for ln in ignored]


def check_graph_sections() -> list[str]:
    """docs/ARCHITECTURE.md generated Mermaid sections must match the compiled graphs."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_mermaid

    return gen_mermaid.check()


def main() -> int:
    errors = (
        check_links()
        + check_model_ids()
        + check_changelog()
        + check_readme_status()
        + check_release_cadence()
        + check_ignored_sources()
        + check_graph_sections()
    )
    for e in errors:
        print(f"docs-check: {e}")
    print(f"docs-check: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
