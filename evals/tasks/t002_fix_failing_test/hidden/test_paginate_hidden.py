from pager import paginate


def test_hidden_partial():
    assert paginate(list(range(7)), 3) == [[0, 1, 2], [3, 4, 5], [6]]


def test_hidden_exact():
    assert paginate([1, 2, 3, 4, 5, 6], 3) == [[1, 2, 3], [4, 5, 6]]


def test_hidden_empty():
    assert paginate([], 3) == []
