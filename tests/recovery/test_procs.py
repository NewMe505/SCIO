"""Process identity and liveness, spawning to files, following output, detached starts and
the scio process scan (ADR 0019 decisions 2 and 4, section 12a)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from recovery import procs
from recovery.commands import CommandResult, kill_tree
from recovery.model import ProcessKey

ROOT = Path(__file__).resolve().parents[2]
SLEEPER = "import time; time.sleep(120)"


def _sleeper(*extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", SLEEPER, *extra],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until(condition: Callable[[], bool], timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return bool(condition())


# Identity and liveness


def test_this_process_has_a_key_and_is_alive_but_not_under_another_creation_time() -> None:
    key = procs.process_key(os.getpid())
    assert key is not None and key.pid == os.getpid()
    assert 0 < key.created <= time.time()
    assert procs.self_key().matches(key)
    assert procs.alive(procs.self_key())
    # the same PID with another creation time is a reused PID, not this process
    assert not procs.alive(ProcessKey(key.pid, key.created + 5.0))
    assert not procs.alive(ProcessKey(key.pid, key.created - 5.0))


def test_a_finished_child_is_not_alive_even_while_a_handle_keeps_it() -> None:
    before = time.time()
    child = _sleeper()
    try:
        key = procs.process_key(child.pid)
        assert key is not None
        assert before - 2.0 <= key.created <= time.time() + 1.0
        assert procs.alive(key)
    finally:
        child.kill()
        child.wait()
    # the Popen object still holds the process handle on Windows: the exit code tells
    assert procs.process_key(child.pid) is None
    assert not procs.alive(key)


@pytest.mark.parametrize("pid", [0, -1, 2**40])
def test_an_impossible_pid_has_no_key(pid: int) -> None:
    assert procs.process_key(pid) is None
    assert not procs.alive(ProcessKey(pid, 1.0))


def test_a_filetime_converts_to_epoch_seconds() -> None:
    assert procs.filetime_to_epoch(procs.FILETIME_EPOCH) == 0.0
    assert procs.filetime_to_epoch(procs.FILETIME_EPOCH + 15 * 10**7) == 15.0


def test_the_proc_stat_parser_counts_fields_after_the_command_name() -> None:
    # field 22 (start time) is 500 ticks; the command name holds spaces and a parenthesis
    fields = ["S", *[str(n) for n in range(4, 22)], "500", "0"]
    line = "42 (my (odd) name) " + " ".join(fields)
    assert procs.proc_stat_created(line, 1000.0, 100) == 1005.0
    zombie = line.replace(") S ", ") Z ", 1)
    assert procs.proc_stat_created(zombie, 1000.0, 100) is None


def test_the_ps_parser_reads_lstart_and_refuses_a_zombie() -> None:
    expected = time.mktime(time.strptime("Wed Nov  4 10:00:00 2026", "%a %b %d %H:%M:%S %Y"))
    assert procs.ps_created("Ss   Wed Nov  4 10:00:00 2026\n") == expected
    assert procs.ps_created("Z    Wed Nov  4 10:00:00 2026") is None
    assert procs.ps_created("") is None
    assert procs.ps_created("S garbage") is None


# Spawning and following


def test_spawn_writes_output_to_files_with_stdin_closed(tmp_path: Path) -> None:
    script = "import sys; print('out:' + sys.stdin.read()); print('err', file=sys.stderr)"
    out, err = tmp_path / "logs" / "stdout.log", tmp_path / "logs" / "stderr.log"
    popen = procs.spawn(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        stdout_path=out,
        stderr_path=err,
    )
    assert popen.wait(timeout=60) == 0
    assert out.read_text(encoding="utf-8").strip() == "out:"
    assert err.read_text(encoding="utf-8").strip() == "err"


def _program_only_on(bin_dir: Path) -> str:
    """A program that prints ``found-on-env-path`` and lives only in ``bin_dir``."""
    bin_dir.mkdir()
    name = "scio-recovery-env-path-probe"
    if sys.platform == "win32":
        (bin_dir / f"{name}.cmd").write_text(
            "@echo found-on-env-path\n", encoding="utf-8", newline="\n"
        )
    else:
        path = bin_dir / name
        path.write_text("#!/bin/sh\necho found-on-env-path\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)
    return name


def test_spawn_finds_its_program_on_the_child_env_path_only(tmp_path: Path) -> None:
    """CreateProcess searches the parent's PATH, not the env's (I-183)."""
    name = _program_only_on(tmp_path / "bin")
    env = dict(os.environ)
    env["PATH"] = str(tmp_path / "bin") + os.pathsep + env.get("PATH", "")
    out, err = tmp_path / "out.log", tmp_path / "err.log"
    popen = procs.spawn([name], cwd=tmp_path, env=env, stdout_path=out, stderr_path=err)
    assert popen.wait(timeout=60) == 0
    assert "found-on-env-path" in out.read_text(encoding="utf-8")


def test_spawn_detached_finds_its_program_on_the_child_env_path_only(tmp_path: Path) -> None:
    name = _program_only_on(tmp_path / "bin")
    env = dict(os.environ)
    env["PATH"] = str(tmp_path / "bin") + os.pathsep + env.get("PATH", "")
    log = tmp_path / "logs" / "detached.log"
    pid = procs.spawn_detached([name], cwd=tmp_path, env=env, log_path=log)
    assert pid > 0
    assert _wait_until(lambda: "found-on-env-path" in log.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", 0),
        (b"abc", 3),
        (b"a\xe2\x9c", 1),  # an incomplete 3-byte character waits
        (b"a\xe2\x9c\x93", 4),
        (b"\xf0\x9f\x98", 0),
        (b"a\xc3", 1),
        (b"line\r", 4),  # a final CR waits for its LF
        (b"line\r\n", 6),
        (b"\x80\x80\x80\x80", 4),  # stray continuation bytes are not held back
    ],
)
def test_complete_prefix_holds_back_a_partial_character_and_a_final_cr(
    data: bytes, expected: int
) -> None:
    assert procs.complete_prefix(data) == expected


def test_follow_never_splits_a_character_or_a_crlf_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    sink = io.StringIO()
    path.write_bytes(b"ok \xe2\x9c")
    offsets = procs.follow([(path, sink)], [0])
    assert (sink.getvalue(), offsets) == ("ok ", [3])
    with path.open("ab") as fh:
        fh.write(b"\x93 done\r")
    offsets = procs.follow([(path, sink)], offsets)
    assert sink.getvalue() == "ok \u2713 done"
    with path.open("ab") as fh:
        fh.write(b"\nnext \xff\n")
    offsets = procs.follow([(path, sink)], offsets)
    assert sink.getvalue() == "ok \u2713 done\nnext \ufffd\n"
    assert offsets == [path.stat().st_size]
    # nothing new: nothing written
    assert procs.follow([(path, sink)], offsets) == offsets
    assert sink.getvalue().count("next") == 1


def test_follow_keeps_the_offset_of_a_missing_file_and_survives_a_bad_stream(
    tmp_path: Path,
) -> None:
    sink = io.StringIO()
    assert procs.follow([(tmp_path / "absent.log", sink)], [7]) == [7]
    path = tmp_path / "out.log"
    path.write_bytes("check \u2713\n".encode())
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")
    assert procs.follow([(path, narrow)], [0]) == [path.stat().st_size]
    narrow.seek(0)
    assert narrow.buffer.read() == b"check ?\n"
    closed = io.StringIO()
    closed.close()
    assert procs.follow([(path, closed)], [0]) == [path.stat().st_size]


# Detached starts

PARENT = """
import os
import sys
import time
from pathlib import Path

