"""The local CI-equivalent (CLAUDE.md), with its independent checks side by side (issue #88).

    uv run python scripts/gate.py

The same checks and thresholds as CI's ``checks`` and ``evals`` jobs:
1. ``ruff check``, ``ruff format --check``, ``mypy``, in order, stopping at the first failure;
2. ``pytest -n 16`` (the owner's decision, 2026-11-08) and the regression evals (5 trials,
   pass^5 = 1.00, trials run concurrently) at the same time: the tests wait on git and pytest
   subprocesses, the evals on the Fake, and the machine has cores to spare for both;
3. the docs check.

Every Python tool runs as ``python -m`` with this interpreter: Application Control blocks the
venv's launcher ``.exe`` files on the dev machine (I-075, I-146). Each step's output goes to a
log file under ``.scio/gate/<UTC stamp>/``, never a pipe; the gate prints each step's wall time
and the tail of a failed step's log, and exits 1 if any step failed.
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TEST_WORKERS = 16
"""The owner's decision, 2026-11-08 (issue #88); 8 since issue #73."""
EVAL_PARALLEL = 5
"""Trials of a task run at once (``evals.runner --parallel``); the 5 trials and the pass^5
threshold are unchanged."""

FIRST: list[tuple[str, list[str]]] = [
    ("ruff check", [PY, "-m", "ruff", "check", "."]),
    ("ruff format", [PY, "-m", "ruff", "format", "--check", "."]),
    ("mypy", [PY, "-m", "mypy"]),
]
TOGETHER: list[tuple[str, list[str]]] = [
    ("pytest", [PY, "-m", "pytest", "-n", str(TEST_WORKERS)]),
    (
        "evals",
        [
            PY,
            "-m",
            "evals.runner",
            "--suite",
            "regression",
            "--provider",
            "fake",
            "--trials",
            "5",
            "--min-pass-pow-k",
            "1.0",
            "--parallel",
            str(EVAL_PARALLEL),
        ],
    ),
]
LAST: list[tuple[str, list[str]]] = [("docs", [PY, "scripts/check_docs.py"])]


def steps() -> list[tuple[str, list[str]]]:
    """Every step, in the order the gate reports them."""
    return [*FIRST, *TOGETHER, *LAST]


@dataclass
class Step:
    name: str
    code: int
    seconds: float
    log: Path


def _run(name: str, argv: list[str], logs: Path) -> Step:
    log = logs / f"{name.replace(' ', '-')}.log"
    start = time.monotonic()
    with log.open("wb") as out:
        code = subprocess.run(argv, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
    return Step(name, code, time.monotonic() - start, log)


VERDICT_MARKS = ("pass^", " passed", " failed", "problem(s)", "Success:", "Found ")
"""Lines that state a step's verdict (pytest's count, the evals' pass^k, mypy's), shown in
place of the log's last line when there is one."""


def _report(step: Step) -> None:
    lines = step.log.read_text(encoding="utf-8", errors="replace").splitlines()
    last = next(
        (ln.strip() for ln in reversed(lines) if any(m in ln for m in VERDICT_MARKS)),
        next((ln.strip() for ln in reversed(lines) if ln.strip()), ""),
    )
    verdict = "ok  " if step.code == 0 else "FAIL"
    print(
        f"{verdict} {step.name:<12} {step.seconds / 60:5.1f} min  exit {step.code}  {last[:120]}",
        flush=True,
    )
    if step.code != 0:
        print("\n".join(f"    {line}" for line in lines[-40:]), flush=True)


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):  # a cp1252 console cannot print every log line
        sys.stdout.reconfigure(errors="replace")
    logs = ROOT / ".scio" / "gate" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logs.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    print(f"gate: logs in {logs}", flush=True)
    for name, argv in FIRST:
        step = _run(name, argv, logs)
        _report(step)
        if step.code != 0:
            return 1
    with ThreadPoolExecutor(max_workers=len(TOGETHER)) as pool:
        together = list(pool.map(lambda s: _run(s[0], s[1], logs), TOGETHER))
    for step in together:
        _report(step)
    failed = any(s.code != 0 for s in together)
    for name, argv in LAST:
        step = _run(name, argv, logs)
        _report(step)
        failed = failed or step.code != 0
    print(f"gate: {'FAILED' if failed else 'passed'} in {(time.monotonic() - start) / 60:.1f} min")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
