# -*- coding: utf-8 -*-
"""
keyoverlay.py —— 直接按 keyboard-mouse.svg 渲染的键鼠覆盖层。

思路：不再手写几何，而是**解析 SVG 里的路径**（含二次/三次贝塞尔、圆角、字形路径、
transform 矩阵），再用 Pillow 逐元素栅格化。因此形状、圆角、字重、鼠标轮廓和
参考图完全一致，只是把"按下高亮"做成了可交互的状态。

用法：
    python keyoverlay.py                 # 默认放屏幕左下角、1.2 倍、点击穿透
    python keyoverlay.py --scale 1.0
    python keyoverlay.py --pos 40,900
    python keyoverlay.py --bg            # 连 SVG 的黑色底板一起画
    python keyoverlay.py --mouse-yellow  # 鼠标左右键常亮黄色（不带按下反馈）
    python keyoverlay.py --solid         # 取消点击穿透
    python keyoverlay.py --preview out.png [--pressed W,LMB]
    python keyoverlay.py --preview out.png --pressed WHEEL_UP   # 预览滚轮上滚（WHEEL_DOWN 同理）
    python keyoverlay.py --no-wheel      # 不采集滚轮（不注册 Raw Input）
    python keyoverlay.py --wheel-hold 0.15
    python keyoverlay.py --toggle-vk 0x78 --quit-vk 0x79

运行中：F9 隐藏/显示，F10 退出。
"""
import argparse
import ctypes
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from ctypes import wintypes

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# 打包成 exe（PyInstaller）后：__file__ 指向临时解包目录，exe 实际在 sys.executable 旁边。
# HERE 用来放日志等"要留得住"的文件；_RES 用来找打包进去的只读资源。
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
    _RES = getattr(sys, "_MEIPASS", HERE)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    _RES = HERE


def _resource(name):
    """先找 exe/脚本旁边的同名文件（换设计稿只需丢一个 svg 过去），否则用打包进去的那份。"""
    local = os.path.join(HERE, name)
    if os.path.exists(local):
        return local
    return os.path.join(_RES, name)


SVG_FILE = _resource("keyboard-mouse.svg")

# ============================ 外观参数 ============================
SS = 4                     # 超采样倍数
ART_SCALE = 0.34           # SVG 单位 -> scale=1 时的像素
HILITE = (233, 223, 104, 255)      # 按下填充（取自 SVG 黄色渐变中间色）
WHEEL_ARROW = (222, 45, 38, 255)    # 滚轮滚动方向箭头：黄底上用红色
# 方向箭头几何（相对滚轮胶囊中心的 SVG 单位，向上版本；向下时 y 取反）
WHEEL_HEAD_DX = 16.0               # 折线头单侧半宽
WHEEL_HEAD_Y = -31.0               # 尖端 y
WHEEL_BARB_Y = -13.0               # 折线两翼 y
WHEEL_TAIL_Y = 31.0                # 箭杆末端 y
WHEEL_STROKE = 6.0                 # 箭头线宽（SVG 单位）
WHEEL_DOT_R = 11.5                 # 中键按下时那个红点的半径

# --- 鼠标移动方向箭头（独立叠加层，不进整帧缓存） --------------------------------
MOVE_ARROW = (222, 45, 38, 255)
MOVE_CENTER = (1035.0, 575.0)      # 箭杆尾端位置，SVG 坐标（鼠标主体几何中心）
MOVE_DIRS = 16                     # 方向档位：每 22.5° 一档
MOVE_LEN = 28.0                    # scale=1 时从中心算起的箭头长度（像素）
MOVE_HEAD = 13.0                   # 折线头半宽（像素）
MOVE_STROKE = 4.5                  # 箭头线宽（像素）
MOVE_DECAY = 0.6                   # 位移平滑系数：越大越"记着"旧方向，抖动少但转向迟钝
MOVE_MIN = 2.0                     # 平滑后的位移小于此值就当作没动（像素）
# 按下时字母保持 SVG 原本的白色，不改成深色（想反过来就把下面这行接进 _tile 的 label 分支）
# DARK_TEXT = (26, 29, 35, 255)
GLOW = True                # 白线外发光（对应 SVG 里那层 19% 的模糊副本）
GLOW_ALPHA = 0.19
SHADOW = True              # 深色投影，压到亮画面上也能看清
SHADOW_BLUR = 1.4
SHADOW_OFFSET = (1, 2)
SHADOW_ALPHA = 0.55
SHADOW_MARGIN = 6

# 元素 id / 字形 aria-label -> 虚拟键码
KEYMAP = {
    "Q": 0x51, "W": 0x57, "E": 0x45,
    "SHIFT": 0xA0, "A": 0x41, "S": 0x53, "D": 0x44,
    "CTRL": 0xA2, "SPACE": 0x20,
}
MOUSE_BTNS = [("LMB", 0x01), ("RMB", 0x02), ("MMB", 0x04), ("X1", 0x05), ("X2", 0x06)]
# 右 Shift / 右 Ctrl 也映射到同一个显示名，否则按右边那颗不会亮
ALT_VK = {"SHIFT": (0xA1,), "CTRL": (0xA3,)}
POLL_KEYS = list(KEYMAP.items()) + [(n, v) for n, vs in ALT_VK.items() for v in vs]

