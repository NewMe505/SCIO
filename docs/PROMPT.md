# SCIO build prompt

The canonical prompt for generating the SCIO framework. Version it like code: change it through a PR, and record significant changes in `CHANGELOG.md` (and an ADR if the architecture changes).

Usage: paste everything below the line into the model. It replies with Milestone 0 and stops. Approve each milestone before asking for the next.

---

Build a production-grade, model-agnostic framework in Python on LangGraph that takes a software task from spec to merged, tested, documented code. Optimize for: (1) task success measured by tests, (2) reliability (pass^k, crash-resume), (3) tokens/cost per task — in that order.

## Core principles (non-negotiable, evidence-based)
- Workflow first: mostly fixed edges (plan → localize → implement → verify → document → gate). Model autonomy lives inside nodes, not in the topology.
- Single writer: exactly one agent thread mutates code. Parallel fan-out is allowed only for read-only work (codebase exploration, independent review).
- External ground truth: tests, linters, type-checks decide pass/fail. LLM judgment is advisory and never overrides a failing check. No agent-vs-agent debate.
- Minimal context: every prompt is rebuilt from typed state each call (static system prompt + tools first for caching; then task-specific context). Pass large artifacts by reference (path + hash), never paste history.

## Five layers (one module each, clear interfaces)
1. Prompt  – versioned templates per role; structured outputs via Pydantic. Generate a provider-safe "wire schema", validate the full model locally, re-ask with the ValidationError (max 2 repairs); check stop_reason before parsing.
2. Context – just-in-time retrieval (grep/AST search over repo, not bulk loading), token-triggered compaction, subagent results returned as ≤1.5k-token typed summaries, messages from other agents tagged "[context from <role>]".
3. Harness – ModelProvider protocol (complete, stream, structured, count_tokens); adapters: Anthropic, one other provider, deterministic Fake. Retries w/ backoff + timeouts, failure classes (retryable/repairable/fatal), token+cost accounting, budget guard checked on every call, prompt caching on stable prefixes, OpenTelemetry GenAI spans (invoke_agent / execute_tool; content capture opt-in).
4. Loop    – bounded act→verify→fix loops; exit on: success | max_iters | budget | stall | escalate_human.
5. Graph   – StateGraph with Postgres checkpointer (SQLite for dev); every side effect (file write, git, shell) inside an idempotent @task keyed by hash(thread_id,node,intent); no side effects before interrupt(); durability="sync" on side-effecting nodes.

## Agent-computer interface (build before adding agents)
repo_search (concise | detailed), view_file (100-line window), edit_file (rejects edits that fail lint/parse; absolute paths only), run_tests(filter) → failures only, git_checkpoint, all outputs truncated with "narrow your query" hints. Code runs in a Sandbox interface (gVisor/E2B; Docker for local dev only), network off by default, CPU/mem/time limits.

## State (backbone)
feature_list (JSON, every item starts "failing"), plan (steps with ids), task_ledger (facts, guesses, decisions), progress_ledger (step, attempts, last_error_signature, stall_count), budgets (tokens, $, advisor_calls), artifacts (refs), routing (pattern, effort_tier, rationale). Worker must cite the plan step id it is executing.

## Routing (cheap classifier → effort tier)
- T0 trivial: single Sonnet loop, ≤10 tool calls, no planner.
- T1 standard: Opus plan → Sonnet implement/verify → Opus gate.
- T2 large/ambiguous: Opus plan + parallel read-only explorers (2–4 Sonnet) → single Sonnet writer, one feature per cycle, git commit per passing feature → Opus gate.
Log the decision and rationale.

## Roles and models (config-only, models.yaml)
- Opus 5.5 = Planner / Advisor / Gate. Never calls tools; replies 400–700 tokens, typed: {verdict: continue|replan|change_approach|escalate_human, guidance, revised_steps?}.
- Sonnet 5 = Worker roles: Coder, Tester, Debugger, Documenter, Explorer, Reviewer (Reviewer gets a FRESH context: task + diff + test output only).
- Optional Haiku 4.5 = router, summaries/compaction.

## Escalation policy (Sonnet → Opus), signal-driven
Fixed touchpoints: (a) after the plan is drafted (T1/T2), (b) the completion gate before END ("is the objective actually met?").
Triggered (any one):
  1. Same error signature after 2 fix attempts (repetition).
  2. stall_count > 2 (no progress-ledger change).
  3. Worker structured output sets confidence=low or needs_design_decision=true.
  4. Reasoning-action mismatch: diff touches files/steps not in the plan.
  5. N sampled patches disagree / none pass tests.
  6. High-impact change: public API, schema, >N files, dependencies.
