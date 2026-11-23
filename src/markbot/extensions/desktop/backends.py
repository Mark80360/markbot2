"""桌面控制的平台实现（ADR 0015）：截图、鼠标、键盘、窗口、Windows 控件树。

优先使用可选依赖（``pip install -e ".[desktop]"``）：mss（截图）、Pillow（缩放）、
pyautogui（鼠标键盘）、pywinauto（Windows 控件树）。没有安装时退回：

- Windows：ctypes 调用 user32（SendInput / SetCursorPos / EnumWindows），PowerShell 截图；
- Linux（X11）：xdotool / wmctrl，截图用 scrot / gnome-screenshot / ImageMagick import / grim；
- macOS：screencapture 截图（鼠标键盘需 pyautogui）。

所有函数是同步的，由调用方放进线程执行。坐标一律是**屏幕物理像素**（Windows 下已声明 DPI 感知）。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


class DesktopError(RuntimeError):
    pass


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def has_display() -> bool:
    if IS_WINDOWS or IS_MAC:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run(argv: list[str], timeout: float = 15) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise DesktopError(f"找不到命令 {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DesktopError(f"{argv[0]} 超时") from exc
    if proc.returncode != 0:
        raise DesktopError(f"{argv[0]} 失败: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


# ------------------------------------------------------------------ Windows ctypes
_DPI_SET = False


def _win() -> Any:
    import ctypes

    global _DPI_SET
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    if not _DPI_SET:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 旧系统没有 shcore
            try:
                user32.SetProcessDPIAware()
            except Exception:  # noqa: BLE001
                pass
        _DPI_SET = True
    return user32


VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "cmd": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "capslock": 0x14,
    "printscreen": 0x2C, "menu": 0x5D,
}
VK.update({f"f{index}": 0x6F + index for index in range(1, 13)})
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}


def _vk(name: str) -> int:
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1 and (key.isalnum()):
        return ord(key.upper())
    raise DesktopError(f"不认识的按键: {name}")


def _win_send_unicode(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    user32 = _win()
    keyeventf_unicode, keyeventf_keyup = 0x0004, 0x0002
    for char in text:
        if char == "\n":
            press_keys(["enter"])
            continue
        units = char.encode("utf-16-le")
        for index in range(0, len(units), 2):
            code = int.from_bytes(units[index:index + 2], "little")
            events = (INPUT * 2)()
            for slot, flags in enumerate((keyeventf_unicode, keyeventf_unicode | keyeventf_keyup)):
                events[slot].type = 1  # INPUT_KEYBOARD
                events[slot].u.ki = KEYBDINPUT(0, code, flags, 0, 0)
            user32.SendInput(2, events, ctypes.sizeof(INPUT))
        time.sleep(0.004)


# ------------------------------------------------------------------ 截图
@dataclass
class Shot:
    path: str
    width: int          # 保存的图片尺寸
    height: int
    screen_width: int   # 实际屏幕尺寸
    screen_height: int
    left: int = 0       # 屏幕原点偏移（多显示器）
    top: int = 0

    @property
    def scale(self) -> float:
        return self.screen_width / self.width if self.width else 1.0


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:32]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _capture(target: Path) -> tuple[int, int, int, int]:
    """把主显示器截到 ``target``（PNG），返回 (left, top, width, height)。"""
    if _has("mss"):
        import mss
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            image = sct.grab(monitor)
            mss.tools.to_png(image.rgb, image.size, output=str(target))
            return monitor["left"], monitor["top"], image.size[0], image.size[1]
    if _has("PIL") and (IS_WINDOWS or IS_MAC):
        if IS_WINDOWS:
            _win()
        from PIL import ImageGrab

        image = ImageGrab.grab()
        image.save(target, "PNG")
        return 0, 0, image.width, image.height
    if IS_WINDOWS:
        _win()
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "Add-Type -Name D -Namespace W -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();';"
            "[W.D]::SetProcessDPIAware() | Out-Null;"
            "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
            f"$bmp.Save('{target}',[System.Drawing.Imaging.ImageFormat]::Png)"
        )
        _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
    elif IS_MAC:
        _run(["screencapture", "-x", "-t", "png", str(target)])
    else:
        for argv in (
            ["grim", str(target)],
            ["scrot", "-o", str(target)],
            ["gnome-screenshot", "-f", str(target)],
            ["import", "-window", "root", str(target)],
        ):
            if shutil.which(argv[0]):
                _run(argv, timeout=30)
                break
        else:
            raise DesktopError('没有可用的截图工具：pip install -e ".[desktop]"（mss），或安装 scrot / grim')
    width, height = _png_size(target)
    if not width:
        raise DesktopError("截图失败：没有生成 PNG 文件")
    return 0, 0, width, height


def screenshot(target: Path, max_width: int = 1600) -> Shot:
    target.parent.mkdir(parents=True, exist_ok=True)
    left, top, width, height = _capture(target)
    out_w, out_h = width, height
    if max_width and width > max_width and _has("PIL"):
        from PIL import Image

        with Image.open(target) as image:
            ratio = max_width / width
            out_w, out_h = max_width, max(1, int(height * ratio))
            image.convert("RGB").resize((out_w, out_h), Image.LANCZOS).save(target, "PNG", optimize=True)
    return Shot(str(target), out_w, out_h, width, height, left, top)


# ------------------------------------------------------------------ 鼠标键盘
def _pyautogui() -> Any | None:
    if not _has("pyautogui"):
        return None
    if IS_WINDOWS:
        _win()
    import pyautogui

    pyautogui.FAILSAFE = True  # 鼠标甩到屏幕角落可紧急中止
    pyautogui.PAUSE = 0.05
    return pyautogui


def cursor_position() -> tuple[int, int]:
    gui = _pyautogui()
    if gui is not None:
        point = gui.position()
        return int(point[0]), int(point[1])
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        point = wintypes.POINT()
        _win().GetCursorPos(ctypes.byref(point))
        return point.x, point.y
    if not IS_MAC and shutil.which("xdotool"):
        out = _run(["xdotool", "getmouselocation", "--shell"])
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return int(values.get("X", 0)), int(values.get("Y", 0))
    return 0, 0


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    gui = _pyautogui()
    if gui is not None:
        gui.click(x=x, y=y, button=button, clicks=clicks, interval=0.08)
        return
    if IS_WINDOWS:
        user32 = _win()
        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        for _ in range(clicks):
            user32.mouse_event(flags[0], 0, 0, 0, 0)
            user32.mouse_event(flags[1], 0, 0, 0, 0)
            time.sleep(0.08)
        return
    if not IS_MAC and shutil.which("xdotool"):
        code = {"left": "1", "middle": "2", "right": "3"}[button]
        _run(["xdotool", "mousemove", str(int(x)), str(int(y)), "click", "--repeat", str(clicks), code])
        return
    raise DesktopError('没有可用的鼠标控制：pip install -e ".[desktop]"（pyautogui），Linux 可安装 xdotool')


def move(x: int, y: int) -> None:
    gui = _pyautogui()
    if gui is not None:
        gui.moveTo(x, y, duration=0.1)
    elif IS_WINDOWS:
        _win().SetCursorPos(int(x), int(y))
    elif shutil.which("xdotool"):
        _run(["xdotool", "mousemove", str(int(x)), str(int(y))])
    else:
        raise DesktopError("没有可用的鼠标控制")


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    gui = _pyautogui()
    if gui is not None:
        gui.moveTo(x1, y1, duration=0.1)
        gui.dragTo(x2, y2, duration=0.4, button="left")
        return
    if IS_WINDOWS:
        user32 = _win()
        user32.SetCursorPos(int(x1), int(y1))
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        for step in range(1, 11):
            user32.SetCursorPos(int(x1 + (x2 - x1) * step / 10), int(y1 + (y2 - y1) * step / 10))
            time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        return
    if shutil.which("xdotool"):
        _run(["xdotool", "mousemove", str(x1), str(y1), "mousedown", "1", "mousemove", str(x2), str(y2), "mouseup", "1"])
        return
    raise DesktopError("没有可用的鼠标控制")


def scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    """amount > 0 向上，< 0 向下（单位：滚轮格）。"""
    if x is not None and y is not None:
        move(x, y)
    gui = _pyautogui()
    if gui is not None:
        gui.scroll(int(amount) * 120 if IS_WINDOWS else int(amount))
        return
    if IS_WINDOWS:
        _win().mouse_event(0x0800, 0, 0, int(amount) * 120, 0)
        return
    if shutil.which("xdotool"):
        button = "4" if amount > 0 else "5"
        _run(["xdotool", "click", "--repeat", str(abs(int(amount))), button])
        return
    raise DesktopError("没有可用的鼠标控制")


def type_text(text: str) -> None:
    if IS_WINDOWS:
        _win_send_unicode(text)  # SendInput Unicode：中文与符号都能输入，不受输入法影响
        return
    gui = _pyautogui()
    if gui is not None and text.isascii():
        gui.write(text, interval=0.01)
        return
    if not IS_MAC and shutil.which("xdotool"):
        _run(["xdotool", "type", "--delay", "8", "--", text], timeout=60)
        return
    raise DesktopError("没有可用的键盘输入：Linux 请安装 xdotool；非 ASCII 文本需要 xdotool")


def press_keys(keys: list[str]) -> None:
    """按下组合键，如 ["ctrl", "s"]；单个按键传一个元素。"""
    names = [key.strip().lower() for key in keys if key.strip()]
    if not names:
        raise DesktopError("没有指定按键")
    if IS_WINDOWS:
        user32 = _win()
        codes = [_vk(name) for name in names]
        for code in codes:
            user32.keybd_event(code, 0, 0x1 if code in _EXTENDED else 0, 0)
            time.sleep(0.02)
        for code in reversed(codes):
            user32.keybd_event(code, 0, (0x1 if code in _EXTENDED else 0) | 0x2, 0)
            time.sleep(0.02)
        return
    gui = _pyautogui()
    if gui is not None:
        gui.hotkey(*names)
        return
    if shutil.which("xdotool"):
        mapping = {"ctrl": "ctrl", "control": "ctrl", "enter": "Return", "return": "Return", "esc": "Escape",
                   "escape": "Escape", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
                   "pageup": "Prior", "pagedown": "Next", "win": "super", "cmd": "super", "up": "Up",
                   "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
                   "tab": "Tab", "space": "space"}
        combo = "+".join(mapping.get(name, name.upper() if name.startswith("f") and name[1:].isdigit() else name)
                         for name in names)
        _run(["xdotool", "key", combo])
        return
    raise DesktopError("没有可用的键盘控制")


# ------------------------------------------------------------------ 窗口
@dataclass
class Window:
    handle: str
    title: str
    process: str = ""
    pid: int = 0
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    active: bool = False


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    if _has("psutil"):
        import psutil

        try:
            return psutil.Process(pid).name()
        except Exception:  # noqa: BLE001
            return ""
    if not IS_WINDOWS:
        try:
            return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def list_windows() -> list[Window]:
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        user32 = _win()
        active = user32.GetForegroundWindow()
        found: list[Window] = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: Any, _lparam: Any) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if rect.right - rect.left < 50 or rect.bottom - rect.top < 30:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append(
                Window(
                    handle=str(int(hwnd)),
                    title=buffer.value,
                    process=_process_name(pid.value),
                    pid=pid.value,
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    active=int(hwnd) == int(active or 0),
                )
            )
            return True

        user32.EnumWindows(proto(callback), 0)
        return found
    if IS_MAC:
        raise DesktopError("macOS 暂不支持列出窗口")
    if shutil.which("wmctrl"):
        active = ""
        if shutil.which("xdotool"):
            try:
                active = str(int(_run(["xdotool", "getactivewindow"]).strip()))
            except (DesktopError, ValueError):
                active = ""
        windows: list[Window] = []
        for line in _run(["wmctrl", "-lpG"]).splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            handle, _desk, pid, x, y, w, h, _host, title = parts
            windows.append(
                Window(
                    handle=str(int(handle, 16)),
                    title=title,
                    process=_process_name(int(pid)),
                    pid=int(pid),
                    rect=(int(x), int(y), int(x) + int(w), int(y) + int(h)),
                    active=str(int(handle, 16)) == active,
                )
            )
        return windows
    raise DesktopError("列出窗口需要 wmctrl（Linux）")


def active_window() -> Window | None:
    try:
        windows = list_windows()
    except DesktopError:
        return None
    return next((window for window in windows if window.active), None)


def focus_window(handle: str) -> None:
    if IS_WINDOWS:
        user32 = _win()
        hwnd = int(handle)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 前台锁：先模拟一次 Alt，Windows 才允许把别的进程窗口切到前台
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x2, 0)
        if not user32.SetForegroundWindow(hwnd):
            raise DesktopError("系统拒绝切换前台窗口（可能被其它全屏程序占用）")
        return
    if shutil.which("wmctrl"):
        _run(["wmctrl", "-ia", hex(int(handle))])
        return
    if shutil.which("xdotool"):
        _run(["xdotool", "windowactivate", "--sync", str(int(handle))])
        return
    raise DesktopError("切换窗口需要 wmctrl 或 xdotool")


# ------------------------------------------------------------------ Windows 控件树
@dataclass
class Control:
    ref: str
    kind: str
    name: str
    rect: tuple[int, int, int, int]
    value: str = ""
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


INTERACTIVE = {
    "Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "Hyperlink", "MenuItem", "ListItem",
    "TabItem", "TreeItem", "Document", "SplitButton", "Slider", "Spinner", "DataItem", "Text",
}


def inspect_controls(handle: str, limit: int = 150, only_interactive: bool = True) -> list[Control]:
    if not IS_WINDOWS:
        raise DesktopError("控件树目前只支持 Windows；其它系统请用截图 + 坐标")
    if not _has("pywinauto"):
        raise DesktopError('读取控件树需要 pywinauto：pip install -e ".[desktop]"')
    _win()
    from pywinauto import Desktop

    window = Desktop(backend="uia").window(handle=int(handle))
    controls: list[Control] = []
    for element in window.descendants():
        try:
            info = element.element_info
            kind = str(info.control_type or "")
            if only_interactive and kind not in INTERACTIVE:
                continue
            rect = info.rectangle
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            name = str(info.name or "").strip()
            if kind == "Text" and not name:
                continue
            value = ""
            if kind in {"Edit", "ComboBox"}:
                try:
                    value = str(element.get_value() or "")[:80]
                except Exception:  # noqa: BLE001
                    value = ""
            controls.append(
                Control(
                    ref=f"c{len(controls) + 1}",
                    kind=kind,
                    name=name[:80],
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    value=value,
                    enabled=bool(info.enabled),
                )
            )
        except Exception:  # noqa: BLE001 - 个别控件读取失败不影响整体
            continue
        if len(controls) >= limit:
            break
    return controls


def launch(program: str, args: list[str]) -> int:
    if IS_WINDOWS and not args and not Path(program).suffix and shutil.which(program) is None:
        # 开始菜单里的应用名（如 notepad、calc）或协议（ms-settings:）
        os.startfile(program)  # type: ignore[attr-defined]
        return 0
    flags = 0x00000008 if IS_WINDOWS else 0  # DETACHED_PROCESS
    proc = subprocess.Popen(  # noqa: S603 - 程序由用户确认
        [program, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=not IS_WINDOWS,
    )
    return proc.pid


def capabilities() -> dict[str, str]:
    """当前环境可用的后端（用于状态说明与 doctor）。"""
    return {
        "display": "yes" if has_display() else "no",
        "screenshot": "mss" if _has("mss") else ("PIL" if _has("PIL") else ("powershell" if IS_WINDOWS else "system")),
        "input": "pyautogui" if _has("pyautogui") else ("win32" if IS_WINDOWS else ("xdotool" if shutil.which("xdotool") else "none")),
        "controls": "pywinauto" if IS_WINDOWS and _has("pywinauto") else "none",
        "scale": "PIL" if _has("PIL") else "none",
    }


__all__ = [
    "Control",
    "DesktopError",
    "Shot",
    "Window",
    "active_window",
    "capabilities",
    "click",
    "cursor_position",
    "drag",
    "focus_window",
    "has_display",
    "inspect_controls",
    "launch",
    "list_windows",
    "move",
    "press_keys",
    "screenshot",
    "scroll",
    "type_text",
]
