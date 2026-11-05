"""T1 localize: the explorer's files join the plan (I-151), and a stored summary stays valid
JSON with its files under the 1.5k-token cap (I-152).

The excerpts are copied verbatim from the graphcalc run's artifacts (run log S8):
``.scio/artifacts/build-graphcalc-m1-p4-a2/explore-localize.txt`` (relevant_files, finding 1)
and ``build-graphcalc-m2-p7-a1/explore-localize.txt`` (findings 2, 3, 5, 6, 8 and 9; that
artifact was cut before its relevant_files). The tracked lists are ``git ls-tree`` of the
product at each attempt's base commit (f2e8d3e for P4, 942dee3 for P7).
"""

from __future__ import annotations

import json

from scio.graph.nodes.planning import (
    SUMMARY_MAX_CHARS,
    add_localized_files,
    localized_files,
    summary_text,
)
from scio.loop.escalation import drifted_files
from scio.state.models import ExplorerSummary, Plan, PlanStep, Role
from scio.toolchain import get_toolchain

PY = get_toolchain("python")
ROOT = "/work/graphcalc"

_SHARED = [
    ".dockerignore",
    ".github/workflows/ci.yml",
    ".gitignore",
    ".scio/product/progress.json",
    "CHANGELOG.md",
    "Dockerfile",
    "LICENSE",
    "NOTES.md",
    "PRODUCT.md",
    "README.md",
    "graphcalc/__init__.py",
    "graphcalc/__main__.py",
    "graphcalc/calc.py",
    "graphcalc/cli.py",
    "graphcalc/numfmt.py",
    "graphcalc/static/app.js",
    "graphcalc/static/index.html",
    "graphcalc/static/style.css",
    "pyproject.toml",
    "tests/__init__.py",
    "tests/acceptance/__init__.py",
    "tests/acceptance/conftest.py",
    "tests/acceptance/test_acceptance.py",
    "tests/conftest.py",
    "tests/test_calc_evaluate.py",
    "tests/test_smoke.py",
    "tests/test_static.py",
]
TRACKED_P4 = sorted(_SHARED)
TRACKED_P7 = sorted(
    [
        *_SHARED,
        "graphcalc/history.py",
        "graphcalc/names.py",
        "tests/test_angle_mode.py",
        "tests/test_functions.py",
        "tests/test_history.py",
        "tests/test_p5_complex.py",
        "tests/test_p5_domain_error.py",
        "tests/test_p5_symbols_mode.py",
    ]
)

P4_RELEVANT_FILES = [
    "graphcalc/calc.py",
    "graphcalc/cli.py",
    "graphcalc/numfmt.py",
    "tests/test_calc_evaluate.py",
    "tests/conftest.py",
    "PRODUCT.md",
    "README.md",
    "pyproject.toml",
    ".scio/product/progress.json",
]
P4_FINDINGS = [
    "There is no file graphcalc/evaluator.py in the repo. The evaluator module is "
    "graphcalc/calc.py (confirmed by `find graphcalc -type f`). Any new work must target "
    "graphcalc/calc.py, not a new evaluator.py.",
]
P7_FINDINGS = [
    "graphcalc/state.py does not exist. Glob(**/state.py) found only a sympy vendor file under "
    ".scio/sandbox/venv; no state.py in graphcalc/ or the project root.",
    "graphcalc/server.py does not exist. The HTTP server/handler logic that a file named "
    "server.py would normally hold currently lives entirely in graphcalc/cli.py (278 lines): "
    "Handler class (cli.py:188-230), build_server() (cli.py:233-243), serve()/main() "
    "(cli.py:248-277), route handlers _handle_calc_evaluate (cli.py:140-186) and "
    "_handle_calc_history_get/_delete (cli.py:124-137), and static-file serving _serve_static "
    "(cli.py:58-92). Only /health, /, /static/<name>, POST /api/calc/evaluate, GET+DELETE "
    "/api/calc/history are routed (cli.py:202-227); there is no /api/calc/vars or "
    "/api/state/* route yet.",
    "PRODUCT.md (checked into graphcalc/) already pins the target behavior under feature P7 "
    "(PRODUCT.md:46-49): `name:=expr` defines a variable, usable later ('a:=5' then '2a+1' -> "
    "exact '11'), GET /api/calc/vars returns {'variables':{'a':'5'},'functions':{...}}, and "
    "redefining a reserved constant (e.g. 'pi:=3') must 400 with 'cannot redefine constant "
    "pi'. P9 (PRODUCT.md:55-59) separately pins disk persistence: --state PATH (default "
    "./graphcalc_state.json), atomic writes, reload on startup, --in-memory disables writes, "
    "and the repo's .gitignore must list graphcalc_state.json.",
    ".scio/product/progress.json shows only milestone M1 (features P1-P4) is 'done' "
    "(progress.json:5-22,25-64); P5 ('Non-real results / domain_error / redefining a "
    "constant'), P6 ('history + ans'), P7 ('variable/function definitions'), P8 and P9 "
    "('disk-backed state') are all still 'status: todo' (progress.json:66-114). Code for "
    "P5/P6's groundwork (graphcalc/history.py, graphcalc/names.py, calc.py's domain_error/ans "
    "handling) already exists even though progress.json marks those todo; P7 (assignment) and "
    "P9 (state file) have no code at all yet -- confirming state.py, server.py and "
    "test_calc_definitions.py are files S1 would create fresh, not files to edit.",
    "graphcalc/names.py:check_not_constant(name) (names.py:24-31) already raises "
    "calc.CalcError(f'cannot redefine constant {name}') for any name in RESERVED_NAMES = "
    "frozenset(_rad_names()) | {'i','oo','nan'} (names.py:21), built from "
    "graphcalc.calc._rad_names() rather than a hand-copied list (names.py:13-16,19-21). Its "
    "docstring says it has 'no production caller yet' and exists only so P7's `:=` can call "
    "it (names.py:8-11) -- new assignment code should call check_not_constant before storing "
    "a variable.",
    "graphcalc/cli.py:_handle_calc_evaluate (cli.py:140-186) is the pattern new assignment "
    "handling must fit: read+validate JSON body (cli.py:161-172), call into the calc module "
    "catching only CalcError as a 400 (cli.py:174-178), append to history only when "
    "domain_error is None (cli.py:179-182), and wrap everything in a catch-all except "
    "Exception -> 400 (cli.py:160,184-185) so this endpoint 'must never answer with a 500' "
    "(cli.py:156).",
]
# plan-v1.txt of P7 attempt 1, step S1 (S2 names the same files)
P7_PLANNED = [
    "graphcalc/calc.py",
    "graphcalc/state.py",
    "graphcalc/server.py",
    "tests/test_calc_definitions.py",
]


