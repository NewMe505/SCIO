"""``configs/models.yaml`` as typed config, plus the provider registry.

Code refers to *roles*; roles refer to *models*; models refer to *providers*. Model IDs
appear nowhere else in the codebase (``scripts/check_docs.py`` enforces this).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from scio.harness.accounting import ModelPricing
from scio.harness.caching import CachePolicy
from scio.harness.provider import ModelProvider
from scio.settings import Settings

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ProviderConfig(BaseModel):
    adapter: Literal["anthropic", "openai", "fake"]


class ModelConfig(ModelPricing):
    provider: str
    context_window: int
    max_output_tokens: int
    cache_min_tokens: int = 0
    refusal_fallback: bool = False

    @property
    def pricing(self) -> ModelPricing:
        return ModelPricing(
            input_per_mtok=self.input_per_mtok,
            output_per_mtok=self.output_per_mtok,
            cache_read_per_mtok=self.cache_read_per_mtok,
            cache_write_per_mtok=self.cache_write_per_mtok,
        )


class RoleConfig(BaseModel):
    model: str
    effort: Effort = "medium"
    max_tokens: int = 4096


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    roles: dict[str, RoleConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_refs(self) -> ModelsConfig:
        for mid, m in self.models.items():
            if m.provider not in self.providers:
                raise ValueError(f"model {mid!r} references unknown provider {m.provider!r}")
        for rid, r in self.roles.items():
            if r.model not in self.models:
                raise ValueError(f"role {rid!r} references unknown model {r.model!r}")
            if r.max_tokens > self.models[r.model].max_output_tokens:
                raise ValueError(f"role {rid!r} max_tokens exceeds model max_output_tokens")
        return self

    @classmethod
    def load(cls, path: Path) -> ModelsConfig:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError as exc:
            raise KeyError(f"unknown role {name!r}; known: {sorted(self.roles)}") from exc

    def model(self, model_id: str) -> ModelConfig:
        try:
            return self.models[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model {model_id!r}; known: {sorted(self.models)}") from exc

    def provider_name_for(self, model_id: str, override: str | None = None) -> str:
        return override or self.model(model_id).provider


class ProviderRegistry:
    """Lazily constructs adapters so importing the harness needs no credentials."""

    def __init__(self, config: ModelsConfig, settings: Settings) -> None:
        self._config = config
        self._settings = settings
        self._instances: dict[str, ModelProvider] = {}

    def register(self, name: str, provider: ModelProvider) -> None:
        """Inject a ready instance (tests register a scripted Fake under ``fake``)."""
        self._instances[name] = provider

    def get(self, name: str) -> ModelProvider:
        if name not in self._instances:
            self._instances[name] = self._build(name)
        return self._instances[name]

    def _build(self, name: str) -> ModelProvider:
        try:
            adapter = self._config.providers[name].adapter
        except KeyError as exc:
            raise KeyError(f"unknown provider {name!r}") from exc
        if adapter == "anthropic":
            from scio.harness.providers.anthropic import AnthropicProvider

            return AnthropicProvider(
                timeout_s=self._settings.request_timeout_s,
                cache=CachePolicy(),
                cache_min_tokens={
                    mid: m.cache_min_tokens
                    for mid, m in self._config.models.items()
                    if m.provider == name
                },
                refusal_fallback_models=frozenset(
                    mid
                    for mid, m in self._config.models.items()
                    if m.provider == name and m.refusal_fallback
                ),
            )
        if adapter == "openai":
            from scio.harness.providers.openai import OpenAIProvider

            return OpenAIProvider(timeout_s=self._settings.request_timeout_s)
        from scio.harness.providers.fake import FakeProvider

        return FakeProvider()
