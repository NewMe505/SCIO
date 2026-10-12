"""M4 projections: scope, explore, plan, step implement, debug, advisor brief, review."""

from __future__ import annotations

from scio.context.assembler import (
    PYTHON_FILTER_HELP,
    PYTHON_SELECTOR_HELP,
    PYTHON_TEST_DIR,
    feature_block,
    tag_context,
    task_block,
)
from scio.state.models import (
    EscalationTrigger,
    FeatureItem,
    Plan,
    PlanStep,
    ReviewerOutput,
    TaskSpec,
)


def _summaries_block(summaries: list[str]) -> str:
    if not summaries:
        return ""
    return "\n\n".join(tag_context("explorer", s) for s in summaries)


def scope_message(task: TaskSpec, *, repo_stats: str, file_tree: str) -> str:
    return "\n".join(
        [
            task_block(task),
            "",
            f"## Repository\n{repo_stats}",
            "",
            "## Files (truncated)",
            file_tree,
            "",
            "Produce exploration questions and a draft feature list as JSON matching the schema.",
        ]
    )


def explore_message(question: str, *, repo_root: str) -> str:
    return (
        f"Question: {question}\n\nRepository root: {repo_root} (absolute paths only). "
        "Answer with facts and file:line citations. Finish with the JSON summary."
    )


def localize_question(step: PlanStep | None, task: TaskSpec) -> str:
    if step is None:
        return f"Where in this repository should '{task.title}' be implemented and tested?"
    files = ", ".join(step.files_expected) or "the files the plan expects"
    return (
        f"For plan step {step.id} ({step.description}): what do {files} currently contain, "
        "how are tests run, and what conventions must new code follow?"
    )


def plan_message(
    task: TaskSpec,
    features: list[FeatureItem],
    *,
    summaries: list[str],
    previous: Plan | None,
    revision_reason: str | None,
    filter_help: str = PYTHON_FILTER_HELP,
) -> str:
    parts = [task_block(task), "", feature_block(features)]
    if summaries:
        parts += ["", _summaries_block(summaries)]
    if previous is not None:
        steps = "\n".join(
            f"- {s.id} [{'done' if s.done else 'open'}] {s.description} "
            f"(features {', '.join(s.feature_ids)}; files {', '.join(s.files_expected)})"
            for s in previous.steps
        )
        parts += [
            "",
            f"## Previous plan v{previous.version}",
            steps,
            "",
            f"## Revision reason\n{revision_reason or 'unspecified'}",
            "",
            "Revise minimally; keep the ids of unchanged steps and keep done steps done.",
        ]
    parts += [
        "",
        f"Every feature id must be covered by exactly one step. Give each feature a {filter_help} "
        "in test_filter. Output JSON matching the schema.",
    ]
    return "\n".join(parts)


def plan_checklist(plan: Plan, features: list[FeatureItem]) -> str:
    by_id = {f.id: f for f in features}
    lines = [f"## SCIO plan v{plan.version}", "", plan.rationale.strip(), ""]
    for s in plan.steps:
        lines.append(f"### {s.id}: {s.description}")
        for fid in s.feature_ids:
            f = by_id.get(fid)
            if f is None:
                continue
            box = "x" if f.status.value == "passing" else " "
            lines.append(f"- [{box}] {f.id}: {f.description}")
        if s.files_expected:
            lines.append(f"  files: {', '.join(s.files_expected)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def step_message(
    task: TaskSpec,
    step: PlanStep,
    features: list[FeatureItem],
    *,
    repo_root: str,
    attempt: int,
    summaries: list[str],
    guidance: str | None,
    last_verify: str | None,
    selector_help: str = PYTHON_SELECTOR_HELP,
    test_dir: str = PYTHON_TEST_DIR,
) -> str:
    parts = [
        task_block(task),
        "",
        f"## Plan step {step.id}: {step.description}",
        f"Files expected: {', '.join(step.files_expected) or '(unspecified)'}",
        f"Tests go under {test_dir}. Touching other files is plan drift and will be escalated.",
        "",
        feature_block(features),
        "",
        f"Repository root: {repo_root} (use absolute paths under it)",
        f"Plan step to execute: {step.id}",
        f"Attempt: {attempt}",
    ]
    if summaries:
        parts += ["", _summaries_block(summaries)]
    if guidance:
        parts += ["", tag_context("advisor", guidance)]
    if last_verify:
        parts += ["", tag_context("verify", f"Last verification result:\n{last_verify}")]
    parts += [
        "",
        "Implement this step and its tests, run them, then stop. In your summary, tests_added "
        f"must list the tests that prove this step's features ({selector_help}), "
        "existing tests included; verification runs exactly those.",
    ]
    return "\n".join(parts)


def debug_message(
    step: PlanStep | None,
    *,
    repo_root: str,
    failing_output: str,
    signature: str | None,
    attempts: int,
    guidance: str | None,
) -> str:
    parts = [
        f"Plan step: {step.id if step else 'S1'}" + (f" ({step.description})" if step else ""),
        f"Repository root: {repo_root}",
        f"Plan step to execute: {step.id if step else 'S1'}",
        f"Attempts on this error signature: {attempts} (signature {signature or 'n/a'})",
        "",
        tag_context("verify", f"Failing checks:\n{failing_output}"),
    ]
    if guidance:
        parts += ["", tag_context("advisor", guidance)]
    parts += [
        "",
        "Fix the root cause, sweep the repository for the same construct, rerun, then stop.",
    ]
    return "\n".join(parts)


def advisor_brief(
    task: TaskSpec,
    step: PlanStep | None,
    *,
    triggers: list[EscalationTrigger],
    diff_stat: str,
    last_error: str,
    attempts: int,
    worker_summary: str | None,
    impact_flags: list[str],
    verify_passed: bool = False,
) -> str:
    why = ", ".join(t.value for t in triggers)
    if verify_passed:
        # ADR 0008: a pre-commit review of a passing high-impact diff, not a failure.
        why += (
            "\nVerify passed: tests and lint are green. This is a pre-commit review of a "
            "high-impact diff. Reply `continue` if the change is sound as it stands; "
            "`change_approach` or `replan` only if the diff itself should change."
        )
    return "\n".join(
        [
            f"# Goal\n{task.title}: {task.description.strip()[:600]}",
            "",
            f"# Step\n{step.id if step else 'S1'}: {step.description if step else '(no plan)'}",
            f"Expected files: {', '.join(step.files_expected) if step else '-'}",
            "",
            f"# Why you are consulted\n{why}",
            f"Impact flags: {'; '.join(impact_flags) or 'none'}",
            "",
            f"# Diff summary\n{diff_stat[:800]}",
            "",
            f"# Last error (attempt {attempts} on this signature)\n{last_error[:1500]}",
            "",
            f"# Worker's own summary\n{worker_summary or '-'}",
            "",
            "Reply with your verdict and concrete guidance as JSON matching the schema.",
        ]
    )


def review_message(task: TaskSpec, *, diff_text: str, test_output: str) -> str:
    return "\n".join(
        [
            task_block(task),
            "",
            "## Diff",
            "```diff",
            diff_text[:20_000],
            "```",
            "",
            tag_context("verify", test_output[:3000]),
            "",
            "Review the diff. Output JSON matching the schema.",
        ]
    )


def review_notes(review: ReviewerOutput | None) -> str:
    if review is None or not review.findings:
        return "Reviewer: no findings."
    rows = [f"- [{f.severity}] {f.file}:{f.line or '-'} {f.summary}" for f in review.findings]
    return f"Reviewer ({review.overall}):\n" + "\n".join(rows)
