"""Scripted feature work for the Fake product build (ADR 0011): three features of the
``todo_cli`` product that ``scio new`` scaffolds on the Fake provider.

P1 (T0) adds an entry-point test; P2 (T1) adds ``add``/``list`` with a JSON store; P3 (T1)
adds tags and ``export --json``, with a wrong first implementation so the escalation path
(repeated error, advisor, guided fix) runs inside a product build when the responder is in
``wrong_first`` mode. Whole-file edits because ``edit_file`` lints every edit (I-057).
"""

from __future__ import annotations

from evals.fake_scripts import Edit, Step, TaskScript


def scaffold_cli(pkg: str) -> str:
    """The ``cli.py`` the python scaffold ships, so the first edit's ``old`` text matches."""
    from scio.product.scaffold import python_scaffold
    from scio.state.models import ProductFeature, ProductSpec

    spec = ProductSpec(
        name=pkg,
        title=pkg,
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="d", acceptance=["a"])],
        quality_bar=["q"],
    )
    return python_scaffold(spec)[f"{pkg}/cli.py"]


PKG = "todo_cli"

ENTRY_TESTS = f'''import subprocess
import sys
from pathlib import Path

from {PKG} import __version__


def test_module_entry_prints_the_version():
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-m", "{PKG}", "--version"], cwd=root, capture_output=True, text=True
    )
    assert out.returncode == 0
    assert out.stdout.strip() == __version__
'''

STORE = '''"""JSON-file store for todos."""

import json
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, items):
        self.path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def add(self, text, tags=()):
        items = self.load()
        items.append({"id": len(items) + 1, "text": text, "tags": list(tags)})
        self.save(items)
        return items[-1]
'''

CLI_P2 = f'''"""Command line entry point."""

import argparse
import os

from {PKG} import __version__
from {PKG}.store import Store


def _store():
    return Store(os.environ.get("TODO_FILE", "todo.json"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="{PKG}")
    parser.add_argument("--version", action="store_true", help="print the version")
    sub = parser.add_subparsers(dest="cmd")
    add = sub.add_parser("add")
    add.add_argument("text")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.cmd == "add":
        item = _store().add(args.text)
        print(f"added {{item['id']}}: {{item['text']}}")
        return 0
    if args.cmd == "list":
        for item in _store().load():
            print(f"{{item['id']}}. {{item['text']}}")
        return 0
    parser.print_usage()
    return 2
'''

ADD_LIST_TESTS = f"""from {PKG}.cli import main


def test_add_then_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todo.json"))
    assert main(["add", "x"]) == 0
    assert main(["list"]) == 0
    assert "1. x" in capsys.readouterr().out
"""


def _cli_p3(export_line: str) -> str:
    return f'''"""Command line entry point."""

import argparse
import json
import os

from {PKG} import __version__
from {PKG}.store import Store


def _store():
    return Store(os.environ.get("TODO_FILE", "todo.json"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="{PKG}")
    parser.add_argument("--version", action="store_true", help="print the version")
    sub = parser.add_subparsers(dest="cmd")
    add = sub.add_parser("add")
    add.add_argument("text")
    add.add_argument("--tag", action="append", default=[])
    sub.add_parser("list")
    export = sub.add_parser("export")
    export.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.cmd == "add":
        item = _store().add(args.text, args.tag)
        print(f"added {{item['id']}}: {{item['text']}}")
        return 0
    if args.cmd == "list":
        for item in _store().load():
            tags = " ".join(f"#{{t}}" for t in item["tags"])
            print(f"{{item['id']}}. {{item['text']}} {{tags}}".rstrip())
        return 0
    if args.cmd == "export":
        items = _store().load()
        {export_line}
        return 0
    parser.print_usage()
    return 2
'''


CLI_P3 = _cli_p3("print(json.dumps(items))")
CLI_P3_WRONG = _cli_p3("print(items)  # not JSON: Python repr with single quotes")

EXPORT_TESTS = f"""import json

from {PKG}.cli import main


def test_export_json_with_tags(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todo.json"))
    assert main(["add", "x", "--tag", "home"]) == 0
    capsys.readouterr()
    assert main(["export", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["tags"] == ["home"]
"""


def _changelog(line: str) -> Edit:
    return Edit("CHANGELOG.md", "### Added\n", f"### Added\n- {line}\n")


PRODUCT_SCRIPTS: list[TaskScript] = [
    TaskScript(
        key="walking skeleton",
        tier="T0",
        features=[
            ("F1", "python -m todo_cli --version prints the version", "entry"),
            ("F2", "the smoke test passes", "entry"),
        ],
        steps=[
            Step(
                "S1",
                "prove the module entry point with a subprocess test",
                ["F1", "F2"],
                ["tests/test_entry.py"],
                edits=[Edit("tests/test_entry.py", "", ENTRY_TESTS)],
                test_filter="entry",
            )
        ],
        docs=[_changelog("Entry-point test: `python -m todo_cli --version` (P1).")],
    ),
    TaskScript(
        key="add and list",
        tier="T1",
        features=[("F1", "todo add then todo list prints the item", "add_list")],
        steps=[
            Step(
                "S1",
                "add and list subcommands over a JSON store",
                ["F1"],
                [f"{PKG}/cli.py", f"{PKG}/store.py", "tests/test_add_list.py"],
                edits=[
                    Edit(f"{PKG}/store.py", "", STORE),
                    Edit(f"{PKG}/cli.py", scaffold_cli(PKG), CLI_P2),
                    Edit("tests/test_add_list.py", "", ADD_LIST_TESTS),
                ],
                test_filter="add_list",
            )
        ],
        docs=[_changelog("`add` and `list` subcommands with a JSON store (P2).")],
    ),
    TaskScript(
        key="tags and json export",
        tier="T1",
        features=[("F1", "export --json prints valid JSON with tags", "export_json")],
        steps=[
            Step(
                "S1",
                "tags on add, export --json",
                ["F1"],
                [f"{PKG}/cli.py", "tests/test_export.py"],
                edits=[
                    Edit(f"{PKG}/cli.py", CLI_P2, CLI_P3),
                    Edit("tests/test_export.py", "", EXPORT_TESTS),
                ],
                test_filter="export_json",
                wrong_edits=[
                    Edit(f"{PKG}/cli.py", CLI_P2, CLI_P3_WRONG),
                    Edit("tests/test_export.py", "", EXPORT_TESTS),
                ],
                fix_edit=Edit(f"{PKG}/cli.py", CLI_P3_WRONG, CLI_P3),
            )
        ],
        docs=[_changelog("Tags and `export --json` (P3).")],
    ),
]
