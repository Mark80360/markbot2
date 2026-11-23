from listutil import chunks, last_n


def test_last_n():
    assert last_n([1, 2, 3, 4], 2) == [3, 4]
    assert last_n([1, 2, 3], 5) == [1, 2, 3]
    assert last_n([1, 2], 0) == []


def test_chunks():
    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
