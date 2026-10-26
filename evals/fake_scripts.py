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
    debug_edits: list[Edit] = field(default_factory=list)
    """What the debugger applies when verify fails after a correct step (t009: a lint failure
    in a file the step never touched). Without these the debugger would replay ``edits``."""


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


STORE_ITEMS = "    def items(self):\n        return dict(sorted(self._items.items()))\n"
STORE_REMOVE = (
    STORE_ITEMS
    + "\n    def remove(self, name, qty=None):\n"
    + "        if name not in self._items:\n            raise KeyError(name)\n"
    + "        have = self._items[name]\n        if qty is None:\n            qty = have\n"
    + "        if qty <= 0 or qty > have:\n"
    + '            raise ValueError("qty must be between 1 and the stock")\n'
    + "        left = have - qty\n        if left:\n            self._items[name] = left\n"
    + "        else:\n            del self._items[name]\n"
)
STORE_REMOVE_TESTS = (
    "import pytest\n\nfrom inventory.store import Store\n\n\ndef test_remove_store():\n"
    "    s = Store()\n    s.add('bolt', 3)\n    s.remove('bolt', 1)\n"
    "    assert s.items() == {'bolt': 2}\n    s.remove('bolt')\n    assert s.items() == {}\n"
    "    with pytest.raises(KeyError):\n        s.remove('bolt')\n"
)
CLI_LIST_PARSER = "    sub.add_parser('list')\n"
CLI_REMOVE_PARSER = (
    CLI_LIST_PARSER + "    rm = sub.add_parser('remove')\n    rm.add_argument('name')\n"
    "    rm.add_argument('--qty', type=int)\n"
)
CLI_LIST_BRANCH = "    elif args.cmd == 'list':\n"
CLI_REMOVE_BRANCH = (
    "    elif args.cmd == 'remove':\n        store.remove(args.name, args.qty)\n"
    "        print(f'removed {args.name}' if args.qty is None "
    "else f'removed {args.qty} {args.name}')\n" + CLI_LIST_BRANCH
)
CLI_REMOVE_TESTS = (
    "from inventory.cli import main\nfrom inventory.store import Store\n\n\n"
    "def test_remove_cli(capsys):\n    s = Store()\n    s.add('bolt', 3)\n"
    "    assert main(['remove', 'bolt', '--qty', '2'], store=s) == 0\n"
    "    assert main(['remove', 'bolt'], store=s) == 0\n"
    "    assert capsys.readouterr().out == 'removed 2 bolt\\nremoved bolt\\n'\n"
)

WITHDRAW_OLD = (
    '            raise ValueError("insufficient funds")\n        self.balance += amount\n'
)
WITHDRAW_NEW = (
    '            raise ValueError("insufficient funds")\n        self.balance -= amount\n'
)
LEDGER_IMPORT_OLD = '"""ledger: a minimal account balance."""\n\nimport os\n\n\nclass Account:\n'
LEDGER_IMPORT_NEW = '"""ledger: a minimal account balance."""\n\n\nclass Account:\n'

PAD_OLD = "def _pad(cell, width):\n    return cell.ljust(width)\n"
PAD_NEW = (
    "def _is_number(text):\n    try:\n        float(text)\n    except ValueError:\n"
    "        return False\n    return True\n\n\n"
    "def _pad(cell, width):\n"
    "    return cell.rjust(width) if _is_number(cell) else cell.ljust(width)\n"
)
DEMO_OLD = "item   | qty | price\nwidget | 3   | 2.5\ngear   | 12  | 10.0\n"
DEMO_NEW = "item   | qty | price\nwidget |   3 |   2.5\ngear   |  12 |  10.0\n"
MEDIAN_TAIL = "    return float(s[mid])\n"
STDEV_DEF = (
    "\n\ndef stdev(values):\n    if not values:\n"
    '        raise ValueError("stdev of an empty sequence")\n'
    "    m = mean(values)\n"
    "    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5\n"
)
STATS_TEST_HEAD = "from metrics.stats import mean, median\n"
STDEV_TESTS = (
    "from metrics.stats import stdev\n\n\ndef test_stdev_population():\n"
    "    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == 2.0\n\n\n"
    "def test_stdev_empty():\n    with pytest.raises(ValueError):\n        stdev([])\n"
)
REPORT_IMPORT_OLD = "from metrics.stats import mean, median\n"
REPORT_IMPORT_NEW = "from metrics.stats import DECIMALS, mean, median\n"

