"""``scio <build|new|run|resume> --outcome-file PATH`` (ADR 0019 decision 11): one JSON object
on every exit path, in the shape the recovery supervisor reads (``OutcomeFile`` in
``recovery/model.py``). The CLI's own output and exit codes stay as they were, and a crash
is re-raised unchanged after the file is written.

The supervisor and SCIO share the shape, never code: ``scio`` does not import ``recovery``.
This test imports both to hold the two copies of the shape together."""

from __future__ import annotations

import json
import subprocess
import traceback
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest

from recovery.model import OUTCOME_VERSION, OutcomeException, OutcomeFile, OutcomeFrame
from scio.cli import OUTCOME_VERSION as SCIO_OUTCOME_VERSION
from scio.cli import main
from scio.graph.run import RunOutcome, initial_state
from scio.graph.run_context import RunContext
from scio.harness.failures import FatalError
from scio.harness.github.client import GitHubError
from scio.state.models import ExitReason, LedgerEntry, Role, TaskSpec

PROMPT = "A CLI todo app with tags and JSON export"
BUILD_THREAD = "build-todo_cli-m1"


@pytest.fixture(autouse=True)
def _offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "fake")
    monkeypatch.setenv("SCIO_FAKE_SCRIPT", "1")
    monkeypatch.setenv("SCIO_CHECKPOINT_DSN", f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}")
    # the fake GitHub answers at once: no test waits for a CI status
    monkeypatch.setattr("scio.product.pr_ci.CI_WAIT_S", 0.0)
    monkeypatch.setattr("scio.product.pr_ci.CI_POLL_S", 0.0)


