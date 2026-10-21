# -*- coding: utf-8 -*-
"""
stop.py —— 强行关掉所有键鼠覆盖层实例（不依赖热键）。

用法：
    python stop.py          # 优雅关闭：给窗口发 WM_CLOSE，等 1.5 秒，还活着的直接结束进程
    python stop.py --force  # 不给警告，直接结束进程

之所以要这个工具：热键有可能被键盘的多媒体键占掉（需要 Fn+F10），
或者开了多个旧实例，按一次 F10 只关掉其中一个。
这个脚本按窗口类名找，一个不漏。
"""
import ctypes
import sys
import time
from ctypes import wintypes

CLASS_NAME = "KeyMouseOverlayWnd"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def find_windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == CLASS_NAME:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append((hwnd, pid.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def main():
    force = "--force" in sys.argv
    wins = find_windows()
    if not wins:
        print("没有正在运行的覆盖层实例。")
        return 0

    print("找到 %d 个实例：%s" % (len(wins), ", ".join("pid=%d" % p for _, p in wins)))
    if not force:
        for hwnd, _ in wins:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)      # WM_CLOSE
        for _ in range(15):
            time.sleep(0.1)
            if not find_windows():
                break

    left = find_windows()
    for hwnd, pid in left:
        h = kernel32.OpenProcess(0x0001, False, pid)     # PROCESS_TERMINATE
        if h:
            kernel32.TerminateProcess(h, 0)
            kernel32.CloseHandle(h)

    time.sleep(0.3)
    left = find_windows()
    print("已全部关闭。" if not left else "还有 %d 个没关掉（可能需要管理员权限）。" % len(left))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