PRESS_BY_ID = {
    "key-q": "Q", "key-w": "W", "key-e": "E",
    "key-shift": "SHIFT", "key-a": "A", "key-s": "S", "key-d": "D",
    "key-ctrl": "CTRL", "key-space": "SPACE",
    "mouse-left": "LMB", "mouse-right": "RMB", "mouse-wheel": "MMB",
    "mouse-side-front": "X1", "mouse-side-back": "X2",
}


# ============================ SVG 解析 ============================
_NUM = r"-?\d*\.?\d+(?:[eE][-+]?\d+)?"
_TOK = re.compile(r"[MmLlHhVvCcQqSsTtZz]|" + _NUM)


def _mul(a, b):
    """矩阵相乘：先 b 后 a。"""
    return [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
            a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]]


def _parse_transform(s):
    m = [1, 0, 0, 1, 0, 0]
    for name, arg in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.findall(_NUM, arg)]
        if not v:
            continue
        if name == "translate":
            t = [1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0]
        elif name == "scale":
            t = [v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0]
        elif name == "matrix" and len(v) >= 6:
            t = v[:6]
        elif name == "skewX":
            t = [1, 0, math.tan(math.radians(v[0])), 1, 0, 0]
        elif name == "skewY":
            t = [1, math.tan(math.radians(v[0])), 0, 1, 0, 0]
        else:
            continue
        m = _mul(m, t)
    return m


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _path_subpaths(d, seg=8):
    """把 path 的 d 解析成若干子路径：[(points, closed), ...]"""
    tok = _TOK.findall(d)
    subs = []
    i, cmd = 0, None
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    pts = []

    def flush(closed):
        if len(pts) >= 2:
            subs.append((list(pts), closed))

    while i < len(tok):
        if tok[i].isalpha():
            cmd = tok[i]
            i += 1
        if cmd in ("Z", "z"):
            flush(True)
            pts = []
            cur = start
            cmd = None
            continue
        try:
            if cmd in ("M", "m"):
                x, y = float(tok[i]), float(tok[i + 1]); i += 2
                if cmd == "m":
                    x, y = cur[0] + x, cur[1] + y
                flush(False)
                pts = [(x, y)]
                cur = start = (x, y)
                cmd = "L" if cmd == "M" else "l"
            elif cmd in ("L", "l"):
                x, y = float(tok[i]), float(tok[i + 1]); i += 2
                if cmd == "l":
                    x, y = cur[0] + x, cur[1] + y
                pts.append((x, y)); cur = (x, y)
            elif cmd in ("H", "h"):
                x = float(tok[i]); i += 1
                x = cur[0] + x if cmd == "h" else x
                pts.append((x, cur[1])); cur = (x, cur[1])
            elif cmd in ("V", "v"):
                y = float(tok[i]); i += 1
                y = cur[1] + y if cmd == "v" else y
                pts.append((cur[0], y)); cur = (cur[0], y)
            elif cmd in ("Q", "q"):
                x1, y1, x, y = [float(t) for t in tok[i:i + 4]]; i += 4
                if cmd == "q":
                    x1, y1 = cur[0] + x1, cur[1] + y1
                    x, y = cur[0] + x, cur[1] + y
                for s in range(1, seg + 1):
                    t = s / seg
                    a = (1 - t) ** 2; b = 2 * (1 - t) * t; c = t * t
                    pts.append((a * cur[0] + b * x1 + c * x, a * cur[1] + b * y1 + c * y))
                cur = (x, y)
            elif cmd in ("C", "c"):
                x1, y1, x2, y2, x, y = [float(t) for t in tok[i:i + 6]]; i += 6
                if cmd == "c":
                    x1, y1 = cur[0] + x1, cur[1] + y1
                    x2, y2 = cur[0] + x2, cur[1] + y2
                    x, y = cur[0] + x, cur[1] + y
                for s in range(1, seg + 1):
                    t = s / seg
                    a = (1 - t) ** 3; b = 3 * (1 - t) ** 2 * t
                    c = 3 * (1 - t) * t * t; e = t ** 3
                    pts.append((a * cur[0] + b * x1 + c * x2 + e * x,
                                a * cur[1] + b * y1 + c * y2 + e * y))
                cur = (x, y)
            else:
                i += 1
        except (IndexError, ValueError):
            break
    flush(False)
    return subs


def _rect_subpaths(x, y, w, h, rx, ry, seg=8):
    if rx <= 0 and ry <= 0:
        return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)]
    pts = []
    for cx, cy, a0 in ((x + w - rx, y + ry, -90), (x + w - rx, y + h - ry, 0),
                       (x + rx, y + h - ry, 90), (x + rx, y + ry, 180)):
        for s in range(seg + 1):
            a = math.radians(a0 + 90 * s / seg)
            pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return [(pts, True)]


