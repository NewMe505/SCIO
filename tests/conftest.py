from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scio.harness.accounting import CostLedger
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.harness.telemetry import configure_tracing
from scio.settings import REPO_ROOT, Settings


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "fake")
    monkeypatch.delenv("SCIO_PRECISE_PRECHECK", raising=False)
    monkeypatch.delenv("SCIO_OTEL_CAPTURE_CONTENT", raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(provider_override="fake", models_path=REPO_ROOT / "configs" / "models.yaml")


@pytest.fixture
def config(settings: Settings) -> ModelsConfig:
    return ModelsConfig.load(settings.models_path)


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


@pytest.fixture(scope="session")
def _span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    configure_tracing(exporter, simple=True)  # one global provider per process
    return exporter


@pytest.fixture
def spans(_span_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    _span_exporter.clear()
    yield _span_exporter
    _span_exporter.clear()


@pytest.fixture
def harness(config: ModelsConfig, settings: Settings, fake: FakeProvider) -> Harness:
    registry = ProviderRegistry(config, settings)
    registry.register("fake", fake)
    return Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=1_000_000, usd_max=100.0)),
        ledger=CostLedger(),
        retry=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=False),
        sleep=lambda _s: None,
    )


def live_enabled() -> bool:
    return os.environ.get("SCIO_RUN_LIVE_TESTS") == "1"
