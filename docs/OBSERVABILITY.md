# Observability: how to watch SCIO

SCIO can be watched in three layers. Each answers a different question, and none of them needs another:

| Layer | Use it for |
|---|---|
| 1. GitHub | the product's history as a team sees it: milestones, issues, PRs, CI |
| 2. A trace viewer | each model call: latency, tokens, model, cost |
| 3. The dashboard | SCIO's own state machine: which step each thread is on, what stop waits for you, cost by role and feature |

The CLI views (`scio status`, `scio report`) cover the same ground as the dashboard, one command at a time. They are described at the end.

On this machine, run SCIO as `uv run python -m scio ...`, never through the `scio` launcher. Windows Application Control blocks the venv's `scio.exe` (CLAUDE.md, I-075). Behind the corporate proxy, set `UV_SYSTEM_CERTS=1` for every `uv` command.

## 1. GitHub

Use it for the record of a product build, and for anything you would show someone else.

With `--gh owner/name`, `scio new` creates the repository, and `scio build` keeps it current. For each milestone it creates a GitHub milestone and an issue per feature. It opens the milestone PR and waits for CI and for your merge. A release goes through a release PR, then a tag and a GitHub Release (ADR 0016).

```sh
uv run python -m scio new --prompt "<idea>" --dir ../myproduct --gh owner/myproduct
uv run python -m scio build --product ../myproduct --gh owner/myproduct
```

GitHub shows what SCIO published. It does not show the step a thread is on, a stop that has not reached a PR yet, or what a feature cost.

## 2. A trace viewer for model calls

Use it for one model call at a time: how long it took, which model served it, its tokens and its cost, and where retries happened.

SCIO emits OpenTelemetry GenAI spans from the harness ([`scio/harness/telemetry.py`](../scio/harness/telemetry.py)), so this layer needs no SCIO code. Any viewer that accepts OTLP over HTTP works.

### Turn it on

1. Install the `otlp` extra. `uv sync` installs exactly the extras you name and removes the others. To keep the dashboard too, name both:
   ```sh
   UV_SYSTEM_CERTS=1 uv sync --extra otlp
   UV_SYSTEM_CERTS=1 uv sync --extra otlp --extra dashboard   # with the dashboard
   ```
2. Set the exporter and the endpoint, then run SCIO as usual:
   ```sh
   export SCIO_OTEL_EXPORTER=otlp
   export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
   uv run python -m scio build --product ../myproduct
   ```
   - `SCIO_OTEL_EXPORTER` takes `none` (the default), `console` (spans printed to stdout) or `otlp`.
   - The exporter sends OTLP over HTTP, as protobuf. `OTEL_EXPORTER_OTLP_ENDPOINT` is the collector's base URL, without a path; the exporter appends `/v1/traces` itself. Do not put `/v1/traces` in this variable, or the path is doubled. To give the full URL instead, set the standard `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (for example `http://localhost:4318/v1/traces`).
   - Unset, the endpoint is `http://localhost:4318/v1/traces`, the standard OTLP HTTP port.
   - Spans are batched and flushed when the process exits.

### Arize Phoenix

Phoenix takes OTLP over HTTP on its UI port, 6006, not on 4318:

```sh
docker run --rm -p 6006:6006 arizephoenix/phoenix:latest
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006
```

Open http://localhost:6006. Without Docker, `pip install arize-phoenix` and `phoenix serve` start the same server.

### Jaeger all-in-one

Jaeger takes OTLP over HTTP on 4318 and serves its UI on 16686:

```sh
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Open http://localhost:16686 and pick the service `scio`.

### What the spans carry

Attribute names follow the OTel GenAI semantic conventions at the version pinned in `telemetry.SEMCONV_VERSION` (1.37.0). The service name is `scio`.

- **`invoke_agent <role>`**, one per model call:
  - `gen_ai.operation.name` (`invoke_agent`), `gen_ai.agent.name` (the role), `gen_ai.provider.name`;
  - `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`;
  - `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens`;
  - `scio.usd` (the call's cost at SCIO's prices) and `scio.attempts` (retries included);
  - `scio.tag.<key>` for each tag the node set: `scio.tag.thread` (the thread id) on every node's calls, plus keys such as `step`, `attempt`, `round`, `debug`, `explore`, `trigger`, `plan_version`, `milestone` or `revision` where they apply.
  - A failed call has status `ERROR` and the exception recorded.
- **`execute_tool <tool>`**, one per tool a worker ran through SCIO's toolbox: `gen_ai.operation.name` (`execute_tool`), `gen_ai.tool.name`, `scio.call_index` and `scio.role`.

Each model call is its own trace; there is no span for a node or a whole thread. To follow one thread, filter on `scio.tag.thread`.

Prompt and completion text is off by default. `SCIO_OTEL_CAPTURE_CONTENT=1` adds `gen_ai.system_instructions`, `gen_ai.input.messages` and `gen_ai.output.messages`. These hold the prompts and the code the model saw, so turn it on only for a viewer you trust.

### The gap under the Claude Code worker backend

With `SCIO_WORKER_BACKEND=claude_code` (ADR 0012), a worker turn (coder, debugger, documenter, explorer) runs inside the `claude` binary. SCIO records its cost in the ledger, but it opens no span for the turn. The tools run inside the binary too, so they produce no `execute_tool` spans. Those turns are missing from the traces. The tool-less roles (`SCIO_PROVIDER_OVERRIDE=claude_code`) still go through the harness and do get spans. The dashboard and `scio report` read the ledger, so they show the worker turns' cost either way.

## 3. The dashboard

Use it to watch a build live: every thread in one place, the step each one is on, the stop that waits for you, and where the money goes. The design and its limits are in [ADR 0017](adr/0017-dashboard.md).

### Run it

```sh
UV_SYSTEM_CERTS=1 uv sync --extra dashboard    # add --extra otlp to keep the exporter
uv run python -m scio dashboard --root . --root ../graphcalc-run
```

Open http://127.0.0.1:8765. `--port` changes the port.

A root is a directory SCIO ran in: the directory a `scio run`, `scio new` or `scio build` was started from. It holds `.scio/checkpoints.sqlite` (the checkpoint DB) and `.scio/ledger/` (one cost ledger per thread). Name one `--root` per directory; the example shows SCIO's own threads and the graphcalc run side by side. A root without a checkpoint DB is listed with no threads.

### What it shows

- **The threads list**, grouped by root and product. Each row has its kind (build, bootstrap or task), phase, exit reason, last update and cost, and a status:
  - a **waiting** badge, carrying the stop's question, when a thread waits for your decision;
  - a **crashed** badge when the process died in a node. The latest checkpoint has a pending error, and a resume clears it;
  - no badge for a process killed without an exception: it still reads in progress, and its last update shows how long it has been quiet.
- **A build thread:** the milestone and its features, with each attempt's tier, status, iterations, escalations, cost and commit, and a link to the feature's own thread. Also the integration check lines and the PR link.
- **A feature thread** (a task): iterations, escalations, and the last test and verify lines.
- **A bootstrap thread** (`scio new`): the interview's questions, each revision round's notes, the bootstrap commit and the repository.
- **The lit graph:** the thread's graph (build, bootstrap, or the task's tier) drawn from the same source as ARCHITECTURE.md's diagrams, with the nodes it runs next lit. Mermaid loads from a CDN; without it the page falls back to text.
- **Costs** by role and, for a build, by feature over every attempt. A build's total includes its feature threads. The figures are the ones `scio report` prints, built by the same functions.
- **The live timeline:** the thread's ledger of facts, guesses and decisions, oldest first.

The page updates without a reload. Once a second the server checks each thread for a new checkpoint, a new pending write (a stop or a crash) and ledger growth, and pushes the threads that changed over a WebSocket.

### Safe beside a live build

The dashboard is read-only. It opens the checkpoint DB read-only, has no write route, and closes each connection after each read, so it never holds up the build's writes. It is safe to run beside a live build. On an idle DB it may leave empty `-wal` and `-shm` files next to it; they are SQLite's index files, and the next writer cleans them up. The DB file itself is not changed.

The dashboard answers only on 127.0.0.1. It cannot answer a stop: answer it from the CLI, as the waiting badge's question says (`scio build --product DIR --decision "<text>"`, or `scio new --thread ID --decision "<text>"`).

## 4. The CLI views

These read the same files, one command at a time:

```sh
uv run python -m scio status --product ../myproduct     # the product's milestones and features
uv run python -m scio report --thread build-myproduct-m1 # one thread's report
```

- `scio status --product DIR` prints the product's `.scio/product/progress.json`: the bootstrap commit and build thread, each milestone with its features, PR and cost, and each feature's status, tier and commit.
- `scio report --thread ID` writes one thread's report (a task, a build or a bootstrap) to `--out` (default `.scio/reports`): tokens and cost by role, escalations, the timeline and tool signals.
  - It reads the checkpoint DB of the current directory (`SCIO_CHECKPOINT_DSN`, default `sqlite:///.scio/checkpoints.sqlite`). Run it from the root the thread ran in, or point `SCIO_CHECKPOINT_DSN` at that root's DB.

## See also

- [ADR 0017](adr/0017-dashboard.md): the dashboard's decisions and their reasons.
- [ARCHITECTURE.md](ARCHITECTURE.md): the graphs the dashboard draws, and the one-writer rule it keeps.
- [`scio/harness/telemetry.py`](../scio/harness/telemetry.py): the span names and attributes.
- [`scio/dashboard/contract.py`](../scio/dashboard/contract.py): the dashboard's API and its JSON shapes.
