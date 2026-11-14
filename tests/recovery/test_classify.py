"""Each row of the classifier flips on its own evidence, and does not flip without it (the
ADR 0005 triage pattern, ADR 0019 decision 5). Then the signature: stable across numbers,
paths and durations, different for a different innermost frame."""

from __future__ import annotations

import re

import pytest

from recovery.classify import (
    TASK_FAULT_REASONS,
    classify,
    innermost_scio_frame,
    normalise_message,
    scio_module_path,
    signature,
)
from recovery.model import Evidence, FailureClass, Frame

LIVE_WT = r"C:\Users\playa\OneDrive\Desktop\projects\SCIO\.scio\live-wt"
VENV = r"C:\Users\playa\OneDrive\Desktop\projects\SCIO\.venv\Lib\site-packages"
STDLIB = r"C:\Program Files\Python312\Lib"
CLAUDE_CODE = "scio/harness/providers/claude_code.py:run"


def i180_frames(scio_root: str = LIVE_WT, sep: str = "\\", line: int = 89) -> list[Frame]:
    """The frames of I-180's traceback: scio's CLI, LangGraph, scio's provider, then the
    standard library's subprocess innermost."""

    def at(root: str, *parts: str) -> str:
        return sep.join([root, *parts])

    return [
        Frame("<frozen runpy>", 198, "_run_module_as_main"),
        Frame(at(scio_root, "scio", "cli.py"), 228, "_build"),
        Frame(at(VENV, "langgraph", "pregel", "main.py"), 2600, "invoke"),
        Frame(at(scio_root, "scio", "product", "build.py"), 700, "plan_milestone"),
        Frame(at(scio_root, "scio", "harness", "providers", "claude_code.py"), line, "run"),
        Frame(at(STDLIB, "subprocess.py"), 1628, "_communicate"),
    ]


def i180_message(
    seconds: str = "600", argv: str = "'claude', '-p', '--output-format', 'json'"
) -> str:
    return f"Command '[{argv}]' timed out after {seconds} seconds"


def i180(**overrides: object) -> Evidence:
    base: dict[str, object] = dict(
        exit_code=1,
        exception_type="subprocess.TimeoutExpired",
        exception_message=i180_message(),
        frames=i180_frames(),
    )
    base.update(overrides)
    return Evidence(**base)  # type: ignore[arg-type]


def halt(reason: str, ledger: str = "", **overrides: object) -> Evidence:
    base: dict[str, object] = dict(exit_code=1, halt_reason=reason, last_ledger_line=ledger)
    base.update(overrides)
    return Evidence(**base)  # type: ignore[arg-type]


FEATURE_HARNESS_ERROR = (
    "F2 attempt 1 failed: RepairableError: the worker produced no diff; "
    "the next `scio build` starts attempt 2"
)
FEATURE_HUMAN_STOP = "F2 failed (the owner said stop: wrong approach); build stopped"
NOT_LOGGED_IN = "claude -p failed: Not logged in · Please run /login"
RATE_LIMIT_WARNING = "retry 1/3 after 2.0s: rate limit reached for requests"


def cls(ev: Evidence) -> FailureClass:
    return classify(ev).failure_class


# --- rows 4a and 4b, and row 9's exceptions (the orchestrator's decisions, 2026-11-14) ---


def test_a_ctrl_c_is_a_human_stop_not_a_repair() -> None:
    ev = i180(exception_type="KeyboardInterrupt", exception_message="")
    assert cls(ev) is FailureClass.HUMAN_STOP
    assert classify(ev).signature == ""


def test_the_same_traceback_without_a_ctrl_c_is_harness_suspect() -> None:
    assert cls(i180()) is FailureClass.HARNESS_SUSPECT


def test_a_humans_feature_stop_holding_a_marker_word_is_a_task_fault() -> None:
    """The owner's free text at the feature decision can name a rate limit or a login; it
    is still the owner's stop, never a replay or an escalation for a sign-in."""
    for words in ("rate limit handling is wrong", "the authentication flow is wrong"):
        ledger = f"F2 failed ({words}); build stopped"
        assert cls(halt("feature_failed", ledger)) is FailureClass.TASK_FAULT


