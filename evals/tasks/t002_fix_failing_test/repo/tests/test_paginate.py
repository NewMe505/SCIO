from pager import paginate


def test_full_pages():
    assert paginate([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_last_partial_page():
    assert len(paginate(list(range(7)), 3)) == 3
