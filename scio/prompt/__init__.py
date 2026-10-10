"""Layer 1: prompts.

Versioned per-role templates, provider-safe wire schemas generated from Pydantic
models, and the validate-and-repair loop for structured outputs.
"""

from scio.prompt.registry import PromptRegistry, PromptTemplate
from scio.prompt.repair import StructuredResult, structured_call
from scio.prompt.wire_schema import wire_schema

__all__ = ["PromptRegistry", "PromptTemplate", "StructuredResult", "structured_call", "wire_schema"]
