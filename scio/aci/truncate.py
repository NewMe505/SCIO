"""Output truncation with an actionable hint. Every tool result passes through here."""

from __future__ import annotations

DEFAULT_MAX_CHARS = 8_000


def truncate(text: str, *, max_chars: int = DEFAULT_MAX_CHARS, hint: str) -> str:
    """Keep the head and tail of ``text`` and say how to narrow the query."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        f"{text[:head].rstrip()}\n"
        f"[... {omitted} characters omitted; narrow your query: {hint} ...]\n"
        f"{text[-tail:].lstrip()}"
    )
