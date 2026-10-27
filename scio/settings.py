"""Process-level settings, read from the environment with the ``SCIO_`` prefix.

Provider credentials are *not* here: the Anthropic and OpenAI SDKs read
``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` (or an ``ant auth login`` profile)
themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set as ``SCIO_<FIELD>``."""

    model_config = SettingsConfigDict(env_prefix="SCIO_", env_file=".env", extra="ignore")

    models_path: Path = REPO_ROOT / "configs" / "models.yaml"
    budgets_path: Path = REPO_ROOT / "configs" / "budgets.yaml"

    provider_override: str | None = None
    """Route every role to this provider (``fake`` in CI) regardless of models.yaml."""

    role_overrides_path: Path | None = None
    """YAML mapping role -> model (or role config) applied over models.yaml (A/B configs)."""

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

    # --- M2: graph durability, sandbox, GitHub ---
    checkpoint_dsn: str = "sqlite:///.scio/checkpoints.sqlite"
    """``memory://`` (tests), ``sqlite:///<path>`` (dev) or ``postgresql://...`` (prod)."""
    effects_path: Path = Path(".scio/effects.sqlite")
    """Durable log of idempotent side effects, keyed by hash(thread_id, node, intent)."""

    toolchain: str = "auto"
    """Toolchain profile for the target repository (ADR 0009, ADR 0014): ``auto`` detects it
    from the repository's marker files (``pyproject.toml`` -> ``python``, ``package.json`` and
    ``tsconfig.json`` -> ``node-ts``) and falls back to ``python``; a profile name forces it.
    A product's ``PRODUCT.md`` names its stack and wins inside ``scio new`` and ``scio build``."""

    sandbox_backend: Literal["local", "docker", "e2b"] = "local"
    """``local`` is for tests and dev only: it cannot disable the network. Read by
    ``RunContext.create`` (I-054)."""
    product_sandbox_backend: Literal["auto", "local", "docker"] = "auto"
    """The backend ``scio build`` uses (M9): ``auto`` is Docker when a daemon answers and the
    worker backend is ``aci``, else ``local`` with the reason in the ledger; ``local`` and
    ``docker`` force one. Task mode and ``scio new`` keep ``sandbox_backend``."""
    coverage_floor: int = Field(default=80, ge=0, le=100)
    """Line-coverage floor of the product quality bar (M11), in percent of the product's own
    code (tests excluded): ``integrate`` fills it into ``{coverage_floor}`` in a profile
    check's argv."""
    sandbox_docker_image: str = "python:3.12-slim"
    """Needs ``git`` for product builds: ``docker/sandbox.Dockerfile`` adds it to this image."""
    sandbox_e2b_template: str | None = None
    sandbox_timeout_s: int = 120
    sandbox_memory_mb: int = 2048
    sandbox_cpus: float = 1.0

    gh_repo: str | None = None
    """``owner/name`` the GitHubClient acts on. None disables GitHub writes."""

    # --- ADR 0012: the Claude Code backend (a Claude subscription instead of API billing) ---
    worker_backend: Literal["aci", "claude_code"] = "aci"
    """``aci``: SCIO's own tool loop over its sandbox tools. ``claude_code``: every
    tool-using turn (coder, debugger, documenter, explorer) is one ``claude -p`` run in the
    sandbox root. Pair with ``SCIO_PROVIDER_OVERRIDE=claude_code`` to keep the tool-less
    roles off the API as well."""
    claude_code_bin: str = "claude"
    """The unmodified Claude Code binary (on PATH, or an absolute path)."""
    claude_code_budget_factor: float = 4.0
    """Scale for the tier dollar caps in ``budgets.yaml`` on the ``claude_code`` backend:
    Claude Code's cost estimate for the same work is about four times the API figure (I-069)."""
    claude_code_token_factor: float = 10.0
    """Scale for the tier token caps on the ``claude_code`` backend: its tokens are mostly
    cheap cache traffic (about 380,000 billable tokens per estimated dollar in the live product
    evals), so tokens need a larger factor than dollars for both caps to bind together (I-099)."""


def get_settings() -> Settings:
    """Build settings from the environment. Not cached so tests can override env."""
    return Settings()
