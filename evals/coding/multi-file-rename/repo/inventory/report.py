from inventory.core import get_total


def summary(items):
    return f"{len(items)} items, total {get_total(items)}"
