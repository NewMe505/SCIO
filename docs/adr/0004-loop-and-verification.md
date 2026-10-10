# ADR 0004: Worker loop and verification

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M3

## Context
The first end-to-end tier needs a worker turn that uses tools, a verification step that is the sole judge of progress, and explicit exits. The spec requires typed worker outputs, tests as ground truth, bounded loops, and a gate that never overrides a failing check.

## Decisions
1. **A turn ends with a separate structured call.** The worker uses tools until it stops, hits the tool cap (10 in T0) or the model-call cap; then one more call, with the whole transcript and a fixed summary instruction, returns `WorkerOutput` through the validate-and-repair loop. Parsing JSON out of free text at the end of a tool-using turn was rejected: constrained decoding on a dedicated call is reliable and the extra call is cheap relative to the turn.
2. **Append-only transcript, size-triggered trimming.** Messages are never deleted or reordered; old tool results are replaced by a placeholder when the transcript exceeds a character budget, keeping the last three intact. This keeps the cached prefix stable and keeps the harness compatible with preserved-thinking checks. Model-driven compaction (Haiku) is deferred to M4/M5 when transcripts get long enough to need it.
3. **Verify has a fixed order and reports every layer.** (a) the working tree must differ from the base commit, (b) feature tests must exist and pass, (c) the whole suite must pass, (d) lint must be clean. A feature's `test_filter` comes from the plan (M4) or, in T0, from the worker's `tests_added`; "no tests collected" is a failure, so an agent cannot pass by writing no tests. The output of every layer is stored as one artifact that the next worker turn sees as `[context from verify]`.
4. **The gate is advisory on top of ground truth.** If any feature is failing, a `pass` verdict is rewritten to `escalate_human` in code, with the override recorded in the reasoning. The research basis for the gate is the objective check, not a second opinion on tests.
5. **Exit reasons are state, routing is pure.** Nodes write `exit_reason`; conditional edges only read state. `success` is set only by `merge_wait` after the human decision, so a run that ends any other way is never reported as a success.
6. **Non-T0 routing halts in M3.** The router runs on every task and its decision is logged; tiers that do not exist yet stop for a human rather than silently running T0.

## Consequences
- One extra model call per worker turn (the summary). Measured on the Fake provider at real-model prices, a full T0 run of t001 is 13 calls.
- `tests_added` is a T0 convenience; M4 moves test ownership to the plan (`PlanStep.feature_ids` → `FeatureItem.test_filter`).
- The gate prompt sees docs-check results only through the PR body; a direct docs-check input to the gate is a small M4 follow-up.
