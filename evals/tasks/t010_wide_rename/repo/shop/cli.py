"""Command line."""

from shop.money import format_cents


def main(argv):
    return print(format_cents(int(argv[0]))) or 0
