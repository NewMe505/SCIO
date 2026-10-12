# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-10-12
### Changed
- Product Mode roadmap (`docs/PRODUCT_MODE.md`, M6–M11) and ADR 0007: a product orchestrator over the task graph, toolchain profiles for any stack, milestone approvals; two defects logged for M6 (I-053, I-054).
- A passing verify whose diff is high impact (more than 8 files, dependency or schema files, a removed public symbol) now gets one advisor review before commit (ADR 0008): `review_decision`, a `triage` route from a passing verify, and an `advise -> git_checkpoint` edge for `continue`. Until now every advisor trigger required a failing verify, and no live run had ever called the advisor (I-056).
### Added
- t010_wide_rename (T1, pinned): `format_cents` becomes `format_money(cents, currency)` across nine caller modules; the correct diff passes verify and trips the impact detector, so the advisor reviews it. Fake script edits each caller in one whole-file edit because `edit_file` lints every edit (I-057).

## [0.3.0] - 2026-10-12
### Changed
- E2B is no longer a planned live tier: no account (I-029, wontfix). Local backend for own fixtures, Docker for untrusted repositories (ADR 0003).
### Added
- t008_snapshot_coupling: a formatting change that breaks a CLI snapshot test and a README example test outside the coder's filtered run; the first regression task built to make verify's full-suite step fail and the debugger run live.
- t009_verify_only_lint (T1, pinned): an undefined name in a module no test imports; the coder's `run_tests` is green and only verify's lint step fails, so the debugger must run and wire the constant rather than delete the module (hidden tests check both). First live debugger run: one turn, $0.06 of a $0.34 trial, fix plus sweep. Fake scripts gained `Step.debug_edits` for the debugger's turn.
- `tier_pin:` on an eval task runs that tier without consulting the router, which rated t008 T0 and skipped the nodes the task exists to exercise (I-050). `RunContext.tier_pin`; `route` records `classifier_model="pinned"`.
- Two realistic regression tasks: t006 (a change spanning the store module, the CLI and their tests) and t007 (a fixture whose own CI is red on `ruff` with the repo config and on `pytest`); hidden tests run the fixture's own lint rules. One live trial each passed ($0.35 and $0.40).
- Suite cost cap: `evals/runner --max-usd`; the report records spend, cap and skipped tasks.
- Nightly live suite: `scripts/nightly_live.py` (profile auth, cost cap, rows in `docs/EVAL_HISTORY.md`) registered as a Windows scheduled task on the dev machine with `--max-usd 12`; the GitHub nightly workflow gained the cap, parallel trials and auth-token secret support.
- `scio report` tool signals, counted from the worker transcripts with no model call: calls and error rate per tool, repeated identical calls within a turn, `view_file` re-reads, and whether the documenter edited docs when code changed. The documenter's transcripts are now kept as `log` artifacts like the coder's.
### Fixed
- Eval trials leaked their temp directory on Windows (636 directories): read-only git objects and an open effect-log handle, both hidden by `rmtree(ignore_errors=True)`. `remove_tree` clears the bit, `RunContext.close()` releases the handle (I-049).
- Eval fixtures are copied without `__pycache__` and tool caches, so bytecode left by running a fixture's tests in place can no longer become tracked content that shows up in the agent's commits (I-048).

## [0.2.0] - 2026-10-11
### Fixed
- Token budgets count billable-equivalent tokens (cache reads at 0.1x, writes at 1.25x) so prompt caching lowers the count it enforces (I-041).
- Prompt caching now covers the conversation: a third breakpoint on the last message block makes each worker turn re-read its transcript at cache price (I-040, `docs/IDEAS_TRIAGE.md`).
- A tool crash (e.g. `view_file` on a directory) is returned to the model as an error result instead of ending the run (I-032).
- Budget pre-check reserves half of `max_tokens` instead of all of it; explorer loops capped at 6 model calls / 8 tool calls; small task budgets raised to the tier the live router picks (I-033, I-035).
- `tests_added` wording: the tests that *prove* the features, existing or new; bug-fix tasks no longer loop on "add tests" (I-034).
- T1 verify adopts the worker's reported tests when the plan's guessed test names match nothing (I-037); worker turns capped at 12 model calls and transcripts trimmed from 30k characters (I-038); the planner no longer emits exploration steps and `normalize_steps` drops no-op steps and covers missing features (I-039).
- verify runs each reported test selector correctly: `path::name` node ids and file paths as positional pytest targets, bare words as `-k`; found by the first live Anthropic run (I-031).
- pdoc no longer warns on PEP 695 generics: `scio/prompt/repair.py` and `scio/loop/agent_loop.py` drop the `__future__` annotations import (I-008).
### Added
- `evals/runner --parallel N`: a task's trials run concurrently (independent sandboxes, checkpoints and budgets).
- Live Anthropic acceptance recorded: t001 pass^5 = 1.00 (5 trials) via an `ant auth login` profile.
- Live sandbox checks: `tests/live/test_docker_live.py` runs wherever Docker is available (CI runners included) and `tests/live/test_e2b_live.py` runs when `E2B_API_KEY` is set (I-016).

## [0.1.0] - 2026-10-10
### Added
- Observability: per-run report (`scio report`), persisted cost ledger per thread, `RunReport` with cost by role, escalations and timeline.
- A/B role configurations (`evals/configs/*.yaml`, `SCIO_ROLE_OVERRIDES_PATH`); eval runner `--config`; capability suite with scenario tasks (`fixture`, `scenario`, `expect_pass`); nightly evals workflow posting per-config tables.
- `evals/tune.py` escalation threshold sweep; `EscalationConfig` enforces `max_stall >= repeat_threshold - 1`; ADR 0006.
- Release workflow and `scripts/release_notes.py`; `SCIO_FAKE_SCRIPT=1` offline demo mode for the CLI; the CLI runs the unified graph (router picks the tier).
- README acceptance-status table; first real GitHub end-to-end run recorded.
### Fixed
- `prepare_repo` writes repo-local git excludes so test caches are never committed (I-028).
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
