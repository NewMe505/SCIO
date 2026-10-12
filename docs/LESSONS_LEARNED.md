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

### Snapshot coupling task (2026-10-12, issue #29)
t008 asks for a formatting change whose obvious implementation breaks a CLI snapshot test and a README example test that a filtered `run_tests` never executes. It was built so that verify's full-suite step fails and the debugger runs live for the first time (ADR 0006 decision 4 has never had a live data point). Two live trials on `sonnet_opus_advisor`:

| trial | tier | result | iterations | tool calls | cost | debugger called |
|---|---|---|---:|---:|---:|---|
| 1 (router) | T0 | pass | 2 | 17 | $0.19 | no (T0 has none) |
| 2 (`tier_pin: T1`) | T1 | pass | 2 (one per step) | 23 | $0.39 | no |

What happened instead:
- The router (Haiku) rated the task T0 at confidence 0.70: "the mention of docs and the test suite is minor verification rather than scope expansion". The hint in `task.yaml` only sizes the budget. `tier_pin:` now makes the route deterministic for tasks built around one path (I-050).
- With the pin, the planner split the work into two steps and the coder, in step S1, ran the whole suite itself before summarizing, saw the snapshot test fail, and regenerated `tests/golden/demo.txt` even though the step's file list did not name it ("outside expected files ... needed for full suite to pass"). Verify then passed first time on both steps. In the T0 trial the coder also ran the full suite, stopped after finding the golden file, and fixed it on the next turn.
- So the trap was defused by the coder's own habit of running the full suite, not by verify. The debugger is still live-untested. A task that forces it needs a failure the coder cannot see from inside its own turn: a check verify runs and the coder's `run_tests` does not (lint on the repo's rule set, a hidden-but-declared CI step), or a test that only fails on a fresh checkout.

Lessons:
- A hint is not a pin. Every eval task whose purpose is a specific graph path must state that path in a way the harness enforces; the router is a model and it is told to prefer the cheaper tier.
- Design a "must fail verify" task from what the worker *cannot observe*, not from what the plan's filter omits. Sonnet runs the full suite unprompted; a filtered-out test is not hidden from it.
- Read the cost ledger by role before claiming a node ran. `tier=T1 iterations=2` looked like plan, fail, debug; the ledger had no debugger row and the second iteration was the second step.
- Two causes can hide behind one `ignore_errors=True` (I-049): read-only git objects and an open SQLite handle each blocked the cleanup, and 636 trial directories accumulated silently. Cleanup code logs failures; it never ignores them.

Actions: `tier_pin` (`EvalTask`, `RunContext`, `route`; `tests/unit/test_tier_pin.py`); `remove_tree` and `RunContext.close()` (`test_remove_tree_deletes_read_only_git_objects`, leak assertion in `test_cli_exit_codes`); ledger I-049, I-050; this section. Open: a task that makes verify fail against a full-suite-running coder (candidate: t007's pattern with a lint-only failure in a file the step does not touch).

### Verify-only failure task (2026-10-12, issue #31)
t009 applies the lesson from t008: the failure must be one the coder cannot observe from inside its own turn. The fixture's `metrics/report.py` uses `DECIMALS` without importing it; no test imports that module, so `run_tests` is green at every filter and only verify's lint step (`ruff --select E9,F63,F7,F82,F821` over the tree) fails. The task itself is an unrelated feature (`stdev`). One live trial on `sonnet_opus_advisor`, pinned T1:

| tier | result | iterations | tool calls | cost | debugger called |
|---|---|---:|---:|---:|---|
| T1 | pass, 4 hidden tests | 2 | 14 | $0.34 | yes: 9 calls, $0.06 |

What happened: the coder added `stdev` and its tests in one turn (filters `stats`, `stdev`, `test_stdev` all green, full suite green); verify reported `lint_only`; triage took the default route to debug; the debugger viewed `report.py` and `stats.py`, added `DECIMALS` to the import, searched the repository for the same construct, reran the tests and reported a sweep with the files it ruled out; the second verify passed and the rest of the run (review, docs, gate) was ordinary. The debugger did not delete the module or the rounding (hidden test). First live data point for ADR 0006 decision 4: one debug turn resolved a lint-only failure at about a fifth of the run's cost.

Lessons:
- The reliable way to force a graph path in an eval is to put the failure outside the worker's tools. Here the coder has `run_tests` and no lint tool, so an undefined name in an unimported module is invisible to it and visible to verify. Build path-specific tasks from the tool gap, then pin the tier (I-050).
- The debugger prompt's "sweep the repository for the same construct" produced a real sweep (pattern, files checked, files ruled out) on the first live run; keep the structured `sweep` field, it is the evidence that the fix was not local.
- Hidden tests for a lint-only task must check the *fix*, not just the lint: `format_report([1, 2, 4]) == "mean=2.33 median=2.0"` rules out deleting the module or the rounding, which would also silence F821.
- Hidden tests under `evals/` are linted by SCIO's own ruff config (I-051): write them to that standard, not the fixture's.

Actions: t009 fixture, hidden tests and fake script (`Step.debug_edits`, so the scripted debugger has something to apply when the step was correct); `test_t1_lint_only_failure_runs_the_debugger` asserts the debugger ran at iteration 2 and was shown a lint failure and no test failure; ledger I-051, I-052; this section.

### Advisor review of passing high-impact diffs (2026-10-12, issue #37, ADR 0008)
Issue #37 set out to force the advisor live with a wide rename. A read-only pass over `escalation.py` and `verify.py` before writing any code showed the premise was wrong: every trigger was evaluated only after a failing verify (I-056). The owner chose the design change: a passing verify with unreviewed high-impact flags now gets one pre-commit advisor review; `continue` commits without another coder turn. t010 (rename `format_cents` to `format_money` across nine modules, pinned T1) is the first task built on it. One live trial on `sonnet_opus_advisor`:

| tier | result | iterations | tool calls | cost | advisor calls |
|---|---|---:|---:|---:|---|
| T1 | pass, 3 hidden tests | 3 | 57 | $0.64 | 2, both resolved ($0.08) |

What happened:
- The planner split the work into S1 (`money.py` and its tests) and S2 (migrate the callers). S1's two-file diff passed verify but removed the public `format_cents`, which the impact detector flags on its own, so the ADR 0008 review fired on a diff of two files, not eleven. Opus answered `continue` ($0.03) and the step committed with no extra coder turn.
- S2 was the first *natural* live escalation SCIO has ever produced: the plan's `test_filter` named a test that did not exist, verify failed on "no tests collected", the coder had reported low confidence and had touched files outside the step's list, so `low_confidence, plan_drift` routed to `advise`. Opus answered `continue` with guidance ($0.05); the next coder turn made verify pass and the record resolved. Both ADR 0005 paths (review and failure) now have a live data point in one run.
- The fake coder's first version of the rename was rejected edit by edit: `edit_file` lints each edit and reverts one that leaves a name undefined, so an import line and its call site cannot change in two steps (I-057). A real coder would hit the same wall if it edited in halves.

Lessons:
- Read the entry condition, not the signal list. A trigger that exists in `detect` is only reachable from the node that calls `decide`; the eval premise failed on the call site, not the signal.
- The impact detector fires on *what* changed before *how much*: a removed public symbol flags a two-file diff. Tasks that want the file-count trigger must keep the rename and the migration in one step, or accept that the review lands on the first step.
- Natural escalations come from the harness's seams (a planned filter that matches nothing, a worker reporting low confidence), not from hard code. Fixing those seams will make the advisor rarer again; keep ADR 0008 so it still has a deterministic path.
- Rename edits are atomic per file. Scripted or live, change the import and every use in one `edit_file` call.

Actions: ADR 0008; `review_decision`, verify/advise routing, `advise -> git_checkpoint` edge, regenerated Mermaid; t010 fixture, hidden tests, fake script with whole-file caller edits; `test_passing_high_impact_diff_is_reviewed_once`, `test_t1_passing_high_impact_diff_gets_one_advisor_review`; ledger I-056, I-057; ADR 0006 live-data note updated. Open: a test_filter the planner invents should be rejected by the plan normaliser (it names a test that does not exist yet and the step does not add); candidate ledger entry once reproduced on the Fake.

## M6: Foundations for Product Mode (2026-10-12, issue #39, ADR 0009)

### Lessons applied
- Pre-flight 12 (apply a new indirection at every call site in the same change): a read-only research pass produced the full inventory of Python-specific literals before any code was written: two command builders, one lint builder, one script runner, five prompt strings, two file-count helpers, the signature parser, the drift and impact file rules, two cache lists, and the settings fields nobody read. Every item became an edit or a test.
- I-050 and I-053 (a hint is not a pin): `scio run --tier` now pins the route with the same `tier_pin` the evals use, and a budget that was only a default follows the tier the classifier picks.
- Pre-flight 6 and 10: all edits went through the file tools; every check chain started with `set -o pipefail`.
- The live-trial rule: one kept t001 trial ran on the refactored code before claiming "no behaviour change": pass, $0.11, 1 iteration, 4 tool calls, the same shape and cost as the nightly rows before M6.

### What succeeded
- The `python` profile reproduces the pre-M6 commands byte for byte, and `test_toolchain.py` says so with the old literals copied into the test, not re-derived. The regression suite passed at pass^5 = 1.00 with no fixture or script change, which is the milestone's definition of "no behaviour change".
- The boundary is mechanical: `test_no_stack_specific_command_outside_the_toolchain_package` scans `scio/` for command tokens and cache literals outside `scio/toolchain/`. It passed on the first run after the extraction, which means the inventory had been complete.
- The I-054 sweep found four more declared-but-unread settings (I-059). Grepping every field of a settings model for a reader is cheap and should be a standing check.

### What failed
- The first live t001 attempt failed before reaching the model: the `ant auth login` profile's refresh token had died (`invalid_grant`, I-060) after serving ten live trials earlier the same day. Nothing in SCIO can refresh the profile; the owner logged in again and the retry passed. Budget the re-login into any day with more than a handful of live runs.
- A `Protocol` with plain attribute annotations does not accept a frozen dataclass (mypy: "expected settable variable, got read-only attribute"). Protocol members that profiles implement as dataclass fields must be declared as read-only properties.
- `projections.py` imports from `assembler.py`, so the profile-derived prompt constants had to live in the assembler, the lower module, not where they were first written (a circular import caught at the first run).

### Actions
- `scio/toolchain/` (`base.py`, `python.py`, registry), `configs/profiles/python.yaml`, ADR 0009; `Settings.toolchain`; `RunContext.toolchain`, `make_sandbox`, `apply_tier_budgets`; `route` resizes a default budget; `scio run --tier` pins.
- `tests/unit/test_toolchain.py` (profile equality with the old literals, exit-code semantics, file rules, the boundary scan); `tests/unit/test_m6_foundations.py` (backend selection, effects path, live-test gate, default budget follows the routed tier, explicit budgets untouched).
- PROMPT.md non-goal narrowed; ARCHITECTURE F-8 superseded; README ADR list; CHANGELOG; ledger I-059, I-060.

### Carried into M7
- Stack detection for existing repositories and the `node-ts` profile are M10; until then `SCIO_TOOLCHAIN=python` is the only accepted value.
- The sandbox still carries SCIO's own Python runtime facts (`python` argv rewrite, `PYTHONUTF8`, `PIP_NO_INDEX`); M9's two-phase network design decides whether those move into the profile or stay with the sandbox.

## M7: Greenfield bootstrap (2026-10-12, issue #42, ADR 0010)

### Lessons applied
- A read-only research pass before any code (as in M6) listed the mechanics to reuse: `interrupt` payload shapes and `Command(resume=...)`, the structured-output and role pattern, static prompt templates, the Fake responder's schema markers, the serializer allow-list, `run_effect` with world probes, and the CLI's dispatch. Every one was reused rather than reinvented; the product graph is 400 lines because of it.
- I-039 and I-046: the planner's draft goes through a normaliser (`scio/product/spec.py`) before a human sees it, and the notes about what was changed are in the ledger and the approval payload.
- The live-trial rule: one kept `scio new` run from an empty directory, read before claiming done.

### What succeeded
- The whole path ran on the Fake on the first attempt after lint and types were clean: interview pause, answers, spec pause, approval, bootstrap, green local CI, crash-style resume with no duplicate commit, three rejections halting, the CLI contract. Twelve tests, then a thirteenth for the CLI.
- The live run from an empty directory: Opus asked four questions that were all about things the prompt left open (who installs it, the day-one command sequence, runtime and storage, what the export is for), wrote a ten-feature spec whose criteria are black-box assertions (exact outputs, exit codes, file paths), the scaffold's lint and tests were green, and the bootstrap commit carried the marker. Cost $0.15: interviewer $0.025, planner $0.127, no other roles.
- Ground truth before commit: the profile's lint and test commands ran on the scaffold and a failure would have halted with `bootstrap_failed`; no model verdict is involved in the bootstrap.

### What failed
- The first live run never reached a model: `scio new --prompt ... --thread new-live1` resumed a thread that did not exist (I-061). The Fake tests drove the library, not the CLI, so the contract was untested; the CLI test now drives all three calls.
- The first lint and type run after writing the graph found eight findings each: an unused import, three list concatenations, four long lines in the licence text, a `Protocol`-typed LangGraph call the task graph avoids by typing its compiled graph as `Any`, and `BudgetExceeded` imported from the wrong module. All caught by the chain in the same step, none reached a commit.
- The planner's `P1` ("`todo --version` prints `todo 0.1.0`", a console command named `todo`) is not what the scaffold builds (`python -m tagtodo --version` prints `0.1.0`), yet `progress.json` marks P1 done because the scaffold's own smoke test passed. The skeleton the planner describes and the skeleton the scaffold ships are two different things.

### Actions
- `scio/product/` (graph, spec normaliser and renderer, scaffolds), `ProductState` and the two output schemas in `scio/state/models.py`, roles `interviewer` and `product_planner` with templates, `scio new` in the CLI, Fake markers `interview_questions` and `product_name`.
- `tests/graph/test_product_new_fake.py` (end to end, resume, rejection cap, normaliser invariants, CLI contract); ADR 0010; README usage; CHANGELOG; ledger I-061.

### Carried into M8
- P1 ownership: either the scaffold must satisfy the planner's P1 criteria (install a console script named after the product and print `<name> <version>`), or P1 must be run as the first feature task against the scaffold instead of being marked done. Decide in M8 before the milestone loop trusts `progress.json`.
- `scio report` has no product-thread view; add one when the milestone loop produces escalations and costs worth reporting.
- Publishing (`--gh`) is implemented behind a confirmation interrupt but has no Fake coverage: `gh repo create` needs an injected runner like `GitHubClient` has.

### Carried forward (post-0.1.0)
- I-019 (placeholder key): re-run the regression suite, the A/B configs and `evals/tune.py` live when a key exists; revisit ADR 0006 decision 1 with real resolution rates. Update 2026-10-12: the suite and configs run nightly; the sweep still waits, because no live run has escalated (31 task rows, zero advisor calls) and a sweep over runs that never escalate measures nothing.
- I-016: Docker and E2B live checks on a machine that has them.
- I-008, I-009: cosmetic pdoc warning; branch protection needs GitHub Pro or a public repository.
- Open question 3 (patch sampling): the sampling step is wired but off; measure before enabling.
