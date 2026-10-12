import pytest

from inventory.store import Store


def test_add_and_items():
    s = Store()
    s.add('bolt', 2)
    s.add('bolt')
    s.add('nut')
    assert s.items() == {'bolt': 3, 'nut': 1}


def test_add_rejects_non_positive():
    with pytest.raises(ValueError):
        Store().add('bolt', 0)
