"""Regenerate the Mermaid sections of docs/ARCHITECTURE.md from the compiled graphs.

Usage: ``python scripts/gen_mermaid.py [--check]``. With ``--check`` nothing is written;
exit 1 if any section is missing or stale (this is what ``check_docs.py`` calls).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "ARCHITECTURE.md"
TIER_MODULES = ("scio.graph.tiers",)  # registered graphs live here from M3


def load_graphs() -> dict[str, object]:
    from scio.graph.registry import GRAPHS

    for mod in TIER_MODULES:
        with contextlib.suppress(ModuleNotFoundError):
            importlib.import_module(mod)
    return {name: factory() for name, factory in GRAPHS.items()}


def check(doc: Path = DOC) -> list[str]:
    from scio.graph.mermaid import render_mermaid, section_matches, section_names

    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    graphs = load_graphs()
    errors: list[str] = []
    for name, compiled in graphs.items():
        if not section_matches(text, name, render_mermaid(compiled)):
            errors.append(
                f"{doc.name}: graph section {name!r} is missing or stale; "
                "run scripts/gen_mermaid.py"
            )
    for name in section_names(text):
        if name not in graphs:
            errors.append(f"{doc.name}: graph section {name!r} has no registered graph")
    return errors


def generate(doc: Path = DOC) -> list[str]:
    from scio.graph.mermaid import render_mermaid, update_doc_section

    changed: list[str] = []
    for name, compiled in load_graphs().items():
        if update_doc_section(doc, name, render_mermaid(compiled)):
            changed.append(name)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        errors = check()
        for e in errors:
            print(f"gen_mermaid: {e}")
        return 1 if errors else 0
    changed = generate()
    print(f"gen_mermaid: updated {changed or 'nothing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
