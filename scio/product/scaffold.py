"""Walking-skeleton scaffolds per toolchain profile (ADR 0010, ADR 0014).

A scaffold is a mapping of repository-relative paths to file text. Each stack's scaffold
installs nothing: the ``python`` one is a package with ``--version`` and one pytest smoke
test; the ``node-ts`` one is a TypeScript entry file run by Node 22's type stripping with one
``node:test`` smoke test. Both ship black-box acceptance tests under the profile's
``acceptance_dir`` that skip until ``scio build`` starts the app, CI running the profile's
lint and test commands, README, CHANGELOG, LICENSE, ``.gitignore`` from the profile's cache
excludes, and the product memory files. ``scaffold_for`` picks by ``spec.stack``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from scio.product.progress import ProgressFile
from scio.state.models import ProductSpec
from scio.toolchain import Toolchain, get_toolchain

MIT = """MIT License

Copyright (c) {year} {owner}

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify, merge,
publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""


def progress_json(spec: ProductSpec, *, bootstrap_commit: str | None) -> str:
    """``.scio/product/progress.json``: the memory across sessions (ADR 0007 decision 2).

    Every feature starts ``todo``, the walking skeleton included: the scaffold proves the
    toolchain, and ``scio build`` runs P1 as the first feature task (ADR 0011 decision 1).
    """
    return ProgressFile.initial(spec, bootstrap_commit=bootstrap_commit).to_json()


def _common_files(
    spec: ProductSpec,
    tc: Toolchain,
    *,
    owner: str,
    run_cmd: str,
    develop_cmds: list[str],
    skeleton_summary: str,
) -> dict[str, str]:
    """The stack-neutral part of every scaffold: README, CHANGELOG, LICENSE, .gitignore,
    NOTES."""
    gitignore = "".join(f"{p}\n" if "*" in p else f"{p}/\n" for p in tc.cache_excludes)
    return {
        "README.md": (
            f"# {spec.title}\n\n{spec.vision}\n\n"
            "## Run\n\n```\n"
            f"{run_cmd}\n"
            "```\n\n## Develop\n\n```\n" + "\n".join(develop_cmds) + "\n```\n\n"
            "Product specification: [PRODUCT.md](PRODUCT.md). "
            "Progress: `.scio/product/progress.json`.\n"
        ),
        "CHANGELOG.md": (
            f"# Changelog\n\n## [Unreleased]\n### Added\n- Walking skeleton: {skeleton_summary}, "
            "CI (SCIO bootstrap).\n"
        ),
        "LICENSE": MIT.format(year=datetime.now(UTC).year, owner=owner),
        ".dockerignore": ".git\n.scio\n" + "".join(f"{p}\n" for p in tc.cache_excludes),
        ".gitignore": gitignore + ".scio/sandbox/\n.scio/ledger/\n.scio/artifacts/\n",
        "NOTES.md": (
            "# Notes\n\nWorking notes SCIO and humans leave for the next session. "
            "Newest first.\n\n"
            f"- Bootstrapped by `scio new` ({datetime.now(UTC).date().isoformat()}): "
            "walking skeleton, CI, smoke test.\n"
        ),
    }


DOCKER_PORT = 8000


def dockerfile(spec: ProductSpec, tc: Toolchain) -> str:
    """The release artifact every scaffold ships (M11, PRODUCT_MODE component 5): the
    profile's ``release_image`` lines, then the same command the acceptance runner starts
    (``app_argv`` and target). An http product listens on 0.0.0.0 inside the container."""
    target = [*tc.runtime().app_argv, tc.app_target(spec.name, spec.app.module)]
    image = spec.name.replace("_", "-")
    head = (
        f"# The release image: docker build -t {image} . && docker run --rm {image}\n"
        + tc.release_image
    )
    if spec.app.kind == "http":
        args = [a.replace("{port}", str(DOCKER_PORT)) for a in spec.app.args]
        return (
            head
            + f"ENV HOST=0.0.0.0\nEXPOSE {DOCKER_PORT}\n"
            + f"CMD {json.dumps([*target, *args])}\n"
        )
    return head + f"ENTRYPOINT {json.dumps(target)}\nCMD {json.dumps(['--version'])}\n"


