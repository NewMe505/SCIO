"""Reading a finished run's evidence, and snapshotting the workspace without writing it.

The databases are backed up while a writer holds them open in WAL mode, and once more after
the writer died without a checkpoint: the case where a read-write connection would write
the original."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from recovery.classify import classify
from recovery.config import WorkspaceConfig
from recovery.evidence import (
    backup_sqlite,
    fingerprints,
    manifest_paths,
    parse_traceback,
    read_evidence,
    read_outcome,
    snapshot,
    tail,
)
from recovery.model import FailureClass, RunRecord, RunStatus
from recovery.scrub import REDACTED

ANTHROPIC = "sk-" + "ant-" + "api03-Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1Jh"
REPO = r"C:\Users\playa\OneDrive\Desktop\projects\SCIO"
LIVE_WT = REPO + r"\.scio\live-wt"
CLAUDE_CODE = "scio/harness/providers/claude_code.py:run"
I180_MESSAGE = "Command '['claude', '-p', '--output-format', 'json']' timed out after 600 seconds"

# I-180's traceback as Windows prints it. <REPO> and <MESSAGE> keep the lines under 100
# characters; they are filled in below, so the text parsed is the full one.
I180_TRACEBACK = r"""Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "<REPO>\.scio\live-wt\scio\__main__.py", line 5, in <module>
    raise SystemExit(main())
                     ^^^^^^
  File "<REPO>\.scio\live-wt\scio\cli.py", line 228, in _build
    outcome = run_build(ctx, gh_repo=gh_repo)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<REPO>\.venv\Lib\site-packages\langgraph\pregel\main.py", line 2600, in invoke
    for chunk in self.stream(
  File "<REPO>\.scio\live-wt\scio\product\build.py", line 700, in plan_milestone
    plan = planner.call(ctx, spec)
  File "<REPO>\.scio\live-wt\scio\harness\providers\claude_code.py", line 89, in run
    res = subprocess.run(argv, input=prompt, capture_output=True, timeout=timeout_s)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Program Files\Python312\Lib\subprocess.py", line 550, in run
    stdout, stderr = process.communicate(input, timeout=timeout)
  File "C:\Program Files\Python312\Lib\subprocess.py", line 1628, in _communicate
    raise TimeoutExpired(self.args, orig_timeout)
subprocess.TimeoutExpired: <MESSAGE>
""".replace("<REPO>", REPO).replace("<MESSAGE>", I180_MESSAGE)

TOOL_CRASH_LOG = r"""tool run_tests crashed
Traceback (most recent call last):
  File "<REPO>\.scio\live-wt\scio\aci\toolbox.py", line 180, in call
    return fn(**args)
ValueError: bad argument
""".replace("<REPO>", REPO)

BUILD_HEADER = "build thread build-p-m1 phase=halted exit={exit} dir C:/ws/product"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "(missing)"


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        write(path / rel, text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def make_db(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.close()


def rows(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT x FROM t ORDER BY rowid")]
    finally:
        con.close()


def wal_writer(path: Path) -> sqlite3.Connection:
    """A writer like SCIO's: WAL mode, and no checkpoint, so committed rows stay in the WAL."""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    return con


def run_record(tmp_path: Path, **files: str) -> RunRecord:
    """A finished run whose stdout, stderr and outcome hold ``files``; a key that is not
    given leaves that file absent."""
    run_dir = tmp_path / "runs" / "run-1"
    paths = {name: run_dir / f"{name}.txt" for name in ("stdout", "stderr", "outcome")}
    for name, text in files.items():
        write(paths[name], text)
    return RunRecord(
        run_id="run-1",
        workspace=str(tmp_path / "ws"),
        launcher=["sh", "scio.sh"],
        argv=["build", "--product", "product"],
        env={},
        started_at=0.0,
        status=RunStatus.EXITED,
        exit_code=1,
        run_dir=str(run_dir),
        stdout_path=str(paths["stdout"]),
        stderr_path=str(paths["stderr"]),
        outcome_path=str(paths["outcome"]),
    )


def outcome(**fields: Any) -> str:
    base: dict[str, Any] = {
        "version": 1,
        "command": "build",
        "status": "halted",
        "exit_code": 1,
        "thread": "build-p-m1",
        "exit_reason": None,
        "reason": None,
        "resume": None,
        "exception": None,
    }
    base.update(fields)
    return json.dumps(base)


# --- tail ---


def test_tail_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert tail(tmp_path / "nope.txt") == ""


def test_tail_of_a_small_file_is_all_of_it(tmp_path: Path) -> None:
    assert tail(write(tmp_path / "a.txt", "one\ntwo\n")) == "one\ntwo\n"


def test_tail_starts_at_a_line_boundary(tmp_path: Path) -> None:
    lines = [f"line {i:04d} {'x' * (i % 7)}" for i in range(500)]
    path = write(tmp_path / "a.txt", "\n".join(lines) + "\n")
    out = tail(path, max_bytes=1000)
    assert 0 < len(out.encode()) <= 1000
    assert out.splitlines()[0] in lines
    assert (path.read_text(encoding="utf-8")).endswith(out)


def test_tail_keeps_a_first_line_that_starts_exactly_at_the_offset(tmp_path: Path) -> None:
    path = write(tmp_path / "a.txt", "aaaa\nbbbb\ncccc\n")
    assert tail(path, max_bytes=10) == "bbbb\ncccc\n"
    assert tail(path, max_bytes=9) == "cccc\n"


def test_tail_of_one_line_longer_than_the_window_is_empty(tmp_path: Path) -> None:
    assert tail(write(tmp_path / "a.txt", "x" * 100), max_bytes=10) == ""


def test_tail_normalises_line_endings_decodes_with_replacement_and_scrubs(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo \xff\r\nkey " + ANTHROPIC.encode() + b"\r\n")
    assert tail(path) == f"one\ntwo \ufffd\nkey {REDACTED}\n"


# --- read_outcome ---


def test_read_outcome_reads_a_json_object_and_nothing_else(tmp_path: Path) -> None:
    assert read_outcome(tmp_path / "missing.json") is None
    assert read_outcome(write(tmp_path / "bad.json", "{not json")) is None
    assert read_outcome(write(tmp_path / "list.json", "[1, 2]")) is None
    assert read_outcome(write(tmp_path / "ok.json", outcome(status="done"))) == json.loads(
        outcome(status="done")
    )


# --- parse_traceback ---


def test_parse_the_i180_traceback() -> None:
    exc_type, message, frames = parse_traceback("some log line\n" + I180_TRACEBACK)
    assert exc_type == "subprocess.TimeoutExpired"
    assert message == I180_MESSAGE
    assert [f.function for f in frames] == [
        "_run_module_as_main",
        "_run_code",
        "<module>",
        "_build",
        "invoke",
        "plan_milestone",
        "run",
        "run",
        "_communicate",
    ]
    assert frames[6].path == LIVE_WT + r"\scio\harness\providers\claude_code.py"
    assert frames[6].line == 89
    assert frames[-1].path == r"C:\Program Files\Python312\Lib\subprocess.py"


def test_parse_keeps_the_last_traceback_of_a_chain() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "/srv/scio/scio/a.py", line 1, in first\n'
        "KeyError: 'x'\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/scio/scio/b.py", line 2, in second\n'
        "    raise ValueError(msg)\n"
        "ValueError: second failure\n"
        "  with a second line\n"
    )
    exc_type, message, frames = parse_traceback(text)
    assert (exc_type, [f.function for f in frames]) == ("ValueError", ["second"])
    assert message == "second failure\n  with a second line"
    assert frames[0].path == "/srv/scio/scio/b.py"


def test_parse_a_traceback_whose_header_the_tail_cut_off() -> None:
    cut = I180_TRACEBACK.split("\n", 9)[-1]  # starts inside the frames
    assert "Traceback" not in cut
    exc_type, message, frames = parse_traceback(cut)
    assert exc_type == "subprocess.TimeoutExpired" and message == I180_MESSAGE
    assert frames[-1].function == "_communicate" and len(frames) == 5


def test_parse_an_exception_without_a_message() -> None:
    text = 'Traceback (most recent call last):\n  File "/x/scio/cli.py", line 3, in main\n'
    assert parse_traceback(text + "KeyboardInterrupt\n") == (
        "KeyboardInterrupt",
        "",
        parse_traceback(text + "KeyboardInterrupt\n")[2],
    )
    assert parse_traceback(text + "KeyboardInterrupt\n")[2][0].function == "main"


def test_parse_finds_nothing_in_text_without_a_traceback() -> None:
    assert parse_traceback("retry 1/3 after 2.0s: rate limit\nall good\n") == ("", "", [])
    assert parse_traceback("") == ("", "", [])


# --- read_evidence ---


def test_an_outcome_file_is_read_first_and_scrubbed(tmp_path: Path) -> None:
    frames = [
        {"path": LIVE_WT + r"\scio\cli.py", "line": 228, "function": "_build"},
        {
            "path": LIVE_WT + r"\scio\harness\providers\claude_code.py",
            "line": 89,
            "function": "run",
        },
        {"path": r"C:\Program Files\Python312\Lib\subprocess.py", "line": 1628, "function": "_x"},
    ]
    exc = {"type": "subprocess.TimeoutExpired", "message": I180_MESSAGE, "frames": frames}
    text = outcome(status="crashed", exception=exc, reason=f"key {ANTHROPIC}")
    # stdout claims a pause: the outcome file wins over the text
    run = run_record(tmp_path, outcome=text, stdout="PAUSED for a human:\n", stderr="noise\n")
    ev = read_evidence(run)
    assert ev.outcome is not None and ev.outcome["status"] == "crashed"
    assert ev.paused is False
    assert ev.exception_type == "subprocess.TimeoutExpired"
    assert ev.exception_message == I180_MESSAGE
    assert [f.line for f in ev.frames] == [228, 89, 1628]
    assert ev.last_ledger_line == f"key {REDACTED}"
    assert ANTHROPIC not in json.dumps(ev.outcome)
    c = classify(ev)
    assert (c.failure_class, c.innermost_scio_frame) == (FailureClass.HARNESS_SUSPECT, CLAUDE_CODE)


def test_an_outcome_file_gives_a_pause_and_a_github_stop(tmp_path: Path) -> None:
    paused = run_record(tmp_path / "a", outcome=outcome(status="paused", exit_code=0))
    paused.exit_code = 0
    assert read_evidence(paused).paused is True
    assert classify(read_evidence(paused)).failure_class is FailureClass.HUMAN_STOP
    stop = "scio build stopped on GitHub: gh: HTTP 502"
    github = run_record(tmp_path / "b", outcome=outcome(exit_reason="github", reason=stop))
    ev = read_evidence(github)
    assert (ev.halt_reason, ev.last_ledger_line) == ("github", stop)
    assert classify(ev).failure_class is FailureClass.EXTERNAL_TRANSIENT


def test_a_crash_outcome_without_an_exception_falls_back_to_stderr(tmp_path: Path) -> None:
    run = run_record(tmp_path, outcome=outcome(status="crashed"), stderr=I180_TRACEBACK)
    ev = read_evidence(run)
    assert ev.exception_type == "subprocess.TimeoutExpired" and len(ev.frames) == 9


def test_a_pause_from_the_text_when_there_is_no_outcome_file(tmp_path: Path) -> None:
    stdout = BUILD_HEADER.format(exit="None") + '\nPAUSED for a human:\n{"stop": "merge"}\n'
    stdout += 'resume with: scio build --product p --thread t --decision "<text>"\n'
    run = run_record(tmp_path, stdout=stdout)
    run.exit_code = 0
    ev = read_evidence(run)
    assert (ev.paused, ev.halt_reason, ev.outcome) == (True, None, None)
    assert classify(ev).failure_class is FailureClass.HUMAN_STOP


def test_a_pause_shows_by_its_resume_hint_when_a_long_interrupt_cut_the_marker(
    tmp_path: Path,
) -> None:
    stdout = '  "diff": "..."\n}\nresume with: scio build --product p --thread t --decision "x"\n'
    assert read_evidence(run_record(tmp_path, stdout=stdout)).paused is True


def test_a_halt_from_the_header_and_the_reason_line(tmp_path: Path) -> None:
    stdout = (
        BUILD_HEADER.format(exit="integration_failed") + "\n"
        "milestones done - · current M1 calc\n"
        "  F1 a1 T1 passed exit=success $0.1000 escalations=0/0\n"
        "usd 0.1000 (orchestrator 0.0100)\n"
        "reason: integration failed: 2 tests failed\n"
        "  test_add\n"
        "release v0.1.0 FAILED: not tagged\n"
        "the release report\n"
    )
    ev = read_evidence(run_record(tmp_path, stdout=stdout, stderr=TOOL_CRASH_LOG))
    assert ev.halt_reason == "integration_failed"
    assert ev.last_ledger_line == "integration failed: 2 tests failed\n  test_add"
    assert ev.frames == []  # SCIO printed its summary: the logged traceback did not end it
    assert classify(ev).failure_class is FailureClass.TASK_FAULT


def test_a_feature_human_stop_with_a_logged_tool_crash_stays_a_task_fault(tmp_path: Path) -> None:
    stdout = BUILD_HEADER.format(exit="feature_failed") + "\n"
    stdout += "reason: F1 failed (the owner stopped it); build stopped\n"
    ev = read_evidence(run_record(tmp_path, stdout=stdout, stderr=TOOL_CRASH_LOG))
    assert (ev.halt_reason, ev.exception_type) == ("feature_failed", "")
    assert classify(ev).failure_class is FailureClass.TASK_FAULT


def test_a_feature_failure_line_names_its_halt_when_the_tail_cut_the_header(
    tmp_path: Path,
) -> None:
    line = "F1 attempt 2 failed: HarnessError: boom; the next `scio build` starts attempt 3"
    ev = read_evidence(run_record(tmp_path, stdout=f"reason: {line}\n"))
    assert (ev.halt_reason, ev.last_ledger_line) == ("feature_failed", line)
    assert classify(ev).failure_class is FailureClass.HARNESS_SUSPECT


def test_a_traceback_from_stderr_when_scio_wrote_nothing_else(tmp_path: Path) -> None:
    ev = read_evidence(run_record(tmp_path, stderr="starting\n" + I180_TRACEBACK))
    assert ev.exception_type == "subprocess.TimeoutExpired"
    assert ev.exception_message == I180_MESSAGE
    c = classify(ev)
    assert (c.failure_class, c.innermost_scio_frame) == (FailureClass.HARNESS_SUSPECT, CLAUDE_CODE)


def test_a_traceback_that_is_not_the_end_of_stderr_did_not_end_the_run(tmp_path: Path) -> None:
    ev = read_evidence(run_record(tmp_path, stderr=TOOL_CRASH_LOG + "\nthe run went on\n"))
    assert (ev.exception_type, ev.frames) == ("", [])


def test_hung_and_orphaned_come_from_the_runs_status(tmp_path: Path) -> None:
    run = run_record(tmp_path)
    run.exit_code = None
    run.status = RunStatus.KILLED_HANG
    assert (read_evidence(run).hung, read_evidence(run).orphaned) == (True, False)
    assert classify(read_evidence(run)).failure_class is FailureClass.HANG
    run.status = RunStatus.ORPHANED
    assert (read_evidence(run).hung, read_evidence(run).orphaned) == (False, True)
    assert classify(read_evidence(run)).failure_class is FailureClass.ORPHAN
    run.status = RunStatus.EXITED
    assert (read_evidence(run).hung, read_evidence(run).orphaned) == (False, False)


def test_an_orphan_whose_child_wrote_its_outcome_is_read_from_it(tmp_path: Path) -> None:
    """The supervisor died, and the child finished on its own: the outcome file says how."""
    run = run_record(tmp_path, outcome=outcome(status="done", exit_code=0))
    run.status, run.exit_code = RunStatus.ORPHANED, None
    ev = read_evidence(run)
    assert (ev.orphaned, ev.exit_code) == (False, 0)
    assert classify(ev).failure_class is FailureClass.DONE


def test_a_run_with_no_files_gives_empty_evidence(tmp_path: Path) -> None:
    ev = read_evidence(run_record(tmp_path))
    assert (ev.stdout_tail, ev.stderr_tail, ev.outcome, ev.frames) == ("", "", None, [])


# --- backup_sqlite ---


def test_backup_while_a_writer_holds_the_wal_open(tmp_path: Path) -> None:
    src, dst = tmp_path / "src.sqlite", tmp_path / "copy" / "dst.sqlite"
    make_db(src, ["a"])
    writer = wal_writer(src)
    try:
        writer.execute("BEGIN")
        writer.execute("INSERT INTO t VALUES ('uncommitted')")
        before = (sha(src), sha(Path(f"{src}-wal")))
        assert backup_sqlite(src, dst) is True
        assert (sha(src), sha(Path(f"{src}-wal"))) == before
        assert rows(dst) == ["a"]
        writer.execute("COMMIT")
        before = (sha(src), sha(Path(f"{src}-wal")))
        assert backup_sqlite(src, dst) is True  # over the earlier copy
        assert (sha(src), sha(Path(f"{src}-wal"))) == before
        assert rows(dst) == ["a", "uncommitted"]
    finally:
        writer.close()
    assert not list(dst.parent.glob("*.tmp"))


def test_backup_after_the_writer_died_leaves_its_wal_in_place(tmp_path: Path) -> None:
    """The writer exited without a checkpoint. A read-write connection that closed last
    would fold the WAL into the main file and delete it, writing the original."""
    src = tmp_path / "src.sqlite"
    script = tmp_path / "writer.py"
    write(
        script,
        "import os, sqlite3, sys\n"
        "con = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "con.execute('PRAGMA journal_mode=WAL')\n"
        "con.execute('PRAGMA wal_autocheckpoint=0')\n"
        "con.execute('CREATE TABLE t(x TEXT)')\n"
        "con.execute(\"INSERT INTO t VALUES ('a')\")\n"
        "os._exit(0)\n",
    )
    subprocess.run([sys.executable, str(script), str(src)], check=True, timeout=60)
    wal = Path(f"{src}-wal")
    assert wal.exists()
    before = (sha(src), sha(wal))
    assert backup_sqlite(src, tmp_path / "dst.sqlite") is True
    assert (sha(src), sha(wal)) == before
    assert rows(tmp_path / "dst.sqlite") == ["a"]


def test_backup_of_a_missing_database_is_false(tmp_path: Path) -> None:
    assert backup_sqlite(tmp_path / "nope.sqlite", tmp_path / "dst.sqlite") is False
    assert not (tmp_path / "dst.sqlite").exists()


# --- snapshot and fingerprints ---


THREAD = "build-p-m1"


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[WorkspaceConfig, RunRecord]:
    ws = tmp_path / "ws"
    scio = ws / ".scio"
    make_db(scio / "checkpoints.sqlite", ["c1"])
    make_db(scio / "effects.sqlite", ["e1"])
    write(scio / "ledger" / f"{THREAD}.jsonl", '{"usd": 0.1}\n')
    write(scio / "ledger" / f"{THREAD}-p1-a1.jsonl", '{"usd": 0.2}\n')
    write(scio / "ledger" / "build-q-m1.jsonl", '{"usd": 9}\n')
    product = git_repo(ws / "product", {"README.md": "# p\n", ".gitignore": ".scio/\n"})
    git(product, "branch", "milestone/m1")
    git(product, "branch", "agent/f1-a1")
    write(product / ".scio" / "product" / "progress.json", '{"product": "p"}\n')
    live_wt = git_repo(tmp_path / "live-wt", {"x.py": "x = 1\n"})
    cfg = WorkspaceConfig(
        workspace=ws,
        launcher=("sh", "scio.sh"),
        scio_repo=tmp_path / "SCIO",
        scio_gh_repo="o/r",
        live_wt=live_wt,
    )
    run = run_record(
        tmp_path,
        stdout=f"build thread {THREAD} phase=halted exit=feature_failed dir product\n",
        stderr=f"ANTHROPIC_API_KEY={ANTHROPIC}\n" + I180_TRACEBACK,
        outcome=outcome(status="crashed"),
    )
    run.product_dir, run.build_thread = "product", THREAD
    return cfg, run


def originals(cfg: WorkspaceConfig) -> dict[str, str]:
    """The workspace's data files. Not SQLite's ``-shm`` index or an empty ``-wal``: a
    read-only reader of a cleanly closed WAL database creates both, and they hold no data."""
    ws = cfg.workspace
    files = [
        p
        for p in sorted((ws / ".scio").rglob("*"))
        if p.is_file()
        and not p.name.endswith("-shm")
        and not (p.name.endswith("-wal") and p.stat().st_size == 0)
    ]
    files += [ws / "product" / ".git" / "index", ws / "product" / ".git" / "HEAD"]
    files.append(ws / "product" / ".scio" / "product" / "progress.json")
    return {str(p.relative_to(ws)): sha(p) for p in files}


def test_snapshot_copies_the_failed_runs_state_and_writes_no_original(
    workspace: tuple[WorkspaceConfig, RunRecord], tmp_path: Path
) -> None:
    cfg, run = workspace
    before = originals(cfg)
    prints = fingerprints(run, cfg)
    dest = tmp_path / "incidents" / "inc-1"
    manifest = snapshot(run, cfg, dest)
    assert originals(cfg) == before
    assert fingerprints(run, cfg) == prints
    assert sorted(manifest) == [
        "checkpoints",
        "effects",
        "git",
        f"ledger:{THREAD}-p1-a1.jsonl",
        f"ledger:{THREAD}.jsonl",
        "outcome",
        "progress",
        "stderr",
        "stdout",
    ]
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert rows(Path(manifest["checkpoints"])) == ["c1"]
    assert rows(Path(manifest["effects"])) == ["e1"]
    assert Path(manifest["progress"]).read_text(encoding="utf-8") == '{"product": "p"}\n'
    report = Path(manifest["git"]).read_text(encoding="utf-8")
    head = git(cfg.workspace / "product", "rev-parse", "HEAD")
    assert head in report and "main" in report
    assert f"milestone/m1 {head}" in report and f"agent/f1-a1 {head}" in report
    stderr = Path(manifest["stderr"]).read_text(encoding="utf-8")
    assert ANTHROPIC not in stderr and "subprocess.TimeoutExpired" in stderr
    assert Path(manifest[f"ledger:{THREAD}.jsonl"]).read_text(encoding="utf-8") == '{"usd": 0.1}\n'
    assert snapshot(run, cfg, dest) == manifest  # twice: the same result
    assert originals(cfg) == before


def test_snapshot_skips_what_is_missing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = WorkspaceConfig(ws, ("sh", "scio.sh"), tmp_path, "o/r", tmp_path / "live-wt")
    manifest = snapshot(run_record(tmp_path), cfg, tmp_path / "dest")
    assert manifest == {}
    assert json.loads((tmp_path / "dest" / "manifest.json").read_text(encoding="utf-8")) == {}


def test_snapshot_records_a_database_it_cannot_copy(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    write(ws / ".scio" / "checkpoints.sqlite", "this is not a database, it is long enough " * 4)
    cfg = WorkspaceConfig(ws, ("sh", "scio.sh"), tmp_path, "o/r", tmp_path / "live-wt")
    manifest = snapshot(run_record(tmp_path), cfg, tmp_path / "dest")
    assert "checkpoints" not in manifest
    assert "checkpoints.sqlite" in Path(manifest["errors"]).read_text(encoding="utf-8")


def test_fingerprints_change_when_a_head_moves_or_a_database_changes(
    workspace: tuple[WorkspaceConfig, RunRecord],
) -> None:
    cfg, run = workspace
    first = fingerprints(run, cfg)
    assert first == fingerprints(run, cfg)
    product = cfg.workspace / "product"
    assert first["product_head"] == git(product, "rev-parse", "HEAD")
    assert first["product_branch"] == "main"
    assert first["live_wt_head"] == git(cfg.live_wt, "rev-parse", "HEAD")

    write(product / "README.md", "# p, changed\n")
    git(product, "commit", "-q", "-am", "moved")
    moved = fingerprints(run, cfg)
    assert moved["product_head"] != first["product_head"]
    assert moved["checkpoints_sha256"] == first["checkpoints_sha256"]

    writer = wal_writer(cfg.workspace / ".scio" / "effects.sqlite")
    try:
        main_file = sha(cfg.workspace / ".scio" / "effects.sqlite")
        writer.execute("INSERT INTO t VALUES ('e2')")  # committed, still in the WAL
        assert sha(cfg.workspace / ".scio" / "effects.sqlite") == main_file
        changed = fingerprints(run, cfg)
    finally:
        writer.close()
    assert changed["effects_sha256"] != moved["effects_sha256"]
    assert changed["checkpoints_sha256"] == moved["checkpoints_sha256"]

    write(cfg.live_wt / "x.py", "x = 2\n")
    git(cfg.live_wt, "commit", "-q", "-am", "moved")
    assert fingerprints(run, cfg)["live_wt_head"] != first["live_wt_head"]


def test_fingerprints_mark_what_is_missing(tmp_path: Path) -> None:
    cfg = WorkspaceConfig(tmp_path / "ws", ("sh",), tmp_path, "o/r", tmp_path / "no-wt")
    found = fingerprints(run_record(tmp_path), cfg)
    assert set(found.values()) == {"(missing)"}
    assert sorted(found) == [
        "checkpoints_sha256",
        "effects_sha256",
        "live_wt_head",
        "product_branch",
        "product_head",
    ]


def test_manifest_paths_are_in_a_stable_order() -> None:
    manifest = {"stdout": "/d/stdout.txt", "checkpoints": "/d/c.sqlite", "git": "/d/git.txt"}
    reordered = dict(reversed(list(manifest.items())))
    assert manifest_paths(manifest) == ["/d/c.sqlite", "/d/git.txt", "/d/stdout.txt"]
    assert manifest_paths(reordered) == manifest_paths(manifest)