MONEY_OLD = (
    "def format_cents(cents):\n"
    '    """Format an integer amount of cents as dollars: 1234 -> "$12.34"."""\n'
    '    sign = "-" if cents < 0 else ""\n'
    "    cents = abs(cents)\n"
    '    return f"{sign}${cents // 100}.{cents % 100:02d}"\n'
)
MONEY_NEW = (
    'SYMBOLS = {"USD": "$", "EUR": "EUR ", "GBP": "GBP "}\n\n\n'
    'def format_money(cents, currency="USD"):\n'
    '    """Format an integer amount of cents in ``currency``: 1234 -> "$12.34"."""\n'
    "    if currency not in SYMBOLS:\n"
    '        raise ValueError(f"unknown currency {currency!r}")\n'
    '    sign = "-" if cents < 0 else ""\n'
    "    cents = abs(cents)\n"
    '    return f"{sign}{SYMBOLS[currency]}{cents // 100}.{cents % 100:02d}"\n'
)
# Mirrors the t010 fixture modules (signature, body expression, docstring); keep in sync with
# evals/tasks/t010_wide_rename/repo/shop/*.py.
MONEY_CALLERS: dict[str, tuple[str, str, str]] = {
    "cart": ("cart_total_line(items)", 'f"total {format_cents(sum(items))}"', "Cart totals."),
    "checkout": ("checkout_summary(cents)", 'f"pay {format_cents(cents)}"', "Checkout messages."),
    "invoice": ("invoice_line(desc, cents)", 'f"{desc}: {format_cents(cents)}"', "Invoice lines."),
    "receipt": ("receipt_footer(cents)", 'f"paid {format_cents(cents)}, thank you"', "Receipts."),
    "refund": ("refund_message(cents)", 'f"refunded {format_cents(cents)}"', "Refund notices."),
    "tax": ("tax_line(cents, rate)", 'f"tax {format_cents(round(cents * rate))}"', "Tax lines."),
    "discount": (
        "discount_line(cents, pct)",
        'f"discount {format_cents(round(cents * pct / 100))}"',
        "Discount lines.",
    ),
    "report": (
        "daily_report(totals)",
        '"\\n".join(f"{day} {format_cents(c)}" for day, c in totals.items())',
        "Daily report.",
    ),
    "cli": ("main(argv)", "print(format_cents(int(argv[0]))) or 0", "Command line."),
}


def _caller_source(mod: str, fn: str) -> str:
    sig, expr, doc = MONEY_CALLERS[mod]
    body = expr.replace("format_cents(", f"{fn}(")
    return f'"""{doc}"""\n\nfrom shop.money import {fn}\n\n\ndef {sig}:\n    return {body}\n'


MONEY_TESTS_OLD = (
    "from shop.money import format_cents\n\n\ndef test_format_cents():\n"
    '    assert format_cents(1234) == "$12.34"\n    assert format_cents(5) == "$0.05"\n'
    '    assert format_cents(-250) == "-$2.50"\n'
)
MONEY_TESTS_NEW = (
    "import pytest\n\nfrom shop.money import format_money\n\n\ndef test_format_money_usd():\n"
    '    assert format_money(1234) == "$12.34"\n    assert format_money(5) == "$0.05"\n'
    '    assert format_money(-250) == "-$2.50"\n\n\ndef test_format_money_currencies():\n'
    '    assert format_money(1234, "EUR") == "EUR 12.34"\n'
    '    assert format_money(-250, "GBP") == "-GBP 2.50"\n'
    "    with pytest.raises(ValueError):\n"
    '        format_money(1, "XXX")\n'
)


