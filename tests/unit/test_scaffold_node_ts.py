"""The node-ts walking skeleton (ADR 0014): written by the scaffold, run by Node with no
download, probed and tested black-box the same way as the python one."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.product.acceptance import start_app, wait_healthy
from scio.product.scaffold import SCAFFOLDS, node_ts_scaffold, scaffold_for
from scio.state.models import AppSpec, ProductFeature, ProductSpec
from scio.toolchain import node_ts_toolchain
from tests.node_support import requires_node


def _spec(kind: str) -> ProductSpec:
    return ProductSpec(
        name="shop_api",
        title="Shop API",
        vision="A small shop service.",
        users=["a developer"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["starts"])],
        quality_bar=["tests"],
        stack="node-ts",
        dependencies=["zod@^3.22", "@scope/thing"],
        app=AppSpec(kind=kind, args=["serve", "--port", "{port}"]) if kind == "http" else AppSpec(),
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_scaffold_registry_and_file_shape() -> None:
    assert set(SCAFFOLDS) == {"python", "node-ts"}
    files = scaffold_for(_spec("cli"))
    assert files == node_ts_scaffold(_spec("cli"))
    for path in (
        "package.json",
        "tsconfig.json",
        "tsconfig.build.json",
        "eslint.config.js",
        "src/main.ts",
        "src/cli.ts",
        "src/version.ts",
        "tests/smoke.test.ts",
        "tests/acceptance/acceptance.test.ts",
        ".github/workflows/ci.yml",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        ".gitignore",
        "NOTES.md",
        "Dockerfile",
        ".dockerignore",
    ):
        assert path in files, path
    assert 'ENTRYPOINT ["node", "src/main.ts"]' in files["Dockerfile"]
    assert not any(p.endswith(".py") for p in files)
    assert (
        '"zod": "^3.22"' in files["package.json"] and '"@scope/thing": "*"' in files["package.json"]
    )
    assert '"name": "shop-api"' in files["package.json"]
    assert "node src/main.ts --version" in files["README.md"]
    assert (
        files[".gitignore"].startswith("node_modules/\n")
        and ".scio/sandbox/" in files[".gitignore"]
    )
    assert "setup-node" in files[".github/workflows/ci.yml"]
    with pytest.raises(KeyError, match="no scaffold"):
        scaffold_for(_spec("cli").model_copy(update={"stack": "cobol"}))


@requires_node
def test_cli_skeleton_runs_tests_and_lints_offline(tmp_path: Path) -> None:
    tc = node_ts_toolchain()
    _write(tmp_path, node_ts_scaffold(_spec("cli")))
    sb = LocalSandbox(tmp_path)
    r = sb.exec(["node", "src/main.ts", "--version"])
    assert r.ok and r.stdout.strip() == "0.1.0", r.output
    r = sb.exec(tc.test_argv(filter=None, path=None), timeout_s=120)
    assert tc.test_outcome(r.exit_code, r.stdout) == "passed", r.output
    # three smoke tests (two in-process for the coverage floor, M11) plus the cli acceptance
    # test, which spawns the entry file itself
    assert "# tests 4" in r.stdout and "# pass 4" in r.stdout
    # the acceptance test alone runs black-box and passes (it spawns the entry file)
    r = sb.exec(tc.test_argv(filter=None, path=f"{sb.root}/{tc.acceptance_dir}"), timeout_s=120)
    assert tc.test_outcome(r.exit_code, r.stdout) == "passed", r.output
    # lint without eslint installed is "not the agent's problem", a syntax check is real
    r = sb.exec(tc.lint_argv("."), timeout_s=120)
    assert tc.lint_findings(r.exit_code, r.stdout, r.stderr) is None
    assert sb.exec(tc.syntax_check_argv("src/cli.ts") or []).ok
    (tmp_path / "src" / "bad.ts").write_text("const x: number = ;\n", encoding="utf-8")
    assert not sb.exec(tc.syntax_check_argv("src/bad.ts") or []).ok
    # non-erasable TypeScript loads nowhere under type stripping; the gate must refuse it too
    (tmp_path / "src" / "pp.ts").write_text(
        "export class S {\n  constructor(private readonly p: string) {}\n}\n", encoding="utf-8"
    )
    r = sb.exec(tc.syntax_check_argv("src/pp.ts") or [])
    assert not r.ok and "ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX" in r.output


@requires_node
def test_http_skeleton_serves_its_health_path_and_passes_acceptance(tmp_path: Path) -> None:
    tc = node_ts_toolchain()
    spec = _spec("http")
    _write(tmp_path, node_ts_scaffold(spec))
    sb = LocalSandbox(tmp_path)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    argv = [
        *tc.runtime().app_argv,
        tc.app_target(spec.name, spec.app.module),
        "serve",
        "--port",
        str(port),
    ]
    assert argv[:2] == ["node", "src/main.ts"]
    app = start_app(sb, argv, url, tc)
    try:
        assert wait_healthy(app, spec.app.health_path, timeout_s=30)
        r = sb.exec(
            tc.test_argv(filter=None, path=f"{sb.root}/{tc.acceptance_dir}"),
            timeout_s=120,
            env={"APP_URL": url},
        )
        assert tc.test_outcome(r.exit_code, r.stdout) == "passed", r.output
        assert "# pass 1" in r.stdout and "# skipped 0" in r.stdout
    finally:
        log = app.stop()
    assert "serving on http://127.0.0.1" in log
