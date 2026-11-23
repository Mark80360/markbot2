from textutil import slugify


def test_slugify():
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Already--Dashed__Text!! ") == "already-dashed-text"
    assert slugify("Python 3.12 Release") == "python-3-12-release"
    assert slugify("---") == ""