def _rename_edits() -> list[Edit]:
    """money.py, then one whole-file edit per caller, then the tests: eleven files, which
    trips the impact detector's file count (t010, ADR 0008). A caller must change in one
    edit: ``edit_file`` lints each edit and rejects an import or a call site changed alone
    (undefined name)."""
    edits = [Edit("shop/money.py", MONEY_OLD, MONEY_NEW)]
    for mod in MONEY_CALLERS:
        edits.append(
            Edit(
                f"shop/{mod}.py",
                _caller_source(mod, "format_cents"),
                _caller_source(mod, "format_money"),
            )
        )
    edits.append(Edit("tests/test_money.py", MONEY_TESTS_OLD, MONEY_TESTS_NEW))
    return edits


ALIGN_TESTS = (
    "from textfmt.table import render_table\n\n\ndef test_align_numbers_right():\n"
    "    assert render_table([['a', '1'], ['bbb', '22']]) == 'a   |  1\\nbbb | 22'\n"
)


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
    TaskScript(
        key="inventory",
        tier="T1",
        features=[
            ("F1", "Store.remove", "remove_store"),
            ("F2", "CLI remove subcommand", "remove_cli"),
        ],
        steps=[
            Step(
                "S1",
                "add Store.remove with tests",
                ["F1"],
                ["inventory/store.py"],
                edits=[
                    Edit("inventory/store.py", STORE_ITEMS, STORE_REMOVE),
                    Edit("tests/test_remove_store.py", "", STORE_REMOVE_TESTS),
                ],
                test_filter="remove_store",
            ),
            Step(
                "S2",
                "add the remove subcommand to the CLI with tests",
                ["F2"],
                ["inventory/cli.py"],
                edits=[
                    Edit("inventory/cli.py", CLI_LIST_PARSER, CLI_REMOVE_PARSER),
                    Edit("inventory/cli.py", CLI_LIST_BRANCH, CLI_REMOVE_BRANCH),
                    Edit("tests/test_remove_cli.py", "", CLI_REMOVE_TESTS),
                ],
                test_filter="remove_cli",
            ),
        ],
        docs=[changelog("`remove` in the store and the `remove` CLI subcommand.")],
    ),
    TaskScript(
        key="ci green",
        tier="T1",
        features=[
            ("F1", "withdraw reduces the balance", "withdraw"),
            ("F2", "ruff clean under the repo config", "tests/test_ledger.py"),
        ],
        steps=[
            Step(
                "S1",
                "fix withdraw to subtract",
                ["F1"],
                ["ledger/__init__.py"],
                edits=[Edit("ledger/__init__.py", WITHDRAW_OLD, WITHDRAW_NEW)],
                test_filter="withdraw",
            ),
            Step(
                "S2",
                "remove the unused import that fails ruff",
                ["F2"],
                ["ledger/__init__.py"],
                edits=[Edit("ledger/__init__.py", LEDGER_IMPORT_OLD, LEDGER_IMPORT_NEW)],
                test_filter="tests/test_ledger.py",
                tests_added=["tests/test_ledger.py"],
            ),
        ],
        docs=[changelog("fix: `withdraw` subtracts; unused import removed so CI lint passes.")],
    ),
    TaskScript(
        key="right-align",
        tier="T1",
        features=[
            ("F1", "numeric cells right-aligned", "align"),
            ("F2", "snapshot and README regenerated", "snapshot or readme"),
        ],
        steps=[
            Step(
                "S1",
                "right-align numeric cells; regenerate the snapshot and the README example",
                ["F1", "F2"],
                ["textfmt/table.py", "tests/golden/demo.txt", "README.md"],
                edits=[
                    Edit("textfmt/table.py", PAD_OLD, PAD_NEW),
                    Edit("tests/golden/demo.txt", DEMO_OLD, DEMO_NEW),
                    Edit("README.md", DEMO_OLD, DEMO_NEW),
                    Edit("tests/test_align.py", "", ALIGN_TESTS),
                ],
                test_filter="align",
                tests_added=["align", "snapshot or readme"],
            )
        ],
        docs=[changelog("`render_table` right-aligns numeric cells.")],
    ),
    TaskScript(
        key="standard deviation",
        tier="T1",
        features=[
            ("F1", "population stdev with ValueError on empty input", "stdev"),
            ("F2", "stdev covered by tests", "stdev"),
            ("F3", "CI workflow passes", "stdev or stats"),
        ],
        steps=[
            Step(
                "S1",
                "add stdev to metrics.stats with tests",
                ["F1", "F2", "F3"],
                ["metrics/stats.py", "tests/test_stats.py"],
                edits=[
                    Edit("metrics/stats.py", MEDIAN_TAIL, MEDIAN_TAIL + STDEV_DEF),
                    Edit("tests/test_stats.py", STATS_TEST_HEAD, STATS_TEST_HEAD + STDEV_TESTS),
                ],
                test_filter="stdev",
                # verify's lint step finds the undefined name in metrics/report.py, a file the
                # step never touched; the debugger wires the constant instead of deleting it.
                debug_edits=[Edit("metrics/report.py", REPORT_IMPORT_OLD, REPORT_IMPORT_NEW)],
            )
        ],
        docs=[changelog("`stdev` (population standard deviation) in `metrics.stats`.")],
    ),
    TaskScript(
        key="format_money",
        tier="T1",
        features=[
            ("F1", "format_money with currency symbols and ValueError", "format_money"),
            ("F2", "every caller migrated, old name gone", "format_money or test_modules"),
            ("F3", "tests cover the new function", "format_money"),
        ],
        steps=[
            Step(
                "S1",
                "rename format_cents to format_money(cents, currency) and migrate every caller",
                ["F1", "F2", "F3"],
                ["shop/money.py", *(f"shop/{m}.py" for m in MONEY_CALLERS)],
                edits=_rename_edits(),
                test_filter="format_money or test_modules",
            )
        ],
        docs=[changelog("`format_money(cents, currency)` replaces `format_cents`.")],
    ),
]


