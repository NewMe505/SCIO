from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from scio.graph.checkpointer import open_checkpointer
from scio.graph.effects import EffectLog, effect, effect_key, run_effect
from scio.graph.mermaid import read_section, render_mermaid, section_matches, update_doc_section


def test_effect_key_is_deterministic_and_distinct() -> None:
    assert effect_key("t", "n", "i") == effect_key("t", "n", "i")
    assert effect_key("t", "n", "i") != effect_key("t", "n", "j")


def test_run_effect_applies_once_and_persists(tmp_path: Path) -> None:
    log = EffectLog(tmp_path / "fx.sqlite")
    calls = {"n": 0}

    def apply() -> dict[str, int]:
        calls["n"] += 1
        return {"v": calls["n"]}

    r1 = run_effect(log, thread_id="t", node="n", intent="i", apply=apply)
    r2 = run_effect(log, thread_id="t", node="n", intent="i", apply=apply)
    assert r1 == r2 == {"v": 1} and calls["n"] == 1
    log.close()
    reopened = EffectLog(tmp_path / "fx.sqlite")  # survives a process restart
    assert run_effect(reopened, thread_id="t", node="n", intent="i", apply=apply) == {"v": 1}
    assert reopened.records()[0].status == "done"


def test_run_effect_uses_probe_when_started_but_not_done(tmp_path: Path) -> None:
    log = EffectLog(tmp_path / "fx.sqlite")
    applied = {"n": 0}

    def apply() -> str:
        applied["n"] += 1
        return "sha1"

    def boom() -> None:
        raise RuntimeError("crash between apply and record")

    with pytest.raises(RuntimeError):
        run_effect(log, thread_id="t", node="n", intent="commit", apply=apply, after_apply=boom)
    assert log.get(effect_key("t", "n", "commit")) is not None
    # resume: the probe sees the world already changed; apply is not repeated
    out = run_effect(
        log, thread_id="t", node="n", intent="commit", apply=apply, probe=lambda: "sha1"
    )
    assert out == "sha1" and applied["n"] == 1
    assert log.get(effect_key("t", "n", "commit")).status == "done"  # type: ignore[union-attr]


class S(TypedDict):
    log: Annotated[list[str], operator.add]


def _graph(fx: EffectLog) -> StateGraph:
    g = StateGraph(S)

    def a(_s: S) -> dict[str, list[str]]:
        v = effect(fx, node="a", intent="x", apply=lambda: "did-a")
        return {"log": [v]}

    g.add_node("a", a)
    g.add_edge(START, "a")
    g.add_edge("a", END)
    return g


def test_effect_inside_graph_reads_thread_id(tmp_path: Path) -> None:
    fx = EffectLog(tmp_path / "fx.sqlite")
    with open_checkpointer("memory://") as saver:
        compiled = _graph(fx).compile(checkpointer=saver)
        out = compiled.invoke(
            {"log": []}, config={"configurable": {"thread_id": "T1"}}, durability="sync"
        )
    assert out["log"] == ["did-a"]
    assert fx.get(effect_key("T1", "a", "x")).status == "done"  # type: ignore[union-attr]


def test_open_checkpointer_sqlite_and_unknown(tmp_path: Path) -> None:
    with open_checkpointer(f"sqlite:///{(tmp_path / 'c' / 'ck.sqlite').as_posix()}") as saver:
        assert saver is not None
    with pytest.raises(ValueError), open_checkpointer("redis://x"):
        pass


def test_mermaid_render_and_doc_sections(tmp_path: Path) -> None:
    compiled = _graph(EffectLog(":memory:")).compile()
    src = render_mermaid(compiled)
    assert "__start__" in src and "a" in src and "__end__" in src
    doc = tmp_path / "ARCH.md"
    doc.write_text("# Arch\n\nprose\n", encoding="utf-8", newline="\n")
    assert update_doc_section(doc, "t0", src) is True
    assert update_doc_section(doc, "t0", src) is False  # idempotent
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("# Arch\n\nprose\n") and read_section(text, "t0") == src.strip()
    assert section_matches(text, "t0", src)
    assert update_doc_section(doc, "t0", src + "X --> Y\n") is True
    assert not section_matches(doc.read_text(encoding="utf-8"), "t0", src)
