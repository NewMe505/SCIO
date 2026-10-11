# Issue ledger

Running log of every problem hit while building SCIO, with how and when it was resolved. Add an entry when the problem is found; complete it when it is resolved. Each resolved entry records the **sweep**: the search made for the same mistake elsewhere and what it found. Statuses: `open`, `resolved`, `wontfix` (with reason).

Template:

```
### I-NNN · <short title>
- Found: <date> · <milestone> · <where: file, command, step>
- Symptom: <what was observed, exact error text if short>
- Root cause: <the actual mistake, not the symptom>
- Fix: <what changed, commit or PR>
- Sweep: <pattern searched, scope, instances found and fixed, guard added>
- Resolved: <date> · Status: open | resolved | wontfix
```

---

### I-001 · Multi-file bash heredoc batch aborted
- Found: 2026-10-10 · M1 · writing 12 prompt templates in one Bash command with chained `cat > f <<'EOF'` blocks
- Symptom: `unexpected EOF while looking for matching `''`; no files written
- Root cause: a single shell command carrying twelve quoted heredocs was mangled by the command wrapper before bash parsed it; the apostrophes in the template prose ended up outside a heredoc
- Fix: templates written one file per tool call (`Write`), content unchanged
- Sweep: all other heredoc uses in the session were single-file and succeeded; rule added to `docs/LESSONS_LEARNED.md` (one heredoc per command when the body contains quotes)
- Resolved: 2026-10-10 · Status: resolved

### I-002 · `uv sync` fails with `invalid peer certificate: UnknownIssuer`
- Found: 2026-10-10 · M1 · first `uv sync --all-extras --group dev`
- Symptom: TLS error fetching `pypi.org` metadata
- Root cause: the machine trusts PyPI through the system certificate store (proxy or corporate CA); uv's bundled store does not include it
- Fix: `uv sync --native-tls`; documented in README usage and CLAUDE.md
- Sweep: CI runs on GitHub-hosted runners and is unaffected; no other network tooling in M1
- Resolved: 2026-10-10 · Status: resolved

### I-003 · OpenTelemetry tracer provider can be set only once per process
- Found: 2026-10-10 · M1 · `tests/unit/test_harness_core.py` span tests, 2 failures
- Symptom: `Overriding of current TracerProvider is not allowed`; second test's exporter saw no spans
- Root cause: `configure_tracing()` built a fresh `TracerProvider` and called `set_tracer_provider` on every call; OTel ignores all but the first
- Fix: `configure_tracing` creates the global provider once and attaches each exporter to it; tests use one session-scoped in-memory exporter cleared per test (`tests/conftest.py`)
- Sweep: searched `scio/` for other process-global registrations set per call (`set_tracer_provider`, `logging.basicConfig`, `set_meter_provider`): only this one. `Settings` is built per call by design and is not global state
- Resolved: 2026-10-10 · Status: resolved

### I-004 · mypy `no-any-return` from integer power and untyped walker
- Found: 2026-10-10 · M1 · `uv run mypy`
- Symptom: `Returning Any` in `scio/harness/retry.py` and `scio/prompt/wire_schema.py`
- Root cause: `2**attempt` with an `int` exponent is typed `Any` by mypy; the recursive `_walk` returns `Any` by design and was returned unchecked
- Fix: `2.0**attempt`; `assert isinstance(result, dict)` at the single public boundary
- Sweep: `grep -rn "\*\*" scio/` finds no other integer-power expressions used in typed returns; mypy `strict` stays on so recurrences fail CI
- Resolved: 2026-10-10 · Status: resolved

### I-005 · `ruff format` rewrites Python code blocks inside Markdown
- Found: 2026-10-10 · M1 · `docs/ARCHITECTURE.md` and `README.md` changed after `ruff format .`
- Symptom: aligned comments and wrapped lists in the M0 State code block were reflowed; README snippet flagged as unformatted
- Root cause: ruff formats fenced Python blocks in Markdown files it is pointed at
- Fix: accepted (consistent formatting, no semantic change); `ruff format --check .` is part of the local pre-push command so docs stay formatted
- Sweep: all `.md` files run through `ruff format` in CI; no other code-bearing docs exist yet
- Resolved: 2026-10-10 · Status: resolved

### I-006 · CRLF line endings introduced by Python-written patches on Windows
- Found: 2026-10-10 · M1 · git warnings at commit; `ruff format --check` flagged README
- Symptom: 6 tracked files (`CHANGELOG.md`, `README.md`, `docs/ARCHITECTURE.md`, `scio/harness/core.py`, `scio/prompt/repair.py`, `scripts/check_docs.py`) had CRLF
- Root cause: `Path.write_text()` in text mode on Windows translates `\n` to `\r\n` unless `newline="\n"` is passed; exactly the files patched that way were affected
- Fix: files normalized to LF; `evals/report.py` writes now pass `newline="\n"`
- Sweep: `grep -rn "write_text(" scio evals scripts tests | grep -v newline=` found `evals/report.py` (fixed) and two test fixtures writing single-line files (harmless); rule added to CONTRIBUTING "File hygiene" and CLAUDE.md; `.gitattributes` already enforces LF in the repo
- Resolved: 2026-10-10 · Status: resolved

### I-007 · Push rejected: OAuth token lacks `workflow` scope
- Found: 2026-10-10 · M1 · `git push -u origin feat/1-harness`
- Symptom: `refusing to allow an OAuth App to create or update workflow .github/workflows/ci.yml without workflow scope`
- Root cause: the `gh` login token has scopes `gist, read:org, repo`; GitHub requires `workflow` to push workflow files
- Fix: run `gh auth refresh -h github.com -s workflow` (interactive, needs the browser), then push again
- Sweep: any future push adding files under `.github/workflows/` needs the scope; noted in LESSONS_LEARNED pre-flight list. The device-flow refresh must be run by the owner in their own prompt: started from an agent session it times out before the browser step completes
- Resolved: 2026-10-10 (owner ran `gh auth refresh -s workflow`; branch pushed, PR #2 opened) · Status: resolved

### I-008 · pdoc cannot resolve PEP 695 type parameters under `from __future__ import annotations`
- Found: 2026-10-10 · M1 · `python scripts/gen_api_docs.py`
- Symptom: `Error parsing type annotation StructuredResult[T] ... name 'T' is not defined` (warnings; docs still built)
- Root cause: ruff's `UP047` rewrote generics to `def f[T: BaseModel](...)`; pdoc evaluates the stringified annotations and cannot see the scoped type parameter
- Fix: `scio/prompt/repair.py` and `scio/loop/agent_loop.py` no longer import `annotations` from `__future__`; their generics evaluate at definition time and pdoc resolves them. `python scripts/gen_api_docs.py` prints zero warnings
- Sweep: four modules mix PEP 695 generics with the future import (`repair.py`, `agent_loop.py`, `graph/effects.py`, `harness/retry.py`); only the first two produced warnings because only they expose the type parameter in a public signature. Rule: a module that defines a PEP 695 generic in a public signature must not use the future import
- Resolved: 2026-10-10 · Status: resolved

### I-010 · Backslash escapes unescaped when Python patch scripts are passed through the shell tool
- Found: 2026-10-10 · M1 · patching `evals/report.py` and `CONTRIBUTING.md` from a Python heredoc; found by the file-change notice and `grep -n 'newline='`
- Symptom: `newline="\n"` landed in both files as a quote, a real line break, and a quote; `report.py` no longer parsed
- Root cause: the shell tool wrapper processes `\\n` inside the command text before bash sees the quoted heredoc, so Python received a literal newline instead of the two characters backslash-n. Same class as I-001 (command text transformed before the shell parses it)
- Fix: both files repaired with the file-edit tool; the patch script avoided writing any escape sequence through the shell
- Sweep: `grep -rn -A1 'newline=' CONTRIBUTING.md evals/report.py` plus a tree-wide search for lines ending in a dangling quote found exactly these two sites; rule added to LESSONS_LEARNED pre-flight item 6: edit files containing backslash escapes with the file tools, never via a shell-embedded script
- Resolved: 2026-10-10 · Status: resolved

### I-011 · Docs link checker reads inline code as links; passes on Windows, fails on Linux
- Found: 2026-10-10 · M1 · first CI run on PR #2, "Docs checks" step
- Symptom: `docs/ISSUE_LEDGER.md: broken link -> ...`; the same command reported 0 problems locally
- Root cause: two defects. (1) `LINK_RE` was applied to raw Markdown, so the inline code `` `def f[T: BaseModel](...)` `` in entry I-008 matched as a link with target `...`. (2) The existence test `(md.parent / target).exists()` is platform-dependent: Windows resolves `...` to a parent directory, Linux does not, so the local run hid the bug
- Fix: `scripts/check_docs.py` strips fenced code blocks and inline code spans before scanning, and rejects targets made only of dots; link scanning moved to `links_in(path)` so it is unit-testable
- Sweep: every regex in `check_docs.py` reviewed for the same "scan raw text" mistake: `check_model_ids` scans code lines on purpose (model IDs in comments or strings are also leaks). Searched `scio/`, `evals/`, `scripts/` for other `Path.exists()` checks on user-supplied strings: none. Guard: `tests/unit/test_check_docs.py` covers code spans, fenced blocks and the `...` target, and runs the three checks against the real repo
- Resolved: 2026-10-10 · Status: resolved

### I-012 · `uv pip install` outside `pyproject.toml` triggered a rebuild that hit the TLS error again
- Found: 2026-10-10 · M2 · installing LangGraph ad hoc to introspect its API
- Symptom: `Failed to fetch https://pypi.org/simple/hatchling/ ... invalid peer certificate: UnknownIssuer` on the next `uv run`
- Root cause: two mistakes. Dependencies were added with `uv pip install` instead of `pyproject.toml`, so the next `uv run` re-resolved the project; and the TLS flag was passed per command instead of being a session default (I-002 was fixed for one command, not systemically)
- Fix: dependencies added to `pyproject.toml` (`langgraph`, `langgraph-checkpoint-sqlite`; `postgres` and `e2b` extras) and `UV_SYSTEM_CERTS=1` exported for every `uv` call (`UV_NATIVE_TLS` is deprecated)
- Sweep: CLAUDE.md and README now name the environment variable; no other ad-hoc installs in the session
- Resolved: 2026-10-10 · Status: resolved

