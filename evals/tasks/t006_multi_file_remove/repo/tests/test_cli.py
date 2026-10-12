from inventory.cli import main
from inventory.store import Store


def test_add_then_list(capsys):
    s = Store()
    assert main(['add', 'bolt', '--qty', '2'], store=s) == 0
    assert main(['list'], store=s) == 0
    assert capsys.readouterr().out == 'added 2 bolt\nbolt: 2\n'