def _color(txt, default=None):
    if not txt or txt in ("none", "transparent"):
        return None
    if txt.startswith("url("):
        return None
    if txt.startswith("#"):
        h = txt[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    return default


def load_svg(path):
    """解析成元素列表，每个元素带：子路径、填充、描边、线宽、包围盒、按下映射。"""
    root = ET.parse(path).getroot()
    out = []

    def walk(node, attrs, mat):
        tag = node.tag.split("}")[-1]
        if tag in ("defs", "title", "desc", "filter", "linearGradient", "stop", "use"):
            return
        a = dict(attrs)
        for k in ("fill", "stroke", "stroke-width"):
            if k in node.attrib:
                a[k] = node.attrib[k]
        m = _mul(mat, _parse_transform(node.attrib.get("transform")))
        if tag in ("g", "svg"):
            for ch in node:
                walk(ch, a, m)
            return
        subs = []
        if tag == "path" and node.attrib.get("d"):
            subs = _path_subpaths(node.attrib["d"])
        elif tag == "rect":
            try:
                rx = float(node.attrib.get("rx") or 0)
                ry = float(node.attrib.get("ry") or rx)
                subs = _rect_subpaths(float(node.attrib["x"]), float(node.attrib["y"]),
                                      float(node.attrib["width"]), float(node.attrib["height"]),
                                      rx, ry)
            except (KeyError, ValueError):
                return
        else:
            return
        subs = [([_apply(m, x, y) for x, y in p], c) for p, c in subs]
        if not subs:
            return
        xs = [x for p, _ in subs for x, _ in p]
        ys = [y for p, _ in subs for _, y in p]
        det = abs(m[0] * m[3] - m[1] * m[2]) ** 0.5 or 1.0
        try:
            sw = float(a.get("stroke-width", 1)) * det
        except ValueError:
            sw = 1.0 * det
        eid = node.attrib.get("id")
        label = (node.attrib.get("aria-label") or "").upper()
        out.append(dict(
            id=eid,
            press=PRESS_BY_ID.get(eid) or (label if label in KEYMAP else None),
            label=bool(label),
            subs=subs,
            fill=_color(a.get("fill"), (0, 0, 0, 255)),
            yellow=str(a.get("fill") or "").startswith("url("),
            stroke=_color(a.get("stroke")),
            sw=sw,
            bbox=(min(xs), min(ys), max(xs), max(ys)),
        ))

    walk(root, {"fill": None, "stroke": None, "stroke-width": 1}, [1, 0, 0, 1, 0, 0])
    return out


ART = load_svg(SVG_FILE)
_BODY = next((e for e in ART if e["id"] == "background"), None)
_CONTENT = [e for e in ART if e["id"] != "background"]
BX0 = min(e["bbox"][0] for e in _CONTENT)
BY0 = min(e["bbox"][1] for e in _CONTENT)
BX1 = max(e["bbox"][2] for e in _CONTENT)
BY1 = max(e["bbox"][3] for e in _CONTENT)

_TILES = {}


# ============================ 栅格化 ============================
def _tile(index, k, hit, mouse_yellow=False, wheel=0):
    """把单个元素渲染成小图（带缓存）。返回 (图, 左上角坐标)。

    wheel: 1=向上滚、-1=向下滚、0=没滚。只有滚轮元素会用到，其余元素恒传 0，
    否则同一张键帽会因为滚动方向不同被缓存成十几份。
    """
    key = (index, round(k, 4), hit, mouse_yellow, wheel)
    if key in _TILES:
        return _TILES[key]

    e = ART[index]
    s = ART_SCALE * k
    x0, y0, x1, y1 = e["bbox"]
    sw = max(1, int(round(e["sw"] * s)))
    pad = sw + 3
    W = max(1, int(round((x1 - x0) * s)) + 2 * pad + 2)
    H = max(1, int(round((y1 - y0) * s)) + 2 * pad + 2)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    fill, stroke = e["fill"], e["stroke"]
    rolling = e["id"] == "mouse-wheel" and wheel
    if e["yellow"]:
        fill = HILITE if (hit or mouse_yellow) else None
    elif (hit or rolling) and not e["label"]:
        # 只有键帽（轮廓）变黄，字母保持原本的白色
        fill = HILITE

    def tp(x, y):
        return ((x - x0) * s + pad, (y - y0) * s + pad)

    for pts, closed in e["subs"]:
        p = [tp(x, y) for x, y in pts]
        if fill and closed:
            d.polygon(p, fill=fill)
        if stroke:
            d.line(p + [p[0]] if closed else p, fill=stroke, width=sw, joint="curve")

    if rolling:
        # 滚动方向箭头：黄底上画红色箭头，向上滚指上、向下滚指下（y 轴镜像）
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        sign = -1.0 if wheel < 0 else 1.0
        aw = max(1, int(round(WHEEL_STROKE * s)))
        tip = tp(cx, cy + sign * WHEEL_HEAD_Y)
        d.line([tp(cx, cy + sign * WHEEL_TAIL_Y), tip],
               fill=WHEEL_ARROW, width=aw)
        d.line([tp(cx - WHEEL_HEAD_DX, cy + sign * WHEEL_BARB_Y), tip,
                tp(cx + WHEEL_HEAD_DX, cy + sign * WHEEL_BARB_Y)],
               fill=WHEEL_ARROW, width=aw, joint="curve")
        d.ellipse([tip[0] - aw / 2.0, tip[1] - aw / 2.0,
                   tip[0] + aw / 2.0, tip[1] + aw / 2.0], fill=WHEEL_ARROW)
    elif e["id"] == "mouse-wheel" and hit:
        # 中键按下：居中一颗红点，和滚动的"带方向箭头"区分开。
        # 滚动优先——按着中键同时滚时只画箭头，方向信息更有用。
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        r = WHEEL_DOT_R * s
        c = tp(cx, cy)
        d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=WHEEL_ARROW)

    pos = (int(round(x0 * s)) - pad, int(round(y0 * s)) - pad)
    _TILES[key] = (im, pos)
    return im, pos


