# -*- coding: utf-8 -*-
"""make_icon.py —— 把任意一张图片做成 Windows 用的 app.ico。

用法：
    python make_icon.py                    # 用 app_icon_src.png
    python make_icon.py 我的图.png
    python make_icon.py 我的图.png out.ico

做法：
* 居中裁成正方形（原图不是正方形时不会拉伸变形）；
* 大尺寸（>=48）用完整构图，小尺寸（<=32）自动裁到中间 66% 并锐化——
  这样 16px 的任务栏图标不会糊成一团；
* 输出 256/128/64/48/32/24/16 七个尺寸的多分辨率 ico。

换完图标记得重新打包：双击 build_exe.bat。
"""
import os
import sys

from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SIZES = [256, 128, 64, 48, 32, 24, 16]


def build(src, dst):
    im = Image.open(src).convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2,
                  (w - side) // 2 + side, (h - side) // 2 + side))
    im = im.resize((1024, 1024), Image.LANCZOS)

    s = im.size[0]
    n = int(s * 0.66)
    o = (s - n) // 2
    small = im.crop((o, o, o + n, o + n))

    frames = {}
    for size in SIZES:
        base = small if size <= 32 else im
        f = base.resize((size, size), Image.LANCZOS)
        if size <= 32:
            f = f.filter(ImageFilter.UnsharpMask(radius=1.0, percent=140, threshold=2))
        frames[size] = f

    frames[256].save(dst, format="ICO", sizes=[(x, x) for x in SIZES])
    return frames


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "app_icon_src.png")
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "app.ico")
    if not os.path.exists(src):
        print("找不到图片：", src)
        return 1
    build(src, dst)
    print("已生成 %s（%s）" % (dst, ", ".join("%dpx" % s for s in SIZES)))
    print("接着双击 build_exe.bat 重新打包即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
