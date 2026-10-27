"""Stack-neutral parts of the product-ready quality bar (M11, ADR 0007 decision 5): the
secret scan, the release files and the README's run commands. All are SCIO's own code, not
a tool the product must install, so they run on every machine and are never "skipped: tool
absent"; the README commands come from the profile's command words."""

from __future__ import annotations

import math
import re
import shlex
from collections import Counter

from scio.aci.sandbox.base import Sandbox
from scio.toolchain import Toolchain

RUN_START = "<!-- scio:run -->"
RUN_END = "<!-- scio:run-end -->"
"""The README block whose commands must work (PRODUCT_MODE component 5): ``integrate`` runs
them, and the product's own CI runs the same block."""

RELEASE_FILES = ("README.md", "CHANGELOG.md", "LICENSE", "Dockerfile")
"""What a released product carries besides its code (PRODUCT_MODE component 5)."""

ALLOW_MARKER = "scio: allow-secret"
"""A line carrying this comment is not reported (a documented test fixture)."""

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{50,})")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
"""Token formats with a fixed prefix: a match is a finding on its own."""

ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|credentials?)\w*\b[\"']?\s*[:=]\s*"
    r"[\"']([^\"'\s]{16,})[\"']"
)
"""A credential-named key assigned a long quoted literal; reported when the value looks
random (letters and digits mixed, high entropy), so ``token = "replace-me-before-deploying"``
is not: readable words score high on entropy alone."""

MIN_ENTROPY = 3.5
MAX_BYTES = 1_000_000


def looks_random(value: str) -> bool:
    has_digit = any(c.isdigit() for c in value)
    has_letter = any(c.isalpha() for c in value)
    return has_digit and has_letter and entropy(value) >= MIN_ENTROPY


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    counts = Counter(value)
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def secrets_in(text: str) -> list[tuple[int, str]]:
    """``(line number, kind)`` for every likely secret in ``text``; never the value."""
    found: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        kinds = [name for name, rx in SECRET_PATTERNS if rx.search(line)]
        if not kinds:
            m = ASSIGNMENT.search(line)
            if m and looks_random(m.group(1)):
                kinds = ["credential-like assignment"]
        found += [(i, k) for k in kinds[:1]]
    return found


def scan_secrets(sb: Sandbox) -> list[str]:
    """``path:line: kind`` for likely secrets in the files git tracks in the product."""
    r = sb.exec(["git", "ls-files", "-z"], timeout_s=60)
    if not r.ok:
        return [f"(git ls-files failed: {r.stderr.strip()[:200]})"]
    findings: list[str] = []
    for rel in (p for p in r.stdout.split("\0") if p):
        path = f"{sb.root}/{rel}"
        try:
            text = sb.read_text(path)
        except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
            continue  # binary or unreadable (a backend may raise RuntimeError): not text
        if len(text) > MAX_BYTES:
            continue
        findings += [f"{rel}:{line}: {kind}" for line, kind in secrets_in(text)]
    return findings


def missing_release_files(sb: Sandbox) -> list[str]:
    return [f for f in RELEASE_FILES if not sb.exists(f"{sb.root}/{f}")]


def readme_run_commands(text: str) -> list[str] | None:
    """The command lines between the run markers (code fences dropped); None without them."""
    if RUN_START not in text or RUN_END not in text:
        return None
    block = text.split(RUN_START, 1)[1].split(RUN_END, 1)[0]
    lines = (ln.strip() for ln in block.splitlines())
    return [ln for ln in lines if ln and not ln.startswith("```")]


def readme_report(sb: Sandbox, tc: Toolchain) -> tuple[str, bool]:
    """Run the README's run block. Every command must start with one of the profile's
    command words (no free-form shell, PRODUCT_MODE component 4) and exit 0."""
    path = f"{sb.root}/README.md"
    commands = readme_run_commands(sb.read_text(path)) if sb.exists(path) else None
    if not commands:
        return (
            f"[readme] FAILED: README.md has no run commands between {RUN_START} and {RUN_END}",
            False,
        )
    for command in commands:
        argv = shlex.split(command)
        if argv[0] not in tc.command_words:
            words = ", ".join(tc.command_words)
            return f"[readme] FAILED: `{command}`: only {words} commands are run", False
        r = sb.exec(argv, timeout_s=120)
        if not r.ok:
            return f"[readme] FAILED: `{command}` exited {r.exit_code}\n{r.output[-800:]}", False
    return f"[readme] passed: {len(commands)} run command(s)", True


def release_report(sb: Sandbox, tc: Toolchain) -> tuple[list[str], bool]:
    """The ``[secrets]``, ``[release files]`` and ``[readme]`` lines of the integration
    report."""
    secrets = scan_secrets(sb)
    missing = missing_release_files(sb)
    readme, readme_ok = readme_report(sb, tc)
    lines = [
        "[secrets] clean"
        if not secrets
        else f"[secrets] FAILED: {len(secrets)} likely secret(s)\n" + "\n".join(secrets[:20]),
        "[release files] present: " + ", ".join(RELEASE_FILES)
        if not missing
        else "[release files] FAILED: missing " + ", ".join(missing),
        readme,
    ]
    return lines, not secrets and not missing and readme_ok