_FRAMES = OrderedDict()
FRAME_CACHE_MAX = 24


def render(pressed=(), scale=1.0, bg=False, mouse_yellow=False, move=-1):
    """move: -1 表示不画移动箭头，0..MOVE_DIRS-1 是方向档位。

    箭头做成叠加层：基础帧照旧按按键组合缓存，只在返回前复制一份再叠箭头。
    否则 (按键组合 × 16 个方向) 会把只有 24 格的帧缓存彻底冲垮。
    """
    key = (frozenset(pressed), round(scale, 3), bg, mouse_yellow)
    if key in _FRAMES:
        _FRAMES.move_to_end(key)
        im = _FRAMES[key]
        if move < 0:
            return im
        out = im.copy()
        _overlay_move(out, move, scale, bg)
        return out
    k = scale * SS
    pressed = key[0]
    if bg and _BODY is not None:
        box = (0, 0, 1280, 736)
    else:
        box = (BX0, BY0, BX1, BY1)

    pad = int(round(ART_SCALE * k * 4))
    W = int(round((box[2] - box[0]) * ART_SCALE * k)) + 2 * pad
    H = int(round((box[3] - box[1]) * ART_SCALE * k)) + 2 * pad
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ox, oy = pad - int(round(box[0] * ART_SCALE * k)), pad - int(round(box[1] * ART_SCALE * k))

    wheel = 1 if "WHEEL_UP" in pressed else (-1 if "WHEEL_DOWN" in pressed else 0)
    order = ([ART.index(_BODY)] if (bg and _BODY) else []) + \
            [i for i, e in enumerate(ART) if e["id"] != "background"]
    for i in order:
        e = ART[i]
        tile, pos = _tile(i, k, e["press"] in pressed, mouse_yellow,
                          wheel if e["id"] == "mouse-wheel" else 0)
        im.alpha_composite(tile, (pos[0] + ox, pos[1] + oy))

    im = im.resize((max(1, round(im.width / SS)), max(1, round(im.height / SS))),
                   Image.LANCZOS)

    if GLOW:
        mask = im.getchannel("A").point(lambda v: int(v * GLOW_ALPHA))
        glow = Image.new("RGBA", im.size, (255, 255, 255, 0))
        glow.putalpha(mask)
        glow = glow.filter(ImageFilter.GaussianBlur(max(0.8, 1.7 * ART_SCALE * scale * 1.6)))
        glow.alpha_composite(im)
        im = glow

    if SHADOW:
        im = _with_shadow(im)
    _FRAMES[key] = im
    while len(_FRAMES) > FRAME_CACHE_MAX:
        _FRAMES.popitem(last=False)
    if move < 0:
        return im
    out = im.copy()
    _overlay_move(out, move, scale, bg)
    return out


def _overlay_move(im, idx, scale, bg):
    """把方向箭头叠到已渲染好的整帧上（像素坐标，不参与帧缓存）。"""
    x, y = MOVE_CENTER
    box = (0, 0, 1280, 736) if bg else (BX0, BY0, BX1, BY1)
    s = ART_SCALE * scale
    off = SHADOW_MARGIN if SHADOW else 0
    # render 里的 pad 换算回最终像素：pad / SS == ART_SCALE * scale * 4
    pad = ART_SCALE * scale * 4.0
    cx = off + pad + (x - box[0]) * s
    cy = off + pad + (y - box[1]) * s

    ang = math.radians(idx * (360.0 / MOVE_DIRS))
    ux, uy = math.cos(ang), -math.sin(ang)      # 屏幕 y 轴向下，所以上行取负
    nx, ny = -uy, ux                            # 垂直于前进方向
    L = MOVE_LEN * scale
    H = MOVE_HEAD * scale
    aw = max(1, int(round(MOVE_STROKE * scale)))
    # 尾端固定在中心，箭头从中心往外指：方向变化时根部不动，视觉上更稳
    tip = (cx + ux * L, cy + uy * L)
    tail = (cx, cy)

    d = ImageDraw.Draw(im)
    d.line([tail, tip], fill=MOVE_ARROW, width=aw)
    for sgn in (1, -1):
        b = (tip[0] - ux * H * 0.75 + sgn * nx * H * 0.6,
             tip[1] - uy * H * 0.75 + sgn * ny * H * 0.6)
        d.line([b, tip], fill=MOVE_ARROW, width=aw, joint="curve")
    d.ellipse([tip[0] - aw / 2.0, tip[1] - aw / 2.0,
               tip[0] + aw / 2.0, tip[1] + aw / 2.0], fill=MOVE_ARROW)


def _with_shadow(im):
    m = SHADOW_MARGIN
    canvas = Image.new("RGBA", (im.width + 2 * m, im.height + 2 * m), (0, 0, 0, 0))
    alpha = im.getchannel("A").point(lambda v: int(v * SHADOW_ALPHA))
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.putalpha(alpha)
    sh = sh.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    canvas.alpha_composite(sh, (m + SHADOW_OFFSET[0], m + SHADOW_OFFSET[1]))
    canvas.alpha_composite(im, (m, m))
    return canvas


