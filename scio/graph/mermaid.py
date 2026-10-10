"""Mermaid rendering of compiled graphs and the docs sections that must match them.

``docs/ARCHITECTURE.md`` carries generated sections delimited by::

    <!-- scio:graph:<name>:begin -->
    ```mermaid
    ...
    ```
    <!-- scio:graph:<name>:end -->

``scripts/gen_mermaid.py`` rewrites them from the compiled graphs in
:data:`scio.graph.registry.GRAPHS`; ``scripts/check_docs.py`` fails if they drifted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def render_mermaid(compiled: Any) -> str:
    """Mermaid source for a compiled LangGraph graph (``CompiledStateGraph``)."""
    text: str = compiled.get_graph().draw_mermaid()
    return text.strip() + "\n"


def _markers(name: str) -> tuple[str, str]:
    return f"<!-- scio:graph:{name}:begin -->", f"<!-- scio:graph:{name}:end -->"


def _section_re(name: str) -> re.Pattern[str]:
    begin, end = (re.escape(m) for m in _markers(name))
    return re.compile(rf"{begin}\n(?P<body>.*?){end}", re.DOTALL)


def render_section(name: str, mermaid: str) -> str:
    begin, end = _markers(name)
    return f"{begin}\n```mermaid\n{mermaid.strip()}\n```\n{end}"


def section_names(text: str) -> list[str]:
    return re.findall(r"<!-- scio:graph:([\w-]+):begin -->", text)


def read_section(text: str, name: str) -> str | None:
    """The Mermaid source inside a section, or None if the section is absent."""
    m = _section_re(name).search(text)
    if not m:
        return None
    body = m.group("body")
    fence = re.search(r"```mermaid\n(?P<src>.*?)```", body, re.DOTALL)
    return fence.group("src").strip() if fence else body.strip()


def update_doc_section(path: Path, name: str, mermaid: str) -> bool:
    """Write the section (replacing or appending). Returns True if the file changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = render_section(name, mermaid)
    pattern = _section_re(name)
    new = (
        pattern.sub(lambda _m: block, text)
        if pattern.search(text)
        else text.rstrip("\n") + "\n\n" + block + "\n"
    )
    if new == text:
        return False
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def section_matches(text: str, name: str, mermaid: str) -> bool:
    current = read_section(text, name)
    return current is not None and current.strip() == mermaid.strip()
