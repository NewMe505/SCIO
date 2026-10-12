"""Scripted Fake-provider behaviour that drives every tier through the eval tasks.

The responder is data-driven: a :class:`TaskScript` per task describes the plan, the
edits each step makes, and the docs edit. The responder keys on the requested output
schema first (router / scoper / planner / explorer / worker / advisor / reviewer / gate /
PR body), then on role, plan step and attempt for tool-using turns. Failure modes used by
tests are switches on the responder (``coder_mode``, ``advisor_verdict``, ``router_tier``).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

from scio.harness.providers.fake import FakeReply
from scio.harness.types import CompletionRequest, StopReason, ToolCall

ROOT_RE = re.compile(r"Repository root: (?P<root>\S+)")
TITLE_RE = re.compile(r"^# Task: (?P<title>.+)$", re.MULTILINE)
STEP_RE = re.compile(r"Plan step to execute: (?P<step>S\d+)")
ATTEMPT_RE = re.compile(r"Attempt: (?P<n>\d+)|Attempts on this error signature: (?P<k>\d+)")


@dataclass(frozen=True)
class Edit:
    path: str  # relative to the repository root
    old: str
    new: str


@dataclass(frozen=True)
class Step:
    id: str
    description: str
    feature_ids: list[str]
    files_expected: list[str]
    edits: list[Edit]
    test_filter: str
    wrong_edits: list[Edit] = field(default_factory=list)
    """Edits that leave the step failing (same error every time), for escalation tests."""
    fix_edit: Edit | None = None
    """Turns the wrong implementation into the right one (applied once guided by the advisor)."""
    tests_added: list[str] | None = None
    """What the worker reports in ``tests_added``; defaults to ``[test_filter]``."""


@dataclass(frozen=True)
class TaskScript:
    key: str
    tier: str
    features: list[tuple[str, str, str]]  # (id, description, test_filter)
    steps: list[Step]
    docs: list[Edit]
    questions: list[str] = field(default_factory=list)


# ---------- task scripts ----------

SLUGIFY_OLD = '"""textutils: small text helpers."""\n'
SLUGIFY_NEW = '''"""textutils: small text helpers."""

import re


def slugify(text: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics into ``-`` and strip edge dashes."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''
SLUGIFY_WRONG = '''"""textutils: small text helpers."""


def slugify(text: str) -> str:
    return text.lower()
'''
SLUGIFY_TESTS = """from textutils import slugify


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_and_strips():
    assert slugify("--a  b__c--") == "a-b-c"
"""

PAGER_OLD = "    for i in range(0, len(items) - 1, size):\n"
PAGER_NEW = "    for i in range(0, len(items), size):\n"
PAGER_WRONG = "    for i in range(1, len(items), size):\n"

CACHE_SIG = "    def get_or_set(self, key, factory, ttl=60.0):\n"
CACHE_DOC = (
    CACHE_SIG
    + '        """Return the cached value for ``key`` or compute it with ``factory``.\n\n'
    + "        Args:\n            key: cache key.\n            factory: zero-argument callable producing the value.\n"
    + "            ttl: seconds the value stays fresh.\n\n        Returns:\n            The cached or freshly computed value.\n"
    + '        """\n'
)
CACHE_TESTS = "from cache import Cache\n\n\ndef test_docs_present():\n    assert 'ttl' in (Cache.get_or_set.__doc__ or '')\n"

CIRCLE_NEW = '''"""shapes: area helpers."""

import math


def area_circle(r):
    if r < 0:
        raise ValueError("negative radius")
    return math.pi * r**2
'''
CIRCLE_WRONG = '''"""shapes: area helpers."""

import math


def area_circle(r):
    return math.pi * r
'''
RECT_ADD = """

def area_rect(w, h):
    if w < 0 or h < 0:
        raise ValueError("negative side")
    return w * h
"""
CIRCLE_TESTS = "import math\n\nimport pytest\n\nfrom shapes import area_circle\n\n\ndef test_circle():\n    assert area_circle(1) == pytest.approx(math.pi)\n    with pytest.raises(ValueError):\n        area_circle(-1)\n"
RECT_TESTS = "import pytest\n\nfrom shapes import area_rect\n\n\ndef test_rect():\n    assert area_rect(2, 3) == 6\n    with pytest.raises(ValueError):\n        area_rect(-1, 1)\n"

