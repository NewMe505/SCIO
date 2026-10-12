# ADR 0007: Product mode, a product orchestrator over the task graph

- **Status:** Accepted
- **Date:** 2026-10-11
- **Milestone:** M6 (planning); implemented M6–M11

## Context
SCIO's task graph (M0–M5) takes one task on one existing Python repository to one pull request, and it is reliable at that: t001 passes live at pass^5 = 1.00, and the nightly regression suite has pass^3 = 1.00. The owner wants SCIO to turn a prompt into product-ready software, in any stack. The owner approves each milestone, and there is no product-level spend ceiling. The code survey (see [PRODUCT_MODE.md](../PRODUCT_MODE.md)) found no greenfield path, no state that persists across runs, and Python-only verification throughout.

## Decision
1. **Add a layer instead of rewriting.** A new product orchestrator (`scio/product/`) calls the existing task graph as a subgraph, one feature at a time. The task graph remains the unit of work, and its guarantees carry over unchanged: one writer, ground-truth verification, idempotent side effects, and capped escalation.
2. **Persist product state in the target repository.** `PRODUCT.md`, `.scio/product/progress.json`, ADRs and `NOTES.md` together are the memory across sessions (the "initializer + progress file" harness).
3. **A human approves at each product milestone.** Each milestone is one PR into the default branch, and merging stays human (CONTRIBUTING). Creating a GitHub repository also requires human confirmation.
4. **Handle stacks through toolchain profiles.** Every Python-specific command moves behind a `Toolchain` protocol (ADR 0009, M6). A new stack is a profile, not new logic in the graph.
5. **No product-level dollar cap by default.** Per-feature tier caps, stall caps and iteration caps remain as runaway guards. Cost is reported at every approval stop.
6. **Ground truth defines "product-ready".** The release gate runs a fixed quality bar: tests with a coverage floor, lint, types, build, smoke/e2e against the running app, dependency audit, secret scan, and checked README instructions. A failing check overrides any model verdict.

## Consequences
- This supersedes the PROMPT.md non-goal "No UI" for *products SCIO builds*; SCIO itself still has no UI. It also supersedes assumption F-8 (Python-only targets). Both docs are updated in M6.
- **Upside:** the proven per-feature path is reused, and each milestone stays reviewable.
- **Downside:** because there is one writer, a product build runs serially. A large product takes hours, not minutes.
- **New risk: dependency installation needs network access.** It is contained by the two-phase sandbox: an allowlisted install phase, then a test phase with the network off. Docker becomes the default backend for product builds.
- **To validate:** the product eval suite (p001–p003, M11), measuring features passed, human interventions, $/feature and milestone pass^k.
