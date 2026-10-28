# ADR 0017: A live, read-only dashboard: `scio dashboard` over the checkpoint DBs, local only, plain HTML/JS

- **Status:** Accepted
- **Date:** 2026-10-27
- **Issue:** #68

## Context
The owner watches SCIO through CLI output (`scio status`, `scio report`), files (`RUN_LOG.md`) and GitHub. None of these shows SCIO's own state machine as it moves. A product build such as the graphcalc run has several threads at once: the build thread and one per feature attempt. Stops wait for the owner, and cost accrues across all of them. To see which step each thread is on, the owner has to run `scio report` thread by thread.

Everything a view needs is already on disk and typed:
- each thread's latest state, its pending stop and its timeline are in the LangGraph checkpoint DB (`<root>/.scio/checkpoints.sqlite`);
- costs are in the cost ledgers (`<root>/.scio/ledger/<thread>.jsonl`);
- feature status is in the product's `.scio/product/progress.json`;
- the report models (`ProductReport`, `BootstrapReport`, `RunReport`) already turn all three kinds of thread into JSON.

The constraint is that the dashboard must never disturb a live build. The build is a separate process and the only writer of its checkpoint DB, ledgers and product repository. The owner chose a view-only dashboard with a plain HTML/JS page.

The shared shapes are fixed in [`scio/dashboard/contract.py`](../../scio/dashboard/contract.py): the response models, the `DataSource` protocol and the HTTP surface. Example payloads in `tests/fixtures/dashboard/` validate against them.

## Decision
1. **Read-only by construction.** Reading is the only thing the code can do, not a convention it follows.
   - The data layer opens each checkpoint DB with a `file:...?mode=ro` URI. It wraps that connection in LangGraph's `SqliteSaver` and skips `SqliteSaver.setup()`, which would run a PRAGMA and `CREATE TABLE` statements. A write through the connection raises.
   - There is no write route. Every HTTP route is `GET` or `HEAD`, plus the `/ws` WebSocket, which only pushes. A test (`tests/unit/test_dashboard_app.py`) asserts that every HTTP route's methods are within `GET` and `HEAD`, so a write route cannot be added by accident.
   - It runs beside a live build without touching the build's files. One measured caveat: on a DB with no open writer, a `mode=ro` reader leaves empty `-wal` and `-shm` sidecar files. These are SQLite's WAL index, and the next writer cleans them up. The main DB file stays byte-identical. A test (`tests/unit/test_dashboard_data.py`) hashes it before and after the reads.
   - Each read opens its own connection and closes it before returning (the `DataSource` contract). A read transaction is never held across polls, so it never blocks the writer's WAL checkpoint and the `-wal` file cannot grow because of the dashboard.
2. **Local only.** The page shows prompts, stop questions, costs and paths, so it must not be reachable from outside the machine.
   - `scio dashboard --root DIR [--root DIR ...] [--port 8765]` binds `127.0.0.1` and prints the URL.
   - The `Host` header is checked against `127.0.0.1` and `localhost`. Binding to loopback does not stop DNS rebinding, where a foreign page resolves its own name to 127.0.0.1. The Host check does.
   - The WebSocket refuses a foreign `Origin`. Browsers do not apply the same-origin policy to WebSockets, so any page could otherwise open `/ws`.
3. **An optional extra.** `dashboard` installs FastAPI and uvicorn (`uv sync --extra dashboard`). The core install is unchanged: only the `scio dashboard` command imports FastAPI and uvicorn, so the rest of SCIO runs without them.
4. **A plain HTML/JS page with no build step.** `scio/dashboard/static/` holds `index.html`, `app.js` and `style.css`, served as they are. The owner chose view-only and plain JS. With no bundler, no `node_modules` and no second toolchain in the repository, the page can be read and changed with a text editor. Mermaid, which draws the graphs, comes from cdnjs at a pinned version. When it cannot load, the page falls back to text, so the dashboard still works offline. The cost bars are inline SVG.
5. **Reuse, not a second model.** The dashboard builds its views with the same functions as `scio report`: `build_product_report`, `build_bootstrap_report` and `build_report`, over `CostLedger.load_entries`. It classifies threads by their checkpoint channels, never by name, under the same rules as `scio report` (I-066, I-108):
   - `feature_runs` or `product_dir` means a build;
   - `target_dir` means a bootstrap (`scio new`);
   - anything else is a task.

   So the dashboard and `scio report` print the same costs, and a fix to one fixes both. The graph diagrams come from `scio.graph.registry.GRAPHS` through `render_mermaid`, the same source as ARCHITECTURE.md's generated sections.
6. **Change detection by polling.** Once a second the server reads each thread's `ThreadVersion` from each root:
   - its latest checkpoint id;
   - its count of `writes` rows (a stop arrives as a pending write on an existing checkpoint, and so do a crash and a parallel task's progress, so the checkpoint id alone misses them);
   - its ledger file's size.

   This is two grouped queries per root and one `stat` per ledger. When a version differs from the last poll, the server pushes a `ChangeEvent` (`{"changed": [...], "removed": [...], "at": ...}`) on `/ws`. The page refetches the threads list, and the open thread when it changed or is the parent build of a thread that changed. No hook into the running build exists or is needed, because the build is a separate process that SCIO does not ask to publish anything.
7. **Crashed is a status.** A pending `__error__` write on the latest checkpoint means the process died in a node; a resume clears it. The data layer derives a thread's status once, in this order: `error`, `crashed`, `waiting`, `halted`, `done`, `in_progress` (`ThreadStatus` in the contract). The API and the page never re-derive it.
8. **Out of scope, left for a v2.**
   - Answering stops from the page. That would make the dashboard a writer of the checkpoint DB beside the build, and SCIO keeps one writer per product ([ARCHITECTURE.md](../ARCHITECTURE.md)). Stops are answered with `scio build --decision` or `scio new --decision`, as before.
   - Postgres checkpoints (`postgresql://` in `SCIO_CHECKPOINT_DSN`). A root is a directory with a SQLite checkpoint DB.

## Consequences
- Tests: `tests/unit/test_dashboard_contract.py` (the fixtures validate against the contract), `tests/unit/test_dashboard_data.py` (checkpoints from the Fake build for the three kinds of thread, the `merge_wait` stop, costs equal to the ledger sums, the DB byte-identical after reads, a write raising), `tests/unit/test_dashboard_app.py` (every endpoint against a fake `DataSource` built from the fixtures, the GET/HEAD-only rule, a `changed` push after a version bump), `tests/unit/test_dashboard_cli.py` (loopback only, no host option, a clear message without the extra) and `tests/live/test_dashboard_page.py` (Playwright against the fixtures, skipped without Chromium).
- The diagram depends on a CDN. Offline, or if cdnjs is blocked, the page falls back to text. The version is pinned, so a new Mermaid release cannot change the page. A move to a new version is a deliberate edit.
- Polling costs a little per root every second: two small grouped queries and one `stat` per ledger. A root with many threads costs more per poll. None of it touches the build's process.
- The `-wal` and `-shm` sidecar files can appear next to an idle DB the dashboard has read. They are empty and harmless, and the next writer cleans them up. A tool that expects only `checkpoints.sqlite` in `.scio/` sees them.
- A process killed without an exception (a `kill -9`, a closed terminal, a lost machine) leaves no `__error__` write, so its thread reads `in_progress`, not `crashed`. Only `updated_at` shows that it has gone quiet. A stale-thread rule would need a threshold per node, which is left open.
- Open: answering stops from the page (decision 8) needs a design that keeps one writer per product, for example by handing the decision to the same `scio build --decision` path. Postgres roots are open as well.
