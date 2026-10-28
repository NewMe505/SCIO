# Lessons learned

One section per milestone, written when the milestone closes. **Read this whole file before starting a milestone** and say which lessons shape the plan. Every "action" below is something that exists (a file, a test, a rule, a ledger id), not an intention. Problems referenced as `I-NNN` are in [ISSUE_LEDGER.md](ISSUE_LEDGER.md).

## Guardrails (Always / Never)

Every bug and every correction becomes one rule here (the owner's rule, 2026-10-27). A rule starts with **Always** or **Never**, cites its ledger id (or the correction it came from), and names its **guard**: what fails when the rule is broken. A rule is the index; the guard is what stops drift. Prefer, in order: a structural fix that makes the wrong thing impossible (`symbol:`), a regression test shown to fail on the old code (`test:`), a scan of the whole codebase (`test:` or `check:`), a Claude Code hook (`hook:`), and `manual:` with the reason only when nothing can check it. `scripts/check_docs.py::check_guardrails` fails when a resolved ledger entry from I-104 on is cited by no rule, or when a named guard does not exist.

1. **Never** let a commit, merge or tag that SCIO makes depend on the machine's git identity; pass `SCIO_GIT_IDENTITY` (I-104). Guard: `symbol:SCIO_GIT_IDENTITY`, `test:test_upgrade_brings_a_pre_m11_product_to_the_current_scaffold`
2. **Always** run the tests with git as a CI runner sees it: no global config, no guessed identity (I-104). Guard: `symbol:_ci_git_identity`
3. **Always** confirm the previous push's CI on the branch finished green before pushing again (I-104). Guard: `hook:push_gate`
4. **Never** assume a docker-live test runs only in the `docker-live` job; both CI jobs have a daemon, so a test that needs the built image skips only when no image is named (I-105). Guard: `test:test_a_node_product_installs_and_tests_inside_the_container`
5. **Never** place a product in a sandbox image that cannot run its stack; probe the image for the stack's capability before choosing the backend (I-101). Guard: `test:test_auto_probes_the_image_for_the_product_stack`, `test:test_the_readiness_check_runs_the_probe_inside_the_image`
6. **Never** drop a human's commit when syncing `main` after a merge; only SCIO's own bookkeeping commits may be reset (I-102). Guard: `test:test_github_mode_never_drops_a_human_commit_on_local_main`, `test:test_github_mode_survives_squash_merged_milestone_prs`
7. **Never** tag a release outside `main`'s history; with `--gh` the cut goes through a release PR and the tag goes on its merge (I-103). Guard: `test:test_github_mode_survives_squash_merged_milestone_prs`, `test:test_github_mode_crash_after_the_pr_duplicates_nothing`
8. **Always** confirm on GitHub that a PR is merged (`gh pr view N --json mergedAt` non-null) before any post-merge step (I-087). Guard: `manual:` a reported merge arrives in conversation, which no check can see; CONTRIBUTING step 5 carries the command
9. **Never** run a bare `git stash`; other sessions share the stash stack (owner correction). Guard: `hook:push_gate`
10. **Always** end a status update with an ETA in minutes and its basis (owner correction). Guard: `manual:` a property of chat replies, not of the repository; kept in the session memory
11. **Never** commit a Python file that fails `ruff check` or `ruff format --check`; lint runs before the commit, not first at the pre-push chain (I-106). Guard: `hook:push_gate`, `test:test_the_commit_gate_lints_every_changed_python_file`
12. **Never** let SCIO reset, check out or merge over a hand edit of PRODUCT.md; the edit is committed as bookkeeping on a milestone branch and re-read by the next milestone (I-109). Guard: `symbol:carrying_product_md`, `test:test_a_hand_edit_of_product_md_survives_resets_merges_and_reaches_main`
13. **Never** let a quality bar promise tools the stack's profile does not use, and never let the interviewer offer a choice the profile settles (I-107). Guard: `symbol:resolve_quality_bar`, `test:test_a_quality_bar_never_promises_tools_the_profile_does_not_use`, `test:test_the_interviewer_and_planner_see_what_each_stack_fixes`
14. **Always** read a checkpoint with the model of the graph that wrote it; every kind of thread (task, `scio new`, `scio build`) has a report (I-108). Guard: `symbol:load_product_state`, `test:test_cli_build_status_and_report`
15. **Always** record a thread's spend across processes: a resume carries the checkpoint's spend into the new process, and the guard still caps each process (I-110). Guard: `symbol:carry_spend`, `test:test_a_thread_records_the_spend_of_every_invocation`
16. **Always** match a Python definition by name with its generic form too (`def name[T](`), not only `def name(` (I-111). Guard: `test:test_guard_exists_looks_in_the_repository`
17. **Always** make an edit whose text contains a backslash with the Edit or Write tool, never through a shell-embedded script; a script edit asserts its anchor count before it writes (I-071, I-072, I-112). Guard: `manual:` a hook cannot tell a mangled backslash from an intended `\n` escape; the anchor assert turns a miss into a loud failure, never a silent wrong edit
18. **Never** let a test fake accept what the real service refuses; a fake GitHub refuses a missing label or milestone, and SCIO creates a resource before it names it (I-113). Guard: `symbol:ensure_label`, `test:test_an_issue_label_is_created_once_and_an_existing_one_is_left_alone`
19. **Never** end a CLI command with a traceback when an external service refuses a step; say what failed and how to resume from the checkpoint (I-100, I-113). Guard: `test:test_a_github_refusal_stops_scio_build_with_a_resume_hint_not_a_traceback`

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
15. A node that interrupts does nothing else: a node re-runs from its first statement when the graph resumes, so a model call (or any effect without a probe) before `interrupt()` in the same node runs twice and the human answers a draft they never saw (I-065). Put the stop in its own node that reads from state, and test it on the Fake with a responder whose answer changes on every call.

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

## M8: Decompose + milestone loop (2026-10-12, issue #44, ADR 0011)

### Lessons applied
- Pre-flight 1: the whole file and the open ledger entries were read first. Two entries (I-053, I-054) were still `open` although M6 had fixed them; they were completed before any M8 code (an entry is finished when the fix lands, not when the retrospective mentions it).
- Pre-flight 8: a six-line probe script confirmed, before the design was fixed, that a graph invoked inside another graph's node with its own `thread_id` gets an independent checkpoint thread (namespace `""`, resumable from a fresh graph object). The nested-subgraph design rests on that measurement, not on the LangGraph docs.
- I-046 (the model reports, the plan decides): the milestone planner proposes a grouping and tiers; `normalise_milestone` enforces PRODUCT.md order, a consecutive prefix from the first open feature and the cap, and the orchestrator alone picks the next feature.
- I-050 and I-053 (a hint is not a pin): every feature's `RunContext` carries `tier_pin`, so the router is never consulted inside a product build.
- ADR 0003: every side effect in the loop (milestone branch, issue files and their commit, GitHub milestone, issues, the fast-forward after a feature, push, PR, ready, merge or pull, the progress commit) is a `run_effect` with a world probe, and the crash tests kill the process at the second feature's gate and after the PR.
- The M7 carry-over (P1 ownership) was decided first, as option (b): every feature starts `todo` and P1 is a feature run. The M7 test that asserted P1 `done` was changed with the ADR, not worked around.

### What succeeded
- The whole loop ran on the Fake at the first attempt once lint and types were clean, except for one real harness defect it exposed (below): planner, issues, P1 at T0, P2 at T1, fast-forwards, integration checks, gate, PR file, merge, progress commit, second milestone, done, and the product's own CI green on `main`.
- Sharing the orchestrator's open checkpointer with each feature subgraph (`run_graph(..., saver=...)`) avoided a second SQLite connection inside a running node; the deterministic thread id per attempt made crash resume a probe (`thread_started`) instead of state bookkeeping.
- `Harness.child` gave each feature its own budget guard and cost ledger while keeping one provider registry (and so one SDK client per process, I-044); the product's `FeatureRun` records cost, iterations and escalations straight from the subgraph's final `SCIOState`, and the typed-handoff test compares the two.
- The GitHub path was tested end to end without GitHub: a canned `gh` runner for milestones, issues, PR and merge state, plus a bare local `origin` for the real push and pull.

### What failed
- The first Fake build stalled on P1: its only edit creates a new test file, and verify's "something changed" check read `git diff --stat`, which ignores untracked files (I-063). Ten eval tasks and every live run had edited an existing file, so the hole was invisible. The P1 script is now a new-files-only task in CI.
- `uv.lock` had been stale since 0.3.0 (I-062): the release chore never relocks. `test_version.py` now checks the lock.
- A crash test counted commits on the feature branch and forgot that the branch includes the milestone's earlier commits; the assertion, not the code, was wrong (`milestone/1..agent/...` ranges fix it).
- The first live `scio new` after M8 approved a different `PRODUCT.md` from the one shown ("tagtodo, 8 features" on screen, "todo_cli, 7 features" on disk) and paid the interviewer and the planner twice (I-065): both M7 nodes made their model call and interrupted in the same node, and a node re-runs from its start on resume. The M8 loop had been written with its stops in separate nodes (`gate_decision`, `merge_wait`) without going back to M7. The scripted Fake returns the same draft every call, so no test could see it; the guard test now varies the draft per call and counts calls. Pre-flight item 15.

### Live milestone (2026-10-12, `sonnet_opus_advisor`)
`scio new` from an empty directory (interview, approval, bootstrap: $0.13 across four Opus calls, two of them the I-065 duplicates) then `scio build` for one milestone on the resulting `todo_cli` product (seven features in PRODUCT.md). The planner grouped P1 to P4 as M1 "Walking skeleton, storage, add and list" with P1 and P4 at T0, P2 and P3 at T1.

| feature | tier | result | iterations | tool calls | cost | escalations | what happened |
|---|---|---|---:|---:|---:|---|---|
| P1 walking skeleton (console script `todo --version`) | T0 | skipped by human | 1 | 22 | $0.21 | 0 | the coder wrote the entry point, `[project.scripts]` and a subprocess smoke test, then asked a design question: the sandbox has no install step, so `todo` is not on PATH (M9 scope) |
| P2 JSON file storage | T1 | pass | 4 | 44 | $0.77 | 1 resolved (`plan_drift, high_impact`, advisor `change_approach`) | verify failed on a test P1 had left in the tree (I-067); the advisor named the drift, the guided turn fixed it, two more steps passed first time |
| P3 `todo add` with tags | T1 | pass | 2 | 23 | $0.52 | 0 | plan, localize, one coder turn per step |
| P4 `todo list` | T0 | pass | 1 | 12 | $0.23 | 0 | one coder turn |
| orchestrator | | | | | $0.05 | | milestone planner $0.023, milestone gate $0.030 |

Integration checks on `milestone/1`: lint clean, 20 tests passed. The milestone gate returned `escalate_human` on its own ("P1 has no commit, so no feature run covers its criteria") before the orchestrator's override could apply; `proceed` opened the PR file, `merge and stop` merged `milestone/1` into `main` with the `[scio:merge:M1]` marker and recorded M1 in `progress.json` ($1.73 features, $1.78 total). Human stops: four (P1's decision, the gate, the merge, and the approval of PRODUCT.md).

