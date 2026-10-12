import argparse

from textfmt.table import render_table

DEMO_HEADERS = ["item", "qty", "price"]
DEMO_ROWS = [["widget", 3, 2.5], ["gear", 12, 10.0]]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="textfmt")
    parser.add_argument("--demo", action="store_true", help="print the demo table")
    args = parser.parse_args(argv)
    if args.demo:
        print(render_table(DEMO_ROWS, headers=DEMO_HEADERS))
        return 0
    parser.print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
