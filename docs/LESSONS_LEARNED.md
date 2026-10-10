# Lessons learned

One section per milestone, written when the milestone closes. **Read this whole file before starting a milestone** and say which lessons shape the plan. Every "action" below is something that exists (a file, a test, a rule, a ledger id), not an intention. Problems referenced as `I-NNN` are in [ISSUE_LEDGER.md](ISSUE_LEDGER.md).

## Standing pre-flight list (apply at the start of every milestone)

1. Read this file and the open entries in the issue ledger.
2. Check the API reference before any provider code; the spec was written against an older API surface in three places (see M0).
3. Confirm the `gh` token scopes cover what the milestone will push (`workflow` for CI files) before creating the branch (I-007).
4. Run the full local CI-equivalent (command in `CLAUDE.md`) before every push, not just the tests. A check that passes on Windows can still fail on Linux (I-011): any check touching paths needs a unit test with explicit inputs, not only a run against the repo.
5. Write files with LF endings; pass `newline="\n"` from Python on Windows (I-006).
6. One heredoc per shell command when the body contains quotes, and never pass backslash escapes (`\n`, `\t`) through a shell-embedded script; use the file tools for those edits (I-001, I-010).
7. Add dependencies to `pyproject.toml` and run `uv sync`; never `uv pip install` ad hoc. Export `UV_SYSTEM_CERTS=1` once per session (I-012).
8. Introspect an installed SDK (`inspect.signature`, `dir`) before writing an adapter against it; write the adapter's tests with an injected client or runner so they run without the service (M2).

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

### Actions
- `pyproject.toml` owns every dependency; `UV_SYSTEM_CERTS=1` is in CLAUDE.md as the environment rule (I-012).
- `docs/adr/0003-idempotent-effects-and-sandbox-tiers.md` records the durability and sandbox decisions.
- `tests/durability/test_crash_resume.py` is the executable acceptance criterion for crash safety and runs in CI.
- Ledger I-012 to I-016.

### Carried into M3
- I-016 open: run the gated Docker and E2B live tests when a suitable machine is available.
- Register the `t0` graph in `scio.graph.registry` and replace the hand-drawn T0 diagram in ARCHITECTURE.md with a generated section.
- Open question 3 (patch sampling N) is deferred to M4 where sampling is built.
- Every side-effecting node must name an `intent` and a `probe`; make that the node template shape from the first M3 node.
