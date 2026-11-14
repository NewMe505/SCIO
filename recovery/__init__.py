"""SCIO's external recovery supervisor (ADR 0019).

It watches a ``scio build`` from outside, as its parent process. When the build fails, it
classifies the failure. If the failure is a SCIO harness fault, it preserves the build's
state and has a Claude Code session repair SCIO in a tested PR. After a human merges that
PR, it moves ``.scio/live-wt`` and replays the original command, so the build resumes
from its checkpoint.

The package uses the standard library only and never imports ``scio``, so a broken SCIO
cannot break its own supervisor. ``tests/recovery/test_boundary.py`` enforces that.
"""

__version__ = "0.1.0"