from recovery import procs

log = Path(sys.argv[1])
pid = procs.spawn_detached(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    cwd=log.parent,
    env=dict(os.environ),
    log_path=log,
)
print(pid, flush=True)
time.sleep(120)
"""


def test_a_detached_child_outlives_its_killed_parent(tmp_path: Path) -> None:
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    parent = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "detached.log")],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    child_pid = 0
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        child = procs.process_key(child_pid)
        assert child is not None
        # kill the parent alone: kill_tree would take the child with it
        parent.kill()
        parent.wait(timeout=60)
        time.sleep(0.5)
        assert procs.alive(child), "the detached child died with its parent"
    finally:
        parent.kill()
        parent.wait(timeout=60)
        if parent.stdout is not None:
            parent.stdout.close()
        if child_pid:
            kill_tree(child_pid)
    assert _wait_until(lambda: not procs.alive(child))


# The scio process scan


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (r"C:\Python312\python.exe -m scio build --product P --gh o/p", True),
        (r'"C:\Program Files\Python\python.exe" -m scio status --product P', True),
        ("uv run --project C:/SCIO --no-sync python -m scio build --product P", True),
        ("python -m scio", True),
        ("python -m scio.cli build", True),
        ('python -m "scio" resume --thread t', True),
        ("python -m scio dashboard --port 8765", False),
        ('python -m scio "dashboard"', False),
        ("python -m scio_x build", False),
        ("python -m scioextra build", False),
        ("python -m recovery run --workspace W -- build --product P", False),
        ("sh scio.sh build --product P", False),
        ("python scio/cli.py build", False),
        ("", False),
        (None, False),
    ],
)
def test_the_scio_matcher_takes_the_module_and_skips_the_dashboard(
    line: str | None, expected: bool
) -> None:
    assert procs.is_scio_command(line) is expected


def test_the_process_list_parsers_read_cim_json_and_ps_output() -> None:
    one = json.dumps({"ProcessId": 7, "CommandLine": "python -m scio build"})
    many = json.dumps(
        [{"ProcessId": 1, "CommandLine": None}, {"ProcessId": 2, "CommandLine": "x -m scio"}]
    )
    assert procs.parse_cim_processes(one) == [(7, "python -m scio build")]
    assert procs.parse_cim_processes(many) == [(1, ""), (2, "x -m scio")]
    assert procs.parse_cim_processes("  ") == []
    ps = "    1 /sbin/init\n  42 python -m scio build --product P\n"
    assert procs.parse_ps_processes(ps) == [
        (1, "/sbin/init"),
        (42, "python -m scio build --product P"),
    ]
    rows = [(1, "python -m scio build"), (2, "python -m scio dashboard"), (3, "python -m scio")]
    assert procs.scio_pids(rows, exclude=3) == [1]


class _CannedRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(list(argv))
        return self.result


def _listing(rows: list[tuple[int, str]]) -> str:
    if sys.platform == "win32":
        return json.dumps([{"ProcessId": p, "CommandLine": c} for p, c in rows])
    return "".join(f"{p:>6} {c}\n" for p, c in rows)


def test_scio_processes_reads_the_platform_listing_and_excludes_itself() -> None:
    rows = [
        (101, "python -m scio build --product P"),
        (102, "python -m scio dashboard"),
        (os.getpid(), "python -m scio build"),
        (103, "python -m pytest"),
    ]
    runner = _CannedRunner(CommandResult(0, _listing(rows), ""))
    assert procs.scio_processes(runner=runner) == [101]
    head = runner.calls[0][0]
    assert head == ("powershell" if sys.platform == "win32" else "ps")


def test_scio_processes_raises_when_the_listing_cannot_be_read() -> None:
    with pytest.raises(RuntimeError, match="cannot list"):
        procs.scio_processes(runner=_CannedRunner(CommandResult(1, "", "denied")))
    if sys.platform == "win32":
        with pytest.raises(RuntimeError, match="cannot read"):
            procs.scio_processes(runner=_CannedRunner(CommandResult(0, "{not json", "")))


def test_scio_processes_finds_a_real_scio_command_line_and_not_a_dashboard() -> None:
    # python -c ignores what follows, so these only look like scio processes
    build = _sleeper("-m", "scio", "build", "--product", "P")
    dashboard = _sleeper("-m", "scio", "dashboard")
    try:
        found = procs.scio_processes()
    finally:
        for p in (build, dashboard):
            p.kill()
            p.wait()
    assert build.pid in found
    assert dashboard.pid not in found
    assert os.getpid() not in found
