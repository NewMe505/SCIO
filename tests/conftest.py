from __future__ import annotations

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
def _ci_git_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Git as a CI runner sees it: no global config, so no user identity, and no identity
    guessed from the host name. A commit that names none fails here as it does on CI (I-104)."""
    empty = tmp_path_factory.getbasetemp() / "gitconfig-global"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    for name in (
        "EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")


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
    """``SCIO_RUN_LIVE_TESTS=1``, read through ``Settings.run_live_tests`` (I-059)."""
    from scio.settings import Settings

    return Settings().run_live_tests
