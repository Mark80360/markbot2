from calc import average, safe_divide


def test_safe_divide():
    assert safe_divide(6, 3) == 2
    assert safe_divide(1, 0) is None


def test_average():
    assert average([1, 2, 3]) == 2
    assert average([]) is None