def _summary(
    findings: list[str], files: list[str], questions: list[str] | None = None
) -> ExplorerSummary:
    return ExplorerSummary(
        question="For plan step S1: what do the planned files contain?",
        findings=findings,
        relevant_files=files,
        open_questions=questions or [],
    )


# ---------- I-151: which files the explorer localized ----------


def test_p4_relevant_files_yield_the_source_files_not_docs_or_bookkeeping() -> None:
    got = localized_files(
        _summary(P4_FINDINGS, P4_RELEVANT_FILES), tracked=TRACKED_P4, is_source=PY.is_source
    )
    assert got == [
        "graphcalc/calc.py",
        "graphcalc/cli.py",
        "graphcalc/numfmt.py",
        "tests/test_calc_evaluate.py",
        "tests/conftest.py",
    ]
    for left_out in ("PRODUCT.md", "README.md", "pyproject.toml", ".scio/product/progress.json"):
        assert left_out not in got
    assert "graphcalc/evaluator.py" not in got  # the plan's guess, which does not exist


def test_p7_findings_citations_yield_cli_and_names() -> None:
    """The P7 artifact lost its relevant_files to the old cut; the citations alone name the
    files the coder went on to edit, as ``path:symbol`` or a bare ``name.py:line``."""
    got = localized_files(_summary(P7_FINDINGS, []), tracked=TRACKED_P7, is_source=PY.is_source)
    assert got == ["graphcalc/cli.py", "graphcalc/names.py"]


def test_citation_forms_are_normalized_to_the_tracked_spelling() -> None:
    win_root = "C:\\runs\\graphcalc"
    s = _summary(
        [
            "see C:\\runs\\graphcalc\\graphcalc\\calc.py:12 and ./graphcalc/numfmt.py:3",
            "the handler (cli.py:140) and `tests/test_history.py::test_round_trip`",
            "ambiguous bare names stay out: conftest.py:1, __init__.py:1",
            "not citations: 127.0.0.1:8000, 19:30 UTC, graphcalc.calc.evaluate: a function",
        ],
        ["C:/runs/graphcalc/graphcalc/history.py", "graphcalc/names.py:24 (check_not_constant)"],
    )
    got = localized_files(s, tracked=TRACKED_P7, is_source=PY.is_source, root=win_root)
    assert got == [
        "graphcalc/history.py",
        "graphcalc/names.py",
        "graphcalc/calc.py",
        "graphcalc/numfmt.py",
        "graphcalc/cli.py",
        "tests/test_history.py",
    ]


def _p7_plan() -> Plan:
    return Plan(
        author=Role.PLANNER,
        rationale="P7 attempt 1",
        steps=[
            PlanStep(
                id="S1", description="variables", feature_ids=["F1"], files_expected=P7_PLANNED
            ),
            PlanStep(
                id="S2", description="functions", feature_ids=["F2"], files_expected=P7_PLANNED
            ),
            PlanStep(
                id="S3", description="done", feature_ids=["F3"], files_expected=["a.py"], done=True
            ),
            PlanStep(id="S4", description="no files named", feature_ids=["F4"], files_expected=[]),
        ],
    )


