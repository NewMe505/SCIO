"""``python -m recovery``: run, tick, status, incidents, hold, release. OWNER: wave-1 agent H.

``run`` exits with the child's own exit code, so a session that called ``sh scio.sh
build ...`` before sees the same status and output under the supervisor. Every other
error prints one line and returns 2, never a traceback.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from recovery import config, history, machine, procs, runner
from recovery.config import WorkspaceConfig
from recovery.model import Incident, IncidentState, RunRecord, RunStatus
from recovery.store import Store

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
"""The directory that holds ``recovery/``: a background tick starts from here."""

STATUS_RUNS = 20
"""How many runs ``status`` lists."""
STATUS_ESCALATED = 10


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m recovery",
        description="SCIO's external recovery supervisor (ADR 0019).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="run the workspace's SCIO launcher as a supervised child",
        description=(
            "Runs <launcher> ARGS in the workspace, echoes its output, and exits with its "
            "code. A failure opens or joins an incident, and a detached tick carries the "
            "recovery on."
        ),
    )
    run.add_argument("--workspace", required=True, help="directory holding recovery.toml")
    run.add_argument("--incident", help="the incident this run resumes (set by a replay)")
    run.add_argument("--resumed-from", help="the run id this run replays (set by a replay)")
    run.add_argument("args", nargs=argparse.REMAINDER, help="-- then the SCIO arguments")

    tick = sub.add_parser("tick", help="reconcile orphans and advance every open incident")
    tick.add_argument("--incident", help="advance only this incident")

    status = sub.add_parser("status", help="runs and incidents, with their log paths")
    status.add_argument("--workspace")

    inc = sub.add_parser("incidents", help="search incident history")
    inc.add_argument("--signature")
    inc.add_argument("--grep")
    inc.add_argument("--limit", type=int, default=20)

    hold = sub.add_parser("hold", help="pause an incident's automation")
    hold.add_argument("incident")
    rel = sub.add_parser("release", help="resume a held incident")
    rel.add_argument("incident")
    return p


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _home() -> Path:
    return config.recovery_home()


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _when(stamp: float | None) -> str:
    if not stamp:
        return "-"
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# run


def _start_tick(incident: Incident, home: Path) -> int:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    log = home / "ticks" / f"{incident.incident_id}-{stamp}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env[config.HOME_ENV] = str(home)
    return procs.spawn_detached(
        [sys.executable, "-m", "recovery", "tick", "--incident", incident.incident_id],
        cwd=PACKAGE_ROOT,
        env=env,
        log_path=log,
    )


def _run(args: argparse.Namespace) -> int:
    scio_args = list(args.args)
    if scio_args and scio_args[0] == "--":
        scio_args = scio_args[1:]
    if not scio_args:
        _err("recovery run: give the SCIO arguments after --, e.g. -- build --product P")
        return 2
    workspace = Path(args.workspace)
    try:
        cfg = WorkspaceConfig.load(workspace)
    except FileNotFoundError:
        _err(f"recovery run: no recovery.toml in {workspace.resolve()}")
        return 2
    except ValueError as exc:  # tomllib.TOMLDecodeError is a ValueError
        _err(f"recovery run: {workspace.resolve() / 'recovery.toml'}: {exc}")
        return 2
    home = _home()
    store = Store.at_home(home)
    try:
        record = runner.supervise(
            cfg,
            store,
            scio_args,
            env=os.environ,
            home=home,
            incident_id=args.incident,
            resumed_from=args.resumed_from,
        )
        holder = store.lease_holder(cfg.lease_name)
    finally:
        store.close()
    if record.status is RunStatus.REFUSED:
        who = f"pid {holder.pid}" if holder is not None else "another process"
        _err(f"recovery run: {cfg.workspace} is busy: {who} holds its lease; nothing started")
        return 2
    code = 1 if record.exit_code is None else record.exit_code
    # From here on, a supervisor problem never changes the child's exit code.
    try:
        deps = machine.default_deps(home, PACKAGE_ROOT)
        incident = machine.on_run_finished(record, deps)
    except Exception as exc:
        _err(f"recovery: run {record.run_id} ended, but it was not recorded: {exc!r}")
        return code
    if incident is not None and incident.open:
        try:
            _start_tick(incident, home)
        except Exception as exc:
            _err(
                f"recovery: incident {incident.incident_id}: the background tick did not "
                f"start ({exc!r}); run python -m recovery tick"
            )
        else:
            _err(
                f"recovery: incident {incident.incident_id} ({incident.failure_class.value}) "
                "opened; recovery continues in the background. python -m recovery status"
            )
    return code


# tick, hold, release


def _tick(args: argparse.Namespace) -> int:
    deps = machine.default_deps(_home(), PACKAGE_ROOT)
    if args.incident:
        incident = machine.drive(args.incident, deps)
        print(f"{incident.incident_id}: {incident.state.value}")
        if incident.escalation:
            print(f"  escalation: {incident.escalation}")
        return 0
    lines = machine.tick(deps)
    for line in lines:
        print(line)
    if not lines:
        print("nothing to do")
    return 0


def _hold(args: argparse.Namespace) -> int:
    incident = machine.hold(args.incident, machine.default_deps(_home(), PACKAGE_ROOT))
    held_from = incident.held_from.value if incident.held_from else "-"
    print(f"{incident.incident_id}: held (from {held_from})")
    return 0


def _release(args: argparse.Namespace) -> int:
    incident = machine.release(args.incident, machine.default_deps(_home(), PACKAGE_ROOT))
    print(f"{incident.incident_id}: released to {incident.state.value}")
    return 0


# status, incidents


def _run_lines(run: RunRecord) -> list[str]:
    code = "-" if run.exit_code is None else str(run.exit_code)
    lines = [
        f"  {run.run_id}  {run.status.value}  exit {code}  {' '.join(run.argv)}",
        f"    started {_when(run.started_at)}, ended {_when(run.ended_at)}",
    ]
    if run.incident_id:
        lines.append(f"    incident: {run.incident_id}")
    if run.resumed_from:
        lines.append(f"    replays: {run.resumed_from}")
    lines.append(f"    dir: {run.run_dir or '-'}")
    if run.stdout_path:
        lines.append(f"    stdout: {run.stdout_path}")
    if run.stderr_path:
        lines.append(f"    stderr: {run.stderr_path}")
    return lines


def _incident_line(incident: Incident) -> list[str]:
    issue = f"#{incident.issue_number}" if incident.issue_number is not None else "-"
    pr = f"#{incident.pr_number}" if incident.pr_number is not None else "-"
    lines = [
        f"  {incident.incident_id}  {incident.state.value}  {incident.failure_class.value}  "
        f"issue {issue}  PR {pr}",
        f"    {incident.summary}",
    ]
    if incident.escalation:
        lines.append(f"    escalation: {incident.escalation}")
    return lines


def _status(args: argparse.Namespace) -> int:
    store = Store.at_home(_home())
    try:
        _print_status(store, args.workspace)
    finally:
        store.close()
    return 0


def _print_status(store: Store, workspace_filter: str | None) -> None:
    def wanted(workspace: str) -> bool:
        return workspace_filter is None or _same_dir(workspace, workspace_filter)

    runs = [r for r in reversed(store.runs()) if wanted(r.workspace)]
    print(f"runs ({len(runs)}, newest first):" if runs else "runs: none")
    for run in runs[:STATUS_RUNS]:
        for line in _run_lines(run):
            print(line)
    if len(runs) > STATUS_RUNS:
        print(f"  ... {len(runs) - STATUS_RUNS} older runs")
    open_ones = [i for i in reversed(store.open_incidents()) if wanted(i.workspace)]
    print("open incidents (newest first):" if open_ones else "open incidents: none")
    for incident in open_ones:
        for line in _incident_line(incident):
            print(line)
    escalated = [
        i
        for i in reversed(store.incidents(states=[IncidentState.ESCALATED]))
        if wanted(i.workspace)
    ]
    if escalated:
        print("escalated, waiting for a human (newest first):")
        for incident in escalated[:STATUS_ESCALATED]:
            for line in _incident_line(incident):
                print(line)


def _incidents(args: argparse.Namespace) -> int:
    store = Store.at_home(_home())
    try:
        found = history.search(store, signature=args.signature, text=args.grep, limit=args.limit)
    finally:
        store.close()
    if not found:
        print("no incidents")
        return 0
    for incident in found:
        print(
            f"{incident.incident_id}  {_when(incident.created_at)}  {incident.state.value}  "
            f"{incident.failure_class.value}  {incident.signature}"
        )
        print(f"  {incident.summary}")
        facts = [
            f"frame {incident.innermost_scio_frame}" if incident.innermost_scio_frame else "",
            f"occurrences {incident.occurrences}",
            f"issue #{incident.issue_number}" if incident.issue_number is not None else "",
            f"PR #{incident.pr_number}" if incident.pr_number is not None else "",
            f"ledger {incident.ledger_id}" if incident.ledger_id else "",
        ]
        print("  " + ", ".join(f for f in facts if f))
        if incident.escalation:
            print(f"  escalation: {incident.escalation}")
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": _run,
    "tick": _tick,
    "status": _status,
    "incidents": _incidents,
    "hold": _hold,
    "release": _release,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except Exception as exc:
        _err(f"recovery {args.command}: {type(exc).__name__}: {exc}")
        return 2
