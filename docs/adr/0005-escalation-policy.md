# ADR 0005: Escalation policy implementation

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M4

## Context
ADR 0001 fixed the policy: a Sonnet worker escalates to an Opus advisor on specific signals, with caps, and the advisor's output is the authoritative plan revision. M4 makes it executable. The spec lists six triggers, a never-escalate list, two fixed touchpoints, caps of one call per step and K per run, and logging of every call with trigger, cost and outcome.

## Decisions
1. **Triage is a pure function stored in state.** `scio.loop.escalation.decide(state, cfg)` returns a `TriageDecision` (route, triggers, reason). The `verify` node stores it after a failing check; the `triage` conditional edge only reads it. Routing never computes, every decision is visible in the ledger and the checkpoint, and the six triggers are unit-testable without a graph.
2. **Signals, as implemented.** (1) the same error signature seen `repeat_threshold` (3) times; (2) `stall_count > max_stall` (2); (3) the worker's `confidence == "low"` or `needs_design_decision`; (4) files touched outside the step's `files_expected`, with `tests/` and `test_*.py` always allowed; (5) `sample_disagreement` set by a sampling step (config `sample_n`, off by default, see open question 3); (6) high-impact flags from a textual diff assessment (dependency files, migrations/schema, removed public `def`/`class`, more than N files), raised once per run. Never escalate: lint-only failures, and any failure with no signal, which is the ordinary `debug` path.
3. **Caps and the cap exit.** `Budget.advisor_allowed(step)` enforces one call per step and K per run. A triggered escalation that the cap refuses routes to `halt` with exit `advisor_cap`, which interrupts for a human like `escalate_human`.
4. **The advisor sees a brief, not history.** Goal, step, triggers, impact flags, diff stat, last error head, attempt count, the worker's own summary: about two thousand tokens, stored as an artifact. Its verdict routes: `continue` and `change_approach` go back to `implement` with the guidance injected as `[context from advisor]`; `replan` with `revised_steps` installs a new plan version authored by the advisor, else re-runs `plan` with the guidance as the revision reason; `escalate_human` halts.
5. **Outcome is decided by the next verify.** An escalation is `pending` until the next verify: passing marks it `resolved`, failing marks it `unresolved`. Tokens and dollars come from the cost ledger delta around the call.
6. **One writer, parallel readers.** T2 explorers run through LangGraph `Send` with a read-only toolbox (`repo_search`, `view_file`) and reduce into `explorer_summaries`; every edit happens in a `coder`, `debugger` or `documenter` toolbox. Spans carry the role so the invariant is asserted from traces, not assumed.
7. **Steps advance at checkpoint time.** `verify` marks a step done; `git_checkpoint` commits that step's passing features and moves `current_step_id` to the next open step. Evaluating the step before advancing is what makes the per-step commit and the per-step advisor cap correct.
8. **ADRs in the target repository.** When an advisor returned `replan` or `change_approach`, or the diff is high impact, the Documenter writes `docs/adr/NNNN-<slug>.md` in the target repository if it keeps ADRs there, before the docs commit.

## Consequences
- Thresholds (3, 2, 8, K per tier) live in `configs/budgets.yaml` under `escalation` and are the M5 tuning surface.
- Plan drift is only evaluated after a failing verify. A drifted patch that passes every check is accepted; the Reviewer and the gate see the diff. Revisit if evals show silent scope creep.
- Patch sampling (signal 5) is wired but off by default; the sampling step itself is an M5 item measured against its token cost.
