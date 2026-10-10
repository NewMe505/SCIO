"""Failure classes. Every error leaving the harness is one of these three kinds.

* :class:`RetryableError`: transient (rate limit, 5xx, timeout, connection). Retry with backoff.
* :class:`RepairableError`: the model answered but the answer is unusable (invalid JSON,
  schema violation, truncated output). The Prompt layer re-asks with the error.
* :class:`FatalError`: do not retry (bad request, auth, refusal, budget, repair cap).
"""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for harness failures."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class RetryableError(HarnessError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.retry_after_s = retry_after_s


class RepairableError(HarnessError):
    def __init__(
        self, message: str, *, raw_text: str = "", cause: BaseException | None = None
    ) -> None:
        super().__init__(message, cause=cause)
        self.raw_text = raw_text


class FatalError(HarnessError):
    pass


class BudgetExceeded(FatalError):
    def __init__(self, message: str, *, tokens_used: int, usd_used: float) -> None:
        super().__init__(message)
        self.tokens_used = tokens_used
        self.usd_used = usd_used


class RefusalError(FatalError):
    def __init__(self, message: str, *, category: str | None) -> None:
        super().__init__(message)
        self.category = category


class StructuredOutputError(FatalError):
    """Raised when the repair cap is hit. Carries every attempt's validation error."""

    def __init__(self, message: str, *, attempts: int, errors: list[str]) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.errors = errors