def test_a_rate_limited_feature_attempt_still_is_transient_not_a_task_fault() -> None:
    ledger = (
        "F2 attempt 1 failed: RetryableError: rate limit reached; "
        "the next `scio build` starts attempt 2"
    )
    assert cls(halt("feature_failed", ledger)) is FailureClass.EXTERNAL_TRANSIENT


@pytest.mark.parametrize(
    "exc", ["BudgetExceeded", "RefusalError", "scio.harness.failures.BudgetExceeded"]
)
def test_a_feature_ended_by_a_spend_cap_or_a_refusal_is_a_task_fault(exc: str) -> None:
    ledger = f"F2 attempt 1 failed: {exc}: over the cap; the next `scio build` starts attempt 2"
    assert cls(halt("feature_failed", ledger)) is FailureClass.TASK_FAULT


def test_a_feature_ended_by_another_harness_error_is_harness_suspect() -> None:
    assert cls(halt("feature_failed", FEATURE_HARNESS_ERROR)) is FailureClass.HARNESS_SUSPECT


# --- row 1: orphaned with no traceback ---


def test_an_orphan_with_no_traceback_is_orphan() -> None:
    assert cls(Evidence(exit_code=None, orphaned=True)) is FailureClass.ORPHAN


def test_an_orphan_whose_stderr_ends_in_a_scio_traceback_is_classified_by_it() -> None:
    assert cls(i180(exit_code=None, orphaned=True)) is FailureClass.HARNESS_SUSPECT


# --- row 2: hung ---


def test_a_hung_run_is_hang_whatever_else_it_shows() -> None:
    assert cls(Evidence(exit_code=None, hung=True)) is FailureClass.HANG
    assert cls(i180(hung=True)) is FailureClass.HANG


def test_the_same_run_not_hung_is_not_hang() -> None:
    assert cls(Evidence(exit_code=None, hung=False)) is FailureClass.UNKNOWN


# --- row 3: exit 0 and paused ---


def test_exit_0_and_paused_is_human_stop() -> None:
    assert cls(Evidence(exit_code=0, paused=True)) is FailureClass.HUMAN_STOP


def test_a_pause_with_a_failing_exit_is_not_human_stop() -> None:
    assert cls(Evidence(exit_code=1, paused=True)) is not FailureClass.HUMAN_STOP


# --- row 4: exit 0 and not paused ---


def test_exit_0_without_a_pause_is_done() -> None:
    assert cls(Evidence(exit_code=0)) is FailureClass.DONE


def test_exit_1_without_anything_else_is_not_done() -> None:
    assert cls(Evidence(exit_code=1)) is FailureClass.UNKNOWN


def test_done_and_human_stop_have_no_signature() -> None:
    assert classify(Evidence(exit_code=0)).signature == ""
    assert classify(Evidence(exit_code=0, paused=True)).signature == ""


# --- row 5: an auth marker, before the scio traceback of row 8 ---


@pytest.mark.parametrize(
    "message",
    [
        NOT_LOGGED_IN,
        'claude -p exited 1: {"type":"error","error":{"type":"authentication_error"}} (401)',
        "token refresh failed: invalid_grant",
    ],
)
def test_an_auth_failure_inside_a_scio_traceback_is_external_auth(message: str) -> None:
    ev = i180(exception_type="scio.harness.failures.FatalError", exception_message=message)
    c = classify(ev)
    assert c.failure_class is FailureClass.EXTERNAL_AUTH
    assert c.innermost_scio_frame == CLAUDE_CODE


def test_an_auth_failure_in_a_feature_ledger_line_is_external_auth() -> None:
    line = (
        f"F1 attempt 2 failed: FatalError: {NOT_LOGGED_IN}; the next `scio build` starts attempt 3"
    )
    assert cls(halt("feature_failed", line)) is FailureClass.EXTERNAL_AUTH


