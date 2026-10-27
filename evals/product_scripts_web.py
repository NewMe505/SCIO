"""Scripted feature work for the Fake ``todo_web`` product (M11, p003): a todo list in the
browser, served by ``http.server`` with the standard library only. One HTML page carries the
list and an add form; the form posts to ``/todos`` and the server redirects back to ``/``.

The unit tests drive the ``Handler`` in-process (the coverage floor counts in-process code,
I-091); the acceptance tests talk to the started server; p003's hidden tests drive it in a
real browser. Feature titles differ from ``todo_api``'s, because scripts are matched by title.
"""

from __future__ import annotations

from evals.fake_scripts import Edit, Step, TaskScript
from evals.product_scripts_http import scaffold_http_cli

PKG = "todo_web"

STORE = '''"""JSON-file store for todos (standard library only)."""

import json
import os
from pathlib import Path

DEFAULT_PATH = Path(".scio") / "sandbox" / "todos.json"


def store_path():
    return Path(os.environ.get("TODO_FILE", str(DEFAULT_PATH)))


def load():
    path = store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def add(title):
    items = load()
    item = {"id": len(items) + 1, "title": title}
    items.append(item)
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return item
'''


def _cli(post: str, extra_import: str) -> str:
    return f'''"""Command line entry point: the todo web app."""

import argparse
import html
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
{extra_import}
from {PKG} import __version__, store

HEALTH_PATH = "/health"


def render(items, error=""):
    rows = "".join(f"<li>{{html.escape(item['title'])}}</li>" for item in items)
    note = f'<p class="error">{{html.escape(error)}}</p>' if error else ""
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Todos</title></head>'
        f"<body><h1>Todos</h1>{{note}}"
        '<form method="post" action="/todos">'
        '<input name="title" aria-label="title"> <button type="submit">Add</button></form>'
        f'<ul id="todos">{{rows}}</ul></body></html>'
    )


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == HEALTH_PATH:
            payload = json.dumps({{"status": "ok", "version": __version__}})
            self._send(200, payload, "application/json")
            return
        if self.path == "/":
            self._send(200, render(store.load()))
            return
        self._send(404, "not found", "text/plain")
{post}
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


POST = """
    def do_POST(self):
        # read the body first: a reply that leaves request bytes unread aborts the
        # connection on Windows (WinError 10053) instead of delivering the status (I-096)
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        if self.path != "/todos":
            self._send(404, "not found", "text/plain")
            return
        title = form.get("title", [""])[0].strip()
        if not title:
            self._send(400, render(store.load(), "title is required"))
            return
        store.add(title)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()
"""

CLI_P1 = _cli("", "")
CLI_P2 = _cli(POST, "from urllib.parse import parse_qs\n")

FIXTURE = """
@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todos.json"))
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
"""

PAGE_TESTS = (
    f"""import threading
import urllib.request
from http.server import HTTPServer

import pytest

from {PKG} import store
from {PKG}.cli import Handler

"""
    + FIXTURE
    + """

def test_the_page_lists_todos_with_a_form(base):
    store.add("buy milk")
    with urllib.request.urlopen(base + "/", timeout=5) as res:
        body = res.read().decode()
    assert "<h1>Todos</h1>" in body and "<li>buy milk</li>" in body
    assert 'name="title"' in body and "<button" in body
"""
)

ADD_TESTS = (
    f"""import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer

import pytest

from {PKG} import store
from {PKG}.cli import Handler


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

"""
    + FIXTURE
    + """

def _post(url, title):
    data = urllib.parse.urlencode({"title": title}).encode()
    try:
        with urllib.request.build_opener(NoRedirect).open(url, data=data, timeout=5) as res:
            return res.status, res.headers.get("Location"), res.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location"), exc.read().decode()


def test_add_from_the_form_redirects_to_the_list(base):
    assert _post(base + "/todos", "buy milk")[:2] == (303, "/")
    assert [item["title"] for item in store.load()] == ["buy milk"]


def test_add_with_an_empty_title_shows_an_error_and_adds_nothing(base):
    status, _, body = _post(base + "/todos", "   ")
    assert status == 400 and "title is required" in body
    assert store.load() == []
    assert _post(base + "/elsewhere", "x")[0] == 404
"""
)

PAGE_ACCEPTANCE = """import urllib.request


def test_the_page_is_served(app_url):
    with urllib.request.urlopen(app_url + "/", timeout=5) as res:
        assert res.status == 200
        assert "<h1>Todos</h1>" in res.read().decode()
"""

ADD_ACCEPTANCE = """import urllib.parse
import urllib.request


def test_a_todo_added_through_the_form_is_on_the_page(app_url):
    data = urllib.parse.urlencode({"title": "write report"}).encode()
    with urllib.request.urlopen(app_url + "/todos", data=data, timeout=5) as res:
        assert res.status == 200  # the 303 is followed to the list
        assert "<li>write report</li>" in res.read().decode()
"""


def _changelog(line: str) -> Edit:
    return Edit("CHANGELOG.md", "### Added\n", f"### Added\n- {line}\n")


WEB_PRODUCT_SCRIPTS: list[TaskScript] = [
    TaskScript(
        key="todo page lists todos",
        tier="T1",
        features=[("F1", "GET / shows the todo list and the add form", "page")],
        steps=[
            Step(
                "S1",
                "an HTML page with the list and the form, over a JSON-file store",
                ["F1"],
                [f"{PKG}/store.py", f"{PKG}/cli.py", "tests/test_page.py"],
                edits=[
                    Edit(f"{PKG}/store.py", "", STORE),
                    Edit(f"{PKG}/cli.py", scaffold_http_cli(PKG), CLI_P1),
                    Edit("tests/test_page.py", "", PAGE_TESTS),
                    Edit("tests/acceptance/test_page_http.py", "", PAGE_ACCEPTANCE),
                ],
                test_filter="page",
            )
        ],
        docs=[_changelog("The todo page: `GET /` lists the todos with an add form (P1).")],
    ),
    TaskScript(
        key="add todos from the page",
        tier="T1",
        features=[("F1", "the form adds a todo and an empty title is refused", "add")],
        steps=[
            Step(
                "S1",
                "POST /todos from the form, redirect to the list, refuse an empty title",
                ["F1"],
                [f"{PKG}/cli.py", "tests/test_add_page.py"],
                edits=[
                    Edit(f"{PKG}/cli.py", CLI_P1, CLI_P2),
                    Edit("tests/test_add_page.py", "", ADD_TESTS),
                    Edit("tests/acceptance/test_add_http.py", "", ADD_ACCEPTANCE),
                ],
                test_filter="add",
            )
        ],
        docs=[_changelog("Adding todos from the page; an empty title is refused (P2).")],
    ),
]
