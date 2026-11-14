"""Remove secrets from text before it is stored, posted to GitHub or put in a brief.
OWNER: wave-1 agent B."""

from __future__ import annotations

import re

REDACTED = "[redacted]"

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{50,})")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
"""A verbatim copy of ``scio/product/release_checks.py``'s ``SECRET_PATTERNS``: token
formats with a fixed prefix. ``tests/recovery/test_scrub.py`` reads SCIO's file as text and
fails when the two lists drift apart."""

ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|credentials?)\w*\b[\"']?\s*[:=]\s*"
    r"[\"']([^\"'\s]{16,})[\"']"
)
"""A verbatim copy of ``release_checks.ASSIGNMENT``: a credential-named key assigned a long
quoted literal. SCIO reports it only when the value looks random; the scrubber redacts it
always, because a false redaction costs nothing here."""

PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)"
)
"""The whole key, not only its header line, which is all ``SECRET_PATTERNS`` matches."""

BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}")

SECRET_WORDS: frozenset[str] = frozenset(
    {
        "key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "auth",
        "authorization",
        "credential",
        "credentials",
    }
)
"""A name is secret when one of its words (split at ``_``, ``-``, ``.`` and camelCase) is
one of these. Whole words, so ``GIT_AUTHOR_NAME``, ``max_tokens`` and ``monkey`` are not."""

_NAME = r"[A-Za-z_][\w.-]*"

ENV_ASSIGNMENT = re.compile(
    rf"(?<![\w.])(?P<name>{_NAME})(?P<sep>\s*=\s*)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'\\,;&]+)"
)
"""``NAME=value``, ``NAME="value"`` and ``name='value'`` (a shell line, a URL query, a
keyword argument)."""

QUOTED_PAIR = re.compile(
    rf"(?P<q>\\?[\"'])(?P<name>{_NAME})(?P=q)(?P<sep>\s*:\s*)"
    r"(?P<vq>\\?[\"'])(?P<value>[^\n]*?)(?P=vq)"
)
"""``"name": "value"`` and ``'name': 'value'`` (JSON, a Python dict), also with escaped
quotes (JSON inside a JSON string)."""

_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_TRIVIAL = re.compile(r"(?i)-?\d+(?:\.\d+)?|true|false|none|null|\[redacted\]")


def secret_name(name: str) -> bool:
    """True when a variable or field name names a secret (see :data:`SECRET_WORDS`)."""
    return any(word.lower() in SECRET_WORDS for word in _WORDS.findall(name))


def _trivial(value: str) -> bool:
    """A value not worth redacting: empty, a number, a boolean or null, already redacted."""
    inner = value.strip("\"'")
    return not inner or _TRIVIAL.fullmatch(inner) is not None


def _redact_env(m: re.Match[str]) -> str:
    value = m.group("value")
    if not secret_name(m.group("name")) or _trivial(value):
        return m.group(0)
    quote = value[0] if value[0] in "\"'" and len(value) > 1 and value[-1] == value[0] else ""
    return f"{m.group('name')}{m.group('sep')}{quote}{REDACTED}{quote}"


def _redact_pair(m: re.Match[str]) -> str:
    if not secret_name(m.group("name")) or _trivial(m.group("value")):
        return m.group(0)
    q, vq = m.group("q"), m.group("vq")
    return f"{q}{m.group('name')}{q}{m.group('sep')}{vq}{REDACTED}{vq}"


def _redact_assignment(m: re.Match[str]) -> str:
    start, end = m.span(1)
    base = m.start(0)
    whole = m.group(0)
    return whole[: start - base] + REDACTED + whole[end - base :]


def scrub(text: str) -> str:
    """``text`` with secrets replaced by ``[redacted]``.

    It covers SCIO's release secret patterns (``scio/product/release_checks.py``:
    private keys, AWS, GitHub, Anthropic, OpenAI, Slack and Google keys), plus any
    ``NAME=value`` or ``"name": "value"`` where the name holds ``KEY``, ``TOKEN``,
    ``SECRET``, ``PASSWORD`` or ``AUTH``. The patterns are copied, not imported (the
    package uses the standard library only), and a test checks that the copy matches
    SCIO's list.
    """
    if not text:
        return text
    out = PRIVATE_KEY_BLOCK.sub(REDACTED, text)
    for _kind, rx in SECRET_PATTERNS:
        out = rx.sub(REDACTED, out)
    out = BEARER.sub(lambda m: m.group(1) + REDACTED, out)
    out = QUOTED_PAIR.sub(_redact_pair, out)
    out = ENV_ASSIGNMENT.sub(_redact_env, out)
    return ASSIGNMENT.sub(_redact_assignment, out)
