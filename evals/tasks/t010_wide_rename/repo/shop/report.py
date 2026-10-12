"""Daily report."""

from shop.money import format_cents


def daily_report(totals):
    return "\n".join(f"{day} {format_cents(c)}" for day, c in totals.items())
