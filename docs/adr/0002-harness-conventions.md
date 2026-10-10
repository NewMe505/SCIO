# ADR 0002: Harness conventions

- **Status:** Accepted
- **Date:** 2026-10-10
- **Milestone:** M1

## Context
Every model call in SCIO goes through one harness so that retries, budgets, cost, caching and tracing are consistent across providers and observable from the graph. Several choices made while building it are not derivable from the code and will affect later milestones.

## Decisions
1. **One retry layer.** Provider SDK retries are disabled (`max_retries=0`). The harness classifies every exception into `RetryableError`, `RepairableError` or `FatalError` and retries only the first, with exponential backoff, jitter and `retry-after`. Attempt counts land in the cost ledger and on the span.
2. **Conservative budget pre-check.** Before a call, the guard reserves the estimated input tokens plus the full `max_tokens` output (scaled by `reserve_output_fraction`, default 1.0). A call that *could* exceed the token or dollar cap is refused before it is made. The post-call update records actual usage. The pre-check uses a local 4-chars-per-token estimate by default; `SCIO_PRECISE_PRECHECK=1` calls the provider's token counter instead.
3. **Structured outputs: constrain on the wire, validate locally.** The wire schema strips keywords providers do not support (length and numeric constraints, patterns, titles, defaults) and closes every object with all properties required. The full Pydantic model is validated after the call and the model is re-asked with the error at most twice. Free-form `dict` fields and recursive models are rejected at schema generation time.
4. **Current-API request shape for Anthropic.** The `thinking` parameter is omitted (adaptive by default); depth is `output_config.effort`, set explicitly per role because Opus 5.5 defaults to `medium`. Typed outputs use `output_config.format`, never a forced tool. Cache breakpoints go on the system block and the last tool, skipped when the prefix is below the model's minimum. Server-side refusal fallbacks (`fallbacks: "default"`) are enabled per model via `refusal_fallback` in `models.yaml`, on for Opus 5.5 only.
5. **Fake provider is first-class.** It is deterministic, scriptable, records every request, and is what CI and the regression evals run on. `SCIO_PROVIDER_OVERRIDE=fake` routes every role to it while cost is still computed at the configured model's price, so offline runs report the money a real run would spend.
6. **Telemetry attribute names are pinned.** `gen_ai.*` keys follow the OTel GenAI semantic conventions at the version in `telemetry.SEMCONV_VERSION`. Prompt and completion content is recorded only with `SCIO_OTEL_CAPTURE_CONTENT=1`. Exporters: `console` for dev and tests, `otlp` (extra) for a collector; this answers open question 1 in ARCHITECTURE.md.
7. **Refusal is fatal.** A `stop_reason: refusal` raises `RefusalError` after accounting. The graph (M3+) decides whether to escalate to a human; the harness never retries a refusal.

## Consequences
- Provider swap is config-only; adapters are ~150 lines each and tested by translation unit tests, with live tests gated behind `SCIO_RUN_LIVE_TESTS=1`.
- Budget refusals can be early near a cap because the reserve is conservative. M5 may lower `reserve_output_fraction` from eval data.
- The OpenAI adapter's token counting is an estimate; its prices in `models.yaml` are placeholders until verified.
