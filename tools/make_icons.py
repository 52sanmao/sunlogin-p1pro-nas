#!/usr/bin/env python3
"""生成飞牛应用图标（ICON.PNG / ICON_256.PNG / app/ui/images/*）。

设计取自面板自身的配色：
  背景  —— #ff9a3c → #e2570a 的橙色渐变（对应 CSS 里的 --orange #f97316）
  主体  —— 白色插线板轮廓 + 四个深橙插孔，缩小到 64px 仍然能认出是插排

    python3 tools/make_icons.py

只依赖 Pillow（打包时才跑，运行时不依赖）。
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter

S = 2048          # 绘制尺寸（4 倍超采样，最后缩到 512 再往下缩）
OUT = 512         # 主图尺寸

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOP = (255, 154, 60)
BOTTOM = (226, 87, 10)
HOLE = (176, 68, 8)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(size):
    img = Image.new("RGB", (1, size), TOP)
    px = img.load()
    for y in range(size):
        px[0, y] = _lerp(TOP, BOTTOM, (y / max(1, size - 1)) ** 0.85)
    return img.resize((size, size), Image.BILINEAR)


def main():
    bg = _gradient(S).convert("RGBA")

    # 圆角遮罩：iOS / 飞牛应用中心常见的超椭圆近似，圆角 22.6%
    radius = int(S * 0.226)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=255)
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    # 左上角一层柔和高光，避免大面积纯色发闷
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).ellipse(
        [-S * 0.45, -S * 0.75, S * 0.95, S * 0.42], fill=54)
    gloss = gloss.filter(ImageFilter.GaussianBlur(S * 0.08))
    canvas.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), (0, 0),
                 Image.composite(gloss, Image.new("L", (S, S), 0), mask))

    # ---- 插线板主体 ----
    sw, sh = int(S * 0.60), int(S * 0.32)
    cx, cy = S // 2, int(S * 0.5)
    box = [cx - sw // 2, cy - sh // 2, cx + sw // 2, cy + sh // 2]
    rad = int(sh * 0.34)

    # 投影
    shadow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [box[0], box[1] + int(S * 0.018), box[2], box[3] + int(S * 0.018)],
        radius=rad, fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.022))
    canvas.paste(Image.new("RGBA", (S, S), HOLE + (255,)), (0, 0), shadow)

    draw.rounded_rectangle(box, radius=rad, fill=(255, 255, 255, 255))

    # ---- 四个插孔 ----
    n = 4
    inset = int(sw * 0.135)
    usable = sw - inset * 2
    slot_w, slot_h = int(S * 0.038), int(sh * 0.44)
    gap = (usable - slot_w * n) / (n - 1) if n > 1 else 0
    for i in range(n):
        x = box[0] + inset + i * (slot_w + gap)
        y = cy - slot_h // 2
        draw.rounded_rectangle(
            [x, y, x + slot_w, y + slot_h],
            radius=slot_w // 2,
            fill=HOLE + (255,),
        )

    # ---- 电源线：从右端引出的平滑尾巴，暗示这是插排而不是移动电源 ----
    # 用二次贝塞尔逐点画圆，避免折线出现"天线"那样的硬拐角。
    cable = int(S * 0.042)
    r = cable / 2.0
    p0 = (box[2] - int(S * 0.010), cy)
    pc = (box[2] + int(S * 0.085), cy + int(S * 0.015))
    p1 = (box[2] + int(S * 0.045), cy + int(S * 0.235))
    steps = 160
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * pc[0] + t * t * p1[0]
        y = u * u * p0[1] + 2 * u * t * pc[1] + t * t * p1[1]
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255))

    master = canvas.resize((OUT, OUT), Image.LANCZOS)

    targets = [
        ("ICON.PNG", 64),
        ("ICON_256.PNG", 256),
        (os.path.join("app", "ui", "images", "icon_64.png"), 64),
        (os.path.join("app", "ui", "images", "icon_256.png"), 256),
    ]
    for rel, size in targets:
        path = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        master.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
        print("  %-32s %dx%d" % (rel, size, size))


if __name__ == "__main__":
    main()
