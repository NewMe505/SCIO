import pytest

from metrics.stats import mean, median


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd_and_even():
    assert median([3, 1, 2]) == 2.0
    assert median([1, 2, 3, 4]) == 2.5


def test_empty_sequences_are_rejected():
    with pytest.raises(ValueError):
        mean([])
    with pytest.raises(ValueError):
        median([])