def script_for(title: str, stack: str = "python") -> TaskScript:
    from evals.product_scripts import PRODUCT_SCRIPTS  # product features first: "json" collides
    from evals.product_scripts_http import HTTP_PRODUCT_SCRIPTS
    from evals.product_scripts_node import NODE_PRODUCT_SCRIPTS

    low = title.lower()
    # The same feature titles exist in two stacks (M10): the task's stack picks the script.
    product_scripts = NODE_PRODUCT_SCRIPTS if stack == "node-ts" else []
    for s in [*product_scripts, *PRODUCT_SCRIPTS, *HTTP_PRODUCT_SCRIPTS, *SCRIPTS]:
        if s.key in low:
            return s
    raise KeyError(f"no fake script for task title {title!r}")


OPEN_FEATURES_RE = re.compile(r"^Open features: (?P<ids>.+)$", re.MULTILINE)
MILESTONE_NUMBER_RE = re.compile(r"Propose milestone M(?P<n>\d+)")


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
        ("interview_questions", "interviewer"),  # product mode (ADR 0010)
        ("product_name", "product_spec"),
        ("milestone_title", "milestone_plan"),  # product mode (ADR 0011)
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
        milestone_gate_verdict: str = "pass",
        milestone_size: int = 2,
        product: str = "todo_cli",
        stack: str = "python",
    ) -> None:
        self.product = product
        """Which scripted product the planner drafts: ``todo_cli`` (a CLI, standard library
        only) or ``todo_api`` (an http server with an approved dependency, M9)."""
        self.stack = stack
        """The stack the scripted planner names and the scripted features are written in
        (``python`` or ``node-ts``, M10): the same ``todo_api`` spec builds in both."""
        self.gate_verdict = gate_verdict
        self.gate_verdicts = list(gate_verdicts or [])
        """Per-call gate verdicts in order; ``gate_verdict`` applies once they run out."""
        self.milestone_gate_verdict = milestone_gate_verdict
        """The product build's milestone gate (ADR 0011), separate from the feature gates."""
        self.milestone_size = milestone_size
        """How many open features the scripted milestone planner groups per milestone."""
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
        if t and (self.script is None or self.script.key not in t.group("title").lower()):
            # A product build runs several tasks through one responder (ADR 0011): a new
            # task title switches the script and restarts the per-task counters.
            self.script = script_for(t.group("title"), stack=self.stack)
            self.counts = {}
        if req.output_schema is not None:
            return self._structured(role, _schema_kind(req.output_schema), text)
        return self._turn(role, text)

    # -- structured outputs ----------------------------------------------------

    def _structured(self, role: str, kind: str, text: str) -> FakeReply:
        sc = self.script
        if kind == "interviewer":
            return _json(
                {
                    "interview_questions": [
                        "Who uses this first, and on what platform?",
                        "Which single workflow must work on day one?",
                        "What is explicitly out of scope for the first release?",
                    ],
                    "rationale": "scripted interview",
                }
            )
        if kind == "product_spec" and self.product == "todo_api":
            from evals.product_scripts_http import DEPENDENCY

            return _json(
                {
                    "product_name": "Todo API",
                    "title": "Todo API",
                    "vision": "A small HTTP todo service with a YAML store.",
                    "users": ["scripts and front ends that need a todo list over HTTP"],
                    "features": [
                        {
                            "id": "F1",
                            "description": "Health and version endpoint",
                            "acceptance": ["GET /health returns status ok and the version"],
                        },
                        {
                            "id": "F2",
                            "description": "Create and list todos",
                            "acceptance": ["POST /todos then GET /todos returns the todo as JSON"],
                        },
                    ],
                    "quality_bar": ["tests pass", "lint clean"],
                    "stack": self.stack,
                    # the Python product borrows pyyaml from SCIO's environment; the Node one
                    # has no host environment to borrow from and stores JSON with node:fs
                    "dependencies": [DEPENDENCY] if self.stack == "python" else [],
                    "app": {
                        "kind": "http",
                        "module": "todo_api" if self.stack == "python" else None,
                        "args": ["serve", "--port", "{port}"],
                        "health_path": "/health",
                    },
                }
            )
        if kind == "product_spec":
            revised = "rejected" in text
            return _json(
                {
                    "product_name": "Todo CLI",  # normaliser slugs this to todo_cli
                    "title": "Todo CLI" + (" (revised)" if revised else ""),
                    "vision": "A command-line todo list with tags and JSON export.",
                    "users": ["people who live in a terminal"],
                    "features": [
                        {
                            "id": "F1",
                            "description": "Add and list todos",
                            "acceptance": ["`todo add x` then `todo list` prints x"],
                        },
                        {
                            "id": "F2",
                            "description": "Tags and JSON export",
                            "acceptance": ["`todo export --json` prints valid JSON with tags"],
                        },
                    ],
                    "quality_bar": ["tests pass with coverage >= 80%", "lint and types clean"],
                    "stack": "python",
                    "dependencies": [],
                }
            )
        if kind == "milestone_plan":
            open_m = OPEN_FEATURES_RE.search(text)
            open_ids = open_m.group("ids") if open_m else "P1"
            ids = [x.strip() for x in open_ids.split(",") if x.strip()]
            num_m = MILESTONE_NUMBER_RE.search(text)
            number = num_m.group("n") if num_m else "1"
            return _json(
                {
                    "milestone_title": f"Milestone {number}",
                    "features": [
                        {"id": fid, "tier": "T0" if fid == "P1" else "T1", "notes": ""}
                        for fid in ids[: self.milestone_size]
                    ],
                    "rationale": "scripted milestone",
                }
            )
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
            if "Verify passed" in text:
                # ADR 0008 pre-commit review of a passing high-impact diff: approve it.
                return _json(
                    {
                        "verdict": "continue",
                        "guidance": "Wide but mechanical change; tests green. Commit as is.",
                        "revised_steps": None,
                        "confidence": "high",
                    }
                )
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
            if text.startswith("# Milestone gate"):
                verdict = self.milestone_gate_verdict
            else:
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
        if role == "debugger" and step.debug_edits:
            return list(step.debug_edits)
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