# ============================ Win32 ============================
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 2

# --- Raw Input：只读一份滚轮事件，不装钩子、不注入按键 -------------------------
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RI_MOUSE_WHEEL = 0x0400
RIDEV_INPUTSINK = 0x00000100   # 浮层永不激活，没有它一个滚轮事件都收不到

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
TOPMOST_REFRESH = 1.0      # 每隔多久重新声明一次置顶（秒）
CLASS_NAME = "KeyMouseOverlayWnd"
QUIT_EVENT = "KeyMouseOverlayQuitEvent"
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint),
                ("biClrImportant", ctypes.c_uint)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint * 3)]


kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT),
                                       ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(POINT),
                                       wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION),
                                       ctypes.c_uint]
# 64 位下句柄是 8 字节，不声明 argtypes 会被当成 int 而溢出
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.DefWindowProcW.restype = LRESULT
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, ctypes.c_uint]

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", ctypes.c_ulong), ("dwSize", ctypes.c_ulong),
                ("hDevice", ctypes.c_void_p), ("wParam", ctypes.c_void_p)]


class RAWMOUSE(ctypes.Structure):
    # usFlags 后必须有 2 字节填充：C 里那个 union 是 ULONG，直接写两个 ushort 会整体错位
    _fields_ = [("usFlags", ctypes.c_ushort), ("_pad", ctypes.c_ushort),
                ("usButtonFlags", ctypes.c_ushort), ("usButtonData", ctypes.c_short),
                ("ulRawButtons", ctypes.c_ulong), ("lLastX", ctypes.c_long),
                ("lLastY", ctypes.c_long), ("ulExtraInformation", ctypes.c_ulong)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


user32.GetRawInputData.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_uint), ctypes.c_uint]
user32.GetRawInputData.restype = ctypes.c_uint
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), ctypes.c_uint,
                                           ctypes.c_uint]
user32.RegisterRawInputDevices.restype = wintypes.BOOL

_WHEEL = {"dir": 0, "t": 0.0}     # 最近一次滚动的方向与时刻
_MOVE = {"dx": 0.0, "dy": 0.0, "t": 0.0}    # 平滑后的累计位移与最后移动时刻
_RAW_BUF = ctypes.create_string_buffer(ctypes.sizeof(RAWINPUT))   # 复用，避免每帧分配

_wndproc_ref = []


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:        # 原始输入：只挑滚轮，鼠标移动直接放过
        size = ctypes.c_uint(ctypes.sizeof(RAWINPUT))
        if user32.GetRawInputData(ctypes.c_void_p(lparam), RID_INPUT, _RAW_BUF,
                                  ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)):
            ri = RAWINPUT.from_buffer_copy(_RAW_BUF)
            if ri.header.dwType == RIM_TYPEMOUSE and (ri.mouse.usButtonFlags & RI_MOUSE_WHEEL):
                _WHEEL["dir"] = 1 if ri.mouse.usButtonData > 0 else -1
                _WHEEL["t"] = time.time()
        return 0
    if msg == 0x0010:          # WM_CLOSE：别人（stop.py / 任务栏）要求关闭
        user32.DestroyWindow(hwnd)
        return 0
    if msg == 0x0002:          # WM_DESTROY
        user32.PostQuitMessage(0)
        return 0
    if msg == 0x0012:          # WM_QUIT
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def create_window(w, h, x, y, clickthrough=True):
    hinst = kernel32.GetModuleHandleW(None)
    class_name = CLASS_NAME
    proc = WNDPROC(_wndproc)
    _wndproc_ref.append(proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0x0003  # CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, 32512)
    wc.lpszClassName = class_name
    user32.RegisterClassExW(ctypes.byref(wc))
    ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if clickthrough:
        ex |= WS_EX_TRANSPARENT
    hwnd = user32.CreateWindowExW(ex, class_name, "KeyMouse Overlay", WS_POPUP,
                                  x, y, w, h, None, None, hinst, None)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    return hwnd


def enable_wheel(hwnd):
    """注册原始输入（只读一份，不拦截）。失败就静默降级，不亮而已，不影响其他功能。"""
    rid = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd)
    return bool(user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                               ctypes.sizeof(RAWINPUTDEVICE)))


_DIB = {}          # (w, h) -> (hdc_mem, hbmp, bits)  复用，避免每帧 CreateDIBSection


def _get_dib(w, h):
    got = _DIB.get((w, h))
    if got:
        return got
    for key in list(_DIB):                 # 尺寸变了就丢掉旧的
        hdc_mem, hbmp, _ = _DIB.pop(key)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负值 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    gdi32.SelectObject(hdc_mem, hbmp)
    user32.ReleaseDC(None, hdc_screen)
    _DIB[(w, h)] = (hdc_mem, hbmp, bits)
    return _DIB[(w, h)]


