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
   - There is no write route. Every HTTP route is `GET` or `HEAD`, plus the `/ws` WebSocket, which only pushes. `test_every_route_is_a_read` asserts this over the app's routes, so a write route cannot be added by accident.
   - It runs beside a live build without touching the build's files. One measured caveat: on a DB with no open writer, a `mode=ro` reader leaves empty `-wal` and `-shm` sidecar files. These are SQLite's WAL index, and the next writer cleans them up. The main DB file stays byte-identical. `test_the_data_layer_never_writes` hashes it before and after the reads and checks that a write raises.
   - Each read opens its own connection and closes it before returning (the `DataSource` contract). A read transaction is never held across polls, so it never blocks the writer's WAL checkpoint and the `-wal` file cannot grow because of the dashboard.
2. **Local only.** The page shows prompts, stop questions, costs and paths, so it must not be reachable from outside the machine.
   - `scio dashboard --root DIR [--root DIR ...] [--port 8765]` binds `127.0.0.1` and prints the URL. It has no host option.
   - The `Host` header is checked against `127.0.0.1` and `localhost` (`test_a_foreign_host_is_refused`). Binding to loopback does not stop DNS rebinding, where a foreign page resolves its own name to 127.0.0.1. The Host check does.
   - The WebSocket accepts an `Origin` only when it matches the request's own host and port (`test_ws_refuses_a_foreign_origin`). Browsers do not apply the same-origin policy to WebSockets, so any page could otherwise open `/ws`. Matching the port, not only the loopback name, keeps another local page (a dev server on another port) from watching it.
   - Every HTTP response carries `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`, so no other page can frame the dashboard.
   - FastAPI's docs routes (`/docs`, `/redoc`, `/openapi.json`) are off. Swagger UI would load from a CDN, and the routes are not needed.
3. **An optional extra.** `dashboard` installs FastAPI and uvicorn. The core install is unchanged: only the `scio dashboard` command imports them, so the rest of SCIO runs without them. Install it with `UV_SYSTEM_CERTS=1 uv sync --all-extras`, or add `--extra dashboard` to the extras you already sync. A bare `uv sync --extra dashboard` uninstalls every extra it does not name (guardrail 21, I-124).
4. **A plain HTML/JS page with no build step.** `scio/dashboard/static/` holds `index.html`, `app.js` and `style.css`, served as they are. The owner chose view-only and plain JS. With no bundler, no `node_modules` and no second toolchain in the repository, the page can be read and changed with a text editor.
   - Mermaid, which draws the graphs, is 11.15.0 from cdnjs, loaded with an SRI `integrity` hash and `crossorigin`. A changed file on the CDN does not run.
   - The page's Content-Security-Policy meta allows scripts only from the page itself and cdnjs: no inline script and no `eval`.
   - The page builds its DOM with `textContent` only, so text from a checkpoint (a stop's question, a model's output) is never parsed as HTML. `test_the_page_never_writes_html_from_data_or_evaluates_code` fails on `innerHTML`, `eval` and the like in `app.js`.
   - Without the CDN the diagram is shown as text, and the rest of the page works (`test_the_page_works_without_the_cdn`).
   - The cost bars are inline SVG.
5. **Reuse, not a second model.** The dashboard builds its views with the same functions as `scio report`: `build_product_report`, `build_bootstrap_report` and `build_report`, over `CostLedger.load_entries`. It classifies threads by their checkpoint channels, never by name, under the same rules as `scio report` (I-066, I-108):
   - `feature_runs` or `product_dir` means a build;
   - `target_dir` means a bootstrap (`scio new`);
   - anything else is a task.

   So the dashboard and `scio report` print the same costs, and a fix to one fixes both. The graph diagrams come from `scio.graph.registry.GRAPHS` through `render_mermaid`, the same source as ARCHITECTURE.md's generated sections. The step lit on a diagram is the thread's `next_nodes`, read from the checkpoint and never from the running process. It combines the `branch:to:<node>` channels with the nodes of the pending `Send` packets (`__pregel_tasks`), which is where T2's parallel explorers appear.
6. **Change detection by polling, per open WebSocket.** Each open `/ws` socket reads `versions()` for every root once a second, in a worker thread, so the server's event loop never blocks on SQLite. A thread's `ThreadVersion` is:
   - its latest checkpoint id;
   - its count of `writes` rows. A stop arrives as a pending write on an existing checkpoint, and so do a crash and a parallel task's progress, so the checkpoint id alone misses them (I-120, `test_a_stop_arriving_changes_the_writes_count_not_the_checkpoint`);
   - its ledger file's size.

   That is two grouped queries per root and one `stat` per ledger. The socket pushes a `ChangeEvent` (`{"changed": [...], "removed": [...], "at": ...}`) only when something changed. A root that fails to read keeps its last snapshot, and the socket stays open. The page refetches the threads list, and the open thread when it changed or is the parent build of a thread that changed. No hook into the running build exists or is needed, because the build is a separate process that SCIO does not ask to publish anything.