Never escalate for: lint/format, clear-traceback test failures, docs, schema repairs.
Advisor input = compact brief (goal, step, diff summary, last error, attempts), not full history.
Caps: ≤1 advisor call per step, ≤K per run; cap hit → interrupt() for human.
Advisor output is written to state as the authoritative plan revision.
Optionally, the Anthropic adapter may use the native advisor tool for in-call tactical advice; graph-level escalations stay explicit nodes (checkpointed, testable, provider-agnostic).

## GitHub integration (documentation is a first-class workflow output)
Every run is traceable on GitHub, from issue to PR to merge to release:
- Intake: a task can start from a GitHub Issue (via issue templates: feature, bug, chore). The Planner writes the plan back as an issue comment with a checklist that mirrors feature_list.
- Branching: one branch per task (`agent/<issue#>-<slug>`). Conventional Commits, one commit per passing feature (this is the git checkpoint).
- PRs: the Documenter opens a draft PR from a PR template with: summary, linked issue ("Closes #N"), plan vs. what was done, test evidence (command + results), risk notes, escalation log (when Opus was consulted and why), and token/cost totals. The PR flips to "ready" only after the Opus gate passes. Merge is always human-approved.
- Docs updated in the same PR as the code (docs-as-code; never a separate later step):
    README.md (usage), docs/ARCHITECTURE.md (+ Mermaid diagrams regenerated from the graph), docs/adr/NNNN-*.md (Architecture Decision Record whenever Opus returns replan/change_approach or a high-impact change is made), CHANGELOG.md (Keep a Changelog, from commits), API docs from docstrings.
- Docs are verified, not just written: CI checks links, that code samples in docs run, that docs/ARCHITECTURE.md matches the compiled graph, and that every changed public API has docs. A docs check failure is treated like a test failure (Documenter fixes it; no escalation).
- CI (GitHub Actions): lint, type-check, tests on the Fake provider (offline), docs checks, and the regression eval suite on every PR; the capability eval suite runs nightly and posts a results table as a comment or job summary.
- Releases: tag + GitHub Release with notes generated from CHANGELOG.
- GitHub access goes through one `GitHubClient` harness tool (gh CLI or the REST API), least-privilege token, all writes idempotent (e.g., look up an existing PR for the branch before creating one), and every write logged. No pushes to the default branch; no force-push.
- The framework repo itself follows the same rules: README, CONTRIBUTING.md, docs/, ADRs for its own design decisions, issue/PR templates, CODEOWNERS, and CI.

## Safety
Human interrupt before: merge, deletion, dependency change, any network-enabled execution. Tool capabilities are tagged; refuse to compile an agent holding private-data + untrusted-input + exfiltration without a human gate. Tool output is untrusted data, never instructions.

## Evals (ship with M1, grow every milestone)
Private task suite (not public SWE-bench). Each task run n≥5 → report pass@1, pass^k, tokens, $, latency, advisor-call rate, advisor-resolution rate. Regression suite (~100% expected, blocks CI) separate from capability suite. Grade final state (tests), not exact trajectories. A/B configs: Sonnet-only vs Sonnet+Opus-advisor vs Opus-only; tune escalation thresholds from data.

## Non-goals
No UI *for SCIO itself*, no multi-tenant auth, no custom vector DB, no agent debate, no parallel writers.

Amended by ADR 0007 (Product Mode, 2026-10-12): the products SCIO builds may have user interfaces and may be in any stack with a toolchain profile (ADR 0009); SCIO remains a CLI with no UI of its own.

## Acceptance criteria
- Crash mid-run resumes from checkpoint with zero duplicated side effects.
- Provider swap is config-only; full test suite passes on the Fake provider offline.
- Sample task ("add a function + tests to a small repo") passes with pass^5 ≥ 0.8 within a stated token budget.
- Advisor calls ≤ the cap, and each is logged with trigger, cost and outcome.
- The sample task produces a GitHub PR linked to its issue, with the template filled in, docs updated in the same PR, and CI green (tests + docs checks + regression evals).

## How to respond
Milestones; do NOT write the whole system at once.
M0 (this reply): (a) Mermaid graph per tier, (b) State as Pydantic code, (c) agent contract table (role, model, inputs, outputs, tools), (d) directory tree, (e) milestone plan with done-criteria: M1 harness+providers+Fake+evals skeleton + repo docs/templates/CI, M2 state+checkpointing+ACI tools+sandbox+GitHubClient, M3 router+T0 loop, M4 T1/T2+escalation, M5 observability+eval tuning, (f) assumptions + open questions. Then stop.
Later milestones: complete runnable files + tests + the command to run them. No restating requirements, no generic explanations.