def blit(hwnd, im, x, y):
    w, h = im.size
    r, g, b, a = im.split()
    prem = Image.merge("RGBA", (ImageChops.multiply(r, a), ImageChops.multiply(g, a),
                                ImageChops.multiply(b, a), a))
    data = prem.tobytes("raw", "BGRA")

    hdc_mem, _hbmp, bits = _get_dib(w, h)
    ctypes.memmove(bits, data, len(data))

    hdc_screen = user32.GetDC(None)
    pt_dst = POINT(x, y)
    pt_src = POINT(0, 0)
    size = SIZE(w, h)
    blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst), ctypes.byref(size),
                               hdc_mem, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA)
    user32.ReleaseDC(None, hdc_screen)


def pump():
    """处理消息队列；收到 WM_QUIT 返回 False。"""
    msg = wintypes.MSG()
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        if msg.message == 0x0012:      # WM_QUIT
            return False
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return True


def is_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def get_cursor():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _log_exc():
    """渲染/贴图出错时记一笔日志——pythonw 没有控制台，不记就等于静默死掉。"""
    try:
        import traceback
        with open(os.path.join(HERE, "keyoverlay.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + traceback.format_exc() + "\n")
    except Exception:
        pass



# ============================ 系统托盘 ============================
# 用 Shell_NotifyIcon 做托盘图标 + 右键菜单：显示/隐藏、重新加载设计稿、退出。
# 托盘消息需要一个能收消息的窗口；这里单独建一个"隐藏的普通窗口"，
# 而不是复用浮层窗口——浮层是 WS_EX_NOACTIVATE|TOOLWINDOW，用它做菜单宿主时
# SetForegroundWindow 会失败，导致"点菜单外面不会关闭"的老毛病。
TRAY_CLASS = "KeyMouseOverlayTray"
TRAY_MSG = 0x8001          # WM_APP + 1
WM_COMMAND = 0x0111
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK, WM_CONTEXTMENU = 0x0202, 0x0205, 0x0203, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0800, 0x0008
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
ID_TOGGLE, ID_RELOAD, ID_QUIT = 1001, 1002, 1003

CMD = {"toggle": False, "reload": False, "quit": False}
_TRAY = {"hwnd": None, "hicon": None, "icon": None, "on": False, "visible": True}


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINTER)] if False else [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

_tray_proc_ref = []


def _load_tray_icon():
    """优先用 exe/脚本旁边的 app.ico，没有就用 exe 自带图标，再没有就系统默认。"""
    cx = user32.GetSystemMetrics(49)      # SM_CXSMICON
    cy = user32.GetSystemMetrics(50)      # SM_CYSMICON
    for name in ("app.ico",):
        path = _resource(name)
        if os.path.exists(path):
            h = user32.LoadImageW(None, path, 1, cx, cy, 0x0010)   # IMAGE_ICON | LR_LOADFROMFILE
            if h:
                return h, True
    if getattr(sys, "frozen", False):
        h = user32.LoadImageW(None, sys.executable, 1, cx, cy, 0x0000)
        if h:
            return h, True
    return user32.LoadIconW(None, ctypes.c_void_p(32512)), False   # IDI_APPLICATION


def tray_set_tip(text):
    d = _TRAY
    if not d["on"]:
        return
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = d["hwnd"]
    nid.uID = 1
    nid.uFlags = NIF_TIP
    nid.szTip = text[:127]
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))


def tray_add():
    d = _TRAY
    if d["on"] or not d["hwnd"]:
        return
    hicon, owned = _load_tray_icon()
    d["hicon"], d["icon"] = hicon, owned
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = d["hwnd"]
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = TRAY_MSG
    nid.hIcon = hicon
    nid.szTip = "键鼠显示  ·  左键单击显示/隐藏  ·  右键菜单"
    ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
    d["on"] = ok
    print("托盘图标：%s" % ("已添加" if ok else "添加失败，错误码 %d" % ctypes.get_last_error()))


def tray_remove():
    d = _TRAY
    if not d["on"]:
        return
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = d["hwnd"]
    nid.uID = 1
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    if d["icon"] and d["hicon"]:
        user32.DestroyIcon(d["hicon"])
    d["on"] = False


def show_tray_menu(hwnd):
    menu = user32.CreatePopupMenu()
    flags = MF_STRING | (0 if _TRAY["visible"] else MF_CHECKED)
    user32.AppendMenuW(menu, MF_STRING, ID_TOGGLE, "隐藏浮层" if _TRAY["visible"] else "显示浮层")
    user32.AppendMenuW(menu, MF_STRING, ID_RELOAD, "重新加载设计稿 (svg)")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)      # 否则点菜单外面不会自动关
    cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                                pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, 0x0000, 0, 0)
    user32.DestroyMenu(menu)
    if cmd == ID_TOGGLE:
        CMD["toggle"] = True
    elif cmd == ID_RELOAD:
        CMD["reload"] = True
    elif cmd == ID_QUIT:
        CMD["quit"] = True


def _tray_wndproc(hwnd, msg, wparam, lparam):
    if msg == TRAY_MSG:
        if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
            show_tray_menu(hwnd)
        elif lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            CMD["toggle"] = True
        return 0
    if msg == WM_COMMAND:
        rid = wparam & 0xFFFF
        if rid == ID_TOGGLE:
            CMD["toggle"] = True
        elif rid == ID_RELOAD:
            CMD["reload"] = True
        elif rid == ID_QUIT:
            CMD["quit"] = True
        return 0
    if msg in (0x0010, 0x0002):        # WM_CLOSE / WM_DESTROY
        return 0                        # 托盘窗口不许被关掉
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def create_tray_window():
    hinst = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(_tray_wndproc)
    _tray_proc_ref.append(proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = TRAY_CLASS
    user32.RegisterClassExW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, TRAY_CLASS, "KeyMouseOverlayTray", WS_POPUP,
                                  0, 0, 0, 0, None, None, hinst, None)
    _TRAY["hwnd"] = hwnd
    return hwnd


