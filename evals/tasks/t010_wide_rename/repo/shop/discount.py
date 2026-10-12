"""Discount lines."""

from shop.money import format_cents


def discount_line(cents, pct):
    return f"discount {format_cents(round(cents * pct / 100))}"