Lessons:
- The planner's P1 criteria need an installed console script, and the M8 sandbox cannot install anything (`PIP_NO_INDEX`, no `install_deps` until M9). The coder diagnosed it exactly and asked instead of faking a pass; the orchestrator turned that into one decision. Until M9, the walking-skeleton prompt should ask for `python -m <pkg>` criteria only, or the planner's P1 should be rewritten by the normaliser.
- A failed attempt leaves a dirty tree, and the next feature inherits it (I-067). The orchestrator now resets the milestone branch before every fresh attempt.
- Every stop that re-runs a model call pays twice and may act on a draft nobody saw (I-065). Found only because the live planner is non-deterministic; the Fake now varies drafts in the guard test.
- After the merge the loop went straight into planning M2 with no stop and no cap; `merge and stop` now exists and threads are named per starting milestone.
- `scio report` on a feature thread inside a build crashed (I-066): the thread kind must come from the checkpoint, not the name.
- Cost shape: a T1 feature on a small product costs $0.5 to $0.8, dominated by coder turns (59% of P2, 53% of P3); the explorer and documenter are the next largest. The orchestrator's own calls are 3% of the milestone.

### Actions
- `scio/product/build.py` (the loop), `scio/product/progress.py` (PRODUCT.md parser, `ProgressFile`, `normalise_milestone`), `scio/product/report.py`, `evals/product_scripts.py` (three scripted features), roles `milestone_planner` and `milestone_gate`, `Harness.child`, `GitHubClient.ensure_milestone` / `ensure_issue` / `find_issue` / `pr_merged`, `run_graph(..., saver=)` and `thread_started`, `scio build` / `scio status` / `scio report` for build threads, registered `product_new` and `product_build` diagrams.
- `tests/graph/test_product_build_fake.py` (ten tests: the two-milestone loop, crash between features, GitHub mode crash after the PR against a bare origin, gate override, integration ground truth, escalations across features, feature decisions, gate escalation, the CLI), `tests/unit/test_product_progress.py`, GitHub client idempotency tests, `test_diff_stat_counts_untracked_files_as_changes`, the lock-version test.
- ADR 0011; README; PRODUCT_MODE status; ARCHITECTURE product graphs section and tree; CHANGELOG; ledger I-062 to I-067, I-053 and I-054 completed; `answers` and `approval` nodes (I-065); `merge and stop`, per-milestone build threads; the working-tree reset before a fresh attempt (I-067); report thread detection by checkpoint shape (I-066).