def test_the_same_traceback_without_an_auth_marker_is_not_external_auth() -> None:
    ev = i180(exception_type="scio.harness.failures.FatalError", exception_message="bad JSON")
    assert cls(ev) is FailureClass.HARNESS_SUSPECT


def test_auth_failures_share_one_signature_whatever_their_details() -> None:
    a = classify(i180(exception_message=f"{NOT_LOGGED_IN} (1)"))
    b = classify(halt("feature_failed", f"F9 attempt 4 failed: FatalError: {NOT_LOGGED_IN}"))
    assert a.signature == b.signature


# --- row 6: a transient marker ---


def test_a_github_stop_on_stderr_is_external_transient() -> None:
    stderr = (
        "scio build stopped on GitHub: gh: API rate limit exceeded (HTTP 403)\n"
        "Every finished step is checkpointed (thread build-p-m1); fix the cause and run the "
        "same scio build command again to resume.\n"
    )
    assert cls(Evidence(exit_code=1, stderr_tail=stderr)) is FailureClass.EXTERNAL_TRANSIENT


def test_a_github_stop_the_outcome_file_records_is_external_transient() -> None:
    ev = halt("github", "scio build stopped on GitHub: label 'agent' not found")
    assert cls(ev) is FailureClass.EXTERNAL_TRANSIENT
    assert cls(halt("github", "label 'agent' not found")) is FailureClass.EXTERNAL_TRANSIENT


def test_a_rate_limited_feature_attempt_is_external_transient() -> None:
    line = "F1 attempt 1 failed: RetryableError: overloaded_error (529); the next `scio build`"
    assert cls(halt("feature_failed", line)) is FailureClass.EXTERNAL_TRANSIENT


def test_a_rate_limit_the_run_rode_out_does_not_hide_the_crash_after_it() -> None:
    """The retry warning is logged to stderr long before the crash: only the traceback
    chain that ended the run is the failure (rows 5 and 6 read that, not all of stderr)."""
    stderr = f"{RATE_LIMIT_WARNING}\n{RATE_LIMIT_WARNING}\nTraceback (most recent call last):\n"
    stderr += '  File "x.py", line 1, in run\nsubprocess.TimeoutExpired: ' + i180_message() + "\n"
    assert cls(i180(stderr_tail=stderr)) is FailureClass.HARNESS_SUSPECT


def test_a_rate_limit_inside_the_chain_that_ended_the_run_is_external_transient() -> None:
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in call\n'
        "anthropic.RateLimitError: rate_limit_error\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 9, in run\n'
        "KeyError: 'usage'\n"
    )
    ev = i180(exception_type="KeyError", exception_message="'usage'", stderr_tail=stderr)
    assert cls(ev) is FailureClass.EXTERNAL_TRANSIENT


def test_a_task_fault_with_a_logged_retry_warning_stays_a_task_fault() -> None:
    ev = halt("integration_failed", "the product's tests failed", stderr_tail=RATE_LIMIT_WARNING)
    assert cls(ev) is FailureClass.TASK_FAULT


# --- row 7: a task-fault halt reason ---


@pytest.mark.parametrize("reason", sorted(TASK_FAULT_REASONS))
def test_every_task_fault_reason_is_task_fault(reason: str) -> None:
    c = classify(halt(reason, "the human said stop"))
    assert c.failure_class is FailureClass.TASK_FAULT
    assert c.signature


@pytest.mark.parametrize("reason", ["max_iters", "feature_failed", "stall"])
def test_a_halt_reason_outside_the_list_is_not_task_fault(reason: str) -> None:
    assert cls(halt(reason, "no ledger line names an exception")) is FailureClass.UNKNOWN


# --- row 8: a traceback through scio ---


