"""Walking-skeleton scaffolds per toolchain profile (ADR 0010).

A scaffold is a mapping of repository-relative paths to file text. The ``python`` scaffold
installs nothing beyond the standard library: a package with ``--version``, one smoke test,
CI running the profile's lint and test commands, README, CHANGELOG, LICENSE, ``.gitignore``
from the profile's cache excludes, and the product memory files.
"""

from __future__ import annotations

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


def python_scaffold(spec: ProductSpec, *, owner: str = "the author") -> dict[str, str]:
    tc: Toolchain = get_toolchain("python")
    pkg = spec.name
    test_cmd = " ".join(tc.test_argv(filter=None, path=None))
    lint_cmd = " ".join(tc.lint_argv("."))
    gitignore = "".join(f"{p}\n" if "*" in p else f"{p}/\n" for p in tc.cache_excludes)
    return {
        "pyproject.toml": (
            "[project]\n"
            f'name = "{pkg}"\n'
            'version = "0.1.0"\n'
            f'description = "{spec.title}"\n'
            'requires-python = ">=3.12"\n'
            "dependencies = [" + ", ".join(f'"{d}"' for d in spec.dependencies) + "]\n\n"
            '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F", "I"]\n'
        ),
        f"{pkg}/__init__.py": f'"""{spec.title}."""\n\n__version__ = "0.1.0"\n',
        f"{pkg}/__main__.py": (f"from {pkg}.cli import main\n\nraise SystemExit(main())\n"),
        f"{pkg}/cli.py": (
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
        ),
        "tests/conftest.py": (
            "import pathlib\nimport sys\n\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        ),
        "tests/test_smoke.py": (
            f"from {pkg} import __version__\n"
            f"from {pkg}.cli import main\n\n\n"
            "def test_version_flag_prints_the_version(capsys):\n"
            '    assert main(["--version"]) == 0\n'
            "    assert capsys.readouterr().out.strip() == __version__\n\n\n"
            "def test_no_arguments_prints_usage():\n"
            "    assert main([]) == 2\n"
        ),
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
            f"      - run: python -m {pkg} --version\n"
        ),
        "README.md": (
            f"# {spec.title}\n\n{spec.vision}\n\n"
            "## Run\n\n```\n"
            f"python -m {pkg} --version\n"
            "```\n\n## Develop\n\n```\n"
            "pip install ruff pytest\n"
            f"{lint_cmd}\n{test_cmd}\n"
            "```\n\n"
            "Product specification: [PRODUCT.md](PRODUCT.md). "
            "Progress: `.scio/product/progress.json`.\n"
        ),
        "CHANGELOG.md": (
            "# Changelog\n\n## [Unreleased]\n### Added\n"
            "- Walking skeleton: package, `--version`, smoke test, CI (SCIO bootstrap).\n"
        ),
        "LICENSE": MIT.format(year=datetime.now(UTC).year, owner=owner),
        ".gitignore": gitignore + ".scio/sandbox/\n.scio/ledger/\n.scio/artifacts/\n",
        "NOTES.md": (
            "# Notes\n\nWorking notes SCIO and humans leave for the next session. "
            "Newest first.\n\n"
            f"- Bootstrapped by `scio new` ({datetime.now(UTC).date().isoformat()}): "
            "walking skeleton, CI, smoke test.\n"
        ),
    }


def scaffold_for(spec: ProductSpec, *, owner: str = "the author") -> dict[str, str]:
    if spec.stack == "python":
        return python_scaffold(spec, owner=owner)
    raise KeyError(f"no scaffold for stack {spec.stack!r}")
