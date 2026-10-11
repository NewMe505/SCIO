# SCIO

A model-agnostic, multi-agent framework built on LangGraph. It takes a software task from spec (or a GitHub Issue) to a merged, tested and documented pull request.

> Status: **Milestone 5 complete (observability, eval tuning, release 0.1.0)**. The build spec is in [docs/PROMPT.md](docs/PROMPT.md); the M0 design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Design in one paragraph
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

After a run, `uv run scio report --thread <id>` writes `.scio/reports/<id>.md` and `.json`:
tokens and dollars by role, every escalation with its outcome, and the ledger timeline.

Evals: `python -m evals.runner --suite regression|capability --provider fake --config evals/configs/<ab>.yaml`
runs a suite under an A/B role configuration (`sonnet_only`, `sonnet_opus_advisor`, `opus_only`);
`--parallel N` runs a task's trials concurrently. `python -m evals.tune` sweeps the escalation thresholds (ADR 0006). The nightly workflow runs
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
- [docs/adr/](docs/adr/): Architecture Decision Records (0001 topology and models, 0002 harness conventions, 0003 idempotent effects and sandbox tiers, 0004 loop and verification, 0005 escalation policy, 0006 thresholds from data)
- [CONTRIBUTING.md](CONTRIBUTING.md): branching, commits, PRs, docs-as-code
- [CHANGELOG.md](CHANGELOG.md)

## Roadmap
- M0 (done): architecture, State schema, agent contracts, directory tree
- M1 (done): harness, providers (Anthropic, OpenAI, Fake), prompt layer, eval skeleton, CI
- M2 (done): state, checkpointing with idempotent effects, ACI tools, sandbox backends, GitHubClient
- M3 (done): router, context and loop layers, the T0 graph end to end, graph-driven evals
- M4 (done): T1/T2 tiers, parallel explorers, planner, six-signal triage, capped Opus advisor, reviewer
- M5 (done): nightly capability suite, A/B configs, threshold tuning (ADR 0006), run reports, release workflow
