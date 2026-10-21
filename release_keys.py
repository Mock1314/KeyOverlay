# -*- coding: utf-8 -*-
"""release_keys.py —— 键盘/鼠标"卡在按下"时的一键解卡。

症状：
  * 打字全是快捷键效果（比如按 W 变成关窗口）——Ctrl / Alt / Win 被卡住了；
  * 打字全是大写或符号不对——Shift 被卡住了；
  * 滚轮不滚动、变成缩放页面——Ctrl 卡着时滚轮就是缩放；
  * 鼠标能移动但点不动——左键卡在"按住拖动"。

原理：给所有虚拟键码补发一遍"抬起"。抬起一个本来没按下的键是无害的
（系统和程序都会忽略），所以这个脚本可以放心双击，不会产生任何"按下"。

用法：
    python release_keys.py       # 解卡并打印结果
    python release_keys.py -q    # 静默
"""
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP = 0x0004, 0x0010
MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_XUP = 0x0040, 0x0100

WATCH = {
    0x10: "Shift", 0xA0: "左Shift", 0xA1: "右Shift",
    0x11: "Ctrl", 0xA2: "左Ctrl", 0xA3: "右Ctrl",
    0x12: "Alt", 0xA4: "左Alt", 0xA5: "右Alt",
    0x5B: "左Win", 0x5C: "右Win",
    0x01: "鼠标左键", 0x02: "鼠标右键", 0x04: "鼠标中键", 0x05: "侧键X1", 0x06: "侧键X2",
}

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
MOUSE_UP = {0x01: MOUSEEVENTF_LEFTUP, 0x02: MOUSEEVENTF_RIGHTUP,
            0x04: MOUSEEVENTF_MIDDLEUP, 0x05: MOUSEEVENTF_XUP, 0x06: MOUSEEVENTF_XUP}


def snapshot():
    return [n for vk, n in WATCH.items() if user32.GetAsyncKeyState(vk) & 0x8000]


def key_up(vk):
    i = INPUT()
    i.type = 1
    i.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def mouse_up(vk):
    i = INPUT()
    i.type = 0
    i.u.mi = MOUSEINPUT(0, 0, 1 if vk == 0x05 else (2 if vk == 0x06 else 0),
                        MOUSE_UP[vk], 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetForegroundWindow.restype = wintypes.HWND


def foreground_name():
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return hwnd, (buf.value or "(无标题)")


def post_keyup(hwnd, vk):
    """直接给前台窗口投递 WM_KEYUP。

    SendInput 的抬起只会送到"当时的前台窗口"，如果那个记着"Ctrl 还按着"的程序此刻不在前台，
    它就一直不知道键已经松了（表现就是打字全是快捷键）。这里补一条消息直接投给它。
    """
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0x00000001)
    if vk in (0x12, 0xA4, 0xA5):
        user32.PostMessageW(hwnd, WM_SYSKEYUP, vk, 0xC0000001)


def main():
    quiet = "-q" in sys.argv
    before = snapshot()
    if not quiet:
        print("解卡前：", "、".join(before) + " 卡在按下状态" if before else "系统层没有键卡住")

    # 鼠标键 + 所有虚拟键码，各补发两轮抬起
    for _ in range(2):
        for vk in MOUSE_UP:
            mouse_up(vk)
        for vk in range(0x01, 0xFF):
            key_up(vk)
        time.sleep(0.08)
    time.sleep(0.2)

    # 再给前台窗口单独补一遍（治"程序自己记着 Ctrl 按着"那种）
    hwnd, title = foreground_name()
    if hwnd:
        for vk in list(WATCH) + list(range(0x01, 0xFF)):
            post_keyup(hwnd, vk)
        if not quiet:
            print("已给前台窗口「%s」补发了一遍抬起" % title)

    after = snapshot()
    print("解卡后：", "、".join(after) + " 仍然卡住（可能是物理按键真的按着，或那个软件自身乱了）"
          if after else "全部松开 ✓")
    if before and not after and not quiet:
        print("已修复：%s 之前卡在按下状态，已补发抬起。" % "、".join(before))
    return 0 if not after else 1


if __name__ == "__main__":
    raise SystemExit(main())
