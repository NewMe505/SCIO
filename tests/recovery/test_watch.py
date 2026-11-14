"""``python -m recovery watch`` (issue #107): an incident's state, timeline and repair
session, once or followed live."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from recovery import cli, watch
from recovery.model import FailureClass, Incident, IncidentState
from recovery.store import Store

WORKTREE = r"C:\Users\me\projects\SCIO\.claude\worktrees\recovery-inc-1"
KEY = "C--Users-me-projects-SCIO--claude-worktrees-recovery-inc-1"


def _incident(**kw: object) -> Incident:
    base: dict[str, object] = {
        "incident_id": "inc-1",
        "signature": "39cc3f620be64916",
        "failure_class": FailureClass.HARNESS_SUSPECT,
        "summary": "subprocess.TimeoutExpired",
        "workspace": "W",
        "state": IncidentState.REPAIRING,
        "created_at": 1.0,
        "updated_at": 1.0,
        "innermost_scio_frame": "scio/harness/providers/claude_code.py:run",
        "attempts": 1,
        "issue_number": 105,
        "branch": "fix/105-recovery-39cc3f",
        "worktree": WORKTREE,
    }
    base.update(kw)
    return Incident(**base)  # type: ignore[arg-type]


def _assistant(stamp: str, *parts: dict[str, object]) -> str:
    event = {"type": "assistant", "timestamp": stamp, "message": {"content": list(parts)}}
    return json.dumps(event)


def _tool(name: str, **inp: object) -> dict[str, object]:
    return {"type": "tool_use", "name": name, "input": inp}


def _says(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def _transcript(claude_dir: Path, lines: list[str], *, key: str = KEY, name: str = "s1") -> Path:
    folder = claude_dir / "projects" / key
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def test_the_project_key_is_claude_codes_directory_name() -> None:
    assert watch.project_key(WORKTREE) == KEY
    assert watch.project_key("/home/me/SCIO/.claude/worktrees/x") == (
        "-home-me-SCIO--claude-worktrees-x"
    )


def test_the_newest_transcript_of_the_worktree_is_found(claude_dir: Path) -> None:
    old = _transcript(claude_dir, ["{}"], name="old")
    new = _transcript(claude_dir, ["{}"], name="new")
    os.utime(old, (time.time() - 60, time.time() - 60))
    assert watch.transcript_for(WORKTREE) == new


def test_a_differently_spelled_parent_still_finds_the_worktrees_transcript(
    claude_dir: Path,
) -> None:
    found = _transcript(
        claude_dir, ["{}"], key="c--users-ME-projects-SCIO--claude-worktrees-recovery-inc-1"
    )
    assert watch.transcript_for(WORKTREE) == found


def test_no_transcript_is_none_not_an_error(claude_dir: Path) -> None:
    assert watch.transcript_for(WORKTREE) is None


def test_steps_are_tool_calls_and_what_the_session_says_in_order(claude_dir: Path) -> None:
    path = _transcript(
        claude_dir,
        [
            json.dumps({"type": "user", "message": {"content": "the brief"}}),
            _assistant(
                "2026-11-14T09:49:10.123Z",
                _says("I'll start by orienting.\nMore detail."),
                _tool("Bash", command="git status", description="Check worktree status"),
            ),
            "not json at all",
            json.dumps({"type": "assistant", "message": "an unknown shape"}),
            _assistant("2026-11-14T09:49:12Z", _tool("Read", file_path="scio/x.py")),
        ],
    )
    steps = watch.session_steps(path)
    assert [(s.stamp, s.kind, s.text) for s in steps] == [
        ("09:49:10", "says", "I'll start by orienting."),
        ("09:49:10", "Bash", "Check worktree status"),
        ("09:49:12", "Read", "scio/x.py"),
    ]


def test_a_torn_last_line_is_left_out_and_secrets_are_scrubbed(claude_dir: Path) -> None:
    path = _transcript(
        claude_dir,
        [
            _assistant(
                "2026-11-14T09:50:00Z",
                _tool("Bash", command="export ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwx"),
            )
        ],
    )
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"type": "assistant", "message": {"content": [')  # still being written
    [step] = watch.session_steps(path)
    assert "sk-ant-" not in step.text and "[redacted]" in step.text


def test_render_shows_the_header_timeline_and_the_sessions_last_steps(claude_dir: Path) -> None:
    store = Store(":memory:", clock=iter([100.0, 101.0]).__next__)
    store.save_incident(_incident())
    store.add_event("inc-1", "detected harness_suspect in run run-1")
    store.add_event("inc-1", "issue_open -> repairing: repair attempt 1 of 2")
    lines = [
        _assistant(f"2026-11-14T09:5{i}:00Z", _tool("Bash", description=f"step {i}"))
        for i in range(5)
    ]
    _transcript(claude_dir, lines)
    text = "\n".join(watch.render(store, "inc-1", last=2))
    assert "incident inc-1: repairing (harness_suspect)" in text
    assert "issue #105" in text and "frame scio/harness/providers/claude_code.py:run" in text
    assert "detected harness_suspect" in text and "repair attempt 1 of 2" in text
    assert "repair session (5 steps, the last 2):" in text
    assert "step 3" in text and "step 4" in text and "step 2" not in text


def test_render_says_when_there_is_no_transcript_and_refuses_an_unknown_incident(
    claude_dir: Path,
) -> None:
    store = Store(":memory:")
    store.save_incident(_incident())
    assert any("no transcript yet" in line for line in watch.render(store, "inc-1"))
    store.save_incident(_incident(incident_id="inc-2", worktree=""))
    assert not any("repair session" in line for line in watch.render(store, "inc-2"))
    with pytest.raises(KeyError):
        watch.render(store, "inc-missing")


def test_follow_prints_only_what_is_new_and_stops_when_the_incident_closes(
    claude_dir: Path,
) -> None:
    store = Store(":memory:")
    store.save_incident(_incident())
    store.add_event("inc-1", "issue_open -> repairing")
    path = _transcript(claude_dir, [_assistant("2026-11-14T09:50:00Z", _says("first"))])
    rounds: list[int] = []

    def sleep(_: float) -> None:
        rounds.append(1)
        if len(rounds) == 1:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(_assistant("2026-11-14T09:51:00Z", _says("second")) + "\n")
            store.add_event("inc-1", "repairing -> pr_verified: PR #106")
            store.save_incident(_incident(state=IncidentState.PR_VERIFIED, pr_number=106))
        else:
            store.save_incident(_incident(state=IncidentState.CLOSED, pr_number=106))

    out = io.StringIO()
    polled = watch.follow(store, "inc-1", out=out, interval_s=0, sleep=sleep, max_rounds=10)
    text = out.getvalue()
    assert polled == 2
    assert text.count("first") == 1 and text.count("second") == 1
    assert "repairing -> pr_verified: PR #106" in text
    assert "state: repairing -> pr_verified" in text and "state: pr_verified -> closed" in text
    assert text.rstrip().endswith("incident inc-1 is closed")


def test_the_cli_watches_an_incident_and_refuses_an_unknown_one(
    claude_dir: Path, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store.at_home(recovery_home)
    store.save_incident(_incident())
    store.close()
    assert cli.main(["watch", "inc-1"]) == 0
    assert "incident inc-1: repairing" in capsys.readouterr().out
    assert cli.main(["watch", "inc-nope"]) == 2
    assert "no incident inc-nope" in capsys.readouterr().err