def test_localized_files_join_every_open_step_and_planned_files_stay() -> None:
    files = localized_files(
        _summary(P7_FINDINGS, ["graphcalc/calc.py"]), tracked=TRACKED_P7, is_source=PY.is_source
    )
    plan, added = add_localized_files(_p7_plan(), files, root=ROOT)
    s1, s2, s3, s4 = plan.steps
    assert s1.files_expected == [*P7_PLANNED, "graphcalc/cli.py", "graphcalc/names.py"]
    assert s2.files_expected == s1.files_expected
    assert s3.files_expected == ["a.py"] and s4.files_expected == []  # done; drift off
    assert added == {
        "S1": ["graphcalc/cli.py", "graphcalc/names.py"],
        "S2": ["graphcalc/cli.py", "graphcalc/names.py"],
    }
    assert plan.version == 1  # the same plan, not a revision


def test_p7_edits_no_longer_drift_but_an_unnamed_source_file_still_does() -> None:
    """S8: the coder edited calc.py, cli.py and names.py and created state.py and the test
    file. Before the fix cli.py and names.py drifted; a file nobody named still must."""
    files = localized_files(_summary(P7_FINDINGS, []), tracked=TRACKED_P7, is_source=PY.is_source)
    plan, _ = add_localized_files(_p7_plan(), files, root=ROOT)
    touched = [
        f"{ROOT}/graphcalc/calc.py",
        f"{ROOT}/graphcalc/cli.py",
        f"{ROOT}/graphcalc/names.py",
        f"{ROOT}/graphcalc/state.py",
        f"{ROOT}/tests/test_calc_definitions.py",
    ]
    dirs = PY.test_dirs
    assert drifted_files(touched, P7_PLANNED, root=ROOT, test_dirs=dirs) == [
        "graphcalc/cli.py",
        "graphcalc/names.py",
    ]
    expected = plan.steps[0].files_expected
    assert drifted_files(touched, expected, root=ROOT, test_dirs=dirs) == []
    unnamed = [*touched, f"{ROOT}/graphcalc/__main__.py", f"{ROOT}/README.md"]
    assert drifted_files(unnamed, expected, root=ROOT, test_dirs=dirs) == [
        "graphcalc/__main__.py",
        "README.md",
    ]


# ---------- I-152: the stored summary keeps its files and stays valid JSON ----------


def test_a_summary_under_the_cap_is_stored_unchanged() -> None:
    s = _summary(P4_FINDINGS, P4_RELEVANT_FILES, ["Is evaluator.py meant to exist?"])
    assert summary_text(s) == json.dumps(s.model_dump(), indent=1)


def test_an_oversized_summary_keeps_relevant_files_and_open_questions_intact() -> None:
    findings = [f"graphcalc/calc.py:{i} fact {i}: " + "x" * 400 for i in range(40)]
    files = P4_RELEVANT_FILES + [f"graphcalc/mod_{i}.py" for i in range(10)]
    questions = ["Should state.py hold variables?", "Where does /api/calc/vars live?"]
    s = _summary(findings, files, questions)
    assert len(json.dumps(s.model_dump(), indent=1)) > SUMMARY_MAX_CHARS
    text = summary_text(s)
    assert len(text) <= SUMMARY_MAX_CHARS
    data = json.loads(text)
    assert data["relevant_files"] == files and data["open_questions"] == questions
    assert data["question"] == s.question
    kept, note = data["findings"][:-1], data["findings"][-1]
    assert kept and kept == findings[: len(kept)]
    assert note.startswith("[trimmed to the 1.5k-token cap:")
    assert f"{len(findings) - len(kept)} of {len(findings)} findings dropped" in note
    assert ExplorerSummary.model_validate_json(text).relevant_files == files


def test_one_giant_finding_is_shortened_and_the_json_stays_valid() -> None:
    s = _summary(['quote " and backslash \\ and unicode \u00e9 ' * 800], ["graphcalc/cli.py"])
    text = summary_text(s)
    assert len(text) <= SUMMARY_MAX_CHARS
    data = json.loads(text)
    assert data["relevant_files"] == ["graphcalc/cli.py"]
    assert data["findings"][0].endswith("...") and "shortened" in data["findings"][-1]


def test_files_that_alone_exceed_the_cap_are_cut_last_and_said_so() -> None:
    files = [f"pkg/module_{i:04d}/implementation.py" for i in range(300)]
    s = _summary(["a fact"] * 5, files, ["q?"])
    text = summary_text(s)
    assert len(text) <= SUMMARY_MAX_CHARS
    data = json.loads(text)
    assert (
        data["open_questions"] == []
        and data["relevant_files"] == files[: len(data["relevant_files"])]
    )
    assert "relevant_files dropped" in data["findings"][-1]
