"""The ``ModelProvider`` protocol every adapter implements."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from scio.harness.types import CompletionRequest, CompletionResponse, StreamEvent


@runtime_checkable
class ModelProvider(Protocol):
    """Synchronous provider interface. Adapters raise only ``HarnessError`` subclasses."""

    name: str

    def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    def stream(self, request: CompletionRequest) -> Iterator[StreamEvent | CompletionResponse]:
        """Yield :class:`StreamEvent` deltas, then exactly one :class:`CompletionResponse`."""
        ...

    def structured(self, request: CompletionRequest, schema: dict[str, Any]) -> CompletionResponse:
        """Constrain the response to ``schema``. The caller validates and repairs."""
        ...

    def count_tokens(self, request: CompletionRequest) -> int: ...