# ---------- python ----------


def python_scaffold(spec: ProductSpec, *, owner: str = "the author") -> dict[str, str]:
    tc: Toolchain = get_toolchain("python")
    pkg = spec.name
    test_cmd = " ".join(tc.test_argv(filter=None, path=None))
    lint_cmd = " ".join(tc.lint_argv("."))
    files = {
        "pyproject.toml": (
            "[project]\n"
            f'name = "{pkg}"\n'
            'version = "0.1.0"\n'
            f'description = "{spec.title}"\n'
            'requires-python = ">=3.12"\n'
            "dependencies = [" + ", ".join(f'"{d}"' for d in spec.dependencies) + "]\n\n"
            '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F", "I"]\n\n'
            # the coverage floor measures the product's own code, not its tests, and not the
            # two-line `python -m` entry that only a subprocess runs (M11)
            f'[tool.coverage.run]\nsource = ["{pkg}"]\nomit = ["*/__main__.py"]\n'
        ),
        f"{pkg}/__init__.py": f'"""{spec.title}."""\n\n__version__ = "0.1.0"\n',
        f"{pkg}/__main__.py": (f"from {pkg}.cli import main\n\nraise SystemExit(main())\n"),
        f"{pkg}/cli.py": _http_cli(pkg) if spec.app.kind == "http" else _cli(pkg),
        # Packages, so a test module may share its basename with one in another directory
        # (pytest's import-file-mismatch error otherwise, M9).
        "tests/__init__.py": "",
        f"{tc.acceptance_dir}/__init__.py": "",
        f"{tc.acceptance_dir}/conftest.py": (
            '"""Black-box acceptance tests: scio build runs them against the started app."""\n\n'
            "import os\n\n"
            "import pytest\n\n\n"
            "@pytest.fixture\n"
            "def app_url():\n"
            '    url = os.environ.get("APP_URL")\n'
            "    if not url:\n"
            '        pytest.skip("APP_URL not set: scio build starts the app for these tests")\n'
            "    return url\n"
        ),
        f"{tc.acceptance_dir}/test_acceptance.py": (
            _http_acceptance(pkg, spec.app.health_path)
            if spec.app.kind == "http"
            else _cli_acceptance(pkg)
        ),
        "Dockerfile": dockerfile(spec, tc),
        "tests/conftest.py": (
            "import pathlib\nimport sys\n\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        ),
        "tests/test_smoke.py": _python_smoke_tests(pkg, http=spec.app.kind == "http"),
        ".github/workflows/ci.yml": (
            "name: ci\n"
            "on: [push, pull_request]\n"
            "jobs:\n"
            "  checks:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n"
            "        with:\n"
            '          python-version: "3.12"\n'
            "      - run: pip install ruff pytest\n"
            f"      - run: {lint_cmd}\n"
            f"      - run: {test_cmd}\n"
            f"      - run: {tc.skeleton_command(pkg)}\n"
        ),
    }
    files.update(
        _common_files(
            spec,
            tc,
            owner=owner,
            run_cmd=tc.skeleton_command(pkg),
            develop_cmds=["pip install ruff pytest", lint_cmd, test_cmd],
            skeleton_summary="package, `--version`, smoke test",
        )
    )
    return files


def _python_smoke_tests(pkg: str, *, http: bool) -> str:
    """The skeleton's own tests, in-process: the coverage floor counts only code that runs in
    the test process (M11); the acceptance tests drive the real command line."""
    text = (
        f"from {pkg} import __version__\n"
        f"from {pkg}.cli import main\n\n\n"
        "def test_version_flag_prints_the_version(capsys):\n"
        '    assert main(["--version"]) == 0\n'
        "    assert capsys.readouterr().out.strip() == __version__\n\n\n"
        "def test_no_arguments_prints_usage():\n"
        "    assert main([]) == 2\n"
    )
    if not http:
        return text
    return (
        "import json\n"
        "import threading\n"
        "import urllib.error\n"
        "import urllib.request\n"
        "from http.server import HTTPServer\n\n"
        "import pytest\n\n"
        + text.replace(
            f"from {pkg}.cli import main\n", f"from {pkg}.cli import HEALTH_PATH, Handler, main\n"
        )
        + "\n\n"
        "def test_the_app_answers_its_health_path_and_404s_elsewhere():\n"
        '    server = HTTPServer(("127.0.0.1", 0), Handler)\n'
        "    threading.Thread(target=server.serve_forever, daemon=True).start()\n"
        "    try:\n"
        '        base = f"http://127.0.0.1:{server.server_port}"\n'
        "        with urllib.request.urlopen(base + HEALTH_PATH) as res:\n"
        '            assert json.load(res) == {"status": "ok", "version": __version__}\n'
        "        with pytest.raises(urllib.error.HTTPError) as missing:\n"
        '            urllib.request.urlopen(base + "/missing")\n'
        "        assert missing.value.code == 404\n"
        "    finally:\n"
        "        server.shutdown()\n"
        "        server.server_close()\n"
    )


def _cli(pkg: str) -> str:
    return (
        '"""Command line entry point: the walking skeleton."""\n\n'
        "import argparse\n\n"
        f"from {pkg} import __version__\n\n\n"
        "def main(argv=None):\n"
        f'    parser = argparse.ArgumentParser(prog="{pkg}")\n'
        '    parser.add_argument("--version", action="store_true", help="print the version")\n'
        "    args = parser.parse_args(argv)\n"
        "    if args.version:\n"
        "        print(__version__)\n"
        "        return 0\n"
        "    parser.print_usage()\n"
        "    return 2\n"
    )


def _http_cli(pkg: str) -> str:
    """A walking skeleton that serves: ``serve --port N`` answers the health path (M9)."""
    return (
        '"""Command line entry point: the walking skeleton with a health endpoint."""\n\n'
        "import argparse\n"
        "import json\n"
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n\n"
        f"from {pkg} import __version__\n\n"
        'HEALTH_PATH = "/health"\n\n\n'
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path == HEALTH_PATH:\n"
        '            body = json.dumps({"status": "ok", "version": __version__}).encode()\n'
        "            self.send_response(200)\n"
        '            self.send_header("Content-Type", "application/json")\n'
        "            self.end_headers()\n"
        "            self.wfile.write(body)\n"
        "            return\n"
        "        self.send_response(404)\n"
        "        self.end_headers()\n\n"
        "    def log_message(self, format, *args):\n"
        "        return\n\n\n"
        "# Process lifecycle: the black-box acceptance tests run it against the started app;\n"
        "# the coverage floor counts in-process code only (M11).\n"
        "def serve(port):  # pragma: no cover\n"
        '    host = os.environ.get("HOST", "127.0.0.1")  # the Dockerfile sets 0.0.0.0\n'
        "    server = HTTPServer((host, port), Handler)\n"
        '    print(f"serving on http://{host}:{port}", flush=True)\n'
        "    try:\n"
        "        server.serve_forever()\n"
        "    except KeyboardInterrupt:\n"
        "        pass\n"
        "    finally:\n"
        "        server.server_close()\n"
        "    return 0\n\n\n"
        "def main(argv=None):\n"
        f'    parser = argparse.ArgumentParser(prog="{pkg}")\n'
        '    parser.add_argument("--version", action="store_true", help="print the version")\n'
        '    sub = parser.add_subparsers(dest="cmd")\n'
        '    run = sub.add_parser("serve", help="start the HTTP server")\n'
        '    run.add_argument("--port", type=int, default=8000)\n'
        "    args = parser.parse_args(argv)\n"
        "    if args.version:\n"
        "        print(__version__)\n"
        "        return 0\n"
        '    if args.cmd == "serve":  # pragma: no cover\n'
        "        return serve(args.port)\n"
        "    parser.print_usage()\n"
        "    return 2\n"
    )


def _cli_acceptance(pkg: str) -> str:
    return (
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"from {pkg} import __version__\n\n"
        "ROOT = Path(__file__).resolve().parents[2]\n\n\n"
        "def test_version_command_black_box():\n"
        "    out = subprocess.run(\n"
        f'        [sys.executable, "-m", "{pkg}", "--version"],\n'
        "        cwd=ROOT,\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "    )\n"
        "    assert out.returncode == 0\n"
        "    assert out.stdout.strip() == __version__\n"
    )


