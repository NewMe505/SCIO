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
- Update 2026-10-26 (M9, ADR 0013 decision 10): the live file now covers the persistent container, the internal network, the egress proxy's allowlist, the product environment and an http app inside the container, and git on the mount, in CI's own `docker-live` job; the dev machine still has no daemon, so every Docker change is first run by CI
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
- Root cause (inferred, not reproduced): six further live t003 trials with state capture all passed ($2.33; first attempt died on I-044 at $0). Every passing gate verdict carried the same caveats: "no separate docs-check output" (the fixture has no docs check, so `gate_message` omitted the section) and "the docstring's wording cannot be inspected from the diff summary alone". A verdict that acts on those caveats is `replan`, and `gate` mapped `replan` to `ExitReason.ESCALATE_HUMAN`, although ARCHITECTURE draws `gate --replan--> plan`. So one draw in seven turned a pass-worthy run into a human escalation
- Fix: (1) `gate_message` always carries a docs-check line (`clean: no docs-check failures reported` when there is nothing to run); (2) gate prompt v2 says the diff is a stat, a criterion is evidenced by passing feature tests, and `replan` means more work would meet it; (3) `gate` returns to `plan` on the first `replan` of a planned run (`progress.gate_replans`, cap 1), passing its reasons as the plan revision reason; the planner appends a step when every step is already done; T0 and a second replan still halt for a human. `tests/graph/test_gate_replan.py`
- Sweep: every verdict-to-edge mapping checked against the ARCHITECTURE diagrams: advisor (`continue`/`change_approach`/`replan`/`escalate_human`) matched; gate did not. Exposed I-046 in the process
- Resolved: 2026-10-11 · Status: resolved (nightly rows will show a `replan` instead of an escalation if the draw recurs)

### I-046 · A worker's reported `step_id` moved the run onto a finished step; passing verifies looped without limit
- Found: 2026-10-11 · `tests/graph/test_gate_replan.py` hung (first run of the gate-replan path); checkpoint dump showed 24 iterations, stall 22, `current_step_id=S1` (done) while `S2` stayed open
- Symptom: coder → verify (all checks passed) → checkpoint (nothing pending) → implement → … forever; no exit reason, because `check_exit` ran only after a failing verify
- Root cause: two defects. `implement.py` wrote `output.step_id or current_step_id`, so a model naming the wrong step (the scripted coder names `S1` for unknown steps; a live model can do the same) changed the plan's current step; and the stall counter was advanced on passing verifies but never compared to `max_stall`, so a pass that changed nothing was not an exit
- Fix: the plan owns the current step (`current_step_id or output.step_id`); a passing verify with `stall_count > max_stall` exits `STALL`. `test_worker_cannot_move_the_run_onto_a_finished_step` (t004, three steps, iterations ≤ 4)
- Sweep: `grep -rn "current_step_id" scio`: the other writer from model output is `worker.py` (T0, no plan, informational only; left as is). `grep -n "check_exit"`: the only call was in the failing branch of verify. Guard: every pass through verify now has a cap, so no graph edge can loop unbounded on green checks
- Resolved: 2026-10-11 · Status: resolved

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

### I-047 · Nightly history row written for a suite with no applicable tasks
- Found: 2026-10-12 · first nightly run after PR #19, run early by hand
- Symptom: `docs/EVAL_HISTORY.md` gained `capability … 0.00 | 0.00 | $0.00 of $4`, which reads as a total failure; the suite ran zero tasks because all three scenario tasks are Fake-only (I-042)
- Root cause: `append_history` wrote metrics unconditionally; `SuiteReport.pass_at_1` is 0.0 for an empty task list
- Fix: `append_history` returns a one-line note and writes nothing when `report.tasks` is empty; the misleading row removed. `test_history_skips_suite_with_no_tasks`
- Sweep: `SuiteReport.to_markdown` already prints `not applicable here: …`; `evals.runner --min-pass-pow-k` on an empty suite would also fail (pass^k 0.0), left as is because an empty regression suite is a real failure. The GitHub nightly workflow runs on the Fake provider where all tasks apply
- Resolved: 2026-10-12 · Status: resolved

