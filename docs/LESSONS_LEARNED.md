# Lessons learned

One section per milestone, written when the milestone closes. **Read this whole file before starting a milestone** and say which lessons shape the plan. Every "action" below is something that exists (a file, a test, a rule, a ledger id), not an intention. Problems referenced as `I-NNN` are in [ISSUE_LEDGER.md](ISSUE_LEDGER.md).

## Standing pre-flight list (apply at the start of every milestone)

1. Read this file and the open entries in the issue ledger.
2. Check the API reference before any provider code; the spec was written against an older API surface in three places (see M0).
3. Confirm the `gh` token scopes cover what the milestone will push (`workflow` for CI files) before creating the branch (I-007).
4. Run the full local CI-equivalent (command in `CLAUDE.md`) before every push, not just the tests. A check that passes on Windows can still fail on Linux (I-011): any check touching paths needs a unit test with explicit inputs, not only a run against the repo.
5. Write files with LF endings; pass `newline="\n"` from Python on Windows (I-006).
6. One heredoc per shell command when the body contains quotes, and never pass any backslash through a shell-embedded script: escapes (`\n`, `\t`), Windows paths, regexes; use the file tools for those edits (I-001, I-010, I-027, I-036). After any shell-embedded patch, run the lint step or `python -c "import scio"` in the same command, and treat a `SyntaxWarning: invalid escape sequence` in the patch output as a failure, because Markdown has no lint to catch it.
7. Add dependencies to `pyproject.toml` and run `uv sync`; never `uv pip install` ad hoc. Export `UV_SYSTEM_CERTS=1` once per session (I-012).
8. Introspect an installed SDK (`inspect.signature`, `dir`) before writing an adapter against it; write the adapter's tests with an injected client or runner so they run without the service (M2).
9. Before pushing, compare what git will send with what the working tree has: `git status --short` must show every new package, and `git ls-files --others --ignored --exclude-standard scio evals tests scripts` must be empty (I-017). The docs check enforces the second. Note that ruff skips git-ignored files, so an ignored package is also an unlinted package.
10. When chaining checks in one shell command, start with `set -o pipefail` and never pipe a check whose exit code matters through `tail`/`grep` without it (I-018). A chain that reaches `git commit` must have been able to fail.
11. Commands that must not run after a failed check go on the same `&&` line; a heredoc's `&&` continuation goes on the opener line, because the line after `EOF` is a new command. Do not pass `-q` to pytest: `addopts` already does, and `-qq` hides the summary (I-023).
12. When adding a keyword argument that every call site should pass (a role tag, an intent), grep for every existing call site in the same change; a default value hides the ones you missed (I-026). When a node writes a value that a following edge reads, the node must not also advance past it (I-025).
13. Anything that refreshes a rotating credential (the login profile's refresh token) must exist once per process: share the SDK client across parallel trials, never build one per harness (I-044). A fan-out that works with a fresh token and fails hours later is this bug. A scheduled task registered with `schtasks` has no catch-up or battery conditions; register it from PowerShell with `StartWhenAvailable` and `WakeToRun`, and treat a missing history row as the alarm (I-045).
14. A model's structured output never names control-flow identity (the current step, the feature in scope); it reports, the plan decides (I-046). Every loop in the graph needs a cap on the passing path as well as the failing one: a pass that changes nothing is a stall. When a diagram says an edge exists (`gate --replan--> plan`), test that the code has it (I-043); the verdict list of every structured output is a list of edges to check.

---

## M0: design (closed 2026-10-09)

### What succeeded
- Reading the current API reference before designing caught three mismatches between the spec and reality: Opus 5.5 advisor output is encrypted (so the native advisor tool cannot be logged for outcome, assumption F-5), Opus 5.5 defaults to `medium` effort (F-4), and the spec names previous-generation Sonnet/Haiku models (F-2, F-3). Each became a numbered assumption instead of a silent choice.
- Numbering assumptions (F-1 to F-12) and open questions with the milestone that must answer them gave M1 a checklist instead of a vague "resolve later".
- Starting the repo from an issue, labels, CODEOWNERS and templates on day one meant M1 could follow CONTRIBUTING from its first commit.

### What failed
- Branch protection is not available on the private free-plan repo (I-009). The "no direct push" rule is convention only.

### Actions
- `docs/ARCHITECTURE.md` section (f): assumptions F-1 to F-12, open questions 1 to 8.
- Issue #1 opened from the feature template with M1 done criteria.

---

## M1: harness, prompt layer, evals skeleton, CI (closed 2026-10-10)

### What succeeded
- Writing the tests in the same pass as the code, then running the CI-equivalent locally, caught every defect before the first push: 2 test failures, 3 type errors, 80 lint findings, 6 line-ending problems. Nothing reached CI broken.
- The Fake provider as a first-class adapter made the whole harness, the repair loop and the eval runner testable offline, with cost still computed at real-model prices.
- Translation-only unit tests for the Anthropic and OpenAI adapters (request kwargs in, response objects out) gave coverage of the SDK boundary without network, while live tests stay gated behind `SCIO_RUN_LIVE_TESTS=1`.
- `scripts/check_docs.py` enforcing "model IDs only in `models.yaml`" turned a design rule into a failing check.

### What failed
- A twelve-heredoc shell command aborted and wrote nothing (I-001). Cost: one extra round of writes.
- `configure_tracing` was written as if the OTel global could be replaced per call (I-003). Cost: two failing tests; the fix also changed the test fixture design.
- Patching files from Python on Windows silently produced CRLF in six files (I-006). The sweep found one production write (`evals/report.py`) with the same latent problem.
- The push was rejected for a missing `workflow` token scope after all the work was done (I-007). Scope should have been checked before the branch was created.
- Protocols for the ledger, retrospectives and sweeps were not in place from the start; this file and the ledger were written retroactively at the end of M1.

### Actions
- `CLAUDE.md` (new): the three continuous-improvement protocols plus engineering rules, loaded in every AI session.
- `CONTRIBUTING.md`: "Continuous improvement protocols" and "File hygiene" sections.
- `.github/pull_request_template.md`: ledger ids, sweep pattern and retrospective checkboxes.
- `scio/prompt/templates/debugger/v2.md`: the Debugger role performs a codebase sweep after every root cause.
- `docs/ISSUE_LEDGER.md` (new): entries I-001 to I-011.
- `evals/report.py`: explicit `newline="\n"` on file writes.
- Pre-flight list above, items 3, 5 and 6.

### Carried into M2
- I-008 (pdoc warning) is open; I-007 (token scope) and I-011 (link checker) were closed after the first CI run.
- The `WorkerOutput` structured schema (M2 state) must gain a `sweep` field so the Debugger's codebase sweep is recorded in state and the PR body, not only in prose.
- Open questions 2 and 3 in ARCHITECTURE.md (sandbox in CI, patch sampling N) are due in M2.

---

## M2: state, checkpointing, ACI tools, sandbox, GitHub client (closed 2026-10-10)

### Lessons applied from M1
- Pre-flight item 3: token scope checked before branching (no push problems this time).
- Pre-flight items 5 and 6: every patch script avoided backslash escapes; hand edits with escapes went through the file tools. Zero CRLF files, zero corrupted patches.
- Pre-flight item 4 (platform differences): the sandbox path rules got explicit Windows and POSIX unit tests instead of only a repo run.
- Carried item: `WorkerOutput.sweep` exists and is tested.

### What succeeded
- Introspecting the installed LangGraph and E2B packages before writing the adapters (signatures of `invoke`, `task`, `interrupt`, `Sandbox.create`, `commands.run`, `files.*`) meant no API was written from memory.
- The two-layer idempotency design (effect log plus world probe) passed the crash-resume test at both crash points on the first run.
- Injected runners for Docker and `gh`, and a fake client for E2B, gave full coverage of command construction without any daemon, key or network.
- The ACI tools were testable end to end on a throwaway repo with real pytest, ruff and git, so "edit rejected by lint" and "failures only" are verified behaviour, not intent.

### What failed
- Installing packages ad hoc with `uv pip install` re-triggered the TLS failure (I-012); the M1 fix had been applied to one command, not to the environment.
- A test double parsed nested branch names wrongly (I-013): the double was less careful than the code under test.
- The error signature preferred the source assertion over pytest's evaluated one (I-014): a "first match wins" rule chose the wrong line.
- Platform-conditional stdlib typing (I-015) cost one mypy round.
- Docker is not installed on the dev machine and there is no E2B key, so two backends are verified structurally only (I-016).
- The whole `scio/aci/sandbox/` package was silently excluded from the commit by an unanchored `sandbox/` line in `.gitignore` (I-017). Local checks run on the working tree and could not see it; CI could.

### Actions
- `scripts/check_docs.py::check_ignored_sources` fails when any source file under the code directories is git-ignored (I-017). Pre-flight item 9 below.
- `pyproject.toml` owns every dependency; `UV_SYSTEM_CERTS=1` is in CLAUDE.md as the environment rule (I-012).
- `docs/adr/0003-idempotent-effects-and-sandbox-tiers.md` records the durability and sandbox decisions.
- `tests/durability/test_crash_resume.py` is the executable acceptance criterion for crash safety and runs in CI.
- Ledger I-012 to I-016.

### Carried into M3
- I-016 open: run the gated Docker and E2B live tests when a suitable machine is available.
- Register the `t0` graph in `scio.graph.registry` and replace the hand-drawn T0 diagram in ARCHITECTURE.md with a generated section.
- Open question 3 (patch sampling N) is deferred to M4 where sampling is built.
- Every side-effecting node must name an `intent` and a `probe`; make that the node template shape from the first M3 node.

---

## M3: router and T0 loop end to end (closed 2026-10-10)

### Lessons applied from M2
- Pre-flight 9 and 10: `git status` reviewed before the push; every check chain started with `set -o pipefail`. No ignored packages, no masked failures.
- Carried items: `t0` is registered and its generated diagram sits next to the M0 sketch; every side-effecting node names an intent and a probe.
- Pre-flight 8: LangGraph's `interrupt`/`Command` and the serializer constructor were introspected before use.

### What succeeded
- The whole T0 graph ran end to end on the first smoke attempt: route, clone, implement, verify (real pytest, real ruff), commit, document, gate, interrupt, resume, hidden tests green. The layered design from M1/M2 meant the graph was mostly wiring.
- A schema-keyed `ScriptedResponder` (decide by the requested output schema, then by role and turn count) made one fake drive the entire run and every exit-reason variant with small flags.
- Verify's fixed order (changes → feature tests → suite → lint) caught the "trivially passing" hole during design: a whole-suite check alone would have passed an agent that wrote nothing.

### What failed
- The recorded live run is blocked by an invalid API key in the environment (I-019). The code path is exercised to the first request only.
- Project tooling linted and type-checked the agent's fixture repository (I-020).
- LangGraph's serializer allow-list was unknown until the first checkpoint warning (I-021).
- Two queue-based loop tests enqueued the wrong number of scripted replies (I-022).

### Actions
- `docs/adr/0004-loop-and-verification.md`.
- `scio.graph.checkpointer.serializer()` allow-lists every state type; the Postgres path is flagged for the same treatment.
- `pyproject.toml` excludes `evals/tasks/*/repo` from ruff and `evals/tasks/` from mypy.
- `tests/graph/test_t0_fake.py` is the executable acceptance for the happy path and all five reachable exit reasons; `tests/graph/test_t0_fake.py::test_every_exit_reason_is_covered` fails if a new `ExitReason` is added without a test.
- Ledger I-019 to I-023.

### Carried into M4
- I-019: run `python -m evals.runner --suite regression --provider anthropic --trials 5` once a valid key is present; record pass^5, tokens and cost here and in the PR.
- T1/T2: `after_route` currently halts non-T0 tiers; M4 replaces that with the plan/explore subgraphs and the six-signal `triage` edge. `check_exit` already returns `ADVISOR_CAP`'s neighbours; the advisor cap exit is added with the `advise` node.
- Move test ownership from `tests_added` to the plan (`PlanStep.feature_ids` → `FeatureItem.test_filter`).
- Postgres checkpointer: construct with the serializer allow-list (I-021 sweep).
- The gate should receive docs-check output directly, not only through the PR body (ADR 0004 consequence).

---

## M4: T1/T2 tiers and the escalation policy (closed 2026-10-10)

### Lessons applied from M3
- All five carried items closed: T1/T2 replace the halt; the plan owns `test_filter` (the planner must set one per feature; `tests_added` remains the T0 fallback); the Postgres saver is built with the serializer allow-list; the gate runs the docs check itself; `advisor_cap` is an exit and interrupts for a human.
- Pre-flight 11: every chained check started with `set -o pipefail`, pytest ran without `-q`, and no command followed a heredoc on its own line. No masked failures this milestone.

### What succeeded
- Making triage a pure function that `verify` stores and the edge reads gave the six-signal policy a unit-test surface with no graph at all: one flip test and one non-flip test per signal, the never-escalate list, both caps, and the exit precedence.
- One builder for all tiers (`build_graph(ctx, tier)`) kept T0 byte-for-byte reusable: the M3 tests passed unchanged on the unified graph before any T1/T2 test was written.
- A data-driven fake (`TaskScript` per task, schema-keyed replies) drove five real eval tasks across all three tiers and every escalation scenario with four switches.
- Span attributes made the one-writer invariant an assertion over traces rather than a claim in prose.

### What failed
- The fake answered the router with a plan because marker detection was ordered wrongly (I-024).
- `verify` advanced the step cursor before the edge evaluated it, and `git_checkpoint` stamped every feature with the first commit (I-025). Two separate defects behind one symptom; the second only appeared after the first was fixed.
- Writer toolboxes created in M3 were untagged, so the trace assertion saw `agent` (I-026). A new keyword argument added in M4 was not applied to the call sites that predated it.

### Actions
- `docs/adr/0005-escalation-policy.md`: signals as implemented, caps, brief, outcome rule, step advancement at checkpoint time, ADRs in the target repository.
- `tests/unit/test_escalation.py` (six flip / non-flip pairs, never-escalate, caps) and `tests/graph/test_t1_t2_fake.py` (T1 happy paths, repeated-error escalation resolved, advisor cap interrupt, plan drift with a generated ADR, advisor `escalate_human`, T2 parallel explorers with one writer, T0 docs task via the router).
- Ledger I-024 to I-026. Pre-flight item 12 below.

### Carried into M5
- I-019 still blocks the live Anthropic acceptance run; M5's A/B configs need it.
- Tune `configs/budgets.yaml` `escalation` thresholds and `advisor_calls_max` from eval data; decide whether to enable `sample_n > 1` (open question 3) after measuring its token cost.
- Plan drift is evaluated only after a failing verify (ADR 0005 consequence); check the capability suite for silent scope creep on passing runs.
- Nightly capability suite and release workflow (M5 scope).

---

## M5: observability, eval tuning, release (closed 2026-10-10)

### Lessons applied from M4
- I-019 was closed as a decision, not a fix: the owner is on a Max subscription, so the key stays a placeholder and every measurement is labelled Fake-provider data. The nightly workflow switches to the real provider on its own when a repository secret appears.
- Thresholds were set from a recorded sweep (ADR 0006), with the data's limits written down next to the numbers.
- Pre-flight 12: the new `ToolBox`-style keyword (`scenario`, `fixture`) was applied to the runner, the solver and the task files in the same change.

### What succeeded
- The first run against a real GitHub repository worked end to end on the first attempt: issue intake, branch push, draft PR with the template and `Closes #1`, docs in the same PR, gate, ready, and that repository's CI green.
- The tuning sweep produced an actionable result and, more usefully, a constraint (`max_stall >= repeat_threshold - 1`) that is now enforced in code.
- The report builds from durable state and a persisted ledger, so observability does not depend on the process that ran the task.

### What failed
- The shell tool unescaped `\n` inside two patch scripts and corrupted two modules (I-027), a recurrence of I-010 despite the rule. The rule was not enough; the guard is now mechanical (lint or import immediately after any shell-embedded patch).
- The real PR committed `__pycache__` files (I-028). Every Fake-provider test counted commits and passed; only the real PR's file list showed it. Tests now assert the tracked file list.

### Actions
- `docs/adr/0006-escalation-thresholds-from-data.md`; `EscalationConfig.__post_init__` enforces the coupling; `evals/tune.py` skips invalid cells.
- `prepare_repo` writes `.git/info/exclude`; `test_happy_path_end_to_end` asserts no cache files are tracked.
- Pre-flight item 6 extended with the mechanical check; ledger I-027, I-028.
- README acceptance table with evidence links; CHANGELOG cut to 0.1.0; release workflow.

### Live validation (2026-10-11, after 0.1.0)
The first runs with a real model found five problems the Fake provider structurally could not (I-031 to I-035): verify built a `-k` expression out of pytest node ids; a `view_file` on a directory crashed the run; the budget pre-check refused calls with 20% left; bug-fix tasks reported no "added" tests and were told to add some; task budgets were sized for the hinted tier, not the routed one. All five were in the harness, none in the model: the live coder fixed every task in one iteration once verify stopped getting in its way.

Lessons:
- A scripted fake reproduces the *shapes* you imagined. Before trusting a fake, write down the shapes a real model is likely to produce (node ids, empty lists, directories as paths, extra JSON fields) and script those too. The t001 script now reports a node id and a keyword for this reason.
- Keep the working directory of the first live run of every task (`GraphSolver(keep_dirs=True)`) and read the artifacts and the per-role ledger before changing anything. Each diagnosis above took one kept run.
- Run one live trial, read the cost by role, then scale. The whole validation cost about $6.
- Fan trials out (`--parallel`), not tasks: tasks share nothing but the rate limit, and the per-trial ledger stays readable.
- The planner is a model too. Given a task that says "the entry point is not documented", Opus planned an exploration step, which a verify built on "the tree must change" can never pass (I-039). Every structured output that drives control flow (plans, routings, verdicts) needs a normaliser that enforces the invariants the graph assumes, and the prompt should state those invariants in the model's terms ("every step changes code or tests and is provable by tests").
- Live T1 runs are three to five times the cost of T0 runs on the same task (t003: $0.64 vs $0.13 for a T0 docs task of similar size), almost all of it in coder turns that resend their transcript. Context growth per turn, not the number of model roles, is the cost driver to work on next (I-038).

### Realistic tasks (2026-10-12, issue #25)
Two regression tasks were added for the realism gaps in `docs/IDEAS_TRIAGE.md` item 7: t006, a change that spans the store module, the CLI and their tests, and t007, a fixture whose own CI (`ruff` with the repo's rule set, then `pytest`) is red. Both passed their first live trial ($0.35 and $0.40, two iterations each, no escalation).

Lessons:
- Hidden tests can run the fixture's own CI: t007's hidden test shells out to `ruff check .` in the repo root, so the agent is graded on the rules the repository selected, not on verify's narrow syntax-and-undefined-names lint. The live coder removed the unused import without a lint tool, from reading `pyproject.toml` and the workflow.
- Read the gate's non-blocking caveats on every first live run. Both runs passed, and both gates mentioned committed bytecode; that was a real defect in the eval harness (I-048), not a model quirk.
- Never run a fixture's tests inside `evals/tasks/*/repo`. Copy it out first (`make_origin` now drops caches either way). The fixtures are the agent's starting state, and anything on disk there ships with it.

### Carried forward (post-0.1.0)
- I-019 (placeholder key): re-run the regression suite, the A/B configs and `evals/tune.py` live when a key exists; revisit ADR 0006 decision 1 with real resolution rates. Update 2026-10-12: the suite and configs run nightly; the sweep still waits, because no live run has escalated (31 task rows, zero advisor calls) and a sweep over runs that never escalate measures nothing.
- I-016: Docker and E2B live checks on a machine that has them.
- I-008, I-009: cosmetic pdoc warning; branch protection needs GitHub Pro or a public repository.
- Open question 3 (patch sampling): the sampling step is wired but off; measure before enabling.
