from config_parser import parse, parse_line


def test_value_with_equals():
    assert parse_line("url = http://x?a=1") == ("url", "http://x?a=1")


def test_comments_and_blank():
    assert parse_line("# comment") is None
    assert parse_line("   ") is None


def test_parse():
    text = "a = 1\n# note\n\nb=x=y\n"
    assert parse(text) == {"a": "1", "b": "x=y"}