def test_the_i180_traceback_is_harness_suspect_at_the_providers_run() -> None:
    """The innermost frame is the standard library's; the innermost scio frame names it."""
    c = classify(i180())
    assert c.failure_class is FailureClass.HARNESS_SUSPECT
    assert c.innermost_scio_frame == CLAUDE_CODE
    assert c.summary.startswith("subprocess.TimeoutExpired: Command")


def test_a_traceback_with_no_scio_frame_is_not_harness_suspect() -> None:
    frames = [f for f in i180_frames() if "scio" not in f.path.split("\\")]
    c = classify(i180(frames=frames))
    assert c.failure_class is FailureClass.UNKNOWN
    assert c.innermost_scio_frame == ""


# --- row 9: feature_failed with an exception, or a human's stop ---


def test_feature_failed_naming_an_exception_is_harness_suspect() -> None:
    c = classify(halt("feature_failed", FEATURE_HARNESS_ERROR))
    assert c.failure_class is FailureClass.HARNESS_SUSPECT
    assert "RepairableError" in c.summary


def test_feature_failed_by_a_human_stop_is_task_fault() -> None:
    assert cls(halt("feature_failed", FEATURE_HUMAN_STOP)) is FailureClass.TASK_FAULT


def test_the_exception_line_without_feature_failed_is_not_row_9() -> None:
    assert cls(halt("max_iters", FEATURE_HARNESS_ERROR)) is FailureClass.UNKNOWN


def test_parallel_features_fall_back_to_the_summarys_failed_exit_error_line() -> None:
    """With parallel features, a passing feature's ledger line can come after the failing
    one, so the last ledger line names no exception (build.py's batch loop)."""
    stdout = "build thread b phase=halted exit=feature_failed dir p\n"
    stdout += "  F1 a1 T1 failed exit=error $0.1200 escalations=0/0\n"
    stdout += "  F2 a1 T1 passed exit=success $0.3000 escalations=0/0\n"
    passed = "F2 passed at T1 in 3 iteration(s), $0.3000; milestone/m1 fast-forward"
    c = classify(halt("feature_failed", passed, stdout_tail=stdout))
    assert c.failure_class is FailureClass.HARNESS_SUSPECT
    assert c.summary.startswith("F1 attempt 1 failed")


def test_a_failed_feature_line_that_is_not_exit_error_is_no_fallback() -> None:
    stdout = "  F1 a1 T1 failed exit=max_iters $0.1200 escalations=0/0\n"
    ev = halt("feature_failed", "F2 passed at T1", stdout_tail=stdout)
    assert cls(ev) is FailureClass.UNKNOWN


# --- row 10 ---


def test_a_refusal_is_unknown() -> None:
    ev = Evidence(exit_code=2, last_ledger_line="scio build: thread t is not paused at a stop")
    c = classify(ev)
    assert c.failure_class is FailureClass.UNKNOWN
    assert c.signature and "exit 2" in c.summary


# --- the signature ---


def test_the_same_defect_with_other_numbers_paths_and_durations_has_one_signature() -> None:
    """live-wt on Windows, a POSIX worktree and site-packages; other line numbers, another
    timeout and a longer argv: one defect, one signature."""
    posix_root = "/home/runner/work/SCIO/SCIO/.claude/worktrees/recovery-7"
    site = "/usr/lib/python3.12/site-packages"
    variants = [
        i180(),
        i180(frames=i180_frames(posix_root, "/", line=91), exception_message=i180_message("1800")),
        i180(
            frames=i180_frames(site, "/", line=120),
            exception_message=i180_message("30.5", "'claude', '-p', '--model', 'opus', '-v'"),
        ),
    ]
    found = {classify(ev).signature for ev in variants}
    assert len(found) == 1
    assert re.fullmatch(r"[0-9a-f]{16}", found.pop())
    assert {classify(ev).innermost_scio_frame for ev in variants} == {CLAUDE_CODE}


