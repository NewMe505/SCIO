from shop.cart import cart_total_line
from shop.invoice import invoice_line
from shop.report import daily_report
from shop.tax import tax_line


def test_cart_and_invoice():
    assert cart_total_line([100, 250]) == "total $3.50"
    assert invoice_line("widget", 999) == "widget: $9.99"


def test_tax_and_report():
    assert tax_line(1000, 0.075) == "tax $0.75"
    assert daily_report({"mon": 100, "tue": 2000}) == "mon $1.00\ntue $20.00"
