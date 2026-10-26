# SCIO: working rules for AI sessions

SCIO is a model-agnostic LangGraph framework that takes a software task from spec to a merged, tested, documented PR. Spec: `docs/PROMPT.md`. Design: `docs/ARCHITECTURE.md`. Evidence: `docs/RESEARCH.md`. Decisions: `docs/adr/`. Build it milestone by milestone (M0 to M5 in ARCHITECTURE.md section e); each milestone is its own branch, issue and draft PR.

## Continuous improvement protocols (mandatory)

1. **Issue ledger: `docs/ISSUE_LEDGER.md`.** The moment a problem appears (failed command, failing test, wrong assumption, environment quirk, rejected push), add an entry with the next `I-NNN` id. When it is resolved, complete the entry: root cause, fix, sweep, status. Never leave a problem out because it was small or quickly fixed.
2. **Milestone retrospectives: `docs/LESSONS_LEARNED.md`.** Before starting a milestone, read this file end to end and say which lessons shape the plan. At the end of a milestone, add a section: what succeeded, what failed, actions taken (each action is a file change, a test, a rule, or a ledger id, not an intention).
3. **Systemic bug sweeps.** A bug fix has three parts: the root cause, a search of the whole codebase for the same construct (record the grep or pattern in the ledger entry), and a guard against recurrence (test, lint rule, docs check, or CONTRIBUTING rule). Fix every instance found. Do not patch only the file where the symptom showed.

## Engineering rules

- Workflow and commits follow `CONTRIBUTING.md`: issue first, branch `feat/<issue#>-<slug>`, Conventional Commits, draft PR from the template with `Closes #N`, docs in the same PR, human merge.
- Run the CI-equivalent locally before every push (in one shell command, start with `set -o pipefail`; never pipe a check through `tail` without it): `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run python -m pytest && uv run python scripts/check_docs.py && uv run python -m evals.runner --suite regression --provider fake --trials 5 --min-pass-pow-k 1.0`. Always the `python -m` form for pytest and scio: Windows Application Control blocks the venv's `pytest.exe` and `scio.exe` launchers on this machine (I-075), so `uv run python -m scio ...` as well.
- Offline by default: `SCIO_PROVIDER_OVERRIDE=fake`. Live provider calls only in `tests/live` behind `SCIO_RUN_LIVE_TESTS=1`, or in explicit eval runs.
- Live credentials come from the `ant auth login` profile (the CLI is installed under `%LOCALAPPDATA%\Programs\ant`). The session may carry a stale placeholder `ANTHROPIC_API_KEY`; run live commands with `env -u ANTHROPIC_API_KEY -u SCIO_PROVIDER_OVERRIDE` so the profile wins. Live runs cost real money: run one trial first, report the cost, then scale. The profile's refresh token rotates: all providers share one SDK client per process (`shared_client`), and an `invalid_grant` error means the profile is dead and the user must run `ant auth login` again.
- Model IDs live only in `configs/models.yaml`. Code names roles. Check the Claude API reference (the `claude-api` skill) before writing any provider code; never write SDK calls from memory.
- Write files with LF line endings (`newline="\n"` in Python on Windows). Any text with a backslash (Windows paths, regexes, escapes) is written with the file tools, never through a shell-embedded script. Behind a corporate proxy, set `UV_SYSTEM_CERTS=1` for every `uv` command (`--native-tls` and `UV_NATIVE_TLS` are deprecated spellings).
- Parallel sessions follow the orchestration rules below. That is about how AI sessions build SCIO; the framework's own runtime keeps one writer per product (ARCHITECTURE.md), which these rules do not change.
- Tests, lint and types are ground truth. Do not weaken a check to make it pass; fix the code or record a ledger entry explaining the exception.

## Existing Codebase Parallel Orchestration Rules (15-Agent Pool)

- **State-First Inspection:** Before spawning agents, inspect the current file tree and state tracker. Never recreate existing modules or duplicate completed work. Here the state trackers are `git status`, the open PRs, `docs/ISSUE_LEDGER.md` and the milestone status in `docs/PRODUCT_MODE.md`.
- **Concurrency Ceiling:** Dispatch up to **15 concurrent sub-agents** per execution wave for any tasks that do not share direct data or file dependencies.
- **Strict File Isolation (Anti-Collision):** In an existing codebase, every parallel sub-agent must be assigned an explicit list of `owned_files`. An agent may read any file in the repository for context, but may ONLY modify its assigned `owned_files`. Shared files (`docs/ISSUE_LEDGER.md`, `docs/LESSONS_LEARNED.md`, `CHANGELOG.md`, `README.md`, `pyproject.toml`, `uv.lock`) are owned by the orchestrator, which writes them after the wave from the agents' reports.
- **Shared State & Contracts:** If multiple modules depend on a shared state machine, base class, or schema, update the shared contract FIRST in a single-agent step, then fan out up to 15 parallel sub-agents to update or build the downstream modules simultaneously.
- **Verification Gate:** After each parallel wave resolves, run syntax/import checks across all modified files before triggering the next wave (`uv run ruff check`, `uv run mypy`, then the affected tests). Only the orchestrator runs git (stage, commit, push) and the full CI-equivalent before a push; sub-agents never commit.
