"""``python -m recovery``: run, tick, status, incidents, hold, release. OWNER: wave-1 agent H.

The contract stub parses arguments so that ``python -S -m recovery --help`` works.
Agent H implements the commands.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(f"recovery {args.command}")
