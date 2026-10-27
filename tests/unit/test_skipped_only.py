"""I-097: a test run in which every selected test was skipped proves nothing. Live p002's P1
was "proved" by black-box acceptance tests that skip without ``APP_URL``; verify accepted
the exit-0 run and only the model gate noticed, so the build halted instead of the coder
fixing it in its next turn."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from scio.aci.sandbox.local import LocalSandbox
from scio.aci.tools import SKIPPED_ONLY, RunTestsInput, run_tests
from scio.graph.nodes.verify import SKIP_HINT, _run, _run_selectors
from scio.graph.run_context import RunContext
from scio.toolchain import node_ts_toolchain, python_toolchain
from tests.node_support import requires_node


def test_both_profiles_recognise_a_run_that_only_skipped() -> None:
    py = python_toolchain()
    assert py.skipped_only("ss\n2 skipped in 0.01s\n")
    assert py.skipped_only("s\n1 skipped, 3 deselected in 0.02s\n")
    assert not py.skipped_only("..s\n2 passed, 1 skipped in 0.03s\n")
    assert not py.skipped_only("3 passed in 0.01s\n") and not py.skipped_only("")
    node = node_ts_toolchain()
    assert node.skipped_only("1..2\n# tests 2\n# pass 0\n# fail 0\n# skipped 2\n# todo 0\n")
    assert not node.skipped_only("# tests 3\n# pass 2\n# fail 0\n# skipped 1\n")
    assert not node.skipped_only("# tests 1\n# pass 1\n# skipped 0\n")


def _python_repo(root: Path) -> LocalSandbox:
    (root / "tests" / "acceptance").mkdir(parents=True)
    (root / "tests" / "acceptance" / "test_black_box.py").write_text(
        "import os\n\nimport pytest\n\n\n"
        "@pytest.mark.skipif(not os.environ.get('APP_URL'), reason='no APP_URL')\n"
        "def test_health_black_box():\n    assert True\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "tests" / "test_smoke.py").write_text(
        "def test_health_in_process():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return LocalSandbox(root)


def test_run_tests_says_so_and_verify_refuses_it_as_a_feature_proof(tmp_path: Path) -> None:
    sb = _python_repo(tmp_path)
    tc = python_toolchain()
    acceptance = f"{sb.root}/tests/acceptance/test_black_box.py"
    out = run_tests(sb, RunTestsInput(path=acceptance), toolchain=tc)
    assert out.startswith(SKIPPED_ONLY), out
    ctx = cast(RunContext, SimpleNamespace(sandbox=sb, toolchain=tc))
    text, ok = _run_selectors(ctx, "tests/acceptance/test_black_box.py")
    assert not ok and SKIP_HINT in text
    # the in-process test proves it; the whole suite (a regression check) passes either way
    assert _run_selectors(ctx, "tests/test_smoke.py")[1]
    assert _run(ctx, None)[1]


@requires_node
def test_the_node_runner_reports_a_skipped_only_selection(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "tests" / "a.test.ts").write_text(
        'import { test } from "node:test";\n\ntest("later", { skip: "no APP_URL" }, () => {});\n',
        encoding="utf-8",
        newline="\n",
    )
    out = run_tests(LocalSandbox(tmp_path), RunTestsInput(), toolchain=node_ts_toolchain())
    assert out.startswith(SKIPPED_ONLY), out
