# ADR 0006: Escalation thresholds set from the tuning sweep

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M5

## Context
ADR 0005 put the escalation thresholds in `configs/budgets.yaml` as the tuning surface. M5 adds `evals/tune.py`, which sweeps `repeat_threshold` × `max_stall` over the capability scenarios and reports pass rate, tokens, dollars, advisor-call rate and resolution rate per cell. The account has no Anthropic API key (ledger I-019, placeholder by decision), so the sweep ran on the Fake provider at real-model prices.

## The data (Fake provider, 2 trials, scenarios c001 repeated-error and c002 plan-drift)

| repeat_threshold | max_stall | pass | tokens | $ | advisor calls | resolved |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 1 | 1.00 | 26606 | 0.0634 | 1.00 | 1.00 |
| 2 | 2 | 1.00 | 26606 | 0.0634 | 1.00 | 1.00 |
| 2 | 3 | 1.00 | 26606 | 0.0634 | 1.00 | 1.00 |
| 3 | 1 | 0.50 | 23676 | 0.0555 | 0.50 | 1.00 |
| 3 | 2 | 1.00 | 29762 | 0.0703 | 1.00 | 1.00 |
| 3 | 3 | 1.00 | 29762 | 0.0703 | 1.00 | 1.00 |
| 4 | 1 | 0.50 | 23676 | 0.0555 | 0.50 | 1.00 |
| 4 | 2 | 0.50 | 26830 | 0.0624 | 0.50 | 1.00 |
| 4 | 3 | 1.00 | 32918 | 0.0773 | 1.00 | 1.00 |

The sweep's own rule ("cheapest cell with pass rate 1.0") picks `repeat_threshold=2, max_stall=1`.

## What the data can and cannot say
- **It measures control flow, not model quality.** The Fake debugger never fixes anything, so every extra debug turn before escalation is pure cost. Escalating on the second identical failure is cheapest *only* because the retry can never succeed. With a real model the first debug turn often fixes a clear-traceback failure, and the research basis (RESEARCH.md: never escalate for clear-traceback failures; advisor calls capped) argues for giving it that chance.
- **It does say something firm about stalls.** Every cell with `max_stall < repeat_threshold - 1` fails half the runs: the stall exit fires before the repeated-error signal can escalate. The two thresholds are coupled: `max_stall >= repeat_threshold - 1`.
- **It bounds the cost of waiting.** Going from threshold 2 to 3 costs about 11% more tokens on a run that does escalate (26.6k → 29.8k), which is the price of one extra debug turn.

## Decision
1. `repeat_threshold = 3` (the spec's "same error after 2 fix attempts"), `max_stall = 2`. The Fake data confirms this cell passes at 1.0 and resolves every escalation; the 11% premium over threshold 2 is the budget reserved for a real debugger's first fix attempt.
2. The coupling `max_stall >= repeat_threshold - 1` is now enforced: `EscalationConfig` raises on a violating configuration, and the sweep grid skips those cells.
3. `advisor_calls_max` stays 3 (T1) and 6 (T2): no scenario needed more than one call per step.
4. Re-run `python -m evals.tune --provider anthropic` when a key exists and revisit decision 1 with real resolution rates. Until then the thresholds are "spec defaults, confirmed safe by control-flow data", not "optimal".

## Consequences
- The sweep is reproducible from `evals/tune.py` and its JSON/Markdown reports live under `evals/results/` (git-ignored; the nightly workflow uploads them as artifacts).
- A future live sweep must include a scenario where the debugger *can* succeed, or it will reproduce the same bias toward early escalation.
- Status 2026-10-12: decision 4 is still pending, now for a different reason than the key. Across 31 live task rows (three nightly regression suites and the first trials of t006 and t007) no run made an advisor call, so the sweep has nothing to measure; it waits for a live task that fails verify at least twice (`docs/IDEAS_TRIAGE.md`, status note).
