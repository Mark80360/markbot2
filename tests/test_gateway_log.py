from __future__ import annotations

import logging
import re
import sys
import threading

from markbot.kernel.gateway_log import gateway_dir, gateway_log_path, install, write


def test_gateway_paths_use_daily_file(tmp_path):
    home = tmp_path / "home"
    assert gateway_dir(home) == home / "gateway"
    path = gateway_log_path(home)
    assert path.parent == home / "gateway"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.log", path.name)


def test_write_appends_timestamped_and_redacted_lines(tmp_path):
    home = tmp_path / "home"
    write(home, "first token=abcdefgh1234")
    write(home, "second")

    text = gateway_log_path(home).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 2
    assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", lines[0])
    assert "abcdefgh1234" not in text
    assert "[REDACTED]" in text
    assert lines[1].endswith("second")


def test_write_swallows_filesystem_errors(tmp_path):
    broken_home = tmp_path / "not-a-dir"
    broken_home.write_text("x", encoding="utf-8")
    write(broken_home, "should not raise")


def test_install_is_idempotent_and_captures_logging(tmp_path):
    home = tmp_path / "home"
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_excepthook = sys.excepthook
    original_thread_hook = getattr(threading, "excepthook", None)
    try:
        path = install(home)
        assert path.parent.exists()
        handlers = [h for h in root.handlers if type(h).__name__ == "_GatewayFileHandler"]
        assert len(handlers) == 1

        def thread_target():
            raise RuntimeError("thread-boom")

        thread = threading.Thread(target=thread_target, name="log-test")
        thread.start()
        thread.join()

        logging.getLogger("markbot.test").warning("gateway token=abcdefgh1234")
        text = gateway_log_path(home).read_text(encoding="utf-8")
        assert "线程 log-test 未捕获异常" in text
        assert "thread-boom" in text
        assert "WARNING markbot.test: gateway" in text
        assert "abcdefgh1234" not in text
        assert "[REDACTED]" in text

        again = install(home)
        assert again == path
        handlers = [h for h in root.handlers if type(h).__name__ == "_GatewayFileHandler"]
        assert len(handlers) == 1
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
        sys.excepthook = original_excepthook
        if original_thread_hook is not None:
            threading.excepthook = original_thread_hook
