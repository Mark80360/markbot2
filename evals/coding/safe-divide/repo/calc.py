def safe_divide(a, b):
    return a / b


def average(values):
    if not values:
        return None
    return safe_divide(sum(values), len(values))