def _outcome(path: Path) -> dict[str, Any]:
    """The file's object, checked against every key and type of the supervisor's shape."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    hints = get_type_hints(OutcomeFile)
    assert set(data) == set(hints)
    assert data["version"] == 1 == OUTCOME_VERSION == SCIO_OUTCOME_VERSION
    assert data["status"] in get_args(hints["status"])
    assert isinstance(data["command"], str) and isinstance(data["exit_code"], int)
    for key in ("thread", "exit_reason", "reason", "resume"):
        assert data[key] is None or isinstance(data[key], str), key
    if data["exception"] is not None:
        assert set(data["exception"]) == set(get_type_hints(OutcomeException))
        for frame in data["exception"]["frames"]:
            assert set(frame) == set(get_type_hints(OutcomeFrame))
    assert b"\r\n" not in path.read_bytes()
    assert not list(path.parent.glob(f"{path.name}.*.tmp"))  # the atomic write cleaned up
    return data


def _bootstrap(target: str) -> None:
    assert main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-cli"]) == 0
    assert main(["new", "--thread", "new-cli", "--decision", "answers"]) == 0
    assert main(["new", "--thread", "new-cli", "--decision", "approve"]) == 0


def test_scio_new_records_each_human_stop_and_then_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "outcome.json"
    target = str(tmp_path / "product")
    flag = ["--outcome-file", str(file)]
    assert main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-o", *flag]) == 0
    data = _outcome(file)
    assert data == {
        "version": 1,
        "command": "new",
        "status": "paused",
        "exit_code": 0,
        "thread": "new-o",
        "exit_reason": None,
        "reason": None,
        "resume": 'scio new --thread new-o --decision "<text>"',
        "exception": None,
    }
    assert f"resume with: {data['resume']}\n" in capsys.readouterr().out
    assert main(["new", "--thread", "new-o", "--decision", "answers", *flag]) == 0
    assert _outcome(file)["status"] == "paused"
    assert main(["new", "--thread", "new-o", "--decision", "approve", *flag]) == 0
    data = _outcome(file)
    assert data["status"] == "done" and data["exit_code"] == 0 and data["thread"] == "new-o"
    assert data["resume"] is None and data["reason"] is None and data["exception"] is None
    assert "phase=done" in capsys.readouterr().out


def test_scio_build_records_a_refusal_a_github_stop_a_pause_and_a_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scio.product.build as build_module

    file = tmp_path / "outcome.json"
    target = str(tmp_path / "product")
    flag = ["--outcome-file", str(file)]
    _bootstrap(target)
    capsys.readouterr()

    # nothing waits yet: the decision is refused, exit 2, and the file says why
    assert main(["build", "--product", target, "--decision", "merge", *flag]) == 2
    err = capsys.readouterr().err
    data = _outcome(file)
    assert data["status"] == "refused" and data["exit_code"] == 2 and data["command"] == "build"
    assert data["thread"] == BUILD_THREAD and data["reason"] == err.strip()
    assert "has no checkpoint" in data["reason"] and data["exit_reason"] is None

    # a GitHub refusal in an orchestrator node (I-113): halted, exit_reason github
    real_run_build = build_module.run_build
    calls: list[int] = []

    def refused_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise GitHubError("gh issue create failed (1): 'product-mode' not found")
        return real_run_build(*args, **kwargs)

    monkeypatch.setattr(build_module, "run_build", refused_once)
    assert main(["build", "--product", target, *flag]) == 1
    stopped, hint = capsys.readouterr().err.strip().split("\n")
    data = _outcome(file)
    assert data["status"] == "halted" and data["exit_code"] == 1
    assert data["exit_reason"] == "github" and data["thread"] == BUILD_THREAD
    assert data["reason"] == stopped and "product-mode" in stopped
    assert data["resume"] == hint and "run the same scio build command again" in hint

    # M1 to its merge stop: paused, with the hint the CLI printed
    assert main(["build", "--product", target, *flag]) == 0
    out = capsys.readouterr().out
    data = _outcome(file)
    assert data["status"] == "paused" and data["exit_code"] == 0 and data["exit_reason"] is None
    assert data["thread"] == BUILD_THREAD and data["reason"] is None
    assert data["resume"].startswith(f"scio build --product {target} --thread {BUILD_THREAD} ")
    assert f"resume with: {data['resume']}\n" in out

    # `stop` at the merge stop ends the thread with an exit reason: halted, the printed reason
    assert main(["build", "--product", target, "--decision", "stop", *flag]) == 1
    out = capsys.readouterr().out
    data = _outcome(file)
    assert data["status"] == "halted" and data["exit_code"] == 1 and data["resume"] is None
    assert data["exit_reason"] == "stopped" and data["thread"] == BUILD_THREAD
    assert data["reason"] and f"reason: {data['reason']}\n" in out


def test_every_refusal_records_the_message_it_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "outcome.json"
    flag = ["--outcome-file", str(file)]
    assert main(["build", "--product", str(tmp_path / "nothing"), *flag]) == 2
    data = _outcome(file)
    assert data["status"] == "refused" and data["exit_code"] == 2 and data["thread"] is None
    assert data["reason"] == capsys.readouterr().err.strip() and "not found" in data["reason"]

    assert main(["new", "--dir", str(tmp_path / "p"), *flag]) == 2  # no prompt, no thread
    data = _outcome(file)
    assert data["status"] == "refused" and data["command"] == "new" and data["thread"] is None
    assert data["reason"] == capsys.readouterr().err.strip()

    # this refusal always printed to stdout; it still does
    assert main(["new", "--thread", "never-started", "--decision", "x", *flag]) == 2
    data = _outcome(file)
    assert data["status"] == "refused" and data["thread"] == "never-started"
    assert data["reason"] == capsys.readouterr().out.strip()

    # argparse's own refusal: the same SystemExit(2) and usage text, and the file records it
    with pytest.raises(SystemExit) as info:
        main(["build", *flag])
    assert info.value.code == 2
    err = capsys.readouterr().err
    data = _outcome(file)
    assert data["status"] == "refused" and data["exit_code"] == 2 and data["command"] == "build"
    assert data["reason"] == err.strip().splitlines()[-1]
    assert data["reason"] == "scio build: error: the following arguments are required: --product"


def _frames(exc: BaseException) -> list[dict[str, Any]]:
    return [
        {"path": f.filename, "line": f.lineno, "function": f.name}
        for f in traceback.extract_tb(exc.__traceback__)
    ]


def test_a_crash_writes_the_exception_and_re_raises_it_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-180's shape: a hung ``claude`` raises ``TimeoutExpired`` deep in a node. The file
    gets the exception; the same exception object leaves ``main``, chained to nothing."""
    import scio.product

    raised: list[BaseException] = []

    def hung_claude() -> None:
        exc = subprocess.TimeoutExpired(["claude", "-p"], 5)
        raised.append(exc)
        raise exc

    def run_product(*args: Any, **kwargs: Any) -> Any:
        hung_claude()

    monkeypatch.setattr(scio.product, "run_product", run_product)
    file = tmp_path / "outcome.json"
    argv = ["new", "--prompt", PROMPT, "--dir", str(tmp_path / "p"), "--thread", "new-crash"]
    with pytest.raises(subprocess.TimeoutExpired) as info:
        main([*argv, "--outcome-file", str(file)])
    assert info.value is raised[0]
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert capsys.readouterr().err == ""  # nothing printed on the way out
    data = _outcome(file)
    assert data["status"] == "crashed" and data["exit_code"] == 1 and data["command"] == "new"
    assert data["thread"] == "new-crash"
    assert data["exit_reason"] is None and data["reason"] is None and data["resume"] is None
    exc = data["exception"]
    assert exc["type"] == "subprocess.TimeoutExpired" and exc["message"] == str(raised[0])
    # main's frames, outermost first: the propagated traceback minus this test's own frame
    assert exc["frames"] == _frames(info.value)[1:]
    assert exc["frames"][0]["function"] == "main"
    assert exc["frames"][-1]["function"] == "hung_claude"
    assert Path(exc["frames"][-1]["path"]) == Path(__file__)


