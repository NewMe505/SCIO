# SCIO

A model-agnostic, multi-agent framework built on LangGraph that turns a prompt into product-ready software. Today it takes one software task, from a spec or a GitHub Issue, to a merged, tested and documented pull request, reliably and live. Product Mode (M6 to M11) is extending that into building whole products from a prompt, in any stack, one approved milestone at a time.

> Status: **v0.4.0 released; Product Mode M6 (Foundations) in progress.** Task mode is proven live: ten regression tasks at pass^5 = 1.00 offline, nightly live suites green, first live debugger and advisor runs recorded. Roadmap: [docs/PRODUCT_MODE.md](docs/PRODUCT_MODE.md); decision records: [docs/adr/](docs/adr/); board: [SCIO Product Mode](https://github.com/users/NewMe505/projects/1); milestones: [M6 to M11](https://github.com/NewMe505/SCIO/milestones).

## Product Mode in one paragraph
A product orchestrator sits above the proven task graph. It interviews you (two to four Opus questions), writes a normalised `PRODUCT.md`, picks a toolchain profile, and bootstraps a repository with CI and a walking skeleton. Then, milestone by milestone, it decomposes work into GitHub issues, runs each feature through the task graph with the tier pinned, verifies the integrated result against a fixed quality bar (tests with a coverage floor, lint, types, build, smoke and e2e against the running app, dependency audit, secret scan, working README instructions), and opens one milestone PR for a human to merge. Any failing check overrides any model verdict. There is one writer, so a product builds serially; there is no product-level dollar cap by default, but per-feature tier caps, stall caps and iteration caps remain as runaway guards, and cost is reported at every approval stop.

| Milestone | Deliverable | Done when |
|---|---|---|
| [M6 Foundations](https://github.com/NewMe505/SCIO/milestone/1) | Toolchain profiles behind one protocol (ADR 0009); `--tier` pins the route; declared settings read | regression pass^5 = 1.00, live t001 unchanged, no Python command outside the profile |
| [M7 Greenfield bootstrap](https://github.com/NewMe505/SCIO/milestone/2) | `scio new`: interview, `PRODUCT.md`, scaffold, git init, CI, walking skeleton | an empty directory becomes a repo with green CI and a smoke test, Fake and live |
| [M8 Decompose + milestone loop](https://github.com/NewMe505/SCIO/milestone/3) | progress file, GitHub milestones and issues, features in sequence, integration verify, milestone PR, resume | a two-milestone Fake product survives crash and resume; one live milestone |
| [M9 Dependencies + runtime](https://github.com/NewMe505/SCIO/milestone/4) | Docker default, two-phase network, `install_deps` / `run_app` / `delete_file`, e2e runner | a product with a third-party dependency and an HTTP server passes black-box e2e |
| [M10 Second stack](https://github.com/NewMe505/SCIO/milestone/5) | `node-ts` profile, stack detection, stack-agnostic acceptance grading | the same spec builds in Python and TypeScript |
| [M11 Product-ready + evals](https://github.com/NewMe505/SCIO/milestone/6) | quality bar, release gate, tag and artifact; p001 CLI, p002 REST API, p003 web app | p001 to p003 pass live; rows in EVAL_HISTORY |

## Task mode in one paragraph
A cheap router classifies each task into an effort tier (T0, T1 or T2). **Opus 5.5** plans, advises and checks that the objective was met before finishing. **Sonnet 5** does the work: coding, testing, debugging and documentation. Only one agent writes code. Parallel fan-out is used only for read-only exploration and review. Tests, linters and type checks decide pass/fail, not LLM opinion. Sonnet escalates to Opus only on specific signals (repeated errors, stalls, low confidence, plan drift, high-impact changes), with a cap on how often. Every run produces a GitHub PR, and the docs are updated in that same PR.

## Five layers
| Layer | Responsibility |
|---|---|
| Prompt | Versioned role templates with typed (Pydantic) outputs |
| Context | Just-in-time retrieval, compaction, artifacts passed by reference |
| Harness | Provider adapters, retries, budgets, caching, tracing, sandbox |
| Loop | Bounded act → verify → fix loops with explicit exit conditions |
| Graph | Checkpointed LangGraph StateGraph and routing |

## Usage

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras --group dev          # install (add --native-tls behind a corporate proxy)
uv run pytest                              # offline, Fake provider
uv run python scripts/check_docs.py        # docs checks (links, model-ID leak, changelog)
uv run python -m evals.runner --suite regression --provider fake --trials 5
uv run python scripts/gen_api_docs.py      # API docs → docs/api/ (pdoc)
```

Run a task against a repository (the router picks T0, T1 or T2). The spec is Markdown: first heading = title, checkbox
lines = acceptance criteria. The run pauses for a human before merge; resume with a decision.

```bash
uv run scio run --spec task.md --repo https://github.com/you/repo --issue 12
uv run scio resume --thread run-ab12cd34 --decision "approved"
SCIO_PROVIDER_OVERRIDE=fake uv run scio run --spec evals/tasks/t001_add_function/spec.md --repo <path>   # offline
```

Set `SCIO_GH_REPO=owner/name` to push the branch and open the draft PR; without it the run is
offline and the PR body is written to `.scio/artifacts/<thread>/pr-body.txt`.
`SCIO_FAKE_SCRIPT=1` with the Fake provider drives the scripted eval behaviour from the CLI.

Product mode (M7, ADR 0010): a prompt becomes a bootstrapped repository. The run stops twice for
you, after the interview questions and on the `PRODUCT.md` draft; a third stop asks before a
GitHub repository is created when `--gh` is given. A prompt starts a thread, no prompt resumes one.

```bash
uv run scio new --prompt "A CLI todo app with tags and JSON export" --dir ./todo [--gh you/todo]
uv run scio new --thread new-ab12cd34 --dir ./todo --decision "1: developers; 2: add and list; 3: no sync"
uv run scio new --thread new-ab12cd34 --dir ./todo --decision approve      # or revision notes
```

The result is a git repository on `main` with `PRODUCT.md`, `NOTES.md`, `.scio/product/progress.json`,
a package that answers `--version`, one smoke test, CI that runs the profile's lint and tests, README,
CHANGELOG, LICENSE and `.gitignore`; lint and tests ran green before the bootstrap commit.

The milestone loop (M8, ADR 0011) then builds the product one approved milestone at a time. Every
feature in `PRODUCT.md` starts `todo`, the walking skeleton included; the orchestrator always takes the
first open feature, an Opus planner groups up to four into a milestone with a tier each, GitHub gets a
milestone and one issue per feature (or `.scio/product/issues/` offline), and each feature runs through
the task graph on its own checkpoint thread on branch `milestone/<n>`. After the last feature the
profile's full suite and lint run on the branch, an Opus milestone gate gives a verdict that any failing
check overrides, and one milestone PR waits for you. A crash at any point resumes without a duplicated
issue, commit or PR; a feature that halts stops for one decision (`retry`, `skip`, or stop).

```bash
uv run scio build --product ./todo [--gh you/todo]        # runs the next milestone, pauses at its PR
uv run scio build --product ./todo --decision merged      # after you merged the PR (offline: "merge")
uv run scio build --product ./todo --decision "merge and stop"   # record the milestone, end the thread
uv run scio build --product ./todo --decision skip        # or retry / stop, when a feature halted
uv run scio status --product ./todo                       # progress.json as text
uv run scio report --thread build-todo_cli                # feature runs, cost, escalations
```

Running on a Claude subscription instead of the API (ADR 0012): with `SCIO_PROVIDER_OVERRIDE=claude_code`
every tool-less role (router, planners, advisor, gates, reviewer) runs as one `claude -p` call, and with
`SCIO_WORKER_BACKEND=claude_code` every tool-using turn (coder, debugger, documenter, explorer) is one
Claude Code session in the sandbox root followed by a typed summary on `--resume`. The binary is the
unmodified Claude Code signed in with your own account (`claude` on PATH, or `SCIO_CLAUDE_CODE_BIN`);
nothing is billed to a Console organisation, the dollars in reports are the API-price estimate Claude
Code prints. Verify, commits, the gate and the PR flow are unchanged; local sandbox only.

```bash
SCIO_PROVIDER_OVERRIDE=claude_code SCIO_WORKER_BACKEND=claude_code uv run scio run --spec task.md --repo ./repo
```

After a run, `uv run scio report --thread <id>` writes `.scio/reports/<id>.md` and `.json`:
tokens and dollars by role, every escalation with its outcome, and the ledger timeline.

Evals: `python -m evals.runner --suite regression|capability --provider fake --config evals/configs/<ab>.yaml`
runs a suite under an A/B role configuration (`sonnet_only`, `sonnet_opus_advisor`, `opus_only`);
`--parallel N` runs a task's trials concurrently; `--max-usd X` stops starting tasks once the suite's
spend reaches X (the report says what was skipped). `python -m evals.tune` sweeps the escalation
thresholds (ADR 0006).

Nightly live suite: `python scripts/nightly_live.py` runs regression and capability on Anthropic
under a total cost cap (default $10), using the `ant auth login` profile, and appends one row per
suite to [docs/EVAL_HISTORY.md](docs/EVAL_HISTORY.md). On the dev machine it is registered as the
Windows scheduled task `SCIO nightly live evals` (03:17 daily); remove it with
`schtasks /delete /tn "SCIO nightly live evals" /f`. The GitHub nightly workflow runs the same
capped commands and switches to the live provider only when an `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN` repository secret exists. The nightly workflow runs
both suites per config and posts the tables to the job summary.

## Acceptance status (PROMPT.md)

| Criterion | Status | Evidence |
|---|---|---|
| Crash mid-run resumes from checkpoint with zero duplicated side effects | met | `tests/durability/test_crash_resume.py`, `tests/graph/test_t0_fake.py::test_resume_after_crash_does_not_duplicate_commits` |
| Provider swap is config-only; full suite passes on the Fake provider offline | met | `configs/models.yaml`, CI runs with `SCIO_PROVIDER_OVERRIDE=fake` |
| Sample task passes with pass^5 ≥ 0.8 within a stated budget | met, live | t001 on Anthropic (Sonnet 5 worker, Opus 5.5 gate, Haiku router): 5/5 trials, one iteration each, ≈55k tokens and $0.13 per trial against a 150k / $1.50 cap (2026-10-11); Fake-provider regression pass^5 = 1.00 as well |
| Advisor calls ≤ cap, each logged with trigger, cost and outcome | met | `EscalationRecord`; `tests/unit/test_escalation.py`, `tests/graph/test_t1_t2_fake.py` |
| Sample task produces a GitHub PR linked to its issue, template filled, docs in the same PR, CI green | met | Recorded 2026-10-10 on the throwaway repository `NewMe505/scio-eval-sandbox` (issue #1 → PR #2, Fake provider, template filled, docs in the PR, that repo's CI green); the repository was deleted afterwards. Ledger I-028 and the M5 retrospective describe the run |

Calling a model through the harness. Roles, models, effort and prices live only in
`configs/models.yaml`; code never names a model.

```python
from pydantic import BaseModel
from scio.harness import Harness, Message
from scio.prompt import PromptRegistry


class Verdict(BaseModel):
    verdict: str
    guidance: str


harness = Harness.from_settings(tier="T1")  # budget guard from configs/budgets.yaml
system = PromptRegistry().get("advisor").render()  # static, cacheable system prompt
request = harness.build_request("advisor", system=system, messages=[Message.user("...brief...")])
result = harness.structured(request, Verdict)  # validate locally, repair ≤2 times
print(result.value, result.attempts, harness.summary())
```

Credentials: either `ANTHROPIC_API_KEY`, or a Claude login profile created with `ant auth login`
(the SDK picks it up automatically). A set `ANTHROPIC_API_KEY` always wins over the profile, so unset
a stale one. Install the CLI from https://github.com/anthropics/anthropic-cli/releases.

Environment (`SCIO_*`, see `scio/settings.py`): `SCIO_PROVIDER_OVERRIDE=fake` routes every
role to the offline Fake provider (CI does this); `SCIO_OTEL_EXPORTER=console|otlp` turns on
tracing (`otlp` reads `OTEL_EXPORTER_OTLP_ENDPOINT`, install the `otlp` extra);
`SCIO_OTEL_CAPTURE_CONTENT=1` opts in to recording prompt text on spans. Provider credentials
are read by the SDKs (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). Live tests:
`SCIO_RUN_LIVE_TESTS=1 uv run pytest tests/live -m live`.

M2 settings: `SCIO_CHECKPOINT_DSN` (`memory://`, `sqlite:///.scio/checkpoints.sqlite`, `postgresql://...`),
`SCIO_EFFECTS_PATH` (idempotent side-effect log), `SCIO_SANDBOX_BACKEND=local|docker|e2b` with
`SCIO_SANDBOX_DOCKER_IMAGE` / `SCIO_SANDBOX_E2B_TEMPLATE` and the `postgres` / `e2b` extras, and
`SCIO_GH_REPO=owner/name` for the GitHub client. Regenerate graph diagrams with
`uv run python scripts/gen_mermaid.py` (the docs check fails when they drift).

## Docs
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): Milestone 0 design (graphs, State, agent contracts, directory tree, milestone plan)
- [docs/PROMPT.md](docs/PROMPT.md): the build spec / generation prompt
- [docs/RESEARCH.md](docs/RESEARCH.md): the evidence behind each design decision
- [docs/PRODUCT_MODE.md](docs/PRODUCT_MODE.md): roadmap M6–M11, from single tasks to building product-ready software from a prompt in any stack
- [docs/IDEAS_TRIAGE.md](docs/IDEAS_TRIAGE.md): ROI triage of proposed improvements against live data
- [docs/adr/](docs/adr/): Architecture Decision Records (0001 topology and models, 0002 harness conventions, 0003 idempotent effects and sandbox tiers, 0004 loop and verification, 0005 escalation policy, 0006 thresholds from data, 0007 product mode, 0008 advisor review of high-impact diffs, 0009 toolchain profiles, 0010 product graph and bootstrap, 0011 milestone loop, 0012 Claude Code backend)
- [CONTRIBUTING.md](CONTRIBUTING.md): branching, commits, PRs, docs-as-code
- [CHANGELOG.md](CHANGELOG.md)

## Roadmap
Task mode (complete, released as 0.1.0 to 0.4.0):
- M0 (done): architecture, State schema, agent contracts, directory tree
- M1 (done): harness, providers (Anthropic, OpenAI, Fake), prompt layer, eval skeleton, CI
- M2 (done): state, checkpointing with idempotent effects, ACI tools, sandbox backends, GitHubClient
- M3 (done): router, context and loop layers, the T0 graph end to end, graph-driven evals
- M4 (done): T1/T2 tiers, parallel explorers, planner, six-signal triage, capped Opus advisor, reviewer
- M5 (done): nightly capability suite, A/B configs, threshold tuning (ADR 0006), run reports, release workflow
- post-0.1.0 (done): live validation, realistic and path-specific eval tasks t006 to t010, advisor review of passing high-impact diffs (ADR 0008)

Product Mode ([docs/PRODUCT_MODE.md](docs/PRODUCT_MODE.md), ADR 0007; tracked on the [project board](https://github.com/users/NewMe505/projects/1)):
- M6 (in progress): toolchain profiles (ADR 0009), `--tier` pins the route, every declared setting read
- M7: `scio new`, greenfield bootstrap from a prompt to a repository with green CI
- M8: progress file, GitHub milestones and issues, the milestone loop with crash-safe resume
- M9: Docker by default, two-phase network, dependency installation, running the app for e2e
- M10: `node-ts` profile and stack detection
- M11: product-ready quality bar, release gate, product eval suite p001 to p003
