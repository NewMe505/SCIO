"""The secret scrubber: SCIO's token formats (a copy that cannot drift) plus credential-named
assignments, and nothing else."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from recovery import scrub as scrub_module
from recovery.scrub import REDACTED, scrub, secret_name

ROOT = Path(__file__).resolve().parents[2]
RELEASE_CHECKS = ROOT / "scio" / "product" / "release_checks.py"

# Built by concatenation, as tests/unit/test_release_checks.py does, so no literal token
# sits in the source.
GITHUB = "gh" + "p_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
GITHUB_PAT = "github" + "_pat_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6"
ANTHROPIC = "sk-" + "ant-" + "api03-Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1Jh"
OPENAI = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7"
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
SLACK = "xo" + "xb-" + "1234567890-abcdefghij"
GOOGLE = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
KEY_BLOCK = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7x\nq9Zl2c\n-----END RSA PRIVATE KEY-----"
)
SHA = "e3b530d1f0c2a4b6c8d0e2f4a6b8c0d2e4f6a8b0"


def _compiled_patterns(source: str, name: str) -> list[str]:
    """The string literals passed to ``re.compile`` in the module-level assignment to
    ``name``."""
    for node in ast.parse(source).body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if value is None or not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        return [
            call.args[0].value
            for call in ast.walk(value)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "compile"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ]
    raise AssertionError(f"{name} not found")


def test_the_copied_patterns_match_scios_release_checks() -> None:
    """recovery/ cannot import scio, so it copies the patterns. This reads SCIO's file as
    text and fails when either list changes without the other."""
    source = RELEASE_CHECKS.read_text(encoding="utf-8")
    theirs = _compiled_patterns(source, "SECRET_PATTERNS")
    assert len(theirs) >= 7, "the scan found fewer patterns than SCIO has: fix the scan"
    ours = [rx.pattern for _kind, rx in scrub_module.SECRET_PATTERNS]
    missing = [p for p in theirs if p not in ours]
    assert missing == [], f"copy these into recovery/scrub.py SECRET_PATTERNS: {missing}"
    stale = [p for p in ours if p not in theirs]
    assert stale == [], f"SCIO dropped these; drop them from the copy too: {stale}"
    assert _compiled_patterns(source, "ASSIGNMENT") == [scrub_module.ASSIGNMENT.pattern]


@pytest.mark.parametrize(
    "secret", [GITHUB, GITHUB_PAT, ANTHROPIC, OPENAI, AWS, SLACK, GOOGLE], ids=lambda s: s[:6]
)
def test_every_token_format_is_redacted(secret: str) -> None:
    out = scrub(f"the value was {secret} here")
    assert secret not in out
    assert out == f"the value was {REDACTED} here"


def test_a_github_token_and_an_anthropic_key_are_redacted_in_a_traceback_message() -> None:
    text = f"FatalError: claude -p failed: bad key {ANTHROPIC} (token {GITHUB})"
    out = scrub(text)
    assert ANTHROPIC not in out and GITHUB not in out
    assert out == f"FatalError: claude -p failed: bad key {REDACTED} (token {REDACTED})"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ANTHROPIC_API_KEY=abc123def456", f"ANTHROPIC_API_KEY={REDACTED}"),
        ('ANTHROPIC_API_KEY="abc 123 def"', f'ANTHROPIC_API_KEY="{REDACTED}"'),
        ("export GH_TOKEN=s3cr3t-value", f"export GH_TOKEN={REDACTED}"),
        ("SCIO_DB_PASSWORD=hunter2", f"SCIO_DB_PASSWORD={REDACTED}"),
        ("https://x.test/cb?code=1&auth=abc123xyz", f"https://x.test/cb?code=1&auth={REDACTED}"),
        ("Client(api_key='abc123def')", f"Client(api_key='{REDACTED}')"),
        ('{"api_key": "abc123def"}', f'{{"api_key": "{REDACTED}"}}'),
        ("{'refreshToken': 'r-123-x'}", f"{{'refreshToken': '{REDACTED}'}}"),
        ('{\\"client_secret\\": \\"abc\\"}', f'{{\\"client_secret\\": \\"{REDACTED}\\"}}'),
        ("Authorization: Bearer abcdefghijklmnop1234", f"Authorization: Bearer {REDACTED}"),
    ],
)
def test_a_credential_named_assignment_is_redacted(text: str, expected: str) -> None:
    assert scrub(text) == expected


def test_a_private_key_is_redacted_whole_not_only_its_header() -> None:
    out = scrub(f"before\n{KEY_BLOCK}\nafter")
    assert out == f"before\n{REDACTED}\nafter"
    assert "MIIE" not in scrub(f"{KEY_BLOCK.split('-----END')[0]}")  # a tail cut the END line


@pytest.mark.parametrize(
    "text",
    [
        "PAUSED for a human:",
        "build thread build-graphcalc-m3 phase=halted exit=feature_failed dir C:/p",
        "F2 attempt 1 failed: RepairableError: worker produced no diff",
        f"live-wt moved to {SHA}",
        f"commit {SHA[:12]} merged",
        "KeyError: 'feature_runs'",
        "GIT_AUTHOR_NAME=scio max_tokens=4096 tokens_used=12 author=me",
        "SCIO_TOKEN_BUDGET=1000 auth=true api_key=",
        '{"key": null, "token": 3}',
        "Command '['claude', '-p', '--output-format', 'json']' timed out after 600 seconds",
        "",
    ],
)
def test_ordinary_text_and_a_commit_sha_are_left_alone(text: str) -> None:
    assert scrub(text) == text


def test_scrubbing_twice_changes_nothing_more() -> None:
    text = f"ANTHROPIC_API_KEY={ANTHROPIC} and {GITHUB}"
    assert scrub(scrub(text)) == scrub(text)


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("ANTHROPIC_API_KEY", True),
        ("apiKey", True),
        ("x-api-key", True),
        ("ANTHROPIC_AUTH_TOKEN", True),
        ("accessToken", True),
        ("client_secret", True),
        ("passwd", True),
        ("GIT_AUTHOR_NAME", False),
        ("max_tokens", False),
        ("monkey", False),
        ("keyboard", False),
    ],
)
def test_a_name_is_secret_by_its_words_not_its_letters(name: str, secret: bool) -> None:
    assert secret_name(name) is secret
