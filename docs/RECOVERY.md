# The external recovery supervisor

The supervisor keeps a `scio build` alive through failures in SCIO itself. It runs as the build's parent process, outside SCIO, and uses the standard library only. When the build fails because of a SCIO harness fault, it does the following:
1. Preserves the build's state.
2. Opens a `recovery` issue.
3. Starts a Claude Code session that repairs SCIO in a tested PR.
4. Once the owner has merged that PR, moves `.scio/live-wt`.
5. Checks that the repaired SCIO can read the halted thread.
6. Replays the original command, so the build resumes from its checkpoint.

The design and the owner's decisions are in [ADR 0019](adr/0019-external-recovery-supervisor.md).

The supervisor never merges. It never answers a human stop (merge, gate, release, a feature decision). It never writes the workspace's databases, and it never touches the product.

## Set up once

1. **Pin the supervisor's own code.** It runs from its own worktree, never from live-wt (which moves after every repair) or from the main checkout (which changes branches). Only the owner moves it:
   ```sh
   git -C <SCIO> worktree add --detach .scio/recovery-wt <sha on main>
   # later, to update the supervisor itself:
   git -C <SCIO>/.scio/recovery-wt checkout --detach <newer sha on main>
   ```
2. **Describe the workspace.** In the directory the SCIO launcher runs from (for graphcalc, `graphcalc-run/`), write `recovery.toml`:
   ```toml
   launcher = ["sh", "scio.sh"]          # the workspace's existing launcher, unchanged
   scio_repo = "C:/Users/<you>/OneDrive/Desktop/projects/SCIO"
   scio_gh_repo = "NewMe505/SCIO"
   # live_wt = "<scio_repo>/.scio/live-wt"   (default)
   # claude_bin = "claude"                   (default)

   [limits]                               # optional; defaults in recovery/config.py
   hang_after_s = 2700                    # no progress for 45 minutes kills the build
   ```
3. **Register the scheduled tick.** The tick advances incidents and picks up builds orphaned by a reboot or by a dead supervisor. It runs at logon and every 10 minutes. Use PowerShell, not `schtasks` (I-045):
   ```powershell
   $wt = "C:/Users/<you>/OneDrive/Desktop/projects/SCIO/.scio/recovery-wt"
   $py = (Get-Command python).Source
   $action = New-ScheduledTaskAction -Execute $py -Argument "-m recovery tick" -WorkingDirectory $wt
   $triggers = @(
     (New-ScheduledTaskTrigger -AtLogOn),
     (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10))
   )
   $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
     -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
   Register-ScheduledTask -TaskName "SCIO recovery tick" -Action $action -Trigger $triggers -Settings $settings
   ```
   The task's PATH has no `sh`. A replay therefore runs with the environment recorded from the original run, and every program is looked up on that environment's PATH (I-183).

   The 4-hour limit covers one tick running both repair attempts back to back, 2 × 90 minutes. A tick that is busy with a repair holds the tick lease, so the next scheduled ticks return at once (`IgnoreNew`).

## Run a build under supervision

Where you used `sh scio.sh build ...`, run the same arguments through the supervisor, from its worktree:

```sh
cd <SCIO>/.scio/recovery-wt
python -m recovery run --workspace <workspace> -- build --product <product dir> --gh <owner/repo>
```

`run` passes the build's output through and exits with the build's own exit code, so `PAUSED for a human:`, the resume hints and the exit status are exactly what `sh scio.sh` gives. When the build fails and the failure needs recovery, `run` prints one line naming the incident, and the recovery carries on in a background tick.

## What happens to a failure

| The build ended with | The supervisor |
|---|---|
| done, or a human stop | records it; nothing else |
| a task fault (`integration_failed`, `gate_escalated`, `release_failed`, `stopped`, `budget`, `load_failed`) | records it as a closed incident; the owner decides, as before |
| a sign-in failure (`not logged in`, `invalid_grant`) | opens an issue labelled `needs-human`: sign in again; no repair |
| a rate limit, a used-up usage window, a GitHub refusal | replays after a backoff (up to 3 times), then escalates |
| a traceback in SCIO's code, a harness error in a feature, or a hang | opens a `recovery` issue and starts a repair session; after the owner's merge, it updates live-wt, runs the health check and replays |
| a reboot or a dead supervisor (orphan) | replays; no repair |

The repair session runs `claude -p --permission-mode auto` in a fresh worktree under `.claude/worktrees/recovery-<incident>`. It works under SCIO's own CLAUDE.md and hooks: reproduce, ledger entry, sweep, regression test, guardrail, gate and a draft PR. It cannot merge, force-push, push to `main`, stash or call `gh api`. Its claims are checked against GitHub: the PR, CI on its head, a changed test and a ledger entry. The workspace's state must be unchanged.

## Commands

| Command | What it does |
|---|---|
| `python -m recovery status [--workspace W]` | runs (with their log directories) and open incidents |
| `python -m recovery incidents [--signature S] [--grep TEXT]` | searches incident history |
| `python -m recovery hold <incident>` and `release <incident>` | pause and resume an incident's automation |
| `python -m recovery tick [--incident I]` | advance incidents now instead of waiting for the scheduled tick |

## Where things are

The supervisor's state lives under `%LOCALAPPDATA%/SCIO/recovery` (override with `SCIO_RECOVERY_HOME`). That keeps it outside OneDrive and outside every checkout.

| Path | What it holds |
|---|---|
| `state.sqlite` | runs, incidents, their timelines, the step log and leases |
| `runs/<run id>/` | each run's `stdout.log`, `stderr.log` and SCIO's `outcome.json` |
| `incidents/<incident id>/` | the snapshot taken before any repair: database backups, `progress.json`, git facts, logs |
| `incidents.jsonl` | one line per incident transition, for search |
| `replays/`, `ticks/` | logs of background replays and ticks |

Recorded incidents are what happened, not lessons. The investigated root cause is the ledger entry the repair PR adds (`docs/ISSUE_LEDGER.md`). A validated lesson is the guardrail with a running guard (`docs/LESSONS_LEARNED.md`).
