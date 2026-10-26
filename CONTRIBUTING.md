# Contributing

These rules apply to human contributors and to SCIO's own agents.

## Workflow
1. **Start from an issue.** Use one of the issue templates (feature, bug, chore).
2. **Branch:** `agent/<issue#>-<slug>` for agent work, `<type>/<issue#>-<slug>` for humans. Never push to `main` directly; never force-push. A ruleset on `main` enforces this: pull request required, the `checks` workflow must pass, no force-pushes, no deletion.
3. **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`). One commit per passing feature.
4. **Pull request:** open it as a draft using the PR template. Link the issue with `Closes #N`. It becomes ready for review only after all checks pass (and, for agent runs, after the Opus gate passes).
5. **Merge:** always approved by a human.

## Docs-as-code
Docs change **in the same PR** as the code they describe:
- `README.md` when usage changes.
- `docs/ARCHITECTURE.md` and its Mermaid diagrams when the graph changes.
- `docs/adr/NNNN-title.md` for any architectural decision, replan, or high-impact change.
- `CHANGELOG.md` under `[Unreleased]`.
- Docstrings for every public API.

CI checks docs: links, runnable examples, architecture diagram vs. the compiled graph, and public-API doc coverage. A docs failure blocks merge just like a test failure.

## Required checks (every PR)
- lint
- type-check
- tests (Fake provider, offline)
- docs checks
- regression eval suite

## The landing page
- `README.md` says where the project stands at every push: its `> Status:` line names the latest released version and the milestone in progress, the milestone table marks what is done, and the ADR list names the newest ADR. `scripts/check_docs.py::check_readme_status` fails the pre-push chain and CI when it is stale (owner's rule, 2026-10-26).

## Tests and the environment
- `Settings` is environment-backed. A test of a `Settings` field constructs `Settings(field=...)` explicitly and never relies on the default: CI exports values (`SCIO_PROVIDER_OVERRIDE=fake`, `SCIO_PRODUCT_SANDBOX_BACKEND=local`) that a bare `Settings()` inherits (I-080).
- A test that depends on a tool being importable asserts where it comes from (`__file__` under the expected environment), not only that it imports; the dev machine's global packages are not the runner's (I-074).
- Run the suite with `uv run python -m pytest`: some machines' application-control policies block the venv's `pytest.exe` launcher (I-075).

## Continuous improvement protocols
These three protocols are mandatory for humans and agents. `CLAUDE.md` restates them for AI sessions.

1. **Issue ledger** ([docs/ISSUE_LEDGER.md](docs/ISSUE_LEDGER.md)). Every problem found while working (failing check, wrong assumption, tooling failure, environment quirk) gets an entry *when it is found*, and the entry is completed *when it is resolved*: symptom, root cause, fix, the sweep performed, status. A PR that fixed a problem links its ledger ids.
2. **Milestone retrospectives** ([docs/LESSONS_LEARNED.md](docs/LESSONS_LEARNED.md)). At the end of every milestone, record what succeeded, what failed, and the concrete actions taken. Read the whole document before starting the next milestone and cite the lessons that shaped the plan.
3. **Systemic bug sweeps.** A bug is never fixed in isolation. After the root cause is known, search the entire codebase for the same construct (state the grep or pattern in the ledger entry), fix every instance, and add a guard that prevents recurrence (a test, a lint rule, a docs check, or a CONTRIBUTING rule). "Patched the symptom" is not a valid resolution.

## Concurrent sessions
- One session (human or agent) per working checkout. A second concurrent session works in its own `git worktree add` directory (ledger I-058).
- Every scripted commit starts with a branch assertion, e.g. `test "$(git branch --show-current)" = "docs/34-product-mode" && git commit ...`, so a checkout switched by someone else fails loudly instead of committing to the wrong branch.

## File hygiene
- Line endings are LF everywhere (`.gitattributes` enforces it). When writing files from Python on Windows, pass `newline="\n"`; ledger entry I-006 explains why.
- Model IDs live only in `configs/models.yaml`; `scripts/check_docs.py` fails on any other occurrence.