CLI_OLD = "    parser.add_argument('name')\n    args = parser.parse_args(argv)\n    print(greeting(args.name))\n    return 0\n"
CLI_NEW = (
    "    parser.add_argument('name')\n    parser.add_argument('--json', action='store_true')\n"
    "    args = parser.parse_args(argv)\n    if args.json:\n        import json\n\n"
    "        print(json.dumps({'name': args.name, 'greeting': greeting(args.name)}))\n"
    "    else:\n        print(greeting(args.name))\n    return 0\n"
)
CLI_TESTS = "import json\n\nfrom tool.cli import main\n\n\ndef test_json_flag(capsys):\n    assert main(['--json', 'Ada']) == 0\n    assert json.loads(capsys.readouterr().out)['name'] == 'Ada'\n"


def changelog(line: str) -> Edit:
    return Edit("CHANGELOG.md", "## [Unreleased]\n", f"## [Unreleased]\n### Added\n- {line}\n")


SCRIPTS: list[TaskScript] = [
    TaskScript(
        key="slugify",
        tier="T0",
        features=[("F1", "slugify", "slugify")],
        steps=[
            Step(
                "S1",
                "implement slugify with tests",
                ["F1"],
                ["textutils/__init__.py"],
                edits=[
                    Edit("textutils/__init__.py", SLUGIFY_OLD, SLUGIFY_NEW),
                    Edit("tests/test_slugify.py", "", SLUGIFY_TESTS),
                ],
                test_filter="slugify",
                wrong_edits=[
                    Edit("textutils/__init__.py", SLUGIFY_OLD, SLUGIFY_WRONG),
                    Edit("tests/test_slugify.py", "", SLUGIFY_TESTS),
                ],
                fix_edit=Edit("textutils/__init__.py", SLUGIFY_WRONG, SLUGIFY_NEW),
                tests_added=["tests/test_slugify.py::test_slugify_basic", "slugify"],
            )
        ],
        docs=[changelog("`slugify(text)` in `textutils`.")],
    ),
    TaskScript(
        key="paginate",
        tier="T1",
        features=[
            ("F1", "existing failing test passes", "paginate"),
            ("F2", "no other test changes status", "paginate"),
        ],
        steps=[
            Step(
                "S1",
                "fix the range bound in paginate",
                ["F1", "F2"],
                ["pager/__init__.py"],
                edits=[Edit("pager/__init__.py", PAGER_OLD, PAGER_NEW)],
                test_filter="paginate",
                wrong_edits=[Edit("pager/__init__.py", PAGER_OLD, PAGER_WRONG)],
                fix_edit=Edit("pager/__init__.py", PAGER_WRONG, PAGER_NEW),
            )
        ],
        docs=[changelog("fix: `paginate` keeps the last partial page.")],
    ),
    TaskScript(
        key="get_or_set",
        tier="T0",
        features=[
            ("F1", "docstring", "docs"),
            ("F2", "readme example", "docs"),
            ("F3", "changelog", "docs"),
        ],
        steps=[
            Step(
                "S1",
                "document get_or_set",
                ["F1", "F2", "F3"],
                ["cache/__init__.py", "README.md", "CHANGELOG.md"],
                edits=[
                    Edit("cache/__init__.py", CACHE_SIG, CACHE_DOC),
                    Edit(
                        "README.md",
                        "from cache import Cache\n",
                        "from cache import Cache\n\nc = Cache()\nc.get_or_set('k', lambda: 1, ttl=30)\n",
                    ),
                    changelog("docs: `Cache.get_or_set` documented."),
                    Edit("tests/test_docs.py", "", CACHE_TESTS),
                ],
                test_filter="docs",
            )
        ],
        docs=[],
    ),
    TaskScript(
        key="circle and rectangle",
        tier="T1",
        features=[("F1", "area_circle", "circle"), ("F2", "area_rect", "rect")],
        steps=[
            Step(
                "S1",
                "add area_circle with tests",
                ["F1"],
                ["shapes/__init__.py"],
                edits=[
                    Edit("shapes/__init__.py", '"""shapes: area helpers."""\n', CIRCLE_NEW),
                    Edit("tests/test_circle.py", "", CIRCLE_TESTS),
                ],
                test_filter="circle",
                wrong_edits=[
                    Edit("shapes/__init__.py", '"""shapes: area helpers."""\n', CIRCLE_WRONG),
                    Edit("tests/test_circle.py", "", CIRCLE_TESTS),
                ],
                fix_edit=Edit("shapes/__init__.py", CIRCLE_WRONG, CIRCLE_NEW),
            ),
            Step(
                "S2",
                "add area_rect with tests",
                ["F2"],
                ["shapes/__init__.py"],
                edits=[
                    Edit(
                        "shapes/__init__.py",
                        "    return math.pi * r**2\n",
                        "    return math.pi * r**2\n" + RECT_ADD,
                    ),
                    Edit("tests/test_rect.py", "", RECT_TESTS),
                ],
                test_filter="rect",
            ),
        ],
        docs=[changelog("`area_circle` and `area_rect` in `shapes`.")],
    ),
    TaskScript(
        key="json",
        tier="T2",
        features=[
            ("F1", "--json prints a JSON object", "json"),
            ("F2", "plain output unchanged", "json"),
        ],
        steps=[
            Step(
                "S1",
                "add a --json flag to the CLI",
                ["F1", "F2"],
                ["tool/cli.py"],
                edits=[
                    Edit("tool/cli.py", CLI_OLD, CLI_NEW),
                    Edit("tests/test_json.py", "", CLI_TESTS),
                ],
                test_filter="json",
            )
        ],
        docs=[changelog("`--json` flag for the greeting CLI.")],
        questions=[
            "Where is the CLI entry point and how are arguments parsed?",
            "How are the existing CLI tests written?",
        ],
    ),
]


