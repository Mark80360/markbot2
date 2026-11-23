import inventory
from inventory import summary, total_value


def test_total_value():
    items = [{"price": 2, "qty": 3}, {"price": 1, "qty": 4}]
    assert total_value(items) == 10
    assert summary(items) == "2 items, total 10"


def test_old_name_removed():
    assert not hasattr(inventory, "get_total")
