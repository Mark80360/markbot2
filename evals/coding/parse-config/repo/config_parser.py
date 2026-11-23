def parse_line(line):
    """解析 key=value；注释（# 开头）与空行返回 None。"""
    key, value = line.split("=")
    return key.strip(), value.strip()


def parse(text):
    result = {}
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result
