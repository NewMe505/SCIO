"""Tool signals in the run report are counted from worker transcripts, no model call."""

from __future__ import annotations

from scio.graph.report import build_report, parse_transcript, tool_signals
from scio.state.models import (
    ArtifactRef,
    Budget,
    GitHubRefs,
    Role,
    SCIOState,
    TaskSpec,
)

CODER = """user: do the step
assistant: Let me look.
assistant: <tool_use view_file {'path': '/w/pkg/a.py', 'start_line': 1}>
assistant: <tool_use view_file {'path': '/w/pkg/a.py', 'start_line': 101}>
tool_result: 1| x = 1
tool_result: 101| y = 2
assistant: <tool_use edit_file {'path': '/w/pkg/a.py', 'old_text': 'x = 1', 'new_text': 'x = (1'}>
tool_result (error): edit rejected by lint (reverted):
pkg/a.py:1:7: E999 SyntaxError
assistant: <tool_use edit_file {'path': '/w/pkg/a.py', 'old_text': 'x = 1', 'new_text': 'x = (1'}>
tool_result (error): edit rejected by lint (reverted):
pkg/a.py:1:7: E999 SyntaxError
assistant: <tool_use edit_file {'path': '/w/pkg/a.py', 'old_text': 'x = 1', 'new_text': 'x = 2'}>
tool_result: edited /w/pkg/a.py (+0 lines)
assistant: <tool_use run_tests {'filter': 'a'}>
tool_result: all tests passed: 1 passed in 0.01s
assistant: Done.
"""

DOCUMENTER = "\n".join(
    [
        "user: document",
        "assistant: <tool_use view_file {'path': '/w/CHANGELOG.md', 'start_line': 1}>",
        "tool_result: 1| # Changelog",
        "assistant: <tool_use edit_file {'path': '/w/CHANGELOG.md', "
        "'old_text': '## [Unreleased]\\n', 'new_text': '## [Unreleased]\\n- x\\n'}>",
        "tool_result: edited /w/CHANGELOG.md (+1 lines)",
        "assistant: <tool_use edit_file {'path': '/w/README.md', 'old_text': 'nope', "
        "'new_text': 'x'}>",
        "tool_result (error): old_text not found in /w/README.md",
        "",
    ]
)

CODE_DIFF = (
    "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
    "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)
DOCS_ONLY_DIFF = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"


def _ref(kind: str, path: str, role: Role) -> ArtifactRef:
    return ArtifactRef(kind=kind, path=path, sha256="x", produced_by=role)  # type: ignore[arg-type]


def _state(artifacts: dict[str, ArtifactRef]) -> SCIOState:
    return SCIOState(
        thread_id="run-s",
        phase="done",
        task=TaskSpec(title="t", description="d", acceptance=["a"], repo_url="r"),
        budgets=Budget(tokens_max=1, usd_max=1.0, advisor_calls_max=0),
        github=GitHubRefs(owner="o", repo="r"),
        artifacts=artifacts,
    )


TEXTS = {"/a/coder-01.txt": CODER, "/a/documenter-01.txt": DOCUMENTER, "/a/diff.txt": CODE_DIFF}


def _read(ref: ArtifactRef) -> str | None:
    return TEXTS.get(ref.path)


def test_parse_transcript_pairs_results_with_calls_in_order() -> None:
    calls = parse_transcript(CODER)
    assert [c[0] for c in calls] == [
        "view_file",
        "view_file",
        "edit_file",
        "edit_file",
        "edit_file",
        "run_tests",
    ]
    assert [c[2] for c in calls] == [False, False, True, True, False, False]
    assert calls[0][1] == {"path": "/w/pkg/a.py", "start_line": 1}


def test_signals_count_errors_repeats_rereads_and_docs() -> None:
    state = _state(
        {
            "coder:1": _ref("log", "/a/coder-01.txt", Role.CODER),
            "documenter:1": _ref("log", "/a/documenter-01.txt", Role.DOCUMENTER),
            "diff:final": _ref("diff", "/a/diff.txt", Role.SYSTEM),
            "verify:last": _ref("test_output", "/a/missing.txt", Role.TESTER),
        }
    )
    s = tool_signals(state, _read)
    assert s.turns == 2
    rows = {r.tool: (r.calls, r.errors) for r in s.by_tool}
    assert rows == {"view_file": (3, 0), "edit_file": (5, 3), "run_tests": (1, 0)}
    assert s.repeated_calls == 1 and s.repeated_examples[0].startswith("coder:1: edit_file")
    assert s.rereads == 1  # paging the same file counts; the documenter's one view does not
    assert s.docs_touched == ["CHANGELOG.md"]  # the failed README edit does not count
    assert s.code_changed is True and s.documenter_completed is True
    md = "\n".join(s.to_lines())
    assert "| edit_file | 5 | 3 | 0.60 |" in md and "documenter: touched CHANGELOG.md" in md


def test_documenter_incomplete_and_unknown() -> None:
    texts = dict(TEXTS)
    texts["/a/documenter-01.txt"] = "assistant: Nothing to document.\n"
    state = _state(
        {
            "coder:1": _ref("log", "/a/coder-01.txt", Role.CODER),
            "documenter:1": _ref("log", "/a/documenter-01.txt", Role.DOCUMENTER),
            "diff:final": _ref("diff", "/a/diff.txt", Role.SYSTEM),
        }
    )
    s = tool_signals(state, lambda ref: texts.get(ref.path))
    assert s.documenter_completed is False
    assert "code changed but no documentation file was edited" in "\n".join(s.to_lines())

    texts["/a/diff.txt"] = DOCS_ONLY_DIFF
    s = tool_signals(state, lambda ref: texts.get(ref.path))
    assert s.code_changed is False and s.documenter_completed is None

    s = tool_signals(_state({"coder:1": _ref("log", "/a/gone.txt", Role.CODER)}), _read)
    assert s.turns == 0 and s.documenter_completed is None and s.by_tool == []
    assert "| none |" in "\n".join(s.to_lines())


def test_report_carries_signals() -> None:
    state = _state({"coder:1": _ref("log", "/a/coder-01.txt", Role.CODER)})
    report = build_report(state, [], read_artifact=_read)
    assert report.tool_signals.by_tool[0].tool == "edit_file"
    assert "## Tool signals" in report.to_markdown()
    assert report.model_dump(mode="json")["tool_signals"]["repeated_calls"] == 1
