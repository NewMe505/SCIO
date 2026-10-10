# Contributing

These rules apply to human contributors and to SCIO's own agents.

## Workflow
1. **Start from an issue.** Use one of the issue templates (feature, bug, chore).
2. **Branch:** `agent/<issue#>-<slug>` for agent work, `<type>/<issue#>-<slug>` for humans. Never push to `main` directly; never force-push.
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
