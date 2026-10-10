# SCIO

A model-agnostic, multi-agent framework built on LangGraph. It takes a software task from spec (or a GitHub Issue) to a merged, tested and documented pull request.

> Status: **design phase (Milestone 0 delivered)**. The build spec is in [docs/PROMPT.md](docs/PROMPT.md); the M0 design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Design in one paragraph
A cheap router classifies each task into an effort tier (T0, T1 or T2). **Opus 5.5** plans, advises and checks that the objective was met before finishing. **Sonnet 5** does the work: coding, testing, debugging and documentation. Only one agent writes code. Parallel fan-out is used only for read-only exploration and review. Tests, linters and type checks decide pass/fail, not LLM opinion. Sonnet escalates to Opus only on specific signals (repeated errors, stalls, low confidence, plan drift, high-impact changes), with a cap on how often. Every run produces a GitHub PR, and the docs are updated in that same PR.

## Five layers
| Layer | Responsibility |
|---|---|
| Prompt | Versioned role templates with typed (Pydantic) outputs |
| Context | Just-in-time retrieval, compaction, artifacts passed by reference |
| Harness | Provider adapters, retries, budgets, caching, tracing, sandbox |
| Loop | Bounded act → verify → fix loops with explicit exit conditions |
| Graph | Checkpointed LangGraph StateGraph and routing |

## Docs
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): Milestone 0 design (graphs, State, agent contracts, directory tree, milestone plan)
- [docs/PROMPT.md](docs/PROMPT.md): the build spec / generation prompt
- [docs/RESEARCH.md](docs/RESEARCH.md): the evidence behind each design decision
- [docs/adr/](docs/adr/): Architecture Decision Records
- [CONTRIBUTING.md](CONTRIBUTING.md): branching, commits, PRs, docs-as-code
- [CHANGELOG.md](CHANGELOG.md)

## Roadmap
- M0: architecture, State schema, agent contracts, directory tree
- M1: harness, providers (including a Fake), eval skeleton, repo docs and CI
- M2: state, checkpointing, agent tools, sandbox, GitHubClient
- M3: router and T0 loop
- M4: T1/T2 tiers and the escalation policy
- M5: observability and eval tuning