def _http_acceptance(pkg: str, health_path: str) -> str:
    return (
        "import json\n"
        "import urllib.request\n\n"
        f"from {pkg} import __version__\n\n\n"
        "def test_health_endpoint_black_box(app_url):\n"
        f'    with urllib.request.urlopen(app_url + "{health_path}", timeout=5) as resp:\n'
        "        assert resp.status == 200\n"
        "        body = json.loads(resp.read())\n"
        '    assert body["status"] == "ok" and body["version"] == __version__\n'
    )


# ---------- node-ts ----------

NODE_DEV_DEPENDENCIES = {
    "typescript": "^5.8",
    "eslint": "^9.20",
    "@eslint/js": "^9.20",
    "typescript-eslint": "^8.25",
    "@types/node": "^22.13",
}
"""The product's own quality tools; its CI installs them, SCIO's sandbox never does."""


def _npm_dependency(spec: str) -> tuple[str, str] | None:
    """``zod@^3`` -> (``zod``, ``^3``); ``@scope/x`` -> (``@scope/x``, ``*``). A non-registry
    spec (``file:vendor/x``, ``github:o/r``, a URL or a path) -> None: its package name lives
    in the target's own manifest, so the install phase writes it (I-088)."""
    s = spec.strip()
    if ":" in s or s.startswith((".", "/")) or ("/" in s and not s.startswith("@")):
        return None
    body = s[1:] if s.startswith("@") else s
    name, sep, rng = body.partition("@")
    name = ("@" if s.startswith("@") else "") + name
    return name, (rng if sep and rng else "*")


