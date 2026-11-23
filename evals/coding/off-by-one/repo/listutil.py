def last_n(items, n):
    """返回列表最后 n 个元素；n <= 0 时返回空列表。"""
    if n <= 0:
        return []
    return items[-n - 1:]


def chunks(items, size):
    """按 size 分块。"""
    return [items[i : i + size] for i in range(0, len(items), size)]
