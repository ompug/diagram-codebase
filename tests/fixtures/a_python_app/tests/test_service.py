from app.forecast import reorder_quantity
from app.models import Item


def test_reorder():
    assert reorder_quantity(Item(1, "x", 0)) == 19
