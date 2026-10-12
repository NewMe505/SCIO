"""Invoice lines."""

from shop.money import format_cents


def invoice_line(desc, cents):
    return f"{desc}: {format_cents(cents)}"
