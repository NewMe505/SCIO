import argparse

from tool.greet import greeting


def main(argv=None):
    parser = argparse.ArgumentParser(prog='greet')
    parser.add_argument('name')
    args = parser.parse_args(argv)
    print(greeting(args.name))
    return 0
