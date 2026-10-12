import argparse

from inventory.store import Store

STORE = Store()


def main(argv=None, store=None):
    store = store if store is not None else STORE
    parser = argparse.ArgumentParser(prog='inventory')
    sub = parser.add_subparsers(dest='cmd', required=True)
    add = sub.add_parser('add')
    add.add_argument('name')
    add.add_argument('--qty', type=int, default=1)
    sub.add_parser('list')
    args = parser.parse_args(argv)
    if args.cmd == 'add':
        store.add(args.name, args.qty)
        print(f'added {args.qty} {args.name}')
    elif args.cmd == 'list':
        for name, qty in store.items().items():
            print(f'{name}: {qty}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
