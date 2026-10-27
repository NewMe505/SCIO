"""Hidden black-box tests for p002 (M11): the released REST API over HTTP.

The grader starts the product from its ``## Runtime`` section and sets ``APP_URL``. The
store may already hold todos from the build's own acceptance run, so nothing here assumes
an empty list or a particular id."""

import json
import os
import urllib.error
import urllib.request

URL = os.environ["APP_URL"]


def _call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        URL + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read()
            return res.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_health_reports_ok_and_the_release_version():
    assert _call("GET", "/health") == (200, {"status": "ok", "version": "0.1.0"})


def test_unknown_paths_are_404():
    assert _call("GET", "/nowhere")[0] == 404


def test_created_todo_is_returned_and_listed():
    status, todo = _call("POST", "/todos", {"title": "Buy milk"})
    assert status == 201
    assert isinstance(todo["id"], int) and todo["title"] == "Buy milk" and todo["done"] is False
    status, items = _call("GET", "/todos")
    assert status == 200 and isinstance(items, list)
    assert todo in items


def test_ids_are_unique_across_creates():
    a = _call("POST", "/todos", {"title": "first"})[1]
    b = _call("POST", "/todos", {"title": "second"})[1]
    assert a["id"] != b["id"]


def test_an_empty_title_is_rejected():
    assert _call("POST", "/todos", {"title": "   "})[0] == 400
