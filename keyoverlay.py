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

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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
# 两套布局：classic 是上游原版 9 键，extended 是后来扩的 25 键。切换时只换 SVG 源。
LAYOUT_FILES = {
    "classic": _resource("keyboard-mouse-classic.svg"),
    "extended": _resource("keyboard-mouse.svg"),
}
LAYOUT_NAMES = {"classic": "经典布局", "extended": "扩展布局"}

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
MOVE_ARROW = (233, 223, 104, 255)   # 沿用 SVG 设计稿的黄色，和红色滚轮箭头区分开
MOVE_CENTER = (1382.0, 660.0)      # 箭头旋转中心，SVG 坐标。启动后按 mouse-body 的 bbox 重算，
                                   # 换布局时自动跟着走，不用手改（写死的值只作兜底）
MOVE_DIRS = 16                     # 方向档位：每 22.5° 一档
MOVE_LEN = 32.0                    # scale=1 时的箭头总长（以箭身中心为旋转点，两端各占一半）
MOVE_HEAD = 12.0                   # 折线头尺寸（像素）
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
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
    "TAB": 0x09,
    "Q": 0x51, "W": 0x57, "E": 0x45, "R": 0x52, "T": 0x54,
    "SHIFT": 0xA0, "A": 0x41, "S": 0x53, "D": 0x44, "F": 0x46, "G": 0x47,
    "ALT": 0xA4, "Z": 0x5A, "X": 0x58, "C": 0x43, "V": 0x56, "B": 0x42,
    "CTRL": 0xA2, "SPACE": 0x20,
}
MOUSE_BTNS = [("LMB", 0x01), ("RMB", 0x02), ("MMB", 0x04), ("X1", 0x05), ("X2", 0x06)]
# 右侧的 Shift / Ctrl / Alt 也映射到同一个显示名，否则按右边那颗不会亮
ALT_VK = {"SHIFT": (0xA1,), "CTRL": (0xA3,), "ALT": (0xA5,)}
POLL_KEYS = list(KEYMAP.items()) + [(n, v) for n, vs in ALT_VK.items() for v in vs]