def script_for(title: str) -> TaskScript:
    low = title.lower()
    for s in SCRIPTS:
        if s.key in low:
            return s
    raise KeyError(f"no fake script for task title {title!r}")


# ---------- helpers ----------


def _schema_kind(schema: dict[str, object]) -> str:
    properties = schema.get("properties")
    props = set(properties.keys()) if isinstance(properties, dict) else set()
    for marker, kind in (
        ("tier", "router"),
        ("exploration_questions", "scoper"),
        ("steps", "planner"),
        ("relevant_files", "explorer"),
        ("guidance", "advisor"),
        ("findings", "reviewer"),
        ("objective_met", "gate"),
        ("plan_vs_done", "pr_body"),
        ("next_action", "worker"),
    ):
        if marker in props:
            return kind
    return "unknown"


def _tool(name: str, **inp: object) -> FakeReply:
    ident = abs(hash(json.dumps(inp, sort_keys=True, default=str))) % 100_000
    return FakeReply(
        text="",
        tool_calls=[ToolCall(id=f"call-{name}-{ident}", name=name, input=dict(inp))],
        stop_reason=StopReason.TOOL_USE,
    )


def _json(obj: object) -> FakeReply:
    return FakeReply(text=json.dumps(obj))


def _edit_call(root: str, e: Edit) -> FakeReply:
    return _tool("edit_file", path=f"{root}/{e.path}", old_text=e.old, new_text=e.new)


# ---------- the responder ----------


