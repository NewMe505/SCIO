"""Descriptive statistics over a sequence of numbers."""

DECIMALS = 2
"""Rounding used by the report helpers."""


def mean(values):
    if not values:
        raise ValueError("mean of an empty sequence")
    return sum(values) / len(values)


def median(values):
    if not values:
        raise ValueError("median of an empty sequence")
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return float(s[mid])