### I-013 · Fake `gh` runner parsed nested branch names wrongly
- Found: 2026-10-10 · M2 · `tests/unit/test_github_client.py::test_ensure_branch_is_idempotent_and_guards_default`
- Symptom: second `ensure_branch("agent/3-x")` created the branch again
- Root cause: the test double took the last path segment of `repos/o/r/git/ref/heads/agent/3-x` (`3-x`) as the branch name; the client itself was correct
- Fix: the double splits on `/git/ref/heads/`
- Sweep: searched the client and tests for other `rsplit("/", 1)` on ref paths: none. Branch names with slashes are the project convention (`agent/<issue>-<slug>`), so every ref-handling path must assume them
- Resolved: 2026-10-10 · Status: resolved

### I-014 · Error signature ignored the evaluated assertion in pytest output
- Found: 2026-10-10 · M2 · `tests/unit/test_state.py::test_error_signature_pytest_style`
- Symptom: two pytest failures that differ only in the evaluated `E   assert 2 == 3` line hashed to the same signature
- Root cause: the first matching `assert` line was the *source* line (`assert len(...) == 3`), identical in both failures; the evaluated `E   assert` line came later and was ignored
- Fix: pytest's `E` explanation line takes precedence over the source assertion when both are present
- Sweep: reviewed every regex in `signatures.py` for the same "first match wins" assumption; the frame regex intentionally keeps the *last* frame and is documented as such. Test covers both traceback and pytest shapes
- Resolved: 2026-10-10 · Status: resolved

