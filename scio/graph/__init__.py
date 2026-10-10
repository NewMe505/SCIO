"""Layer 5: the graph runtime.

M2 ships the durable parts every tier shares: the checkpointer, idempotent side
effects, and Mermaid rendering of compiled graphs. Tiers and nodes follow in M3/M4.
"""

from scio.graph.checkpointer import open_checkpointer
from scio.graph.effects import EffectLog, effect_key, run_effect
from scio.graph.mermaid import render_mermaid, update_doc_section

__all__ = [
    "EffectLog",
    "effect_key",
    "open_checkpointer",
    "render_mermaid",
    "run_effect",
    "update_doc_section",
]