def node_ts_scaffold(spec: ProductSpec, *, owner: str = "the author") -> dict[str, str]:
    """A TypeScript walking skeleton that needs no download: Node 22 strips the types,
    ``node:test`` runs the tests. Type checking and lint run once the product's own
    devDependencies are installed (its CI does; SCIO's quality bar reports them as skipped
    until then)."""
    tc: Toolchain = get_toolchain("node-ts")
    pkg = spec.name
    http = spec.app.kind == "http"
    test_cmd = " ".join(f'"{a}"' if "*" in a else a for a in tc.test_argv(filter=None, path=None))
    package = {
        "name": pkg.replace("_", "-"),
        "version": "0.1.0",
        "description": spec.title,
        "private": True,
        "type": "module",
        "engines": {"node": ">=22.18"},
        "scripts": {
            "start": tc.skeleton_command(pkg).replace(" --version", ""),
            "test": test_cmd,
            "lint": "eslint .",
            "typecheck": "tsc --noEmit",
            "build": "tsc -p tsconfig.build.json",
        },
        "dependencies": dict(
            dep for d in spec.dependencies if (dep := _npm_dependency(d)) is not None
        ),
        "devDependencies": NODE_DEV_DEPENDENCIES,
    }
    files = {
        "package.json": json.dumps(package, indent=2) + "\n",
        "tsconfig.json": json.dumps(
            {
                "compilerOptions": {
                    "target": "es2022",
                    "module": "nodenext",
                    "moduleResolution": "nodenext",
                    "strict": True,
                    "noEmit": True,
                    "allowImportingTsExtensions": True,
                    "erasableSyntaxOnly": True,
                    "verbatimModuleSyntax": True,
                    "types": ["node"],
                    "skipLibCheck": True,
                },
                "include": ["src", "tests"],
            },
            indent=2,
        )
        + "\n",
        "tsconfig.build.json": json.dumps(
            {
                "extends": "./tsconfig.json",
                "compilerOptions": {
                    "noEmit": False,
                    "allowImportingTsExtensions": False,
                    "rewriteRelativeImportExtensions": True,
                    "outDir": "dist",
                },
                "include": ["src"],
            },
            indent=2,
        )
        + "\n",
        "Dockerfile": dockerfile(spec, tc),
        "eslint.config.js": (
            'import js from "@eslint/js";\n'
            'import tseslint from "typescript-eslint";\n\n'
            "export default tseslint.config(\n"
            '  { ignores: ["dist/", "node_modules/", ".scio/", "coverage/"] },\n'
            "  js.configs.recommended,\n"
            "  ...tseslint.configs.recommended,\n"
            ");\n"
        ),
        "src/version.ts": f'/** {spec.title}. */\nexport const VERSION = "0.1.0";\n',
        "src/cli.ts": _node_http_cli(pkg, spec.app.health_path) if http else _node_cli(pkg),
        "src/main.ts": (
            'import { main } from "./cli.ts";\n\nprocess.exitCode = main(process.argv.slice(2));\n'
        ),
        "tests/smoke.test.ts": _node_smoke_tests(spec.app.health_path if http else None),
        f"{tc.acceptance_dir}/acceptance.test.ts": (
            _node_http_acceptance(spec.app.health_path) if http else _node_cli_acceptance()
        ),
        ".github/workflows/ci.yml": (
            "name: ci\n"
            "on: [push, pull_request]\n"
            "jobs:\n"
            "  checks:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-node@v4\n"
            "        with:\n"
            '          node-version: "22"\n'
            "      - run: npm install\n"
            "      - run: npm run lint\n"
            "      - run: npm run typecheck\n"
            "      - run: npm test\n"
            f"      - run: {tc.skeleton_command(pkg)}\n"
        ),
    }
    files.update(
        _common_files(
            spec,
            tc,
            owner=owner,
            run_cmd=tc.skeleton_command(pkg),
            develop_cmds=["npm install", "npm run lint", "npm run typecheck", "npm test"],
            skeleton_summary="TypeScript entry file, `--version`, node:test smoke test",
        )
    )
    return files


