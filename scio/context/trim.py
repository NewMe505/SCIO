"""Size-triggered transcript trimming (no model call).

Old tool results are the bulk of an agent transcript. When the message list exceeds
``max_chars``, tool results outside the last ``keep_last`` tool-result messages are
replaced by a one-line placeholder. The list stays append-only in structure: nothing is
removed or reordered, only the content of old tool results is shortened, so the cached
prefix (system + tools) is untouched.
"""

from __future__ import annotations

from scio.harness.types import Message, ToolResultPart

PLACEHOLDER = "[earlier tool output trimmed to save context; call the tool again if needed]"


def total_chars(messages: list[Message]) -> int:
    n = 0
    for m in messages:
        for p in m.parts:
            n += (
                len(p.text)
                if p.type == "text"
                else len(p.content)
                if p.type == "tool_result"
                else 64
            )
    return n


def trim_messages(
    messages: list[Message], *, max_chars: int = 30_000, keep_last: int = 3
) -> list[Message]:
    """Return a copy with old tool results shortened when the transcript is too large."""
    if total_chars(messages) <= max_chars:
        return list(messages)
    result_idx = [
        i for i, m in enumerate(messages) if any(p.type == "tool_result" for p in m.parts)
    ]
    protected = set(result_idx[-keep_last:]) if keep_last else set()
    out: list[Message] = []
    for i, m in enumerate(messages):
        if i in protected or i not in result_idx:
            out.append(m)
            continue
        parts = [
            ToolResultPart(tool_use_id=p.tool_use_id, content=PLACEHOLDER, is_error=p.is_error)
            if isinstance(p, ToolResultPart) and len(p.content) > len(PLACEHOLDER)
            else p
            for p in m.parts
        ]
        out.append(Message(role=m.role, parts=parts))
    return out
