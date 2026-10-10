"""Structured output with local validation and bounded repair.

Flow per call: check ``stop_reason`` → parse JSON → validate the full Pydantic model.
On failure, re-ask with the error appended (assistant turn = the bad output, user turn
= the error), at most ``max_repairs`` times. Then raise :class:`StructuredOutputError`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from scio.harness.failures import RepairableError, StructuredOutputError
from scio.harness.types import CompletionRequest, CompletionResponse, Message, StopReason
from scio.prompt.wire_schema import wire_schema

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    attempts: int
    response: CompletionResponse
    repairs: list[str] = field(default_factory=list)
    """The validation errors that triggered each repair, in order."""


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def parse_structured[T: BaseModel](response: CompletionResponse, model_cls: type[T]) -> T:
    """Parse and validate one response, raising :class:`RepairableError` on any defect."""
    if response.stop_reason is StopReason.MAX_TOKENS:
        raise RepairableError(
            "output was truncated at max_tokens; respond with the complete JSON object, "
            "more concisely",
            raw_text=response.text,
        )
    text = _strip_fences(response.text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RepairableError(f"invalid JSON: {exc}", raw_text=response.text, cause=exc) from exc
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise RepairableError(
            f"schema validation failed:\n{exc}", raw_text=response.text, cause=exc
        ) from exc


def repair_message(error: RepairableError) -> Message:
    return Message.user(
        "Your previous output was rejected.\n"
        f"Reason: {error.message}\n"
        "Return only a corrected JSON object that satisfies the schema. No prose."
    )


def structured_call[T: BaseModel](
    call: Callable[[CompletionRequest], CompletionResponse],
    request: CompletionRequest,
    model_cls: type[T],
    *,
    max_repairs: int = 2,
) -> StructuredResult[T]:
    """Run ``call`` with a wire schema for ``model_cls`` and repair up to ``max_repairs`` times."""
    schema = request.output_schema or wire_schema(model_cls)
    req = request.model_copy(update={"output_schema": schema})
    repairs: list[str] = []
    attempts = 0
    while True:
        attempts += 1
        response = call(req)
        try:
            value = parse_structured(response, model_cls)
        except RepairableError as exc:
            repairs.append(exc.message)
            if attempts > max_repairs:
                raise StructuredOutputError(
                    f"{model_cls.__name__}: still invalid after {max_repairs} repairs",
                    attempts=attempts,
                    errors=repairs,
                ) from exc
            req = req.model_copy(
                update={
                    "messages": [
                        *req.messages,
                        Message.assistant(response.text or "(empty output)"),
                        repair_message(exc),
                    ]
                }
            )
            continue
        return StructuredResult(value=value, attempts=attempts, response=response, repairs=repairs)
