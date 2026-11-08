"""Parallel features (ADR 0018, issue #82): a milestone's independent features build at once,
each in its own git worktree, and the orchestrator alone merges them onto the milestone branch.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evals.fake_scripts import Edit, PerThreadResponder, Step, TaskScript
from evals.product_scripts import PRODUCT_SCRIPTS
from scio.graph.checkpointer import open_checkpointer
from scio.graph.run_context import RunContext
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeReply
from scio.harness.types import CompletionRequest
from scio.product import resume_product, run_product
from scio.product.build import (
    MILESTONES_DIR,
    build_product_build_graph,
    build_thread_id,
    resume_build,
    run_build,
)
from scio.product.parallel import merge_feature, worktrees_root
from scio.product.progress import PRODUCT_MD
from scio.state.models import MilestonePlan
from tests.graph.test_product_build_fake import (
    PRODUCT,
    PROMPT,
    Crash,
    Responder,
    _git,
    _harness,
    _settings,
)

THREAD = build_thread_id(PRODUCT)  # build-todo_cli-m1


def _thread(fid: str, attempt: int = 1) -> str:
    return f"{THREAD}-{fid.lower()}-a{attempt}"


class _Requests:
    """Wraps a responder and records the ``thread`` tag of every provider request: the
    harness tags each call with the task-graph thread that made it."""

    def __init__(self, inner: Responder) -> None:
        self.inner = inner
        self.threads: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, req: CompletionRequest) -> FakeReply:
        with self._lock:
            self.threads.append(req.tags.get("thread", ""))
        reply: FakeReply = self.inner(req)
        return reply

    def count(self, thread: str) -> int:
        with self._lock:
            return self.threads.count(thread)


def _bootstrap(tmp_path: Path, responder: Responder) -> Harness:
    """``scio new`` on the Fake: ``tmp_path/product`` becomes the bootstrapped product."""
    harness: Harness = _harness(_settings(tmp_path), responder)
    boot = RunContext.create(
        thread_id="new-1",
        sandbox_root=tmp_path / "product",
        tier="T1",
        tier_pin="T1",
        settings=harness.settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
    )
    try:
        run_product(boot, prompt=PROMPT)
        resume_product(boot, "answers")
        assert resume_product(boot, "approve").state.phase == "done"
    finally:
        boot.close()
    return harness


def _build_ctx(tmp_path: Path, harness: Harness, n: int) -> RunContext:
    """The build context with ``parallel_features=n``; a second call is a fresh process."""
    return RunContext.create(
        thread_id=THREAD,
        sandbox_root=tmp_path / "product",
        tier="T1",
        tier_pin="T1",
        settings=harness.settings.model_copy(update={"parallel_features": n}),
        harness=harness,
        work_dir=tmp_path / ".scio",
    )


def _parallel_product(tmp_path: Path, responder: Responder, n: int = 2) -> RunContext:
    """``scio new`` on the Fake, then a build context with ``parallel_features=n``."""
    return _build_ctx(tmp_path, _bootstrap(tmp_path, responder), n)


HEADER_RE = re.compile(r"^### (?P<id>P\d+): .*$", re.MULTILINE)


def _retitle(repo: Path, titles: dict[str, str]) -> None:
    """Give features new descriptions in PRODUCT.md before the build, so their task titles
    (``Pn: <description>``) pick the scripts these tests add. Criteria stay as they are."""
    md = repo / PRODUCT_MD
    text = md.read_text(encoding="utf-8")

    def header(m: re.Match[str]) -> str:
        fid = m.group("id")
        return f"### {fid}: {titles[fid]}" if fid in titles else m.group(0)

    md.write_text(HEADER_RE.sub(header, text), encoding="utf-8", newline="\n")


def _use_scripts(monkeypatch: pytest.MonkeyPatch, *scripts: TaskScript) -> None:
    """``script_for`` imports PRODUCT_SCRIPTS when it is called; these are searched first."""
    monkeypatch.setattr("evals.product_scripts.PRODUCT_SCRIPTS", [*scripts, *PRODUCT_SCRIPTS])


def _changelog(line: str) -> Edit:
    return Edit("CHANGELOG.md", "### Added\n", f"### Added\n- {line}\n")


def _script(key: str, edits: list[Edit], test_filter: str, files: list[str]) -> TaskScript:
    return TaskScript(
        key=key,
        tier="T1",
        features=[("F1", f"{key} works", test_filter)],
        steps=[Step("S1", key, ["F1"], files, edits=edits, test_filter=test_filter)],
        docs=[_changelog(f"{key.capitalize()}.")],
    )


@dataclass
class _Merge:
    """What one ``merge_feature`` call left behind in the product tree."""

    feature: str
    attempt: int
    result: dict[str, Any]
    before: str  # the milestone head before the call
    after: str  # ... and after it
    checked_out: str
    status: str  # `git status --porcelain` of the product tree
    merges: str  # the merge commits' subjects on the milestone branch


def _spy_merges(monkeypatch: pytest.MonkeyPatch, repo: Path) -> list[_Merge]:
    real = merge_feature  # what scio.product.build calls
    seen: list[_Merge] = []

    def spy(
        ctx: RunContext, plan: MilestonePlan, fid: str, attempt: int, branch: str, head: str
    ) -> dict[str, Any]:
        before = _git(repo, "rev-parse", plan.branch)
        out = real(ctx, plan, fid, attempt, branch, head)
        seen.append(
            _Merge(
                fid,
                attempt,
                out,
                before,
                _git(repo, "rev-parse", plan.branch),
                _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                _git(repo, "status", "--porcelain"),
                _git(repo, "log", "--merges", "--format=%s", plan.branch),
            )
        )
        return out

    monkeypatch.setattr("scio.product.build.merge_feature", spy)
    return seen


def _pending_interrupts(ctx: RunContext) -> list[Any]:
    """Every interrupt the build thread is paused on, read from its checkpoint."""
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        snap = graph.get_state({"configurable": {"thread_id": ctx.thread_id}})
    return [i.value for t in snap.tasks for i in t.interrupts]


def _worktrees(repo: Path) -> list[str]:
    return [ln for ln in _git(repo, "worktree", "list").splitlines() if ln.strip()]


def _leftovers(repo: Path) -> list[str]:
    """The product tree's changes, but for the milestone PR body that ``merge_wait`` leaves
    untracked for the human (offline builds write it there)."""
    body = f"?? {MILESTONES_DIR}/M1-pr.md"
    return [ln for ln in _git(repo, "status", "--porcelain").splitlines() if ln != body]


def test_two_independent_features_build_at_once_and_both_reach_the_milestone(
    tmp_path: Path,
) -> None:
    """P1 (an entry-point test) and P2 (a store and two subcommands) share no file but
    CHANGELOG.md, where each documenter adds a line at the same place. They run in two
    worktrees at once; P1 fast-forwards the milestone branch, P2 merges onto it with git's
    union driver for CHANGELOG.md and the suite passes on the merged tree."""
    responder = PerThreadResponder(independent=True)
    ctx = _parallel_product(tmp_path, responder)
    repo = tmp_path / "product"
    try:
        out = run_build(ctx)
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-4:]
    finally:
        ctx.close()
    assert [(r.feature_id, r.attempt, r.status) for r in s.feature_runs] == [
        ("P1", 1, "passed"),
        ("P2", 1, "passed"),
    ]
    texts = [e.text for e in s.ledger]
    assert any("2 features at once from milestone/1" in t for t in texts), texts
    assert any("P1 passed" in t and "fast-forwarded" in t for t in texts), texts
    assert any("P2 passed" in t and "suite passed" in t for t in texts), texts
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "milestone/1").splitlines()
    assert {"tests/test_entry.py", "todo_cli/store.py", "tests/test_add_list.py"} <= set(tracked)
    changelog = _git(repo, "show", "milestone/1:CHANGELOG.md")
    assert "(P1)" in changelog and "(P2)" in changelog  # the union driver kept both lines
    merges = _git(repo, "log", "--merges", "--format=%s", "milestone/1")
    assert "[scio:feature-merge:M1:P2:a1]" in merges
    # the worktrees are gone, outside the product tree they never were in
    assert not any(worktrees_root(ctx).glob("*")) if worktrees_root(ctx).exists() else True
    assert "worktrees" not in _git(repo, "status", "--porcelain")
    assert _git(repo, "worktree", "list").count("\n") == 0  # the product tree only


TITLE_LINE = "# Todo CLI\n"  # README.md's first line in the scaffold


def _banner(key: str, line: str) -> TaskScript:
    """A line inserted right under README.md's title: two of these conflict textually, and
    either still applies on a tree that has the other. The smoke test proves it (existing
    tests count)."""
    return _script(
        key,
        [Edit("README.md", TITLE_LINE, f"{TITLE_LINE}{line}\n")],
        "version_flag",
        ["README.md"],
    )


def test_a_textual_conflict_is_undone_and_the_feature_runs_again_from_the_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 and P2 are planned independent but both insert a line at the same place in
    README.md. P1 fast-forwards the milestone branch; P2's merge conflicts, is aborted and
    reset, and P2's run is recorded failed with exit reason ``merge``. Its attempt 2 runs
    serially in the product tree from the new head, where its insertion applies beside P1's,
    so it passes and fast-forwards: the milestone has both lines and P2's a1 never merged."""
    _use_scripts(
        monkeypatch,
        _banner("banner alpha", "Alpha banner."),
        _banner("banner beta", "Beta banner."),
    )
    responder = PerThreadResponder(independent=True)
    ctx = _parallel_product(tmp_path, responder)
    repo = tmp_path / "product"
    _retitle(repo, {"P1": "Banner alpha under the title", "P2": "Banner beta under the title"})
    merges = _spy_merges(monkeypatch, repo)
    try:
        out = run_build(ctx)
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-4:]
    finally:
        ctx.close()

    # the batch: P1 fast-forwarded, P2 conflicted in README.md and was undone at once
    assert [(m.feature, m.attempt, m.result["ok"]) for m in merges] == [
        ("P1", 1, True),
        ("P2", 1, False),
    ]
    p2 = merges[1]
    assert p2.result["how"] == "conflict in README.md", p2.result
    assert p2.after == p2.before == merges[0].after  # the milestone head is P1's, untouched
    assert p2.checked_out == "milestone/1" and p2.status == "", p2.status
    assert p2.merges == ""  # no merge commit of P2 (P1's was a fast-forward)

    runs = [(r.feature_id, r.attempt, r.status, r.exit_reason) for r in s.feature_runs]
    assert runs == [
        ("P1", 1, "passed", "success"),
        ("P2", 1, "failed", "merge"),
        ("P2", 2, "passed", "success"),
    ]
    assert s.feature_runs[2].thread_id == _thread("P2", 2)
    texts = [e.text for e in s.ledger]
    assert any("2 features at once from milestone/1" in t for t in texts), texts
    assert any(
        "P2 passed in its worktree; its merge onto milestone/1: conflict in README.md" in t
        and "The merge is undone, and attempt 2 runs alone from the new head" in t
        for t in texts
    ), texts
    # attempt 2 is the serial loop: a thread of its own, in the product tree, from P1's head
    assert any(f"P2: starting thread {_thread('P2', 2)}" in t for t in texts), texts
    assert not any("features at once" in t and "-p2-a2" in t for t in texts), texts
    p1_head, p2_head = s.feature_runs[0].commit_sha, s.feature_runs[2].commit_sha
    assert p1_head and p2_head
    _git(repo, "merge-base", "--is-ancestor", p1_head, p2_head)  # raises when it is not
    readme = _git(repo, "show", "milestone/1:README.md")
    assert readme.startswith(f"{TITLE_LINE}Beta banner.\nAlpha banner.\n"), readme[:80]
    subjects = _git(repo, "log", "--format=%s", "milestone/1")
    assert "[scio:feature-merge:M1:P2:a1]" not in subjects
    assert _leftovers(repo) == []
    assert len(_worktrees(repo)) == 1  # the product tree only


