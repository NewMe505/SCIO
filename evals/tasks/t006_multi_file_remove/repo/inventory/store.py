class Store:
    """In-memory stock: item name -> quantity."""

    def __init__(self):
        self._items = {}

    def add(self, name, qty=1):
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._items[name] = self._items.get(name, 0) + qty

    def items(self):
        return dict(sorted(self._items.items()))