7. **Crashed is a status, and waiting is exact.** The data layer derives a thread's status once, in this order: `error`, `crashed`, `waiting`, `halted`, `done`, `in_progress` (`ThreadStatus` in the contract). The API and the page never re-derive it.
   - `crashed`: a pending `__error__` write on the latest checkpoint means the process died in a node. The thread lists as `crashed` with the exception's text (`test_a_crash_lists_as_crashed_with_the_exception`). A resume does not clear it at once. LangGraph records nothing of a resume until the resumed node returns, so a crashed build resumed with `scio build` reads `crashed` until its `feature` node returns (I-133).
   - `waiting`: an `__interrupt__` write with no `__resume__` write on the same checkpoint. A stop that has been answered and whose node is running again reads `in_progress`. The rule is exact because every SCIO node interrupts at most once (I-065), and a scan test keeps that true.
8. **The attempt in progress is shown.** SCIO appends a `FeatureRun` to the build's state only when an attempt ends. Without more, the feature running now would read "not run" and its spend would be missing. Found on the live graphcalc build.
   - The build view also links each feature thread of the build that has no run yet, as `running`. Its feature id and attempt come from the thread id (`<build>-p4-a1`), which `scio build` derives the same way.
   - Its spend so far is its cost ledger's sum, which grows with every model call. It counts in the cost by role and by feature, and separately as `running_usd`. Only an attempt with no ledger entries falls back to its checkpoint's `budgets.usd_used`. A checkpoint is written only when a node returns, so it lags a long worker turn.
   - The build's headline cost stays the figure `scio report` prints, which counts finished runs.
9. **A live run's files are read as they are being written.**
   - A ledger's half-written last line is left out, not an error (I-127). `CostLedger.load_entries` was fixed, so `scio report` on a live run gets the same fix.
   - One unreadable root does not hide the others. The threads list skips it and logs why, and `/api/threads?root=` for that root answers 503.
10. **Out of scope, left for a v2.**
   - Answering stops from the page. That would make the dashboard a writer of the checkpoint DB beside the build, and SCIO keeps one writer per product ([ARCHITECTURE.md](../ARCHITECTURE.md)). Stops are answered with `scio build --decision` or `scio new --decision`, as before.
   - Postgres checkpoints (`postgresql://` in `SCIO_CHECKPOINT_DSN`). A root is a directory with a SQLite checkpoint DB.

## Consequences
- Tests:
  - `tests/unit/test_dashboard_contract.py`: the fixtures validate against the contract;
  - `tests/unit/test_dashboard_data.py`: checkpoints from the Fake build for the three kinds of thread, the `merge_wait` stop, costs equal to what `scio report` prints, the guards named above, and a torn last ledger line;
  - `tests/unit/test_dashboard_app.py`: every endpoint against a fake `DataSource` built from the fixtures, the guards named above, and the pushes (a new checkpoint, a stop with no new checkpoint, a new or removed thread, a failing root, no polling once the client leaves);
  - `tests/unit/test_dashboard_cli.py`: loopback only, no host option, a clear message without the extra;
  - `tests/live/test_dashboard_page.py`: Playwright against the fixtures, skipped without Chromium.
- The diagram depends on a CDN. Offline, or if cdnjs is blocked, it is text. The version is pinned and hash-checked, so a new Mermaid release cannot change the page. Moving to a new version means editing the URL and the hash together.
- Polling costs a little per open page every second: two small grouped queries and one `stat` per ledger, for every root. Two open tabs poll twice, and nothing polls with no page open. A root with many threads costs more per poll. None of it touches the build's process.
- The `-wal` and `-shm` sidecar files can appear next to an idle DB the dashboard has read. They are empty and harmless, and the next writer cleans them up. A tool that expects only `checkpoints.sqlite` in `.scio/` sees them.
- A process killed without an exception (a `kill -9`, a closed terminal, a lost machine) leaves no `__error__` write, so its thread reads `in_progress`, not `crashed`. Only `updated_at` shows that it has gone quiet. A stale-thread rule would need a threshold per node, which is left open.
- A crashed thread that has been resumed still reads `crashed` until the resumed node returns, because the checkpoint DB records nothing of the resume before then (I-133). For a build that can last a whole feature attempt.
- A build's cost has two figures while a feature runs: the headline (finished runs, as `scio report` prints it) and the breakdowns that include `running_usd`. They agree again once the attempt ends.
- Open: answering stops from the page (decision 10) needs a design that keeps one writer per product, for example by handing the decision to the same `scio build --decision` path. Postgres roots are open as well.
