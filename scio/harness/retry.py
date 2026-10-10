"""Retry with exponential backoff and jitter. The single retry layer for all providers.

Provider SDKs have their own retry; adapters disable it (``max_retries=0``) so that
attempts, delays and accounting live in one place and are observable.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from scio.harness.failures import RetryableError

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True

    def delay_for(self, attempt: int, retry_after_s: float | None, rng: random.Random) -> float:
        """Delay before attempt ``attempt + 1`` (``attempt`` is 0-based, the one that failed)."""
        if retry_after_s is not None:
            return min(max(retry_after_s, 0.0), self.max_delay_s)
        delay: float = min(self.base_delay_s * (2.0**attempt), self.max_delay_s)
        if self.jitter:
            delay += rng.uniform(0, min(1.0, delay))
        return min(delay, self.max_delay_s)


def with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, RetryableError, float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn`` until it succeeds or raises a non-retryable error.

    Only :class:`RetryableError` is retried. Everything else propagates immediately.
    """
    rng = rng or random.Random()
    attempt = 0
    while True:
        try:
            return fn()
        except RetryableError as exc:
            attempt += 1
            if attempt >= policy.max_attempts:
                raise
            delay = policy.delay_for(attempt - 1, exc.retry_after_s, rng)
            log.warning(
                "retry %d/%d after %.1fs: %s", attempt, policy.max_attempts - 1, delay, exc.message
            )
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