def _task(ctx: RunContext) -> TaskSpec:
    return TaskSpec(title="t", description="d", acceptance=["a"], repo_url="r")


def test_scio_run_and_resume_record_their_stops_and_a_builtin_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scio.cli as cli

    spec = tmp_path / "task.md"
    spec.write_text("# t\n\n- [ ] a\n", encoding="utf-8", newline="\n")
    file = tmp_path / "outcome.json"
    flag = ["--outcome-file", str(file)]
    run = ["run", "--spec", str(spec), "--repo", "r", "--sandbox", str(tmp_path / "sb")]

    def paused(ctx: RunContext, task: TaskSpec) -> RunOutcome:
        return RunOutcome(state=initial_state(ctx, task), interrupt={"question": "approve?"})

    monkeypatch.setattr(cli, "run_graph", paused)
    assert main([*run, "--thread", "run-o", *flag]) == 0
    data = _outcome(file)
    assert data["status"] == "paused" and data["command"] == "run" and data["thread"] == "run-o"
    assert data["resume"] == "scio resume --thread run-o --decision <text>"
    assert f"resume with: {data['resume']}\n" in capsys.readouterr().out

    def ended(reason: ExitReason) -> Any:
        def resume_graph(ctx: RunContext, decision: str | None) -> RunOutcome:
            state = initial_state(ctx, _task(ctx)).model_copy(
                update={
                    "exit_reason": reason,
                    "task_ledger": [LedgerEntry(kind="fact", text="spent", source=Role.SYSTEM)],
                }
            )
            return RunOutcome(state=state, interrupt=None)

        return resume_graph

    resume = ["resume", "--thread", "run-o", "--sandbox", str(tmp_path / "sb"), *flag]
    monkeypatch.setattr(cli, "resume_graph", ended(ExitReason.BUDGET))
    assert main(resume) == 1
    data = _outcome(file)
    assert data["status"] == "halted" and data["exit_code"] == 1 and data["command"] == "resume"
    assert data["exit_reason"] == "budget" and data["reason"] == "spent"
    monkeypatch.setattr(cli, "resume_graph", ended(ExitReason.SUCCESS))
    assert main(resume) == 0
    data = _outcome(file)
    assert data["status"] == "done" and data["exit_reason"] is None and data["reason"] is None

    def broken(ctx: RunContext, task: TaskSpec) -> RunOutcome:
        raise KeyError("missing")

    monkeypatch.setattr(cli, "run_graph", broken)
    with pytest.raises(KeyError) as info:
        main([*run, "--thread", "run-crash", *flag])
    data = _outcome(file)
    assert data["status"] == "crashed" and data["thread"] == "run-crash"
    assert data["exception"]["type"] == "KeyError"  # a builtin, unqualified as printed
    assert data["exception"]["message"] == str(info.value) == "'missing'"
    assert data["exception"]["frames"] == _frames(info.value)[1:]
    assert data["exception"]["frames"][-1]["function"] == "broken"