ABSENCE_TEST = """from pathlib import Path


def test_absence_check():
    root = Path(__file__).resolve().parent.parent
    assert not (root / "todo_cli" / "plugin.py").exists()
"""
PLUGIN = '"""A plugin constant."""\n\nPLUGIN = "on"\n'
PLUGIN_TEST = """from todo_cli.plugin import PLUGIN


def test_plugin_constant():
    assert PLUGIN == "on"
"""


def test_a_clean_merge_that_turns_the_suite_red_is_undone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 adds a test that ``todo_cli/plugin.py`` does not exist; P2 creates that module.
    Each passes in its own worktree and the merge is textually clean, but the suite on the
    merged tree is red, so P2's merge is undone and recorded ``merge``. Attempt 2 runs from
    the new head, where P1's test fails whatever P2 does: it cannot pass, so the build pauses
    at P2's feature decision, and the module never reaches the milestone branch."""
    _use_scripts(
        monkeypatch,
        _script(
            "absence check",
            [Edit("tests/test_absence_check.py", "", ABSENCE_TEST)],
            "absence_check",
            ["tests/test_absence_check.py"],
        ),
        _script(
            "plugin constant",
            [
                Edit("todo_cli/plugin.py", "", PLUGIN),
                Edit("tests/test_plugin_constant.py", "", PLUGIN_TEST),
            ],
            "plugin_constant",
            ["todo_cli/plugin.py", "tests/test_plugin_constant.py"],
        ),
    )
    responder = PerThreadResponder(independent=True)
    ctx = _parallel_product(tmp_path, responder)
    repo = tmp_path / "product"
    _retitle(repo, {"P1": "Absence check: no plugin module yet", "P2": "Plugin constant module"})
    merges = _spy_merges(monkeypatch, repo)
    try:
        out = run_build(ctx)
        s = out.state
        # attempt 2 cannot pass on a tree with P1's test: the human decides
        assert out.paused and out.interrupt, s.ledger[-4:]
        assert out.interrupt["feature"] == "P2"
        assert out.interrupt["thread"] == _thread("P2", 2)
    finally:
        ctx.close()

    assert [(m.feature, m.attempt, m.result["ok"]) for m in merges] == [
        ("P1", 1, True),
        ("P2", 1, False),
    ]
    p2 = merges[1]
    assert p2.result["how"] == "merged cleanly, but the suite on the merged tree is failed"
    assert p2.after == p2.before and p2.status == "" and p2.merges == ""
    assert [(r.feature_id, r.attempt, r.status, r.exit_reason) for r in s.feature_runs] == [
        ("P1", 1, "passed", "success"),
        ("P2", 1, "failed", "merge"),
    ]
    texts = [e.text for e in s.ledger]
    assert any(
        t.startswith("P2 passed in its worktree")
        and "the suite on the merged tree is failed" in t
        and "The merge is undone" in t
        for t in texts
    ), texts
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "milestone/1").splitlines()
    assert "tests/test_absence_check.py" in tracked
    assert "todo_cli/plugin.py" not in tracked and "tests/test_plugin_constant.py" not in tracked
    assert "[scio:feature-merge:M1:P2:a1]" not in _git(repo, "log", "--format=%s", "--all")


