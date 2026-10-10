"""Live Anthropic checks. Skipped unless ``SCIO_RUN_LIVE_TESTS=1`` and credentials exist.

Run: ``SCIO_RUN_LIVE_TESTS=1 pytest tests/live -m live -s``
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from scio.harness.core import Harness
from scio.harness.types import Message
from scio.settings import Settings
from tests.conftest import live_enabled

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not live_enabled(), reason="SCIO_RUN_LIVE_TESTS != 1"),
]


class Verdict(BaseModel):
    verdict: Literal["continue", "replan", "change_approach", "escalate_human"]
    guidance: str


@pytest.fixture
def live_harness() -> Harness:
    return Harness.from_settings(Settings(provider_override=None, precise_precheck=True))


def test_prompt_cache_read_on_second_identical_prefix(live_harness: Harness) -> None:
    # > 1024 tokens so the prefix clears every configured model's minimum.
    system = ("You are a careful assistant. " * 400).strip()
    req = live_harness.build_request(
        "router", system=system, messages=[Message.user("Say OK.")], max_tokens=16
    )
    first = live_harness.complete(req)
    second = live_harness.complete(
        req.model_copy(update={"messages": [Message.user("Say OK again.")]})
    )
    assert first.usage.cache_write_tokens > 0 or first.usage.cache_read_tokens > 0
    assert second.usage.cache_read_tokens > 0, "no cache hit: a silent invalidator is in the prefix"


def test_structured_output_roundtrip(live_harness: Harness) -> None:
    req = live_harness.build_request(
        "advisor",
        system="You are an advisor. Reply with JSON only.",
        messages=[Message.user("The worker's test passes. Verdict?")],
        max_tokens=300,
    )
    res = live_harness.structured(req, Verdict)
    assert res.value.verdict in {"continue", "replan", "change_approach", "escalate_human"}
    assert live_harness.ledger.totals().usd > 0
