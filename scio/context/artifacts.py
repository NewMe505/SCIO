"""Artifact store: large text lives on disk, state carries ``ArtifactRef`` (path + sha256)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scio.harness.caching import estimate_tokens
from scio.state.models import ArtifactRef, Role

Kind = ArtifactRef.model_fields["kind"].annotation


class ArtifactStore:
    def __init__(self, root: Path | str, thread_id: str) -> None:
        self.root = Path(root) / thread_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._n = 0

    def put(
        self, kind: str, text: str, *, produced_by: Role, name: str | None = None
    ) -> ArtifactRef:
        self._n += 1
        stem = name or f"{self._n:03d}-{kind}"
        path = self.root / f"{stem}.txt"
        path.write_text(text, encoding="utf-8", newline="\n")
        return ArtifactRef(
            kind=kind,  # type: ignore[arg-type]
            path=str(path.resolve()),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            tokens=estimate_tokens(len(text)),
            produced_by=produced_by,
        )

    def get(self, ref: ArtifactRef) -> str:
        return Path(ref.path).read_text(encoding="utf-8")

    def head(self, ref: ArtifactRef, max_chars: int = 4000) -> str:
        text = self.get(ref)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n[... {len(text) - max_chars} more characters in {ref.path}]"