NOOP = TaskScript(
    key="edits nothing",
    tier="T1",
    features=[("F1", "nothing changes", "version_flag")],
    steps=[Step("S1", "change nothing", ["F1"], [], edits=[], test_filter="version_flag")],
    docs=[],
)


def test_one_feature_halts_while_the_other_passes_and_a_resume_never_reruns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1's script makes no edit, so its run halts; P2 passes beside it. The batch merges P2
    and stops once, for P1. 'skip' re-runs the batch node: P2's finished subgraph makes no
    provider call and is not merged again, P1 is recorded skipped and its worktree goes."""
    _use_scripts(monkeypatch, NOOP)
    requests = _Requests(PerThreadResponder(independent=True))
    ctx = _parallel_product(tmp_path, requests)
    repo = tmp_path / "product"
    _retitle(repo, {"P1": "Noop step that edits nothing"})
    try:
        out = run_build(ctx)
        s = out.state
        assert out.paused and out.interrupt, s.ledger[-4:]
        assert out.interrupt["feature"] == "P1" and out.interrupt["thread"] == _thread("P1")
        pending = _pending_interrupts(ctx)
        assert len(pending) == 1 and pending[0]["feature"] == "P1", pending
        # P2 reached the milestone branch while P1 waits for its decision
        assert "todo_cli/store.py" in _git(repo, "ls-tree", "-r", "--name-only", "milestone/1")
        p2_branch = "agent/p2-add-and-list-todos"
        _git(repo, "merge-base", "--is-ancestor", p2_branch, "milestone/1")
        assert (worktrees_root(ctx) / _thread("P1")).exists()  # kept for P1's decision
        p2_calls = requests.count(_thread("P2"))
        p1_calls = requests.count(_thread("P1"))
        assert p2_calls > 0 and p1_calls > 0

        out = resume_build(ctx, "skip")
        s = out.state
    finally:
        ctx.close()
    assert requests.count(_thread("P2")) == p2_calls  # P2's subgraph did not run again
    assert requests.count(_thread("P1")) == p1_calls  # nor P1's: the answer closed it
    assert not any(t.endswith("-a2") for t in requests.threads)
    runs = sorted((r.feature_id, r.attempt, r.status) for r in s.feature_runs)
    assert runs == [("P1", 1, "skipped"), ("P2", 1, "passed")]
    # the build goes on without P1: the gate overrides its pass and asks the human
    assert out.paused and s.phase == "gate", s.ledger[-4:]
    assert s.last_gate and "features not passed: P1" in s.last_gate.reasoning
    subjects = _git(repo, "log", "--format=%s", "milestone/1").splitlines()
    assert sum(x.startswith("feat: todo add then todo list") for x in subjects) == 1
    assert not (worktrees_root(ctx) / _thread("P1")).exists()
    assert not (worktrees_root(ctx) / _thread("P2")).exists()
    assert len(_worktrees(repo)) == 1  # the product tree only


@pytest.mark.parametrize(
    "crash_after", [1, 2], ids=["after-P1-fast-forward", "after-P2-merge-commit"]
)
def test_a_crash_mid_batch_resumes_without_duplicate_commits_or_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    """The process dies right after a merge returns (the effect is recorded, the batch's
    state is not). A fresh process resumes the same thread: neither subgraph runs again,
    no merge repeats, and the milestone branch has each feature's commits exactly once."""
    requests = _Requests(PerThreadResponder(independent=True))
    harness = _bootstrap(tmp_path, requests)
    repo = tmp_path / "product"
    real = merge_feature  # what scio.product.build calls
    calls: list[str] = []

    def crash_once(
        ctx: RunContext, plan: MilestonePlan, fid: str, attempt: int, branch: str, head: str
    ) -> dict[str, Any]:
        out = real(ctx, plan, fid, attempt, branch, head)
        calls.append(fid)
        if len(calls) == crash_after:
            raise Crash(f"killed after merging {fid}")
        return out

    monkeypatch.setattr("scio.product.build.merge_feature", crash_once)
    ctx = _build_ctx(tmp_path, harness, 2)
    try:
        with pytest.raises(Crash):
            run_build(ctx)
    finally:
        ctx.close()
    assert calls == ["P1", "P2"][:crash_after]
    before = {t: requests.count(t) for t in (_thread("P1"), _thread("P2"))}
    assert all(before.values()), before

    again = _build_ctx(tmp_path, harness, 2)  # a new process on the same build thread
    try:
        out = resume_build(again, None)
        s = out.state
        assert out.paused and s.phase == "merge_wait", s.ledger[-4:]
    finally:
        again.close()
    assert {t: requests.count(t) for t in before} == before  # no subgraph ran twice
    assert not any(t.endswith("-a2") for t in requests.threads)
    assert [(r.feature_id, r.attempt, r.status) for r in s.feature_runs] == [
        ("P1", 1, "passed"),
        ("P2", 1, "passed"),
    ]
    subjects = _git(repo, "log", "--format=%s", "milestone/1").splitlines()
    assert len(subjects) == len(set(subjects)), subjects  # no commit twice
    assert sum(x.startswith("feat:") for x in subjects) == 2
    assert sum(x.startswith("docs:") for x in subjects) == 2
    assert sum("[scio:feature-merge:M1:P2:a1]" in x for x in subjects) == 1
    assert not any("[scio:feature-merge:M1:P1:" in x for x in subjects)  # a fast-forward
    assert _leftovers(repo) == []
    assert len(_worktrees(repo)) == 1


HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _serial_build(tmp_path: Path, *, independent: bool) -> tuple[list[str], list[Any], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    responder = PerThreadResponder(independent=independent)
    ctx = _parallel_product(tmp_path, responder, n=1)
    try:
        out = run_build(ctx)
        assert out.paused and out.state.phase == "merge_wait", out.state.ledger[-4:]
    finally:
        ctx.close()
    texts = [HEX_RE.sub("<sha>", e.text) for e in out.state.ledger]
    runs = [
        (r.feature_id, r.attempt, r.status, r.exit_reason, r.iterations, r.thread_id)
        for r in out.state.feature_runs
    ]
    return texts, runs, worktrees_root(ctx)


def test_parallel_features_1_is_the_serial_loop_even_for_an_independent_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SCIO_PARALLEL_FEATURES=1`` (the default) never takes the batch path, whatever the
    plan says: no "features at once" line, no worktree, no merge commit, and the ledger and
    the feature runs are the ones a plan without ``depends_on`` gets."""

    def never(*_a: object, **_k: object) -> Any:
        raise AssertionError("the batch path ran with parallel_features=1")

    monkeypatch.setattr("scio.product.build._run_batch", never)
    monkeypatch.setattr("scio.product.build.ensure_worktree", never)
    monkeypatch.setattr("scio.product.build.merge_feature", never)
    # sibling directories of one length: the repository root is in every prompt, and the
    # Fake's token counts (so the ledger's costs) follow its length
    texts, runs, root = _serial_build(tmp_path / "par", independent=True)
    repo = tmp_path / "par" / "product"
    assert not root.exists()
    assert not any("features at once" in t for t in texts), texts
    assert [r[:4] for r in runs] == [("P1", 1, "passed", "success"), ("P2", 1, "passed", "success")]
    assert any("P1: starting thread" in t for t in texts) and any(
        "P2: starting thread" in t for t in texts
    )
    assert _git(repo, "log", "--merges", "--format=%s", "milestone/1") == ""
    assert len(_worktrees(repo)) == 1
    # the same build from a plan that says nothing about dependencies (today's loop)
    serial_texts, serial_runs, _ = _serial_build(tmp_path / "ser", independent=False)
    assert runs == serial_runs
    assert texts == serial_texts


GAMMA = _script("gamma note", [Edit("GAMMA.md", "", "Gamma.\n")], "version_flag", ["GAMMA.md"])


def test_a_feature_whose_merge_failed_retries_alone_even_with_an_unrun_feature_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three independent features, two at a time: P2's merge conflicts with P1's, and P3 has
    not run yet. Issue #82: the conflicted feature runs again serially from the new head;
    P3 comes after it."""
    _use_scripts(
        monkeypatch,
        _banner("banner alpha", "Alpha banner."),
        _banner("banner beta", "Beta banner."),
        GAMMA,
    )
    responder = PerThreadResponder(independent=True, milestone_size=3)
    ctx = _parallel_product(tmp_path, responder)
    repo = tmp_path / "product"
    _retitle(
        repo,
        {
            "P1": "Banner alpha under the title",
            "P2": "Banner beta under the title",
            "P3": "Gamma note file",
        },
    )
    try:
        out = run_build(ctx)
        s = out.state
    finally:
        ctx.close()
    texts = [e.text for e in s.ledger]
    batches = [t for t in texts if "features at once" in t]
    assert len(batches) == 1, batches  # today: a second batch runs P2 a2 beside P3
    assert any(f"P2: starting thread {_thread('P2', 2)}" in t for t in texts), texts


DEEPEST_PYC = "tests/acceptance/__pycache__/test_acceptance.cpython-312-pytest-9.1.1.pyc"


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_a_finished_worktree_is_removed_from_a_deep_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disjoint pair from a work directory 160 characters deep: the product tree's paths
    stay under Windows' 260-character MAX_PATH, a worktree's deepest file (pytest's bytecode
    of the acceptance test, which Python writes past it) does not. The worktrees must still
    be gone when the batch returns, as in the shallow run."""
    pad = 160 - len(str(tmp_path)) - 1
    if pad < 1:
        pytest.skip("the base temporary directory is already too deep")
    base = tmp_path / ("w" * pad)
    base.mkdir()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")  # whatever the machine's git says
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.longpaths")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
    ctx = _parallel_product(base, PerThreadResponder(independent=True))
    tree = worktrees_root(ctx) / _thread("P1")
    assert len(str(tree / DEEPEST_PYC)) > 260 > len(str(base / "product" / DEEPEST_PYC))
    try:
        out = run_build(ctx)
        assert out.paused and out.state.phase == "merge_wait", out.state.ledger[-4:]
    finally:
        ctx.close()
    assert len(_worktrees(base / "product")) == 1  # git forgot them either way
    assert not tree.exists() and not (worktrees_root(ctx) / _thread("P2")).exists()
