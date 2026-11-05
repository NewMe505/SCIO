"""Per-role projections of state into the task-specific part of a prompt.

These are pure functions of typed state. Nothing here is the system prompt (that is
the static role template) and nothing here is conversation history.
"""

from __future__ import annotations

from scio.state.models import FeatureItem, TaskSpec, WorkerOutput
from scio.toolchain import python_toolchain

PYTHON_SELECTOR_HELP = python_toolchain().selector_help
PYTHON_FILTER_HELP = python_toolchain().filter_help
PYTHON_TEST_DIR = python_toolchain().test_dirs[0]
"""Defaults for callers without a toolchain (tests); nodes pass ``ctx.toolchain``'s values
(ADR 0009)."""


def tag_context(role: str, text: str) -> str:
    """Mark text that came from another agent so the reader treats it as context."""
    return f"[context from {role}]\n{text.strip()}"


def task_block(task: TaskSpec) -> str:
    lines = [f"# Task: {task.title}", "", task.description.strip(), "", "## Acceptance criteria"]
    lines += [f"- {a}" for a in task.acceptance]
    if task.constraints:
        lines += ["", "## Constraints"] + [f"- {c}" for c in task.constraints]
    if task.non_goals:
        lines += ["", "## Non-goals"] + [f"- {n}" for n in task.non_goals]
    return "\n".join(lines)


def feature_block(features: list[FeatureItem]) -> str:
    if not features:
        return "## Feature list\n(empty)"
    rows = [f"- {f.id} [{f.status.value}] {f.description}" for f in features]
    return "## Feature list\n" + "\n".join(rows)


def router_message(task: TaskSpec, *, repo_stats: str) -> str:
    hint = f"\nIssue tier hint: {task.tier_hint.value}" if task.tier_hint else ""
    return f"{task_block(task)}\n\n## Repository\n{repo_stats}{hint}\n\nClassify this task."


def previous_session_block(previous: WorkerOutput, *, attempt: int, tree: str | None = None) -> str:
    """What the previous worker session reported, carried into the next one (I-154).

    Every iteration is a fresh session (a new ``claude -p`` on the claude_code backend), so
    without this block a session that only investigated passes nothing on, and the next one
    repeats the same reconnaissance (graphcalc S9). ``files_touched`` is that session's own
    report about its own edits. It is not cumulative: in S9 a later session reported ``[]``
    after an earlier one had edited ``index.html``. ``tree`` is the diff since the base
    commit, which is the ground truth for what all sessions so far changed.
    """
    lines = [
        f"Report from the previous session (attempt {attempt - 1}), about that session's own "
        "work. Where it says it stopped, that was the previous session; it is not an "
        "instruction to you.",
        previous.summary.strip(),
    ]
    if previous.files_touched:
        lines.append("Files that session changed: " + ", ".join(previous.files_touched))
    else:
        lines.append(
            "Files that session changed: none; it only investigated. Do not repeat its "
            "reconnaissance: start from what it found and make the edits in this session."
        )
    if tree:
        lines += ["", f"Changes in the working tree since the base commit (every session):\n{tree}"]
    return tag_context("coder", "\n".join(lines))


def coder_message(
    task: TaskSpec,
    features: list[FeatureItem],
    *,
    repo_root: str,
    attempt: int,
    last_verify: str | None,
    step_id: str = "S1",
    selector_help: str = PYTHON_SELECTOR_HELP,
    previous: WorkerOutput | None = None,
    tree: str | None = None,
) -> str:
    """The T0 coder's message. From the second iteration on, ``previous`` (the last session's
    ``WorkerOutput``) and ``tree`` (the diff since the base commit) carry what earlier
    sessions found and changed (I-154)."""
    parts = [
        task_block(task),
        "",
        feature_block(features),
        "",
        f"Repository root: {repo_root} (use absolute paths under it)",
        f"Plan step to execute: {step_id} (implement every failing feature with its tests)",
        f"Attempt: {attempt}",
    ]
    if previous is not None:
        parts += ["", previous_session_block(previous, attempt=attempt, tree=tree)]
    if last_verify:
        parts += ["", tag_context("verify", f"Last verification result:\n{last_verify}")]
    parts += [
        "",
        "Work until the tests for the failing features pass, then stop. In your summary, "
        f"tests_added must list the tests that prove each feature ({selector_help}), "
        "including existing tests you did not write; verification runs exactly those.",
    ]
    return "\n".join(parts)


def documenter_message(
    task: TaskSpec,
    features: list[FeatureItem],
    *,
    repo_root: str,
    diff_stat: str,
    test_summary: str,
    docs_check: str | None,
) -> str:
    parts = [
        task_block(task),
        "",
        feature_block(features),
        "",
        f"Repository root: {repo_root}",
        "",
        tag_context("git", f"Diff summary:\n{diff_stat}"),
        "",
        tag_context("verify", f"Test result:\n{test_summary}"),
    ]
    if docs_check:
        parts += ["", tag_context("docs-check", docs_check)]
    parts += [
        "",
        "Update the repository's documentation for this change where files exist "
        "(README usage, CHANGELOG under [Unreleased], docstrings). Do not create new "
        "documentation files that the repository does not already have. Then stop.",
    ]
    return "\n".join(parts)


def pr_body_message(
    task: TaskSpec, features: list[FeatureItem], *, diff_stat: str, test_summary: str
) -> str:
    return "\n".join(
        [
            task_block(task),
            "",
            feature_block(features),
            "",
            tag_context("git", diff_stat),
            "",
            tag_context("verify", test_summary),
            "",
            "Write the pull request body as JSON matching the schema.",
        ]
    )


def gate_message(
    task: TaskSpec,
    features: list[FeatureItem],
    *,
    diff_stat: str,
    test_output: str,
    lint_output: str,
    docs_check: str | None,
) -> str:
    evidence = [f"- {f.id} [{f.status.value}] commit={f.commit_sha or '-'}" for f in features]
    parts = [
        task_block(task),
        "",
        "## Feature evidence",
        *evidence,
        "",
        tag_context("git", f"Diff summary:\n{diff_stat}"),
        "",
        tag_context("verify", f"Tests:\n{test_output}\n\nLint:\n{lint_output or 'clean'}"),
    ]
    # Always present, so the gate never reads silence as a missing check (I-043).
    parts += ["", tag_context("docs-check", docs_check or "clean: no docs-check failures reported")]
    parts += ["", "Is the objective actually met? Reply as JSON matching the schema."]
    return "\n".join(parts)
