from cache import Cache


def test_get_or_set_caches():
    c = Cache()
    assert c.get_or_set('k', lambda: 1) == 1
    assert c.get_or_set('k', lambda: 2) == 1