class ScriptedResponder:
    """Deterministic agent behaviour for the eval tasks and the escalation scenarios.

    coder_mode: solve | idle | busy | design_question | wrong_first | drift
    """

    def __init__(
        self,
        *,
        gate_verdict: str = "pass",
        coder_mode: str = "solve",
        router_tier: str | None = None,
        advisor_verdict: str = "change_approach",
        gate_verdicts: list[str] | None = None,
    ) -> None:
        self.gate_verdict = gate_verdict
        self.gate_verdicts = list(gate_verdicts or [])
        """Per-call gate verdicts in order; ``gate_verdict`` applies once they run out."""
        self.coder_mode = coder_mode
        self.router_tier = router_tier
        self.advisor_verdict = advisor_verdict
        self.counts: dict[str, int] = {}
        self.root: str | None = None
        self.script: TaskScript | None = None
        self._lock = threading.Lock()

    def _n(self, key: str) -> int:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

    # -- dispatch --------------------------------------------------------------

    def __call__(self, req: CompletionRequest) -> FakeReply:
        role = req.role or "model"
        text = "\n".join(m.text() for m in req.messages)
        m = ROOT_RE.search(text)
        if m:
            self.root = m.group("root")
        t = TITLE_RE.search(text)
        if t and self.script is None:
            self.script = script_for(t.group("title"))
        if req.output_schema is not None:
            return self._structured(role, _schema_kind(req.output_schema), text)
        return self._turn(role, text)

    # -- structured outputs ----------------------------------------------------

    def _structured(self, role: str, kind: str, text: str) -> FakeReply:
        sc = self.script
        if kind == "router":
            tier = self.router_tier or (sc.tier if sc else "T0")
            return _json({"tier": tier, "rationale": f"scripted: {tier}", "confidence": 0.9})
        if kind == "scoper":
            qs = sc.questions if sc else ["Where are the tests?", "What are the conventions?"]
            feats = [
                {"id": i, "description": d, "test_filter": f}
                for i, d, f in (sc.features if sc else [])
            ]
            return _json({"exploration_questions": qs, "draft_features": feats})
        if kind == "planner":
            assert sc is not None
            return _json(
                {
                    "steps": [
                        {
                            "id": s.id,
                            "description": s.description,
                            "feature_ids": s.feature_ids,
                            "files_expected": s.files_expected,
                            "done": False,
                        }
                        for s in sc.steps
                    ],
                    "feature_list": [
                        {"id": i, "description": d, "test_filter": f, "status": "failing"}
                        for i, d, f in sc.features
                    ],
                    "rationale": "scripted plan",
                    "risks": ["none"],
                }
            )
        if kind == "explorer":
            return _json(
                {
                    "question": "scripted",
                    "findings": [f"{self.root}/README.md:1 project readme"],
                    "relevant_files": [f"{self.root}/README.md"],
                    "open_questions": [],
                }
            )
        if kind == "advisor":
            return _json(
                {
                    "verdict": self.advisor_verdict,
                    "guidance": "Use the correct implementation: replace the wrong block with the right one and rerun the tests.",
                    "revised_steps": None,
                    "confidence": "high",
                }
            )
        if kind == "reviewer":
            return _json({"findings": [], "overall": "approve"})
        if kind == "gate":
            n = self._n("gate")
            verdict = (
                self.gate_verdicts[n - 1] if n <= len(self.gate_verdicts) else self.gate_verdict
            )
            met = verdict == "pass"
            return _json(
                {
                    "verdict": verdict,
                    "objective_met": met,
                    "unmet_acceptance": [] if met else ["(scripted) criteria judged unmet"],
                    "reasoning": "scripted gate verdict",
                }
            )
        if kind == "pr_body":
            return _json(
                {
                    "summary": f"Scripted solution for {sc.key if sc else 'task'}.",
                    "plan_vs_done": [f"{s.id} {s.description}" for s in (sc.steps if sc else [])],
                    "test_evidence": "pytest -> passed",
                    "risk_notes": "none",
                }
            )
        if kind == "worker":
            return self._worker_output(role, text)
        return _json({})

    def _worker_output(self, role: str, text: str) -> FakeReply:
        step = self._step(text)
        if role == "coder" and self.coder_mode == "design_question":
            return _json(
                {
                    "step_id": step.id if step else "S1",
                    "summary": "blocked on a design decision",
                    "files_touched": [],
                    "confidence": "low",
                    "needs_design_decision": True,
                    "design_question": "Should unicode letters be kept?",
                    "next_action": "verify",
                    "tests_added": [],
                }
            )
        touched: list[str] = []
        tests: list[str] = []
        if (
            step is not None
            and role in ("coder", "debugger")
            and self.coder_mode not in ("idle", "busy")
        ):
            edits = self._edits_for(role, step, text)
            touched = sorted({f"{self.root}/{e.path}" for e in edits})
            tests = step.tests_added or [step.test_filter]
        return _json(
            {
                "step_id": step.id if step else "S1",
                "summary": f"{role} turn finished ({self.coder_mode})",
                "files_touched": touched,
                "confidence": "high",
                "needs_design_decision": False,
                "next_action": "verify",
                "tests_added": tests,
                "sweep": {
                    "pattern": "range(",
                    "files_checked": touched,
                    "files_fixed": touched,
                    "ruled_out": [],
                }
                if role == "debugger" and touched
                else None,
            }
        )

    # -- tool-using turns ------------------------------------------------------

    def _step(self, text: str) -> Step | None:
        if self.script is None:
            return None
        m = STEP_RE.search(text)
        sid = m.group("step") if m else self.script.steps[0].id
        return next((s for s in self.script.steps if s.id == sid), self.script.steps[0])

    def _edits_for(self, role: str, step: Step, text: str) -> list[Edit]:
        guided = "[context from advisor]" in text
        if self.coder_mode in ("wrong_first", "drift") and step.wrong_edits:
            if guided and step.fix_edit is not None:
                return [step.fix_edit]
            if role == "debugger":
                # a "fix" that changes nothing material: the same error survives
                return [
                    Edit(
                        step.files_expected[0],
                        step.wrong_edits[0].new,
                        step.wrong_edits[0].new.rstrip("\n") + "\n# retry\n",
                    )
                ]
            edits = list(step.wrong_edits)
            if self.coder_mode == "drift":
                edits.append(
                    Edit("README.md", "", "") if False else Edit("README.md", "# ", "# [drift] ")
                )
            return edits
        return list(step.edits)

    def _turn(self, role: str, text: str) -> FakeReply:
        root = self.root or "/work"
        if role in ("coder", "debugger"):
            if self.coder_mode in ("idle", "design_question"):
                return FakeReply(text="Nothing to do.")
            if self.coder_mode == "busy":
                return _tool("view_file", path=f"{root}/README.md", start_line=1)
            step = self._step(text)
            if step is None:
                return FakeReply(text="No script for this task.")
            a = ATTEMPT_RE.search(text)
            attempt = (a.group("n") or a.group("k")) if a else "1"
            n = self._n(
                f"{role}:{step.id}:{attempt}:{'g' if '[context from advisor]' in text else ''}"
            )
            edits = self._edits_for(role, step, text)
            if n <= len(edits):
                e = edits[n - 1]
                if e.old == "" and e.new == "":
                    return FakeReply(text="skip")
                return _edit_call(root, e)
            if n == len(edits) + 1:
                return _tool("run_tests", filter=step.test_filter)
            return FakeReply(text="Done with this step.")
        if role == "documenter":
            docs = self.script.docs if self.script else []
            n = self._n("documenter")
            if n <= len(docs):
                return _edit_call(root, docs[n - 1])
            return FakeReply(text="Docs updated.")
        if role == "explorer":
            n = self._n(f"explorer:{threading.get_ident()}")
            if n % 2 == 1:
                return _tool("repo_search", query="def ", mode="concise")
            return FakeReply(text="Explored.")
        return FakeReply(text=f"{role}: ok")
