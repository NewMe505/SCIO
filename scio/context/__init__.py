"""Layer 2: context.

Every prompt is rebuilt from typed state for every call: a static, cacheable system
prompt per role, then a task-specific projection. Large content is stored in the
:class:`ArtifactStore` and passed by reference; transcripts are trimmed by size.
"""

from scio.context.artifacts import ArtifactStore
from scio.context.assembler import (
    coder_message,
    documenter_message,
    gate_message,
    router_message,
    tag_context,
)
from scio.context.trim import trim_messages

__all__ = [
    "ArtifactStore",
    "coder_message",
    "documenter_message",
    "gate_message",
    "router_message",
    "tag_context",
    "trim_messages",
]
