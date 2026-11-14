"""The supervisor (recovery/) and SCIO share no code, only command lines and a file shape.
These tests import both sides and check that what the supervisor sends, SCIO's own
parser accepts (I-184: the health check first called a `scio --version` SCIO never had)."""

from __future__ import annotations

import contextlib
import io
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from recovery.commands import CommandResult
from recovery.config import WorkspaceConfig
from recovery.model import RunRecord
from recovery.runner import OUTCOME_SUBCOMMANDS
from recovery.runtime import SMOKE_ARGS, health_check
from scio.cli import _parser

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
