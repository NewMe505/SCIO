"""The ``Harness`` facade: the one object the graph uses to call a model.

Every call goes through, in order: role config → request → budget pre-check → traced,
retried provider call → cost accounting → budget update. Structured calls add the
validate-and-repair loop from :mod:`scio.prompt.repair`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

import yaml
from pydantic import BaseModel

from scio.harness.accounting import CostLedger
from scio.harness.budget import BudgetGuard, BudgetLimits, BudgetsConfig
from scio.harness.caching import estimate_tokens
from scio.harness.config import ModelConfig, ModelsConfig, ProviderRegistry
from scio.harness.failures import RefusalError
from scio.harness.provider import ModelProvider
from scio.harness.retry import RetryPolicy, with_retry
from scio.harness.telemetry import configure_tracing, invoke_agent_span, record_response
from scio.harness.types import CompletionRequest, CompletionResponse, Message, StopReason, ToolSpec
from scio.settings import Settings, get_settings

if TYPE_CHECKING:
    from scio.prompt.repair import StructuredResult

T = TypeVar("T", bound=BaseModel)

_UNLIMITED = BudgetLimits(tokens_max=10**12, usd_max=10**9)


class Harness:
    def __init__(
        self,
        config: ModelsConfig,
        providers: ProviderRegistry,
        *,
        settings: Settings | None = None,
        budget: BudgetGuard | None = None,
        ledger: CostLedger | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.providers = providers
        self.settings = settings or get_settings()
        self.budget = budget or BudgetGuard(_UNLIMITED)
        self.ledger = ledger or CostLedger()
        self.retry = retry or RetryPolicy(max_attempts=self.settings.max_attempts)
        self._sleep = sleep

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        tier: str | None = None,
        budget: BudgetGuard | None = None,
    ) -> Harness:
        settings = settings or get_settings()
        if settings.otel_exporter != "none":
            configure_tracing(settings.otel_exporter)
        config = ModelsConfig.load(settings.models_path)
        if settings.role_overrides_path is not None:
            loaded = yaml.safe_load(settings.role_overrides_path.read_text(encoding="utf-8"))
            data: dict[str, Any] = dict(loaded) if isinstance(loaded, dict) else {}
            roles = data.get("roles")
            config = config.with_role_overrides(dict(roles) if isinstance(roles, dict) else data)
        if budget is None and tier is not None:
            budget = BudgetsConfig.load(settings.budgets_path).guard_for(tier)
        return cls(config, ProviderRegistry(config, settings), settings=settings, budget=budget)

    # -- request construction ------------------------------------------------

    def model_for(self, role: str) -> ModelConfig:
        return self.config.model(self.config.role(role).model)

    def build_request(
        self,
        role: str,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int | None = None,
        tags: dict[str, str] | None = None,
    ) -> CompletionRequest:
        rc = self.config.role(role)
        return CompletionRequest(
            model=rc.model,
            system=system,
            messages=list(messages),
            tools=list(tools),
            max_tokens=max_tokens or rc.max_tokens,
            effort=rc.effort,
            role=role,
            tags=tags or {},
        )

    # -- calls ---------------------------------------------------------------

    def provider_for(self, request: CompletionRequest) -> ModelProvider:
        name = self.config.provider_name_for(request.model, self.settings.provider_override)
        return self.providers.get(name)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._call(request, lambda p, r: p.complete(r))

    def structured(
        self, request: CompletionRequest, model_cls: type[T], *, max_repairs: int = 2
    ) -> StructuredResult[T]:
        def call(req: CompletionRequest) -> CompletionResponse:
            assert req.output_schema is not None
            schema = req.output_schema
            return self._call(req, lambda p, r: p.structured(r, schema))

        # Imported here, not at module level: scio.prompt.repair imports the harness
        # package, so a top-level import makes `import scio.prompt` circular (I-030).
        from scio.prompt.repair import structured_call

        return structured_call(call, request, model_cls, max_repairs=max_repairs)

    def _call(
        self,
        request: CompletionRequest,
        fn: Callable[[ModelProvider, CompletionRequest], CompletionResponse],
    ) -> CompletionResponse:
        model_cfg = self.config.model(request.model)
        provider = self.provider_for(request)
        est_in = (
            provider.count_tokens(request)
            if self.settings.precise_precheck
            else estimate_tokens(request.approx_input_chars())
        )
        self.budget.reserve(
            est_input_tokens=est_in, max_output_tokens=request.max_tokens, pricing=model_cfg.pricing
        )
        attempts = 0
        capture = self.settings.otel_capture_content
        with invoke_agent_span(request, provider=provider.name, capture_content=capture) as span:

            def once() -> CompletionResponse:
                nonlocal attempts
                attempts += 1
                return fn(provider, request)

            response = with_retry(once, self.retry, sleep=self._sleep)
            usd = model_cfg.pricing.cost_of(response.usage)
            self.ledger.record(
                role=request.role or "model",
                model=response.model,
                provider=provider.name,
                usage=response.usage,
                usd=usd,
                latency_ms=response.latency_ms,
                request_id=response.request_id,
                attempts=attempts,
                tags=request.tags,
            )
            self.budget.record(response.usage, usd)
            record_response(span, response, usd=usd, attempts=attempts, capture_content=capture)
        if response.stop_reason is StopReason.REFUSAL:
            raise RefusalError(
                f"model refused (category={response.refusal_category})",
                category=response.refusal_category,
            )
        return response

    def summary(self) -> dict[str, Any]:
        return {"budget": self.budget.snapshot().model_dump(), "costs": self.ledger.to_dict()}