### I-015 · mypy on Windows rejects the POSIX-only `resource` module
- Found: 2026-10-10 · M2 · `uv run mypy` on `scio/aci/sandbox/local.py`
- Symptom: `Module has no attribute "setrlimit"`; a `**kwargs` dict for `preexec_fn` also broke the `subprocess.run` overloads
- Root cause: platform-conditional stdlib modules are typed per platform; building keyword arguments dynamically loses the overload match
- Fix: `importlib.import_module("resource")` typed as `Any` inside the POSIX-only function; `preexec_fn=... if sys.platform != "win32" else None` passed directly
- Sweep: no other platform-conditional imports in `scio/`
- Resolved: 2026-10-10 · Status: resolved

### I-016 · Docker and E2B backends not exercised live
- Found: 2026-10-10 · M2 · `docker --version` → command not found on the dev machine; no E2B key
- Symptom: `DockerSandbox` and `E2BSandbox` are verified only through injected runners and fake clients
- Root cause: environment
- Fix: `tests/live/test_docker_live.py` runs the real `DockerSandbox` (exec, files through the mount, network off, timeout kill, non-zero exit) wherever a Docker daemon is reachable and skips elsewhere. GitHub's ubuntu runners have Docker, so the project's own CI exercises it on every PR from this change on. The E2B half is split out as I-029
- Sweep: the Docker live test also checks that a timed-out container is actually gone (`docker ps` empty), which the unit test could only assert by command construction
- Resolved: 2026-10-10 (Docker) · Status: resolved

### I-030 · `import scio.prompt` alone was a circular import since M1
- Found: 2026-10-10 · I-008 fix · the post-patch import guard from pre-flight item 6 (`python -c "import scio.prompt.repair ..."`)
- Symptom: `ImportError: cannot import name 'StructuredResult' from partially initialized module 'scio.prompt.repair'`
- Root cause: `scio.harness.core` imported `scio.prompt.repair` at module level, and `repair` imports `scio.harness.failures`, which runs `scio/harness/__init__.py`, which imports `core`. Entering through the harness first (what every test and the CLI do) happens to work; entering through `scio.prompt` does not. Present since M1 and never exercised
- Fix: `core` imports `structured_call` inside `Harness.structured` and keeps `StructuredResult` under `TYPE_CHECKING`
- Sweep: `tests/unit/test_imports.py` imports sixteen modules one at a time in a fresh `python -I` subprocess, so any future cycle fails CI regardless of test import order
- Resolved: 2026-10-10 · Status: resolved

### I-031 · verify turned pytest node ids into a `-k` expression; live runs failed with the work done
- Found: 2026-10-11 · first live Anthropic regression run (4 of 5 tasks failed; `evals/results/regression-anthropic-20261011T013459Z.md`)
- Symptom: `no tests collected for filter '(tests/test_slugify.py::test_slugify_lowercases) or (...)'` while `[run_tests all] all tests passed: 5 passed`; the coder retried four times against a complaint it could not fix; $0.18 per task spent on nothing
- Root cause: `WorkerOutput.tests_added` was documented as "pytest -k expressions (or test file paths)" but verify joined every entry into one `-k` expression. Real models report node ids (`path::name`) or file paths; `-k` matches substrings of test *names* and a path or `::` never matches. The Fake script reports a bare keyword, so no test covered the path form
- Fix: verify normalises every selector: a `path::name` or `*.py` entry runs as a positional pytest target, a bare word runs as `-k`; each selector is run on its own. The Fake t001 script now reports one node id and one keyword so both branches are exercised offline
- Sweep: the same `test_filter` string from the planner (T1/T2) flows through the same helper; `RunTestsInput` already accepted `path` so no tool change was needed. Searched for other places that build `-k` expressions: only verify did
- Resolved: 2026-10-11 · Status: resolved

### I-032 · A tool crash ended the run: `view_file` on a directory raised out of the toolbox
- Found: 2026-10-11 · live run of t005 (T2); `PermissionError: [Errno 13] Permission denied: '...\work'` during `localize`
- Symptom: the whole graph run failed with a Python exception after one explorer call; $0.06 spent, nothing recorded in the ledger
- Root cause: two defects. `view_file` only handled `UnicodeDecodeError`, so reading a directory (a `PermissionError` on Windows, `IsADirectoryError` on Linux) escaped; and `ToolBox.call` only converted `ToolError` into an error result, so any other exception from any tool propagated through the loop and the node and killed the run
- Fix: `view_file` reports "not a readable file ... use repo_search"; `ToolBox.call` turns every exception into an error tool result (logged), so the model sees the failure and the run continues
- Sweep: the other four tools raise `ToolError` for their known failure modes; the generic guard now covers the unknown ones in all of them. `tests/unit/test_tool_crash_guard.py` covers both the directory case and an arbitrary exception
- Resolved: 2026-10-11 · Status: resolved

