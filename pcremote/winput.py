"""Thin ctypes wrapper around the Win32 SendInput API.

Injecting input at the OS level (rather than through a browser or app API)
means the remote works in every normal Windows application.
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
user32.VkKeyScanW.restype = ctypes.c_short


def _send(*inputs: INPUT) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


# ---------------------------------------------------------------- mouse

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def mouse_move(dx: int, dy: int) -> None:
    if dx or dy:
        _send(_mouse(MOUSEEVENTF_MOVE, dx, dy))


def mouse_button(button: str, action: str) -> None:
    down, up = _BUTTONS[button]
    if action == "down":
        _send(_mouse(down))
    elif action == "up":
        _send(_mouse(up))
    elif action == "click":
        _send(_mouse(down), _mouse(up))
    elif action == "dbl":
        _send(_mouse(down), _mouse(up), _mouse(down), _mouse(up))


def mouse_scroll(dx: int, dy: int) -> None:
    """dy > 0 scrolls up, dx > 0 scrolls right (in wheel units, 120 = 1 notch)."""
    events = []
    if dy:
        events.append(_mouse(MOUSEEVENTF_WHEEL, data=dy))
    if dx:
        events.append(_mouse(MOUSEEVENTF_HWHEEL, data=dx))
    _send(*events)


# ------------------------------------------------------------- keyboard

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "apps": 0x5D,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})  # F1 = 0x70

# Keys that need KEYEVENTF_EXTENDEDKEY to be distinguished from numpad keys.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
             0x5B, 0x5D, 0x2C, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}

MODIFIERS = ("ctrl", "alt", "shift", "win")


def _vk_events(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    return _key(vk=vk, flags=flags)


def resolve_key(name: str) -> tuple[int, list[str]] | None:
    """Map a key name or single character to (vk, implied modifiers)."""
    lower = name.lower()
    if lower in VK:
        return VK[lower], []
    if len(name) == 1:
        res = user32.VkKeyScanW(name)
        if res == -1:
            return None
        vk, shift_state = res & 0xFF, (res >> 8) & 0xFF
        implied = []
        if shift_state & 1:
            implied.append("shift")
        if shift_state & 2:
            implied.append("ctrl")
        if shift_state & 4:
            implied.append("alt")
        return vk, implied
    return None


def key_combo(key: str, mods: list[str] | None = None) -> bool:
    """Press and release `key` while holding `mods`. Returns False if unknown."""
    resolved = resolve_key(key)
    if resolved is None:
        return False
    vk, implied = resolved
    mod_list = []
    for m in list(mods or []) + implied:
        if m in MODIFIERS and m not in mod_list:
            mod_list.append(m)
    mod_vks = [VK[m] for m in mod_list]
    events = [_vk_events(m, False) for m in mod_vks]
    events.append(_vk_events(vk, False))
    events.append(_vk_events(vk, True))
    events.extend(_vk_events(m, True) for m in reversed(mod_vks))
    _send(*events)
    return True


def key_up(name: str) -> None:
    resolved = resolve_key(name)
    if resolved:
        _send(_vk_events(resolved[0], True))


def type_text(text: str) -> None:
    """Type arbitrary Unicode text, independent of keyboard layout."""
    events = []
    for ch in text:
        if ch == "\n":
            events += [_vk_events(VK["enter"], False), _vk_events(VK["enter"], True)]
        elif ch == "\t":
            events += [_vk_events(VK["tab"], False), _vk_events(VK["tab"], True)]
        else:
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):  # surrogate pairs -> two code units
                unit = int.from_bytes(data[i:i + 2], "little")
                events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE))
                events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send(*events)
