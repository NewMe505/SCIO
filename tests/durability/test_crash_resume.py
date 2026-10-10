"""M2 acceptance: a 3-node graph with file-write and git side effects is killed mid-run
and resumed from the SQLite checkpoint with zero duplicated writes or commits."""

from __future__ import annotations

import operator
import subprocess
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from scio.aci.sandbox.local import LocalSandbox
from scio.aci.tools import GitCheckpointInput, find_commit, git_checkpoint, head_sha
from scio.graph.checkpointer import DURABILITY, open_checkpointer
from scio.graph.effects import EffectLog, current_thread_id, effect_key, run_effect


class Crash(RuntimeError):
    pass


class S(TypedDict):
    log: Annotated[list[str], operator.add]


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)


def _commit_count(path: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=path, capture_output=True, text=True
    )
    return int(out.stdout.strip() or 0)


@pytest.mark.parametrize(
    "crash_point", ["between_apply_and_record", "after_effect_before_node_end"]
)
def test_crash_mid_run_resumes_without_duplicate_side_effects(
    tmp_path: Path, crash_point: str
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    sb = LocalSandbox(repo)
    fx = EffectLog(tmp_path / "effects.sqlite")
    counters = {"write_a": 0, "commit": 0, "write_b": 0}
    crash_armed = {"on": True}

    def n1(_s: S) -> dict[str, list[str]]:
        def apply() -> str:
            counters["write_a"] += 1
            sb.write_text(f"{sb.root}/a.txt", "a\n")
            return "a.txt"

        return {
            "log": [
                run_effect(
                    fx, thread_id=current_thread_id(), node="n1", intent="write:a", apply=apply
                )
            ]
        }

    def n2(_s: S) -> dict[str, list[str]]:
        tid = current_thread_id()
        marker = f"[scio:{effect_key(tid, 'n2', 'commit:F1')}]"

        def apply() -> str:
            counters["commit"] += 1
            git_checkpoint(sb, GitCheckpointInput(message=f"feat: F1 {marker}"))
            return head_sha(sb)

        def after_apply() -> None:
            if crash_point == "between_apply_and_record" and crash_armed["on"]:
                crash_armed["on"] = False
                raise Crash("killed after the commit, before the effect log recorded it")

        sha = run_effect(
            fx, thread_id=tid, node="n2", intent="commit:F1",
            apply=apply, probe=lambda: find_commit(sb, marker), after_apply=after_apply,
        )  # fmt: skip
        if crash_point == "after_effect_before_node_end" and crash_armed["on"]:
            crash_armed["on"] = False
            raise Crash("killed after the effect completed, before the node returned")
        return {"log": [sha]}

    def n3(_s: S) -> dict[str, list[str]]:
        def apply() -> str:
            counters["write_b"] += 1
            sb.write_text(f"{sb.root}/b.txt", "b\n")
            return "b.txt"

        return {
            "log": [
                run_effect(
                    fx, thread_id=current_thread_id(), node="n3", intent="write:b", apply=apply
                )
            ]
        }

    g = StateGraph(S)
    g.add_node("n1", n1)
    g.add_node("n2", n2)
    g.add_node("n3", n3)
    g.add_edge(START, "n1")
    g.add_edge("n1", "n2")
    g.add_edge("n2", "n3")
    g.add_edge("n3", END)

    dsn = f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}"
    config = {"configurable": {"thread_id": "run-1"}}

    # First process: crashes inside n2.
    with open_checkpointer(dsn) as saver:
        compiled = g.compile(checkpointer=saver)
        with pytest.raises(Crash):
            compiled.invoke({"log": []}, config=config, durability=DURABILITY)
    assert counters == {"write_a": 1, "commit": 1, "write_b": 0}
    assert _commit_count(repo) == 1

    # Second process: fresh checkpointer handle and effect log on the same files; resume.
    fx2 = EffectLog(tmp_path / "effects.sqlite")
    with open_checkpointer(dsn) as saver:
        compiled = g.compile(checkpointer=saver)
        # nodes close over fx (same file); fx2 proves the log is readable from a new handle
        assert fx2.get(effect_key("run-1", "n1", "write:a")) is not None
        out = compiled.invoke(None, config=config, durability=DURABILITY)

    assert counters == {"write_a": 1, "commit": 1, "write_b": 1}, "a side effect was duplicated"
    assert _commit_count(repo) == 1
    assert (repo / "a.txt").read_text() == "a\n" and (repo / "b.txt").read_text() == "b\n"
    assert out["log"] == ["a.txt", head_sha(sb), "b.txt"]
    assert fx2.get(effect_key("run-1", "n2", "commit:F1")).status == "done"  # type: ignore[union-attr]
