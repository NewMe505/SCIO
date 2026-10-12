"""Cart totals."""

from shop.money import format_cents


def cart_total_line(items):
    return f"total {format_cents(sum(items))}"
