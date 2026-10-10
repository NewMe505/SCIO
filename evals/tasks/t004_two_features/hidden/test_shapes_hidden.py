import math

import pytest
from shapes import area_circle, area_rect


def test_hidden_circle():
    assert area_circle(2) == pytest.approx(math.pi * 4)
    with pytest.raises(ValueError):
        area_circle(-1)


def test_hidden_rect():
    assert area_rect(2, 3) == 6
    with pytest.raises(ValueError):
        area_rect(-1, 2)
