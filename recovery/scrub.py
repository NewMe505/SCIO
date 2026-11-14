"""Remove secrets from text before it is stored, posted to GitHub or put in a brief.
OWNER: wave-1 agent B."""

from __future__ import annotations

REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """``text`` with secrets replaced by ``[redacted]``.

    It covers SCIO's release secret patterns (``scio/product/release_checks.py``:
    private keys, AWS, GitHub, Anthropic, OpenAI, Slack and Google keys), plus any
    ``NAME=value`` or ``"name": "value"`` where the name holds ``KEY``, ``TOKEN``,
    ``SECRET``, ``PASSWORD`` or ``AUTH``. The patterns are copied, not imported (the
    package uses the standard library only), and a test checks that the copy matches
    SCIO's list.
    """
    raise NotImplementedError
