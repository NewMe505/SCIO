"""The supervisor (recovery/) and SCIO share no code, only command lines and a file shape.
These tests import both sides and check that what the supervisor sends, SCIO's own
parser accepts (I-184: the health check first called a `scio --version` SCIO never had),
and that what SCIO reports, the supervisor's classifier reads as meant (I-182)."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from recovery.classify import classify
from recovery.commands import CommandResult
from recovery.config import WorkspaceConfig
from recovery.model import Evidence, FailureClass, RunRecord
from recovery.runner import OUTCOME_SUBCOMMANDS
from recovery.runtime import SMOKE_ARGS, health_check
from scio.cli import HARNESS_HALT, _parser, harness_stop_line
from scio.harness.failures import (
    BudgetExceeded,
    FatalError,
    HarnessError,
    RetryableError,
    StructuredOutputError,
)
from scio.harness.providers.claude_code import claude_runner

LAUNCHER = ("sh", "scio.sh")


def scio_parses(args: Sequence[str]) -> int:
    """The exit code SCIO's argument parser gives ``args``: 0 when it accepts them
    (``--help`` exits 0), 2 when it refuses them."""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            _parser().parse_args(list(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


class ParsingLauncher:
    """A runner that, instead of running the launcher, hands its SCIO arguments to SCIO's
    real parser."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = list(argv[len(LAUNCHER) :])
        self.seen.append(args)
        code = scio_parses(args)
        return CommandResult(code, "", "" if code == 0 else f"scio refused {args}")


def test_the_parser_check_refuses_a_flag_scio_lacks() -> None:
    assert scio_parses(["--version"]) == 2
    assert scio_parses(list(SMOKE_ARGS)) == 0


@pytest.mark.parametrize(
    "args",
    [
        ["build", "--product", "P", "--gh", "o/p"],
        ["new", "--prompt", "an idea", "--dir", "D"],
        ["run", "--spec", "S.md", "--repo", "R"],
        ["resume", "--thread", "T"],
    ],
)
def test_the_outcome_flag_the_supervisor_appends_is_accepted(args: list[str]) -> None:
    assert args[0] in OUTCOME_SUBCOMMANDS
    assert scio_parses([*args, "--outcome-file", "outcome.json"]) == 0


def test_the_outcome_flag_goes_only_to_subcommands_that_take_it() -> None:
    assert set(OUTCOME_SUBCOMMANDS) == {"build", "new", "run", "resume"}
    assert scio_parses(["status", "--product", "P", "--outcome-file", "o.json"]) == 2


def test_every_health_check_command_is_one_scios_cli_accepts(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".scio").mkdir(parents=True)
    # A checkpoint database so step 3 (report on a copy) runs instead of being skipped.
    with sqlite3.connect(workspace / ".scio" / "checkpoints.sqlite") as conn:
        conn.execute("CREATE TABLE t (x)")
    cfg = WorkspaceConfig(
        workspace=workspace,
        launcher=LAUNCHER,
        scio_repo=tmp_path / "SCIO",
        scio_gh_repo="o/SCIO",
        live_wt=tmp_path / "SCIO" / ".scio" / "live-wt",
    )
    run = RunRecord(
        run_id="run-1",
        workspace=str(workspace),
        launcher=list(LAUNCHER),
        argv=["build", "--product", str(tmp_path / "product"), "--gh", "o/p"],
        env={},
        started_at=0.0,
        product_dir=str(tmp_path / "product"),
        build_thread="build-p-m1",
    )
    launcher = ParsingLauncher()
    problems = health_check(cfg, run, tmp_path / "health", runner=launcher)
    assert problems == []
    assert [args[0] for args in launcher.seen] == ["--help", "status", "report"]


def test_scios_harness_stops_land_in_the_classifier_rows_meant_for_them() -> None:
    """I-180, I-182, issue #105: SCIO's stop line and its ``claude`` timeout message, each
    made by SCIO's own code, read as the supervisor means them. A timeout the harness could
    not retry away is transient (replay after a backoff), an expired login is auth, a model
    that never met its schema is a SCIO suspect (repair), a spend cap is the task's."""
    run = claude_runner(sys.executable)
    with pytest.raises(RetryableError) as timed_out:
        run(["-c", "import time; time.sleep(30)"], timeout_s=0.5)
    cases: list[tuple[HarnessError, FailureClass]] = [
        (timed_out.value, FailureClass.EXTERNAL_TRANSIENT),
        (FatalError("claude -p failed: Not logged in (subtype=-)"), FailureClass.EXTERNAL_AUTH),
        (
            StructuredOutputError("milestone_planner: no valid answer", attempts=3, errors=["x"]),
            FailureClass.HARNESS_SUSPECT,
        ),
        (BudgetExceeded("spent", tokens_used=1, usd_used=1.0), FailureClass.TASK_FAULT),
    ]
    for exc, expected in cases:
        line = harness_stop_line("build", exc)
        ev = Evidence(exit_code=1, halt_reason=HARNESS_HALT, last_ledger_line=line)
        assert classify(ev).failure_class is expected, line