### Carried into M9
- I-064 is closed by the live milestone above. Before M9: make the walking-skeleton criteria installable without a package install, or give the normaliser a rule for P1 (the planner wrote a console-script P1 twice in two live runs).
- `integrate` runs tests and lint only; the build, smoke against a running app, dependency audit and the coverage floor arrive with M9's runtime commands (`install_deps`, `run_app`) and the Docker default.
- A pre-flight credential probe for live product commands (one cheap call before the interview) would turn I-060/I-064 into a one-line message instead of a traceback.
- Repositories bootstrapped by 0.5.0 carry the old `progress.json` shape (P1 `done`, one milestone); `ProgressFile.sync_features` adds missing records but does not reset P1. Re-bootstrap or edit by hand until a migration is worth writing.

## Claude Code backend (2026-10-12, issue #47, ADR 0012)

### Why
After the M8 live milestone the owner said they had been billed for API usage and want only their Max subscription to pay. Anthropic's terms allow a subscription login only inside the unmodified Claude Code binary; the Max plan's included API credits are the other sanctioned route (and remain the fallback: link the Console org on claude.ai, no card). The backend makes SCIO run its model work through `claude -p`.

### Lessons applied
- Pre-flight 8: every flag the backend relies on was read from `claude --help` and the headless docs, then exercised live (a restricted `--json-schema` call on the Max login) before any code: `--bare` would have silently switched to API-key auth, and print mode refuses to nest under `CLAUDECODE=1`.
- The `GitHubClient` pattern (an injectable runner, every argv recorded) made the whole backend testable without the binary or a login: the canned runner in the graph test performs the scripted edits in the sandbox, so SCIO's real verify grades what "Claude Code" wrote.
- `scripts/check_docs.py` caught model ids written into the new tests; they now come from `models.yaml` through the role table.

### What succeeded
- Two seams were enough: a `ModelProvider` for tool-less roles and a worker with `AgentLoop`'s surface behind `make_worker`; the four node call sites changed one line each and every existing test passed unchanged on the `aci` backend.
- `stream-json` rendered into SCIO's transcript shape keeps `scio report`'s tool signals working for Claude Code turns; `Harness.account` keeps budgets, caps and the cost ledger honest with Claude Code's own usage numbers.

### What failed
- The first live trial script passed an `EvalTask` where the solver wants `to_task_like(task)`; a harness detail, no spend.
- The first `scio new` through the backend died on its first call: print mode returned `is_error` with no message, and the provider called every unexplained error fatal (I-068). The harness already had a retry policy; the provider just never raised the retryable class. Transient process failures are now retried and the subtype is carried in the message.
- The first `scio build` through the backend died in P1's documenter turn: the T0 cap of $0.50, written for API prices, was handed to `--max-budget-usd` as the remaining $0.29 and Claude Code stopped the turn; the backend turned that into a traceback instead of SCIO's `budget` exit (I-069). Two fixes: tier caps scale by a measured factor (4.0) on this backend, and the binary's budget stop maps to `BudgetExceeded`, which every node already handles. The eval solver scales task budgets the same way.
- P4's documenter wrote a 1,200-character summary against SCIO's 800-character cap, the re-ask came back long again, and the build died (I-070): the schema handed to `--json-schema` carries no `maxLength`, so the binary cannot enforce prose caps. The worker now trims over-long string fields and validates again before any re-ask; structural errors still re-ask once.
- A lesson about guards: a cap expressed in dollars is only as portable as the price list behind it. When a backend changes what a dollar measures, scale the guard with data from one trial before running anything long.