def _node_smoke_tests(health_path: str | None) -> str:
    """The skeleton's own tests. The coverage floor counts only code that runs in the test
    process (M11), so ``main`` and the app are exercised in-process; the spawned entry file
    proves the real command line on top."""
    head = (
        'import { test } from "node:test";\n'
        'import assert from "node:assert/strict";\n'
        'import { spawnSync } from "node:child_process";\n'
        + ('import type { AddressInfo } from "node:net";\n' if health_path else "")
        + "\n"
        + (
            'import { createApp, HEALTH_PATH, main } from "../src/cli.ts";\n'
            if health_path
            else 'import { main } from "../src/cli.ts";\n'
        )
        + 'import { VERSION } from "../src/version.ts";\n\n'
    )
    body = (
        'test("the entry file prints the version", () => {\n'
        '  const out = spawnSync(process.execPath, ["src/main.ts", "--version"], {\n'
        '    encoding: "utf8",\n'
        "  });\n"
        "  assert.equal(out.status, 0);\n"
        "  assert.equal(out.stdout.trim(), VERSION);\n"
        "});\n\n"
        'test("--version returns 0", () => {\n'
        '  assert.equal(main(["--version"]), 0);\n'
        "});\n\n"
        'test("no arguments is a usage error", () => {\n'
        "  assert.equal(main([]), 2);\n"
        "});\n"
    )
    if health_path is None:
        return head + body
    app = (
        "\n"
        'test("the app answers its health path and 404s elsewhere", async () => {\n'
        "  const server = createApp();\n"
        '  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));\n'
        "  try {\n"
        "    const { port } = server.address() as AddressInfo;\n"
        "    const ok = await fetch(`http://127.0.0.1:${port}${HEALTH_PATH}`);\n"
        "    assert.equal(ok.status, 200);\n"
        '    assert.deepEqual(await ok.json(), { status: "ok", version: VERSION });\n'
        "    assert.equal((await fetch(`http://127.0.0.1:${port}/missing`)).status, 404);\n"
        "  } finally {\n"
        "    server.close();\n"
        "  }\n"
        "});\n"
    )
    return head + body + app


def _node_cli(pkg: str) -> str:
    return (
        "/** Command line entry point: the walking skeleton. */\n"
        'import { VERSION } from "./version.ts";\n\n'
        "export function main(argv: readonly string[]): number {\n"
        '  if (argv[0] === "--version") {\n'
        "    console.log(VERSION);\n"
        "    return 0;\n"
        "  }\n"
        '  console.error("usage: node src/main.ts --version");\n'
        "  return 2;\n"
        "}\n"
    )