PRESS_BY_ID = {
    "key-1": "1", "key-2": "2", "key-3": "3", "key-4": "4", "key-5": "5",
    "key-tab": "TAB",
    "key-q": "Q", "key-w": "W", "key-e": "E", "key-r": "R", "key-t": "T",
    "key-shift": "SHIFT",
    "key-a": "A", "key-s": "S", "key-d": "D", "key-f": "F", "key-g": "G",
    "key-alt": "ALT",
    "key-z": "Z", "key-x": "X", "key-c": "C", "key-v": "V", "key-b": "B",
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


def compute_move_center():
    """箭头中心 = 鼠标主体的几何中心。换布局时鼠标位置会变，所以不能写死。"""
    for e in ART:
        if e.get("id") == "mouse-body":
            b = e["bbox"]
            return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
    return MOVE_CENTER


MOVE_CENTER = compute_move_center()

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

    polys = [([tp(x, y) for x, y in pts], closed) for pts, closed in e["subs"]]
    if fill:
        # 异或合成所有子路径，保留字形内部的镂空（R 的碗、A 的三角、D 的孔…）
        mask = Image.new("L", (W, H), 0)
        for p, closed in polys:
            if not closed or len(p) < 3:
                continue
            tmp = Image.new("L", (W, H), 0)
            ImageDraw.Draw(tmp).polygon(p, fill=255)
            mask = ImageChops.difference(mask, tmp)
        im.paste(fill, (0, 0), mask)
    if stroke:
        for p, closed in polys:
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
        box = tuple(_BODY["bbox"])
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
    box = tuple(_BODY["bbox"]) if bg else (BX0, BY0, BX1, BY1)
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
    # 箭头以自身中心为旋转点：方向变化时中点在原地转，不往外甩
    tip = (cx + ux * L * 0.5, cy + uy * L * 0.5)
    tail = (cx - ux * L * 0.5, cy - uy * L * 0.5)

    d = ImageDraw.Draw(im)
    # 箭身画到箭头底部即可，避免粗线从三角头两侧露出
    head_len = H * 1.10
    head_half = H * 0.55
    base = (tip[0] - ux * head_len, tip[1] - uy * head_len)
    d.line([tail, base], fill=MOVE_ARROW, width=aw)
    # 箭头用实心三角，边缘锐利，放大也不糊
    tri = [tip,
           (base[0] + nx * head_half, base[1] + ny * head_half),
           (base[0] - nx * head_half, base[1] - ny * head_half)]
    d.polygon(tri, fill=MOVE_ARROW)


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
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
user32.SetCapture.argtypes = [wintypes.HWND]
user32.ReleaseCapture.argtypes = []
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
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_POPUP = 0x0000, 0x0800, 0x0008, 0x0010
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
ID_TOGGLE, ID_RELOAD, ID_QUIT, ID_PANEL = 1001, 1002, 1003, 1004
ID_POS_BASE, ID_MON_BASE = 1100, 1120     # 九宫格格位 1100..1108，显示器 1120..112F

CMD = {"toggle": False, "reload": False, "quit": False, "anchor": False,
       "panel": False, "repaint": False}
_TRAY = {"hwnd": None, "hicon": None, "icon": None, "on": False, "visible": True,
         "click_t": 0.0}

# 全局生效中的配置：命令行的默认值、ini 里存的值、控制面板里改的值，最后都落到这张表，
# 主循环每帧从这里读，所以面板改完不用重启。
CFG = {
    "layout": "extended",      # classic | extended
    "scale": 1.2,
    "cell": 6, "mon": 0, "margin": 40,     # cell: 九宫格格位，6 = 左下
    "bg": False,               # 画 SVG 的深色底板
    "mouse_yellow": False,     # 鼠标左右键常亮
    "solid": False,            # True = 取消点击穿透
    "hold": 0.03,              # 按下后至少点亮多久
    "wheel_on": True, "wheel_hold": 0.15,
    "move_on": True, "move_hold": 0.25,
    "pinned": False,           # True = 用 --pos 钉死位置，重绘后不再按格位回位
}
CFG_DEFAULTS = dict(CFG)
_POS = CFG                     # 旧代码里的 _POS[...] 继续可用（cell / mon 就在这张表里）
CELL_NAMES = ("↖ 左上", "↑ 上中", "↗ 右上",
              "← 左中", "● 居中", "→ 右中",
              "↙ 左下", "↓ 下中", "↘ 右下")
CELL_SHORT = ("左上", "上中", "右上", "左中", "居中", "右中", "左下", "下中", "右下")
CFG_FILE = os.path.join(HERE, "keyoverlay.ini")


# ---- 显示器枚举 --------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.HDC,
                                     ctypes.POINTER(RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p,
                                       MONITORENUMPROC, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]


def list_monitors():
    """返回 [(left, top, width, height), ...]，主屏永远排第一，其余按左边界排序。"""
    found = []

    def _cb(hmon, hdc, lprc, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            found.append((1 if (mi.dwFlags & 1) else 0, r.left, r.top,
                          r.right - r.left, r.bottom - r.top))
        return True

    try:
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    except Exception:
        _log_exc()
    if not found:
        return [(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))]
    found.sort(key=lambda m: (-m[0], m[1], m[2]))     # 主屏优先，再按位置
    return [m[1:] for m in found]


def anchor_xy(cell, cw, ch, screen, margin):
    """九宫格第 cell 格的"内容左上角"坐标。cw/ch 是内容尺寸，不含阴影留白。"""
    sx, sy, sw, sh = screen
    col, row = cell % 3, cell // 3
    dx = (margin if col == 0 else
          (sw - cw) // 2 if col == 1 else sw - cw - margin)
    dy = (margin if row == 0 else
          (sh - ch) // 2 if row == 1 else sh - ch - margin)
    return int(sx + dx), int(sy + dy)


CFG_KEYS = ("layout", "scale", "cell", "mon", "margin", "bg", "mouse_yellow",
            "solid", "hold", "wheel_on", "wheel_hold", "move_on", "move_hold")


def _cfg_cast(key, raw):
    """按默认值的类型解析 ini 里的字符串，坏值一律丢弃。"""
    d = CFG_DEFAULTS[key]
    raw = raw.strip()
    if isinstance(d, bool):
        return raw not in ("0", "false", "False", "no", "")
    if isinstance(d, int):
        return int(float(raw))
    if isinstance(d, float):
        return float(raw)
    return raw if raw in LAYOUT_FILES else d


def cfg_load():
    """读 keyoverlay.ini；文件不存在、缺项、值坏了都退回默认值。"""
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.partition("=")
                k = k.strip().lower()
                if sep and k in CFG_DEFAULTS:
                    try:
                        CFG[k] = _cfg_cast(k, v)
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    cfg_clamp()
    return CFG


def cfg_clamp():
    """把数值限制在安全范围里，免得手改 ini 写出一个点不亮的值。"""
    CFG["layout"] = CFG["layout"] if CFG["layout"] in LAYOUT_FILES else "extended"
    CFG["scale"] = max(0.5, min(3.0, float(CFG["scale"])))
    CFG["cell"] = max(0, min(8, int(CFG["cell"])))
    CFG["mon"] = max(0, int(CFG["mon"]))
    CFG["margin"] = max(0, min(400, int(CFG["margin"])))
    CFG["hold"] = max(0.02, min(0.5, float(CFG["hold"])))
    CFG["wheel_hold"] = max(0.05, min(1.5, float(CFG["wheel_hold"])))
    CFG["move_hold"] = max(0.05, min(1.5, float(CFG["move_hold"])))


def cfg_save():
    try:
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            f.write("# KeyOverlay 设置（控制面板或托盘菜单里改，一般不用手改）\n")
            for k in CFG_KEYS:
                v = CFG[k]
                if isinstance(v, bool):
                    v = 1 if v else 0
                f.write("%s=%s\n" % (k, v))
    except Exception:
        pass          # 放在只读目录时就不记了，不影响使用


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


def _build_menu():
    menu = user32.CreatePopupMenu()
    user32.AppendMenuW(menu, MF_STRING, ID_TOGGLE,
                       "隐藏浮层" if _TRAY["visible"] else "显示浮层")
    user32.AppendMenuW(menu, MF_STRING, ID_RELOAD, "重新加载设计稿 (svg)")
    user32.AppendMenuW(menu, MF_STRING, ID_PANEL, "控制面板…")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

    # --- 浮层位置：九宫格 + （多屏时）显示器选择 ---
    posmenu = user32.CreatePopupMenu()
    subs = [posmenu]
    for i, name in enumerate(CELL_NAMES):
        fl = MF_STRING | (MF_CHECKED if _POS["cell"] == i else 0)
        user32.AppendMenuW(posmenu, fl, ID_POS_BASE + i, name)
    try:
        mons = list_monitors()
    except Exception:
        mons = []
    if len(mons) > 1:
        user32.AppendMenuW(posmenu, MF_SEPARATOR, 0, None)
        monmenu = user32.CreatePopupMenu()
        subs.append(monmenu)
        for i in range(len(mons)):
            fl = MF_STRING | (MF_CHECKED if _POS["mon"] == i else 0)
            user32.AppendMenuW(monmenu, fl, ID_MON_BASE + i, "显示器 %d" % (i + 1))
        user32.AppendMenuW(posmenu, MF_POPUP, monmenu, "放在哪个屏幕")
    user32.AppendMenuW(menu, MF_POPUP, posmenu, "浮层位置")

    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")
    return menu, subs


def _menu_cmd(cmd):
    """菜单命令统一分发（弹菜单和 WM_COMMAND 两条路径共用）。"""
    if cmd == ID_TOGGLE:
        CMD["toggle"] = True
    elif cmd == ID_RELOAD:
        CMD["reload"] = True
    elif cmd == ID_PANEL:
        CMD["panel"] = True
    elif cmd == ID_QUIT:
        CMD["quit"] = True
    elif ID_POS_BASE <= cmd < ID_POS_BASE + 9:
        _POS["cell"] = cmd - ID_POS_BASE
        cfg_save()
        CMD["anchor"] = True
    elif ID_MON_BASE <= cmd < ID_MON_BASE + 16:
        _POS["mon"] = cmd - ID_MON_BASE
        cfg_save()
        CMD["anchor"] = True


def show_tray_menu(hwnd):
    menu, subs = _build_menu()
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)      # 否则点菜单外面不会自动关
    cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                                pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, 0x0000, 0, 0)
    user32.DestroyMenu(menu)
    for m in subs:                        # 子菜单是独立 HMENU，不单独销毁会漏 GDI 句柄
        user32.DestroyMenu(m)
    if cmd:
        _menu_cmd(cmd)


def _tray_wndproc(hwnd, msg, wparam, lparam):
    if msg == TRAY_MSG:
        if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
            show_tray_menu(hwnd)
        elif lparam == WM_LBUTTONDBLCLK:
            # 双击开控制面板；顺手把单击计时清掉，别让双击再顺带切一次显隐
            _TRAY["click_t"] = 0.0
            _TRAY["skip_until"] = time.time() + 0.8
            CMD["panel"] = True
        elif lparam == WM_LBUTTONUP:
            if time.time() >= _TRAY.get("skip_until", 0.0):
                _TRAY["click_t"] = time.time()
        return 0
    if msg == WM_COMMAND:
        _menu_cmd(wparam & 0xFFFF)
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


def reload_art(layout=None):
    """重新解析 SVG 并清空贴片/帧缓存。

    layout 给 None 表示"照当前文件重读"（托盘里的重新加载设计稿）；
    给 "classic"/"extended" 表示切换布局。
    """
    global ART, _BODY, _CONTENT, BX0, BY0, BX1, BY1, SVG_FILE, MOVE_CENTER
    if layout in LAYOUT_FILES:
        CFG["layout"] = layout
        SVG_FILE = LAYOUT_FILES[layout]
    ART = load_svg(SVG_FILE)
    _BODY = next((e for e in ART if e["id"] == "background"), None)
    _CONTENT = [e for e in ART if e["id"] != "background"]
    BX0 = min(e["bbox"][0] for e in _CONTENT)
    BY0 = min(e["bbox"][1] for e in _CONTENT)
    BX1 = max(e["bbox"][2] for e in _CONTENT)
    BY1 = max(e["bbox"][3] for e in _CONTENT)
    MOVE_CENTER = compute_move_center()
    _TILES.clear()
    _FRAMES.clear()


# ============================ 控制面板 ============================
# 自绘面板：Pillow 画好整张图 -> UpdateLayeredWindow 贴上去，点击靠一张矩形命中表。
# 控件只有四种原语（单选行 / 分段按钮 / 步进器 / 开关），全是矩形，不需要拖拽。
PANEL_CLASS = "KeyMouseOverlayPanel"
PANEL_W = 420          # 逻辑宽度（px @100%）
PANEL_PAD = 14         # 阴影留白
PANEL_SS = 3           # 绘制超采样倍数，缩小后有抗锯齿效果
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0201, 0x0202, 0x0200

PANEL = {"hwnd": None, "visible": False, "pos": (0, 0), "scale": 1.0,
         "hits": [], "drag": None, "items_h": 0}

# 面板配色：仿 Win11 浅色，跟浮层那套深色是两回事
PC = {
    "bg": (243, 243, 243, 255),
    "titlebar": (250, 250, 250, 255),
    "card": (251, 251, 251, 255),
    "border": (214, 214, 214, 255),
    "line": (233, 233, 233, 255),
    "text": (27, 27, 27, 255),
    "sub": (107, 107, 107, 255),
    "hint": (126, 126, 126, 255),
    "accent": (0, 103, 192, 255),
    "accent_bg": (232, 242, 252, 255),
    "white": (255, 255, 255, 255),
    "off": (198, 198, 198, 255),
    "danger": (179, 38, 30, 255),
    "ok": (46, 158, 91, 255),
    "btn": (253, 253, 253, 255),
    "btn_line": (203, 203, 203, 255),
}

_FONTS = {}


def pfont(size, bold=False):
    """微软雅黑足够接近 Segoe UI 的中文观感；找不到就退回默认位图字体。"""
    key = (size, bold)
    if key in _FONTS:
        return _FONTS[key]
    f = None
    for name in (("msyhbd.ttc", "msyh.ttc", "segoeuib.ttf") if bold
                 else ("msyh.ttc", "segoeui.ttf", "simhei.ttf")):
        try:
            f = ImageFont.truetype("C:/Windows/Fonts/" + name, size)
            break
        except Exception:
            continue
    if f is None:
        f = ImageFont.load_default()
    _FONTS[key] = f
    return f


def panel_layout():
    """把面板排一遍，返回 (绘制指令, 总高度, 命中表)。坐标都是逻辑像素。

    排完再画，而不是边画边算——这样高度是已知的，能一次开好画布。
    """
    W, PAD = PANEL_W, 16
    it, hits = [], []

    def rect(x0, y0, x1, y1, r=0, fill=None, outline=None, w=1):
        it.append(("rect", (x0, y0, x1, y1), r, fill, outline, w))

    def ell(x0, y0, x1, y1, fill=None, outline=None, w=1):
        it.append(("ellipse", (x0, y0, x1, y1), fill, outline, w))

    def txt(x, y, s, size=13, bold=False, fill=None, anchor="la"):
        it.append(("text", (x, y), s, size, bold, fill or PC["text"], anchor))

    def line(x0, y0, x1, y1, fill=None, w=1):
        it.append(("line", (x0, y0, x1, y1), fill or PC["line"], w))

    def hit(x0, y0, x1, y1, act, arg=None):
        hits.append((x0, y0, x1, y1, act, arg))

    # --- 标题栏（整条可拖动，右侧关闭）---
    # 先铺一张足够高的底色，最后会被圆角蒙版裁到真实高度
    rect(0, 0, W, 3000, fill=PC["bg"])
    rect(0, 0, W, 32, fill=PC["titlebar"])
    line(0, 31.5, W, 31.5)
    # 图标：圆角蓝底 + 六个白色小键帽，比直接贴深色 app.ico 更搭浅色标题栏
    rect(11, 9, 25, 23, r=4, fill=PC["accent"])
    for _r in range(2):
        for _c in range(3):
            _x = 12.7 + _c * 3.8
            _y = 13.0 + _r * 3.4
            rect(_x, _y, _x + 3, _y + 2.6, r=0.8, fill=(255, 255, 255, 240))
    txt(30, 16, "KeyOverlay 控制面板", 12, False, PC["sub"], "lm")
    hit(0, 0, W - 34, 32, "drag")
    hit(W - 34, 0, W, 32, "close")
    txt(W - 17, 16, "×", 16, False, PC["sub"], "mm")

    # --- 大标题 ---
    y = 32 + 14
    txt(PAD, y, "KeyOverlay 控制面板", 16, True)
    y += 23
    txt(PAD, y, "切换显示方案与反馈细节，改动立即生效", 12, False, PC["sub"])
    y += 17 + 13

    # --- 按键布局 ---
    txt(PAD, y, "按键布局", 11, False, PC["hint"])
    y += 15 + 6
    card1 = y
    rect(PAD, card1, W - PAD, card1 + 94, r=6, fill=PC["card"], outline=PC["border"])
    for i, (key, name, desc) in enumerate((
            ("classic", "经典布局", "Q W E · Shift A S D · Ctrl 空格"),
            ("extended", "扩展布局", "1-5 · Tab · Q-T · A-G · Z-B · Alt"))):
        ry = card1 + 5 + i * 42
        sel = CFG["layout"] == key
        if sel:
            ell(PAD + 12, ry + 15, PAD + 24, ry + 27, fill=PC["accent"])
            ell(PAD + 16.5, ry + 19.5, PAD + 19.5, ry + 22.5, fill=PC["white"])
        else:
            ell(PAD + 12, ry + 15, PAD + 24, ry + 27, fill=PC["white"],
                outline=(154, 154, 154, 255))
        txt(PAD + 30, ry + 7, name, 13)
        txt(PAD + 30, ry + 25, desc, 11, False, PC["sub"])
        if i:
            line(PAD + 12, ry, W - PAD - 12, ry)
        hit(PAD, ry, W - PAD, ry + 42, "layout", key)
    y = card1 + 94 + 12

    # --- 浮层缩放 ---
    txt(PAD, y, "浮层缩放", 11, False, PC["hint"])
    y += 15 + 6
    gap = 5.0
    bw = (W - 2 * PAD - 3 * gap) / 4.0
    for i, v in enumerate((0.8, 1.0, 1.2, 1.5)):
        bx = PAD + i * (bw + gap)
        sel = abs(CFG["scale"] - v) < 0.01
        rect(bx, y, bx + bw, y + 30, r=5,
             fill=PC["accent_bg"] if sel else PC["white"],
             outline=PC["accent"] if sel else PC["border"])
        txt(bx + bw / 2, y + 15, "%.1f" % v, 12, False,
            PC["accent"] if sel else PC["text"], "mm")
        hit(bx, y, bx + bw, y + 30, "scale", v)
    y += 30 + 12

    # --- 显示位置：九宫格 + 边距 + 穿透 ---
    txt(PAD, y, "显示位置", 11, False, PC["hint"])
    y += 15 + 6
    gx, gy = PAD, y
    for i in range(9):
        bx = gx + (i % 3) * 30
        by = gy + (i // 3) * 24
        sel = CFG["cell"] == i
        rect(bx, by, bx + 26, by + 20, r=3,
             fill=PC["accent_bg"] if sel else PC["white"],
             outline=PC["accent"] if sel else PC["border"])
        hit(bx, by, bx + 26, by + 20, "cell", i)

    rx = gx + 26 * 3 + 8 * 2 + 16
    txt(rx, gy + 3, "边距", 12)
    rect(rx + 32, gy, rx + 54, gy + 20, r=3, fill=PC["white"], outline=PC["border"])
    txt(rx + 43, gy + 10, "−", 13, False, PC["text"], "mm")
    hit(rx + 32, gy, rx + 54, gy + 20, "margin", -5)
    txt(rx + 72, gy + 10, "%d" % CFG["margin"], 12, False, PC["text"], "mm")
    rect(rx + 90, gy, rx + 112, gy + 20, r=3, fill=PC["white"], outline=PC["border"])
    txt(rx + 101, gy + 10, "+", 13, False, PC["text"], "mm")
    hit(rx + 90, gy, rx + 112, gy + 20, "margin", 5)

    # 点击穿透：开着 = 浮层不吃鼠标
    through = not CFG["solid"]
    ty = gy + 30
    tx0, tx1 = rx, rx + 30
    rect(tx0, ty, tx1, ty + 16, r=8, fill=PC["accent"] if through else PC["off"])
    kx = tx1 - 14 if through else tx0 + 2
    ell(kx, ty + 2, kx + 12, ty + 14, fill=PC["white"])
    txt(rx + 38, ty + 8, "点击穿透", 12, False, PC["text"], "lm")
    hit(tx0, ty - 2, rx + 98, ty + 18, "through")
    y = gy + 24 * 3 + 12

    # --- 反馈 ---
    txt(PAD, y, "反馈", 11, False, PC["hint"])
    y += 15 + 6
    card2 = y
    rect(PAD, card2, W - PAD, card2 + 110, r=6, fill=PC["card"], outline=PC["border"])
    rows = (("wheel_on", "滚轮方向箭头", "wheel_hold", 0.15),
            ("move_on", "鼠标移动箭头", "move_hold", 0.25),
            ("bg", "深色底板", None, None))
    vx = W - PAD - 12
    for i, (flag, label, hkey, hstep) in enumerate(rows):
        ry = card2 + 5 + i * 34
        on = bool(CFG[flag])
        ox0, ox1 = PAD + 12, PAD + 42
        rect(ox0, ry + 9, ox1, ry + 25, r=8, fill=PC["accent"] if on else PC["off"])
        kx = ox1 - 14 if on else ox0 + 2
        ell(kx, ry + 11, kx + 12, ry + 23, fill=PC["white"])
        txt(PAD + 50, ry + 17, label, 12, False, PC["text"], "lm")
        hit(ox0, ry + 5, PAD + 46 + 70, ry + 29, flag)
        if i:
            line(PAD + 12, ry, W - PAD - 12, ry)
        if hkey:
            rect(vx - 22, ry + 7, vx, ry + 27, r=3, fill=PC["white"],
                 outline=PC["border"])
            txt(vx - 11, ry + 17, "+", 13, False, PC["text"], "mm")
            hit(vx - 22, ry + 7, vx, ry + 27, hkey, hstep)
            txt(vx - 42, ry + 17, "%.2fs" % CFG[hkey], 11, False, PC["sub"], "mm")
            rect(vx - 84, ry + 7, vx - 62, ry + 27, r=3, fill=PC["white"],
                 outline=PC["border"])
            txt(vx - 73, ry + 17, "−", 13, False, PC["text"], "mm")
            hit(vx - 84, ry + 7, vx - 62, ry + 27, hkey, -hstep)
    y = card2 + 110 + 12

    # --- 状态行 ---
    vis = _TRAY["visible"]
    state = "当前：%s · %.1f× · %s · %s" % (
        LAYOUT_NAMES[CFG["layout"]], CFG["scale"],
        CELL_SHORT[CFG["cell"]], "浮层显示中" if vis else "浮层已隐藏")
    ell(PAD + 1, y + 5, PAD + 7, y + 11, fill=PC["ok"] if vis else PC["off"])
    txt(PAD + 14, y + 8, state, 11, False, PC["sub"], "lm")
    y += 16 + 10

    # --- 底部按钮 ---
    bw2 = (W - 2 * PAD - 8) / 2.0
    rect(PAD, y, PAD + bw2, y + 34, r=5, fill=PC["btn"], outline=PC["btn_line"])
    txt(PAD + bw2 / 2, y + 17, "隐藏浮层" if vis else "显示浮层", 12, False,
        PC["text"], "mm")
    hit(PAD, y, PAD + bw2, y + 34, "togglewin")
    rect(PAD + bw2 + 8, y, W - PAD, y + 34, r=5, fill=PC["btn"],
         outline=PC["btn_line"])
    txt(PAD + bw2 + 8 + bw2 / 2, y + 17, "退出程序", 12, False, PC["danger"], "mm")
    hit(PAD + bw2 + 8, y, W - PAD, y + 34, "quit")
    y += 34 + 14
    return it, y, hits


def panel_scale_for(h):
    """面板按系统 DPI 放大；屏幕太矮时整体缩一档，保证整块放得下。"""
    try:
        hdc = user32.GetDC(None)
        dpi = gdi32.GetDeviceCaps(hdc, 88)      # LOGPIXELSX
        user32.ReleaseDC(None, hdc)
        s = max(1.0, dpi / 96.0)
    except Exception:
        s = 1.0
    mons = list_monitors()
    if mons:
        sh = mons[0][3]
        if h * s > sh * 0.86:
            s = max(0.78, sh * 0.86 / h)
    return s


def _panel_draw_img(items, h, s):
    """把指令画成一张 RGBA 图（先按 s*超采样 画，再缩小）。

    超采样总倍率封顶 4：高 DPI 下 s 本身就有 2 以上，再乘 3 会让绘制量翻四倍，
    点一下要等两百多毫秒。4 倍已经看不出锯齿了。
    """
    ss = min(PANEL_SS * s, 4.0)
    W = PANEL_W
    im = Image.new("RGBA", (max(1, int(round(W * ss))), max(1, int(round(h * ss)))),
                   (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def B(b):
        return [b[0] * ss, b[1] * ss, b[2] * ss, b[3] * ss]

    for item in items:
        kind = item[0]
        if kind == "rect":
            _, box, r, fill, outline, w = item
            d.rounded_rectangle(B(box), radius=max(0, r) * ss, fill=fill,
                                outline=outline, width=max(1, int(round(w * ss))))
        elif kind == "ellipse":
            _, box, fill, outline, w = item
            d.ellipse(B(box), fill=fill, outline=outline,
                      width=max(1, int(round(w * ss))))
        elif kind == "line":
            _, box, fill, w = item
            d.line(B(box), fill=fill, width=max(1, int(round(w * ss))))
        elif kind == "text":
            _, (x, y), s_text, size, bold, fill, anchor = item
            d.text((x * ss, y * ss), s_text, font=pfont(max(8, int(round(size * ss))), bold),
                   fill=fill, anchor=anchor)

    # 圆角裁剪
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, im.width - 1, im.height - 1], radius=int(8 * ss), fill=255)
    im.putalpha(mask)

    out = im.resize((max(1, int(round(W * s))), max(1, int(round(h * s)))),
                    Image.LANCZOS)
    return _panel_shadow(out)


def _panel_shadow(im):
    """面板自己的投影：比浮层那套更宽更淡，看起来才像一扇窗口。"""
    m = PANEL_PAD
    canvas = Image.new("RGBA", (im.width + 2 * m, im.height + 2 * m), (0, 0, 0, 0))
    alpha = im.getchannel("A").point(lambda v: int(v * 0.42))
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.putalpha(alpha)
    sh = sh.filter(ImageFilter.GaussianBlur(7))
    canvas.alpha_composite(sh, (m, m + 3))
    canvas.alpha_composite(im, (m, m))
    return canvas


def panel_redraw():
    """重画面板并贴上去。面板没开就什么都不做。"""
    hwnd = PANEL["hwnd"]
    if not hwnd or not PANEL["visible"]:
        return
    try:
        items, h, hits = panel_layout()
        s = PANEL["scale"] or panel_scale_for(h)
        im = _panel_draw_img(items, h, s)
        PANEL["hits"] = hits
        PANEL["items_h"] = h
        px, py = PANEL["pos"]
        blit(hwnd, im, int(px - PANEL_PAD), int(py - PANEL_PAD))
    except Exception:
        _log_exc()


def panel_hit(mx, my):
    """mx/my 是窗口内物理像素；扣掉阴影留白后换算回逻辑坐标再查表。"""
    s = PANEL["scale"] or 1.0
    lx = (mx - PANEL_PAD) / s
    ly = (my - PANEL_PAD) / s
    for x0, y0, x1, y1, act, arg in PANEL["hits"]:
        if x0 <= lx < x1 and y0 <= ly < y1:
            return act, arg
    return None, None


def panel_action(act, arg):
    if act == "drag":
        return
    if act == "close":
        panel_hide()
        return
    if act == "togglewin":
        CMD["toggle"] = True
        return
    if act == "quit":
        CMD["quit"] = True
        return

    need_repaint = True
    if act == "layout":
        reload_art(arg)
    elif act == "scale":
        CFG["scale"] = arg
    elif act == "cell":
        CFG["cell"] = arg
        CFG["pinned"] = False      # 面板里选了格位，就别再守着 --pos 的固定坐标
    elif act == "margin":
        CFG["margin"] = max(0, min(400, CFG["margin"] + arg))
        CFG["pinned"] = False
    elif act == "through":
        CFG["solid"] = not CFG["solid"]
    elif act in ("wheel_on", "move_on", "bg"):
        CFG[act] = not CFG[act]
    elif act in ("wheel_hold", "move_hold"):
        CFG[act] = round(max(0.05, min(1.5, CFG[act] + arg)), 2)
    else:
        need_repaint = False

    if need_repaint:
        cfg_save()
        CMD["repaint"] = True
    panel_redraw()


def _panel_wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_LBUTTONDOWN:
        mx = ctypes.c_short(lparam & 0xFFFF).value
        my = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        act, arg = panel_hit(mx, my)
        if act == "drag":
            PANEL["drag"] = get_cursor()
            user32.SetCapture(hwnd)
        elif act:
            panel_action(act, arg)
        return 0
    if msg == WM_MOUSEMOVE and PANEL["drag"]:
        cx, cy = get_cursor()
        dx, dy = cx - PANEL["drag"][0], cy - PANEL["drag"][1]
        PANEL["drag"] = (cx, cy)
        px, py = PANEL["pos"]
        PANEL["pos"] = (px + dx, py + dy)
        user32.SetWindowPos(hwnd, None, int(px + dx - PANEL_PAD),
                            int(py + dy - PANEL_PAD), 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return 0
    if msg == WM_LBUTTONUP:
        if PANEL["drag"]:
            PANEL["drag"] = None
            user32.ReleaseCapture()
        return 0
    if msg == 0x0010:              # WM_CLOSE -> 只是关面板，不退出程序
        panel_hide()
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_panel_proc_ref = []


def create_panel_window():
    hinst = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(_panel_wndproc)
    _panel_proc_ref.append(proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, 32512)
    wc.lpszClassName = PANEL_CLASS
    user32.RegisterClassExW(ctypes.byref(wc))
    # 不加 WS_EX_TRANSPARENT（要收点击），也不加 WS_EX_APPWINDOW（不进任务栏）
    ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    hwnd = user32.CreateWindowExW(ex, PANEL_CLASS, "KeyOverlay 控制面板", WS_POPUP,
                                  0, 0, 10, 10, None, None, hinst, None)
    PANEL["hwnd"] = hwnd
    return hwnd


def panel_show():
    hwnd = PANEL["hwnd"] or create_panel_window()
    if not hwnd:
        return
    items, h, hits = panel_layout()
    s = panel_scale_for(h)
    PANEL["scale"] = s
    mons = list_monitors()
    sx, sy, sw, sh = mons[min(CFG["mon"], len(mons) - 1)]
    pw, ph = PANEL_W * s, h * s
    PANEL["pos"] = (int(sx + (sw - pw) / 2), int(sy + (sh - ph) / 2))
    PANEL["visible"] = True
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    panel_redraw()
    panel_topmost()


def panel_hide():
    if PANEL["hwnd"]:
        user32.ShowWindow(PANEL["hwnd"], 0)
    PANEL["visible"] = False
    PANEL["drag"] = None


def panel_toggle():
    panel_hide() if PANEL["visible"] else panel_show()


def panel_topmost():
    if PANEL["hwnd"]:
        user32.SetWindowPos(PANEL["hwnd"], ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


GWL_EXSTYLE = -20
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]


def apply_solid(hwnd, solid):
    """点击穿透开关：加/去掉 WS_EX_TRANSPARENT，面板里一改就生效。"""
    try:
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new = (ex & ~WS_EX_TRANSPARENT) if solid else (ex | WS_EX_TRANSPARENT)
        if new != ex:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
    except Exception:
        _log_exc()


# ============================ 主流程 ============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", default=None, help="左上角坐标 x,y（覆盖配置文件里的格位）")
    ap.add_argument("--layout", default=argparse.SUPPRESS, choices=("classic", "extended"),
                    help="布局：classic=原版 9 键，extended=25 键（默认）")
    ap.add_argument("--margin", type=int, default=argparse.SUPPRESS)
    ap.add_argument("--scale", type=float, default=argparse.SUPPRESS)
    ap.add_argument("--bg", action="store_true", default=argparse.SUPPRESS,
                    help="连 SVG 的黑色底板一起画")
    ap.add_argument("--mouse-yellow", action="store_true", default=argparse.SUPPRESS,
                    help="鼠标左右键常亮黄色")
    ap.add_argument("--solid", action="store_true", default=argparse.SUPPRESS,
                    help="取消点击穿透")
    ap.add_argument("--preview", default=None, help="只渲染一张静态图到指定路径")
    ap.add_argument("--pressed", default="W,LMB", help="预览时高亮的键，逗号分隔")
    ap.add_argument("--hold", type=float, default=argparse.SUPPRESS,
                    help="按下后至少点亮多久（秒）。调小=连点更容易分成两下，调大=更快的手点也能看清")
    ap.add_argument("--no-tray", action="store_true", help="不创建系统托盘图标")
    ap.add_argument("--debug", action="store_true", help="把状态变化写进 keyoverlay.log，排查连点用")
    ap.add_argument("--poll", type=float, default=0.006,
                    help="轮询间隔（秒），默认 0.006")
    ap.add_argument("--wheel-hold", type=float, default=argparse.SUPPRESS,
                    help="滚轮滚动后至少点亮多久（秒）。滚动是瞬时事件，比 --hold 长才看得清")
    ap.add_argument("--no-wheel", action="store_true", default=argparse.SUPPRESS,
                    help="不采集滚轮滚动（不注册 Raw Input，零开销）")
    ap.add_argument("--move-hold", type=float, default=argparse.SUPPRESS,
                    help="鼠标停止后方向箭头再保留多久（秒），默认 0.25")
    ap.add_argument("--no-move", action="store_true", default=argparse.SUPPRESS,
                    help="不显示鼠标移动方向箭头")
    ap.add_argument("--move-deg", type=float, default=None,
                    help="预览时移动箭头的角度（0=向右，90=向上，180=向左，270=向下）")
    ap.add_argument("--toggle-vk", default="0x78", help="隐藏/显示热键的虚拟键码（默认 0x78 = F9）")
    ap.add_argument("--quit-vk", default="0x79,0x7A",
                    help="退出热键的虚拟键码，可写多个（默认 0x79,0x7A = F10 或 F11；0x7A 其实是 F11）")
    args = ap.parse_args()

    # 配置优先级：内置默认 < keyoverlay.ini < 命令行显式参数
    cfg_load()
    if hasattr(args, "layout"):
        CFG["layout"] = args.layout
    if hasattr(args, "scale"):
        CFG["scale"] = args.scale
    if hasattr(args, "margin"):
        CFG["margin"] = args.margin
    if hasattr(args, "hold"):
        CFG["hold"] = args.hold
    if hasattr(args, "wheel_hold"):
        CFG["wheel_hold"] = args.wheel_hold
    if hasattr(args, "move_hold"):
        CFG["move_hold"] = args.move_hold
    if getattr(args, "bg", False):
        CFG["bg"] = True
    if getattr(args, "mouse_yellow", False):
        CFG["mouse_yellow"] = True
    if getattr(args, "solid", False):
        CFG["solid"] = True
    if getattr(args, "no_wheel", False):
        CFG["wheel_on"] = False
    if getattr(args, "no_move", False):
        CFG["move_on"] = False
    cfg_clamp()

    if args.preview:
        reload_art(CFG["layout"])
        hit = tuple(v.strip().upper() for v in args.pressed.split(",") if v.strip())
        mv = -1 if args.move_deg is None else \
            int(round(args.move_deg / (360.0 / MOVE_DIRS))) % MOVE_DIRS
        im = render(pressed=hit, scale=CFG["scale"], bg=CFG["bg"],
                    mouse_yellow=CFG["mouse_yellow"], move=mv)
        bg = Image.new("RGBA", im.size, (32, 38, 52, 255))
        bg.alpha_composite(im)
        bg.convert("RGB").save(args.preview)
        print("已生成预览:", args.preview, im.size, LAYOUT_NAMES[CFG["layout"]])
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

    reload_art(CFG["layout"])
    img = render(scale=CFG["scale"], bg=CFG["bg"], mouse_yellow=CFG["mouse_yellow"])
    w, h = img.size
    pad = SHADOW_MARGIN if SHADOW else 0
    if args.pos:
        CFG["pinned"] = True        # 钉死了位置，之后重绘就不再按格位回位
        x, y = [int(v) for v in args.pos.split(",")]
        x -= pad
        y -= pad
    else:
        mons = list_monitors()
        sc = mons[min(CFG["mon"], len(mons) - 1)]
        cx, cy = anchor_xy(CFG["cell"], w - 2 * pad, h - 2 * pad, sc, CFG["margin"])
        x, y = cx - pad, cy - pad

    hwnd = create_window(w, h, x, y, clickthrough=not CFG["solid"])
    if not hwnd:
        print("创建窗口失败，错误码:", ctypes.get_last_error())
        return 1

    # 这里只决定要不要注册 Raw Input（--no-wheel 才真正不注册）；显不显示看 CFG["wheel_on"]，
    # 面板里随时能开回来，所以不能因为 ini 里关过就干脆不注册
    wheel_on = False if getattr(args, "no_wheel", False) else enable_wheel(hwnd)

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
    POLL = max(0.002, args.poll)
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

    def place():
        """按 CFG 里的格位算出窗口左上角（已含阴影留白的偏移）。"""
        mons = list_monitors()
        if CFG["mon"] >= len(mons):
            CFG["mon"] = 0
        sc = mons[CFG["mon"]]
        cx, cy = anchor_xy(CFG["cell"], w - 2 * pad, h - 2 * pad, sc, CFG["margin"])
        return cx - pad, cy - pad

    print("已启动：%s · %dx%d @ (%d,%d)   F9 隐藏/显示，F10 退出"
          % (LAYOUT_NAMES[CFG["layout"]], w, h, x, y))
    print("滚轮反馈：%s" % ("已启用（Raw Input 只读，不拦截）" if wheel_on else "未启用"))

    try:
        while True:
            if not pump():
                break
            if h_event and kernel32.WaitForSingleObject(h_event, 0) == 0:
                break
            now = time.time()
            hold = max(0.02, CFG["hold"])
            wheel_hold = max(0.05, CFG["wheel_hold"])
            move_hold = max(0.05, CFG["move_hold"])
            cur = get_cursor()
            mdx, mdy = cur[0] - last_cursor[0], cur[1] - last_cursor[1]
            last_cursor = cur
            if mdx or mdy:
                # 指数平滑：单次采样当方向会让手抖带着箭头乱摆
                _MOVE["dx"] = _MOVE["dx"] * MOVE_DECAY + mdx
                _MOVE["dy"] = _MOVE["dy"] * MOVE_DECAY + mdy
                _MOVE["t"] = now
            move = -1
            if CFG["move_on"] and now - _MOVE["t"] < move_hold and \
                    abs(_MOVE["dx"]) + abs(_MOVE["dy"]) >= MOVE_MIN:
                deg = math.degrees(math.atan2(-_MOVE["dy"], _MOVE["dx"]))
                move = int(round(deg / (360.0 / MOVE_DIRS))) % MOVE_DIRS

            down = {n for n, vk in POLL_KEYS if is_down(vk)}
            down |= {b[0] for b in MOUSE_BTNS if is_down(b[1])}
            for n in down:
                last_seen[n] = now
            state = frozenset(n for n, t in last_seen.items() if now - t < hold)
            if CFG["wheel_on"] and wheel_on and now - _WHEEL["t"] < wheel_hold:
                state = state | {"WHEEL_UP" if _WHEEL["dir"] > 0 else "WHEEL_DOWN"}

            changed = state != prev_state
            # 按键变化立即响应；只有方向变了才受最小间隔约束
            if changed or (move != prev_move and now - last_draw >= MIN_DRAW_GAP):
                if dbg_f and changed:
                    dbg_f.write("%.3f %s\n" % (now, ",".join(sorted(state)) or "-"))
                    dbg_f.flush()
                if visible:
                    try:
                        img = render(pressed=state, scale=CFG["scale"], bg=CFG["bg"],
                                     mouse_yellow=CFG["mouse_yellow"], move=move)
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

            # 面板里改过设置：重渲染 + 按格位重定位（尺寸可能变了）
            if CMD["repaint"]:
                CMD["repaint"] = False
                try:
                    apply_solid(hwnd, CFG["solid"])
                    img = render(pressed=prev_state, scale=CFG["scale"], bg=CFG["bg"],
                                 mouse_yellow=CFG["mouse_yellow"],
                                 move=prev_move if CFG["move_on"] else -1)
                    w, h = img.size
                    if not CFG["pinned"]:
                        x, y = place()
                    user32.SetWindowPos(hwnd, None, x, y, w, h,
                                        SWP_NOZORDER | SWP_NOACTIVATE)
                    if visible:
                        blit(hwnd, img, x, y)
                    print("设置已应用：%s · %.1f× · %s · %dpx → (%d,%d)"
                          % (LAYOUT_NAMES[CFG["layout"]], CFG["scale"],
                             CELL_SHORT[CFG["cell"]], CFG["margin"], x, y))
                except Exception:
                    _log_exc()
                if PANEL["visible"]:
                    panel_redraw()

            if CMD["reload"]:
                CMD["reload"] = False
                try:
                    reload_art()
                except Exception:
                    _log_exc()
                img = render(pressed=prev_state, scale=CFG["scale"],
                             bg=CFG["bg"], mouse_yellow=CFG["mouse_yellow"])
                if img.size != (w, h):
                    w, h = img.size
                    user32.SetWindowPos(hwnd, None, x, y, w, h,
                                        SWP_NOZORDER | SWP_NOACTIVATE)
                blit(hwnd, img, x, y)

            if CMD["anchor"]:
                CMD["anchor"] = False
                try:
                    x, y = place()
                    user32.SetWindowPos(hwnd, None, x, y, w, h,
                                        SWP_NOZORDER | SWP_NOACTIVATE)
                    if visible:
                        blit(hwnd, img, x, y)
                    print("浮层位置：%s · 显示器%d → (%d,%d)"
                          % (CELL_NAMES[CFG["cell"]], CFG["mon"] + 1, x, y))
                except Exception:
                    _log_exc()

            if CMD["panel"]:
                CMD["panel"] = False
                panel_toggle()

            # 托盘单击延时一拍：确认不是双击再切显示/隐藏
            if _TRAY["click_t"] and now - _TRAY["click_t"] > 0.28:
                _TRAY["click_t"] = 0.0
                CMD["toggle"] = True

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
                tray_set_tip("键鼠显示（显示中）· 单击隐藏 · 双击控制面板 · 右键菜单" if visible
                             else "键鼠显示（已隐藏）· 单击显示 · 双击控制面板 · 右键菜单")
                print("显示" if visible else "隐藏")
                if PANEL["visible"]:
                    panel_redraw()
            if f10 and not prev_f10:
                print("退出")
                break
            prev_f9, prev_f10 = f9, f10

            # 有些游戏会抢置顶，把浮层压到下面；隔一秒重申一次
            if now - last_top > TOPMOST_REFRESH:
                user32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                panel_topmost()
                last_top = now
            time.sleep(POLL)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            tray_remove()
        except Exception:
            pass
        if PANEL["hwnd"]:
            try:
                user32.DestroyWindow(PANEL["hwnd"])
            except Exception:
                pass
        user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
