# SCIO

A model-agnostic, multi-agent framework built on LangGraph. It takes a software task from spec (or a GitHub Issue) to a merged, tested and documented pull request.

> Status: **Milestone 1 (harness + providers + evals skeleton + CI)**. The build spec is in [docs/PROMPT.md](docs/PROMPT.md); the M0 design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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

## Usage (M1: harness only)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras --group dev          # install (add --native-tls behind a corporate proxy)
uv run pytest                              # offline, Fake provider
uv run python scripts/check_docs.py        # docs checks (links, model-ID leak, changelog)
uv run python -m evals.runner --suite regression --provider fake --trials 5
uv run python scripts/gen_api_docs.py      # API docs → docs/api/ (pdoc)
```

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

Environment (`SCIO_*`, see `scio/settings.py`): `SCIO_PROVIDER_OVERRIDE=fake` routes every
role to the offline Fake provider (CI does this); `SCIO_OTEL_EXPORTER=console|otlp` turns on
tracing (`otlp` reads `OTEL_EXPORTER_OTLP_ENDPOINT`, install the `otlp` extra);
`SCIO_OTEL_CAPTURE_CONTENT=1` opts in to recording prompt text on spans. Provider credentials
are read by the SDKs (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). Live tests:
`SCIO_RUN_LIVE_TESTS=1 uv run pytest tests/live -m live`.

## Docs
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): Milestone 0 design (graphs, State, agent contracts, directory tree, milestone plan)
- [docs/PROMPT.md](docs/PROMPT.md): the build spec / generation prompt
- [docs/RESEARCH.md](docs/RESEARCH.md): the evidence behind each design decision
- [docs/adr/](docs/adr/): Architecture Decision Records (0001 topology and models, 0002 harness conventions)
- [CONTRIBUTING.md](CONTRIBUTING.md): branching, commits, PRs, docs-as-code
- [CHANGELOG.md](CHANGELOG.md)

## Roadmap
- M0 (done): architecture, State schema, agent contracts, directory tree
- M1 (done): harness, providers (Anthropic, OpenAI, Fake), prompt layer, eval skeleton, CI
- M2: state, checkpointing, agent tools, sandbox, GitHubClient
- M3: router and T0 loop
- M4: T1/T2 tiers and the escalation policy
- M5: observability and eval tuning
