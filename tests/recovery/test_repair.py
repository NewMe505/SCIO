"""The repair session: its worktree, its argv and environment, its verdict, and the check
that never trusts the verdict (ADR 0019 decision 7, guardrail 60)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from recovery.commands import CommandResult, run_command
from recovery.config import Limits, WorkspaceConfig
from recovery.github import GitHub
from recovery.model import (
    VERDICT_SCHEMA,
    CiStatus,
    FailureClass,
    Incident,
    IncidentState,
    PullRequest,
    RepairVerdict,
)
from recovery.repair import (
    CI_PENDING,
    DENIED_TOOLS,
    branch_name,
    closes_issue,
    prepare_worktree,
    repair_argv,
    repair_env,
    run_repair,
    verify_claim,
    worktree_path,
)

ISSUE = 95
SIGNATURE = "abcdef0123456789"
BRANCH = "fix/95-recovery-abcdef"


@dataclass
class Call:
    argv: list[str]
    cwd: str | Path | None
    stdin: str | None
    timeout_s: float | None
    env: Mapping[str, str] | None


@dataclass
class FakeRunner:
    """Records every call and answers from ``results`` (by the first matching argv
    fragment), else ``default``."""

    default: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))
    results: dict[str, CommandResult] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(Call(list(argv), cwd, stdin, timeout_s, env))
        joined = " ".join(argv)
        for fragment, result in self.results.items():
            if fragment in joined:
                return result
        return self.default


@dataclass
class RecordingGit:
    """The real ``run_command``, with every argv recorded."""

    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(list(argv))
        return run_command(argv, cwd=cwd, stdin=stdin, timeout_s=timeout_s, env=env)


class FakeGh(GitHub):
    """The three reads ``verify_claim`` makes, from canned data; no ``gh`` process."""

    def __init__(
        self,
        prs: dict[int, PullRequest] | None = None,
        files: dict[int, list[str]] | None = None,
        ci: dict[str, CiStatus] | None = None,
    ) -> None:
        super().__init__("o/SCIO")
        self.prs = prs or {}
        self.files = files or {}
        self.ci = ci or {}

    def pr(self, number: int) -> PullRequest | None:
        return self.prs.get(number)

    def pr_files(self, number: int) -> list[str]:
        return self.files.get(number, [])

    def ci_status(self, sha: str) -> CiStatus:
        return self.ci.get(sha, "none")


def make_cfg(repo: Path, limits: Limits | None = None) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace=repo.parent / "ws",
        launcher=("sh", "scio.sh"),
        scio_repo=repo,
        scio_gh_repo="o/SCIO",
        live_wt=repo / ".scio" / "live-wt",
        limits=limits or Limits(),
    )


def make_incident(issue: int | None = ISSUE) -> Incident:
    return Incident(
        incident_id="inc-1",
        signature=SIGNATURE,
        failure_class=FailureClass.HARNESS_SUSPECT,
        summary="TimeoutExpired in scio/harness/providers/claude_code.py:run",
        workspace="ws",
        state=IncidentState.REPAIRING,
        created_at=1.0,
        updated_at=1.0,
        issue_number=issue,
    )


# -- names --------------------------------------------------------------------


def test_the_branch_and_worktree_are_named_from_the_issue_and_incident(tmp_path: Path) -> None:
    incident = make_incident()
    assert branch_name(incident) == BRANCH
    path = worktree_path(make_cfg(tmp_path / "SCIO"), incident)
    assert path == tmp_path / "SCIO" / ".claude" / "worktrees" / "recovery-inc-1"


def test_an_incident_without_an_issue_has_no_branch() -> None:
    with pytest.raises(ValueError, match="no GitHub issue"):
        branch_name(make_incident(issue=None))


# -- the worktree, on a real repository with a bare origin --------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def scio_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway SCIO clone whose ``origin`` is a local bare repository with one commit
    on ``main``. The machine's git config is shut out."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    repo = tmp_path / "SCIO"
    origin.mkdir()
    repo.mkdir()
    _git("init", "--bare", "-b", "main", cwd=origin)
    _git("init", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("scio\n", encoding="utf-8", newline="\n")
    _git("add", "README.md", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "one", cwd=repo)
    _git("remote", "add", "origin", origin.as_posix(), cwd=repo)
    _git("push", "origin", "main", cwd=repo)
    return repo


def _branch_of(path: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_the_worktree_is_made_from_origin_main_and_synced_with_system_certs(
    scio_repo: Path,
) -> None:
    cfg = make_cfg(scio_repo)
    uv = FakeRunner()
    git = RecordingGit()
    path = prepare_worktree(cfg, make_incident(), git=git, uv=uv)

    assert path == worktree_path(cfg, make_incident())
    assert (path / "README.md").read_text(encoding="utf-8") == "scio\n"
    assert _branch_of(path) == BRANCH
    assert any(c[3:5] == ["fetch", "origin"] for c in git.calls)
    add = next(c for c in git.calls if c[3:5] == ["worktree", "add"])
    assert add[-1] == "origin/main" and "--no-track" in add
    upstream = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "@{upstream}"],
        capture_output=True,
        text=True,
    )
    assert upstream.returncode != 0, "the repair branch must not track main"
    [sync] = uv.calls
    assert sync.argv == ["uv", "sync", "--frozen", "--all-extras", "--group", "dev"]
    assert sync.cwd == path
    assert sync.env is not None and sync.env["UV_SYSTEM_CERTS"] == "1"


def test_a_second_call_reuses_the_worktree(scio_repo: Path) -> None:
    cfg = make_cfg(scio_repo)
    first = prepare_worktree(cfg, make_incident(), git=RecordingGit(), uv=FakeRunner())
    (first / "work.txt").write_text("in progress\n", encoding="utf-8", newline="\n")
    git = RecordingGit()
    uv = FakeRunner()
    second = prepare_worktree(cfg, make_incident(), git=git, uv=uv)

    assert second == first
    assert (second / "work.txt").exists(), "the reused worktree keeps its work"
    assert not any("add" in c for c in git.calls)
    assert len(uv.calls) == 1, "uv sync runs again: it covers a crash before the sync"


def test_a_branch_left_by_an_earlier_attempt_is_checked_out_again(scio_repo: Path) -> None:
    cfg = make_cfg(scio_repo)
    path = prepare_worktree(cfg, make_incident(), git=RecordingGit(), uv=FakeRunner())
    _git("worktree", "remove", "--force", str(path), cwd=scio_repo)
    assert not path.exists()

    again = prepare_worktree(cfg, make_incident(), git=RecordingGit(), uv=FakeRunner())
    assert again == path and _branch_of(path) == BRANCH


def test_a_worktree_on_another_branch_is_refused_not_reused(scio_repo: Path) -> None:
    cfg = make_cfg(scio_repo)
    path = worktree_path(cfg, make_incident())
    _git("worktree", "add", "-b", "other", str(path), "main", cwd=scio_repo)
    with pytest.raises(RuntimeError, match="not fix/95-recovery-abcdef"):
        prepare_worktree(cfg, make_incident(), git=RecordingGit(), uv=FakeRunner())


@pytest.mark.parametrize(
    ("fragment", "named"),
    [("fetch origin", "git fetch origin"), ("worktree add", "git worktree add")],
)
def test_a_failed_git_step_raises_naming_the_command(
    tmp_path: Path, fragment: str, named: str
) -> None:
    git = FakeRunner(results={fragment: CommandResult(128, "", "fatal: no such remote")})
    uv = FakeRunner()
    with pytest.raises(RuntimeError, match=named) as err:
        prepare_worktree(make_cfg(tmp_path / "SCIO"), make_incident(), git=git, uv=uv)
    assert "fatal: no such remote" in str(err.value)
    assert uv.calls == [], "nothing is synced after a failed git step"


def test_a_failed_or_hung_uv_sync_raises_naming_the_command(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path / "SCIO")
    failing = FakeRunner(default=CommandResult(2, "", "lockfile out of date"))
    with pytest.raises(RuntimeError, match="uv sync --frozen"):
        prepare_worktree(cfg, make_incident(), git=FakeRunner(), uv=failing)
    hung = FakeRunner(default=CommandResult(-1, "", "", timed_out=True))
    with pytest.raises(RuntimeError, match=r"uv sync .*timed out"):
        prepare_worktree(cfg, make_incident(), git=FakeRunner(), uv=hung)


# -- the session's argv and environment ----------------------------------------


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_the_argv_loads_scio_rules_under_auto_with_the_deny_list(tmp_path: Path) -> None:
    argv = repair_argv(make_cfg(tmp_path), max_turns=7, max_budget_usd=1.5)

    for never in ("--safe-mode", "--bare", "--restricted", "--allowedTools", "--allowed-tools"):
        assert never not in argv
    assert argv[0] == "-p"
    assert _flag(argv, "--permission-mode") == "auto"
    assert _flag(argv, "--permission-prompts") == "none"
    assert _flag(argv, "--output-format") == "json"
    assert json.loads(_flag(argv, "--json-schema")) == VERDICT_SCHEMA
    assert _flag(argv, "--max-turns") == "7"
    assert _flag(argv, "--max-budget-usd") == "1.50"
    start = argv.index("--disallowedTools") + 1
    denied = argv[start : start + len(DENIED_TOOLS)]
    assert tuple(denied) == DENIED_TOOLS
    assert argv[start + len(DENIED_TOOLS)] == "--name", "--name ends the variadic deny list"
    assert _flag(argv, "--name") == "scio-recovery"


def test_the_environment_drops_secrets_and_nesting_and_stays_offline() -> None:
    env = repair_env(
        {
            "PATH": "/bin",
            "CLAUDECODE": "1",
            "ANTHROPIC_API_KEY": "sk-ant-x",
            "anthropic_auth_token": "t",
            "SCIO_PROVIDER_OVERRIDE": "claude_code",
            "SCIO_WORKER_BACKEND": "claude_code",
            "scio_parallel_features": "2",
            "SCIO_CHECKPOINT_DSN": "sqlite:///C:/ws/.scio/checkpoints.sqlite",
        }
    )
    # every workspace SCIO_* setting is dropped, so none reaches the session's gate
    assert env == {"PATH": "/bin", "SCIO_PROVIDER_OVERRIDE": "fake", "UV_SYSTEM_CERTS": "1"}


def test_the_session_metrics_come_from_claude_codes_envelope_not_the_answer(
    tmp_path: Path,
) -> None:
    """The drill sets the repair caps from these (I-099), so the model's own claim about
    its turns or cost is never what is recorded."""
    cfg = make_cfg(tmp_path / "SCIO", LIMITS)
    metrics = {"num_turns": 42, "total_cost_usd": 3.25, "duration_ms": 61000, "session_id": "s-1"}
    answer = {**GOOD, "num_turns": 1, "cost_usd": 0.0}
    good = FakeRunner(default=_result(structured_output=answer, **metrics))
    verdict = run_repair(cfg, make_incident(), tmp_path / "wt", "brief", claude=good)
    assert verdict.verdict == "repaired"
    got = (verdict.num_turns, verdict.cost_usd, verdict.duration_ms, verdict.session_id)
    assert got == (42, 3.25, 61000, "s-1")
    capped = FakeRunner(default=_result(is_error=True, subtype="error_max_turns", **metrics))
    stopped = run_repair(cfg, make_incident(), tmp_path / "wt", "brief", claude=capped)
    assert stopped.verdict == "cannot_repair" and stopped.num_turns == 42


# -- run_repair ------------------------------------------------------------------

LIMITS = Limits(repair_timeout_s=12.0, repair_max_turns=7, repair_max_budget_usd=1.5)

GOOD = {
    "verdict": "repaired",
    "pr_number": 101,
    "branch": BRANCH,
    "ledger_id": "I-180",
    "root_cause": "a hung claude escaped every guard",
    "regression_tests": ["tests/unit/test_x.py::test_y"],
    "gate": "passed",
    "risks": [],
    "note": "",
}


def _result(**fields: Any) -> CommandResult:
    data = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    data.update(fields)
    return CommandResult(0, json.dumps(data), "")


@pytest.fixture
def session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variables a repair session must not inherit, set so their absence means
    something."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "claude_code")


@pytest.mark.usefixtures("session_env")
def test_a_good_structured_answer_becomes_the_verdict(tmp_path: Path) -> None:
    claude = FakeRunner(default=_result(structured_output=GOOD))
    cfg = make_cfg(tmp_path / "SCIO", LIMITS)
    verdict = run_repair(cfg, make_incident(), tmp_path / "wt", "THE BRIEF", claude=claude)

    assert verdict == RepairVerdict.from_dict(GOOD)
    [call] = claude.calls
    assert call.argv[0] == "claude"
    assert call.argv[1:] == repair_argv(cfg, max_turns=7, max_budget_usd=1.5)
    assert call.cwd == tmp_path / "wt"
    assert call.stdin == "THE BRIEF"
    assert call.timeout_s == 12.0
    assert call.env is not None
    for name in ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        assert name not in call.env
    assert call.env["SCIO_PROVIDER_OVERRIDE"] == "fake"
    assert call.env.get("PATH") == os.environ.get("PATH")


def test_the_binary_comes_from_the_config(tmp_path: Path) -> None:
    cfg = WorkspaceConfig(
        workspace=tmp_path,
        launcher=("sh", "scio.sh"),
        scio_repo=tmp_path,
        scio_gh_repo="o/SCIO",
        live_wt=tmp_path,
        claude_bin="C:/tools/claude.exe",
    )
    claude = FakeRunner(default=_result(structured_output=GOOD))
    run_repair(cfg, make_incident(), tmp_path, "b", claude=claude)
    assert claude.calls[0].argv[0] == "C:/tools/claude.exe"


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (CommandResult(-1, "", "", timed_out=True), "timed out after 12 s"),
        (CommandResult(1, "Traceback: junk", "boom"), "exited 1 with no JSON"),
        (CommandResult(1, "", "not logged in"), "not logged in"),
        (
            _result(is_error=True, subtype="error_max_turns", result=""),
            "subtype=error_max_turns",
        ),
        (
            _result(is_error=True, subtype="error_during_execution", result="API down"),
            "API down",
        ),
        (
            CommandResult(
                1,
                json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True}),
                "",
            ),
            "subtype=error_max_budget_usd",
        ),
        (CommandResult(0, json.dumps([1, 2]), ""), "not a JSON object"),
        (_result(), "no structured_output"),
        (_result(structured_output={**GOOD, "verdict": "fixed"}), "answer.verdict is 'fixed'"),
        (
            _result(structured_output={k: v for k, v in GOOD.items() if k != "root_cause"}),
            "lacks the required key 'root_cause'",
        ),
        (_result(structured_output={**GOOD, "pr_number": True}), "answer.pr_number is bool"),
        (
            _result(structured_output={**GOOD, "regression_tests": ["a", 3]}),
            r"answer.regression_tests\[1\] is int",
        ),
        (_result(structured_output="repaired"), "answer is str, not object"),
    ],
    ids=[
        "timeout",
        "junk",
        "no-stdout",
        "max-turns",
        "is-error",
        "budget-cap-exit-1",
        "not-object",
        "no-structured-output",
        "bad-enum",
        "missing-key",
        "bool-as-int",
        "bad-item",
        "not-an-object-answer",
    ],
)
def test_every_failed_session_becomes_cannot_repair_with_the_reason(
    tmp_path: Path, result: CommandResult, reason: str
) -> None:
    cfg = make_cfg(tmp_path / "SCIO", LIMITS)
    verdict = run_repair(cfg, make_incident(), tmp_path, "b", claude=FakeRunner(default=result))
    assert verdict.verdict == "cannot_repair"
    assert verdict.pr_number is None
    assert re.search(reason, verdict.note), verdict.note


def test_a_null_pr_number_and_extra_keys_still_fit_the_schema(tmp_path: Path) -> None:
    answer = {"verdict": "not_scio", "root_cause": "r", "gate": "not_run", "note": "n", "x": 1}
    claude = FakeRunner(default=_result(structured_output={**answer, "pr_number": None}))
    verdict = run_repair(make_cfg(tmp_path), make_incident(), tmp_path, "b", claude=claude)
    assert verdict == RepairVerdict(verdict="not_scio", root_cause="r", gate="not_run", note="n")


# -- verify_claim ----------------------------------------------------------------

HEAD_SHA = "f" * 40
PRINTS = {"live_wt_head": "aaa", "product_head": "bbb", "checkpoints_sha256": "ccc"}


def _pr(**fields: Any) -> PullRequest:
    base: dict[str, Any] = {
        "number": 101,
        "state": "OPEN",
        "merged_at": None,
        "merge_sha": "",
        "head_ref": BRANCH,
        "head_sha": HEAD_SHA,
        "body": "Fixes the hang.\n\nCloses #95\n",
        "is_draft": True,
    }
    base.update(fields)
    return PullRequest(**base)


GOOD_FILES = ["scio/x.py", "tests/unit/test_x.py", "docs/ISSUE_LEDGER.md"]


def _gh(
    pr: PullRequest | None = None, files: list[str] | None = None, ci: CiStatus = "success"
) -> FakeGh:
    pr = pr if pr is not None else _pr()
    return FakeGh(
        prs={pr.number: pr},
        files={pr.number: GOOD_FILES if files is None else files},
        ci={pr.head_sha: ci},
    )


def _verify(
    verdict: RepairVerdict | None = None,
    gh: GitHub | None = None,
    after: Mapping[str, str] | None = None,
    tmp: Path = Path("."),
) -> list[str]:
    return verify_claim(
        verdict or RepairVerdict.from_dict(GOOD),
        make_incident(),
        make_cfg(tmp / "SCIO"),
        gh=gh or _gh(),
        before=PRINTS,
        after=PRINTS if after is None else after,
    )


def test_a_true_claim_has_no_problem() -> None:
    assert _verify() == []


def test_a_claimed_pr_that_does_not_exist_is_a_problem() -> None:
    problems = _verify(gh=FakeGh())
    assert problems == ["no PR: #101 does not exist"]
    no_number = RepairVerdict.from_dict({**GOOD, "pr_number": None})
    assert _verify(verdict=no_number) == ["no PR: the verdict has no pr_number"]


def test_each_broken_part_of_the_claim_is_its_own_problem() -> None:
    pr = _pr(head_ref="fix/95-other", body="Refs #95", state="CLOSED")
    problems = _verify(gh=_gh(pr, files=["scio/x.py"], ci="failure"))
    assert problems == [
        "PR #101 is closed without a merge",
        f"PR #101 head branch is fix/95-other, not {BRANCH}",
        "PR #101 body has no `Closes #95`",
        f"ci failure on PR #101 head {HEAD_SHA}",
        "PR #101 changes no file under tests/",
        "PR #101 does not change docs/ISSUE_LEDGER.md",
    ]


def test_a_merged_pr_is_accepted_and_a_wrong_claimed_branch_is_not() -> None:
    merged = _pr(state="MERGED", merged_at="2026-11-14T10:00:00Z", merge_sha="m" * 40)
    assert _verify(gh=_gh(merged)) == []
    lying = RepairVerdict.from_dict({**GOOD, "branch": "fix/95-elsewhere"})
    assert _verify(verdict=lying) == [f"the verdict names branch fix/95-elsewhere, not {BRANCH}"]


@pytest.mark.parametrize("ci", ["pending", "none"])
def test_ci_not_finished_is_reported_as_pending(ci: CiStatus) -> None:
    assert _verify(gh=_gh(ci=ci)) == [CI_PENDING]
    assert CI_PENDING == "ci pending"


def test_a_changed_fingerprint_is_named_by_its_key() -> None:
    after = {**PRINTS, "product_head": "zzz"}
    after.pop("checkpoints_sha256")
    assert _verify(after=after) == [
        "fingerprint checkpoints_sha256 changed: ccc -> (none)",
        "fingerprint product_head changed: bbb -> zzz",
    ]


def test_a_verdict_that_is_not_repaired_is_a_problem_and_fingerprints_still_count() -> None:
    gh = FakeGh()  # never read: there is no PR to check
    not_scio = RepairVerdict(verdict="not_scio", note="the product's own bug")
    assert _verify(verdict=not_scio, gh=gh) == ["the verdict is not_scio, not repaired"]
    moved = {**PRINTS, "live_wt_head": "moved"}
    assert _verify(verdict=not_scio, gh=gh, after=moved) == [
        "the verdict is not_scio, not repaired",
        "fingerprint live_wt_head changed: aaa -> moved",
    ]


@pytest.mark.parametrize(
    ("body", "closes"),
    [
        ("Closes #95", True),
        ("closes #95.", True),
        ("Fixes #95 and more", True),
        ("Resolved: #95", True),
        ("Closes #950", False),
        ("Closes #9", False),
        ("Refs #95", False),
        ("Closes 95", False),
        ("Encloses #95", False),
    ],
)
def test_closes_matches_only_this_issue(body: str, closes: bool) -> None:
    assert closes_issue(body, 95) is closes
