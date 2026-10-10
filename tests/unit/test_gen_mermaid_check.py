from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from langgraph.graph import END, START, StateGraph

import scio.graph.tiers  # noqa: F401 - register t0/t1/t2 before any test patches the registry
from scio.graph import registry
from scio.settings import REPO_ROOT


def _load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "gen_mermaid.py"
    spec = importlib.util.spec_from_file_location("gen_mermaid", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _demo() -> object:
    g = StateGraph(dict)
    g.add_node("x", lambda s: s)
    g.add_edge(START, "x")
    g.add_edge("x", END)
    return g.compile()


def test_generate_then_check_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gm = _load()
    monkeypatch.setattr(registry, "GRAPHS", {"demo": _demo})
    doc = tmp_path / "ARCHITECTURE.md"
    doc.write_text("# A\n", encoding="utf-8", newline="\n")
    assert gm.check(doc) == [
        f"{doc.name}: graph section 'demo' is missing or stale; run scripts/gen_mermaid.py"
    ]
    assert gm.generate(doc) == ["demo"]
    assert gm.check(doc) == []
    monkeypatch.setattr(registry, "GRAPHS", {})
    assert gm.check(doc) == [f"{doc.name}: graph section 'demo' has no registered graph"]


def test_repo_doc_is_in_sync() -> None:
    assert _load().check() == []
