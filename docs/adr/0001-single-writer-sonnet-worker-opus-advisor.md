# ADR 0001: Single writer, Sonnet 5 worker, Opus 5.5 planner/advisor

- **Status:** Accepted
- **Date:** 2026-10-09

## Context
SCIO has to take software tasks from spec to a merged PR reliably and at reasonable token cost. Coding is mostly sequential work. Research shows multi-agent setups get worse on sequential tasks and amplify errors when agents write independently (see [RESEARCH.md](../RESEARCH.md)).

## Decision
1. **Only one agent writes code.** Parallel fan-out is limited to read-only work (exploration, review).
2. **Sonnet 5 is the worker** for all worker roles (Coder, Tester, Debugger, Documenter, Explorer, Reviewer).
3. **Opus 5.5 plans, advises and gates.** It never calls tools and returns short typed verdicts (400–700 tokens).
4. **Escalation is signal-driven and capped.** Sonnet escalates to Opus on:
   - a repeated error signature after 2 fix attempts
   - stall_count > 2
   - low confidence or a design decision it can't make
   - drift from the plan
   - sampled patches that disagree
   - a high-impact change

   Limit: ≤1 advisor call per step and ≤K per run; when the cap is hit, the run goes to a human.
5. **External checks decide pass/fail.** Tests, lint and types decide; LLM review is advisory only.
6. Model IDs live only in `models.yaml`. The policy is provider-agnostic.

## Consequences
- **Upside:** most calls run on the cheaper model; Opus spend is predictable; fewer coordination failures.
- **Downside:** less parallelism on large tasks, partly offset by parallel read-only exploration in T2.
- **To validate:** A/B Sonnet-only vs Sonnet + Opus advisor vs Opus-only on the private eval suite, then tune thresholds N and K from the escalation-rate and resolution-rate metrics.
