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
- Fix: none yet (cosmetic)
- Sweep: only `scio/prompt/repair.py` uses PEP 695 generics; if the warning matters, drop the `__future__` import in that module
- Resolved: — · Status: open (low)

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
- Fix: pending. Needs one run of a gated live test on a machine with Docker, and one with `E2B_API_KEY`
- Sweep: n/a
- Resolved: — · Status: open

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

### I-009 · Branch protection unavailable on a private free-plan repository
- Found: 2026-10-10 · M0 · `gh api repos/.../rulesets`
- Symptom: `Upgrade to GitHub Pro or make this repository public to enable this feature` (403)
- Root cause: rulesets and branch protection on private repos require GitHub Pro
- Fix: none; the no-direct-push and no-force-push rules are convention (CONTRIBUTING) until the repo is public or the plan changes
- Sweep: n/a
- Resolved: — · Status: open (owner decision)
