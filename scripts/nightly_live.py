"""Nightly live evals with a cost ceiling, run on a machine that has an ``ant auth login`` profile.

Runs the regression and capability suites on Anthropic, each with its own cap so one suite
cannot starve the other, appends one row per suite to ``docs/EVAL_HISTORY.md``, and writes
the full reports under ``evals/results/``. Exit code 0 even when a suite is capped or
fails: the history row is the signal, not the exit code.

    python scripts/nightly_live.py [--max-usd 10] [--trials 3] [--parallel 3] [--config NAME]

Register on Windows (runs as the logged-in user, so the profile is available)::

    schtasks /create /tn "SCIO nightly live evals" /sc daily /st 03:17 ^
      /tr "cmd /c cd /d <repo> && uv run python scripts/nightly_live.py >> .scio\\nightly.log 2>&1"
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "docs" / "EVAL_HISTORY.md"
HEADER = (
    "| date (UTC) | suite | provider+config | k | pass@1 | pass^k | advisor rate | resolved "
    "| spend | capped |\n"
    "|---|---|---|---:|---:|---:|---:|---:|---:|---|\n"
)


def append_history(report: object, config_name: str) -> str:
    from evals.report import SuiteReport

    assert isinstance(report, SuiteReport)
    when = report.finished_at.strftime("%Y-%m-%d %H:%M")
    row = (
        f"| {when} | {report.suite} | {report.provider} | {report.k} | "
        f"{report.pass_at_1:.2f} | {report.pass_pow_k:.2f} | {report.advisor_call_rate:.2f} | "
        f"{report.advisor_resolution_rate:.2f} | ${report.usd_total:.2f}"
        f"{' of $' + format(report.usd_cap, '.0f') if report.usd_cap is not None else ''} | "
        f"{'yes: ' + ', '.join(report.skipped_tasks) if report.capped else 'no'} |\n"
    )
    if not HISTORY.exists():
        HISTORY.write_text(
            "# Eval history\n\nOne row per nightly suite run (live provider unless noted). "
            "Appended by `scripts/nightly_live.py`; full reports are in `evals/results/` "
            "(not committed).\n\n" + HEADER,
            encoding="utf-8",
            newline="\n",
        )
    with HISTORY.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(row)
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nightly live evals with a cost ceiling.")
    ap.add_argument("--max-usd", type=float, default=10.0, help="total ceiling across both suites")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument(
        "--config", default="sonnet_opus_advisor", help="A/B config name (evals/configs)"
    )
    ap.add_argument("--provider", default="anthropic")
    args = ap.parse_args(argv)

    # The profile must win: a stale key in the environment would shadow it.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("SCIO_PROVIDER_OVERRIDE", None)
    sys.path.insert(0, str(ROOT))
    from evals.runner import RESULTS_DIR, run_suite

    config = ROOT / "evals" / "configs" / f"{args.config}.yaml"
    out_dir = RESULTS_DIR / "nightly" / datetime.now(UTC).strftime("%Y-%m-%d")
    split = {"regression": 0.6, "capability": 0.4}
    spent = 0.0
    for suite, share in split.items():
        cap = max(0.0, min(args.max_usd * share, args.max_usd - spent))
        if cap <= 0:
            print(f"{suite}: skipped, ceiling reached (${spent:.2f} of ${args.max_usd:.2f})")
            continue
        report = run_suite(
            suite,
            provider=args.provider,
            trials=args.trials,
            tasks_dir=ROOT / "evals" / "tasks",
            config=config,
            parallel=args.parallel,
            max_usd=cap,
        )
        spent += report.usd_total
        report.write(out_dir)
        sys.stdout.write(report.to_markdown())
        sys.stdout.write(append_history(report, args.config))
    print(f"total spend ${spent:.2f} of ${args.max_usd:.2f}; history: {HISTORY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