def test_a_different_innermost_scio_frame_has_another_signature() -> None:
    worker = [
        *i180_frames()[:4],
        Frame(LIVE_WT + r"\scio\loop\claude_code_worker.py", 140, "run_turn"),
        Frame(STDLIB + r"\subprocess.py", 1628, "_communicate"),
    ]
    a, b = classify(i180()), classify(i180(frames=worker))
    assert b.innermost_scio_frame == "scio/loop/claude_code_worker.py:run_turn"
    assert a.signature != b.signature


def test_a_different_exception_type_or_message_has_another_signature() -> None:
    base = classify(i180()).signature
    assert classify(i180(exception_type="KeyError")).signature != base
    assert classify(i180(exception_message="claude returned no JSON")).signature != base


def test_signature_is_the_class_type_frame_and_normalised_message() -> None:
    a = signature(FailureClass.HARNESS_SUSPECT, "T", CLAUDE_CODE, "after 600 seconds at 0x7ff1")
    b = signature(FailureClass.HARNESS_SUSPECT, "T", CLAUDE_CODE, "after 5 seconds at 0x1")
    assert a == b and len(a) == 16
    assert signature(FailureClass.UNKNOWN, "T", CLAUDE_CODE, "after 5 seconds") != a


# --- normalise_message ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("attempt 3 of 12", "attempt <n> of <n>"),
        ("thread build-graphcalc-m3-p2-a1", "thread build-graphcalc-m<n>-p<n>-a<n>"),
        ("head e3b530d moved from 1fb2443ab", "head <n> moved from <n>"),
        ("object at 0x7ffd5a3c", "object at <n>"),
        ("id 123e4567-e89b-12d3-a456-426614174000 gone", "id <n> gone"),
        ("missing key 'feature_runs'", "missing key <q>"),
        ('no field "x" in "y"', "no field <q> in <q>"),
        (r"C:\Users\me\SCIO\x.py not found", "<path> not found"),
        ("/home/u/x/progress.json not found", "<path> not found"),
        (r"\\server\share\x.db locked", "<path> locked"),
        ("at 2026-11-14T10:00:00.123Z and 2026-11-13 09:00", "at <ts> and <ts>"),
        ("timed out after 600 seconds", "timed out after <dur>"),
        ("waited 2.5s then 1 second", "waited <dur> then <dur>"),
        ("can't parse the defaced plan", "can't parse the defaced plan"),
        ("Command '['claude', '-p']' timed out", "Command <q> timed out"),
    ],
)
def test_normalise_message_replaces_volatile_parts(text: str, expected: str) -> None:
    assert normalise_message(text) == expected


def test_two_occurrences_of_i180_normalise_equally() -> None:
    short = i180_message("600", "'claude', '-p'")
    long = i180_message("1800.0", "'claude', '-p', '--output-format', 'json', '--model', 'x'")
    assert (
        normalise_message(short) == normalise_message(long) == "Command <q> timed out after <dur>"
    )


# --- the scio frame ---


@pytest.mark.parametrize(
    ("path", "module"),
    [
        (
            LIVE_WT + r"\scio\harness\providers\claude_code.py",
            "scio/harness/providers/claude_code.py",
        ),
        ("/home/u/scio/scio/cli.py", "scio/cli.py"),
        ("/usr/lib/python3.12/site-packages/scio/graph/effects.py", "scio/graph/effects.py"),
        (VENV + r"\langgraph\pregel\main.py", ""),
        ("/home/u/scio/.venv/lib/python3.12/site-packages/pydantic/main.py", ""),
        (r"C:\Users\me\SCIO\tests\unit\test_x.py", ""),
        (r"C:\Users\me\SCIO\.scio\live-wt\README.md", ""),
        ("<frozen runpy>", ""),
    ],
)
def test_a_frame_is_scios_only_under_a_scio_package(path: str, module: str) -> None:
    assert scio_module_path(path) == module


def test_the_innermost_scio_frame_skips_library_frames_inside_it() -> None:
    assert innermost_scio_frame(i180_frames()) == CLAUDE_CODE
    assert innermost_scio_frame([Frame(VENV + r"\pydantic\main.py", 1, "validate")]) == ""
    assert innermost_scio_frame([]) == ""