def test_a_harness_error_stops_new_run_and_resume_with_a_resume_hint_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The I-182 sweep: the interviewer, the product planner and every task-graph node guard
    only ``BudgetExceeded``, so any other ``HarnessError`` (an expired login, a schema the
    model never met, retries run out) ended ``scio new``, ``run`` and ``resume`` in a
    traceback. Each stops with the error and the command that resumes its thread."""
    import scio.cli as cli
    import scio.product

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise FatalError("claude -p failed: no message (subtype=-)")

    monkeypatch.setattr(scio.product, "run_product", failing)
    monkeypatch.setattr(cli, "run_graph", failing)
    monkeypatch.setattr(cli, "resume_graph", failing)
    spec = tmp_path / "task.md"
    spec.write_text("# t\n\n- [ ] a\n", encoding="utf-8", newline="\n")
    sandbox = ["--sandbox", str(tmp_path / "sb")]
    task_resume = f"scio resume --thread r-h --sandbox {tmp_path / 'sb'}"
    cases = [
        (["new", "--prompt", PROMPT, "--dir", str(tmp_path / "p")], "n-h", "scio new --thread n-h"),
        (["run", "--spec", str(spec), "--repo", "r", *sandbox], "r-h", task_resume),
        (["resume", *sandbox], "r-h", task_resume),
    ]
    file = tmp_path / "outcome.json"
    for argv, thread, resume in cases:
        command = argv[0]
        capsys.readouterr()
        assert main([*argv, "--thread", thread, "--outcome-file", str(file)]) == 1, command
        stop = f"scio {command} stopped on a harness error: FatalError: claude -p failed"
        err = capsys.readouterr().err
        assert stop in err and resume in err and "Traceback" not in err, err
        data = _outcome(file)
        assert (data["status"], data["exit_reason"]) == ("halted", "harness"), command
        assert data["thread"] == thread and data["resume"] == resume
        assert data["reason"] == f"{stop}: no message (subtype=-)"


def test_without_the_flag_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scio.cli as cli
    import scio.product

    writes: list[Any] = []
    monkeypatch.setattr(cli, "_write_outcome", lambda *a: writes.append(a))
    target = str(tmp_path / "product")
    assert main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-q"]) == 0
    assert main(["build", "--product", str(tmp_path / "nothing")]) == 2

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(scio.product, "run_product", boom)
    with pytest.raises(RuntimeError):
        main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-r"])
    with pytest.raises(SystemExit):
        main(["build"])
    assert writes == []


def test_a_file_that_cannot_be_written_changes_nothing_but_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scio.product

    blocked = tmp_path / "blocked"
    blocked.mkdir()  # os.replace cannot put a file where a directory is
    flag = ["--outcome-file", str(blocked)]
    assert main(["build", "--product", str(tmp_path / "nothing"), *flag]) == 2
    refusal, warning = capsys.readouterr().err.strip().split("\n")
    assert "not found: run scio new first" in refusal
    assert warning.startswith(f"scio: warning: could not write the outcome file {blocked}")
    assert blocked.is_dir() and not list(tmp_path.glob("*.tmp"))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(scio.product, "run_product", boom)
    with pytest.raises(RuntimeError, match="boom") as info:
        main(["new", "--prompt", PROMPT, "--dir", str(tmp_path / "p"), *flag])
    assert info.value.__context__ is None  # the failed write is not chained onto the crash
    assert "could not write the outcome file" in capsys.readouterr().err
