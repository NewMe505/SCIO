"""Skip marker for tests that drive a real browser (M11, p003). The dev machine and CI's
``checks`` job install Chromium with ``python -m playwright install chromium``; anywhere else
these tests skip rather than fail (I-074, I-080: a test never depends on what a machine
happens to have)."""

from __future__ import annotations

import pytest


def _chromium_launches() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception:  # no browser download, no system libraries: not this machine's job
        return False
    return True


HAS_BROWSER = _chromium_launches()
requires_browser = pytest.mark.skipif(
    not HAS_BROWSER, reason="needs Playwright's Chromium (python -m playwright install chromium)"
)
