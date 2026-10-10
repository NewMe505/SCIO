"""Versioned prompt templates, one directory per role: ``templates/<role>/v<N>.md``.

A template is the role's *static* system prompt. It must not contain anything that
varies per call (dates, ids, task text); those go in the messages so the system prompt
stays byte-stable and cacheable. Placeholders use ``{{name}}`` and are substituted
only for values that are constant for a role version (for example the role name).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_VERSION_RE = re.compile(r"^v(\d+)\.md$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class PromptTemplate:
    role: str
    version: int
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def id(self) -> str:
        return f"{self.role}@v{self.version}:{self.sha256[:12]}"

    def placeholders(self) -> list[str]:
        return sorted(set(_PLACEHOLDER_RE.findall(self.text)))

    def render(self, **values: str) -> str:
        missing = [p for p in self.placeholders() if p not in values]
        if missing:
            raise PromptError(f"{self.id}: missing values for {missing}")

        def sub(m: re.Match[str]) -> str:
            return values[m.group(1)]

        return _PLACEHOLDER_RE.sub(sub, self.text)


class PromptRegistry:
    def __init__(self, root: Path = TEMPLATES_DIR) -> None:
        self.root = root
        if not root.is_dir():
            raise PromptError(f"templates directory not found: {root}")

    def roles(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and self.versions(p.name))

    def versions(self, role: str) -> list[int]:
        d = self.root / role
        if not d.is_dir():
            return []
        out: list[int] = []
        for f in d.iterdir():
            m = _VERSION_RE.match(f.name)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def get(self, role: str, version: int | None = None) -> PromptTemplate:
        versions = self.versions(role)
        if not versions:
            raise PromptError(f"no templates for role {role!r} under {self.root}")
        v = version if version is not None else versions[-1]
        if v not in versions:
            raise PromptError(f"role {role!r} has no v{v}; available: {versions}")
        text = (self.root / role / f"v{v}.md").read_text(encoding="utf-8")
        return PromptTemplate(role=role, version=v, text=text)
