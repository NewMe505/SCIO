"""Scripted feature work for the Fake ``todo_api`` product (M9, ADR 0013): an HTTP server
with an approved third-party dependency, so the install phase, ``run_app`` semantics and the
black-box acceptance step run through the whole milestone loop offline.

The dependency is ``pyyaml``, which SCIO's own environment already carries: a local product
environment sees it through the ``.pth`` link to that environment, so the install phase is
satisfied without a download. The server stores its todos under ``.scio/sandbox/``
(git-ignored) so a black-box run leaves the tree clean.
"""

from __future__ import annotations

from evals.fake_scripts import Edit, Step, TaskScript

PKG = "todo_api"
DEPENDENCY = "pyyaml"


def scaffold_http_cli(pkg: str) -> str:
    """The ``cli.py`` the python scaffold ships for an http product (the first edit's old text)."""
    from scio.product.scaffold import python_scaffold
    from scio.state.models import AppSpec, ProductFeature, ProductSpec

    spec = ProductSpec(
        name=pkg,
        title=pkg,
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="d", acceptance=["a"])],
        quality_bar=["q"],
        app=AppSpec(kind="http", args=["serve", "--port", "{port}"]),
    )
    return python_scaffold(spec)[f"{pkg}/cli.py"]


HEALTH_UNIT_TESTS = f"""import json
import threading
import urllib.request
from http.server import HTTPServer

from {PKG} import __version__
from {PKG}.cli import Handler


def test_health_endpoint_reports_the_version():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{{port}}/health", timeout=5) as resp:
            body = json.loads(resp.read())
        assert body == {{"status": "ok", "version": __version__}}
    finally:
        server.shutdown()
        server.server_close()
"""

STORE = '''"""YAML-file store for todos (pyyaml is on the approved dependency list)."""

import os
from pathlib import Path

import yaml

DEFAULT_PATH = Path(".scio") / "sandbox" / "todos.yaml"


def store_path():
    return Path(os.environ.get("TODO_FILE", str(DEFAULT_PATH)))


def load():
    path = store_path()
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return list(data)


def save(items):
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(items, sort_keys=True), encoding="utf-8")


def add(title):
    items = load()
    item = {"id": len(items) + 1, "title": title, "done": False}
    items.append(item)
    save(items)
    return item
'''

CLI_P2 = f'''"""Command line entry point: an HTTP todo server."""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from {PKG} import __version__, store

HEALTH_PATH = "/health"
TODOS_PATH = "/todos"


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == HEALTH_PATH:
            self._send_json(200, {{"status": "ok", "version": __version__}})
            return
        if self.path == TODOS_PATH:
            self._send_json(200, store.load())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != TODOS_PATH:
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{{}}")
        title = str(payload.get("title", "")).strip()
        if not title:
            self._send_json(400, {{"error": "title must not be empty"}})
            return
        self._send_json(201, store.add(title))

    def log_message(self, format, *args):
        return


def serve(port):  # pragma: no cover  (process lifecycle: the acceptance tests run it)
    host = os.environ.get("HOST", "127.0.0.1")
    server = HTTPServer((host, port), Handler)
    print(f"serving on http://{{host}}:{{port}}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="{PKG}")
    parser.add_argument("--version", action="store_true", help="print the version")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("serve", help="start the HTTP server")
    run.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.cmd == "serve":  # pragma: no cover
        return serve(args.port)
    parser.print_usage()
    return 2
'''

TODOS_UNIT_TESTS = f"""import json
import threading
import urllib.request
from http.server import HTTPServer

from {PKG}.cli import Handler


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={{"Content-Type": "application/json"}}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_create_then_list_todos(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todos.yaml"))
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{{server.server_address[1]}}"
        status, created = _post(base + "/todos", {{"title": "Buy milk"}})
        assert status == 201 and created["id"] == 1 and created["done"] is False
        with urllib.request.urlopen(base + "/todos", timeout=5) as resp:
            items = json.loads(resp.read())
        assert items == [created]
        assert (tmp_path / "todos.yaml").exists()
    finally:
        server.shutdown()
        server.server_close()
"""

TODOS_ERROR_TESTS = f"""import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from {PKG}.cli import Handler


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todos.yaml"))
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{{server.server_address[1]}}"
    server.shutdown()
    server.server_close()


def _status(req):
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_todos_reject_an_empty_title_and_unknown_paths(base):
    empty = json.dumps({{"title": " "}}).encode()
    assert _status(urllib.request.Request(base + "/todos", data=empty)) == 400
    assert _status(urllib.request.Request(base + "/other", data=b"{{}}")) == 404
"""
"""The tests the scripted debugger adds when verify reports the coverage floor (M11, I-091):
P2's own tests leave the two error paths of ``do_POST`` uncovered (93% of the product)."""

TODOS_ACCEPTANCE = """import json
import urllib.request


def test_create_and_list_over_http(app_url):
    req = urllib.request.Request(
        app_url + "/todos",
        data=json.dumps({"title": "Write report"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 201
        created = json.loads(resp.read())
    with urllib.request.urlopen(app_url + "/todos", timeout=5) as resp:
        items = json.loads(resp.read())
    assert any(item["id"] == created["id"] and item["title"] == "Write report" for item in items)
"""


def _changelog(line: str) -> Edit:
    return Edit("CHANGELOG.md", "### Added\n", f"### Added\n- {line}\n")


HTTP_PRODUCT_SCRIPTS: list[TaskScript] = [
    TaskScript(
        key="health and version endpoint",
        tier="T0",
        features=[("F1", "GET /health returns status ok and the version", "health")],
        steps=[
            Step(
                "S1",
                "prove the health endpoint with a unit test against a live handler",
                ["F1"],
                ["tests/test_health.py"],
                edits=[Edit("tests/test_health.py", "", HEALTH_UNIT_TESTS)],
                test_filter="health",
            )
        ],
        docs=[_changelog("Health endpoint test (P1).")],
    ),
    TaskScript(
        key="create and list todos",
        tier="T1",
        features=[("F1", "POST /todos then GET /todos returns the todo as JSON", "todos")],
        steps=[
            Step(
                "S1",
                "todos resource over HTTP with a YAML store",
                ["F1"],
                [f"{PKG}/store.py", f"{PKG}/cli.py", "tests/test_todos.py"],
                edits=[
                    Edit(f"{PKG}/store.py", "", STORE),
                    Edit(f"{PKG}/cli.py", scaffold_http_cli(PKG), CLI_P2),
                    Edit("tests/test_todos.py", "", TODOS_UNIT_TESTS),
                    Edit("tests/acceptance/test_todos_http.py", "", TODOS_ACCEPTANCE),
                ],
                test_filter="todos",
                debug_edits=[Edit("tests/test_todos_errors.py", "", TODOS_ERROR_TESTS)],
            )
        ],
        docs=[_changelog("`POST /todos` and `GET /todos` with a YAML store (P2).")],
    ),
]