### I-048 · Fixture bytecode became tracked content and showed up in the agent's commits
- Found: 2026-10-12 · first live trial of t006 and t007 (issue #25); the gate's reasoning noted "compiled __pycache__ .pyc files were committed" on both runs
- Symptom: the agent's diff and commits included `__pycache__/*.pyc` under the fixture packages; the gate passed with a non-blocking caveat
- Root cause: the fixtures' tests had been run in place while authoring them, leaving `__pycache__` on disk (git-ignored in SCIO, so invisible to `git status`). `make_origin` copied the whole tree, so the `.pyc` files were part of the fixture's base commit; `prepare` writes `.git/info/exclude`, but an exclude only hides *untracked* files, and tracked bytecode is rewritten by every test run
- Fix: `make_origin` copies with `shutil.ignore_patterns` for `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`; the stray directories deleted from the working tree. `test_make_origin_leaves_caches_behind`
- Sweep: `find evals/tasks -name __pycache__` now empty; `grep -rn "copytree" evals scio tests scripts` finds one call, `make_origin`, so every sandbox (local, Docker mount, E2B upload) starts from the filtered clone. The scratch diagnostics copy whole task directories but hand the repo to `make_origin`
- Resolved: 2026-10-12 · Status: resolved

### I-049 · Every eval trial leaked its temp directory on Windows (636 directories, 51 MB)
- Found: 2026-10-12 · issue #29 · `ls $TEMP/scio-*` after the first live trial of t008 showed three t008 directories where one was kept on purpose
- Symptom: `scio-<task>-<rand>` directories accumulate under `%TEMP%`, each holding `origin/.git/objects` and `.scio/effects.sqlite`; nothing reported a failure
- Root cause: two defects hidden by one flag. `GraphSolver.solve` cleaned up with `shutil.rmtree(tmp, ignore_errors=True)`. (1) git object files are read-only, and `rmtree` cannot delete them on Windows without clearing the bit. (2) `RunContext` never closed the effect log's SQLite connection, so `effects.sqlite` was still open (`WinError 32`) when the cleanup ran. `ignore_errors` swallowed both
- Fix: `evals.solvers.remove_tree` deletes with an `onexc` handler that clears the read-only bit and retries; `RunContext.close()` closes the effect log and `GraphSolver._solve` calls it in a `finally`; the solver logs a warning instead of silently ignoring a cleanup failure. The 636 leaked directories were removed with the new helper
- Sweep: `grep -rn "rmtree\|TemporaryDirectory\|mkdtemp" scio evals tests scripts` finds one other temp site, `evals/tune.py` (one small YAML file per sweep, no handles; left as is). `grep -rn "sqlite3.connect" scio` finds two: the checkpointer (closed in its context manager) and the effect log (now closed by `RunContext.close`). Guards: `test_remove_tree_deletes_read_only_git_objects`, and `test_cli_exit_codes` now points `tempfile.tempdir` at `tmp_path` and asserts no `scio-*` directory survives a suite run
- Resolved: 2026-10-12 · Status: resolved

### I-050 · The router rated t008 T0, so the task never reached plan, verify or the debugger
- Found: 2026-10-12 · issue #29 · first live trial of t008 (`sonnet_opus_advisor`): `tier=T0 exit=success iterations=2 tool_calls=17`, $0.19
- Symptom: the task passed, but through the T0 loop: the coder saw the failing snapshot test in its own full-suite run and fixed it on the next turn. The debugger, the node the task was built to exercise live (ADR 0006 decision 4), was not called
- Root cause: wrong assumption that `tier_hint: T1` drives the tier. The hint only sizes the budget; the router (Haiku) decides, its prompt says to prefer the cheaper tier when in doubt, and it read the "existing suite passes and the documented example is correct" criteria as "minor verification rather than scope expansion" (confidence 0.70). The router sees the spec and a file count, not the test suite's coupling, so no rewording of the spec makes the route deterministic
- Fix: `tier_pin:` in `task.yaml` (`EvalTask`, `EvalTaskLike`, `RunContext.tier_pin`); `route` emits that tier with `classifier_model="pinned"` and no model call. t008 pins T1. Second live trial with the pin: routed T1, passed, $0.39, two steps, no debugger either: the coder ran the full suite inside its own turn and regenerated the snapshot before verify saw it (LESSONS_LEARNED, "Snapshot coupling task")
- Sweep: `grep -rn "tier_hint" evals scio` shows the hint still feeds only budgets and the router message (as a hint); no other task depends on a tier it cannot guarantee (the capability tasks reuse scripts that return the tier on the Fake). Guard: `tests/unit/test_tier_pin.py` (pinned route skips the classifier; t008 is the only pinned task)
- Resolved: 2026-10-12 · Status: resolved

### I-051 · t009 hidden test failed SCIO's own lint (import block order)
- Found: 2026-10-12 · issue #31 · `uv run ruff check .` on the first run after writing `evals/tasks/t009_verify_only_lint/hidden/test_stdev_hidden.py`
- Symptom: `I001 Import block is un-sorted or un-formatted`: `import pytest` and `from metrics.stats import stdev` were separated by a blank line
- Root cause: hidden tests live under `evals/`, so SCIO's ruff config lints them, and it classifies both `pytest` and the fixture package as third-party (one block). t007's hidden test never hit this because it imports no third-party module besides the fixture
- Fix: the two imports joined into one block
- Sweep: `uv run ruff check evals/tasks` clean for all nine tasks; the rule is already enforced by the CI chain, so no extra guard
- Resolved: 2026-10-12 · Status: resolved

### I-052 · Test assumed a per-iteration `verify:<n>` artifact key that verify never writes
- Found: 2026-10-12 · issue #31 · first run of `test_t1_lint_only_failure_runs_the_debugger`
- Symptom: `KeyError: 'verify:1'`
- Root cause: verify stores its output under the single key `verify:last` (the numbered files on disk are the artifact store's own numbering); workers store `<role>:<iteration>`. I assumed the worker convention applied to verify
- Fix: the test reads the debugger's transcript (`debugger:2`), which embeds the verify output it was shown, and asserts it contains the lint failure and no test failure
- Sweep: `grep -rn '"verify:' scio tests` shows one writer and no other reader of a numbered verify key
- Resolved: 2026-10-12 · Status: resolved

### I-029 · E2B backend not exercised live (no API key)
- Found: 2026-10-10 · M5 follow-up, split from I-016
- Symptom: `E2BSandbox` is verified with a fake client only
- Root cause: no `E2B_API_KEY` on this project
- Fix: none; decision 2026-10-12 (issue #23): no E2B account. The local subprocess backend stays the default for the project's own fixtures (every live and nightly run so far used it); `DockerSandbox` is the isolated tier and its live test runs in CI on every PR (I-016). The E2B backend and `tests/live/test_e2b_live.py` stay in the tree, skipped without the key. The dev machine has neither Docker nor WSL; running Docker there needs `wsl --install`, a reboot, and Docker Desktop or Docker Engine in the distro
- Sweep: `grep -n "E2B" docs/*.md docs/adr/*.md` — ADR 0003 consequence updated to say which tiers are live-verified
- Resolved: 2026-10-12 · Status: wontfix

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

### I-053 · `scio run --tier` sets the budget but not the route
- Found: 2026-10-11 · Product Mode survey (#34) · `scio/cli.py` passes `tier` to `RunContext.create`, which uses it only for the budget row; `tier_pin` is set only by the eval runner
- Symptom: a CLI run with `--tier T0` can be routed to T2 by the router and then work under T0's $0.50 budget (same failure class as I-035 and I-050)
- Root cause: `tier` only selected the budget row; the route came from the classifier, which prefers the cheaper tier (I-050)
- Fix: M6 (PR #41): `scio run --tier` passes `tier_pin` to `RunContext.create` and `route` skips the classifier; without `--tier` a default budget is resized for the routed tier (`apply_tier_budgets`)
- Sweep: every `RunContext.create` call site (`cli.py`, `evals/solvers.py`, tests) reviewed for the same hint-versus-pin confusion; guard `tests/unit/test_m6_foundations.py` (default budget follows the routed tier, explicit budgets untouched). Entry completed late, in M8 (the M6 retrospective recorded the fix but this entry was left open)
- Resolved: 2026-10-12 · Status: resolved

### I-054 · `SCIO_SANDBOX_BACKEND` is never read
- Found: 2026-10-11 · Product Mode survey (#34) · `grep -rn sandbox_backend scio` finds only the field in `scio/settings.py`
- Symptom: setting `SCIO_SANDBOX_BACKEND=docker` has no effect; `RunContext.create` always builds a `LocalSandbox`
- Root cause: the field was declared with the other M2 settings and no reader was ever written
- Fix: M6 (PR #41): `make_sandbox` in `scio/graph/run_context.py` selects local, docker or e2b from `settings.sandbox_backend`
- Sweep: every `Settings` field grepped for a reader; four more declared-but-unread fields found and wired (I-059); guard `tests/unit/test_m6_foundations.py` (backend selection). Entry completed late, in M8
- Resolved: 2026-10-12 · Status: resolved

### I-058 · A commit landed on local `main` because another session switched the shared checkout
- Found: 2026-10-11 · Product Mode docs (#34) · `git push` rejected (non-fast-forward); `git status -sb` showed `main` instead of `docs/34-product-mode`
- Symptom: the docs commit was on local `main`; the push of `main` was rejected, so nothing reached the remote
- Root cause: two AI sessions shared one working checkout. Between `git switch -c docs/34-product-mode` and `git commit`, the other session (which was cutting `release/0.3.0`) ran `git checkout main` in this checkout (reflog `HEAD@{1}`); the commit command did not check the branch first
- Fix: moved the commit to `docs/34-product-mode`, reset local `main` to `origin/main`, rebased onto `origin/main` (clean)
- Sweep: `git branch -vv` and `git worktree list` showed one other worktree (`release/0.3.0`) and no other stray commits; the ruleset on `main` (I-009) would also have rejected a direct push
- Guard: CONTRIBUTING rule: one session per checkout (others use `git worktree add`), and every commit command starts with a branch assertion (`test "$(git branch --show-current)" = "<branch>"`)
- Resolved: 2026-10-11 · Status: resolved
### I-055 · Package version stayed at 0.1.0 through the 0.2.0 release
- Found: 2026-10-12 · release 0.3.0 cut · `grep -n '^version' pyproject.toml` showed `0.1.0` while `v0.2.0` was tagged
- Symptom: `pyproject.toml` and `scio.__version__` said 0.1.0 after the 0.2.0 release; the changelog and the tag were the only places the version moved
- Root cause: the 0.2.0 release PR (#16) cut the changelog only; nothing in the release procedure or `scripts/release_notes.py` touches the package version
- Fix: both files bumped to 0.3.0 in this release; the release procedure in the auto-version-control rule now includes the two version fields
- Sweep: `grep -rn "0.1.0\|__version__" scio tests` finds no other copy of the version and no test pinning it. Guard: `test_package_version_matches_changelog` asserts `scio.__version__` equals the newest released section of `CHANGELOG.md`
- Resolved: 2026-10-12 · Status: resolved

### I-056 · Issue #37 assumed a wide diff alone would call the advisor
- Found: 2026-10-12 · issue #37 · reading `scio/loop/escalation.py` and `verify.py` before writing the task (a read-only research pass)
- Symptom: the issue's premise, "a 9+ file diff trips the high-impact signal and `advise` runs", was false: `decide` runs only from a failing verify, so a passing wide diff went straight to checkpoint
- Root cause: the ARCHITECTURE triage diagram starts at "verify failed" and ADR 0005 routes a passing high-impact diff to a Documenter ADR only. The issue was written from the signal list, not from the entry condition
- Fix: ADR 0008; `review_decision` in `escalation.py`; `verify` stores it on a passing verify, `after_verify` routes to `triage`, `advise` commits on `continue` through the new `advise -> git_checkpoint` edge. Guard: `test_passing_high_impact_diff_is_reviewed_once` and the t010 graph test
- Sweep: `grep -rn "decide(" scio` finds the one call site in `verify`; `grep -rn "after_advise\|after_verify" scio tests` lists every reader of the two edges, all updated. The Mermaid sections of ARCHITECTURE.md are regenerated by `scripts/gen_mermaid.py`, which `check_docs.py` enforces
- Resolved: 2026-10-12 · Status: resolved

### I-057 · The scripted rename was rejected edit by edit: `edit_file` lints every edit
- Found: 2026-10-12 · issue #37 · first run of `test_t1_passing_high_impact_diff_gets_one_advisor_review`: the fake coder applied only `money.py`, every caller edit came back `edit rejected by lint (reverted)`
- Symptom: verify failed with `ImportError: cannot import name 'format_cents'` in every caller; the run escalated on the failing path (high_impact + change_approach) and halted at the advisor cap
- Root cause: `_rename_edits` changed each caller in two edits (import line, then call site). `edit_file` runs ruff's undefined-name check after every edit and reverts one that leaves `format_money` or `format_cents` undefined, so neither half could land on its own. A real coder would hit the same wall if it edited a rename in halves
- Fix: one whole-file edit per caller, built from `MONEY_CALLERS` (signature, body, docstring), the same table the fixture generator used; a check in the test run asserts the fake's "before" text equals each fixture module
- Sweep: `grep -n "Edit(" evals/fake_scripts.py` shows every other script edits an import and its use in one `Edit` or adds whole functions; no other two-step rename exists. Lesson recorded: rename edits must be atomic per file
- Resolved: 2026-10-12 · Status: resolved

### I-059 · Four more `Settings` fields were declared but never read (sweep of I-054)
- Found: 2026-10-12 · M6 (#39) · the I-054 sweep: every `Settings` field grepped for a reader outside `settings.py`
- Symptom: `sandbox_docker_image`, `sandbox_e2b_template`, `effects_path` and `run_live_tests` had no reader. `RunContext` always built a `LocalSandbox` and hard-coded `.scio/effects.sqlite`; `tests/conftest.py` read `SCIO_RUN_LIVE_TESTS` from `os.environ` directly
- Root cause: fields were added with the M2 design and the readers were never written; nothing checks that a declared setting is consumed
- Fix: `make_sandbox(settings, root, limits)` selects `local`/`docker`/`e2b` and passes the image or template (I-054); `RunContext.create` uses `settings.effects_path` when no explicit work dir isolates the run; `live_enabled()` reads `Settings().run_live_tests`. Tests: `test_make_sandbox_reads_the_backend_setting`, `test_effects_path_setting_is_used_without_an_explicit_work_dir`, `test_run_live_tests_setting_is_read`
- Sweep: every field of `Settings` now has at least one reader (`grep -rn "settings\.<field>\|\.<field>" scio evals tests` per field). Guard: none mechanical yet; a test that introspects `Settings.model_fields` and greps for readers is a candidate if the list grows again
- Resolved: 2026-10-12 · Status: resolved

### I-060 · The `ant auth login` profile died mid-session (`invalid_grant`), blocking the M6 live trial
- Found: 2026-10-12 · M6 (#39) · first live trial of t001 on the toolchain-profile code: `WorkloadIdentityError: user_oauth refresh failed (HTTP 400): invalid_grant, Refresh token not found or invalid` at the first `route` call, $0.00 spent
- Symptom: every live call fails before the model is reached; the Fake-provider suite is unaffected. Earlier in the same day the profile served ten live trials (t008 to t010, about $2.40)
- Root cause: the profile's refresh token rotated and the stored one is no longer valid; CLAUDE.md names this exact error as "the profile is dead and the user must run `ant auth login` again". Nothing in SCIO can refresh it
- Fix: the owner re-ran `ant auth login`; the kept t001 trial then passed on the toolchain-profile code ($0.11, 1 iteration, 4 tool calls), the same shape and cost as the nightly rows before M6
- Sweep: `grep -rn invalid_grant scio` shows the error is surfaced as `FatalError` by `scio/harness/providers/anthropic.py`, not retried (correct: retrying a dead token burns nothing but cannot succeed). Guard: `scripts/nightly_live.py` already reports this case; no further code change
- Resolved: 2026-10-12 · Status: resolved

### I-061 · `scio new --thread` on a fresh start resumed a thread that did not exist
- Found: 2026-10-12 · M7 (#42) · first live run of `scio new`: `EmptyInputError: Received no input for __start__`, then a second call failed validating an empty checkpoint (`ProductState` missing thread_id, prompt, target_dir, budgets)
- Symptom: `scio new --prompt ... --dir ... --thread new-live1` went straight to `resume_product`; the first invoke left an empty checkpoint, so the real resume then failed too. Nothing was spent and no directory was created
- Root cause: `_new` treated `--thread` as the resume signal. The Fake end-to-end tests call the library (`run_product` / `resume_product`) and never went through the CLI, so the contract was untested (pre-flight: a fake reproduces the shapes you thought of)
- Fix: the absence of `--prompt` means resume; `--thread` only names the thread. `test_cli_new_starts_with_a_chosen_thread_and_resumes_without_a_prompt` drives the three CLI calls on the Fake and the two usage errors
- Sweep: `scio run` / `scio resume` are separate subcommands and have no such ambiguity. Guard: the CLI test above; README documents the two forms
- Resolved: 2026-10-12 · Status: resolved

### I-062 · `uv.lock` recorded the project at 0.3.0 after two releases
- Found: 2026-10-12 · M8 · `git status` on `main` showed `uv.lock` modified before any work; `uv run` rebuilt and reinstalled `scio` on every invocation
- Symptom: the lock said `version = "0.3.0"` for the `scio` package while `pyproject.toml` said 0.5.0; every `uv run` printed "Uninstalled 1 package / Installed 1 package"
- Root cause: the release chore bumps `pyproject.toml` and `CHANGELOG.md` but never ran `uv lock`, so the lock drifted at 0.4.0 and again at 0.5.0; `uv run` re-syncs the editable package each time it notices the mismatch
- Fix: `uv lock` on the M8 branch; the lock now says 0.5.0
- Sweep: `git log -p --follow uv.lock` shows the version line was last updated at 0.3.0; the two release PRs (#38 area, #45) touched only `pyproject.toml` and `CHANGELOG.md`. Guard: `tests/unit/test_version.py` gains an assertion that the lock's `scio` version equals the project version, so a release cut without relocking fails CI
- Resolved: 2026-10-12 · Status: resolved

### I-063 · A feature delivered only by new files was invisible to verify ("no changes since base")
- Found: 2026-10-12 · M8 · first Fake product build: P1 (a T0 task whose only edit creates `tests/test_entry.py`) stalled after four coder turns with `verify: no_changes` while `git status` showed the new file as `??`
- Symptom: verify's step 0 (`diff_stat`, "something must have changed since the base commit") read `git diff --stat <base>`, which lists tracked files only; an untracked new file is not a change to it, so every iteration was a stall and the run exited `stall`. Every earlier eval script also edited an existing file, so the hole never showed on the Fake, and live coders happened to touch existing files too
- Root cause: `git diff` compares the index and tree to the base; files git has never seen are outside both. The harness equated "diff is empty" with "nothing changed"
- Fix: `scio/graph/nodes/common.py::diff_stat` appends ` new file: <path>` for every untracked file (`git ls-files --others --exclude-standard`); `implement._diff_text` runs `git add -N` on untracked files before `git diff` so the full diff (impact detection, ADR 0008 review, the final diff artifact) includes them
- Sweep: `grep -rn '"diff"' scio` finds the two call sites above plus the product build's `git diff --stat main milestone/n` (committed branches, no untracked files involved). `git_checkpoint` already stages with `git add -A`, so commits were never affected. Guard: `tests/unit/test_cli_and_nodes.py::test_diff_stat_counts_untracked_files_as_changes`, and the M8 Fake product's P1 script is a new-files-only task that runs in every CI
- Resolved: 2026-10-12 · Status: resolved

### I-064 · The `ant auth login` profile died again before the M8 live milestone (recurrence of I-060)
- Found: 2026-10-12 · M8 · `env -u ANTHROPIC_API_KEY -u SCIO_PROVIDER_OVERRIDE uv run scio new --prompt ... --thread new-live2`, first model call (interviewer)
- Symptom: `FatalError: WorkloadIdentityError("user_oauth refresh failed (HTTP 400): {'error': 'invalid_grant', 'error_description': 'Refresh token not found or invalid'}")`; nothing spent, thread `new-live2` holds an empty checkpoint
- Root cause: the profile's refresh token rotates and expires between sessions (I-060); SCIO cannot refresh it, and `scio new` has no pre-flight check, so the failure surfaces as a traceback from inside the graph
- Fix: the owner runs `ant auth login`, then the live milestone runs on a fresh thread (`new-live3`; an empty checkpoint on `new-live2` would otherwise be resumed, I-061). Code change: none in M8; a pre-flight credential probe before the first product call is a candidate (`docs/IDEAS_TRIAGE.md`)
- Sweep: every live command in CLAUDE.md and the nightly script already strips the placeholder key; the gap is the missing probe, not a wrong command. Guard: this entry and the M8 runbook in LESSONS_LEARNED
- Resolved: 2026-10-12 · Status: resolved (the owner logged in again with `ant auth login`; the live milestone ran on thread `new-live3` / `build-todo_cli`, recorded in LESSONS_LEARNED M8)

### I-065 · `scio new` approved a PRODUCT.md the human never saw: model call and interrupt in one node
- Found: 2026-10-12 · M8 live milestone · `scio new --thread new-live3 --decision approve`: the approval payload showed "tagtodo, 8 features"; the repository was bootstrapped with "todo_cli, 7 features"; the cost ledger shows `interviewer: 2 calls`, `product_planner: 2 calls`
- Symptom: every human stop of the M7 graph re-ran the model call before it on resume (interviewer $0.008 and planner $0.057 paid twice, $0.065 wasted), and the answer applied to a fresh draft, not the one shown. The Fake tests could not see it because the scripted planner returns the same draft every call
- Root cause: LangGraph re-executes a node from its first statement when a run resumes; `interrupt()` only short-circuits once reached. `interview` and `specify` made the model call and then interrupted in the same node, and `interview`'s `if state.questions` guard could never fire because the questions reach state only when the node returns, after the interrupt. The M8 loop was written with the stop in its own node (`milestone_gate` then `gate_decision`) but the M7 nodes were not revisited
- Fix: `interview` and `specify` now only call the model and return; `answers` and `approval` are interrupt-only nodes that read the questions and the draft from state (`scio/product/graph.py`); the approval payload's cost comes from state too
- Sweep: `grep -rn "interrupt(" scio` lists seven call sites; `checkpoint_gate.merge_wait` and `halt`, `product.graph.publish`, and `build.gate_decision` / `merge_wait` / `feature` make no model call before the interrupt (`feature` resumes a paused or finished subgraph, which repeats nothing). Guard: `test_the_draft_shown_is_the_draft_approved_and_no_model_call_repeats` drives the Fake with a planner whose draft changes on every call and counts calls; rule added to the pre-flight list in LESSONS_LEARNED
- Resolved: 2026-10-12 · Status: resolved

### I-066 · `scio report` on a feature thread inside a build crashed: thread kind decided by name prefix
- Found: 2026-10-12 · M8 live milestone · `scio report --thread build-todo_cli-p2-a1` exited 1 (validation error: the checkpoint is an `SCIOState`, the CLI tried to read a `ProductBuildState`)
- Symptom: the report command picked the product view for every thread whose id starts with `build-`, and every feature run inside a build is named `build-<product>-...`
- Root cause: the thread kind was inferred from the name, not from the checkpoint; the naming scheme for feature threads was chosen after the report branch was written
- Fix: `scio/cli.py::_is_build_thread` reads the checkpoint and checks for `feature_runs`; the CLI test reports a feature thread as a task run
- Sweep: `grep -rn 'startswith("build-")' scio tests` finds no other use; the memory note "report detects by prefix" was wrong and is corrected by the test. Guard: `test_cli_build_status_and_report`
- Resolved: 2026-10-12 · Status: resolved

### I-067 · A skipped feature's uncommitted edits leaked into the next feature's run
- Found: 2026-10-12 · M8 live milestone · P2's first verify failed on `tests/test_smoke.py::test_todo_console_script_version`, a test P1's attempt had written; the advisor flagged `plan_drift, high_impact` because `pyproject.toml` and `tests/test_smoke.py` were outside S1's expected files ($0.05 advisor call, one extra coder iteration)
- Symptom: P1 exited `escalate_human` and was skipped with its working tree dirty (edits to `cli.py`, `pyproject.toml`, `tests/test_smoke.py`, never committed because verify never passed). The next feature's `prepare_repo` ran `git checkout milestone/1` then `checkout -B agent/p2-...`, and git carried the modified files across, so P2 started from P1's half-done work and committed part of it
- Root cause: the task graph assumes it starts on a clean checkout (it clones in task mode); in a product build the sandbox is the product repository and only the orchestrator knows that a previous attempt ended without a commit. Nothing reset the tree between attempts
- Fix: `feature` node, before a fresh attempt: `git checkout -q <milestone branch>`, `git reset -q --hard`, `git clean -fdq` (ignored files such as `.scio/sandbox` are kept). A resume of a running attempt does not reset
- Sweep: the other places that switch branches in the product build (`plan_milestone` to main, `issues` to the milestone branch, `merge_wait` to main, `update_progress` to main) run after a passed feature's fast-forward or after the loop, when the tree is clean; `prepare_repo` in task mode clones fresh. Guard: `test_feature_failure_stops_for_one_human_decision` plants a stray file and a modified tracked file before answering `skip` and asserts the next attempt starts clean
- Resolved: 2026-10-12 · Status: resolved

### I-068 · A transient `claude -p` error with no message was treated as fatal by the Claude Code backend
- Found: 2026-10-12 · Claude Code backend live milestone (#47) · `scio new` through `SCIO_PROVIDER_OVERRIDE=claude_code`, first interviewer call
- Symptom: `FatalError: claude -p failed: unknown error`; the same call repeated by hand a minute later succeeded ($0.01 estimate, Opus)
- Root cause: print mode returned `is_error: true` with neither `result` nor `error` text (a transient failure of the run itself); `parse_result` mapped every `is_error` without a retryable keyword to `FatalError`, so the harness's retry policy never saw it, and the message carried no subtype to diagnose
- Fix: `parse_result` builds the message from `result`, `error`, `errors` and `subtype`; an `is_error` with no usable message, or whose subtype names a transient condition (`error_during_execution`, `error_max_turns` excluded), raises `RetryableError` so `with_retry` re-runs the process; authentication, credit and budget messages stay fatal
- Sweep: the worker's `_summary` and stream path go through the same `parse_result`; the `ClaudeRunResult` non-zero-exit path already carried stderr. Guard: `test_provider_failures_are_classified` gains the empty-message and subtype cases
- Resolved: 2026-10-12 · Status: resolved

### I-069 · T0 budgets sized at API prices starve a Claude Code turn; `error_max_budget_usd` crashed the build
- Found: 2026-10-12 · Claude Code backend live milestone (#47) · `scio build` on `todo_tags`, P1 (T0): the coder turn passed ($0.21 estimate), the documenter's `claude -p` stopped at `--max-budget-usd 0.29` (the T0 cap of $0.50 minus the coder's spend) and the whole build died with `FatalError: claude -p failed: Reached maximum budget ($0.29) (subtype=error_max_budget_usd)`
- Symptom: a T0 task that costs $0.11 on the API costs about $0.57 in Claude Code's estimate (ADR 0012 live trial), so the T0 tier cap, written for API prices, is reached by the second worker turn; and the cap was reported by the binary as an error the backend mapped to a fatal crash instead of SCIO's graceful `budget` exit
- Root cause: two things. The tier budgets (`configs/budgets.yaml`) are API-price guards and the backend passed the remaining amount straight to `--max-budget-usd`; and `parse_result` had no mapping from `error_max_budget_usd` to `BudgetExceeded`, which every node already catches
- Fix: `Settings.claude_code_budget_factor` (default 4.0, from the measured t001 ratio) scales every tier's `usd_max` and `tokens_max` when the worker backend is `claude_code` (`BudgetsConfig.scaled`, applied in `RunContext.create`, `apply_tier_budgets` and `feature_context`); `parse_result` raises `BudgetExceeded` on `error_max_budget_usd`, so the task halts with `exit_reason=budget` and a product build stops for one human decision instead of a traceback
- Sweep: `grep -rn "BudgetsConfig.load" scio evals` lists the three loaders (all now go through `tier_budgets(settings)`); `evals/solvers.py` sizes eval budgets from `task.yaml` and is scaled the same way. Guard: `test_claude_code_backend.py` (budget exit mapping, scaled tiers), the live build resumed to its PR
- Resolved: 2026-10-12 · Status: resolved

### I-070 · A Claude Code summary over a field's max length crashed the build after one re-ask
- Found: 2026-10-12 · Claude Code backend live milestone (#47) · `scio build` on `todo_tags`, P4's documenter: `FatalError: claude -p (documenter) summary never matched WorkerOutput: summary String should have at most 800 characters`
- Symptom: P1 to P3 had passed; the documenter's typed summary was 1,000+ characters, the one re-ask with the validation error came back long again, and the whole build died
- Root cause: the JSON schema handed to `--json-schema` is the API wire schema, which carries no `maxLength` (the structured-output grammar does not support it), so Claude Code cannot enforce SCIO's field caps; the worker treated a length overflow like a structural mismatch and gave up after one re-ask instead of trimming, although a cap on prose is a presentation limit, not a correctness one
- Fix: `coerce_lengths` in `scio/loop/claude_code_worker.py` truncates string fields that exceed their `max_length` (from the model's field constraints) and re-validates locally before any re-ask; a re-ask now only happens for errors that are not length overflows; the re-ask text names the field caps
- Sweep: `AgentLoop` goes through `structured_call` (API repair loop) where the same overflow would also re-ask; `grep -n "max_length" scio/state/models.py` lists every capped prose field (summary 800, guidance 2800, reasoning 2000, rationale 400/600/800, description 400, vision 1200, notes 400). Guard: `test_worker_trims_overlong_prose_fields_instead_of_failing`
- Resolved: 2026-10-12 · Status: resolved

### I-071 · A `\n` escape passed through a shell-embedded Python patch became a real newline (recurrence of I-010, I-027)
- Found: 2026-10-13 · M9 · appending two tests to `tests/unit/test_aci_tools.py` through a `python - <<'EOF'` heredoc whose string literals contained `\n`
- Symptom: `ruff format` refused the file: `missing closing quote in string literal` at the two lines where `"X = 1\n"` had been written as a quote, a newline and a quote
- Root cause: pre-flight item 6 says never pass a backslash through a shell-embedded script; the patch did, and the double escaping guessed for the heredoc was wrong. The lint step in the same command caught it before any test ran, which is the mechanical guard from I-027 doing its job
- Fix: the two lines rewritten with the file edit tool
- Sweep: the other shell-embedded patches of this milestone (`test_sandbox.py` fake, `backends.py`, `nightly_live.py`) carried no backslashes. Guard: unchanged (lint or import immediately after every shell patch); the rule stands, the habit slipped
- Resolved: 2026-10-13 · Status: resolved

### I-072 · Three more shell-embedded patches mangled backslashes in one session (I-071 recurred twice within the hour)
- Found: 2026-10-13 · M9 · `python - <<'EOF'` patches of `scio/product/scaffold.py` and `scio/product/build.py`: the first aborted with a `SyntaxError` inside the patch script, the second failed its anchor assertion because the anchor text contained `\n`, so nothing was written and the work was redone with the file tools
- Symptom: wasted turns; no corrupted file (every patch asserted its anchors or crashed before writing, and the lint-after-patch rule caught the one that did write, I-071)
- Root cause: the habit of patching source through a shell heredoc whenever an edit spans several places, against pre-flight item 6, which exists because of exactly this (I-001, I-010, I-027). Double-escaping the backslashes for the heredoc is guesswork; the shell wrapper and the heredoc disagree on when `\n` is a newline
- Fix: no code change. Rule tightened for the assistant: any patch whose text contains a backslash, a quote-heavy Python literal, or a regex is done with the Edit/Write tools, one edit per site, never through a heredoc; heredocs are for appending prose to Markdown and for short shell lists only
- Sweep: this session's remaining heredoc patches (ledger and changelog appends, yaml appends) carried no backslashes. Guard: the assistant's project memory records the rule (feedback memory `scio-no-shell-patches`), so it is loaded in every session
- Resolved: 2026-10-13 · Status: resolved

### I-073 · Two test modules with the same basename broke a product's whole suite (pytest import file mismatch)
- Found: 2026-10-13 · M9 · the Fake `todo_api` product: P2's scripted coder added `tests/test_todos.py` next to the scaffold's `tests/acceptance/test_todos.py`; every `pytest` run in the repository then failed at collection with `import file mismatch ... use a unique basename`, so verify failed on every iteration and the feature stalled
- Symptom: a feature that was correct could never pass verify; the debugger's turns could not see the cause because the error names pytest's import rules, not the code
- Root cause: the scaffold's `tests/` and `tests/acceptance/` were plain directories, so pytest imported both files as top-level module `test_todos`; any product that keeps unit and acceptance tests with matching names hits this
- Fix: the scaffold ships `tests/__init__.py` and `tests/acceptance/__init__.py`, so modules are package-qualified and basenames may repeat; the scripted acceptance test was also renamed
- Sweep: `evals/tasks/*/repo/tests` fixtures are single directories (no nesting), unaffected; SCIO's own `tests/` already has `__init__.py` files. Guard: `test_http_product_with_a_dependency_builds_and_passes_black_box_acceptance` keeps a unit test and an acceptance test with related names in the two directories
- Resolved: 2026-10-13 · Status: resolved

### I-074 · The product environment saw the base interpreter's packages, not SCIO's: CI had no pytest and every Fake product build stalled
- Found: 2026-10-26 · M9 · PR #50's CI run (38453958604) failed 8 tests that pass on the dev machine: `test_product_env_is_created_once_and_rewrites_python` with `ModuleNotFoundError: No module named 'pytest'` inside the product venv, and the seven `test_product_build_fake.py` builds stalled at `tests (iteration 4, stall 3)` because every verify ran pytest in that venv
- Symptom: green locally, red on `ubuntu-latest`; the stall messages named the feature, not the environment
- Root cause: `venv_argv` used `--system-site-packages`. Under `uv run` the "system" of a new venv is the **base** interpreter (`sys._base_executable`), not the dev `.venv` the process runs in. The dev machine's base Python 3.12 happens to have pytest 9.1.1 installed globally, so the product venv could import it there; the runner's uv-managed Python has nothing. The design intent ("the host's test tools are visible") was met by accident on one machine
- Fix: the venv is plain; on a `LocalSandbox`, `link_host_env` writes `scio_host_env.pth` into the venv's site-packages naming the running process's `sysconfig` purelib, so exactly SCIO's environment (pytest, pytest-cov, pyyaml) is visible and a product's own installs still precede it on `sys.path`; on any other sandbox the new `Runtime.test_tools` (profile `runtime.test_tools`) are installed with the approved packages during the network-open creation call
- Sweep: `grep -rn "system-site-packages\|system_site" scio evals tests docs configs` found the yaml, `DEFAULT_RUNTIME`, ADR 0013 decision 1, the `todo_api` script docstring and the runtime docstring; all updated. No other place creates an interpreter environment (`grep -rn "venv" scio` shows only `scio/aci/runtime.py`). Guard: the unit test now asserts the `.pth` file exists with the host purelib and that `pytest.__file__` resolves under it; `test_non_local_sandbox_installs_the_test_tools_on_creation` covers the remote branch. Lesson for LESSONS_LEARNED: a test that passes only because of what the dev machine has installed globally is a CI failure waiting to happen; assert *where* a tool comes from, not only that it imports
- Resolved: 2026-10-26 · Status: resolved

### I-075 · Windows Application Control blocks the venv's `pytest.exe` and `scio.exe` launchers
- Found: 2026-10-26 · M9 · `uv run pytest` on the dev machine: `error: Failed to spawn: pytest. Caused by: An Application Control policy has blocked this file. (os error 4551)`; `uv run scio` fails the same way, while `uv run ruff`, `mypy` and `pdoc` run
- Symptom: the CI-equivalent command in CLAUDE.md cannot run as written; any session that follows it stops at pytest
- Root cause: the two are uv's 46 KB unsigned trampoline launchers (`.venv/Scripts/*.exe`) with per-file hashes; the machine's Application Control (Smart App Control) policy decides per hash and blocks those two. Environment, not code
- Fix: run the module forms, `uv run python -m pytest` and `uv run python -m scio`; `scio/__main__.py` added so the second exists. CLAUDE.md's CI-equivalent line and the live-run command notes use the module forms. CI (`ubuntu-latest`) is unaffected and keeps `uv run pytest`
- Sweep: `grep -rn "uv run pytest\|uv run scio" CLAUDE.md CONTRIBUTING.md README.md docs scripts` lists README examples (left for users on ordinary machines) and the workflow (Linux, left). Guard: `test_python_m_scio_is_the_console_entry_point` in `tests/unit/test_cli_and_nodes.py`
- Resolved: 2026-10-26 · Status: resolved

### I-076 · `scio new --thread X --decision ...` without `--dir` bootstrapped the product under `.scio/products/<thread>`, not where the thread started
- Found: 2026-10-26 · M9 live http milestone · `scio new --prompt ... --dir <P>` paused for the interview; the two resume calls passed only `--thread` and `--decision`; the approval payload still showed `target_dir: <P>`, but the bootstrap committed into `SCIO/.scio/products/new-http1` and `<P>` stayed empty
- Symptom: a "done" bootstrap with a commit hash and an empty target directory; `scio build --product <P>` says "run scio new first"
- Root cause: `_new` computed `target = args.dir or .scio/products/<thread>` on every call and passed it as the sandbox root, so on resume the CLI's default replaced the directory the thread was started in; the state's `target_dir` was only informational. The Fake CLI test passed `--dir` on every call, so the path was never exercised
- Fix: on resume the CLI reads `target_dir` from the thread's checkpoint (`product_target_dir`) and uses it; a different `--dir` is reported and ignored; a thread with no checkpoint is a usage error (exit 2). The live product was moved by hand to `<P>` (a fresh repository, nothing else referenced the path)
- Sweep: `scio build --product DIR` and `scio status --product DIR` take the directory explicitly on every call and `scio report --thread` reads state only; no other command derives a path on resume (`grep -n "products\|args.dir" scio/cli.py`). Guard: `test_cli_new_starts_with_a_chosen_thread_and_resumes_without_a_prompt` resumes once without `--dir` and once with a different one
- Resolved: 2026-10-26 · Status: resolved

### I-077 · The Claude Code worker's typed summary died `error_max_turns` when the model's first structured answer missed the schema
- Found: 2026-10-26 · M9 live http milestone (`build-bookmarks_api-m1`, feature P1) · the explorer's `localize` turn ended normally; the `--resume ... --tools "" --max-turns 1 --json-schema` summary call returned `Reached maximum number of turns (1) (subtype=error_max_turns)` and `parse_result` raised `FatalError`; the build crashed before any code was written ($0.34 estimated so far)
- Symptom: a fatal crash in a step that has no tools and one question to answer; the previous live milestone (44 runs) never hit it
- Root cause: Claude Code delivers `--json-schema` output through a `StructuredOutput` tool call. The session transcript (`~/.claude/projects/<product>/2ded7e72....jsonl`) shows the model's payload omitted the required `question` key, Claude Code answered the tool call with `Output does not match required schema: root: must have required property 'question'`, and the retry would have been a second turn, which `--max-turns 1` forbade. SCIO's own re-ask loop never ran because the failure arrived as a run error, not an invalid payload
- Fix: `SUMMARY_MAX_TURNS = 3` for the worker's summary and `STRUCTURED_MAX_TURNS = 3` for the provider's schema calls (a plain completion keeps one turn); a summary attempt that still ends `error_max_turns` is re-asked once with the schema's required keys named, and only then fatal
- Sweep: `grep -rn '"--max-turns"' scio` lists the worker's first turn (the loop's cap, unchanged), the worker's summary and the provider's `_invoke`: both schema paths fixed. Guard: `test_worker_summary_capped_by_claude_codes_schema_check_is_re_asked`; the provider argv test asserts three turns for a schema call
- Resolved: 2026-10-26 · Status: resolved

### I-078 · `DockerSandbox.exists` said the product environment was missing after it was created, so every call re-created and re-installed it
- Found: 2026-10-26 · M9 · first `docker-live` CI run (40372435283): `test_product_environment_lives_inside_the_container` got "created" from the second `ensure_product_env` instead of "reused"
- Symptom: a Docker product build would run `python -m venv` and the pip install before every feature and before `integrate` (slow, and a network-open call each time), while the venv itself worked
- Root cause: a venv's `bin/python` is a symlink to the interpreter inside the container (`/usr/local/bin/python`); on the host side of the mount `Path.exists()` follows it to a path that does not exist and answers False. The local sandbox never sees this (its symlink targets are host paths), and the unit tests drove Docker through an injected runner that creates no files
- Fix: `DockerSandbox.exists` is true for a symlink whose target is absent (`exists() or is_symlink()`)
- Sweep: `grep -n "\.exists()" scio/aci/sandbox/*.py`: `LocalSandbox.exists` (host targets, fine), `E2BSandbox.exists` (asks the sandbox, fine). Guard: the Docker unit test creates a dangling symlink and asserts `exists`; the live test asserts "reused"
- Resolved: 2026-10-26 · Status: resolved

### I-079 · The live Docker test expected the egress proxy's refusal as an HTTP 403 response; urllib reports a refused CONNECT as `URLError`
- Found: 2026-10-26 · M9 · same CI run: `test_install_phase_reaches_the_registry_only_through_the_proxy` got `FAIL URLError` for `https://example.com/` while `https://pypi.org/simple/` answered OK
- Symptom: a red test for behaviour that was correct: the proxy refused the host (the allowlisted host went through)
- Root cause: for an https URL the client sends `CONNECT`; a non-2xx reply to the tunnel request is `URLError("Tunnel connection failed: 403 Forbidden")` in urllib, never an `HTTPError`. The test was written on a machine with no Docker, from the proxy's point of view rather than the client's
- Fix: the probe prints the exception text and the test asserts `URLError` with `403` in it
- Sweep: the unit test of the proxy (`test_egress_proxy.py`) reads the raw `HTTP/1.1 403` line, which is what the proxy sends and is unaffected. Lesson: a live test written without the live system is a hypothesis; the first CI run is its experiment (two of nine were wrong)
- Resolved: 2026-10-26 · Status: resolved

### I-080 · Three new tests read `SCIO_PRODUCT_SANDBOX_BACKEND` from the environment and failed only on CI, where the same commit had just set it
- Found: 2026-10-26 · M9 · CI run 40373061815 `checks`: `test_product_sandbox_backend.py` (two tests) got `local` where they assumed `auto`, and the Fake http build test found the ledger notice without "not enforced"; green on the dev machine, where the variable is unset
- Symptom: the pattern of I-074 again (green here, red there), this time created in the same commit: `ci.yml` gained `SCIO_PRODUCT_SANDBOX_BACKEND: local` so the Fake CLI tests never probe the runner's daemon, and tests built with a bare `Settings()` inherited it
- Root cause: `Settings()` is environment-backed (pydantic-settings); a test that means `auto` must say so. The explicit-local notice also omitted what it does not enforce, so the Fake test's assertion depended on how the backend was chosen
- Fix: every `Settings` in the unit test names `product_sandbox_backend`; the explicit `local` notice says "the network and the registry allowlist are not enforced" like the fallbacks do
- Sweep: `grep -rn "Settings()" tests/unit tests/graph` lists tests of fields that no CI variable sets (`provider_override` is set to `fake` there and every such test sets it explicitly already). Rule for `CONTRIBUTING`: a test of a `Settings` field constructs `Settings(field=...)`; never rely on the default when CI exports a value for it
- Resolved: 2026-10-26 · Status: resolved

### I-081 · `subprocess.run(["npm", ...])` fails with `FileNotFoundError` on Windows: npm and npx are `.cmd` shims
- Found: 2026-10-26 · M10 inventory · `node --version` runs from Python, `npm --version` raises `FileNotFoundError` although `shutil.which("npm")` finds `npm.CMD`; through `LocalSandbox.exec` that is exit 127 "command not found"
- Symptom: every npm step of the node-ts profile (install phase, `npm audit`) would fail on the dev machine and pass on Linux
- Root cause: `CreateProcess` resolves a bare name against `PATH` but not against `PATHEXT`, so a `.cmd` shim is only found when the extension is spelled out; Python's `subprocess` inherits that
- Fix: `scio/aci/sandbox/local.py::resolve_command` resolves a bare command word through `shutil.which` on win32 (after the `python` rewrite); `LocalSandbox.exec`, `run_app` and `start_app` all go through it
- Sweep: `grep -rn "subprocess\.\(run\|Popen\)" scio` lists the sandbox (fixed), `docker.py` (`docker` is an `.exe`), `host_git_identity` (`git` is an `.exe`), `run_app`/`start_app` (fixed). Guard: `test_resolve_command_rewrites_the_interpreter_and_finds_cmd_shims_on_windows`
- Resolved: 2026-10-26 · Status: resolved

### I-082 · `run_tests` grew a context parameter where verify passed the toolchain, and the python default silently answered for a TypeScript product
- Found: 2026-10-26 · M10 slice 7 · `test_same_spec_builds_in_both_stacks[node-ts]` stalled P1 at verify: the kept artifact read `no tests collected for filter 'health': no tests ran in 0.00s` (pytest's words) in a repository with no Python test
- Symptom: a Node product that passes `node --test` by hand fails verify on every iteration; the Fake python build was unaffected, so the unit tests stayed green
- Root cause: slice 4 made `run_tests(sandbox, inp, ctx=None, toolchain=None)` context-aware; `verify.py` called `run_tests(sb, inp, ctx.toolchain)` positionally, so the toolchain landed in `ctx`, `getattr(ctx, "toolchain")` was None, and `python_toolchain()` filled in. A default that hides a wrong call is the hazard (pre-flight 12)
- Fix: the two verify calls and the eval solver pass `toolchain=` by keyword; `_toolchain_of` also accepts a `Toolchain` in the context position
- Sweep: `grep -rn "run_tests(\|edit_file(" scio evals` found exactly the three direct callers (verify twice, `evals/solvers.py`), all fixed. Guard: `test_tools_take_the_toolchain_from_the_context_or_the_direct_argument` runs the node profile through all three call shapes; the both-stacks Fake build is the end-to-end check
- Resolved: 2026-10-26 · Status: resolved

### I-083 · `node --check` accepted a TypeScript parameter property that Node then refused to load, so the edit gate let a broken file through
- Found: 2026-10-26 · M10 slice 7 · the scripted P2 `src/store.ts` used `constructor(private readonly path: string)`; `edit_file`'s syntax gate (`node --check`) passed it, and every test import died with `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX: TypeScript parameter property is not supported in strip-only mode`, stalling the feature
- Symptom: a file the gate accepted broke the whole suite at import time (the TAP shows every test file failing, not the edited one)
- Root cause: `node --check` validates JavaScript syntax after a lenient pass; the strip-only loader rejects non-erasable TypeScript (parameter properties, enums, namespaces) that `--check` does not model. The gate must use the same parser the loader uses
- Fix: the profile's `syntax_check_argv` runs `module.stripTypeScriptTypes(source, {mode: "strip"})` on the file (parses exactly as the loader will, executes nothing; a syntax error and a non-erasable construct both exit 1). The scripted store uses an explicit field; the scaffold's `tsconfig.json` already sets `erasableSyntaxOnly` so `tsc` agrees once installed
- Sweep: `grep -rn "constructor(" evals/product_scripts_node.py scio/product/scaffold.py` finds no other parameter property; the planner prompt v2 and ADR 0014 say "erasable syntax only". Guard: `test_cli_skeleton_runs_tests_and_lints_offline` feeds the gate a parameter property and expects a refusal
- Resolved: 2026-10-26 · Status: resolved

### I-084 · The local sandbox's `RLIMIT_AS` killed Node's TypeScript stripper on Linux: "Cannot allocate Wasm memory for new instance"
- Found: 2026-10-26 · M10 · PR #52's first CI run: the three Node tests (`test_scaffold_node_ts.py` twice, `test_same_spec_builds_in_both_stacks[node-ts]`) failed on `ubuntu-latest` with `RangeError: WebAssembly.Instance(): Out of memory` from `node:internal/deps/amaro` while every `.ts` file loaded; green on the Windows dev machine
- Symptom: the pattern of I-074 and I-080 once more (green here, red there), this time from a limit that exists only on POSIX
- Root cause: `LocalSandbox._preexec` set `RLIMIT_AS` to `Limits.memory_mb` (2 GB). That caps *virtual* address space; V8 reserves tens of gigabytes of it (pointer compression cage, Wasm memory) while using little real memory, so the stripper's Wasm instance could not be created. Windows has no `resource` module, so the dev machine never applied the limit
- Fix: the local sandbox keeps the CPU-time and wall-clock limits and drops `RLIMIT_AS`; `Limits.memory_mb` is enforced by the Docker backend (`--memory`), where it measures resident memory
- Sweep: `grep -rn "RLIMIT" scio` finds only `_preexec`; `docker.py` passes `--memory` (unchanged). Guard: the Node tests run in CI's `checks` job on Linux (`setup-node` 22), which is where the limit applied
- Resolved: 2026-10-26 · Status: resolved

### I-085 · The node-ts install phase leaves `package.json` modified and `package-lock.json` untracked in the product tree
- Found: 2026-10-26 · M10 live milestone (`notes_service`) · after `merge and stop`, `git status` in the product showed ` M package.json` and `?? package-lock.json`
- Symptom: a merged milestone with a dirty tree; the next feature's `git add -A` would commit the lockfile and the rewritten dependency range as part of an unrelated feature
- Root cause: `npm install --save <spec>` rewrites the dependency's range in `package.json` and writes the lockfile; the install phase runs as an orchestrator step outside any feature commit, and the Python profile never showed this because `pip` writes nothing into the tree. PRODUCT_MODE component 4 asks for a lockfile; nothing owns committing it
- Fix: `commit_install` in `scio/product/build.py` commits only the profile's `dependency_files` that the install left changed, as `chore: install approved dependencies [scio:install:<M>:<node>]`, a `run_effect` probed by the marker. `plan_milestone` checks out the new milestone branch, runs the install and commits before the first feature's pre-attempt reset (`git clean -fdq`, I-067), which would otherwise delete the untracked lockfile while `node_modules` still says "installed"; `integrate` commits again if a resumed install wrote anything. Dropping `--save` was ruled out by a probe (2026-10-26, npm 10.9.8): npm saves by default, and `--no-save` also skips the lockfile that PRODUCT_MODE component 4 asks for
- Sweep: every orchestrator step that writes into the product tree outside a feature commit: `grep -n "product_env(\|_write\|write_text" scio/product/build.py` finds `load`, `plan_milestone`, `feature` and `integrate` running the install (the first commits nothing, the next two now commit or reset, `integrate` commits), the issue files (committed with `[scio:issues:]`), the PR file and `update_progress`'s progress and notes (committed by their nodes), and one more instance: `load`'s `_write_progress` after `sync_features` (records added or removed since the last run) was never committed, so P1's reset reverted it and `update_progress` wrote the stale records back. `commit_progress_sync` commits it on the milestone branch with `[scio:sync:<M>:plan_milestone]`; `test_progress_sync_with_product_md_survives_the_first_feature_reset` fails without it (`'P3' in ['P1', 'P2']`). Writing the offline guard also found I-088. Guard: `test_install_phase_changes_are_committed_on_the_milestone_branch` approves a `file:` package for the node `todo_api`, so npm rewrites `package.json` and writes the lockfile with no registry; without the fix it fails with ` M package.json`, `?? package-lock.json`, the live symptom
- Resolved: 2026-10-26 · Status: resolved

### I-086 · Three finished milestones (M8, M9, M10) piled up under `[Unreleased]` behind v0.5.0 for two weeks
- Found: 2026-10-26 · after the PR #52 merge · the owner asked "what about releases"; `git tag` showed v0.5.0 (2026-10-12) as the newest while CHANGELOG `[Unreleased]` held M8, the Claude Code backend, M9 and M10. A local `release/0.6.0` branch from 2026-10-13 existed with no commit on it: a release was started after M8 and dropped
- Symptom: the README said "v0.5.0 released" next to four finished milestones; anyone installing the latest release got M7-era SCIO
- Root cause: nothing tied a release to a milestone merge. The release rule (IDEAS_TRIAGE item 1, the auto-version-control memory) said how to cut one, not when; each session ended at "PR ready, awaiting merge" and the next session started the next milestone, so the cut never had a moment of its own
- Fix: v0.6.0 cut (PR `release/0.6.0`, tag after merge); CONTRIBUTING workflow step 6: a release PR follows every milestone merge; the stale empty branch deleted (`git branch -d`, nothing unmerged)
- Sweep: `git tag` against the CHANGELOG sections and `git branch --list 'release/*'` locally and on origin: v0.1.0 to v0.5.0 each have a section and a tag; the only stale release branch was the empty `release/0.6.0` (origin also keeps a merged `release/0.3.0`, harmless). Guard: `check_release_cadence` in `scripts/check_docs.py` fails when two or more milestones that PRODUCT_MODE.md calls done are named only under `[Unreleased]`; `test_milestones_done_but_only_unreleased_are_reported` replays the pre-0.6.0 changelog, and `test_a_milestone_already_released_is_not_pending_again` checks that one pending milestone passes
- Resolved: 2026-10-26 · Status: resolved

### I-087 · Acted on "PR #52 merged" without checking GitHub, and deleted the local branch of an unmerged PR
- Found: 2026-10-26 · M11 start · the owner said "merge 52 is done"; the next command chained `gh pr view 52` (it printed `OPEN`) into `git branch -d feat/51-m10-node-ts` with `&&`, which only needs the view to succeed, not the PR to be merged; git warned "not yet merged to HEAD" and deleted it
- Symptom: the M10 branch gone locally while its PR waited on the required `checks` job; restored from `origin` at the same commit, nothing lost. The owner said "merged now" a second time while GitHub still showed `OPEN`, `BLOCKED`
- Root cause: a human's "merged" was treated as a fact about GitHub, and a status query sat in an `&&` chain as if its output were an exit code (the I-018 pattern: a chain that reaches a destructive step must have been able to fail)
- Fix: branch clean-up runs only after `gh pr view N --json mergedAt` is non-null, checked in its own step; the actual merge happened at 23:32 (`b97d942`) after CI went green
- Sweep: the same "printed state, ignored exit" shape in this session's other chains: the CI-equivalent chain uses exit codes only; the wait loop compared `.state` to `MERGED` explicitly. Guard: CONTRIBUTING workflow step 5 (confirm `mergedAt` before any post-merge step); the merge-permission memory carries the same rule
- Resolved: 2026-10-26 · Status: resolved

### I-088 · The node scaffold wrote a non-registry dependency into `package.json` as a package named `file:vendor/greet`
- Found: 2026-10-26 · M11 slice 1 · the first run of the I-085 guard test: `npm error code EINVALIDPACKAGENAME: Invalid package name "file:vendor/greet" of package "file:vendor/greet@*"`
- Symptom: any approved spec that is not `name[@range]` (`file:`, `github:`, a URL, a path) produced an invalid `package.json` at bootstrap; the scaffold's own lint and `node --test` do not read `dependencies`, so the bootstrap passed and the install phase failed later
- Root cause: `_npm_dependency` in `scio/product/scaffold.py` split every spec at `@` into a name and a range; the same assumption in `normalise_package` (`scio/aci/runtime.py`) turned `file:vendor/greet` into the name `file`, so approving that spec would have let `install_deps` install any package called `file`
- Fix: `_npm_dependency` returns None for a non-registry spec and the scaffold leaves it to the install phase, which writes the right name from the target's own manifest; `normalise_package` returns such a spec unchanged, so it only matches itself
- Sweep: `grep -rn "partition(\"@\")\|split(\"@\"" scio` finds the two parsers above and nothing else; the python scaffold writes specs verbatim into `pyproject.toml` (PEP 508 already allows `name @ url`). Guard: the I-085 test bootstraps with a `file:` spec, so both parsers are exercised end to end
- Resolved: 2026-10-26 · Status: resolved

### I-089 · Merging `main` across the 0.6.0 cut moved the branch's unreleased changelog entries into the released `[0.6.0]` section, without a conflict
- Found: 2026-10-27 · M11 slice 1 · `git merge origin/main` into `feat/54-m11-product-ready` after PR #55: the branch had added two `### Fixed` entries under `[Unreleased]`; the release had inserted `## [0.6.0]` between `[Unreleased]` and that `### Fixed` list, so git's merge placed the entries under `[0.6.0]`
- Symptom: none from git (the changelog auto-merged; only the ledger conflicted); `grep -n "^## \[\|^### "` after the merge showed the I-085 and I-088 lines inside `[0.6.0]`, which v0.6.0 never shipped
- Root cause: `release_notes.py --cut` renames the `[Unreleased]` header in place and opens a new empty one above it, so every entry list a branch extended now sits under the version header; a text merge has no idea which section a line belongs to
- Fix: the two entries moved back under `[Unreleased]` by hand in the merge commit
- Sweep: the other files the cut touches (`pyproject.toml`, `scio/__init__.py`, `uv.lock`, the README status line) are whole-line values a merge either takes or conflicts on; only the changelog has lists that grow under a header. Guard: `check_released_section_frozen` in `scripts/check_docs.py` compares the newest released section with `git show v<version>:CHANGELOG.md` and fails on any difference (skipped before the tag exists and in a clone without tags); replayed on git's real auto-merge result (`git merge-tree b1efbfc 8bd6e38`) it reports the change, on the fixed branch it passes; `test_an_entry_merged_into_a_released_section_is_caught`
- Resolved: 2026-10-27 · Status: resolved

### I-090 · Three command slips during M11 slice 1, each caught by the tool on the first run
- Found: 2026-10-26 and 2026-10-27 · M11 start and slice 1
- Symptom: (1) `gh release view --json isLatest`: "Unknown JSON field"; (2) a test used git's `:/<text>` commit lookup with `[scio:install:...]`, which git reads as a regex character class; (3) two test lines over the 100-character limit (ruff E501)
- Root cause: (1) a field remembered from `gh release list`, not checked against `gh release view`'s list; (2) the bracketed SCIO markers are regex metacharacters everywhere git takes a pattern; (3) long literal strings in tests
- Fix: (1) `gh release list` shows the Latest flag; (2) the test finds the commit with `git log --fixed-strings --grep=` (the form `find_commit` uses); (3) the strings were split
- Sweep: `grep -rn ':/\[' scio evals tests` finds no other `:/` lookup with a marker. `grep -rn -- "--grep=" scio` found a real instance of (2) in production: the bootstrap commit's crash-resume probe in `scio/product/graph.py` ran `git log --grep=[scio:bootstrap]` without `--fixed-strings`, a character class that matches nearly any message (in a scratch repo it returned `docs: add readme`), so `scio new` resumed in a directory with history could take an unrelated commit for the bootstrap; it calls `find_commit` now. Guard: ruff and the gh error caught (1) and (3) at once; `test_every_git_grep_for_a_marker_is_a_fixed_string` fails on any `--grep=` in `scio/` without `--fixed-strings`
- Resolved: 2026-10-27 · Status: resolved

### I-091 · With tests excluded from coverage, every Fake product misses the 80% floor, and nothing before `integrate` can see or fix it
- Found: 2026-10-27 · M11 slice 2 · making the floor measure the product's own code (python scaffold `[tool.coverage.run] source`, node `--test-coverage-exclude=tests/**` plus Node's built-in coverage as a new check) failed four product builds at `integrate`: python `todo_api` 78.57% (`__main__.py` 0%, `cli.py` argument branches), node `todo_api` 33.33% (`cli.ts` 30%, `store.ts` 31%)
- Symptom: `integration checks on milestone/1: FAILED` after both features passed their own verify; the milestone halts with `integration_failed` and no node in the loop can repair it
- Root cause: two things. (1) The floor was inflated before M11: `--cov` with no source counted the test files, and node had no coverage check at all, so "coverage passed" in the M9 live milestone measured tests too. (2) Coverage runs only in `integrate`; the coder's tools (`run_tests`, `edit_file`) and verify never run it, so a feature cannot see the floor it will be judged by (the t008/t009 lesson: a failure outside the worker's tools needs a path that reports it to the loop). The scaffold's and the scripted features' tests reach the code through a child process (`startServer` spawns `node`), which in-process coverage does not count (probed: Node's runner reports 33% for code only a child ran)
- Fix: the honest measure and the 80% floor stay (`Settings.coverage_floor`, filled into `{coverage_floor}` in the profile's argv). (a) Both scaffolds start above it: node's `src/cli.ts` exports `createApp()` and the smoke tests listen it on port 0 in-process; python's smoke tests drive the `Handler` on a thread; the process lifecycle (`serve`, the `serve` branch of `main`) carries the stack's ignore marker (`# pragma: no cover`, `/* node:coverage disable */`, both probed), and python omits the two-line `__main__.py`. (b) A quality check marked `verify: true` (coverage in both stacks, `typecheck` in node) runs in every product feature's verify once the suite and lint pass (`RunContext.quality_values`, set by `feature_context`); a miss is the new failure kind `quality`, on the never-escalate list, routed to the debugger with the check's output and a hint about in-process tests. (c) The Fake node scripts test the app in-process (`createApp(store)`), and the python P2 script carries `debug_edits` that add the error-path tests
- Sweep: every check that only `integrate` ran: `typecheck` has the same gap (Node strips types, so only `tsc` sees a type error) and is now `verify: true`; `build` and `audit` stay milestone-level (a build or a dependency audit is not a per-feature fix, and both are "skipped: tool absent" until a product installs its tools). The live products `bookmarks_api` and `notes_service` predate the coverage config and the in-process tests, so their next `scio build` meets the honest floor (CHANGELOG notes it). Guards: `test_a_fresh_scaffold_meets_the_coverage_floor` (both stacks, cli and http); `test_a_feature_below_the_coverage_floor_is_fixed_inside_its_own_run` (floor 95, P2 at 93%: `verify: quality`, `triage -> debug`, the debugger's tests, P2 passes in 2 iterations; without the verify step the milestone halts at `integrate`); `test_a_missed_product_quality_check_never_escalates_even_with_signals`
- Resolved: 2026-10-27 · Status: resolved
