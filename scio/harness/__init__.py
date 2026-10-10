"""Layer 3: the model harness.

Provider adapters behind one :class:`~scio.harness.provider.ModelProvider` protocol,
plus retries, budget guard, cost accounting, prompt-cache placement and tracing.
The graph never imports a provider SDK; it talks to :class:`~scio.harness.core.Harness`.
"""

from scio.harness.core import Harness
from scio.harness.failures import (
    BudgetExceeded,
    FatalError,
    HarnessError,
    RepairableError,
    RetryableError,
    StructuredOutputError,
)
from scio.harness.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    StopReason,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
    Usage,
)

__all__ = [
    "BudgetExceeded",
    "CompletionRequest",
    "CompletionResponse",
    "FatalError",
    "Harness",
    "HarnessError",
    "Message",
    "RepairableError",
    "RetryableError",
    "StopReason",
    "StructuredOutputError",
    "TextPart",
    "ToolCall",
    "ToolResultPart",
    "ToolSpec",
    "ToolUsePart",
    "Usage",
]
