# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [Unreleased]
### Added
- T1 and T2 tiers in one graph builder (`scio/graph/build.py`): `scope`, parallel `explore` (LangGraph `Send`, read-only), `localize`, `plan` (plan-owned test filters), `post_plan` (issue checklist), `implement` (one step per cycle), `debug`, `advise`, `review`; generated `t1`/`t2` diagrams.
- Escalation policy (`scio/loop/escalation.py`, `scio/loop/impact.py`): six signals, never-escalate list, per-step and per-run caps, `advisor_cap` exit with human interrupt; `TriageDecision` in state; `EscalationRecord` with trigger, brief, verdict, tokens, $ and outcome; advisor guidance injected into the next worker turn; ADR generation in the target repository.
- `ToolBox(role=...)` on spans; `WorkerOutput.sweep` surfaced in the ledger; PR body carries the escalation log and reviewer notes.
- Five real regression tasks (t001–t005) with fixture repositories, hidden tests and a data-driven `ScriptedResponder`; the runner reports advisor call and resolution rates from the graph.
- Postgres checkpointer built with the serializer allow-list; the gate runs the docs check directly.
- ADR 0005: escalation policy implementation.
- Context layer (`scio/context`): artifact store, per-role prompt projections, size-triggered transcript trimming.
- Loop layer (`scio/loop`): `AgentLoop` (tools until done, tool/model caps, typed `WorkerOutput` summary) and exit conditions.
- T0 graph (`scio/graph/tiers/t0.py`): intake, route, prepare_repo, t0_loop, verify, git_checkpoint, document (docs commit, push, draft PR), gate with ground-truth override, pr_ready, merge_wait interrupt, halt. `scio run` / `scio resume` CLI.
- `WorkerOutput.tests_added`; verify requires changes, feature tests, a passing suite and clean lint.
- Eval runner drives the real graph for `grader: tests` tasks and grades with hidden tests; t001 fixture repository; `ScriptedResponder` for deterministic Fake runs.
- Generated `t0` Mermaid section in `docs/ARCHITECTURE.md`; LangGraph serializer allow-list for state types.
- ADR 0004: loop and verification design.
- State backbone (`scio/state`): the M0 schema as Pydantic models with reducers, `WorkerOutput.sweep`, error signatures stable across line numbers and paths, progress fingerprints and stall counting.
- Graph runtime (`scio/graph`): checkpointer selection by DSN (memory, SQLite, Postgres), idempotent side effects keyed by `hash(thread_id, node, intent)` with a durable effect log and world probes, Mermaid rendering with doc sections checked in CI, graph registry.
- Agent-computer interface (`scio/aci`): `Sandbox` protocol with local, Docker and E2B backends; `repo_search`, `view_file`, `edit_file` (parse- and lint-gated), `run_tests` (failures only), `git_checkpoint`; output truncation with hints; capability tags and the compile-time trifecta check; `ToolBox` dispatcher with tracing and a call cap.
- `GitHubClient` (`scio/harness/github`): idempotent branch, issue-comment and draft-PR writes through `gh`, default-branch guard, write log.
- Durability test: a 3-node graph with file and git side effects killed mid-run and resumed with zero duplicated effects.
- `scripts/gen_mermaid.py`; `check_docs.py` now verifies generated graph sections.
- ADR 0003: idempotent side effects and sandbox tiers.
- Harness layer (`scio/harness`): `ModelProvider` protocol; Anthropic, OpenAI and deterministic Fake adapters; single retry layer with backoff and `retry-after`; `BudgetGuard` checked before every call; per-call cost ledger; prompt-cache breakpoint placement; OpenTelemetry GenAI spans with opt-in content capture.
- Prompt layer (`scio/prompt`): versioned per-role templates (12 roles at v1); Pydantic → provider-safe wire schema; structured-output validate-and-repair loop (max 2 repairs).
- `configs/models.yaml` (the only place model IDs live) and `configs/budgets.yaml` (per-tier budgets).
- Eval skeleton (`evals/`): task loader, n-trial runner, pass@1 / pass^k / tokens / $ / latency / advisor-rate metrics, Markdown and JSON reports, three placeholder regression tasks.
- CI (`.github/workflows/ci.yml`): ruff, mypy, pytest on the Fake provider, docs checks, regression evals, API docs artifact. `scripts/check_docs.py`, `scripts/gen_api_docs.py`.
- ADR 0002: harness conventions.
- Continuous-improvement protocols: `CLAUDE.md`, `docs/ISSUE_LEDGER.md` (I-001 to I-010), `docs/LESSONS_LEARNED.md` (M0 and M1 retrospectives), Debugger template v2 with systemic sweeps, PR template checklist.
- Build spec and generation prompt (`docs/PROMPT.md`).
- Research basis with sources (`docs/RESEARCH.md`).
- ADR 0001: single writer, Sonnet 5 worker, Opus 5.5 planner/advisor.
- Repository docs: README, CONTRIBUTING, issue and PR templates.
- Milestone 0 architecture (`docs/ARCHITECTURE.md`): graphs per tier, State schema, agent contracts, directory tree, milestone plan, assumptions and open questions.
