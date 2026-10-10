# SCIO: working rules for AI sessions

SCIO is a model-agnostic LangGraph framework that takes a software task from spec to a merged, tested, documented PR. Spec: `docs/PROMPT.md`. Design: `docs/ARCHITECTURE.md`. Evidence: `docs/RESEARCH.md`. Decisions: `docs/adr/`. Build it milestone by milestone (M0 to M5 in ARCHITECTURE.md section e); each milestone is its own branch, issue and draft PR.

## Continuous improvement protocols (mandatory)

1. **Issue ledger: `docs/ISSUE_LEDGER.md`.** The moment a problem appears (failed command, failing test, wrong assumption, environment quirk, rejected push), add an entry with the next `I-NNN` id. When it is resolved, complete the entry: root cause, fix, sweep, status. Never leave a problem out because it was small or quickly fixed.
2. **Milestone retrospectives: `docs/LESSONS_LEARNED.md`.** Before starting a milestone, read this file end to end and say which lessons shape the plan. At the end of a milestone, add a section: what succeeded, what failed, actions taken (each action is a file change, a test, a rule, or a ledger id, not an intention).
3. **Systemic bug sweeps.** A bug fix has three parts: the root cause, a search of the whole codebase for the same construct (record the grep or pattern in the ledger entry), and a guard against recurrence (test, lint rule, docs check, or CONTRIBUTING rule). Fix every instance found. Do not patch only the file where the symptom showed.

## Engineering rules

- Workflow and commits follow `CONTRIBUTING.md`: issue first, branch `feat/<issue#>-<slug>`, Conventional Commits, draft PR from the template with `Closes #N`, docs in the same PR, human merge.
- Run the CI-equivalent locally before every push: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest && uv run python scripts/check_docs.py && uv run python -m evals.runner --suite regression --provider fake --trials 5 --min-pass-pow-k 1.0`.
- Offline by default: `SCIO_PROVIDER_OVERRIDE=fake`. Live provider calls only in `tests/live` behind `SCIO_RUN_LIVE_TESTS=1`.
- Model IDs live only in `configs/models.yaml`. Code names roles. Check the Claude API reference (the `claude-api` skill) before writing any provider code; never write SDK calls from memory.
- Write files with LF line endings (`newline="\n"` in Python on Windows). Behind a corporate proxy, set `UV_SYSTEM_CERTS=1` for every `uv` command (`--native-tls` and `UV_NATIVE_TLS` are deprecated spellings).
- One writer: never run two agents that edit code concurrently. Parallel work is read-only.
- Tests, lint and types are ground truth. Do not weaken a check to make it pass; fix the code or record a ledger entry explaining the exception.
