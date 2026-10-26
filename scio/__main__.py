"""``python -m scio``: the same entry point as the ``scio`` console script, for machines whose
application-control policy blocks the venv's launcher executable (I-075)."""

from __future__ import annotations

import sys

from scio.cli import main

if __name__ == "__main__":
    sys.exit(main())
