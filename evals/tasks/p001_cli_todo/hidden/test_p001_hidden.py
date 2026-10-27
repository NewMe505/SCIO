"""Hidden black-box tests for p001 (M11): the released CLI, run from outside as a user runs it.

The grader sets ``APP_CMD`` (the start command as a JSON list, the interpreter already the
product's) and ``APP_DIR`` (the product checkout). Nothing here imports the product."""

import json
import os
import subprocess

import pytest

CMD = json.loads(os.environ["APP_CMD"])
DIR = os.environ["APP_DIR"]


@pytest.fixture
def todo(tmp_path):
    env = {**os.environ, "TODO_FILE": str(tmp_path / "todos.json")}

    def run(*args):
        return subprocess.run(
            [*CMD, *args], cwd=DIR, env=env, capture_output=True, text=True, timeout=60
        )

    return run


def test_version_is_the_release(todo):
    out = todo("--version")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0.1.0"


def test_add_prints_the_id_and_list_shows_todos_oldest_first(todo):
    first = todo("add", "buy milk")
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "added 1: buy milk"
    assert todo("add", "call mum").stdout.strip() == "added 2: call mum"
    listed = todo("list")
    assert listed.returncode == 0
    assert listed.stdout.strip().splitlines() == ["1. buy milk", "2. call mum"]


def test_todos_survive_between_runs_in_the_todo_file(todo, tmp_path):
    todo("add", "water plants")
    assert (tmp_path / "todos.json").is_file()
    assert "1. water plants" in todo("list").stdout


def test_tags_show_in_the_list(todo):
    assert todo("add", "pay rent", "--tag", "home", "--tag", "urgent").returncode == 0
    assert todo("list").stdout.strip() == "1. pay rent #home #urgent"


def test_export_json_carries_ids_texts_and_tags(todo):
    todo("add", "buy milk")
    todo("add", "pay rent", "--tag", "home")
    out = todo("export", "--json")
    assert out.returncode == 0, out.stderr
    items = json.loads(out.stdout)
    assert [(i["id"], i["text"], i["tags"]) for i in items] == [
        (1, "buy milk", []),
        (2, "pay rent", ["home"]),
    ]
