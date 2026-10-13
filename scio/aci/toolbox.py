"""``ToolBox``: the tool set an agent holds, with specs for the model and a dispatcher.

Construction runs the capability check, so an unsafe toolset fails at compile time, not
at the first tool call. Every call is traced (``execute_tool`` span), validated against
the input model, and returned as text; errors come back as error results, never raised
into the model loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from scio.aci import runtime as R
from scio.aci import tools as T
from scio.aci.capabilities import Capability, check_toolset
from scio.aci.runtime import ToolContext
from scio.aci.sandbox.base import Sandbox
from scio.aci.tools import ToolError
from scio.harness.telemetry import execute_tool_span
from scio.harness.types import ToolResultPart, ToolSpec
from scio.prompt.wire_schema import wire_schema

log = logging.getLogger(__name__)

ToolFn = Callable[[Sandbox, BaseModel], str]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: ToolFn
    needs_context: bool = False
    """The function takes ``(sandbox, inp, ctx)`` with the toolbox's :class:`ToolContext`
    (the approved dependency list, the toolchain) as a third argument (M9)."""


def _wrap(fn: Callable[[Sandbox, object], str]) -> ToolFn:
    return fn


TOOLS: dict[str, ToolDef] = {
    "repo_search": ToolDef(
        "repo_search",
        "Search the repository with a regular expression. Use mode='concise' first to find files, "
        "then mode='detailed' or view_file for content. Narrow with path= or glob=.",
        T.RepoSearchInput,
        _wrap(T.repo_search),  # type: ignore[arg-type]
    ),
    "view_file": ToolDef(
        "view_file",
        "Show a 100-line numbered window of a file. Absolute path. "
        "Call again with start_line to page.",
        T.ViewFileInput,
        _wrap(T.view_file),  # type: ignore[arg-type]
    ),
    "edit_file": ToolDef(
        "edit_file",
        "Replace one exact occurrence of old_text with new_text, or create a file when "
        "old_text is empty. "
        "Python edits that fail to parse or lint are rejected and reverted.",
        T.EditFileInput,
        _wrap(T.edit_file),  # type: ignore[arg-type]
    ),
    "run_tests": ToolDef(
        "run_tests",
        "Run pytest and return failures only. Always pass filter= for the feature under work.",
        T.RunTestsInput,
        _wrap(T.run_tests),  # type: ignore[arg-type]
    ),
    "delete_file": ToolDef(
        "delete_file",
        "Delete one file (absolute path). Use it to remove a file you created by mistake or "
        "that a change makes obsolete; directories and .git are refused.",
        T.DeleteFileInput,
        _wrap(T.delete_file),  # type: ignore[arg-type]
    ),
    "install_deps": ToolDef(
        "install_deps",
        "Install packages from the product's approved dependency list into the product "
        "environment. A package not on the list is refused: ask for a design decision instead.",
        R.InstallDepsInput,
        _wrap(R.install_deps),  # type: ignore[arg-type]
        needs_context=True,
    ),
    "run_app": ToolDef(
        "run_app",
        "Start the app with the toolchain's app command (python -m <module> args), wait for a "
        "local health_url or for it to exit, return its output, and stop it.",
        R.RunAppInput,
        _wrap(R.run_app),  # type: ignore[arg-type]
        needs_context=True,
    ),
    "git_checkpoint": ToolDef(
        "git_checkpoint",
        "Stage all changes and commit with a Conventional Commit message. Returns the SHA.",
        T.GitCheckpointInput,
        _wrap(T.git_checkpoint),  # type: ignore[arg-type]
    ),
}


class ToolBox:
    def __init__(
        self,
        sandbox: Sandbox,
        tool_names: Iterable[str],
        *,
        human_gate: bool,
        extra_capabilities: dict[str, Iterable[Capability]] | None = None,
        max_calls: int | None = None,
        role: str = "agent",
        context: ToolContext | None = None,
    ) -> None:
        self.role = role
        self.context = context or ToolContext()
        names = list(tool_names)
        unknown = [n for n in names if n not in TOOLS]
        if unknown:
            raise KeyError(f"unknown tools: {unknown}; known: {sorted(TOOLS)}")
        self.capabilities = check_toolset(names, human_gate=human_gate, extra=extra_capabilities)
        self.sandbox = sandbox
        self.tools = {n: TOOLS[n] for n in names}
        self.max_calls = max_calls
        self.calls = 0

    def specs(self) -> list[ToolSpec]:
        """Provider-neutral tool definitions, in a stable order (cacheable prefix)."""
        return [
            ToolSpec(
                name=d.name, description=d.description, input_schema=wire_schema(d.input_model)
            )
            for d in self.tools.values()
        ]

    def call(self, tool_use_id: str, name: str, raw_input: dict[str, object]) -> ToolResultPart:
        self.calls += 1
        with execute_tool_span(name, call_index=self.calls, role=self.role):
            if self.max_calls is not None and self.calls > self.max_calls:
                return ToolResultPart(
                    tool_use_id=tool_use_id,
                    content=(
                        f"tool call budget exhausted ({self.max_calls}); finish with your summary"
                    ),
                    is_error=True,
                )
            d = self.tools.get(name)
            if d is None:
                return ToolResultPart(
                    tool_use_id=tool_use_id,
                    content=f"unknown tool {name!r}; available: {sorted(self.tools)}",
                    is_error=True,
                )
            try:
                inp = d.input_model.model_validate(raw_input)
            except ValidationError as exc:
                return ToolResultPart(
                    tool_use_id=tool_use_id,
                    content=(
                        f"invalid input for {name}: {exc.errors()[0]['msg']} "
                        f"at {exc.errors()[0]['loc']}"
                    ),
                    is_error=True,
                )
            try:
                if d.needs_context:
                    content = d.fn(self.sandbox, inp, self.context)  # type: ignore[call-arg]
                else:
                    content = d.fn(self.sandbox, inp)
                return ToolResultPart(tool_use_id=tool_use_id, content=content)
            except ToolError as exc:
                return ToolResultPart(tool_use_id=tool_use_id, content=str(exc), is_error=True)
            except Exception as exc:
                log.exception("tool %s crashed", name)
                return ToolResultPart(
                    tool_use_id=tool_use_id,
                    content=f"{name} failed: {type(exc).__name__}: {str(exc)[:300]}",
                    is_error=True,
                )
