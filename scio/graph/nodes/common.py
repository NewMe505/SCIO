"""Helpers shared by nodes: git summaries, ledger entries, feature parsing."""

from __future__ import annotations

import re

from scio.aci.sandbox.base import Sandbox
from scio.state.models import FeatureItem, LedgerEntry, Role

_CHECKBOX_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?P<text>\S.*)$")


def features_from_acceptance(acceptance: list[str]) -> list[FeatureItem]:
    items: list[FeatureItem] = []
    for raw in acceptance:
        m = _CHECKBOX_RE.match(raw)
        text = (m.group("text") if m else raw).strip()
        if text:
            items.append(FeatureItem(id=f"F{len(items) + 1}", description=text))
    return items


def acceptance_from_issue_body(body: str) -> list[str]:
    """Checkbox lines from an issue body, in order."""
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(("- [ ]", "- [x]", "- [X]", "* [ ]", "* [x]")):
            out.append(s[5:].strip())
    return out


def fact(text: str, *, source: Role, step_id: str | None = None) -> LedgerEntry:
    return LedgerEntry(kind="fact", text=text, source=source, step_id=step_id)


def decision(text: str, *, source: Role, step_id: str | None = None) -> LedgerEntry:
    return LedgerEntry(kind="decision", text=text, source=source, step_id=step_id)


def git_out(sandbox: Sandbox, *args: str) -> str:
    r = sandbox.exec(["git", *args], timeout_s=60)
    return (
        r.stdout if r.ok else f"(git {' '.join(args)} failed: {r.stderr.strip()[:200]})"
    ).strip()


def diff_stat(sandbox: Sandbox, base_sha: str | None) -> str:
    if not base_sha:
        return git_out(sandbox, "status", "--short") or "(no changes)"
    staged = git_out(sandbox, "diff", "--stat", base_sha)
    return staged or "(no changes since base)"


def slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "task"
