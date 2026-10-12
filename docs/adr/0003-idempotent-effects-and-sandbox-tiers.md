# ADR 0003: Idempotent side effects and sandbox tiers

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M2

## Context
LangGraph checkpoints state between nodes. A crash inside a node re-runs that node from its start on resume, so a file write, git commit or PR creation inside it would happen twice. The spec requires "crash mid-run resumes from checkpoint with zero duplicated side effects" and "every side effect inside an idempotent task keyed by hash(thread_id, node, intent)". Separately, agent-driven code must run somewhere isolated, but the project is developed on machines without Docker and CI runs on GitHub-hosted runners.

## Decisions
1. **Two-layer idempotency, not LangGraph `@task` alone.** `scio.graph.effects.run_effect` keys each effect by `hash(thread_id, node, intent)` and records `started` and `done` (with the JSON result) in an SQLite effect log that outlives the process. A resumed node returns the stored result. For the window between applying the effect and recording `done`, the caller passes a `probe` that detects the effect from the world (the commit whose message carries the key, the open PR for the branch, the file hash). LangGraph's `@task` caches results only inside the checkpoint and cannot see that window, so it is not relied on for correctness; nodes may still use it for parallelism.
2. **Effects carry their key into the world.** Commit messages made by `git_checkpoint` under an effect include `[scio:<key>]`; issue comments carry `<!-- marker -->`; PRs are looked up by head branch. This is what makes the probes cheap and exact.
3. **`durability="sync"` on every run.** State is persisted before the next node starts. The cost is one checkpoint write per node, which is negligible next to a model call.
4. **Three sandbox tiers behind one protocol.** `LocalSandbox` for tests and development (wall-clock and POSIX resource limits; cannot disable the network and logs a warning), `DockerSandbox` for local isolation (`--network none`, `--cpus`, `--memory`, `--pids-limit`, `no-new-privileges`), `E2BSandbox` for CI and production (gVisor microVM, internet off unless requested). Backends that need a daemon or an API key are unit-tested through injected runners or clients; a live run is a separate, gated test.
5. **Tools reject before they act.** `edit_file` parses Python with `ast` before writing and reverts on a lint failure; `view_file` and `repo_search` require absolute paths under the sandbox root; `git_push` refuses the default branch. The capability check runs when a `ToolBox` is built, so an unsafe toolset never reaches a model.

## Consequences
- Every node that has a side effect must name an `intent` and, where the effect is externally visible, supply a `probe`. M3 node templates make this the default shape.
- The effect log is a second durable store next to the checkpointer. Both live under `.scio/` in dev; in prod the log can move to Postgres with the same schema (one table).
- Docker correctness is verified live in CI on every PR (`tests/live/test_docker_live.py`, ledger I-016). E2B correctness is verified by call construction only: the project has no E2B account and will not open one (ledger I-029, wontfix). The local backend is the default for the project's own eval fixtures; Docker is the tier for untrusted repositories.