### I-033 · Budget pre-check too conservative for small T1 budgets; explorers too expensive
- Found: 2026-10-11 · live run of t003 (routed T1, 80k-token budget): `exit=budget` with zero tool calls after plan + localize
- Symptom: 64k of 80k tokens spent before the first coder call; the pre-check then refused the coder (16k `max_tokens` reserved in full)
- Root cause: `reserve_output_fraction: 1.0` assumes every call uses its whole `max_tokens`; live output is a small fraction of that. The localize explorer also resends its transcript on each of up to 10 model calls
- Fix: `reserve_output_fraction: 0.5`; explorer loops capped at 6 model calls and 8 tool calls; the two smallest task budgets raised to the T1 level (t001 150k/$1.5, t003 160k/$1.6)
- Sweep: every loop's `max_model_calls` reviewed: coder/debugger 16 (unchanged, they do the work), documenter 12, explorer 6. ADR 0002's consequence about early refusals is now measured, not predicted
- Resolved: 2026-10-11 · Status: resolved

### I-034 · Bug-fix tasks: the coder reported no tests, verify demanded "tests added", two iterations wasted
- Found: 2026-10-11 · live re-run of t002 with the working directory kept: iteration 1 fixed the bug and ran the suite green, `tests_added: []`; verify answered "no feature tests reported; add tests"; the coder guessed the right node ids only on iteration 3
- Symptom: three iterations and 20 tool calls for a one-line fix; under the earlier budget this was a `budget` exit with the work done
- Root cause: the field was named and described as tests *added*. For a bug fix the proving tests already exist, so an honest model reports none; the verify message then asked for new tests instead of for the names of the existing ones
- Fix: the field keeps its name but its schema description, the coder and step projections, and the verify complaint all say the same thing: list the tests that prove the features, existing or new, as node ids or `-k` keywords; verification runs exactly those
- Sweep: every place that explains `tests_added` (`WorkerOutput`, `coder_message`, `step_message`, verify's message) now carries the same wording; the Fake script already reports node ids for t001 (I-031) and keywords elsewhere
- Resolved: 2026-10-11 · Status: resolved

### I-035 · Task budgets sized for the hinted tier, not the routed tier
- Found: 2026-10-11 · live t003: the router chose T1 (hint T0); plan + localize + a multi-step coder loop spent 147k tokens with every check green, then `exit=budget` at the task cap
- Symptom: a correct run halted mid-plan; per-role ledger: coder 17 calls / 114k input tokens, explorer 5 calls / 18k, planner 1 call
- Root cause: `evals/tasks/*/task.yaml` budgets were set from the tier hint (T0 ≈ 100k) while the real router may escalate; a T1 run needs the T1 tier budget (300k)
- Fix: t003's budget set to the T1 tier; t001's raised to 150k for headroom. The budgets stay per-trial caps; the tier budgets in `configs/budgets.yaml` are unchanged
- Sweep: every task's budget reviewed against the tier the live router actually chose (t001 T0, t002 T0, t003 T1, t004 T0, t005 T1); all now at or above that tier's budget. `evals/runner --parallel N` added so a 5-trial suite takes one trial's wall-clock
- Resolved: 2026-10-11 · Status: resolved

### I-036 · Third recurrence of I-010, in Markdown this time: a Windows path lost `\a`
- Found: 2026-10-11 · `grep -n Programs CLAUDE.md` after a shell-embedded docs patch showed `%LOCALAPPDATA%\Programsnt`
- Symptom: the backslash-a in `\ant` became a bell character; Python also printed `SyntaxWarning: invalid escape sequence '\P'`
- Root cause: as I-010 and I-027: the shell tool processes backslash escapes before the heredoc reaches Python. The I-027 guard (lint or import after a patch) only covers Python modules; Markdown has no such check
- Fix: line repaired with the file-edit tool
- Sweep: `grep -rn` for the other Windows paths written this session (`%LOCALAPPDATA%`, `AppData`) in docs: only this one. Rule hardened in pre-flight item 6: any text containing a backslash (Windows paths, regexes, escape sequences) is written with the file tools, never through a shell-embedded script, and the `SyntaxWarning: invalid escape sequence` line in a patch's output is treated as a failure
- Resolved: 2026-10-11 · Status: resolved

### I-037 · T1 verify preferred the planner's guessed test names over the tests that exist
- Found: 2026-10-11 · live t005 (routed T1): `exit=stall` after 4 iterations and one unresolved advisor call, while the hidden tests passed on the final tree
- Symptom: a correct implementation rejected four times; the escalation machinery engaged for a harness defect
- Root cause: the planner fills `FeatureItem.test_filter` before any test exists (a guess at names); verify ran that guess, got "no tests collected", and ignored the selectors the worker reported in `tests_added` because a plan-provided filter took precedence
- Fix: when a plan filter matches nothing and the worker reported selectors that pass, verify adopts the worker's selectors as the feature's `test_filter` and records both runs in the artifact
- Sweep: the only other producer of `test_filter` is the T0 fallback, which already comes from the worker. The planner prompt still asks for filters (useful as intent), but they are no longer load-bearing
- Resolved: 2026-10-11 · Status: resolved (pending live confirmation)

### I-038 · T1 runs resend a growing transcript on every model call; a docs task cost 283k tokens
- Found: 2026-10-11 · live t003 (routed T1): `exit=budget` at 283k tokens after 2 iterations and 28 tool calls
- Symptom: coder calls of 4k → 9.5k input tokens each, 12 to 17 calls per turn; the transcript trim only kicked in above 60k characters
- Root cause: the worker loop allowed 16 model calls per turn and trimmed old tool results only beyond 60k characters, so a chatty turn resent most of its history a dozen times
- Fix: `max_model_calls` 16 → 12 for worker loops; transcript trimming starts at 30k characters (the last three tool results stay intact)
- Sweep: explorer loops were already capped at 6 (I-033); documenter at 12. The per-role ledger in the run report makes this visible per run
- Resolved: 2026-10-11 · Status: resolved (pending live confirmation)

### I-039 · The planner emitted an "explore only, no code changes" step; verify can never pass it
- Found: 2026-10-11 · live t005 re-run with the directory kept: plan v1 step S1 "Explore only, no code changes. Locate tool/cli.py ..."; the coder obeyed, changed nothing, and verify answered "no changes since the base commit" four times until the stall exit ($0.49, one unresolved advisor call)
- Symptom: a correct model following a plan that cannot be verified
- Root cause: the planner template did not forbid exploration steps, and the `plan` node accepted steps with no feature ids or no verifiable change. Exploration is the explorers' job (scope/localize already ran); verify's first rule (the tree must change) makes a no-op step unpassable by design
- Fix: planner template v2 (no exploration steps; every step changes code or tests and is provable by tests; each feature in exactly one step). `normalize_steps` in the `plan` node drops steps that are featureless or described as explore/investigate/no-change, appends a final step for any uncovered feature, and records what it did in the ledger
- Sweep: the advisor's `revised_steps` go through the same normalisation. Verify's "no changes" message now says a step must change code or tests. `tests/unit/test_plan_normalization.py` covers the drop, the cover, the untouched and the empty cases
- Resolved: 2026-10-11 · Status: resolved (pending live confirmation)

### I-040 · The conversation was never cached: every worker call re-billed its whole transcript
- Found: 2026-10-11 · per-role ledger of the live t005 run: coder 6 calls, 21,485 fresh input tokens, 5,865 cache-read, 0 cache writes on the conversation; t003 spent 202k of 283k tokens in 31 coder calls the same way
- Symptom: worker turns cost roughly the square of their length (`docs/IDEAS_TRIAGE.md` item 2)
- Root cause: the Anthropic adapter placed cache breakpoints on the system prompt and the tool list only. The append-only transcript is the ideal cache prefix and had no breakpoint, so each call re-read it at full price
- Fix: a third breakpoint on the last content block of the last message (`CachePolicy.messages`, default on); the previous transcript is then a cache read at 0.1× (0.05× on Opus 5.5)
- Sweep: four breakpoints are allowed per request; SCIO now uses three. The OpenAI adapter relies on the provider's automatic prefix caching and needs no change. Measured on a live t003 run in the same PR
- Resolved: 2026-10-11 · Status: resolved

### I-041 · The token budget counted cache reads at full weight, so caching could not help it
- Found: 2026-10-11 · live t003 right after I-040: coder fresh input 64 tokens, 184k cache-read, 52k cache-write; `exit=budget` at 284k "tokens" with the work done and $0.46 spent against a $3 cap
- Symptom: caching cut the bill by about a quarter on a chatty run but the token guard still saw the raw sum and halted the run
- Root cause: `BudgetGuard.record` added `Usage.total` (every token kind at weight 1). The spec's token budget is a cost proxy; after I-040 most tokens are cache reads billed at 0.1×
- Fix: `Usage.billable` (fresh + output + 0.1 × cache reads + 1.25 × cache writes) is what the guard, the state budget and the eval `tokens` column count. The dollar cap is unchanged and remains the hard limit
- Sweep: `evals/report.py` and `scio report` read the guard's snapshot, so they follow. `Usage.total` stays for raw accounting in the ledger and spans. `tests/unit/test_billable_tokens.py`
- Resolved: 2026-10-11 · Status: resolved

### I-042 · Scenario tasks ran live, where their failure modes do not exist
- Found: 2026-10-11 · first nightly live run: capability pass@1 0.67 because c003 ("advisor cap, expected not to pass") was solved by the real model three times out of three
- Symptom: a capability number that measures nothing: the `scenario` switches configure the Fake responder only, so on Anthropic c001–c003 are just the underlying tasks, and c003's `expect_pass: false` turns every success into a failure
- Root cause: tasks carried no statement of which providers they are meaningful on
- Fix: `providers: [fake]` on the three scenario tasks; the runner skips tasks whose list excludes the current provider and the report lists them as "not applicable here"
- Sweep: every other task has an empty list (meaningful everywhere). Future live-only scenarios (e.g. a real failing CI) will carry `providers: [anthropic]`
- Resolved: 2026-10-11 · Status: resolved

### I-043 · Live t003 trial ended `escalate_human` with every check green
- Found: 2026-10-11 · first nightly live run: regression t003 trial 1 of 3, `exit=escalate_human`, 3 iterations, hidden tests passing on the final tree (the other two trials passed; pass^3 0.80)
- Symptom: a correct run stopped for a human instead of merging; the per-run artifacts were not kept
- Root cause: unknown; candidates are the gate returning `replan`/`escalate_human` on a passing tree, or a worker setting `needs_design_decision`. The run report (`.scio/reports`) would show which
- Fix: pending. Diagnosis attempt 2026-10-11 15:30: three t003 trials with state capture (scratch script wrapping `run_graph`/`resume_graph`) all died in `route` on I-044 before any model call ($0 spent); the profile must be re-logged (`ant auth login`) before the next attempt. Candidate noted while reading the gate: `gate` maps a `replan` verdict to `ExitReason.ESCALATE_HUMAN` as well (`checkpoint_gate.py`), so a gate that merely asks for another pass also halts for a human
- Sweep: n/a until diagnosed
- Resolved: — · Status: open (blocked on a fresh profile)

### I-044 · Parallel live trials raced on OAuth refresh-token rotation; the profile's refresh token died
- Found: 2026-10-11 · I-043 diagnosis run, `evals.runner` with `--parallel 3` on the Anthropic profile, access token expired 13h earlier
- Symptom: all three trials: `WorkloadIdentityError: user_oauth refresh failed (HTTP 400): {'error': 'invalid_grant', 'error_description': 'Refresh token not found or invalid'}`; afterwards `ant auth status` still shows the profile but every refresh fails
- Root cause: `AnthropicProvider` built one `anthropic.Anthropic()` per instance and the registry builds one per `Harness`, so `--parallel N` made N clients. The SDK's `CredentialsFile` runs the `refresh_token` grant and rotates the token in the file, serialised only inside one client's `TokenCache`. N clients read the same refresh token and refreshed at once; the token endpoint accepts one grant and rejects the rest, and the rotated token was not the one the losers held. The first nightly run worked only because the access token was still fresh
- Fix: `scio/harness/providers/anthropic.py` `shared_client(timeout_s)`: one process-wide client per timeout behind a lock; injected clients unchanged. `tests/unit/test_anthropic_shared_client.py` (same timeout shares, different timeout does not, injected is not shared, 32 concurrent constructions build one). The profile itself needs `ant auth login` again
- Sweep: `grep -rn "Anthropic(\|OpenAI(" scio evals scripts tests`: the only other SDK client is `OpenAIProvider` (static key, no rotation, not affected). No other code refreshes credentials
- Resolved: 2026-10-11 · Status: resolved (code); profile re-login pending

### I-045 · The 03:17 scheduled nightly task was refused by the Task Scheduler
- Found: 2026-10-11 · checking why `.scio/nightly.log` did not exist the morning after PR #17
- Symptom: `Get-ScheduledTaskInfo`: LastRunTime 11:38, LastTaskResult 2147946720 (0x800710E0, "the operator or administrator has refused the request"); no log, no results directory, no history rows
- Root cause: the task was registered with `schtasks`, which cannot set conditions: `StartWhenAvailable`, `AllowStartIfOnBatteries`, `DontStopIfGoingOnBatteries`, `WakeToRun` were all off, so a laptop asleep or on battery at 03:17 skips the run and refuses the catch-up
- Fix: `Set-ScheduledTask` with those four settings and a 6 h execution limit; the registration recipe in `scripts/nightly_live.py` is now the PowerShell form that sets them
- Sweep: only one scheduled task exists for this project. `.github/workflows/nightly-evals.yml` is unaffected (GitHub cron). Guard: the history table in `docs/EVAL_HISTORY.md` is the signal; a missing row for a date means the task did not run
- Resolved: 2026-10-11 · Status: resolved (verify with tomorrow's row)

### I-029 · E2B backend not exercised live (no API key)
- Found: 2026-10-10 · M5 follow-up, split from I-016
- Symptom: `E2BSandbox` is verified with a fake client only
- Root cause: no `E2B_API_KEY` on this project
- Fix: pending. `tests/live/test_e2b_live.py` runs exec, files and the network-off check when the key is set
- Sweep: n/a
- Resolved: — · Status: open (needs a key)

### I-017 · `scio/aci/sandbox/` never committed: swallowed by a broad `.gitignore` pattern
- Found: 2026-10-10 · M2 · first CI run on PR #4, "Lint" step
- Symptom: ruff `I001` in the three test files that import `scio.aci.sandbox`; locally everything passed. CI's ruff classified those imports as third-party because the package did not exist in the checkout
- Root cause: `.gitignore` had an unanchored `sandbox/` (meant for a scratch directory at the repo root), which matches any directory named `sandbox` anywhere, including the new source package. `git add -A` silently skipped it; the local run used the working tree, so nothing noticed
- Fix: pattern anchored to `/sandbox/`; package staged. New docs check `check_ignored_sources` fails if any `.py`/`.md`/`.yaml` under `scio`, `evals`, `tests`, `scripts` is git-ignored
- Sweep: `git ls-files --others --ignored --exclude-standard scio evals tests scripts` after the fix lists nothing. Every other `.gitignore` entry reviewed: `evals/results/` and `docs/api/` are intentional; no other unanchored directory names remain. Second-order effect: ruff honours `.gitignore`, so the package had never been linted or formatted locally either; after un-ignoring it, `ruff check --fix` found 4 issues and `ruff format` rewrote one file
- Resolved: 2026-10-10 · Status: resolved

### I-018 · Local check chain masked a failing exit code behind a pipe
- Found: 2026-10-10 · M2 · second CI run on PR #4 failed on lint after a local chain that "passed"
- Symptom: the chained command `ruff check . | tail -1 && ... && git commit` committed and pushed although ruff reported 4 errors
- Root cause: in a pipeline the exit status is the last command's (`tail`), so a failing check never stopped the `&&` chain
- Fix: `set -o pipefail` at the start of every chained check; the lint step runs unpiped
- Sweep: every chained check command in this session used the `| tail -1 &&` shape; the CI workflow runs each check as its own step and was never affected. Rule added to LESSONS_LEARNED pre-flight item 10 and to CLAUDE.md
- Resolved: 2026-10-10 · Status: resolved

### I-019 · Live Anthropic run blocked: `ANTHROPIC_API_KEY` in the environment is invalid
- Found: 2026-10-10 · M3 · first live trial of t001 through the T0 graph
- Symptom: `FatalError: status 401 ... authentication_error: invalid x-api-key` at the `route` node (first model call); the Fake run of the same task passes
- Root cause: environment; the key is set but not accepted by the API
- Fix: by decision of the owner (2026-10-10, M5): the account is a Claude Max subscription, so the key stays a placeholder. Live runs remain gated (`SCIO_RUN_LIVE_TESTS=1`, nightly workflow switches to the real provider only when a repository secret is set). All accounting, budgets, escalation logs and eval metrics keep working as designed, measured on the Fake provider at real-model prices and labelled as such
- Sweep: the harness classifies 401 as fatal (no retry, no spend), which is the intended behaviour; the eval runner records it as a failed trial with the error text. README's acceptance table marks the live criteria "Fake-provider evidence only"
- Resolved: 2026-10-10 · Status: wontfix (placeholder key by design; re-open when an API key exists)
- Re-opened and resolved 2026-10-11: the owner logged in with `ant auth login` (Claude account, individual org, `user:inference` scope); the SDK picks the profile up with no code change, and the placeholder key is bypassed with `env -u ANTHROPIC_API_KEY`. First live acceptance run: t001 pass^5 = 1.00. The live runs then exposed I-031 to I-035 · Status: resolved

### I-020 · Project lint and type-check ran over the eval fixture repository
- Found: 2026-10-10 · M3 · `uv run mypy` and `ruff check` after adding `evals/tasks/t001_add_function/repo`
- Symptom: mypy `import-not-found` for `textutils` and `no-untyped-def` in the fixture's tests; ruff E501 in the hidden test
- Root cause: `packages = ["scio", "evals"]` and ruff's default discovery include everything under `evals/`, but a fixture repository is the *agent's* code and has its own conventions
- Fix: `[tool.mypy] exclude = ["evals/tasks/"]`, `[tool.ruff] extend-exclude = ["evals/tasks/*/repo"]`; the hidden test (ours) stays linted
- Sweep: `scripts/check_docs.py` link check already skips nothing under `evals/tasks`; its model-ID check scans fixtures too, which is fine (no IDs there). Future fixtures go under `evals/tasks/<id>/repo` so the exclusions apply automatically
- Resolved: 2026-10-10 · Status: resolved

### I-021 · LangGraph warns about unregistered state types in checkpoints
- Found: 2026-10-10 · M3 · first graph smoke run
- Symptom: `Deserializing unregistered type scio.state.models.TaskSpec from checkpoint. This will be blocked in a future version` for every Pydantic model and enum in the state
- Root cause: LangGraph's msgpack serializer requires an allow-list of (module, name) pairs for custom types
- Fix: `scio.graph.checkpointer.serializer()` builds the allow-list from every model and enum in `scio.state.models`; the SQLite and in-memory savers are constructed with it (the `from_conn_string` helper does not accept a serializer)
- Sweep: the Postgres path still uses `from_conn_string`; it needs the same construction when it is first exercised (noted in `checkpointer.py`)
- Resolved: 2026-10-10 · Status: resolved

### I-022 · Scripted Fake replies mis-counted in two loop tests
- Found: 2026-10-10 · M3 · `tests/unit/test_context_loop.py` cap tests
- Symptom: `StructuredOutputError` because the summary call consumed a scripted *tool* reply
- Root cause: the number of enqueued tool replies must equal the number of tool turns the loop will actually make (allowed + one refused at the cap) with the JSON summary last; I enqueued "plenty"
- Fix: scripts sized exactly; comment in the test states the rule
- Sweep: `evals/fake_scripts.ScriptedResponder` keys on `request.output_schema` instead of position, so graph-level runs are immune; only queue-based tests need exact sizing
- Resolved: 2026-10-10 · Status: resolved

### I-023 · Commit chain broke on a doubled `-q`, and the lines after the heredoc ran anyway
- Found: 2026-10-10 · M3 · the commit-push-PR chain for M3
- Symptom: `gh pr create` failed with `No commits between main and feat/5-router-t0-loop`; the push had sent an empty branch
- Root cause: two defects. (1) `pyproject.toml` already sets `addopts = "-q"`, so `pytest -q` became `-qq` and printed no summary line; the `grep "passed|failed"` step matched nothing and the `&&` chain stopped before `git commit`. (2) `git push` and `gh pr create` were written on new lines after the heredoc, so they were separate commands, not part of the chain, and ran regardless
- Fix: pytest invoked without the extra `-q`; the whole chain is one logical command (`git commit -F - <<'EOF' && git push ... && gh pr create ...` with the heredoc body following)
- Sweep: CLAUDE.md's pre-push command does not pass `-q`; CI runs `pytest` without it. Pre-flight item 11 added: anything that must not run after a failed check belongs on the same `&&` line, and a heredoc's continuation `&&` goes on the opener line
- Resolved: 2026-10-10 · Status: resolved

### I-024 · Schema-kind detection in the fake matched the planner before the router
- Found: 2026-10-10 · M4 · first T1 end-to-end test
- Symptom: `StructuredOutputError: RouterOutput: still invalid after 2 repairs`; the fake answered the router's schema with a plan
- Root cause: the fake decides which JSON to return by the first property marker it finds; `rationale` (checked for the planner) is also a `RouterOutput` field, and the router check came later
- Fix: markers reordered most-specific first (`tier`, `exploration_questions`, `steps`, ...); the planner is detected by `steps`
- Sweep: every marker in the table is now unique to its schema (`tier`, `exploration_questions`, `steps`, `relevant_files`, `guidance`, `findings`, `objective_met`, `plan_vs_done`, `next_action`); adding a schema requires a field no other schema has
- Resolved: 2026-10-10 · Status: resolved

### I-025 · Step advanced before the edge evaluated it; checkpoint stamped every feature
- Found: 2026-10-10 · M4 · `test_t1_two_steps_one_commit_each` (one combined commit instead of one per step)
- Symptom: after step S1 passed, the run routed to triage/debug for S2 instead of committing S1; after that fix, S2 had nothing to commit
- Root cause: two defects. `verify` advanced `current_step_id` to the next step inside the node, so the `after_verify` edge judged the *next* step's features; and `git_checkpoint` stamped the commit SHA on every feature without one, not only the features it committed
- Fix: `verify` marks the step done and sets `last_failure_kind`; the edge reads only that; `git_checkpoint` commits the pending passing features, stamps exactly those, and advances the step (ADR 0005 decision 7)
- Sweep: searched the nodes for other "node mutates what the following edge reads" patterns: `advise` sets `phase` for `after_advise` and `exit_reason` for `halt`, both intentionally and documented. No other node advances a cursor its edge depends on
- Resolved: 2026-10-10 · Status: resolved

### I-026 · Writer toolboxes untagged, so the one-writer assertion saw role "agent"
- Found: 2026-10-10 · M4 · `test_t2_parallel_explorers_and_one_writer`
- Symptom: `execute_tool edit_file` spans carried `scio.role=agent` for the T0 coder and the documenter
- Root cause: `ToolBox(role=...)` was added for M4 and set in the new nodes only; the M3 call sites kept the default
- Fix: every `ToolBox` construction names its role; the assertion is `edit roles ⊆ {coder, debugger, documenter}`
- Sweep: `grep -n "ToolBox(" scio/` shows five construction sites, all with `role=`; a default of `"agent"` remains for ad-hoc use but no node uses it
- Resolved: 2026-10-10 · Status: resolved

### I-027 · Recurrence of I-010: escapes in a shell-embedded patch script corrupted two modules
- Found: 2026-10-10 · M5 · `ruff format` reported `f-string: unterminated string` in `scio/cli.py` and `missing closing quote` in `scio/harness/accounting.py`
- Symptom: `newline="\n"` and `+ "\n"` landed as a quote, a real line break, and a quote in four places; the package did not import
- Root cause: the same mechanism as I-010. The rule (pre-flight item 6) existed and was broken anyway, because the patch script was long and the escapes were buried in it
- Fix: all four sites repaired with the file-edit tool; `grep -rn -A1 'newline="$'` and `grep -rn '"$'` over `scio evals scripts tests` confirmed no other corrupted strings
- Sweep: every remaining shell-embedded patch in this milestone is escape-free by construction. Guard added: pre-flight item 6 now says to run `python -c "import scio"` (or the lint step) immediately after any shell-embedded patch, before writing anything else, so a corrupted module is caught in the same step that produced it
- Resolved: 2026-10-10 · Status: resolved

### I-028 · The real end-to-end PR committed `__pycache__` and `.pyc` files
- Found: 2026-10-10 · M5 · PR #2 on `NewMe505/scio-eval-sandbox` (the first run against a real GitHub repository)
- Symptom: the PR's file list contained `tests/__pycache__/*.pyc` and `textutils/__pycache__/*.pyc` next to the real changes
- Root cause: `git_checkpoint` stages with `git add -A`; `run_tests` had produced caches; the target repository has no `.gitignore`, and nothing in SCIO excluded them. The Fake-provider tests never noticed because they only counted commits
- Fix: `prepare_repo` writes repo-local excludes (`.git/info/exclude`: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`), which are never committed and do not touch the target's own `.gitignore`. The T0 happy-path test now asserts no cache files are tracked
- Sweep: the only other staging call is the docs commit in `document`, which goes through the same `git_checkpoint`; `run_tests` already passes `-p no:cacheprovider`, which removes `.pytest_cache` but not Python's own bytecode cache
- Resolved: 2026-10-10 · Status: resolved

### I-009 · Branch protection unavailable on a private free-plan repository
- Found: 2026-10-10 · M0 · `gh api repos/.../rulesets`
- Symptom: `Upgrade to GitHub Pro or make this repository public to enable this feature` (403)
- Root cause: rulesets and branch protection on private repos require GitHub Pro
- Fix: the owner made the repository public on 2026-10-10; ruleset `protect-main` (active) now requires a pull request and the `checks` status for the default branch and blocks force-pushes and deletion
- Sweep: the only other repository SCIO wrote to (the throwaway evaluation repository) has been deleted; `GitHubClient` already refuses default-branch writes on its side
- Resolved: 2026-10-10 · Status: resolved
