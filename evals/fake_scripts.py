"""Scripted Fake-provider behaviour that drives the T0 graph through the t001 task.

The responder keys on the request's role and whether a structured schema was asked for:
structured requests get JSON for the schema that was sent, tool-using turns follow a
per-role script. The sandbox root is read from the Coder's task message so the script
can use absolute paths.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from scio.harness.providers.fake import FakeReply
from scio.harness.types import CompletionRequest, StopReason, ToolCall

ROOT_RE = re.compile(r"Repository root: (?P<root>\S+)")

SLUGIFY_IMPL = '''"""textutils: small text helpers."""

import re


def slugify(text: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics into ``-`` and strip edge dashes."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''

SLUGIFY_TESTS = """from textutils import slugify


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_and_strips():
    assert slugify("--a  b__c--") == "a-b-c"


def test_slugify_lowercases():
    assert slugify("ABC") == "abc"
"""


def _schema_kind(schema: dict[str, object]) -> str:
    properties = schema.get("properties")
    props = set(properties.keys()) if isinstance(properties, dict) else set()
    if "next_action" in props:
        return "worker"
    if "plan_vs_done" in props:
        return "pr_body"
    if "objective_met" in props:
        return "gate"
    if "tier" in props:
        return "router"
    return "unknown"


def _tool(name: str, **inp: object) -> FakeReply:
    return FakeReply(
        text="",
        tool_calls=[
            ToolCall(
                id=f"call-{name}-{abs(hash(json.dumps(inp, sort_keys=True))) % 10_000}",
                name=name,
                input=dict(inp),
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _json(obj: dict[str, object]) -> FakeReply:
    return FakeReply(text=json.dumps(obj))


class ScriptedResponder:
    """Deterministic agent behaviour for t001. Also usable with overrides in tests."""

    def __init__(
        self,
        *,
        gate_verdict: str = "pass",
        coder_mode: str = "solve",
        router_tier: str = "T0",
    ) -> None:
        self.gate_verdict = gate_verdict
        self.coder_mode = coder_mode  # solve | idle | busy | design_question
        self.router_tier = router_tier
        self.counts: dict[str, int] = {}
        self.root: str | None = None

    def _n(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def __call__(self, req: CompletionRequest) -> FakeReply:
        role = req.role or "model"
        text = "\n".join(m.text() for m in req.messages)
        m = ROOT_RE.search(text)
        if m:
            self.root = m.group("root")
        if req.output_schema is not None:
            return self._structured(role, _schema_kind(req.output_schema))
        return self._turn(role)

    # -- structured outputs --------------------------------------------------

    def _structured(self, role: str, kind: str) -> FakeReply:
        if kind == "router":
            return _json(
                {
                    "tier": self.router_tier,
                    "rationale": "one small function with tests",
                    "confidence": 0.92,
                }
            )
        if kind == "gate":
            met = self.gate_verdict == "pass"
            return _json(
                {
                    "verdict": self.gate_verdict,
                    "objective_met": met,
                    "unmet_acceptance": [] if met else ["(scripted) criteria judged unmet"],
                    "reasoning": "scripted gate verdict",
                }
            )
        if kind == "pr_body":
            return _json(
                {
                    "summary": "Adds slugify to textutils with unit tests.",
                    "plan_vs_done": ["S1 implement slugify + tests"],
                    "test_evidence": "pytest -k slugify -> passed",
                    "risk_notes": "none",
                }
            )
        if kind == "worker":
            if role == "coder" and self.coder_mode == "design_question":
                return _json(
                    {
                        "step_id": "S1",
                        "summary": "blocked on a design decision",
                        "files_touched": [],
                        "confidence": "low",
                        "needs_design_decision": True,
                        "design_question": "Should slugify keep unicode letters?",
                        "next_action": "verify",
                        "tests_added": [],
                    }
                )
            solved = role == "coder" and self.coder_mode == "solve"
            return _json(
                {
                    "step_id": "S1",
                    "summary": "implemented slugify and tests"
                    if solved
                    else f"{role} turn finished",
                    "files_touched": [
                        f"{self.root}/textutils/__init__.py",
                        f"{self.root}/tests/test_slugify.py",
                    ]
                    if solved
                    else [],
                    "confidence": "high",
                    "needs_design_decision": False,
                    "next_action": "verify",
                    "tests_added": ["slugify"] if solved else [],
                }
            )
        return _json({})

    # -- tool-using turns ----------------------------------------------------

    def _turn(self, role: str) -> FakeReply:
        n = self._n(f"turn:{role}")
        root = self.root or "/work"
        if role == "coder":
            if self.coder_mode == "idle" or self.coder_mode == "design_question":
                return FakeReply(text="Nothing to do.")
            if self.coder_mode == "busy":
                return _tool("view_file", path=f"{root}/README.md", start_line=1)
            script: list[Callable[[], FakeReply]] = [
                lambda: _tool("repo_search", query="slugify", mode="concise"),
                lambda: _tool("view_file", path=f"{root}/textutils/__init__.py"),
                lambda: _tool(
                    "edit_file",
                    path=f"{root}/textutils/__init__.py",
                    old_text='"""textutils: small text helpers."""\n',
                    new_text=SLUGIFY_IMPL,
                ),
                lambda: _tool(
                    "edit_file",
                    path=f"{root}/tests/test_slugify.py",
                    old_text="",
                    new_text=SLUGIFY_TESTS,
                ),
                lambda: _tool("run_tests", filter="slugify"),
            ]
            if n <= len(script):
                return script[n - 1]()
            return FakeReply(text="Implemented slugify with tests; all green.")
        if role == "documenter":
            if n == 1:
                return _tool(
                    "edit_file",
                    path=f"{root}/CHANGELOG.md",
                    old_text="## [Unreleased]\n",
                    new_text="## [Unreleased]\n### Added\n- `slugify(text)` in `textutils`.\n",
                )
            return FakeReply(text="Docs updated.")
        return FakeReply(text=f"{role}: ok")
