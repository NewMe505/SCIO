# Contributing

These rules apply to human contributors and to SCIO's own agents.

## Workflow
1. **Start from an issue.** Use one of the issue templates (feature, bug, chore).
2. **Branch:** `agent/<issue#>-<slug>` for agent work, `<type>/<issue#>-<slug>` for humans. Never push to `main` directly; never force-push. A ruleset on `main` enforces this: pull request required, the `checks` workflow must pass, no force-pushes, no deletion.
3. **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`). One commit per passing feature.
4. **Pull request:** open it as a draft using the PR template. Link the issue with `Closes #N`. It becomes ready for review only after all checks pass (and, for agent runs, after the Opus gate passes).
5. **Merge:** always approved by a human. Before any post-merge step (deleting a branch, branching from `main`, tagging), confirm on GitHub that the PR is merged: `gh pr view N --json mergedAt` must be non-null, in its own step, not inside an `&&` chain (I-087).
6. **Release after every milestone:** when a milestone PR merges, the next PR is the release: `python scripts/release_notes.py --cut X.Y.Z`, the version in `pyproject.toml`, `scio/__init__.py` and `uv.lock`, the README status line; after its merge, tag `vX.Y.Z` on the merge commit and push the tag. `check_release_cadence` fails when two finished milestones wait unreleased (I-086).

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
- Git in tests sees what a CI runner sees: `tests/conftest.py` empties the global config and forbids a guessed identity, so a commit that names no identity fails locally as on CI. SCIO's own commits pass `SCIO_GIT_IDENTITY`; a test helper that commits passes `-c user.name=... -c user.email=...` (I-104).
- Before pushing the next commit to a branch, confirm the previous push's CI finished green (`gh run list --branch <branch> --limit 3 --json headSha,conclusion`); a watcher on the newest run alone misses an older red one (I-104). For AI sessions the push-gate hook enforces it (`scripts/hooks/push_gate.py`, wired in `.claude/settings.json`): a `git push` is denied while the branch's latest run is red or still running; to push the fix for a red run, prefix the push with `SCIO_ACK_CI=<run id>`. The same hook refuses a bare `git stash`, because parallel sessions share the stash stack. It also holds a `git commit` until the changed Python files pass `ruff check` and `ruff format --check` (I-106).
- Both CI jobs have a Docker daemon, so the docker-live tests also run in `checks` on the plain Python image; a test that needs the built sandbox image skips there only when `SCIO_SANDBOX_DOCKER_IMAGE` is unset (I-105).

## Continuous improvement protocols
These five protocols are mandatory for humans and agents. `CLAUDE.md` restates them for AI sessions.

1. **Issue ledger** ([docs/ISSUE_LEDGER.md](docs/ISSUE_LEDGER.md)). Every problem found while working (failing check, wrong assumption, tooling failure, environment quirk) gets an entry *when it is found*, and the entry is completed *when it is resolved*: symptom, root cause, fix, the sweep performed, status. A PR that fixed a problem links its ledger ids.
2. **Milestone retrospectives** ([docs/LESSONS_LEARNED.md](docs/LESSONS_LEARNED.md)). At the end of every milestone, record what succeeded, what failed, and the concrete actions taken. Read the whole document before starting the next milestone and cite the lessons that shaped the plan.
3. **Systemic bug sweeps.** A bug is never fixed in isolation. After the root cause is known, search the entire codebase for the same construct (state the grep or pattern in the ledger entry), fix every instance, and add a guard that prevents recurrence (a test, a lint rule, a docs check, or a CONTRIBUTING rule). "Patched the symptom" is not a valid resolution.
4. **Always/Never guardrails** ([docs/LESSONS_LEARNED.md](docs/LESSONS_LEARNED.md), `## Guardrails`). Every bug and every correction becomes one rule that starts with **Always** or **Never**, cites its id, and names the guard that fails when the rule is broken: a structural fix, a regression test that failed on the old code, a scan of the codebase, a docs check or a hook; `manual` with the reason only when nothing can check it. `scripts/check_docs.py::check_guardrails` enforces both halves: every resolved ledger entry from I-104 on is cited, and every named guard exists.
5. **Bottleneck audit after every pass.** After each slice, milestone or live run, record the slowest steps (minutes), sequential work that could have run in parallel, repeated friction, and one or two concrete improvements (a file change or a ledger id), in the status report and the retrospective.

## Concurrent sessions
- One session (human or agent) per working checkout. A second concurrent session works in its own `git worktree add` directory (ledger I-058).
- Every scripted commit starts with a branch assertion, e.g. `test "$(git branch --show-current)" = "docs/34-product-mode" && git commit ...`, so a checkout switched by someone else fails loudly instead of committing to the wrong branch.

## File hygiene
- Line endings are LF everywhere (`.gitattributes` enforces it). When writing files from Python on Windows, pass `newline="\n"`; ledger entry I-006 explains why.
- Model IDs live only in `configs/models.yaml`; `scripts/check_docs.py` fails on any other occurrence.
