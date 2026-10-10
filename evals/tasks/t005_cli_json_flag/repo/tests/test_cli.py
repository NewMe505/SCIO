from tool.cli import main


def test_plain(capsys):
    assert main(['Ada']) == 0
    assert capsys.readouterr().out.strip() == 'Hello, Ada!'
