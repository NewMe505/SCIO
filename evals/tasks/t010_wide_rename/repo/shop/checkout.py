"""Checkout messages."""

from shop.money import format_cents


def checkout_summary(cents):
    return f"pay {format_cents(cents)}"
