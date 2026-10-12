"""Refund notices."""

from shop.money import format_cents


def refund_message(cents):
    return f"refunded {format_cents(cents)}"