def _node_http_cli(pkg: str, health_path: str) -> str:
    """A walking skeleton that serves: ``serve --port N`` answers the health path."""
    return (
        "/** Command line entry point: the walking skeleton with a health endpoint. */\n"
        'import { createServer, type Server } from "node:http";\n\n'
        'import { VERSION } from "./version.ts";\n\n'
        f'export const HEALTH_PATH = "{health_path}";\n\n'
        "/** The app, not yet listening: tests listen on port 0 in-process (coverage counts\n"
        " * only in-process code), `serve` listens for real. */\n"
        "export function createApp(): Server {\n"
        "  return createServer((req, res) => {\n"
        '    if (req.method === "GET" && req.url === HEALTH_PATH) {\n'
        '      res.writeHead(200, { "Content-Type": "application/json" });\n'
        '      res.end(JSON.stringify({ status: "ok", version: VERSION }));\n'
        "      return;\n"
        "    }\n"
        "    res.writeHead(404);\n"
        "    res.end();\n"
        "  });\n"
        "}\n\n"
        "/* node:coverage disable */\n"
        "// Process lifecycle: the black-box acceptance tests run it against the started app.\n"
        "export function serve(port: number): number {\n"
        "  const server = createApp();\n"
        '  const host = process.env.HOST ?? "127.0.0.1"; // the Dockerfile sets 0.0.0.0\n'
        "  server.listen(port, host, () => {\n"
        "    console.log(`serving on http://${host}:${port}`);\n"
        "  });\n"
        '  process.on("SIGTERM", () => server.close());\n'
        "  return 0;\n"
        "}\n"
        "/* node:coverage enable */\n\n"
        "export function main(argv: readonly string[]): number {\n"
        '  if (argv[0] === "--version") {\n'
        "    console.log(VERSION);\n"
        "    return 0;\n"
        "  }\n"
        "  /* node:coverage disable */\n"
        '  if (argv[0] === "serve") {\n'
        '    const at = argv.indexOf("--port");\n'
        "    const port = at >= 0 ? Number(argv[at + 1]) : 8000;\n"
        "    return serve(port);\n"
        "  }\n"
        "  /* node:coverage enable */\n"
        '  console.error("usage: node src/main.ts --version | serve --port N");\n'
        "  return 2;\n"
        "}\n"
    )


def _node_cli_acceptance() -> str:
    return (
        "/** Black-box acceptance: the entry file as a user runs it. */\n"
        'import { test } from "node:test";\n'
        'import assert from "node:assert/strict";\n'
        'import { spawnSync } from "node:child_process";\n\n'
        'import { VERSION } from "../../src/version.ts";\n\n'
        'test("--version prints the version", () => {\n'
        '  const out = spawnSync(process.execPath, ["src/main.ts", "--version"], {\n'
        '    encoding: "utf8",\n'
        "  });\n"
        "  assert.equal(out.status, 0);\n"
        "  assert.equal(out.stdout.trim(), VERSION);\n"
        "});\n"
    )


def _node_http_acceptance(health_path: str) -> str:
    return (
        "/** Black-box acceptance: scio build starts the app and sets APP_URL. */\n"
        'import { test } from "node:test";\n'
        'import assert from "node:assert/strict";\n\n'
        'import { VERSION } from "../../src/version.ts";\n\n'
        "const APP_URL = process.env.APP_URL;\n\n"
        "test(\n"
        '  "the health endpoint answers",\n'
        '  { skip: APP_URL ? false : "APP_URL not set: scio build starts the app" },\n'
        "  async () => {\n"
        f"    const res = await fetch(`${{APP_URL}}{health_path}`);\n"
        "    assert.equal(res.status, 200);\n"
        "    const body = (await res.json()) as { status: string; version: string };\n"
        '    assert.equal(body.status, "ok");\n'
        "    assert.equal(body.version, VERSION);\n"
        "  },\n"
        ");\n"
    )


SCAFFOLDS: dict[str, Callable[..., dict[str, str]]] = {
    "python": python_scaffold,
    "node-ts": node_ts_scaffold,
}


def scaffold_for(spec: ProductSpec, *, owner: str = "the author") -> dict[str, str]:
    try:
        build = SCAFFOLDS[spec.stack]
    except KeyError:
        raise KeyError(
            f"no scaffold for stack {spec.stack!r}; known: {sorted(SCAFFOLDS)}"
        ) from None
    return build(spec, owner=owner)
