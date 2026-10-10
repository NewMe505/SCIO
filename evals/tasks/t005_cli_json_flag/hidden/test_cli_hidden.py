import json

from tool.cli import main


def test_hidden_json(capsys):
    assert main(["--json", "Ada"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"name": "Ada", "greeting": "Hello, Ada!"}


def test_hidden_plain_unchanged(capsys):
    main(["Bob"])
    assert capsys.readouterr().out.strip() == "Hello, Bob!"
