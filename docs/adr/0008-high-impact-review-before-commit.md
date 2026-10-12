# ADR 0008: A passing high-impact diff is reviewed by the advisor before commit

- **Status:** Accepted
- **Date:** 2026-10-12
- **Milestone:** post-0.3.0 (issue #37)

## Context
ADR 0005 defines six escalation signals and routes them through `decide`, which the `verify` node calls only after a failing check; the triage diagram in ARCHITECTURE.md starts at "verify failed". Signal 6, the high-impact diff (more than N files, dependency or schema files, a removed public symbol), therefore only mattered when the same step also failed its tests. A wide change that passed first time was committed without a second opinion, and the only trace of its impact was the Documenter's ADR (ADR 0005 decision 8).

Live data made the gap visible. Across 31 nightly rows and the first trials of t006 to t009 the advisor was never called: a competent coder makes verify pass, and t009 showed that even a failure the coder cannot see is fixed by the debugger in one turn. So the advisor, the role the architecture reserves Opus for, had no live data point at all, and no eval task could produce one without a design change (issue #37, discussion on the issue).

## Decision
1. **A passing verify with unreviewed high-impact flags routes to `advise`.** `scio.loop.escalation.review_decision` returns an `advise` decision with the single trigger `HIGH_IMPACT` when `state.high_impact_flags` is non-empty, `high_impact_reviewed` is false, and `Budget.advisor_allowed(step)` holds. `verify` stores it; `after_verify` sends a passing verify to `triage` when such a decision is stored, else to `checkpoint` as before. Nothing else about a passing verify can escalate.
2. **A refused review commits.** When the caps refuse the call, `review_decision` returns None and the run checkpoints as it always did. A passing diff is not a failure, so the advisor-cap halt of ADR 0005 decision 3 does not apply.
3. **`continue` commits without another coder turn.** `advise` marks the escalation record `resolved` immediately and sets the phase to `checkpoint`; `after_advise` routes there (new edge `advise -> git_checkpoint`). `change_approach` and `replan` behave exactly as on a failing verify: the guidance goes to the next coder turn and the next verify resolves the record. `escalate_human` halts.
4. **The brief says what is being asked.** `advisor_brief(verify_passed=True)` adds one paragraph: tests and lint are green, this is a pre-commit review, reply `continue` unless the diff itself should change. The advisor prompt is otherwise unchanged.
5. **Once per run.** `high_impact_reviewed` is set by the first advise and never reset (unchanged from ADR 0005), so a run pays for at most one review.

## Consequences
- The advisor now has a deterministic live path: any task whose correct diff touches more than `high_impact_files` (8) files or removes a public symbol. t010 (`evals/tasks/t010_wide_rename`, pinned T1) is the first; its Fake script and `test_t1_passing_high_impact_diff_gets_one_advisor_review` pin the control flow (one record, trigger `high_impact`, verdict `continue`, outcome `resolved`, no extra coder turn).
- Cost: one Opus call per wide diff, about $0.02 to $0.05 at current prices, on runs that would otherwise have spent nothing on the advisor. The T1 cap of 3 calls per run is unchanged; a review consumes one of them.
- ADR 0006 decision 4 (live resolution rates) still needs failures to measure; this ADR gives the advisor live traffic, not failure data. The two should not be conflated in the eval history: `EscalationRecord.outcome == "resolved"` with verdict `continue` on a passing verify means "approved", not "fixed".
- First live run (t010, `sonnet_opus_advisor`, 2026-10-12): the review fired on step S1, a two-file diff that removed the public `format_cents`, not on the eleven-file migration the task was designed around; the detector's symbol flag precedes its file count. Opus answered `continue` in one call ($0.03) and the step committed without another coder turn. The same run also produced the first natural failure-path escalation (`low_confidence, plan_drift` on S2, verdict `continue`, resolved), so both advisor paths now have live data. Run total $0.64.
- Risk: a wide but trivial diff (a rename across many files) pays for a review it does not need. The threshold `high_impact_files` stays the tuning knob (ADR 0006); if nightly data shows the review approving every time, raise it or add a per-file-size floor.
