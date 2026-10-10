# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [Unreleased]
### Added
- Harness layer (`scio/harness`): `ModelProvider` protocol; Anthropic, OpenAI and deterministic Fake adapters; single retry layer with backoff and `retry-after`; `BudgetGuard` checked before every call; per-call cost ledger; prompt-cache breakpoint placement; OpenTelemetry GenAI spans with opt-in content capture.
- Prompt layer (`scio/prompt`): versioned per-role templates (12 roles at v1); Pydantic → provider-safe wire schema; structured-output validate-and-repair loop (max 2 repairs).
- `configs/models.yaml` (the only place model IDs live) and `configs/budgets.yaml` (per-tier budgets).
- Eval skeleton (`evals/`): task loader, n-trial runner, pass@1 / pass^k / tokens / $ / latency / advisor-rate metrics, Markdown and JSON reports, three placeholder regression tasks.
- CI (`.github/workflows/ci.yml`): ruff, mypy, pytest on the Fake provider, docs checks, regression evals, API docs artifact. `scripts/check_docs.py`, `scripts/gen_api_docs.py`.
- ADR 0002: harness conventions.
- Build spec and generation prompt (`docs/PROMPT.md`).
- Research basis with sources (`docs/RESEARCH.md`).
- ADR 0001: single writer, Sonnet 5 worker, Opus 5.5 planner/advisor.
- Repository docs: README, CONTRIBUTING, issue and PR templates.
- Milestone 0 architecture (`docs/ARCHITECTURE.md`): graphs per tier, State schema, agent contracts, directory tree, milestone plan, assumptions and open questions.
