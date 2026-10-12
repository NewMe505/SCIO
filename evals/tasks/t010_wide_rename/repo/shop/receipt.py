"""Receipts."""

from shop.money import format_cents


def receipt_footer(cents):
    return f"paid {format_cents(cents)}, thank you"
