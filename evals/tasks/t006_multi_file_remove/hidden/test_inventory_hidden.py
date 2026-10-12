import pytest
from inventory.cli import main
from inventory.store import Store


def test_hidden_remove_units_and_whole_entry():
    s = Store()
    s.add("bolt", 5)
    s.remove("bolt", 2)
    assert s.items() == {"bolt": 3}
    s.remove("bolt")
    assert s.items() == {}
    s.add("nut", 1)
    s.remove("nut", 1)
    assert "nut" not in s.items()


def test_hidden_remove_errors():
    s = Store()
    s.add("bolt", 1)
    with pytest.raises(KeyError):
        s.remove("washer")
    with pytest.raises(ValueError):
        s.remove("bolt", 2)
    with pytest.raises(ValueError):
        s.remove("bolt", 0)


def test_hidden_cli_remove(capsys):
    s = Store()
    s.add("bolt", 3)
    assert main(["remove", "bolt", "--qty", "2"], store=s) == 0
    assert main(["remove", "bolt"], store=s) == 0
    assert capsys.readouterr().out == "removed 2 bolt\nremoved bolt\n"
    assert s.items() == {}


def test_hidden_add_and_list_unchanged(capsys):
    s = Store()
    assert main(["add", "nut"], store=s) == 0
    assert main(["list"], store=s) == 0
    assert capsys.readouterr().out == "added 1 nut\nnut: 1\n"
