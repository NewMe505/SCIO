"""Process-level settings, read from the environment with the ``SCIO_`` prefix.

Provider credentials are *not* here: the Anthropic and OpenAI SDKs read
``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` (or an ``ant auth login`` profile)
themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set as ``SCIO_<FIELD>``."""

    model_config = SettingsConfigDict(env_prefix="SCIO_", env_file=".env", extra="ignore")

    models_path: Path = REPO_ROOT / "configs" / "models.yaml"
    budgets_path: Path = REPO_ROOT / "configs" / "budgets.yaml"

    provider_override: str | None = None
    """Route every role to this provider (``fake`` in CI) regardless of models.yaml."""

    request_timeout_s: float = 600.0
    max_attempts: int = 4
    """Total attempts per call including the first; retries use exponential backoff."""

    precise_precheck: bool = False
    """Call the provider's token counter before each call instead of a local estimate."""

    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_capture_content: bool = False
    """Opt-in: record prompt and completion text on spans. Off by default."""

    run_live_tests: bool = False
    """Enable tests marked ``live`` (real provider calls that cost money)."""


def get_settings() -> Settings:
    """Build settings from the environment. Not cached so tests can override env."""
    return Settings()