### Live trial (2026-10-12, Max login, no Console API usage)
t001 through the T0 graph with both seams on (`SCIO_PROVIDER_OVERRIDE=claude_code`, `SCIO_WORKER_BACKEND=claude_code`), run from inside a Claude Code session with `CLAUDECODE` stripped:

| role | `claude -p` runs | output tokens | cache read | cache write | API-price estimate |
|---|---:|---:|---:|---:|---:|
| router (haiku) | 1 | 152 | 0 | 10,747 | $0.001 |
| coder (sonnet) | 2 (session + summary) | 2,602 | 205,212 | 60,010 | $0.217 |
| documenter (sonnet) | 4 (two rounds) | 6,977 | 405,516 | 52,892 | $0.283 |
| gate (opus) | 1 | 354 | 0 | 11,697 | $0.066 |
| total | 8 | | | | $0.57 estimated, $0.00 billed |

Result: pass. The coder's session listed the repository with a read-only Bash call, read the module and the tests, wrote `slugify` and `tests/test_slugify.py`, ran pytest; SCIO's verify passed on the first iteration, `git_checkpoint` committed `feat:`, the documenter updated the CHANGELOG, the gate passed, and the hidden tests pass on the kept tree (9 tests). Nothing was sent to the Console organisation.

Lessons:
- The same task costs about four times its API estimate when run through Claude Code ($0.57 versus $0.11 to $0.15 for t001 on the API), because every `claude -p` process carries Claude Code's own system prompt and tool set as cache writes and reads (60k cache-write tokens on the coder's first turn). On a subscription this is usage-window consumption, not dollars; the estimate is still the right number to cap turns with.
- The documenter's two rounds are four processes; `--resume` keeps the second round's context but each round pays the session start. A single-round documenter would halve its cost on this backend.
- Print mode's read-only command set (`ls`, `cat`) runs without an `--allowedTools` entry, so the coder explores with Bash even though only the test and lint commands were allowed; `acceptEdits` plus `--permission-prompts none` never blocked the run.

### Live milestone through the backend (2026-10-12, Max login, no Console API usage)
`scio new` (interviewer and product planner through `claude -p`, $0.04 estimate; the approval applied to the draft shown, the I-065 fix holding live) bootstrapped `todo_tags` (8 features, P1 needing only `python -m`), then `scio build` ran M1 "Walking skeleton with storage, add and list" with every role through Claude Code:

| feature | tier | result | runs (`claude -p`) | estimate | what happened |
|---|---|---|---:|---:|---|
| P1 walking skeleton | T0 | pass | 7 | $0.50 | first attempt died at the documenter's `--max-budget-usd` (I-069); resumed after the budget fix, passed |
| P2 JSON storage | T1 | pass | 18 | $1.93 | plan, localize, a coder that needed four verify iterations, reviewer, documenter, gate; no escalation |
| P3 `add` with tags | T0 | pass | 7 | $0.62 | one coder turn |
| P4 `list` | T0 | pass | 9 | $1.05 | documenter summary over 800 characters crashed the build once (I-070); resumed after the trim fix, passed |
| orchestrator | | | 3 | $0.16 | planner (twice: the first attempt's call plus the resume), gate pass |
| total | | 4 of 4 | 44 | $4.26 estimated, $0.00 billed | integration green (25 tests, lint clean), milestone gate pass, merged locally with `merge and stop`; the product's own suite and lint pass on `main` |

Lessons:
- Three defects surfaced only live, each in the seam between SCIO's guards and the binary's behaviour: an unexplained transient error (I-068), a dollar cap sized for API prices (I-069), and a prose cap the binary cannot see (I-070). All three were fixed between resumes and the same build thread carried on from its checkpoint each time, which is what the crash-safety work in M8 was for.
- Cost shape on this backend: the T1 feature (P2) cost four times the T0 ones, dominated by nine coder runs; each `claude -p` process re-sends Claude Code's prompt, so iterations are the lever, as on the API, but with a higher constant. A milestone of this size is about $4 of estimate, or roughly a tenth of a Max 5x usage window by the same yardstick.
- The documenter is the most expensive role per feature after the coder (two rounds, each a session plus a summary); M9 should give it one round with the docs check folded into the turn.

## M9: Dependencies + runtime (2026-10-13 to 2026-10-26, issue #49, ADR 0013)

### Lessons applied
- The read-only inventory before any code (as in M6 to M8) listed every seam M9 touches, with lines, and became the issue body; the slices followed it in order: sandbox `delete` and the Claude Code local-only guard, the profile's quality bar, the product environment and install phase, `install_deps` and `run_app`, the `## Runtime` section and the acceptance runner, then the http Fake product.
- Pre-flight 8: the venv, pip and app commands are profile data (`runtime:`), exercised by real processes in the tests (a venv is created, a server is started and probed), not mocked.
- I-063 made the P1 scripts possible: both Fake products' first features add only new files.
- The Claude Code backend's boundary (ADR 0012) shaped the design: installs are an orchestrator step before the turn, never a worker act, so the same code path serves both backends.

### What succeeded
- The done criterion's shape runs offline: `todo_api` approves `pyyaml`, the install phase creates its environment, two features build through the task graph, `integrate` starts the server on a free port, runs the black-box acceptance tests against it, stops it, the milestone merges and the tree is clean.
- "Skipped, tool absent" as a first-class outcome keeps the quality bar honest on machines without `build` or `pip-audit` and lets a product's own CI be the place that installs them.

### What failed
- Shell-embedded patches mangled backslashes three times in one session (I-071, I-072); the rule has existed since M1. The fix is a memory rule for the assistant: edits with backslashes or quote-heavy literals go through the file tools, one site per edit.
- The first `todo_api` build stalled on a feature that was correct: two test modules shared a basename across `tests/` and `tests/acceptance/` and pytest refused to collect (I-073). The scaffold's test directories are packages now.
- The coverage check left `.coverage` in the product tree, which a later feature's `git add -A` would have committed; the profile's cache excludes carry the coverage artifacts now.
- The first push of PR #50 was red on CI and green locally (I-074): the product venv's `--system-site-packages` exposed the *base* interpreter, and only the dev machine's base Python happened to carry pytest. A test asserted that pytest imports, not where it comes from, so the accident passed. Rule: when a test depends on a tool being present, assert its origin (`__file__` under the expected path), and never rely on `--system-site-packages` under `uv run`.
- The dev machine's Application Control policy began blocking the venv's `pytest.exe` and `scio.exe` launchers (I-075); the module forms are the fixed spelling in CLAUDE.md now, and `scio/__main__.py` exists so `python -m scio` works.

### Actions
- `_module_only_skeleton` in `scio/product/spec.py` and the planner rule: a P1 criterion that needs a console script, an entry point or an install becomes the `python -m <package>` form (two live planners had asked for one).
- `scio/aci/runtime.py`, `scio/product/acceptance.py`, `Sandbox.delete` and `exec(network=)`, `ToolDef.needs_context`, `QualityCheck` and `Runtime` in the toolchain, `AppSpec` and the `## Runtime` section, the scaffold's acceptance tests and http skeleton, `evals/product_scripts_http.py`; tests `test_runtime.py`, `test_acceptance.py`, the delete and quality-bar tests, the http product loop test; ADR 0013; ledger I-071 to I-073.
- `link_host_env` and `Runtime.test_tools` (plain venv, `.pth` link on local, tools installed elsewhere), the origin assertion in `test_product_env_is_created_once_and_rewrites_python`, `test_non_local_sandbox_installs_the_test_tools_on_creation`; `scio/__main__.py` and its test; CLAUDE.md's CI-equivalent line; ledger I-074, I-075.

- Docker default (ADR 0013 decision 10): `DockerSandbox` rewritten around one persistent container on an internal network with the egress-proxy sidecar (`scio/aci/sandbox/egress_proxy.py`), `docker_product_ready`, `product_sandbox_backend` and `Settings.product_sandbox_backend`, the ledger notice in `load`, the in-sandbox branch of `run_acceptance`, `docker/sandbox.Dockerfile`, the `docker-live` CI job; tests `test_sandbox.py` (command shapes), `test_egress_proxy.py`, `test_product_sandbox_backend.py`, `tests/live/test_docker_live.py`. Written blind on a machine without Docker (I-016): the first `docker-live` run passed 7 of 9 (a host-side symlink check, I-078, and a wrong expectation about a refused CONNECT, I-079), the second passed 9 of 9 in 19 seconds. The container, the internal network, the proxy's allowlist, the in-container product environment and the in-container http app all worked on the first real run.
- The first live http milestone found two bugs before any code was written: a resume of `scio new` without `--dir` bootstrapped in a default directory (I-076), and the Claude Code worker's typed summary had one turn where Claude Code's own schema bounce needs two (I-077). Both are fixed with tests; the same build resumed from its checkpoint.

### The live http milestone (2026-10-26, Claude Code backend, nothing billed)
`scio new` interviewed and planned `bookmarks_api` (an http.server service persisting to YAML through `pyyaml`, `## Runtime` with `serve --port {port}` and `/health`, 7 features, $0.12 estimated). `scio build` ran M1 "Walking skeleton with bookmark create and list": P1 to P3 at T1, P4 at T0, every feature passed its own verify, one advisor escalation (P1 changed `pyproject.toml`, verdict continue), 63 tool calls and 23 minutes for P1, about 8 minutes for each of the others. Integration on the merged branch: lint clean, 28 tests passed, coverage passed, build and audit skipped (tools absent), `[acceptance] passed (http http://127.0.0.1:63721): 2 passed`, that is the black-box tests ran against the server SCIO started from the product environment. The gate passed, `merge and stop` merged `milestone/1` into `main` and recorded M1 in `progress.json`. Cost: $5.38 estimated over 41 Claude Code runs, $0.00 billed (Max subscription). The M9 done criterion holds on the Fake and live.
- What it found before any code was written: I-076 (resume without `--dir`) and I-077 (one turn for a schema answer). Both fixed in the same session; the build resumed from its checkpoint without a duplicated commit.
- Honest limit: `pyyaml` was "already satisfied" through the `.pth` link to SCIO's environment, so the live run did not download anything; the Docker CI test (`test_product_environment_lives_inside_the_container`) is where a real install through the allowlist proxy is exercised.

### Still open after M9
- `run_app` inside a container; a lockfile for the install phase; the Docker default has run only in CI.

## M10: Second stack (2026-10-26, issue #51, ADR 0014, PR #52)

### Lessons applied
- Inventory first (M6, pre-flight 12): a read-only sweep before any code listed every Python assumption M7 to M9 had re-introduced outside the profile (the scaffold, the normaliser's `python -m` literals, the venv runtime, the acceptance runner's directory and health probe, `run_tests` and `edit_file` never receiving the context's toolchain, the Claude Code allow-rules, the planner prompt, every Fake script); each became an edit in slices 3 to 5 and the list is the issue body.
- The graph names what, a profile says how (ADR 0009): every new need became a protocol member (`acceptance_dir`, `skeleton_command`, `command_words`, `app_target`, `syntax_check_argv`, `health_probe_argv`, `detect`) and the yaml says how; no `if stack ==` was written.
- Tests never depend on what the machine has (I-074, I-080): `tests/node_support.py` skips the Node tests where Node 22 is absent; CI's `checks` job installs it, so the tests count there.
- Measure before designing (pre-flight 8): the TAP shape, "no test files" being exit 0 with `# tests 0`, the name filter, absolute globs, `node --check` on `.ts`, `npm` being a `.cmd` shim and `process.features.typescript == "strip"` were each probed on this machine before the yaml was written.
- One live trial after the Fake (M5): the live `node-ts` milestone runs on the Claude Code backend after the Fake both-stacks build passed.

### What succeeded
- The python profile moved onto the generic loader with its tests unchanged (`test_toolchain.py` still compares the old literals), which is the definition of "no behaviour change" for the refactor.
- The TypeScript walking skeleton runs, tests, lints (eslint absent is "not the agent's problem"), serves its health path and passes black-box acceptance with no download, on the first run of the scaffold test.
- `test_same_spec_builds_in_both_stacks`: the `todo_api` spec bootstraps and builds M1 in Python and in TypeScript through the unchanged milestone loop: install phase, two features through the task graph, the started server, the black-box run, the quality bar (`[typecheck] skipped: tool absent`), merge, a clean tree, the product's own CI commands green.

### What failed
- `run_tests` grew a context parameter in the position where verify passed the toolchain, and the python default answered for the TypeScript product: pytest found no tests in a repository with none (I-082). The unit tests were green because the python build was unaffected; the both-stacks Fake build found it in one run. A default that hides a wrong call is the hazard pre-flight 12 names.
- `node --check` accepted a TypeScript parameter property that the strip-only loader refused, so the edit gate let a file through that broke every import (I-083); the gate now uses the loader's own parser (`module.stripTypeScriptTypes`).
- `subprocess.run(["npm", ...])` cannot spawn npm's `.cmd` shim on Windows (I-081); the local sandbox resolves bare commands through `shutil.which` there.
- The first CI run of the branch failed the three Node tests with "Cannot allocate Wasm memory": the local sandbox's POSIX `RLIMIT_AS` (2 GB of *virtual* address space) killed V8's type stripper, a limit the Windows dev machine never applied (I-084). Third "green here, red there" of the day after I-074 and I-080; the dev machine and the runner differ in packages, environment variables and now resource limits, and CI is the only place all three are the runner's.

### The live node-ts milestone (2026-10-26, Claude Code backend, nothing billed)
`scio new` with a TypeScript brief: the planner (prompt v2, the stack menu rendered from the registry) chose `node-ts`, `src/main.ts` as the entry, `zod` as the one approved dependency, six features with black-box criteria; the bootstrap landed in the directory passed on the first call (the I-076 fix holding live) with the scaffold's lint and `node --test` green. `scio build` ran M1 "Walking skeleton with note create and list": P1 and P2 at T1, P3 at T0, every feature passed its own verify, one advisor escalation (P1 changed `package.json`). The install phase ran `npm install` through the local sandbox on Windows (the `.cmd` resolution of I-081) and, because the scaffold declares the quality tools as devDependencies, installed eslint, typescript and the type definitions too, so the quality bar ran for real: `[lint] clean`, 13 tests passed (1 skipped: the acceptance test awaiting `APP_URL`), `[typecheck] passed`, `[build] passed`, `[audit] passed: found 0 vulnerabilities`, `[acceptance] passed (http ...)`. The gate passed and `merge and stop` merged `milestone/1`. Cost: $4.61 estimated over 36 Claude Code runs, $0.00 billed. The M10 done criterion holds on the Fake and live.
- Found: the install phase leaves `package.json` modified (`npm install --save` rewrites the dependency range) and `package-lock.json` untracked in the product tree after the merge (I-085, open): the lockfile PRODUCT_MODE component 4 asks for exists now but nothing commits it, and the next feature's `git add -A` would.

### Still open after M10
- I-085: commit the install phase's manifest and lockfile changes as their own `[scio:install]` commit on the milestone branch (or install without `--save`).
- Docker for Node products (the sandbox image is Python); `run_app` inside a container; the `test_` prefix in `escalation.py`'s drift rule.

### Actions
- `scio/toolchain/profile.py` (`ProfileToolchain`, `StackParsers`, `load_profile`), `node_ts.py`, `configs/profiles/node-ts.yaml`, the registry with `available_toolchains` and `detect_toolchain`; `Runtime` generalised; `resolve_command`; `ensure_product_env` on `env_marker`; the stack-neutral normaliser, acceptance runner, `run_app`, tool descriptions, allow-rules, planner prompt v2; `node_ts_scaffold` and the `scaffold_for` registry; `resolve_toolchain`, `adopt_toolchain` and the four call sites; `evals/product_scripts_node.py` and `ScriptedResponder(stack=)`; `LocalSandbox` without `RLIMIT_AS` (I-084); `check_readme_status` (the landing page current at every push).
- Tests: `test_toolchain_node_ts.py`, `test_scaffold_node_ts.py`, the node cases in `test_runtime.py` and `test_sandbox.py`, the normaliser test in both stacks, `test_same_spec_builds_in_both_stacks`, `tests/node_support.py`; CI `setup-node`.
- ADR 0014; PRODUCT_MODE status; README status, table and roadmap; CHANGELOG; ledger I-081 to I-085.

## M11: Product-ready + evals (2026-10-26 to 2026-10-27, issue #54, ADR 0015, PR #56)

### Lessons applied
- Inventory first (M6 to M10): a read-only sweep listed the quality-bar gaps, the loop's end, the install path and the eval runner before any code; it became the issue body and the nine slices.
- Measure before designing (pre-flight 8): npm's save and lockfile behaviour, Node 22's coverage flags and its coverage-ignore comments, the scaffold's real coverage (78.57% and 33.33% once tests stopped counting) and a headless Chromium launch were each probed before the code that depends on them.
- A node that interrupts does nothing else (I-065): `release_decision` only asks; the tag and the Release are effects in `release`.
- The failure must be visible to the worker (t008, t009): coverage and node's `typecheck` run in each feature's verify, not only in `integrate` (I-091).
- Green here, red there (I-074, I-080, I-084): the Docker image tests were written for CI's daemon and passed there first time; the browser tests run in CI's `checks` job, which installs Chromium.
- A hint is not a pin (I-050): each product eval pins its stack and interface in a fixed `PRODUCT.md`.
- One live trial before scaling (M5), adjusted by the owner on 2026-10-27: on the Claude Code backend nothing is billed, so the live evals ran without a stop between them.

### What succeeded
- The release gate caught two real defects that every per-feature check had passed: a flaky scripted server (I-096) on the Fake, and nothing on live p001, whose release was clean. Running the whole bar once more on `main` is cheap and earned its place on its first day.
- The honest coverage floor plus the verify-time check works as designed on the Fake: P2 at 93% against a floor of 95 failed its own verify (`quality`), went to the debugger, gained the error-path tests and passed in its second iteration; without the verify step the milestone halts at `integrate`.
- On the final commit all three product evals pass live, in parallel: a CLI todo app with tags and JSON export (p001, 3 features), a REST todo API (p002, 2 features) and a todo web app graded in a real browser (p003, 2 features), each from a fixed `PRODUCT.md` to a released v0.1.0 whose hidden black-box tests pass, with no intervention beyond the designed merge and release stops: $11.63 estimated in all, 21 to 31 minutes each, nothing billed.
- Writing guards before trusting green found more than the guards themselves: the offline I-085 test found I-088, the I-090 sweep found the bootstrap probe's regex bug, the I-085 sweep found the uncommitted progress sync.

### The live product evals (2026-10-27, Claude Code backend, the owner's Max login, nothing billed)
Every attempt, one trial each; dollars are Claude Code's API-price estimates. First attempts passed 1 of 3, and each failure was a defect in SCIO, not in the model's work. On the final commit all three pass.

| task | commit | result | features (tier, iterations, cost) | release | hidden | estimate | time | stops / interventions | found |
|---|---|---|---|---|---|---:|---:|---|---|
| p001 CLI todo | `6f7474a` | pass | P1 T0 2 $0.88; P2 T1 3 $1.47; P3 T1 2 $1.44 (1 advisor escalation, resolved) | v0.1.0 | 5 passed | $3.96 | 24.5 min | 3 / 0 | P1 used 353,267 tokens of a 400,000 T0 cap (I-099 waiting to happen) |
| p002 REST API | `6f7474a` | fail | P1 T0 1, `escalate_human` | none | not run | $0.80 | 6.8 min | 1 / 1 | I-097 |
| p002 REST API | `5eeac8d` | pass | P1 T0 2 $0.82; P2 T1 3 $1.56 | v0.1.0 | 5 passed | $2.43 | 18.7 min | 2 / 0 | P1's first verify refused the all-skipped proof with the new hint, the next turn passed: I-097 confirmed live |
| p003 web app | `5eeac8d` | crash | P1 passed; P2 ran out at its PR body | none | not run | not recorded | 22.9 min | 0 / 0 | I-098, I-099 |
| p003 web app | `94b8849` | pass | P1 T1 3 $1.75; P2 T1 2 $1.71 (1 escalation) | v0.1.0 | 4 passed in Chromium | $3.51 | 24.4 min | 2 / 0 | both features planned at T1, so this run did not exercise I-099 (the measured ratio and its unit test are the evidence) |
| p001 CLI todo | `94b8849` | pass | P1 T0 1 $0.56; P2 T1 3 $1.72; P3 T1 2 $1.44 | v0.1.0 | 5 passed | $3.84 | 25.6 min | 3 / 0 | none |
| p002 REST API | `94b8849` | crash | P1's PR body: Claude Code's schema check refused every answer within three turns | none | not run | not recorded | 5.6 min | 0 / 0 | I-100 |
| p001 CLI todo | `dda626f` | pass | P1 T0 $0.82; P2 T1 $1.26; P3 T1 $2.19 (one milestone) | v0.1.0 | 5 passed | $4.41 | 31.0 min | 2 / 0 | none |
| p002 REST API | `dda626f` | pass | P1 T0 $0.76; P2 T1 $1.90 | v0.1.0 | 5 passed | $2.79 | 21.0 min | 2 / 0 | none |
| p003 web app | `dda626f` | pass | P1 T1 $2.03; P2 T1 $2.27 | v0.1.0 | 4 passed in Chromium | $4.43 | 30.6 min | 2 / 0 | none |

On the final commit `dda626f` all three pass, one trial each, run in parallel: $11.63 estimated in all, nothing billed, no intervention beyond the designed merge and release stops. Across the day: 7 passes in 10 attempts; each of the other 3 (one failure, two crashes) found SCIO defects (I-097; I-098 with I-099; I-100), fixed with a guard before the next attempt.

### What failed
- Live p002's first run halted at its first feature (I-097): the coder rightly found the health endpoint already built and "proved" it with black-box tests that skip outside `integrate`; verify accepted an all-skipped run, and only the model gate's `replan` noticed. The M3 lesson covered "no tests collected" but not "all skipped". A pass that ran nothing is not a pass, in every reader of a test run.
- Release cadence slipped for two weeks (I-086) and I acted on a reported merge without checking GitHub (I-087); both are now checks or rules, not intentions.
- Three of my own mistakes reached a check before a test did: stack data in product code (I-093, caught by the M6 boundary test), a CI timeout nobody had measured (I-094, cancelled a green run), the trial spend that missed every feature's child harness (I-095, caught on the first Fake run before any live run).
- Git's auto-merge silently moved branch entries into a released changelog section (I-089); a text merge does not know which section a line belongs to.
- Live p003's first run ended in a traceback (I-098): one model call in the documenter (the PR body) and the router's call were outside any `BudgetExceeded` guard. Every other node had one; a convention nobody checks is a convention that drifts, so the AST scan now checks it.
- Behind it, a runaway guard calibrated on the wrong quantity (I-099): I-069 measured the dollar ratio of the Claude Code backend and scaled token caps by the same number, but that backend's tokens are mostly cheap cache traffic. Two live product runs gave the same ratio (about 380,000 billable tokens per estimated dollar), which is what the token factor is now set from. A guard needs its own measurement for each quantity it caps.

### Actions
- `commit_install`, `commit_progress_sync` (I-085); `_npm_dependency` and `normalise_package` for non-registry specs (I-088); `check_release_cadence` (I-086), `check_released_section_frozen` (I-089); `find_commit` in the bootstrap probe and the fixed-string scan test (I-090).
- `Settings.coverage_floor`, `QualityCheck.command` and `verify`, `RunContext.quality_values`, the `quality` failure kind; both scaffolds test in-process and mark the serve lifecycle; `test_scaffold_coverage.py`; the coverage-floor failure-path test (I-091).
- `scio/product/release_checks.py` (secret scan, release files, README run block; I-092), `release_image` in both profiles (I-093), Dockerfiles in both scaffolds, the four `docker-live` image tests; CI `checks` budget 25 minutes with `--durations` (I-094).
- The release gate (`release_check`, `release_decision`, `release`, `ReleaseRecord`, `GitHubClient.find_release` / `ensure_release`) and its Fake tests.
- `scio new --spec`; the product suite (`grader: product`, `evals/product_solver.py`, p001 to p003, `--task`), `TrialOutcome.usd_elsewhere` (I-095); Playwright and the CI browser step; `test_product_evals_fake.py`.
- `Toolchain.skipped_only`, the `no tests ran` result, verify's refusal and hint, the acceptance and grader checks (I-097); `test_skipped_only.py`.
- The PR-body and router budget guards and `tests/unit/test_budget_guards.py` (I-098); `Settings.claude_code_token_factor`, `token_factor()`, `BudgetsConfig.scaled(tokens=)` and the cap-ratio assertion (I-099).
- ADR 0015; CONTRIBUTING steps 5 and 6; ledger I-085 to I-099; EVAL_HISTORY rows for the live product runs.

## Post-M11 follow-ups (2026-10-27, issue #58, ADR 0016, PR #59)

### Lessons applied
- Inventory first (M6 to M11): a read-only sweep of the open items in ADRs 0013 to 0015 and the `--gh` path became the issue body and its six slices, and found I-102 and I-103 before any code.
- The Fake only reproduces the shapes you thought of (I-031 to I-035): the GitHub-mode test had faked every merge as a fast-forward, so the I-102 test makes real squash merges on a bare origin (a new commit with the branch's tree) and failed against the old code before the fix.
- A node that interrupts does nothing else (I-065): `release_merge_wait` is its own node between `release` and `release_publish`.
- Green here, red there (I-074, I-080, I-084): the Docker and Node paths were written for CI's daemon and proven there, not on the dev machine.
- Measure before designing (M11): the parallel test run was timed on CI's runner (4 cores) before choosing the worker count.

### What succeeded
- Parallel CI tests: 789 s serial to 314 s on GitHub's 4-core runners, identical counts; the `checks` job has room again inside its 25-minute budget.
- The blind-written docker-live Node test (a real `npm install` through the allowlist proxy, `node --test` on TypeScript) passed on its first CI run, as did the image change behind it (I-101).
- `scio upgrade` on a real M9 product: `bookmarks_api` gained its Dockerfile, `.dockerignore`, README run block, coverage source and CI step in one commit, and its next `scio build` built M2 live (P5 to P7 passed, every integration check and acceptance green, $4.56 estimated, nothing billed); the upgraded README run block and release files passed the bar they were added for.
- `run_app` and the acceptance run now share one app-start module; the move also made a crashing app end the health wait early instead of waiting out the timeout.

### What failed
- Two `checks` runs failed (on `0554bf6` and `be9bb5a`) and I pushed two more slices before noticing: my CI watcher waited on the newest run only, and I did not look at the older ones (I-104). The cause was green-here-red-there once more: the dev machine's global git identity hid two commits that named none (`scio upgrade`'s and a test helper's), and the same gap existed in `scio build` on a product clone.
- I assumed the docker-live tests ran only in the `docker-live` job; both jobs have a Docker daemon, so the new Node test failed in `checks` on the plain Python image (I-105).

### Actions
- `sync_main`, `SyncRefused`, `BOOKKEEPING_MARKERS` and the two squash-merge tests (I-102); the `release` / `release_merge_wait` / `release_publish` split, `ReleaseRecord.pr_number` and `pr_url` (I-103).
- `scio/product/upgrade.py` and `scio upgrade`; `tests/unit/test_upgrade.py`.
- Node 22 in `docker/sandbox.Dockerfile`, `sandbox_probe` in both profiles, `Toolchain.sandbox_probe_argv`, the stack-aware `product_sandbox_backend`, `tests/unit/test_product_sandbox_backend.py` and the docker-live Node test (I-101).
- `scio/aci/apps.py`; `run_app` on any sandbox; `test_run_app_on_a_non_local_sandbox_*` and the docker-live `run_app` test.
- `SCIO_GIT_IDENTITY` on every commit, merge and tag SCIO makes; the autouse `_ci_git_identity` fixture in `tests/conftest.py`, shown to fail the old `upgrade.py` locally (I-104).
- The env-conditional skip of the Node docker-live test (I-105).
- CONTRIBUTING, "Tests and the environment": the CI-like git rule, check the previous push's CI before the next push, and the Docker-in-both-jobs rule (I-104, I-105).
- pytest-xdist in the dev group and `pytest -n` in CI (I-094 follow-up); ADR 0016; ledger I-101 to I-105.
