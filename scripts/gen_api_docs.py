"""Generate API docs from docstrings into ``docs/api/`` with pdoc.

Usage: ``python scripts/gen_api_docs.py`` (output directory is git-ignored; CI uploads it
as an artifact).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "api"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pdoc", "scio", "evals", "-o", str(OUT), "--no-show-source"]
    print(" ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
