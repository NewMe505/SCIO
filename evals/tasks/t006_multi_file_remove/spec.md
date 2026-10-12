# Add remove to the inventory store and CLI

Stock can be added and listed but never removed. Add removal end to end: the
store method, the command line subcommand, and the docs. The store and the CLI
live in different modules and both have their own tests.

Acceptance criteria:
- [ ] `Store.remove(name, qty=None)` removes `qty` units, or the whole entry when `qty` is None; an item whose quantity reaches zero disappears from `items()`
- [ ] `Store.remove` raises `KeyError` for an unknown item and `ValueError` when `qty` is not positive or exceeds the stock
- [ ] `inventory.cli.main(['remove', 'bolt'], store=s)` removes the whole entry and prints `removed bolt`; `main(['remove', 'bolt', '--qty', '2'], store=s)` removes two units and prints `removed 2 bolt`
- [ ] Existing `add` and `list` behaviour is unchanged