def reload_art():
    """重新解析 SVG 并清空贴片/帧缓存（托盘菜单里的"重新加载设计稿"）。"""
    global ART, _BODY, _CONTENT, BX0, BY0, BX1, BY1
    ART = load_svg(SVG_FILE)
    _BODY = next((e for e in ART if e["id"] == "background"), None)
    _CONTENT = [e for e in ART if e["id"] != "background"]
    BX0 = min(e["bbox"][0] for e in _CONTENT)
    BY0 = min(e["bbox"][1] for e in _CONTENT)
    BX1 = max(e["bbox"][2] for e in _CONTENT)
    BY1 = max(e["bbox"][3] for e in _CONTENT)
    _TILES.clear()
    _FRAMES.clear()


# ============================ 主流程 ============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", default=None, help="左上角坐标 x,y（默认左下角）")
    ap.add_argument("--margin", type=int, default=40)
    ap.add_argument("--scale", type=float, default=1.2)
    ap.add_argument("--bg", action="store_true", help="连 SVG 的黑色底板一起画")
    ap.add_argument("--mouse-yellow", action="store_true", help="鼠标左右键常亮黄色")
    ap.add_argument("--solid", action="store_true", help="取消点击穿透")
    ap.add_argument("--preview", default=None, help="只渲染一张静态图到指定路径")
    ap.add_argument("--pressed", default="W,LMB", help="预览时高亮的键，逗号分隔")
    ap.add_argument("--hold", type=float, default=0.03,
                    help="按下后至少点亮多久（秒）。调小=连点更容易分成两下，调大=更快的手点也能看清")
    ap.add_argument("--no-tray", action="store_true", help="不创建系统托盘图标")
    ap.add_argument("--debug", action="store_true", help="把状态变化写进 keyoverlay.log，排查连点用")
    ap.add_argument("--poll", type=float, default=0.006,
                    help="轮询间隔（秒），默认 0.006")
    ap.add_argument("--wheel-hold", type=float, default=0.15,
                    help="滚轮滚动后至少点亮多久（秒）。滚动是瞬时事件，比 --hold 长才看得清")
    ap.add_argument("--no-wheel", action="store_true",
                    help="不采集滚轮滚动（不注册 Raw Input，零开销）")
    ap.add_argument("--move-hold", type=float, default=0.25,
                    help="鼠标停止后方向箭头再保留多久（秒），默认 0.25")
    ap.add_argument("--no-move", action="store_true",
                    help="不显示鼠标移动方向箭头")
    ap.add_argument("--move-deg", type=float, default=None,
                    help="预览时移动箭头的角度（0=向右，90=向上，180=向左，270=向下）")
    ap.add_argument("--toggle-vk", default="0x78", help="隐藏/显示热键的虚拟键码（默认 0x78 = F9）")
    ap.add_argument("--quit-vk", default="0x79,0x7A",
                    help="退出热键的虚拟键码，可写多个（默认 0x79,0x7A = F10 或 F11；0x7A 其实是 F11）")
    args = ap.parse_args()

    if args.preview:
        hit = tuple(v.strip().upper() for v in args.pressed.split(",") if v.strip())
        mv = -1 if args.move_deg is None else \
            int(round(args.move_deg / (360.0 / MOVE_DIRS))) % MOVE_DIRS
        im = render(pressed=hit, scale=args.scale, bg=args.bg,
                    mouse_yellow=args.mouse_yellow, move=mv)
        bg = Image.new("RGBA", im.size, (32, 38, 52, 255))
        bg.alpha_composite(im)
        bg.convert("RGB").save(args.preview)
        print("已生成预览:", args.preview, im.size)
        return 0

    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    # 单实例：已经有实例在跑就让旧的安静退出，自己接管（避免叠出多个浮层，F9 只关掉一个）
    h_event = kernel32.CreateEventW(None, False, False, QUIT_EVENT)
    old = user32.FindWindowW(CLASS_NAME, None)
    if old:
        kernel32.SetEvent(h_event)
        for _ in range(40):
            time.sleep(0.05)
            if not user32.FindWindowW(CLASS_NAME, None):
                break

    img = render(scale=args.scale, bg=args.bg, mouse_yellow=args.mouse_yellow)
    w, h = img.size
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    if args.pos:
        x, y = [int(v) for v in args.pos.split(",")]
    else:
        x, y = args.margin, sh - h - args.margin
    if SHADOW:
        x -= SHADOW_MARGIN
        y -= SHADOW_MARGIN

    hwnd = create_window(w, h, x, y, clickthrough=not args.solid)
    if not hwnd:
        print("创建窗口失败，错误码:", ctypes.get_last_error())
        return 1

    wheel_on = False if args.no_wheel else enable_wheel(hwnd)

    blit(hwnd, img, x, y)

    # 托盘图标：单击显示/隐藏，右键出菜单
    if not args.no_tray:
        try:
            create_tray_window()
            tray_add()
        except Exception:
            _log_exc()

    prev_state = frozenset()
    last_top = 0.0
    last_seen = {}          # 名称 -> 最后按下的时刻，实现最短显示时长
    MIN_HOLD = max(0.02, args.hold)
    WHEEL_HOLD = max(0.02, args.wheel_hold)
    POLL = max(0.002, args.poll)
    move_on = not args.no_move
    MOVE_HOLD = max(0.02, args.move_hold)
    MIN_DRAW_GAP = 0.02     # 仅方向变化时至少间隔这么久再重绘（别把 6ms 轮询拖死）
    prev_move = -1
    last_draw = 0.0
    last_cursor = get_cursor()
    dbg = args.debug
    dbg_f = open(os.path.join(HERE, "keyoverlay.log"), "w", encoding="utf-8") if dbg else None
    toggle_vk = int(args.toggle_vk, 0)
    quit_vks = [int(v, 0) for v in str(args.quit_vk).split(",") if v.strip()]
    prev_f9 = prev_f10 = False
    visible = True
    print("已启动：%dx%d @ (%d,%d)   F9 隐藏/显示，F10 退出" % (w, h, x, y))
    print("滚轮反馈：%s" % ("已启用（Raw Input 只读，不拦截）" if wheel_on else "未启用"))

    try:
        while True:
            if not pump():
                break
            if h_event and kernel32.WaitForSingleObject(h_event, 0) == 0:
                break
            now = time.time()
            cur = get_cursor()
            mdx, mdy = cur[0] - last_cursor[0], cur[1] - last_cursor[1]
            last_cursor = cur
            if mdx or mdy:
                # 指数平滑：单次采样当方向会让手抖带着箭头乱摆
                _MOVE["dx"] = _MOVE["dx"] * MOVE_DECAY + mdx
                _MOVE["dy"] = _MOVE["dy"] * MOVE_DECAY + mdy
                _MOVE["t"] = now
            move = -1
            if move_on and now - _MOVE["t"] < MOVE_HOLD and \
                    abs(_MOVE["dx"]) + abs(_MOVE["dy"]) >= MOVE_MIN:
                deg = math.degrees(math.atan2(-_MOVE["dy"], _MOVE["dx"]))
                move = int(round(deg / (360.0 / MOVE_DIRS))) % MOVE_DIRS

            down = {n for n, vk in POLL_KEYS if is_down(vk)}
            down |= {b[0] for b in MOUSE_BTNS if is_down(b[1])}
            for n in down:
                last_seen[n] = now
            state = frozenset(n for n, t in last_seen.items() if now - t < MIN_HOLD)
            if wheel_on and now - _WHEEL["t"] < WHEEL_HOLD:
                state = state | {"WHEEL_UP" if _WHEEL["dir"] > 0 else "WHEEL_DOWN"}

            changed = state != prev_state
            # 按键变化立即响应；只有方向变了才受最小间隔约束
            if changed or (move != prev_move and now - last_draw >= MIN_DRAW_GAP):
                if dbg_f and changed:
                    dbg_f.write("%.3f %s\n" % (now, ",".join(sorted(state)) or "-"))
                    dbg_f.flush()
                if visible:
                    try:
                        img = render(pressed=state, scale=args.scale, bg=args.bg,
                                     mouse_yellow=args.mouse_yellow, move=move)
                    except Exception:
                        _log_exc()
                        img = None
                    if img is not None:
                        if img.size != (w, h):
                            w, h = img.size
                            user32.SetWindowPos(hwnd, None, x, y, w, h,
                                                SWP_NOZORDER | SWP_NOACTIVATE)
                        blit(hwnd, img, x, y)
                prev_state = state
                prev_move = move
                last_draw = now

            if CMD["quit"]:
                CMD["quit"] = False
                break
            if CMD["reload"]:
                CMD["reload"] = False
                try:
                    reload_art()
                except Exception:
                    _log_exc()
                img = render(pressed=prev_state, scale=args.scale,
                             bg=args.bg, mouse_yellow=args.mouse_yellow)
                if img.size != (w, h):
                    w, h = img.size
                    user32.SetWindowPos(hwnd, None, x, y, w, h,
                                        SWP_NOZORDER | SWP_NOACTIVATE)
                blit(hwnd, img, x, y)

            toggle_req = CMD["toggle"]
            CMD["toggle"] = False
            f9 = is_down(toggle_vk)
            f10 = any(is_down(v) for v in quit_vks)
            if (f9 and not prev_f9) or toggle_req:
                visible = not visible
                _TRAY["visible"] = visible
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else 0)
                if visible:
                    blit(hwnd, img, x, y)
                tray_set_tip("键鼠显示（显示中）· 左键单击隐藏 · 右键菜单" if visible
                             else "键鼠显示（已隐藏）· 左键单击显示 · 右键菜单")
                print("显示" if visible else "隐藏")
            if f10 and not prev_f10:
                print("退出")
                break
            prev_f9, prev_f10 = f9, f10

            # 有些游戏会抢置顶，把浮层压到下面；隔一秒重申一次
            if now - last_top > TOPMOST_REFRESH:
                user32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                last_top = now
            time.sleep(POLL)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            tray_remove()
        except Exception:
            pass
        user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
