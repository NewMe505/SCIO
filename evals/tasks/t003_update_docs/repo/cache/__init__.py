"""cache: a tiny TTL cache."""

import time


class Cache:
    def __init__(self):
        self._data = {}

    def get_or_set(self, key, factory, ttl=60.0):
        now = time.monotonic()
        hit = self._data.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
        value = factory()
        self._data[key] = (value, now + ttl)
        return value
