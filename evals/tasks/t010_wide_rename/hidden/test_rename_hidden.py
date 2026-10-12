from pathlib import Path

import pytest
from shop.cart import cart_total_line
from shop.checkout import checkout_summary
from shop.discount import discount_line
from shop.money import format_money
from shop.receipt import receipt_footer
from shop.refund import refund_message

ROOT = Path(__file__).resolve().parent.parent


def test_hidden_format_money_currencies():
    assert format_money(1234) == "$12.34"
    assert format_money(1234, "EUR") == "EUR 12.34"
    assert format_money(-250, "GBP") == "-GBP 2.50"
    with pytest.raises(ValueError):
        format_money(1, "XXX")


def test_hidden_old_name_is_gone_everywhere():
    for path in (ROOT / "shop").glob("*.py"):
        assert "format_cents" not in path.read_text(encoding="utf-8"), path.name


def test_hidden_callers_still_work_in_dollars():
    assert cart_total_line([100, 250]) == "total $3.50"
    assert checkout_summary(999) == "pay $9.99"
    assert receipt_footer(5) == "paid $0.05, thank you"
    assert refund_message(250) == "refunded $2.50"
    assert discount_line(1000, 10) == "discount $1.00"
