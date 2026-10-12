"""Tax lines."""

from shop.money import format_cents


def tax_line(cents, rate):
    return f"tax {format_cents(round(cents * rate))}"
