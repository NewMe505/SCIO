import pytest

from ledger import Account


def test_deposit():
    a = Account()
    a.deposit(10)
    assert a.balance == 10


def test_withdraw():
    a = Account(10)
    a.withdraw(4)
    assert a.balance == 6


def test_withdraw_rejects_overdraft():
    with pytest.raises(ValueError):
        Account(1).withdraw(2)
