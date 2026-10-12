"""Money formatting shared by every module."""


def format_cents(cents):
    """Format an integer amount of cents as dollars: 1234 -> "$12.34"."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"
